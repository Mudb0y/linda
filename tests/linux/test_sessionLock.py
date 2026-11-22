"""The locked session, as logind and the desktop's screen saver report it, and what NVDA withholds then.

A stand-in screen saver owns org.mate.ScreenSaver on the rig's session bus. The rig has no system bus,
so a stand-in logind answers on a bus daemon of the test's own, which DBUS_SYSTEM_BUS_ADDRESS names.
Both the child and the stand-ins have the same XDG_SESSION_ID, which makes the saver this session's,
except in the tests of the paths through logind: there the child has no XDG_SESSION_ID, as under
GNOME, or a saver's environment cannot be read, as KWin's under Plasma, and the stand-in logind says
which session each process is in.

nix develop . -c tests/linux/rig python3 -m pytest tests/linux/test_sessionLock.py
"""

import logging
import textwrap

import harness
import pytest
from desktopSession import DesktopSession

SERVICES = harness.HERE / "fixtures" / "lockServices.py"
SESSION_PATH = "/org/freedesktop/login1/session/_31"
SESSION_CONFIG = harness.HERE / "fixtures" / "sessionConfig" / "nvda.ini"

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


def startLogind(address=None, *settings):
	if address is None:
		_daemon, address = startSystemBus()
	os.environ["DBUS_SYSTEM_BUS_ADDRESS"] = address
	logind = service("logind", address, SESSION_PATH, *settings)
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

