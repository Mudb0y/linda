"""Linda started by MATE's session manager, as a sign-in starts it: from an autostart entry, from
the entry Linda's own "start after I sign in" setting writes, in the early Initialization phase, and
after linda --register-desktop; how it registers with the session manager; and what becomes of it when
the session ends.

Each test runs a real mate-session on an X server and buses of its own, inside the rig (see
mateSession.py), with marco as its only required component. The launcher the entries name stands in
for the installed one and starts Linda from the checkout, with the transcript synth and the boot
probe plugin, which says one sentence once Linda has started.

	nix develop . -c tests/linux/rig python3 -m pytest -q tests/linux/test_mateSession.py
"""

import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

import configobj
import harness
import pytest
from mateSession import MateSession, lindaConfig

#: What fixtures/globalPlugins/bootProbe.py says once Linda has started.
SENTENCE = "the boot probe has started"
#: Seconds from mate-session starting Linda to its first words; a start under the rig takes about three.
STARTUP_BOUND = 60.0
#: Seconds mate-session holds an early phase open for a program that has not registered.
PHASE_TIMEOUT = 30.0
#: Seconds Linda has to exit once asked to.
EXIT_BOUND = 20.0
#: NVDA+2, and the start of what it says: the command that changes whether typed characters are spoken.
COMMAND_KEY = "Insert+2"
COMMAND_SPEECH = "Speak typed characters "
#: Where that setting is saved, as section and key.
COMMAND_SETTING = ("keyboard", "speakTypedCharacters")
#: What mate-session warns of a program that held an early phase open until its timeout.
NOT_REGISTERED = "Application 'linda.desktop' failed to register before timeout"
#: Seconds Linda gives a session manager to answer its registration, as
#: linuxPlatform.sessionClient.REGISTER_TIMEOUT_MS has it.
REGISTER_TIMEOUT = 10.0


@pytest.fixture
def session(tmp_path: Path):
	with MateSession(tmp_path / "session") as started:
		started.variables["LINDA_BOOT_PROBE"] = "wait"
		yield started


def managerSaid(session: MateSession, text: str) -> bool:
	return any(text in line for _when, line in session.lines())


def waitForSpokenStart(linda: harness.Launch, start: str, since: int, timeout: float) -> str:
	"""Wait for a speak record after the first since that starts with start, and return it."""
	deadline = time.monotonic() + timeout
	while True:
		exited = linda.process.poll() is not None
		for text in linda.spokenTexts()[since:]:
			if text.startswith(start):
				return text
		if exited or time.monotonic() >= deadline:
			raise AssertionError(
				f"{start!r}… was not spoken within {timeout} s: {linda.spokenTexts()[since:]}\n{linda.describe()}"
			)
		time.sleep(0.05)


def assertKeysWork(session: MateSession, linda: harness.Launch) -> None:
	mark = len(linda.spoken())
	session.press(COMMAND_KEY)
	waitForSpokenStart(linda, COMMAND_SPEECH, mark, 10)


def savedSetting(session: MateSession) -> str | None:
	section, key = COMMAND_SETTING
	saved = configobj.ConfigObj(str(session.configHome / "linda" / "nvda.ini"), encoding="utf-8")
	return saved.get(section, {}).get(key)


def startedEnvironment(linda: harness.Launch) -> dict[str, str]:
	lines = (linda.directory / "environment").read_text(encoding="utf-8").splitlines()
	return dict(line.split("=", 1) for line in lines if "=" in line)


def stopLinda(linda: harness.Launch) -> None:
	linda.process.send_signal(signal.SIGTERM)
	assert linda.waitForExit(EXIT_BOUND) == 0, linda.describe()
	linda.assertClean(logNames=("nvda.log",))


def assertStartsAmongTheApplications(session: MateSession) -> None:
	session.start()
	running = session.waitForPhase("RUNNING")
	linda = session.waitForLinda()
	spoke = linda.started + linda.waitForSpeech(SENTENCE, STARTUP_BOUND)
	assert session.phaseTime("APPLICATION") <= linda.started <= running, session.log()
	# The session went on to its last phase without waiting for Linda's first words.
	assert running < spoke, session.log()
	assert not managerSaid(session, NOT_REGISTERED), session.log()
	assert startedEnvironment(linda).get("DESKTOP_AUTOSTART_ID"), "mate-session gave Linda no startup ID"
	assertKeysWork(session, linda)
	stopLinda(linda)


