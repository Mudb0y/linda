"""A session as a person using Basedreader as their only screen reader has one: Alt+Tab between a GTK
and a Qt application under marco, moving and typing in a form, reviewing, reading the window and
cutting it short, input help, the NVDA menu, and exiting through NVDA's own dialog, with every key
pressed on the rig's display.

nix develop . -c tests/linux/rig env BASEDREADER_RIG_WM=marco python3 -m pytest -q tests/linux/test_usable.py

Without BASEDREADER_RIG_WM=marco, DesktopSession starts marco itself. The transcript holds what the
synthesizer is given, after symbols are spoken as words, so "Alt+P" is "Alt plus P".
"""

import re
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import desktopSession
import harness
import pytest
from desktopSession import DesktopSession

SESSION_CONFIG = harness.HERE / "fixtures" / "sessionConfig" / "nvda.ini"
#: The transcript synth takes 10 ms a character, so that reading takes long enough to be cut short.
TIMED_SPEECH = {"BASEDREADER_TRANSCRIPT_MODE": "timed", "BASEDREADER_TRANSCRIPT_MS_PER_CHAR": "10"}
#: Seconds from starting NVDA to its first words; a start under the rig takes about three.
STARTUP_BOUND = 60.0
#: Seconds from a key that cuts speech short to the synth being told to stop.
CANCEL_BOUND = 0.1
#: Seconds NVDA has to exit once its exit dialog is answered.
EXIT_BOUND = 10.0
#: Seconds to wait for anything more after what a step expects.
SETTLE = 0.5

GTK = ("gtkFixture", "Fixture window")
QT = ("qtFixture", "Qt fixture window")
WX = ("wxfixture", "wx fixture")


@pytest.fixture
def desktop(tmp_path: Path) -> Iterator[DesktopSession]:
	with DesktopSession(tmp_path / "desktop") as session:
		yield session


def startSession(desktop: DesktopSession, *apps: tuple[str, str]) -> harness.Launch:
	"""Start each application in turn, so that the last is active, and then NVDA."""
	for name, title in apps:
		app = desktop.startApp(desktop.fixture(name), title=title)
		# On the accessibility desktop before it has presented itself and has the keyboard.
		assert app.stdout.readline() == "ready\n"
	desktopSession.activate(apps[-1][1])
	return desktop.startNVDA(SESSION_CONFIG, env=TIMED_SPEECH)


def step(desktop: DesktopSession, chord: str, *expected: str, timeout: float = 5) -> int:
	"""Press chord and wait for expected, in order; return the mark taken before the key."""
	mark = desktop.mark()
	desktop.press(chord)
	desktop.expect(*expected, since=mark, timeout=timeout)
	return mark


def transcript(nvda: harness.Launch) -> list[dict]:
	"""Every record the transcript synth made: speak, cancel and pause."""
	path = nvda.directory / "transcript.jsonl"
	return harness.transcriptRecords().readRecords(path) if path.exists() else []


def assertOnceAndTogether(spoken: list[str], first: str, second: str) -> None:
	"""first and second were each spoken once, second straight after first."""
	assert spoken.count(first) == 1 and spoken.count(second) == 1, spoken
	assert spoken.index(second) == spoken.index(first) + 1, spoken


def assertExitedCleanly(nvda: harness.Launch) -> None:
	assert nvda.process.returncode == 0, nvda.describe()
	nvda.assertClean(logNames=("nvda.log",))


