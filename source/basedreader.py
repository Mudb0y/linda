#!/usr/bin/env python3
"""Starts NVDA on Linux, as nvda.pyw starts it on Windows.

It does what nvda.pyw does before core.main, with each Windows step replaced by its Linux counterpart
or left out where Linux has none, and then runs core.main.
"""

import os
import signal
import sys
from typing import NoReturn

appDir = os.path.dirname(os.path.abspath(__file__))
# Under -I, Python leaves the script's own directory off sys.path.
if appDir not in sys.path:
	sys.path.insert(0, appDir)

#: Names a file that receives a JSON line for every NoLinuxImplementation raised in this process,
#: including those upstream catches and treats as a feature being absent.
noLinuxImplementationFileVariable = "BASEDREADER_NO_LINUX_IMPLEMENTATION_FILE"


def _recordNoLinuxImplementation(path: str) -> None:
	import json
	import threading
	import traceback

	from linuxPlatform import noLinuxImplementationEvent

	# Line buffered, so that each record is on disk however the process ends.
	recordFile = open(path, "a", encoding="utf-8", buffering=1)  # noqa: SIM115

	def onAudit(event: str, args: tuple) -> None:
		if event != noLinuxImplementationEvent:
			return
		record = {
			"what": str(args[0]),
			"thread": threading.current_thread().name,
			# The frames that raised it, without this hook's and NoLinuxImplementation's own.
			"stack": traceback.format_stack(limit=14)[:-2],
		}
		recordFile.write(json.dumps(record) + "\n")

	sys.addaudithook(onAudit)


#: Milliseconds between attempts to exit while message boxes are closing.
_EXIT_RETRY_INTERVAL = 50
_isExitPending = False


def _exitOnceMessageBoxesClose() -> bool:
	"""Exit through core.triggerNVDAExit, once every open message box has been cancelled.

	triggerNVDAExit refuses to exit while a message box waits for an answer. On Windows the session's
	end posts WM_QUIT, which ends every nested message loop and so every message box, and NVDA then
	exits through its catch-all after the main loop. A signal ends no loop, so the modal dialogs are
	cancelled instead, one each time this runs, and the exit waits until each call that showed a
	message box has returned.
	"""
	global _isExitPending
	import core
	import wx
	from gi.repository import GLib
	from gui.message import isModalMessageBoxActive

	if not isModalMessageBoxActive():
		_isExitPending = False
		core.triggerNVDAExit()
		return GLib.SOURCE_REMOVE
	modalDialogs = [w for w in wx.GetTopLevelWindows() if isinstance(w, wx.Dialog) and w.IsModal()]
	if modalDialogs:
		# Only the innermost, which is the last opened: a dialog further out cannot end its modal loop
		# while another runs inside it. Cancelled, the answer gui.message.messageBox gives once shutdown
		# has begun. Closing it instead would end a message box with no return code, which messageBox
		# cannot translate, or be vetoed by a question with no answer to fall back on.
		modalDialogs[-1].EndModal(wx.ID_CANCEL)
	if not _isExitPending:
		_isExitPending = True
		GLib.timeout_add(_EXIT_RETRY_INTERVAL, _exitOnceMessageBoxesClose)
	return GLib.SOURCE_CONTINUE


def _onQuitSignal(signalNumber: int) -> bool:
	from gi.repository import GLib
	from logHandler import log

	log.info(f"Exiting on {signal.Signals(signalNumber).name}")
	if not _isExitPending:
		_exitOnceMessageBoxesClose()
	# Kept rather than removed, so that a repeated signal reaches this handler again instead of the
	# default action, which ends the process without saving anything.
	return GLib.SOURCE_CONTINUE


def _exitOnQuitSignals() -> None:
	"""Exit through core.triggerNVDAExit on SIGTERM, which a new copy and the end of the session send,
	SIGHUP and SIGINT.

	CPython runs a signal.signal handler only when the main thread next runs Python code, and an idle
	wx main loop waits in C, so NVDA would not notice the signal until something else woke it. GLib
	runs these handlers from the main loop itself, which wxGTK's loop is.
	"""
	from gi.repository import GLib, GLibUnix

	for signalNumber in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
		GLibUnix.signal_add(GLib.PRIORITY_HIGH, signalNumber, _onQuitSignal, signalNumber)


def _fail(message: str) -> NoReturn:
	# Before logHandler.initialize there is no log, and on Linux the caller reads standard error.
	print(f"basedreader: {message}", file=sys.stderr)
	sys.exit(1)


