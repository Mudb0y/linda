"""The locked session, as logind and the desktop's screen saver report it, and what NVDA withholds then.

A stand-in screen saver owns org.mate.ScreenSaver on the rig's session bus. The rig has no system bus,
so a stand-in logind answers on a bus daemon of the test's own, which DBUS_SYSTEM_BUS_ADDRESS names.
Both the child and the stand-ins have the same XDG_SESSION_ID, which makes the saver this session's.

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
import socket
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
		stderr=open(os.path.join(os.environ["XDG_CONFIG_HOME"], f"{{arguments[0]}}{{len(started)}}.stderr"), "w"),
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


def startSaver(name="org.mate.ScreenSaver", path="/org/mate/ScreenSaver", *extra, env=None):
	saver = service("saver", name, path, *extra, env=env)
	readLine(saver, "ready")
	return saver


def openDialog(saver):
	saver.stdin.write("dialog\\n")
	saver.stdin.flush()
	dialog = int(readLine(saver, "dialog ").split()[1])
	readLine(saver, "done")
	return dialog


def startSystemBus(address="unix:tmpdir=/tmp"):
	daemon = subprocess.Popen(
		["dbus-daemon", "--session", "--nofork", "--print-address=1", f"--address={{address}}"],
		stdout=subprocess.PIPE,
		stderr=open(os.path.join(os.environ["XDG_CONFIG_HOME"], f"bus{{len(started)}}.stderr"), "w"),
		text=True,
	)
	started.append(daemon)
	return daemon, daemon.stdout.readline().strip()


def startLogind(address=None):
	if address is None:
		_daemon, address = startSystemBus()
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

announced = []


def onLockStateChanged(isNowLocked):
	announced.append(isNowLocked)


def window(ofProcess, called="a window"):
	class Window(NVDAObject):
		role = controlTypes.Role.WINDOW
		processID = ofProcess
		name = called

	return Window()


def startTracking():
	post_sessionLockStateChanged.register(onLockStateChanged)
	api.setDesktopObject(Desktop())
	began = time.monotonic()
	sessionTracking.initialize()
	initializeSeconds = time.monotonic() - began
	_TrackNVDAInitialization.markInitializationComplete()
	return initializeSeconds


def lockedAfter(command=None, target=None, settle=0.0, within=5.0):
	\"\"\"Carry out command and pump until the lock state changes, at most within seconds, or for
	settle seconds when given.\"\"\"
	before = len(announced)
	if command:
		tell(target, command)
	deadline = time.monotonic() + (settle or within)
	while True:
		time.sleep(0.02)
		AutoPropertyObject.invalidateCaches()
		sessionTracking.pumpAll()
		if (len(announced) > before and not settle) or time.monotonic() > deadline:
			return sessionTracking.isLockScreenModeActive()
"""


def run(tmpPath, body: str, env: dict | None = None, timeout: float = 90) -> harness.ChildResult:
	return harness.runChild(
		tmpPath,
		PRELUDE + textwrap.dedent(body),
		prepare="appDir",
		env={
			"XDG_CONFIG_HOME": str(tmpPath / "xdgConfig"),
			"XDG_SESSION_CLASS": "user",
			"XDG_SESSION_ID": "31",
			**(env or {}),
		},
		recordLevel=logging.WARNING,
		timeout=timeout,
	)


def test_onlyEvidenceOfALockLocksAndTheLockerStaysReadable(tmp_path):
	done = run(
		tmp_path,
		"""
		saver = startSaver()
		logind = startLogind()
		# Already blanking when NVDA starts.
		tell(saver, "active true")
		startTracking()
		sessionLock._waitForFirstAttempts(10)
		result = {"blankAtStart": lockedAfter(settle=1.0)}
		dialog = openDialog(saver)
		result["dialogOpened"] = lockedAfter()
		result["whileLocked"] = {
			"mail": isWithheld(window(4242, "a private message")),
			"saver": isWithheld(window(saver.pid, "screen saver")),
			"dialog": isWithheld(window(dialog, "Unlock")),
			"desktop": isWithheld(Desktop()),
		}
		tell(saver, "nodialog")
		result["dialogClosed"] = lockedAfter(settle=1.0)
		result["saverInactive"] = lockedAfter("active false", saver)
		result["whileUnlocked"] = [isWithheld(window(4242)), isWithheld(window(dialog))]
		# mate-screensaver blanks without locking, and says only that it is active.
		result["blankOnly"] = lockedAfter("active true", saver, settle=1.5)
		tell(saver, "active false")
		# A lock request the saver does not carry out.
		result["lockRefused"] = lockedAfter("lock", logind, settle=1.5)
		tell(saver, "active false")
		tell(logind, "lock")
		result["lockCarriedOut"] = lockedAfter("active true", saver)
		result["unlockedBySaver"] = lockedAfter("active false", saver)
		tell(saver, "active true")
		result["requestAfterActive"] = lockedAfter("lock", logind)
		result["logindUnlock"] = lockedAfter("unlock", logind)
		tell(saver, "active false")
		# A locker that sets LockedHint, as GNOME Shell does.
		tell(saver, "active true")
		result["hintWhileActive"] = lockedAfter("hint true", logind)
		result["hintCleared"] = lockedAfter("hint false", logind)
		tell(saver, "active false")
		result["hintWithoutActiveSaver"] = lockedAfter("hint true", logind, settle=1.0)
		tell(logind, "hint false")
		result["announced"] = list(announced)
		stopServices()
		""",
	)
	done.assertClean()
	assert done.value == {
		"blankAtStart": False,
		"dialogOpened": True,
		"whileLocked": {"mail": True, "saver": False, "dialog": False, "desktop": False},
		"dialogClosed": True,
		"saverInactive": False,
		"whileUnlocked": [False, False],
		"blankOnly": False,
		"lockRefused": False,
		"lockCarriedOut": True,
		"unlockedBySaver": False,
		"requestAfterActive": True,
		"logindUnlock": False,
		"hintWhileActive": True,
		"hintCleared": False,
		"hintWithoutActiveSaver": False,
		"announced": [True, False, True, False, True, False, True, False],
	}
	assert not [r for r in done.logRecords if "locked" in r], done.logRecords