#: For the tests of the paths through logind, where the environment cannot say which session a
#: process is of.
PRELUDE += r"""
import json

ANOTHER_SESSION = "/org/freedesktop/login1/session/_32"


def environment(**variables):
	# This process's environment with only the session variables given, so that a stand-in can
	# name its session, another, or none at all as GNOME Shell does.
	env = {k: v for k, v in os.environ.items() if k not in ("XDG_SESSION_ID", "DISPLAY")}
	env.update(variables)
	return env


def startOwner(name, env=None):
	owner = service("owner", name, env=env)
	readLine(owner, "ready")
	return owner


def calls(logind):
	logind.stdin.write("calls\n")
	logind.stdin.flush()
	answer = json.loads(readLine(logind, "calls ").split(" ", 1)[1])
	readLine(logind, "done")
	return answer


def cgroupApart(processID):
	# The rig's cgroup namespace shows every process in one control group, where KWin has its own;
	# the same control group would make the saver this session's without asking logind.
	read = sessionLock._cgroupOf
	sessionLock._cgroupOf = lambda pid: (
		"0::/user.slice/user-1000.slice/user@1000.service/session.slice/plasma-kwin_wayland.service\n"
		if pid == processID
		else read(pid)
	)


def withheld(*processes):
	return [isWithheld(window(process.pid if hasattr(process, "pid") else process)) for process in processes]


def waitUntil(condition, within):
	deadline = time.monotonic() + within
	while not condition():
		if time.monotonic() > deadline:
			return False
		lockedAfter(settle=0.2)
	return True
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


def test_lockIsSpokenAnnouncementsAreWithheldAndTheFocusIsReadAfter(tmp_path):
	done = run(
		tmp_path,
		"""
		import eventHandler
		import queueHandler
		import ui
		from linuxPlatform.atspi import handler

		spoken = []
		ui.message = lambda text, *args, **kwargs: spoken.append(text)
		queued = []
		eventHandler.queueEvent = lambda name, obj, **kwargs: queued.append((name, obj.name))
		NVDAObject.objectWithFocus = staticmethod(lambda: window(4242, "the editor"))
		processes = {"chat": 4242}
		handler.processIDOf = lambda node: processes[node]

		saver = startSaver()
		startTracking()
		sessionLock._waitForFirstAttempts(10)
		tell(saver, "active true")
		processes["dialog"] = openDialog(saver)
		lockedAfter()
		queueHandler.pumpAll()
		result = {"onLock": list(spoken)}
		spoken.clear()
		handler._speakAnnouncement("a private message", "chat")
		handler._speakAnnouncement("Incorrect password.", "dialog")
		result["announcedWhileLocked"] = list(spoken)
		spoken.clear()
		lockedAfter("active false", saver)
		lockedAfter(settle=0.6)
		queueHandler.pumpAll()
		result["onUnlock"] = list(spoken)
		result["refocused"] = list(queued)
		spoken.clear()
		handler._speakAnnouncement("a public message", "chat")
		result["announcedUnlocked"] = list(spoken)
		# Once something has taken the focus since the lock ended, nothing is read again.
		queued.clear()
		tell(saver, "active true")
		openDialog(saver)
		lockedAfter()
		lockedAfter("active false", saver)
		eventHandler.lastQueuedFocusObject = window(4243, "a new window")
		lockedAfter(settle=0.6)
		result["refocusedAfterAFocus"] = list(queued)
		stopServices()
		""",
	)
	done.assertClean()
	assert done.value == {
		"onLock": ["Screen locked"],
		"announcedWhileLocked": ["Incorrect password."],
		"onUnlock": ["Screen unlocked"],
		"refocused": [["gainFocus", "the editor"]],
		"announcedUnlocked": ["a public message"],
		"refocusedAfterAFocus": [],
	}


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


def test_unlockPromptNamesItsFieldAndSpeaksItsMessages(tmp_path):
	with DesktopSession(tmp_path / "desktop") as desktop:
		prompt = desktop.startApp(desktop.fixture("mate-screensaver-dialog"), title="Unlock prompt")
		assert prompt.stdout.readline() == "ready\n"
		desktop.startNVDA(SESSION_CONFIG).waitForLog("NVDA initialized", 60)
		desktop.expect("Password: edit protected blank", since=0, timeout=15)
		# Two seconds apart, as the real prompt renames its label.
		for message in ("Checking...", "Incorrect password."):
			mark = desktop.mark()
			prompt.stdin.write(message + "\n")
			prompt.stdin.flush()
			assert prompt.stdout.readline() == "done\n"
			desktop.expect(message, since=mark, timeout=5)
		# The clock above, renamed every second, is not read.
		spoken = desktop.spokenSince(0)
		assert not [text for text in spoken if text.count(":") == 2 and text[:2].isdigit()], spoken


# What package LK added for GNOME and Plasma, where this process's environment or a saver's cannot
# say which session it is of and logind decides.

NO_SESSION = {"XDG_SESSION_ID": None}


def test_inNoSessionTheDisplaySessionIsLindasAndTheShellDrawsTheLock(tmp_path):
	"""As under GNOME: Linda, started by the user's manager, is in no session; the screen saver's
	proxy shares its DISPLAY, and GNOME Shell, which draws the lock screen, says neither."""
	done = run(
		tmp_path,
		"""
		shell = startOwner("org.gnome.Shell.ScreenShield", env=environment())
		saver = startSaver(
			"org.gnome.ScreenSaver", "/org/gnome/ScreenSaver", env=environment(DISPLAY=os.environ["DISPLAY"])
		)
		logind = startLogind(None, "others=none")
		startTracking()
		sessionLock._waitForFirstAttempts(10)
		result = {"atStart": lockedAfter(settle=1.0)}
		tell(logind, "lock")
		result["lockCarriedOut"] = lockedAfter("active true", saver)
		result["withheldWhileLocked"] = withheld(4242, saver, shell)
		result["logindUnlock"] = lockedAfter("unlock", logind)
		result["withheldUnlocked"] = withheld(4242)
		tell(saver, "active false")
		tell(saver, "active true")
		result["hintWhileActive"] = lockedAfter("hint true", logind)
		result["hintCleared"] = lockedAfter("hint false", logind)
		tell(saver, "active false")
		asked = calls(logind)
		result["askedOf"] = sorted(
			who
			for who, pid in (("linda", os.getpid()), ("saver", saver.pid), ("shell", shell.pid))
			if pid in asked["pids"]
		)
		result["displayAsked"] = asked.get("Display", 0) > 0
		result["announced"] = list(announced)
		stopServices()
		""",
		env=NO_SESSION,
	)
	done.assertClean()
	assert done.value == {
		"atStart": False,
		"lockCarriedOut": True,
		"withheldWhileLocked": [True, False, False],
		"logindUnlock": False,
		"withheldUnlocked": [False],
		"hintWhileActive": True,
		"hintCleared": False,
		# The saver is this session's by its DISPLAY, so logind is not asked of it.
		"askedOf": ["linda", "shell"],
		"displayAsked": True,
		"announced": [True, False, True, False],
	}


def test_inNoSessionASaverThatComesLaterIsAskedOfAndAnotherSessionsIsNot(tmp_path):
	done = run(
		tmp_path,
		"""
		shell = startOwner("org.gnome.Shell.ScreenShield", env=environment())
		logind = startLogind(None, "others=none")
		startTracking()
		sessionLock._waitForFirstAttempts(10)
		# Its DISPLAY names another session, which logind's answer for a process in none must not overrule.
		other = startSaver(
			"org.freedesktop.ScreenSaver", "/org/freedesktop/ScreenSaver", env=environment(DISPLAY=":98")
		)
		# Only logind can say whose this one is.
		saver = startSaver("org.gnome.ScreenSaver", "/org/gnome/ScreenSaver", env=environment())
		lockedAfter(settle=0.5)
		tell(logind, "lock")
		result = {"otherSessionsSaver": lockedAfter("active true", other, settle=1.5)}
		tell(other, "active false")
		tell(logind, "lock")
		result["lockCarriedOut"] = lockedAfter("active true", saver)
		result["withheldWhileLocked"] = withheld(4242, saver, shell, other)
		result["saverInactive"] = lockedAfter("active false", saver)
		asked = calls(logind)
		result["askedOf"] = sorted(
			who
			for who, pid in (("other", other.pid), ("saver", saver.pid), ("shell", shell.pid))
			if pid in asked["pids"]
		)
		stopServices()
		""",
		env=NO_SESSION,
	)
	done.assertClean()
	assert done.value == {
		"otherSessionsSaver": False,
		"lockCarriedOut": True,
		"withheldWhileLocked": [True, False, False, True],
		"saverInactive": False,
		"askedOf": ["saver", "shell"],
	}


def test_inNoSessionALogindThatComesLateIsHeard(tmp_path):
	done = run(
		tmp_path,
		"""
		shell = startOwner("org.gnome.Shell.ScreenShield", env=environment())
		saver = startSaver("org.gnome.ScreenSaver", "/org/gnome/ScreenSaver", env=environment())
		_daemon, address = startSystemBus()
		os.environ["DBUS_SYSTEM_BUS_ADDRESS"] = address
		startTracking()
		sessionLock._waitForFirstAttempts(10)
		tell(saver, "active true")
		result = {"beforeLogind": lockedAfter(settle=1.0)}
		tell(saver, "active false")
		logind = service("logind", address, SESSION_PATH, "others=none")
		readLine(logind, "ready")
		lockedAfter(settle=1.0)
		tell(logind, "lock")
		result["lockCarriedOut"] = lockedAfter("active true", saver)
		result["withheldWhileLocked"] = withheld(4242, saver, shell)
		result["logindUnlock"] = lockedAfter("unlock", logind)
		stopServices()
		""",
		env=NO_SESSION,
	)
	done.assertClean()
	assert done.value == {
		"beforeLogind": False,
		"lockCarriedOut": True,
		"withheldWhileLocked": [True, False, False],
		"logindUnlock": False,
	}


@pytest.mark.parametrize(
	"settings",
	[
		pytest.param(("others=denied",), id="anotherError"),
		pytest.param(("others=none", "display=/"), id="noDisplaySession"),
		pytest.param(("others=none", "user=false"), id="noSuchUser"),
	],
)
def test_inNoSessionWithoutTheDisplaySessionNothingLocks(tmp_path, settings):
	done = run(
		tmp_path,
		f"""
		shell = startOwner("org.gnome.Shell.ScreenShield", env=environment())
		saver = startSaver(
			"org.gnome.ScreenSaver", "/org/gnome/ScreenSaver", env=environment(DISPLAY=os.environ["DISPLAY"])
		)
		logind = startLogind(None, *{settings!r})
		startTracking()
		sessionLock._waitForFirstAttempts(10)
		tell(logind, "lock")
		result = {{"lockRequested": lockedAfter("active true", saver, settle=1.5)}}
		result["hint"] = lockedAfter("hint true", logind, settle=1.0)
		asked = calls(logind)
		result["askedOf"] = ["linda" if pid == os.getpid() else pid for pid in asked["pids"]]
		stopServices()
		""",
		env=NO_SESSION,
	)
	done.assertClean()
	# With no session of its own, nothing else is asked of logind.
	assert done.value == {"lockRequested": False, "hint": False, "askedOf": ["linda"]}


@pytest.mark.parametrize(
	("settings", "expected"),
	[
		pytest.param(
			("{pid}=" + SESSION_PATH,),
			{
				"lockCarriedOut": True,
				"withheld": [True, False],
				"hint": True,
				"unlocked": False,
				"user": False,
			},
			id="inThisSession",
		),
		pytest.param(
			("{pid}=none",),
			{
				"lockCarriedOut": True,
				"withheld": [True, False],
				"hint": True,
				"unlocked": False,
				"user": True,
			},
			id="inNoSessionWithThisTheDisplaySession",
		),
		pytest.param(
			("{pid}=/org/freedesktop/login1/session/_32",),
			{
				"lockCarriedOut": False,
				"withheld": [False, False],
				"hint": False,
				"unlocked": False,
				"user": False,
			},
			id="inAnotherSession",
		),
		pytest.param(
			("{pid}=none", "display=/org/freedesktop/login1/session/_32"),
			{
				"lockCarriedOut": False,
				"withheld": [False, False],
				"hint": False,
				"unlocked": False,
				"user": True,
			},
			id="inNoSessionWithAnotherTheDisplaySession",
		),
	],
)
def test_inASessionAnUnreadableSaverIsThisSessionsWhereLogindPutsIt(tmp_path, settings, expected):
	"""As under Plasma, whose kwin_wayland cannot be read, and as an X11 session with XDG_SESSION_ID
	reaches it too: the rig's X session is this one, with XDG_SESSION_ID and DISPLAY."""
	done = run(
		tmp_path,
		f"""
		saver = startSaver("org.freedesktop.ScreenSaver", "/org/freedesktop/ScreenSaver", "undumpable")
		cgroupApart(saver.pid)
		logind = startLogind(None, *[setting.format(pid=saver.pid) for setting in {settings!r}])
		startTracking()
		sessionLock._waitForFirstAttempts(10)
		result = {{"environmentRead": sessionLock._environmentOf(saver.pid) is not None}}
		tell(logind, "lock")
		result["lockCarriedOut"] = lockedAfter("active true", saver, within=3)
		result["withheld"] = withheld(4242, saver)
		tell(logind, "unlock")
		tell(saver, "active false")
		lockedAfter(settle=0.3)
		tell(saver, "active true")
		result["hint"] = lockedAfter("hint true", logind, within=3)
		result["unlocked"] = lockedAfter("active false", saver, within=3)
		asked = calls(logind)
		result["askedOf"] = ["saver" if pid == saver.pid else pid for pid in asked["pids"]]
		result["user"] = asked.get("GetUser", 0) > 0
		result["sessionAsked"] = asked.get("GetSession", 0)
		stopServices()
		""",
	)
	done.assertClean()
	value = done.value
	assert value.pop("environmentRead") is False
	assert value.pop("askedOf") == ["saver"], value
	assert value.pop("sessionAsked") == 1, value
	assert value == expected


