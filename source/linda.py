#!/usr/bin/env python3
"""Starts NVDA on Linux, as nvda.pyw starts it on Windows.

It does what nvda.pyw does before core.main, with each Windows step replaced by its Linux counterpart
or left out where Linux has none, and then runs core.main. With say as its first argument it speaks
the rest through linuxPlatform.say and exits instead.
"""

import os
import signal
import sys
from typing import NoReturn

appDir = os.path.dirname(os.path.abspath(__file__))
# Under -I, Python leaves the script's own directory off sys.path.
if appDir not in sys.path:
	sys.path.insert(0, appDir)

#: As the first argument, runs linuxPlatform.say instead of the screen reader.
SAY_COMMAND = "say"


def _tell(message: str) -> None:
	# On Linux the caller reads standard error. logHandler.initialize sends sys.stderr to the log.
	if sys.__stderr__ is not None:
		print(f"linda: {message}", file=sys.__stderr__, flush=True)


def _fail(message: str) -> NoReturn:
	_tell(message)
	sys.exit(1)


def _handOverToSay(arguments: list[str]) -> NoReturn:
	from linuxPlatform import bootstrap

	bootstrap.install()
	from linuxPlatform import say

	say.main(arguments)


def main() -> NoReturn:
	# Before NVDA's parser, which takes any argument it does not know and starts the screen reader anyway.
	if sys.argv[1:2] == [SAY_COMMAND]:
		_handOverToSay(sys.argv[2:])
	if SAY_COMMAND in sys.argv[2:]:
		_tell(
			f"the word {SAY_COMMAND} starts the say mode only as the first argument, so nothing was started"
		)
		sys.exit(2)
	from linuxPlatform import bootstrap

	bootstrap.install()

	import globalVars
	import NVDAState
	from argsParsing import getParser
	from linuxPlatform import quitSignals, singleInstance, synthDriverPaths, testHooks

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
	if appArgs.launcher:
		# The dialog it shows offers to install NVDA or make a portable copy, both through Windows' installer.
		_fail("--launcher is how NVDA's Windows installer starts it, and Linux has no such installer")
	# core.main would hand these to installer, which writes Windows' registry and shortcuts: it fails
	# part way, logs an error and leaves this copy running.
	for option, given in (
		("--install", appArgs.install),
		("--install-silent", appArgs.installSilent),
		("--create-portable", appArgs.createPortable),
		("--create-portable-silent", appArgs.createPortableSilent),
	):
		if given:
			_fail(f"{option} uses NVDA's Windows installer, which Linux does not have")

	try:
		instanceLocks = [
			singleInstance.InstanceLock(singleInstance.lockFilePath(name))
			for name in (singleInstance.APP_NAME, singleInstance.FORMER_APP_NAME)
		]
		isAlreadyRunning = any(lock.holder() is not None for lock in instanceLocks)
	except OSError as e:
		_fail(f"cannot open the lock that keeps one copy per display: {e}")
	if isAlreadyRunning and not appArgs.easeOfAccess:
		if appArgs.check_running:
			sys.exit(0)
		for lock in instanceLocks:
			try:
				singleInstance.terminateRunning(lock)
			except OSError as e:
				# nvda.pyw goes on after this too, and gives up when the lock does not come free.
				_tell(f"could not stop the copy already running: {e}")
	if appArgs.quit or (isAlreadyRunning and appArgs.easeOfAccess):
		sys.exit(0)
	elif appArgs.check_running:
		sys.exit(1)
	quitSignals.install()
	for lock in instanceLocks:
		try:
			isLockTaken = lock.acquire()
		except OSError as e:
			_fail(f"cannot take {lock.path}: {e}")
		if not isLockTaken:
			_fail(f"another copy still holds {lock.path}")
	# Only now, as a copy started under the former name could still have written its configuration
	# as it exited.
	from linuxPlatform import xdg

	for failure in xdg.adoptFormerDirectories():
		_tell(failure)

	from utils.security import isRunningOnSecureDesktop

	if NVDAState._forceSecureModeEnabled():
		appArgs.secure = True
	if isRunningOnSecureDesktop():
		if not NVDAState._serviceDebugEnabled():
			appArgs.secure = True
		appArgs.changeScreenReaderFlag = False
		appArgs.minimal = True
		# nvda.pyw gives the installed copy's systemConfig directory here, which holds the settings
		# copied for the sign-in screen. Linux has nothing of the kind, so the copy on the login screen
		# uses, and creates, its own user's configuration directory, and starts from upstream's defaults.
	# After the secure mode decision, which turns the test hooks off.
	testHooks.install()
	# Before core.main loads the configured synthesizer, which may be one built outside this tree.
	synthDriverPaths.install()

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
	failure: BaseException | None = None
	try:
		import core

		# From then on this copy acts on SIGTERM at once, so a copy replacing it need wait no longer.
		for lock in instanceLocks:
			core.postNvdaStartup.register(lock.markReady)
		core.main()
	except BaseException as e:
		# As nvda.pyw does, SystemExit included: wx raises it when there is no display to open.
		log.critical("core failure", exc_info=True)
		failure = e
	else:
		# The lock is never released by hand: the kernel drops it as the process ends, so a new copy waits
		# for this one to be entirely gone, as it waits on the mutex on Windows.
		log.info("NVDA exit")
	# The copy a restart started sends SIGTERM as it takes over, and as the interpreter finalises it puts
	# the handler's signals back to their default action, so that a copy slower to finalise than the new
	# one to start was ended by the signal instead of exiting cleanly. It is leaving anyway, and the new
	# copy kills it if it lingers. Ignored rather than blocked, as any of this process's threads may
	# take a signal that the main thread blocks.
	signal.signal(signal.SIGTERM, signal.SIG_IGN)
	# logHandler's hook writes to the log, which the interpreter takes apart before the objects whose
	# finalisers fail as it exits, so its own hook failed instead and hid what went wrong. Python's
	# hook writes to standard error, which on Linux is where the caller looks.
	sys.unraisablehook = sys.__unraisablehook__
	if failure is not None:
		logged = f"; the log is {appArgs.logFileName}" if appArgs.logFileName else ""
		_fail(f"core failure, {type(failure).__name__}: {failure}{logged}")
	sys.exit(NVDAState._getExitCode())


if __name__ == "__main__":
	main()