def test_entryWithoutAPhaseStartsLindaAmongTheApplications(session: MateSession):
	session.lindaEntry()
	assertStartsAmongTheApplications(session)


def startInitialization(session: MateSession) -> tuple[harness.Launch, float]:
	"""Start the session with Linda in the Initialization phase; return Linda and when it first spoke."""
	session.lindaEntry("Initialization")
	session.start()
	linda = session.waitForLinda()
	return linda, linda.started + linda.waitForSpeech(SENTENCE, STARTUP_BOUND)


def assertRegistered(linda: harness.Launch) -> str:
	"""The object path Linda's client was registered as."""
	linda.waitForLog("Session manager: registered as ", 30)
	return re.search(r"Session manager: registered as (\S+)", linda.text("nvda.log"))[1]


def assertInitializationEndsOnceLindaIsUp(session: MateSession) -> None:
	session.start()
	linda = session.waitForLinda()
	spoke = linda.started + linda.waitForSpeech(SENTENCE, STARTUP_BOUND)
	ended = session.waitForPhase("INITIALIZATION", "ending", PHASE_TIMEOUT + STARTUP_BOUND)
	assertRegistered(linda)
	# Linda registers as it says its first words, and the phase ends with that.
	assert abs(ended - spoke) < 5, f"the phase ended {ended - spoke:.1f} s after Linda spoke\n{session.log()}"
	assert ended - linda.started < PHASE_TIMEOUT / 2, session.log()
	assert not managerSaid(session, NOT_REGISTERED), session.log()
	# Before the window manager, which the rest of the desktop follows.
	assert linda.started < session.waitForPhase("WINDOW_MANAGER"), session.log()
	session.waitForPhase("RUNNING")
	assertKeysWork(session, linda)
	stopLinda(linda)
	assert "Session manager: unregistered" in linda.text("nvda.log")


def test_initializationPhaseEndsOnceLindaIsUp(session: MateSession):
	session.lindaEntry("Initialization")
	assertInitializationEndsOnceLindaIsUp(session)


def test_entryLindasSettingWritesStartsLindaFirst(tmp_path: Path):
	# A space in the launcher's path, which the entry has to quote.
	with MateSession(tmp_path / "signed in") as session:
		session.variables["LINDA_BOOT_PROBE"] = "wait"
		done = harness.runChild(
			tmp_path,
			f"""
			import config
			from linuxPlatform import launcher, xdg

			launcher.lastingCommand = lambda: {str(session.launcher)!r}
			config.setStartAfterLogon(True)
			with open(xdg.autostartEntryPath(xdg.autostartEntryName), encoding="utf-8") as f:
				result = {{"entry": f.read(), "starts": config.getStartAfterLogon()}}
			""",
			env={
				"XDG_CONFIG_HOME": str(session.configHome),
				"XDG_CONFIG_DIRS": str(session.systemConfig),
				"XDG_CURRENT_DESKTOP": "MATE",
			},
		)
		done.assertClean()
		assert done.value["starts"], done.value
		assert "\nX-MATE-Autostart-Phase=Initialization\n" in done.value["entry"], done.value["entry"]
		assertInitializationEndsOnceLindaIsUp(session)


def assertEndsPromptly(session: MateSession, asked: float) -> None:
	"""mate-session ends the session within a few seconds of the logout, and no client held it up."""
	assert session.manager.wait(PHASE_TIMEOUT) == 0, session.log()
	ended = session.phaseTime("EXIT", "ending")
	assert ended is not None and ended - asked < 5, session.log()
	assert not managerSaid(session, "failed to reply before timeout"), session.log()


