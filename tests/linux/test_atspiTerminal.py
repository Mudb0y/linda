"""MATE Terminal as a person who lives in a shell uses it: output and the prompt spoken as they
appear, typing echoed once it shows and never at a prompt that hides it, the shell's line editing,
the review cursor over the screen with both keyboard layouts, a listing far longer than speech, and a
full-screen program that redraws.

nix develop . -c tests/linux/rig python3 -m pytest -q tests/linux/test_atspiTerminal.py
"""

import time
from collections.abc import Iterator
from pathlib import Path

import desktopSession
import harness
import pytest
from desktopSession import DesktopSession

SESSION_CONFIG = harness.HERE / "fixtures" / "sessionConfig" / "nvda.ini"
TITLE = "Terminal fixture"
#: The transcript synth takes 10 ms a character, so that a long listing is still being read when a key
#: stops it.
TIMED_SPEECH = {"BASEDREADER_TRANSCRIPT_MODE": "timed", "BASEDREADER_TRANSCRIPT_MS_PER_CHAR": "10"}
STARTUP_BOUND = 60.0
#: Seconds to wait for anything more after what a step expects.
SETTLE = 0.6
#: Seconds from a key that cuts speech short to the synth being told to stop.
CANCEL_BOUND = 0.1
#: Lines a listing far longer than the screen may have spoken at most.
FLOOD_BOUND = 40
PASSWORD = "hunter"


@pytest.fixture
def desktop(tmp_path: Path) -> Iterator[DesktopSession]:
	with DesktopSession(tmp_path / "desktop") as session:
		yield session


def startTerminal(desktop: DesktopSession, config: Path) -> harness.Launch:
	desktop.startApp(desktop.fixture("terminal"), title=TITLE)
	desktopSession.activate(TITLE)
	nvda = desktop.startNVDA(config, env=TIMED_SPEECH)
	desktop.expect(TITLE, "terminal $", since=0, timeout=STARTUP_BOUND)
	return nvda


def laptopConfig(directory: Path) -> Path:
	config = directory / "laptop.ini"
	config.write_text(
		SESSION_CONFIG.read_text().replace("[keyboard]\n", "[keyboard]\n\tkeyboardLayout = laptop\n")
	)
	return config


def spoken(desktop: DesktopSession, mark: int) -> list[str]:
	"""What was said since mark, without the empty records that end each spelled character."""
	return [text for text in desktop.spokenSince(mark) if text]


def step(desktop: DesktopSession, chord: str, *expected: str, alone: bool = True) -> list[str]:
	"""Press chord and wait for expected, in order, then for anything more; with alone, nothing else
	may have been said."""
	mark = desktop.mark()
	desktop.press(chord)
	desktop.expect(*expected, since=mark)
	time.sleep(SETTLE)
	said = spoken(desktop, mark)
	if alone:
		assert said == list(expected), said
	return said


def run(desktop: DesktopSession, command: str, *expected: str) -> list[str]:
	"""Type command at the prompt, each letter echoed, then Return; expected must follow, each once."""
	mark = desktop.mark()
	desktop.typeText(command)
	desktop.expect(*[c for c in command if c != " "], since=mark)
	afterTyping = desktop.mark()
	desktop.press("Return")
	desktop.expect(*expected, since=afterTyping)
	time.sleep(SETTLE)
	said = spoken(desktop, afterTyping)
	for text in expected:
		assert said.count(text) == 1, said
	return said


def assertExitedCleanly(desktop: DesktopSession, nvda: harness.Launch) -> None:
	desktop.close()
	assert nvda.process.returncode == 0, nvda.describe()
	nvda.assertClean(logNames=("nvda.log",))