def test_usableSession(desktop: DesktopSession):
	nvda = startSession(desktop, QT, GTK)
	desktop.expect(
		"Fixture window", "Press me button Does nothing Alt plus P", since=0, timeout=STARTUP_BOUND
	)

	step(desktop, "Tab", "Enable sound check box not checked Alt plus E")
	step(desktop, "space", "checked")
	step(desktop, "Tab", "Size grouping", "Medium radio button checked 2 of 3")
	step(desktop, "Tab", "Colour: combo box Green collapsed Alt plus C")
	step(desktop, "Tab", "Name: edit Alt plus N selected Alice")

	# Each letter after the one before has been spoken: a key that types cuts short what is still
	# being said, as on Windows, and "selection removed" comes before the B.
	for letter in "Bob":
		mark = desktop.mark()
		desktop.typeText(letter)
		desktop.expect(letter, since=mark)

	step(desktop, "Left", "b")
	step(desktop, "Insert+Up", "Bob")
	# The review keys on the numpad, with Num Lock off.
	step(desktop, "KP_Up", "Bob")
	step(desktop, "Insert+KP_Begin", "Name: edit…")
	step(desktop, "Insert+t", "Fixture window")
	# The t went to NVDA alone, not into the field.
	step(desktop, "Insert+Up", "Bob")

	mark = step(desktop, "alt+Tab", "Qt fixture window", "Press me button Does nothing Alt plus P")
	time.sleep(SETTLE)
	assertOnceAndTogether(
		desktop.spokenSince(mark), "Qt fixture window", "Press me button Does nothing Alt plus P"
	)
	step(desktop, "Insert+t", "Qt fixture window")
	step(desktop, "alt+Tab", "Fixture window", "Name: edit…")

	# Reading the window, cut short by Control as soon as it has begun.
	before = len(transcript(nvda))
	mark = desktop.mark()
	desktop.press("Insert+b", gap=0)
	deadline = time.monotonic() + 5
	while not desktop.spokenSince(mark):
		assert time.monotonic() < deadline, f"Insert+b said nothing\n{nvda.describe()}"
		time.sleep(0.005)
	pressed = time.monotonic()
	desktop.press("Control_L", gap=0)
	time.sleep(SETTLE)
	records = transcript(nvda)[before:]
	cancels = [i for i, r in enumerate(records) if r["kind"] == "cancel" and r["t"] >= pressed]
	assert cancels, f"Control did not stop the reading: {records}"
	assert records[cancels[0]]["t"] - pressed <= CANCEL_BOUND, records
	assert not [r for r in records[cancels[0] :] if r["kind"] == "speak"], records

	step(desktop, "Insert+1", "input help on…")
	step(desktop, "t", "t…")
	step(desktop, "Insert+1", "input help off")
	step(desktop, "Insert+Up", "Bob")
	# The NVDA key of the laptop layout's users, which this configuration also makes one.
	step(desktop, "Caps_Lock+t", "Fixture window")

	step(desktop, "Insert+q", "Exit NVDA…")
	desktop.press("Return")
	nvda.waitForExit(EXIT_BOUND)
	assertExitedCleanly(nvda)


def test_dateTimeKeySaysTheTimeAndTwiceTheDate(desktop: DesktopSession):
	nvda = startSession(desktop, GTK)
	desktop.expect(
		"Fixture window", "Press me button Does nothing Alt plus P", since=0, timeout=STARTUP_BOUND
	)
	mark = desktop.mark()
	desktop.press("Insert+F12")
	time.sleep(SETTLE * 2)
	spoken = desktop.spokenSince(mark)
	# The rig's locale is C, whose time is written as American English writes it.
	assert len(spoken) == 1 and re.fullmatch(r"\d{1,2}:\d\d [AP]M", spoken[0]), spoken
	mark = desktop.mark()
	desktop.press("Insert+F12", "Insert+F12", gap=0.1)
	time.sleep(SETTLE * 2)
	spoken = desktop.spokenSince(mark)
	# Midnight may fall between the key and this line.
	years = {time.strftime("%Y"), time.strftime("%Y", time.localtime(time.time() - 60))}
	assert len(spoken) == 2 and re.fullmatch(r"\w+day, \w+ \d{1,2}, \d{4}", spoken[1]), spoken
	assert spoken[1][-4:] in years, spoken
	desktop.close()
	assertExitedCleanly(nvda)


def test_nvdaMenuOpensOnItsFirstItemAndCloses(desktop: DesktopSession):
	nvda = startSession(desktop, GTK)
	desktop.expect(
		"Fixture window", "Press me button Does nothing Alt plus P", since=0, timeout=STARTUP_BOUND
	)
	step(desktop, "Insert+n", "Preferences sub Menu P 1 of 9")
	step(desktop, "Escape", "Fixture window", "Press me button Does nothing Alt plus P")
	desktop.close()
	assertExitedCleanly(nvda)


def test_altTabHeldSpeaksTheSecondWindowsTitle(desktop: DesktopSession):
	nvda = startSession(desktop, WX, QT, GTK)
	desktop.expect(
		"Fixture window", "Press me button Does nothing Alt plus P", since=0, timeout=STARTUP_BOUND
	)
	mark = desktop.mark()
	# Keys go to the rig's own display, as DesktopSession.press sends them.
	subprocess.run(["xdotool", "keydown", "alt"], check=True)
	try:
		desktop.press("Tab")
		desktop.expect("Qt fixture window", since=mark)
		desktop.press("Tab")
		desktop.expect("Qt fixture window", "wx fixture", since=mark)
	finally:
		subprocess.run(["xdotool", "keyup", "alt"], check=True)
	desktop.close()
	assertExitedCleanly(nvda)