@pytest.mark.parametrize(
	("shellSession", "shellWithheld"),
	[pytest.param("31", False, id="thisSession"), pytest.param("32", True, id="anotherSession")],
)
def test_inASessionGnomeShellDrawsTheLockOnlyOfItsOwnSession(tmp_path, shellSession, shellWithheld):
	done = run(
		tmp_path,
		f"""
		shell = startOwner(
			"org.gnome.Shell.ScreenShield",
			env=environment(XDG_SESSION_ID={shellSession!r}, DISPLAY=os.environ["DISPLAY"]),
		)
		saver = startSaver("org.gnome.ScreenSaver", "/org/gnome/ScreenSaver")
		logind = startLogind()
		startTracking()
		sessionLock._waitForFirstAttempts(10)
		tell(logind, "lock")
		result = {{"lockCarriedOut": lockedAfter("active true", saver)}}
		result["withheld"] = withheld(4242, saver, shell)
		result["unlocked"] = lockedAfter("unlock", logind)
		result["withheldUnlocked"] = withheld(shell)
		result["calls"] = calls(logind)
		stopServices()
		""",
	)
	done.assertClean()
	assert done.value == {
		"lockCarriedOut": True,
		"withheld": [True, False, shellWithheld],
		"unlocked": False,
		"withheldUnlocked": [False],
		# The environments say it all, so logind is asked nothing of any process.
		"calls": {"pids": [], "GetSession": 1},
	}


