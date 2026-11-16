"""The Linux side of logHandler: where the log lives, the error sound, and crash dumps."""

import faulthandler
import logging
import os
import stat
import sys
import threading
import traceback
from time import monotonic
from typing import TextIO

from linuxPlatform import xdg

#: Written by faulthandler when the process dies of a fatal signal, beside the log.
crashDumpFileName = "nvda_crash.log"
#: The previous crash dump, kept until another crash replaces it.
oldCrashDumpFileName = "nvda_crash-old.log"

#: An error sound requested this many seconds or less after the last one was handled follows it.
ERROR_SOUND_FOLLOW_WINDOW = 1.0
#: After this many requests in a row that follow the previous one, the sound stops being requested.
ERROR_SOUND_FOLLOW_LIMIT = 3
#: Once stopped, the sound is requested again only after this many seconds without an error.
ERROR_SOUND_QUIET_PERIOD = 5.0
#: The longest stopErrorSoundRequests waits for a request already under way.
ERROR_SOUND_STOP_TIMEOUT = 1.0

_crashDumpFile: TextIO | None = None


def stateDirectory() -> str:
	"""Basedreader's directory under the XDG state home, created when missing.

	The log records every key typed at the input log levels, so the directory is the user's alone.
	"""
	path = xdg.userStateDir()
	try:
		os.makedirs(path, mode=0o700, exist_ok=True)
		info = os.stat(path)
		if info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) & 0o077:
			os.chmod(path, 0o700)
	except OSError:
		# Opening the log there then fails, and logHandler reports that through its own fallback.
		pass
	return path


def openPrivately(path: str, mode: str, encoding: str | None, errors: str | None) -> TextIO:
	"""Open a log file for writing, creating it readable and writable by its owner only.

	A file that exists keeps the mode it has, so a log the user placed with -f and opened up on
	purpose stays open.
	"""
	flags = os.O_WRONLY | os.O_CREAT | os.O_CLOEXEC | (os.O_APPEND if "a" in mode else os.O_TRUNC)
	fd = os.open(path, flags, 0o600)
	try:
		return open(fd, mode, encoding=encoding, errors=errors)
	except BaseException:
		os.close(fd)
		raise


def reportUnwritableRecord(record: logging.LogRecord) -> None:
	"""Report a record the log could not write, on the process's real standard error.

	logging's own report goes to sys.stderr, which logHandler redirects into the log. When every
	record fails, as when formatting itself is broken, each report is another failing record and
	the reports never end.
	"""
	stream = sys.__stderr__
	if not logging.raiseExceptions or stream is None:
		return
	try:
		stream.write(f"--- Logging error in a record from {record.pathname}:{record.lineno} ---\n")
		traceback.print_exc(file=stream)
		stream.flush()
	# Nothing is left to report a failure of the last resort to.
	except Exception:  # noqa: BLE001, S110
		pass