def test_shellOutputTypingAndReview(desktop: DesktopSession):
	nvda = startTerminal(desktop, SESSION_CONFIG)

	said = run(desktop, "echo hello there", "hello there", "$")
	assert said == ["hello there", "$"], said

	# The shell's history and line editing, spoken as in an edit field; the recalled command is not
	# spoken a second time as output, nor the rest of the line the shell redraws after Backspace.
	step(desktop, "Up", "$ echo hello there")
	step(desktop, "Left", "e")
	step(desktop, "ctrl+Left", "there")
	step(desktop, "BackSpace", "space")
	step(desktop, "Insert+Up", "$ echo hellothere")
	desktop.press("End", "ctrl+u")
	step(desktop, "Insert+Up", "$")

	# A prompt that does not show what is typed: nothing of it is spoken or logged.
	run(desktop, "secret", "Password:")
	mark = desktop.mark()
	for letter in PASSWORD:
		desktop.typeText(letter)
	time.sleep(SETTLE)
	assert spoken(desktop, mark) == [], spoken(desktop, mark)
	desktop.press("Return")
	desktop.expect(f"{len(PASSWORD)} characters", "$", since=mark)

	said = run(desktop, "lines 3", "line 1", "line 2", "line 3", "$")
	assert said == ["line 1", "line 2", "line 3", "$"], said

	# The review cursor on the screen, with the desktop layout's numpad.
	step(desktop, "KP_Home", "line 3")
	step(desktop, "KP_Up", "line 3")
	step(desktop, "KP_Right", "3")
	step(desktop, "KP_Begin", "3")
	step(desktop, "KP_End", "space")
	step(desktop, "KP_Next", "3")
	step(desktop, "shift+KP_Home", "$ echo hello there")
	step(desktop, "KP_Prior", "hello there")
	step(desktop, "shift+KP_Prior", "blank")

	# A listing of 50,000 lines: read in part, stopped by one key, and NVDA answers at once after.
	before = len(desktop.nvda.spoken())
	mark = desktop.mark()
	desktop.typeText("lines 50000")
	desktop.press("Return", gap=0)
	deadline = time.monotonic() + 10
	while not any(text.startswith("line ") for text in spoken(desktop, mark)):
		assert time.monotonic() < deadline, f"the listing was not read\n{nvda.describe()}"
		time.sleep(0.01)
	pressed = time.monotonic()
	desktop.press("Control_L", gap=0)
	time.sleep(2)
	records = harness.transcriptRecords().readRecords(nvda.directory / "transcript.jsonl")
	cancels = [r for r in records if r["kind"] == "cancel" and r["t"] >= pressed]
	assert cancels and cancels[0]["t"] - pressed <= CANCEL_BOUND, records[-20:]
	after = [r for r in records if r["kind"] == "speak" and r["t"] > cancels[0]["t"]]
	assert not after, after
	listing = [text for text in desktop.nvda.spokenTexts()[before:] if text.startswith("line ")]
	assert 0 < len(listing) <= FLOOD_BOUND, listing
	step(desktop, "Insert+Up", "$")

	assertExitedCleanly(desktop, nvda)
	for name in ("nvda.log", "transcript.jsonl"):
		assert PASSWORD not in nvda.text(name), f"the password is in {name}"


def test_fullScreenProgramWithLaptopLayout(desktop: DesktopSession, tmp_path: Path):
	nvda = startTerminal(desktop, laptopConfig(tmp_path))
	run(desktop, "menu", "Fruit list", "apple", "banana", "hazelnut", "0 marked", "Filter:")

	# Each move speaks the newly highlighted item alone, never the screen again.
	step(desktop, "Down", "banana")
	step(desktop, "Down", "cherry")
	step(desktop, "Up", "banana")
	# The status line, once and whole; the mark space puts on the item is not something typed.
	step(desktop, "space", "1 marked")

	# The laptop layout's review commands, with Caps Lock as the NVDA key.
	step(desktop, "Caps_Lock+shift+period", "star  banana")
	step(desktop, "Caps_Lock+Up", "apple")
	step(desktop, "Caps_Lock+ctrl+Home", "Fruit list")
	step(desktop, "Caps_Lock+ctrl+Right", "list")
	step(desktop, "Caps_Lock+Right", "i")
	step(desktop, "Caps_Lock+ctrl+End", "Filter:")
	step(desktop, "Caps_Lock+l", "star  banana")

	# The first letter after the input line's prompt, which VTE reports together with the prompt's
	# trailing space.
	step(desktop, "k", "k")

	# Leaving it gives back the shell's screen, of which only what is new is spoken.
	mark = desktop.mark()
	desktop.press("Escape")
	desktop.expect("chose banana", "$", since=mark)
	time.sleep(SETTLE)
	assert spoken(desktop, mark) == ["chose banana", "$"], spoken(desktop, mark)

	assertExitedCleanly(desktop, nvda)
