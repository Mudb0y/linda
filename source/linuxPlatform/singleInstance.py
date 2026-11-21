"""One NVDA per display, and how a new one takes over from the one running.

On Windows nvda.pyw finds the running copy by its message window, posts it WM_QUIT, waits for its
process and kills it if it lingers, and then waits on a named mutex. Here one file per display under
XDG_RUNTIME_DIR does all of that: the running copy holds a POSIX record lock on it for as long as the
process lives and records its process ID in it, and a new copy asks that process to quit with
SIGTERM. The kernel drops the lock only when the process is gone, so the lock is held until the very
last thing the old copy does, as the mutex is on Windows.

The running copy takes the lock as it starts, long before it can act on SIGTERM, which it does only
once its main loop runs. On Windows a copy that is still starting has no message window yet and is
never asked to quit. Here it is asked at once, and it marks itself ready in the file once it can act
on the request; the time it has to exit counts from then.

The kernel, not the file, says which process holds the lock: a record lock, unlike flock, can be
asked for its holder. The file alone could name a process that exited long ago, whose ID may since
belong to any other program.

NVDA also starts a new copy of itself to restart, and that copy takes over the same way.
"""

import errno
import fcntl
import os
import re
import select
import signal
import struct
import subprocess
import sys
import time

#: Seconds the running copy has to exit once asked and ready, before it is killed; nvda.pyw's wait.
QUIT_TIMEOUT = 4.0
#: Seconds a copy that is still starting has to become ready, before QUIT_TIMEOUT starts anyway.
START_TIMEOUT = 30.0
#: Seconds a killed copy has to be gone; nvda.pyw's wait after TerminateProcess.
KILL_TIMEOUT = 2.0
#: Seconds to wait for the lock, as nvda.pyw waits for the prior copy's mutex.
LOCK_TIMEOUT = 2.0
_POLL_INTERVAL = 0.05

#: struct flock on 64-bit Linux: type, whence, start, length, process ID, with the C padding.
_FLOCK = "hhqqi4x"

#: What a copy writes in the file: its process ID once it holds the lock, and the second line once
#: it acts on SIGTERM. Anything else, stale bytes left by an earlier copy among them, is no record.
_RECORD = re.compile(rb"(?P<processID>[1-9][0-9]*)\n(?P<ready>ready\n)?")
_READY = b"ready\n"

#: Host parts of a DISPLAY that name the X server on this machine by its socket. A TCP host part,
#: localhost included, is another server: what ssh forwards a display to is a proxy for a remote one.
_LOCAL_HOSTS = ("", "unix")

#: The directory under the runtime directory that holds the locks.
APP_NAME = "linda"
#: Where copies started while the program was Basedreader hold theirs. A copy holds that lock as well,
#: so that either name replaces a copy started under the other and only one reader ever runs.
FORMER_APP_NAME = "basedreader"


def _checkPrivateDirectory(path: str) -> None:
	info = os.lstat(path)
	if info.st_uid != os.getuid() or info.st_mode & 0o077 or not os.path.isdir(path):
		raise PermissionError(f"{path} is not a private directory of this user")


def _runtimeDir() -> str:
	path = os.environ.get("XDG_RUNTIME_DIR", "")
	if os.path.isabs(path):
		return path
	# Where GLib's g_get_user_runtime_dir() falls back to when the session sets none, as under su or
	# sudo. A name of its own in the shared /tmp would let any other user claim it first.
	path = os.environ.get("XDG_CACHE_HOME", "")
	if not os.path.isabs(path):
		path = os.path.join(os.path.expanduser("~"), ".cache")
	# Not the cache home itself, where APP_NAME is the cache directory: that is made at whatever mode
	# the umask gives, which the lock refuses, and emptied by whoever clears caches, which would let a
	# second copy start beside this one.
	path = os.path.join(path, f"{APP_NAME}-runtime")
	os.makedirs(path, 0o700, exist_ok=True)
	return path


def displayName() -> str:
	"""The display this process would run on, in one spelling for each display.

	:0, :0.0 and unix:0 all reach the same X server through its socket, so they must give one lock.
	"""
	display = os.environ.get("DISPLAY")
	if not display:
		return _waylandName()
	host, separator, number = display.rpartition(":")
	if not separator:
		return display.replace("/", "_")
	# Only the screen differs between :0 and :0.0.
	number = re.sub(r"\.\d+$", "", number)
	if host in _LOCAL_HOSTS or host.startswith("/"):
		# A host part starting with a slash is the socket path XQuartz and some launchers use.
		return f":{number}"
	return f"{host}:{number}".replace("/", "_")