def test_logoutEndsLindaWithItsSettingsSaved(session: MateSession):
	linda, _spoke = startInitialization(session)
	assertRegistered(linda)
	session.waitForPhase("RUNNING")
	assertKeysWork(session, linda)
	changed = savedSetting(session)
	asked = time.monotonic()
	session.logout()
	assert linda.waitForExit(EXIT_BOUND) == 0, linda.describe()
	exited = time.monotonic()
	assertEndsPromptly(session, asked)
	assert exited - asked < 10, f"Linda took {exited - asked:.1f} s to exit\n{linda.describe()}"
	assert changed is None and savedSetting(session) is not None, "the setting the key changed was not saved"
	log = linda.text("nvda.log")
	for said in ("asked whether the session may end", "the session is ending", "NVDA exit"):
		assert said in log, log[-6000:]
	linda.assertClean(logNames=("nvda.log",))
	# All that is left is what a display manager stops once the session has ended.
	left = sorted([session.bus.pid, session.xserver.pid])
	deadline = time.monotonic() + 5
	while sorted(session.marked()) != left and time.monotonic() < deadline:
		time.sleep(0.1)
	assert sorted(session.marked()) == left


def test_cancelledLogoutLeavesLindaRunning(session: MateSession):
	from gi.repository import Gio, GLib

	linda, _spoke = startInitialization(session)
	assertRegistered(linda)
	session.waitForPhase("RUNNING")
	# A program that holds the logout up, so that mate-session asks whether to log out regardless.
	inhibitor = Gio.DBusConnection.new_for_address_sync(
		session.busAddress,
		Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
		None,
		None,
	)
	try:
		inhibitor.call_sync(
			"org.gnome.SessionManager",
			"/org/gnome/SessionManager",
			"org.gnome.SessionManager",
			"Inhibit",
			GLib.Variant("(susu)", ("holder", 0, "a test holds the logout", 1)),
			None,
			Gio.DBusCallFlags.NONE,
			5000,
			None,
		)
		mark = len(linda.spoken())
		session.logout()
		waitForSpokenStart(linda, "dialog A program is still running", mark, 20)
		session.press("Escape")
		linda.waitForLog("Session manager: the session goes on", 20)
		assert managerSaid(session, "Cancelling the end of session"), session.log()
		assertKeysWork(session, linda)
		assert linda.process.poll() is None and session.manager.poll() is None
		# The logout that follows, with nothing holding it up, ends Linda as if none had been cancelled.
		inhibitor.close_sync(None)
		asked = time.monotonic()
		session.logout()
		assert linda.waitForExit(EXIT_BOUND) == 0, linda.describe()
		assertEndsPromptly(session, asked)
		linda.assertClean(logNames=("nvda.log",))
	finally:
		if not inhibitor.is_closed():
			inhibitor.close_sync(None)


def test_restartedCopyIsNotTheSessionsClient(session: MateSession):
	"""A copy NVDA starts in its own place gets no startup ID, so it runs as one started by hand."""
	session.variables["LINDA_BOOT_PROBE"] = "restart"
	session.lindaEntry()
	session.start()
	first = session.waitForLinda()
	first.waitForSpeech(SENTENCE, STARTUP_BOUND)
	assert first.waitForExit(EXIT_BOUND) == 0, first.describe()
	# The new copy, which says the sentence too and exits, moved the first copy's log aside, which
	# ended with the same last words. It is not the first copy's process, whose exit waitForLog stops at.
	deadline = time.monotonic() + STARTUP_BOUND
	while not re.search(f"{re.escape(NOT_STARTED_BY_ONE)}.*NVDA exit", first.text("nvda.log"), re.DOTALL):
		assert time.monotonic() < deadline, f"the new copy did not start and exit\n{first.describe()}"
		time.sleep(0.1)
	old = first.text("nvda-old.log")
	assert "Session manager: started NVDA with the startup ID " in old, old[-6000:]
	# The probe restarts NVDA as it starts, which can be before the registration is answered.
	if "Session manager: registered as " in old:
		assert "Session manager: unregistered" in old, old[-6000:]
	assert sessionManagerRecords(first) == [NOT_STARTED_BY_ONE], first.text("nvda.log")[-6000:]
	first.assertClean(logNames=("nvda-old.log", "nvda.log"))