def test_aLockedHintLeftSetDoesNotLockAtStart(tmp_path):
	done = run(
		tmp_path,
		"""
		saver = startSaver()
		logind = startLogind()
		tell(logind, "hint true")
		startTracking()
		sessionLock._waitForFirstAttempts(10)
		result = {
			"atStart": lockedAfter(settle=1.0),
			"blanking": lockedAfter("active true", saver, settle=1.5),
			"appWindowWithheld": isWithheld(window(4242)),
		}
		stopServices()
		""",
	)
	done.assertClean()
	assert done.value == {"atStart": False, "blanking": False, "appWindowWithheld": False}


def test_aScreenSaverOfAnotherSessionLocksNothing(tmp_path):
	done = run(
		tmp_path,
		"""
		# The user's other session, on the same session bus.
		saver = startSaver(env={**os.environ, "XDG_SESSION_ID": "32"})
		startTracking()
		sessionLock._waitForFirstAttempts(10)
		tell(saver, "active true")
		openDialog(saver)
		result = lockedAfter(settle=1.5)
		stopServices()
		""",
	)
	# Its saver is not this session's, so there is nothing here that could tell.
	done.assertClean()
	assert done.value is False


def test_aSourceThatGoesTakesItsLockWithIt(tmp_path):
	done = run(
		tmp_path,
		"""
		saver = startSaver()
		other = startSaver("org.freedesktop.ScreenSaver", "/org/freedesktop/ScreenSaver")
		# Short enough for a socket's name, in the rig's own /tmp.
		busPath = "/tmp/lock_test_system_bus"
		systemBus, address = startSystemBus(f"unix:path={busPath}")
		logind = startLogind(address)
		startTracking()
		sessionLock._waitForFirstAttempts(10)
		tell(saver, "active true")
		result = {"byHint": lockedAfter("hint true", logind)}
		systemBus.kill()
		systemBus.wait()
		result["systemBusGone"] = lockedAfter()
		# The same bus comes back, and is connected to again.
		os.remove(busPath)
		_systemBus, address = startSystemBus(f"unix:path={busPath}")
		logind = startLogind(address)
		time.sleep(3)
		result["byHintAfterReconnecting"] = lockedAfter("hint true", logind)
		tell(logind, "hint false")
		lockedAfter()
		openDialog(saver)
		result["byDialog"] = lockedAfter()
		# Another saver that leaves says nothing of this one's lock.
		other.stdin.close()
		other.wait(5)
		result["otherSaverGone"] = lockedAfter(settle=1.0)
		saver.stdin.close()
		saver.wait(5)
		result["lockingSaverGone"] = lockedAfter()
		stopServices()
		""",
	)
	done.assertClean()
	assert done.value == {
		"byHint": True,
		"systemBusGone": False,
		"byHintAfterReconnecting": True,
		"byDialog": True,
		"otherSaverGone": True,
		"lockingSaverGone": False,
	}


def test_aBusThatNeverAnswersDelaysNothing(tmp_path):
	done = run(
		tmp_path,
		"""
		# A system bus that takes connections and never says a word, and a saver that never answers.
		hung = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
		hungPath = "/tmp/lock_test_hung_bus"
		hung.bind(hungPath)
		hung.listen(8)
		os.environ["DBUS_SYSTEM_BUS_ADDRESS"] = f"unix:path={hungPath}"
		silent = startSaver("org.gnome.ScreenSaver", "/org/gnome/ScreenSaver", "silent")
		saver = startSaver()
		seconds = startTracking()
		result = {"initializeSeconds": seconds, "lockedAtOnce": lockedAfter()}
		tell(saver, "active true")
		openDialog(saver)
		result["lockedByDialog"] = lockedAfter(within=15)
		result["unlocked"] = lockedAfter("active false", saver)
		stopServices()
		""",
	)
	done.assertClean()
	value = done.value
	assert value["initializeSeconds"] < 0.5, value
	assert value["lockedAtOnce"] is False
	assert value["lockedByDialog"] is True
	assert value["unlocked"] is False


def test_withoutEitherSourceTheLockCannotBeTold(tmp_path):
	done = run(
		tmp_path,
		"""
		startTracking()
		sessionLock._waitForFirstAttempts(10)
		result = sessionTracking._querySessionLocked()
		""",
	)
	done.assertClean()
	assert done.value is False
	assert [r for r in done.logRecords if "cannot tell whether this Linux session is locked" in r], (
		done.logRecords
	)