def _waylandName() -> str:
	wayland = os.environ.get("WAYLAND_DISPLAY") or "none"
	# The socket may be named by its path or, under XDG_RUNTIME_DIR, by its name alone.
	if os.path.dirname(wayland) == os.environ.get("XDG_RUNTIME_DIR", "").rstrip("/"):
		wayland = os.path.basename(wayland)
	return wayland.replace("/", "_")


def displayNames(kind: str) -> list[str]:
	"""Each display this process holds a lock for, the Wayland socket first.

	In an X session that is displayName() alone, the one lock there has always been. A Wayland
	session that also has an X display, its Xwayland, locks both: a program started in it may be given
	only one of the two, and a copy from before the session's kind was known locked the X display alone,
	so whichever a new copy is given, it finds the copy running.

	:param kind: linuxPlatform.displayServer.kind(), given rather than asked for so that this module
		needs only the standard library.
	"""
	name = displayName()
	if kind != "wayland" or not os.environ.get("DISPLAY") or not os.environ.get("WAYLAND_DISPLAY"):
		return [name]
	waylandName = _waylandName()
	return [waylandName, name] if waylandName != name else [name]


def lockFilePath(appName: str = APP_NAME, display: str | None = None) -> str:
	"""The lock file for the display this process would run on.

	XDG_RUNTIME_DIR is per user, not per session, so the display is in the name: a user signed in
	on two displays runs a copy on each.

	:param appName: The directory of the lock: FORMER_APP_NAME for the one copies took under that name.
	:param display: One of displayNames(); displayName() by default.
	"""
	directory = os.path.join(_runtimeDir(), appName)
	try:
		os.mkdir(directory, 0o700)
	except FileExistsError:
		pass
	# Whoever can write here could plant a lock file naming any process.
	_checkPrivateDirectory(directory)
	if display is None:
		display = displayName()
	return os.path.join(directory, f"display{display}.lock")


