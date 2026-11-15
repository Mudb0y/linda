"""The Linux side of logHandler: where the log lives, the error sound, and crash dumps."""

import faulthandler
import logging
import os
import sys
import threading
import traceback
from typing import TextIO

#: Written by faulthandler when the process dies of a fatal signal, beside the log.
crashDumpFileName = "nvda_crash.log"
#: The previous crash dump, kept until another crash replaces it.
oldCrashDumpFileName = "nvda_crash-old.log"

_crashDumpFile: TextIO | None = None


def stateDirectory() -> str:
	"""Basedreader's directory under the XDG state home, created when missing."""
	stateHome = os.environ.get("XDG_STATE_HOME", "")
	# The base directory specification says a relative value is invalid and must be ignored.
	if not os.path.isabs(stateHome):
		stateHome = os.path.join(os.path.expanduser("~"), ".local", "state")
	path = os.path.join(stateHome, "basedreader")
	try:
		os.makedirs(path, mode=0o700, exist_ok=True)
	except OSError:
		# Opening the log there then fails, and logHandler reports that through its own fallback.
		pass
	return path


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

	The sound is requested for every ERROR record, from whichever thread logged it. Asked for on
	that thread, a player that logs an error while starting asks for the sound again, without end,
	and a player waiting on a stuck playback thread stops the thread that logged, which may be the
	core. Here the logging thread only sets a flag, requests that arrive while one is handled
	collapse into one, and an error logged while handling a request asks for nothing.
	"""

	def __init__(self) -> None:
		self._pending = threading.Event()
		self._thread: threading.Thread | None = None
		self._startLock = threading.Lock()

	def request(self) -> None:
		if threading.current_thread() is self._thread:
			return
		self._pending.set()
		if self._thread is not None:
			return
		with self._startLock:
			if self._thread is not None:
				return
			thread = threading.Thread(target=self._run, name="logHandler.errorSound", daemon=True)
			try:
				thread.start()
			except RuntimeError:
				# The interpreter is shutting down and starts no threads; the record is still written.
				return
			self._thread = thread

	def _run(self) -> None:
		# Imported here: logHandler imports this module.
		import logHandler

		while True:
			self._pending.wait()
			self._pending.clear()
			try:
				if logHandler.shouldPlayErrorSound():
					logHandler.getOnErrorSoundRequested().notify()
			except Exception:
				logHandler.log.exception("Error requesting the error sound")


_errorSound = _ErrorSoundRequester()


def requestErrorSound() -> None:
	"""Ask for the error sound without blocking the caller and without recursing."""
	_errorSound.request()


def enableCrashDump(logDirectory: str) -> None:
	"""Have faulthandler write every thread's Python stack to a file beside the log on a fatal signal.

	A crash on Linux kills the process before any Python code can run, so the file is opened now and
	faulthandler writes to its descriptor from the signal handler. A previous dump that holds a crash
	is kept as the old one, as the log keeps the previous run's.
	"""
	global _crashDumpFile
	path = os.path.join(logDirectory, crashDumpFileName)
	try:
		if os.path.getsize(path) > 0:
			os.replace(path, os.path.join(logDirectory, oldCrashDumpFileName))
	except OSError:
		pass
	try:
		crashDumpFile = open(path, "w", encoding="utf-8")  # noqa: SIM115
	except OSError:
		return
	faulthandler.enable(file=crashDumpFile, all_threads=True)
	if _crashDumpFile is not None:
		_crashDumpFile.close()
	_crashDumpFile = crashDumpFile