class _ErrorSoundRequester:
	"""Asks for the error sound from a thread of its own.

	The sound is requested for every ERROR record, from whichever thread logged it. A player that
	fails logs an error, and that asks for the sound again: on the logging thread at once, which
	recurses, or from the player's own playback thread a moment later, as nvwave's does, which
	loops. A player waiting on a stuck playback thread also stops the thread that logged, which may
	be the core.

	So the logging thread only sets a flag and never waits. An error logged on this thread, or while
	the sound is being requested, asks for nothing. An error that keeps following the sound it
	caused is a loop no thread check can see, so after ERROR_SOUND_FOLLOW_LIMIT such requests in a
	row the sound stops until errors have been quiet for ERROR_SOUND_QUIET_PERIOD.
	"""

	def __init__(self) -> None:
		self._pending = threading.Event()
		self._thread: threading.Thread | None = None
		self._startLock = threading.Lock()
		self._handling = False
		self._handledAt = float("-inf")
		self._lastErrorAt = float("-inf")
		self._following = 0
		self._stopped = False
		self._closed = False

	def request(self) -> None:
		if self._closed or threading.current_thread() is self._thread:
			return
		now = monotonic()
		quietFor = now - self._lastErrorAt
		self._lastErrorAt = now
		if self._handling:
			return
		if self._stopped:
			if quietFor < ERROR_SOUND_QUIET_PERIOD:
				return
			self._stopped = False
			self._following = 0
		self._pending.set()
		if self._thread is not None:
			return
		# Never wait for the lock: an error logged on this thread while it holds the lock, from a
		# finaliser or a signal handler, would wait for ever. Whoever holds it starts the thread,
		# and the thread finds the request already set.
		if not self._startLock.acquire(blocking=False):
			return
		try:
			if self._thread is not None:
				return
			thread = threading.Thread(target=self._run, name="logHandler.errorSound", daemon=True)
			try:
				thread.start()
			except RuntimeError:
				# The interpreter is shutting down and starts no threads; the record is still written.
				return
			self._thread = thread
		finally:
			self._startLock.release()

	def _run(self) -> None:
		# Imported here: logHandler imports this module.
		import logHandler

		while True:
			self._pending.wait()
			self._pending.clear()
			if self._closed:
				return
			if monotonic() - self._handledAt <= ERROR_SOUND_FOLLOW_WINDOW:
				self._following += 1
			else:
				self._following = 0
			if self._following >= ERROR_SOUND_FOLLOW_LIMIT:
				self._stopped = True
				logHandler.log.warning(
					f"Errors followed the error sound {self._following} times in a row; no error sound "
					f"until no error has been logged for {ERROR_SOUND_QUIET_PERIOD} seconds",
				)
				continue
			self._handling = True
			try:
				logHandler.getOnErrorSoundRequested().notify()
			except Exception:
				logHandler.log.exception("Error requesting the error sound")
			finally:
				self._handling = False
				self._handledAt = monotonic()

	def close(self, timeout: float) -> None:
		"""Ask for nothing more, and wait up to timeout seconds for a request under way to finish.

		The wait is bounded because the request may be stuck in a player.
		"""
		self._closed = True
		thread = self._thread
		if thread is None or thread is threading.current_thread():
			return
		self._pending.set()
		thread.join(timeout)


_errorSound = _ErrorSoundRequester()


def requestErrorSound() -> None:
	"""Ask for the error sound without blocking the caller and without recursing."""
	_errorSound.request()


def stopErrorSoundRequests(timeout: float = ERROR_SOUND_STOP_TIMEOUT) -> None:
	"""Have errors ask for no sound from now on, once a request already under way has finished.

	Call it before the exit sound. nvwave plays one file at a time through a single global player,
	and the sound for an error logged late in shutdown is asked for a moment later on the requester's
	own thread, where it can start in the middle of the exit sound and cut it off. Upstream asks on
	the logging thread, so there the error sound has always started first and the exit sound stops it.
	"""
	_errorSound.close(timeout)


def _openCrashDump(directory: str) -> TextIO:
	path = os.path.join(directory, crashDumpFileName)
	mode = "w"
	try:
		if os.path.getsize(path) > 0:
			try:
				os.replace(path, os.path.join(directory, oldCrashDumpFileName))
			except OSError:
				# The dump holds the last crash and cannot be kept as the old one, so the next crash
				# is written after it.
				mode = "a"
	except FileNotFoundError:
		pass
	return openPrivately(path, mode, "utf-8", None)


def enableCrashDump(logFileName: str | None) -> None:
	"""Have faulthandler write every thread's Python stack to a file beside the log on a fatal signal.

	A crash on Linux kills the process before any Python code can run, so the file is opened now and
	faulthandler writes to its descriptor from the signal handler. A previous dump that holds a crash
	is kept as the old one, as the log keeps the previous run's. Without a log file the dump goes to
	the state directory, and when no file can be opened, to the real standard error.
	Call it once the log's handler is installed, so that a failure here is logged.
	"""
	global _crashDumpFile
	# Imported here: logHandler imports this module.
	from logHandler import log

	directory = os.path.dirname(logFileName) if logFileName else stateDirectory()
	crashDumpFile: TextIO | None = None
	try:
		crashDumpFile = _openCrashDump(directory)
	except OSError as e:
		log.warning(f"Cannot write crash dumps to {directory}: {e}")
	target = crashDumpFile if crashDumpFile is not None else sys.__stderr__
	try:
		if target is None:
			raise ValueError("there is no standard error")
		faulthandler.enable(file=target, all_threads=True)
	except (AttributeError, OSError, RuntimeError, ValueError) as e:
		log.warning(f"Crash dumps are off: {e}")
	if _crashDumpFile is not None:
		_crashDumpFile.close()
	_crashDumpFile = crashDumpFile