#: What Linda logs when no session manager started it.
NOT_STARTED_BY_ONE = "Session manager: none started NVDA, as DESKTOP_AUTOSTART_ID was not set"
#: A stand-in for a session manager that takes registrations and never answers one, on the rig's bus.
SILENT_MANAGER = """
from gi.repository import Gio, GLib

INTERFACE = '''<node><interface name="org.gnome.SessionManager"><method name="RegisterClient">
<arg direction="in" type="s"/><arg direction="in" type="s"/><arg direction="out" type="o"/>
</method></interface></node>'''
held = []


def onCall(connection, sender, path, interface, method, parameters, invocation):
	held.append(invocation)
	print("asked", method, *parameters.unpack(), flush=True)


bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
bus.register_object(
	"/org/gnome/SessionManager", Gio.DBusNodeInfo.new_for_xml(INTERFACE).interfaces[0], onCall, None, None
)
bus.call_sync(
	"org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "RequestName",
	GLib.Variant("(su)", ("org.gnome.SessionManager", 4)), None, Gio.DBusCallFlags.NONE, -1, None,
)
print("ready", flush=True)
loop = GLib.MainLoop()
GLib.io_add_watch(GLib.IOChannel.unix_new(0), GLib.PRIORITY_DEFAULT, GLib.IO_IN | GLib.IO_HUP, lambda *a: loop.quit())
loop.run()
"""


def startOutsideASession(launches, tmp_path: Path, name: str, env: dict[str, str | None]) -> harness.Launch:
	"""Linda on the rig's display and bus, as a start by hand starts it, with env over the rig's."""
	config = lindaConfig(tmp_path / "configs" / name)
	return launches(
		name,
		"-c",
		str(config.parent),
		"-f",
		str(launches.directory / name / "nvda.log"),
		env={"LINDA_BOOT_PROBE": "wait", **env},
	)


def assertRunsAsAlways(linda: harness.Launch) -> None:
	"""Linda speaks, takes a command key and exits on SIGTERM, each in its usual time."""
	assert linda.waitForSpeech(SENTENCE, STARTUP_BOUND) < STARTUP_BOUND / 4, linda.describe()
	mark = len(linda.spoken())
	subprocess.run(["xdotool", "key", COMMAND_KEY], check=True)
	waitForSpokenStart(linda, COMMAND_SPEECH, mark, 10)
	asked = time.monotonic()
	linda.process.send_signal(signal.SIGTERM)
	assert linda.waitForExit(EXIT_BOUND) == 0, linda.describe()
	assert time.monotonic() - asked < 5, linda.describe()
	linda.assertClean(logNames=("nvda.log",))


def sessionManagerRecords(linda: harness.Launch) -> list[str]:
	return re.findall(r"^Session manager: .*$", linda.text("nvda.log"), re.MULTILINE)


def test_sessionManagerThatNeverAnswersCostsNothing(launches, tmp_path: Path):
	# A bus of its own, as the rig's accessibility bus launcher would try to register with it too.
	with open(tmp_path / "bus.log", "w") as busLog:
		bus = subprocess.Popen(
			[
				"dbus-daemon",
				f"--config-file={harness.HERE / 'rig-session.conf'}",
				"--nofork",
				"--print-address=1",
			],
			stdout=subprocess.PIPE,
			stderr=busLog,
			text=True,
		)
	try:
		address = bus.stdout.readline().strip()
		with subprocess.Popen(
			[sys.executable, "-I", "-c", SILENT_MANAGER],
			stdin=subprocess.PIPE,
			stdout=subprocess.PIPE,
			text=True,
			env={**os.environ, "DBUS_SESSION_BUS_ADDRESS": address},
		) as manager:
			try:
				assert manager.stdout.readline() == "ready\n"
				linda = startOutsideASession(
					launches,
					tmp_path,
					"silent",
					{"DESKTOP_AUTOSTART_ID": "an-id", "DBUS_SESSION_BUS_ADDRESS": address},
				)
				linda.waitForSpeech(SENTENCE, STARTUP_BOUND)
				assert manager.stdout.readline() == "asked RegisterClient linda.desktop an-id\n"
				# The registration gives up in its own time, and says so once.
				linda.waitForLog("could not register", REGISTER_TIMEOUT + 20)
				assertRunsAsAlways(linda)
				assert sessionManagerRecords(linda) == [
					"Session manager: started NVDA with the startup ID an-id",
					(
						"Session manager: could not register with org.gnome.SessionManager, so NVDA runs"
						" unregistered: Timeout was reached"
					),
				], linda.text("nvda.log")[-6000:]
			finally:
				manager.stdin.close()
				manager.wait(10)
	finally:
		bus.terminate()
		bus.wait(10)