def main() -> NoReturn:
	recordPath = os.environ.get(noLinuxImplementationFileVariable)
	if recordPath:
		_recordNoLinuxImplementation(recordPath)
	from linuxPlatform import bootstrap

	bootstrap.install()

	import globalVars
	import NVDAState
	from argsParsing import getParser
	from linuxPlatform import singleInstance, testHooks

	# nvda.pyw also checks for its virtual environment here. The Nix development shell provides
	# NVDA's dependencies instead, and bootstrap.install() has checked the one it cannot start without.
	os.chdir(appDir)
	globalVars.appDir = appDir
	globalVars.appPid = os.getpid()

	import config
	import logHandler
	from logHandler import log

	# Windows Store packages exist only on Windows.
	config.isAppX = False
	NVDAState._initializeStartTime()
	# nvda.pyw refuses Windows versions older than 10 here. On Linux winVersion reports 0.0.0, and
	# bootstrap.install() has already refused every platform but Linux.

	(globalVars.appArgs, globalVars.unknownAppArgs) = getParser().parse_known_args()
	# Absolute, so that a later change of directory cannot move them.
	for name in ("configPath", "logFileName", "portablePath"):
		value = getattr(globalVars.appArgs, name)
		if isinstance(value, str):
			setattr(globalVars.appArgs, name, os.path.abspath(value))
	appArgs = globalVars.appArgs

	try:
		instanceLock = singleInstance.InstanceLock(singleInstance.lockFilePath())
		isAlreadyRunning = instanceLock.holder() is not None
	except OSError as e:
		_fail(f"cannot open the lock that keeps one NVDA per display: {e}")
	if isAlreadyRunning and not appArgs.easeOfAccess:
		if appArgs.check_running:
			sys.exit(0)
		try:
			singleInstance.terminateRunning(instanceLock)
		except OSError as e:
			# nvda.pyw goes on after this too, and gives up when the lock does not come free.
			print(f"basedreader: could not stop the NVDA already running: {e}", file=sys.stderr)
	if appArgs.quit or (isAlreadyRunning and appArgs.easeOfAccess):
		sys.exit(0)
	elif appArgs.check_running:
		sys.exit(1)
	# Before the lock is taken: from then on a copy replacing this one may send SIGTERM, which would
	# otherwise end this one without letting go of anything it had started.
	_exitOnQuitSignals()
	if not instanceLock.acquire():
		_fail(f"another NVDA still holds {instanceLock.path}")

	from utils.security import isRunningOnSecureDesktop

	if NVDAState._forceSecureModeEnabled():
		appArgs.secure = True
	if isRunningOnSecureDesktop():
		if not NVDAState._serviceDebugEnabled():
			appArgs.secure = True
		appArgs.changeScreenReaderFlag = False
		appArgs.minimal = True
		# nvda.pyw uses the systemConfig directory of the installed copy here. Linux has none, so the
		# login screen's own user's configuration serves, unwritten, as secure mode writes nothing.
	# After the secure mode decision, which turns the test hooks off.
	testHooks.install()

	logHandler.initialize()
	if log.getEffectiveLevel() == log.DEBUG:
		log.debug(f"Provided arguments: {sys.argv[1:]}")
	import platform

	import buildVersion

	log.info(f"Starting NVDA version {buildVersion.version} {platform.machine()}")
	log.debug("Debug level logging enabled")
	# nvda.pyw sets Windows' screen reader flag here. Its AT-SPI counterpart, the bus's IsEnabled, turns
	# on when NVDA registers for events, and setting it by hand would also write a GSettings key that the
	# desktop's other screen reader shares. Nor does Linux need nvda.pyw's next steps: there are no
	# window messages to let through, the end of the session comes as SIGTERM, handled above, and
	# there is no Ease of Access Center to tell that NVDA is starting or stopping.
	# nvda.pyw applies its comtypes monkey patches first of all. comtypes does nothing on Linux, and
	# its stand-in has none of the internals those patches replace.
	try:
		import core

		core.main()
	except BaseException:
		# As nvda.pyw does, SystemExit included: wx raises it when there is no display to open.
		log.critical("core failure", exc_info=True)
		sys.exit(1)
	# The lock is never released by hand: the kernel drops it as the process ends, so a new copy waits
	# for this one to be entirely gone, as it waits on the mutex on Windows.
	log.info("NVDA exit")
	# logHandler's hook writes to the log, which the interpreter takes apart before the objects whose
	# finalisers fail as it exits, so its own hook failed instead and hid what went wrong. Python's
	# hook writes to standard error, which on Linux is where the caller looks.
	sys.unraisablehook = sys.__unraisablehook__
	sys.exit(NVDAState._getExitCode())


if __name__ == "__main__":
	main()