def test_inNoSessionTheSystemBusGoingTakesTheShellWithItUntilItIsBack(tmp_path):
	done = run(
		tmp_path,
		"""
		shell = startOwner("org.gnome.Shell.ScreenShield", env=environment())
		saver = startSaver(
			"org.gnome.ScreenSaver", "/org/gnome/ScreenSaver", env=environment(DISPLAY=os.environ["DISPLAY"])
		)
		busPath = "/tmp/lock_test_system_bus"
		systemBus, address = startSystemBus(f"unix:path={busPath}")
		logind = startLogind(address, "others=none")
		startTracking()
		sessionLock._waitForFirstAttempts(10)
		tell(logind, "lock")
		result = {"lockCarriedOut": lockedAfter("active true", saver)}
		result["withheldWhileLocked"] = withheld(4242, saver, shell)
		systemBus.kill()
		systemBus.wait()
		# The saver is this session's by its DISPLAY and carried out the request: it is still locked.
		result["systemBusGone"] = lockedAfter(settle=1.0)
		result["withheldWhileGone"] = withheld(4242, saver, shell)
		os.remove(busPath)
		_systemBus, address = startSystemBus(f"unix:path={busPath}")
		logind = startLogind(address, "others=none")
		result["shellBack"] = waitUntil(lambda: not isWithheld(window(shell.pid)), 20)
		result["stillLocked"] = sessionTracking.isLockScreenModeActive()
		result["logindUnlock"] = lockedAfter("unlock", logind)
		stopServices()
		""",
		env=NO_SESSION,
	)
	done.assertClean()
	assert done.value == {
		"lockCarriedOut": True,
		"withheldWhileLocked": [True, False, False],
		"systemBusGone": True,
		"withheldWhileGone": [True, False, True],
		"shellBack": True,
		"stillLocked": True,
		"logindUnlock": False,
	}


