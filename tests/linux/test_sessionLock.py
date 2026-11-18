"""The locked session, as logind and the desktop's screen saver report it, and what NVDA withholds then.

A stand-in screen saver owns org.mate.ScreenSaver on the rig's session bus. The rig has no system bus,
so a stand-in logind answers on a bus daemon of the test's own, which DBUS_SYSTEM_BUS_ADDRESS names.

nix develop . -c tests/linux/rig python3 -m pytest tests/linux/test_sessionLock.py
"""

import logging
import textwrap

import harness

SERVICES = harness.HERE / "fixtures" / "lockServices.py"
SESSION_PATH = "/org/freedesktop/login1/session/_31"

#: Starts the stand-ins and defines the helpers the tests share.
PRELUDE = f"""
import os
import subprocess
import sys
import time

import globalVars

SERVICES = {str(SERVICES)!r}
SESSION_PATH = {SESSION_PATH!r}
started = []


def startCore():
	import config
	from NVDAState import WritePaths

	if not WritePaths.configDir:
		WritePaths.configDir = config.getUserDefaultConfigPath()
	config.initConfigPath()
	config.initialize()
	import NVDAHelper  # noqa: F401


def service(*arguments, env=None):
	os.makedirs(os.environ["XDG_CONFIG_HOME"], exist_ok=True)
	process = subprocess.Popen(
		[sys.executable, SERVICES, *arguments],
		stdin=subprocess.PIPE,
		stdout=subprocess.PIPE,
		# Not the child's, which the harness reads to its end: a stand-in left running would hold it open.
		stderr=open(os.path.join(os.environ["XDG_CONFIG_HOME"], f"{{arguments[0]}}.stderr"), "w"),
		text=True,
		env=env,
	)
	started.append(process)
	return process


def readLine(process, expected):
	line = process.stdout.readline().strip()
	assert line.startswith(expected), (line, expected)
	return line


def tell(process, command):
	process.stdin.write(command + "\\n")
	process.stdin.flush()
	readLine(process, "done")


def startSaver():
	saver = service("saver", "org.mate.ScreenSaver", "/org/mate/ScreenSaver")
	dialog = int(readLine(saver, "dialog ").split()[1])
	readLine(saver, "ready")
	return saver, dialog


def startLogind():
	daemon = subprocess.Popen(
		["dbus-daemon", "--session", "--nofork", "--print-address=1", "--address=unix:tmpdir=/tmp"],
		stdout=subprocess.PIPE,
		stderr=subprocess.DEVNULL,
		text=True,
	)
	started.append(daemon)
	address = daemon.stdout.readline().strip()
	os.environ["DBUS_SYSTEM_BUS_ADDRESS"] = address
	logind = service("logind", address, SESSION_PATH)
	readLine(logind, "ready")
	return logind


def stopServices():
	for process in reversed(started):
		if process.stdin:
			process.stdin.close()
		try:
			process.wait(5)
		except subprocess.TimeoutExpired:
			process.kill()
			process.wait()
"""


def run(tmpPath, body: str) -> harness.ChildResult:
	return harness.runChild(
		tmpPath,
		PRELUDE + textwrap.dedent(body),
		prepare="appDir",
		env={
			"XDG_CONFIG_HOME": str(tmpPath / "xdgConfig"),
			"XDG_SESSION_CLASS": "user",
			"XDG_SESSION_ID": None,
		},
		recordLevel=logging.WARNING,
		timeout=60,
	)


def test_screenSaverAndLogindLockTheSessionAndTheLockerStaysReadable(tmp_path):
	done = run(
		tmp_path,
		"""
		startCore()
		import api
		import controlTypes
		from baseObject import AutoPropertyObject
		from linuxPlatform import sessionLock
		from linuxPlatform.desktop import Desktop
		from NVDAObjects import NVDAObject
		from NVDAState import _TrackNVDAInitialization
		from utils.security import objectBelowLockScreenAndWindowsIsLocked as isWithheld
		from utils.security import post_sessionLockStateChanged
		from winAPI import sessionTracking

		saver, dialog = startSaver()
		logind = startLogind()
		# Already active when NVDA starts.
		tell(saver, "active true")


		def window(ofProcess, called):
			class Window(NVDAObject):
				role = controlTypes.Role.WINDOW
				processID = ofProcess
				name = called

			return Window()


		announced = []


		def onLockStateChanged(isNowLocked):
			announced.append(isNowLocked)


		def lockedAfter(command=None, target=None, settle=0.0):
			before = len(announced)
			if command:
				tell(target, command)
			deadline = time.monotonic() + (settle or 5)
			while True:
				time.sleep(0.02)
				AutoPropertyObject.invalidateCaches()
				sessionTracking.pumpAll()
				if (len(announced) > before and not settle) or time.monotonic() > deadline:
					return sessionTracking.isLockScreenModeActive()


		def withheld():
			return {
				"mail": isWithheld(window(4242, "a private message")),
				"saver": isWithheld(window(saver.pid, "screen saver")),
				"dialog": isWithheld(window(dialog, "Unlock")),
				"desktop": isWithheld(Desktop()),
			}


		post_sessionLockStateChanged.register(onLockStateChanged)
		api.setDesktopObject(Desktop())
		sessionTracking.initialize()
		_TrackNVDAInitialization.markInitializationComplete()
		result = {"atStart": lockedAfter(), "whileLocked": withheld()}
		result["saverInactive"] = lockedAfter("active false", saver)
		result["whileUnlocked"] = withheld()
		result["hintSet"] = lockedAfter("hint true", logind)
		result["hintCleared"] = lockedAfter("hint false", logind)
		result["lockSignal"] = lockedAfter("lock", logind)
		result["unlockSignal"] = lockedAfter("unlock", logind)
		# The screen saver locks, and its own report of the end wins over a LockedHint left set.
		result["hintSetAgain"] = lockedAfter("hint true", logind)
		tell(saver, "active true")
		result["unlockedBySaver"] = lockedAfter("active false", saver)
		tell(logind, "hint false")
		saver.stdin.close()
		saver.wait(5)
		# Nothing on the bus could carry it out, so a Lock signal alone changes nothing.
		result["lockWithoutSaver"] = lockedAfter("lock", logind, settle=0.5)
		result["announced"] = list(announced)
		result["withoutSaver"] = [lockedAfter("hint true", logind), withheld()]
		stopServices()
		""",
	)
	done.assertClean()
	assert done.value == {
		"atStart": True,
		"whileLocked": {"mail": True, "saver": False, "dialog": False, "desktop": False},
		"saverInactive": False,
		"whileUnlocked": {"mail": False, "saver": False, "dialog": False, "desktop": False},
		"hintSet": True,
		"hintCleared": False,
		"lockSignal": True,
		"unlockSignal": False,
		"hintSetAgain": True,
		"unlockedBySaver": False,
		"lockWithoutSaver": False,
		"announced": [True, False, True, False, True, False, True, False],
		# With no screen saver known, only the desktop is above the lock.
		"withoutSaver": [True, {"mail": True, "saver": True, "dialog": True, "desktop": False}],
	}
	# Both sources were there, so nothing was warned about.
	assert not [r for r in done.logRecords if "locked" in r], done.logRecords


def test_withoutEitherSourceTheLockCannotBeTold(tmp_path):
	done = run(
		tmp_path,
		"""
		startCore()
		from winAPI import sessionTracking

		sessionTracking.initialize()
		result = sessionTracking._querySessionLocked()
		""",
	)
	done.assertClean()
	assert done.value is False
	assert [r for r in done.logRecords if "cannot tell whether this Linux session is locked" in r], (
		done.logRecords
	)
