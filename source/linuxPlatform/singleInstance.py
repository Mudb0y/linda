"""One NVDA per display, and how a new one takes over from the one running.

On Windows nvda.pyw finds the running copy by its message window, posts it WM_QUIT, waits for its
process and kills it if it lingers, and then waits on a named mutex. Here one file per display under
XDG_RUNTIME_DIR does all of that: the running copy holds an flock on it for as long as the process
lives and records its process ID in it, and a new copy asks that process to quit with SIGTERM. The
kernel drops the lock only when the process is gone, so the lock is held until the very last thing
the old copy does, as the mutex is on Windows.
"""

import errno
import fcntl
import os
import re
import select
import signal
import tempfile
import time

#: Seconds the running copy has to exit once asked, before it is killed; nvda.pyw's wait.
QUIT_TIMEOUT = 4.0
#: Seconds a killed copy has to be gone; nvda.pyw's wait after TerminateProcess.
KILL_TIMEOUT = 2.0
#: Seconds to wait for the lock, as nvda.pyw waits for the prior copy's mutex.
LOCK_TIMEOUT = 2.0
_POLL_INTERVAL = 0.05


def _runtimeDir() -> str:
	path = os.environ.get("XDG_RUNTIME_DIR", "")
	if os.path.isabs(path):
		return path
	# The base directory specification asks for a stand-in with the same guarantees when the session
	# sets none: owned by the user and closed to everyone else.
	path = os.path.join(tempfile.gettempdir(), f"basedreader-runtime-{os.getuid()}")
	try:
		os.mkdir(path, 0o700)
	except FileExistsError:
		pass
	info = os.lstat(path)
	if info.st_uid != os.getuid() or info.st_mode & 0o077 or not os.path.isdir(path):
		raise PermissionError(f"{path} is not a private directory of this user")
	return path


def lockFilePath() -> str:
	"""The lock file for the display this process would run on.

	XDG_RUNTIME_DIR is per user, not per session, so the display is in the name: a user signed in
	on two displays runs a copy on each.
	"""
	display = os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY") or "none"
	# :0 and :0.0 are one display; only the screen differs.
	display = re.sub(r"\.\d+$", "", display).replace("/", "_")
	directory = os.path.join(_runtimeDir(), "basedreader")
	os.makedirs(directory, mode=0o700, exist_ok=True)
	return os.path.join(directory, f"display{display}.lock")


class InstanceLock:
	"""The lock file of one display, opened by this process."""

	def __init__(self, path: str) -> None:
		self.path = path
		# Not inherited by any program NVDA starts, which would otherwise hold the lock after NVDA exits.
		self._fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
		self.isHeld = False

	def tryAcquire(self) -> bool:
		"""Take the lock if no other process holds it, without waiting."""
		try:
			fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
		except OSError as e:
			if e.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
				return False
			raise
		self.isHeld = True
		return True

	def acquire(self, timeout: float = LOCK_TIMEOUT) -> bool:
		"""Take the lock, waiting up to timeout seconds for the copy holding it to exit, and record
		this process in the file."""
		deadline = time.monotonic() + timeout
		while not self.tryAcquire():
			if time.monotonic() >= deadline:
				return False
			time.sleep(_POLL_INTERVAL)
		os.ftruncate(self._fd, 0)
		os.pwrite(self._fd, f"{os.getpid()}\n".encode(), 0)
		return True

	def holder(self) -> int | None:
		"""The process ID recorded by the copy holding the lock, if it has recorded one yet."""
		data = os.pread(self._fd, 32, 0)
		try:
			processID = int(data.decode("ascii").strip())
		except (UnicodeDecodeError, ValueError):
			return None
		return processID if processID > 0 else None


def _waitForExit(pidfd: int, timeout: float) -> bool:
	poller = select.poll()
	poller.register(pidfd, select.POLLIN)
	return bool(poller.poll(int(timeout * 1000)))


def _waitForHolder(lock: InstanceLock) -> int | None:
	"""The process ID the holder of the lock recorded, or None once the lock is this process's."""
	# A copy records itself right after taking the lock, so an empty file means it is about to.
	deadline = time.monotonic() + LOCK_TIMEOUT
	while not lock.tryAcquire():
		processID = lock.holder()
		if processID is not None:
			return processID
		if time.monotonic() >= deadline:
			raise OSError(f"whatever holds {lock.path} has not recorded its process ID")
		time.sleep(_POLL_INTERVAL)
	return None


def terminateRunning(lock: InstanceLock) -> None:
	"""Ask the copy holding the lock to quit, and kill it if it has not gone within QUIT_TIMEOUT.

	Only the process recorded in the lock file is ever signalled, and only while it still holds the
	lock: the signals go through a pidfd opened before that check, so a process ID that has since been
	reused cannot carry them to another process.
	@raise OSError: If the copy cannot be identified, or is still there after being killed.
	"""
	processID = _waitForHolder(lock)
	if processID is None:
		return
	try:
		pidfd = os.pidfd_open(processID)
	except ProcessLookupError:
		return
	try:
		if lock.tryAcquire() or lock.holder() != processID:
			# It exited after the lock file was read; whatever holds the lock now is not what was read.
			return
		try:
			signal.pidfd_send_signal(pidfd, signal.SIGTERM)
		except ProcessLookupError:
			return
		if _waitForExit(pidfd, QUIT_TIMEOUT):
			return
		try:
			signal.pidfd_send_signal(pidfd, signal.SIGKILL)
		except ProcessLookupError:
			return
		if not _waitForExit(pidfd, KILL_TIMEOUT):
			raise OSError(f"process {processID} did not exit after being killed")
	finally:
		os.close(pidfd)