class InstanceLock:
	"""The lock file of one display, opened by this process."""

	def __init__(self, path: str) -> None:
		self.path = path
		# A record lock is not inherited by a child process; O_CLOEXEC keeps the file itself from
		# children too. Closing any descriptor of this file in this process would drop the lock, which
		# is why this class is the only one that opens it.
		self._fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)

	def tryAcquire(self) -> bool:
		"""Take the lock if no other process holds it, without waiting."""
		try:
			fcntl.lockf(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
		except OSError as e:
			if e.errno in (errno.EACCES, errno.EAGAIN):
				return False
			raise
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
		os.pwrite(self._fd, self._processLine(), 0)
		return True

	@staticmethod
	def _processLine() -> bytes:
		return f"{os.getpid()}\n".encode()

	def markReady(self) -> None:
		"""Record that this process, which holds the lock, now acts on SIGTERM at once."""
		os.pwrite(self._fd, _READY, len(self._processLine()))

	def close(self) -> None:
		"""Close the file, which gives up the lock if this process holds it."""
		os.close(self._fd)

	def holder(self) -> int | None:
		"""The process holding the lock, as the kernel reports it, or None if no other process does.

		:raises OSError: If the holder is in another PID namespace, where its ID means nothing here.
		"""
		request = struct.pack(_FLOCK, fcntl.F_WRLCK, os.SEEK_SET, 0, 0, 0)
		lockType, _whence, _start, _length, processID = struct.unpack(
			_FLOCK,
			fcntl.fcntl(self._fd, fcntl.F_GETLK, request),
		)
		if lockType == fcntl.F_UNLCK:
			return None
		if processID <= 0:
			raise OSError(f"{self.path} is held by a process this one cannot see")
		return processID

	def recorded(self) -> int | None:
		"""The process ID written in the file by the copy that last took the lock."""
		record = self._record()
		return int(record["processID"]) if record else None

	def recordedReady(self) -> int | None:
		"""The process ID in the file, if that copy has also marked itself ready."""
		record = self._record()
		return int(record["processID"]) if record and record["ready"] else None

	def _record(self) -> re.Match[bytes] | None:
		# More than the longest record, so that a longer file cannot pass for one.
		return _RECORD.fullmatch(os.pread(self._fd, 32, 0))


def _waitForExit(pidfd: int, timeout: float) -> bool:
	poller = select.poll()
	poller.register(pidfd, select.POLLIN)
	return bool(poller.poll(int(timeout * 1000)))


def _waitForHolder(lock: InstanceLock) -> int | None:
	"""The process holding the lock once it has recorded itself, or None once no process holds it."""
	# A copy records itself right after taking the lock, so for a moment the file still names the
	# copy before it.
	deadline = time.monotonic() + LOCK_TIMEOUT
	while (processID := lock.holder()) is not None:
		if lock.recorded() == processID:
			return processID
		if time.monotonic() >= deadline:
			raise OSError(f"process {processID} holds {lock.path} but has not recorded itself in it")
		time.sleep(_POLL_INTERVAL)
	return None


def terminateRunning(lock: InstanceLock) -> None:
	"""Ask the copy holding the lock to quit, and kill it if it has not gone within QUIT_TIMEOUT.

	Only the process that the kernel reports as holding the lock, and that recorded itself in the
	file, is ever signalled. The signals go through a pidfd, and the holder is asked for again once
	the pidfd is open, so a process ID that has since been reused cannot carry them to another
	process. A copy that is still starting is given up to START_TIMEOUT to become ready first.

	:raises OSError: If the copy cannot be identified, or is still there after being killed.
	"""
	processID = _waitForHolder(lock)
	if processID is None:
		return
	try:
		pidfd = os.pidfd_open(processID)
	except ProcessLookupError:
		return
	try:
		if lock.holder() != processID:
			# It exited after it was looked up; whatever has this ID now is not what held the lock.
			return
		try:
			signal.pidfd_send_signal(pidfd, signal.SIGTERM)
		except ProcessLookupError:
			return
		readyDeadline = time.monotonic() + START_TIMEOUT
		while lock.recordedReady() != processID and time.monotonic() < readyDeadline:
			if _waitForExit(pidfd, _POLL_INTERVAL):
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


def splitWindowsCommandLine(commandLine: str) -> list[str]:
	"""Split a command line as Windows' C runtime does, which undoes subprocess.list2cmdline.

	shlex.split does not: it treats every backslash as an escape, and Windows quoting escapes only
	the backslashes that come before a double quote.
	"""
	arguments: list[str] = []
	current: list[str] = []
	isInArgument = False
	isQuoted = False
	backslashes = 0
	for char in commandLine:
		if char == "\\":
			backslashes += 1
			continue
		if char == '"':
			current.append("\\" * (backslashes // 2))
			if backslashes % 2:
				current.append('"')
			else:
				isQuoted = not isQuoted
			backslashes = 0
			isInArgument = True
			continue
		if backslashes:
			current.append("\\" * backslashes)
			backslashes = 0
			isInArgument = True
		if char in " \t" and not isQuoted:
			if isInArgument:
				arguments.append("".join(current))
				current = []
				isInArgument = False
			continue
		current.append(char)
		isInArgument = True
	if backslashes:
		current.append("\\" * backslashes)
		isInArgument = True
	if isInArgument:
		arguments.append("".join(current))
	return arguments


def interpreterOptions(origArgv: list[str], scriptPath: str) -> list[str]:
	"""The options this interpreter was started with, such as -I and -B, which sys.argv leaves out.

	:param origArgv: sys.orig_argv, the whole command line.
	:param scriptPath: sys.argv[0], the script as the command line named it.
	"""
	try:
		return origArgv[1 : origArgv.index(scriptPath, 1)]
	except ValueError:
		# Not started from a script file; there is no script position to take the options from.
		return []


def startNewInstance(filePath: str, parameters: str | None, directory: str | None) -> None:
	"""Start the copy that replaces this one, from core's description of it.

	core quotes the parameters for Windows' ShellExecute. A copy run from source is started by the
	interpreter, which core names without the options this one had, so they are carried over: a copy
	started without -I would read PYTHONPATH and the user's site-packages.

	The new copy takes over from this one as from any copy already running, while this one is still
	shutting down: it asks this one to quit, which it already is, and starts once it is gone. It
	stays in this copy's session and process group, so that whatever would have stopped this copy,
	such as its terminal hanging up, stops the new one too.
	"""
	command = [filePath]
	if filePath == sys.executable:
		command += interpreterOptions(sys.orig_argv, sys.argv[0])
	subprocess.Popen(
		[*command, *splitWindowsCommandLine(parameters or "")],
		cwd=directory,
		stdin=subprocess.DEVNULL,
	)