def test_startupIdWithNoSessionManagerIsLoggedOnce(launches, tmp_path: Path):
	linda = startOutsideASession(launches, tmp_path, "noManager", {"DESKTOP_AUTOSTART_ID": "an-id"})
	assertRunsAsAlways(linda)
	records = sessionManagerRecords(linda)
	assert len(records) == 2 and records[1].startswith("Session manager: could not register with"), records


def test_startOutsideASessionManagerIsUnchanged(launches, tmp_path: Path):
	linda = startOutsideASession(launches, tmp_path, "byHand", {"DESKTOP_AUTOSTART_ID": None})
	assertRunsAsAlways(linda)
	assert sessionManagerRecords(linda) == [NOT_STARTED_BY_ONE]


#: Seconds after the session's last phase begins in which no second Linda may start.
SECOND_COPY_WAIT = 5.0


def installApplicationEntry(session: MateSession) -> None:
	"""Linda's application entry, as the package installs it, naming the launcher the session starts."""
	applications = session.dataDir / "applications"
	applications.mkdir(exist_ok=True)
	entry = (harness.ROOT / "linux" / "linda.desktop").read_text(encoding="utf-8")
	entry = re.sub(r"^Exec=.*$", f"Exec={session.launcher}", entry, flags=re.MULTILINE)
	(applications / "linda.desktop").write_text(entry, encoding="utf-8")


def test_registeredLindaStartsOnceInTheInitializationPhase(session: MateSession, tmp_path: Path):
	installApplicationEntry(session)
	done = desktopRegistration(session, tmp_path, "register")
	assert done.value.startswith("MATE's screen reader is now linda in place of orca."), done.value
	session.start()
	linda = session.waitForLinda()
	spoke = linda.started + linda.waitForSpeech(SENTENCE, STARTUP_BOUND)
	ended = session.waitForPhase("INITIALIZATION", "ending", PHASE_TIMEOUT + STARTUP_BOUND)
	assert session.phaseTime("INITIALIZATION") <= linda.started <= ended, session.log()
	assert abs(ended - spoke) < 5, f"the phase ended {ended - spoke:.1f} s after Linda spoke\n{session.log()}"
	assertRegistered(linda)
	time.sleep(max(0, session.waitForPhase("RUNNING") + SECOND_COPY_WAIT - time.monotonic()))
	assert len(session.runs()) == 1, session.log()
	assertKeysWork(session, linda)
	stopLinda(linda)


def test_unregisteredLindaIsNotStarted(session: MateSession, tmp_path: Path):
	installApplicationEntry(session)
	desktopRegistration(session, tmp_path, "register")
	done = desktopRegistration(session, tmp_path, "unregister")
	assert done.value.startswith("MATE's screen reader is back to its default, orca, in place of linda."), (
		done.value
	)
	session.start()
	time.sleep(max(0, session.waitForPhase("RUNNING") + SECOND_COPY_WAIT - time.monotonic()))
	assert session.runs() == [], session.log()


def desktopRegistration(session: MateSession, tmp_path: Path, action: str) -> harness.ChildResult:
	"""linda --register-desktop or --unregister-desktop, as an installed copy runs it in the session."""
	done = harness.runChild(
		tmp_path,
		f"""
		from linuxPlatform import desktopRegistration, launcher

		launcher.lastingCommand = lambda: {str(session.launcher)!r}
		result = desktopRegistration.{action}()
		""",
		env={
			"XDG_CONFIG_HOME": str(session.configHome),
			"XDG_CONFIG_DIRS": str(session.systemConfig),
			"GSETTINGS_BACKEND": "keyfile",
			"GSETTINGS_SCHEMA_DIR": os.environ["LINDA_MATE_DESKTOP_SCHEMAS"],
			"XDG_CURRENT_DESKTOP": "MATE",
		},
	)
	done.assertClean()
	return done
