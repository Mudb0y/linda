"""Linda started by MATE's session manager, as a sign-in starts it: from an autostart entry, from
the entry Linda's own "start after I sign in" setting writes, in the early Initialization phase, and
from MATE's own choice of screen reader; and what becomes of it when the session ends.

Each test runs a real mate-session on an X server and buses of its own, inside the rig (see
mateSession.py), with marco as its only required component. The launcher the entries name stands in
for the installed one and starts Linda from the checkout, with the transcript synth and the boot
probe plugin, which says one sentence once Linda has started.

	nix develop . -c tests/linux/rig python3 -m pytest -q tests/linux/test_mateSession.py
"""

import os
import re
import signal
import time
from pathlib import Path

import configobj
import harness
import pytest
from mateSession import MateSession

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


def test_entryLindasSettingWritesStartsLinda(tmp_path: Path):
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
		assert "X-MATE-Autostart-Phase" not in done.value["entry"], done.value["entry"]
		assertStartsAmongTheApplications(session)


def test_initializationPhaseWaitsForLinda(session: MateSession):
	session.lindaEntry("Initialization")
	session.start()
	linda = session.waitForLinda()
	spoke = linda.started + linda.waitForSpeech(SENTENCE, STARTUP_BOUND)
	ended = session.waitForPhase("INITIALIZATION", "ending", PHASE_TIMEOUT + STARTUP_BOUND)
	waited = ended - session.phaseTime("INITIALIZATION")
	# Linda spoke long before the phase ended, so the session waited on its timeout, not on Linda.
	assert spoke < ended - 10, session.log()
	assert PHASE_TIMEOUT - 1 <= waited <= PHASE_TIMEOUT + 15, (
		f"the phase took {waited:.1f} s\n{session.log()}"
	)
	assert managerSaid(session, NOT_REGISTERED), session.log()
	session.waitForPhase("RUNNING")
	assertKeysWork(session, linda)
	stopLinda(linda)


def test_logoutLeavesLindaRunningUntilTheDisplayCloses(session: MateSession):
	session.lindaEntry()
	session.start()
	linda = session.waitForLinda()
	linda.waitForSpeech(SENTENCE, STARTUP_BOUND)
	assertKeysWork(session, linda)
	asked = time.monotonic()
	session.logout()
	assert session.manager.wait(PHASE_TIMEOUT) == 0, session.log()
	ended = session.phaseTime("EXIT", "ending")
	assert ended is not None and ended - asked < 5, session.log()
	# Nothing told Linda, which speaks on for as long as the X server lasts.
	time.sleep(5)
	assert linda.process.poll() is None, linda.describe()
	mark = len(linda.spoken())
	assertKeysWork(session, linda)
	assert len(linda.spoken()) > mark
	# A display manager closes the X server once the session has ended; that is how Linda ends, in an
	# instant, and without saving the setting the key changed, logging nothing of its end.
	closed = time.monotonic()
	session.closeDisplay()
	status = linda.waitForExit(EXIT_BOUND)
	assert status == 1, linda.describe()
	assert time.monotonic() - closed < 5, linda.describe()
	assert savedSetting(session) is None
	log = linda.text("nvda.log")
	assert "NVDA exit" not in log and not harness.nvdaLogErrorRecords(log), log[-6000:]
	deadline = time.monotonic() + 5
	while session.marked() != [session.bus.pid] and time.monotonic() < deadline:
		time.sleep(0.1)
	assert session.marked() == [session.bus.pid], session.marked()


def test_registeredScreenReaderIsNotStarted(session: MateSession, tmp_path: Path):
	"""linda --register-desktop names the launcher by its path, which mate-session cannot use."""
	done = registerDesktop(session, tmp_path, str(session.launcher))
	assert done.value.startswith(f"MATE's screen reader is now {session.launcher} in place of orca"), (
		done.value
	)
	session.start()
	session.waitForPhase("RUNNING")
	time.sleep(5)
	assert session.runs() == [], session.log()
	assert managerSaid(session, "g_key_file_load_from_dirs: assertion '!g_path_is_absolute (file)' failed")


def test_screenReaderNamedByItsEntryStartsAmongTheApplications(session: MateSession, tmp_path: Path):
	"""What mate-session does with MATE's choice of screen reader when that names an application by its
	desktop entry, as Orca's default does: it starts the Exec of that entry, in the entry's phase."""
	applications = session.dataDir / "applications"
	applications.mkdir()
	entry = (harness.ROOT / "linux" / "linda.desktop").read_text(encoding="utf-8")
	entry = re.sub(r"^Exec=.*$", f"Exec={session.launcher}", entry, flags=re.MULTILINE)
	(applications / "linda.desktop").write_text(entry, encoding="utf-8")
	registerDesktop(session, tmp_path, "linda")
	assertStartsAmongTheApplications(session)


def registerDesktop(session: MateSession, tmp_path: Path, command: str) -> harness.ChildResult:
	done = harness.runChild(
		tmp_path,
		f"""
		from linuxPlatform import desktopRegistration, launcher

		launcher.lastingCommand = lambda: {command!r}
		result = desktopRegistration.register()
		""",
		env={
			"XDG_CONFIG_HOME": str(session.configHome),
			"GSETTINGS_BACKEND": "keyfile",
			"GSETTINGS_SCHEMA_DIR": os.environ["LINDA_MATE_DESKTOP_SCHEMAS"],
			"XDG_CURRENT_DESKTOP": "MATE",
		},
	)
	done.assertClean()
	return done