def test_inASessionASaverMatchedByLogindGoesWithTheSystemBus(tmp_path):
	done = run(
		tmp_path,
		"""
		saver = startSaver("org.freedesktop.ScreenSaver", "/org/freedesktop/ScreenSaver", "undumpable")
		cgroupApart(saver.pid)
		busPath = "/tmp/lock_test_system_bus"
		systemBus, address = startSystemBus(f"unix:path={busPath}")
		logind = startLogind(address)
		startTracking()
		sessionLock._waitForFirstAttempts(10)
		tell(logind, "lock")
		result = {"lockCarriedOut": lockedAfter("active true", saver)}
		systemBus.kill()
		systemBus.wait()
		result["systemBusGone"] = lockedAfter()
		result["withheldWhileGone"] = withheld(4242)
		os.remove(busPath)
		_systemBus, address = startSystemBus(f"unix:path={busPath}")
		logind = startLogind(address)
		result["askedAgain"] = waitUntil(lambda: saver.pid in calls(logind)["pids"], 20)
		# Still active, but what made its spell a lock went with the bus.
		result["stillActiveAfterReconnecting"] = lockedAfter(settle=1.0)
		tell(saver, "active false")
		tell(logind, "lock")
		result["lockAfterReconnecting"] = lockedAfter("active true", saver)
		result["unlockedBySaver"] = lockedAfter("active false", saver)
		stopServices()
		""",
	)
	done.assertClean()
	assert done.value == {
		"lockCarriedOut": True,
		"systemBusGone": False,
		"withheldWhileGone": [False],
		"askedAgain": True,
		"stillActiveAfterReconnecting": False,
		"lockAfterReconnecting": True,
		"unlockedBySaver": False,
	}


def test_inASessionWithReadableSaversLogindIsAskedNothingNew(tmp_path):
	"""The owner's MATE session: XDG_SESSION_ID set and every saver's environment readable, where logind
	is asked only for the session, and is not heard though it would put every saver in this one."""
	done = run(
		tmp_path,
		"""
		saver = startSaver()
		# A saver that says nothing of its session, and one of another.
		bare = startSaver("org.freedesktop.ScreenSaver", "/org/freedesktop/ScreenSaver", env=environment())
		other = startSaver(
			"org.gnome.ScreenSaver", "/org/gnome/ScreenSaver", env=environment(XDG_SESSION_ID="32")
		)
		logind = startLogind()
		startTracking()
		sessionLock._waitForFirstAttempts(10)
		result = {}
		for name, process in (("bare", bare), ("other", other)):
			tell(logind, "lock")
			result[name] = lockedAfter("active true", process, settle=1.5)
			tell(process, "active false")
		tell(logind, "lock")
		result["mate"] = lockedAfter("active true", saver)
		result["withheld"] = withheld(4242, saver, bare, other)
		result["unlocked"] = lockedAfter("active false", saver)
		result["calls"] = calls(logind)
		stopServices()
		""",
	)
	done.assertClean()
	assert done.value == {
		"bare": False,
		"other": False,
		"mate": True,
		"withheld": [True, False, True, True],
		"unlocked": False,
		"calls": {"pids": [], "GetSession": 1},
	}
