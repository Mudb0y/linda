"""Moving by word in MATE Terminal reads the word the shell's caret lands on, each part of a path
included (GitHub issue 8), and moving by word in a GTK entry reads as it did before.

nix develop . -c tests/linux/rig python3 -m pytest -q tests/linux/test_terminalWords.py
"""

import time
from collections.abc import Iterator
from pathlib import Path

import desktopSession
import harness
import pytest
from desktopSession import DesktopSession

SESSION_CONFIG = harness.HERE / "fixtures" / "sessionConfig" / "nvda.ini"
STARTUP_BOUND = 60.0
#: Seconds to wait for anything more after what a step expects.
SETTLE = 0.6
#: Each line typed, then what Control+Right says three times from its start, then Control+Left three
#: times from its end. Bash stops at the end of a word, on the separator after it. The session's
#: punctuation level names a slash and a dot but leaves a hyphen to the synthesizer.
TERMINAL_WORDS = (
	(
		"/usr/bin/bash",
		["usr slash", "bin slash", "bash"],
		["bash", "bin slash", "usr slash"],
	),
	(
		"foo.bar.baz",
		["foo dot bar dot baz"] * 3,
		["foo dot bar dot baz"] * 3,
	),
	(
		"foo-bar-baz",
		["foo-", "bar-", "baz"],
		["baz", "bar-", "foo-"],
	),
	("one two three", ["one", "two", "three"], ["three", "two", "one"]),
)
#: The same in a GTK entry, as Linda read it before the terminal's words changed: GTK also stops at
#: the end of a word, and the end of the entry is blank.
ENTRY_WORDS = (
	("/usr/bin/bash", ["slash", "slash", "blank"], ["bash", "bin", "usr"]),
	(
		"foo.bar.baz",
		["foo dot bar dot baz", "foo dot bar dot baz", "blank"],
		["foo dot bar dot baz"] * 3,
	),
	("foo-bar-baz", ["dash", "dash", "blank"], ["baz", "bar", "foo"]),
	("one two three", ["one", "two", "blank"], ["three", "two", "one"]),
)


@pytest.fixture
def desktop(tmp_path: Path) -> Iterator[DesktopSession]:
	with DesktopSession(tmp_path / "desktop") as session:
		yield session


def step(desktop: DesktopSession, chord: str, expected: str) -> None:
	"""Press chord and wait for expected, then for anything more; nothing else may have been said."""
	mark = desktop.mark()
	desktop.press(chord)
	desktop.expect(expected, since=mark)
	time.sleep(SETTLE)
	said = [text for text in desktop.spokenSince(mark) if text]
	assert said == [expected], f"{chord}: {said}"


def moveByWord(desktop: DesktopSession, forward: list[str], back: list[str]) -> None:
	for expected in forward:
		step(desktop, "ctrl+Right", expected)
	desktop.press("End")
	time.sleep(SETTLE)
	for expected in back:
		step(desktop, "ctrl+Left", expected)


def test_terminalWordsAtTheShellsCaret(desktop: DesktopSession):
	title = "Terminal fixture"
	desktop.startApp(desktop.fixture("terminal"), title=title)
	desktopSession.activate(title)
	nvda = desktop.startNVDA(SESSION_CONFIG)
	desktop.expect(title, "terminal $", since=0, timeout=STARTUP_BOUND)
	for line, forward, back in TERMINAL_WORDS:
		mark = desktop.mark()
		desktop.typeText(line)
		desktop.expect(line[-1], since=mark)
		desktop.press("Home")
		time.sleep(SETTLE)
		moveByWord(desktop, forward, back)
		desktop.press("End", "ctrl+u")
		time.sleep(SETTLE)
	desktop.close()
	assert nvda.process.returncode == 0, nvda.describe()


def test_entryWordsUnchanged(desktop: DesktopSession):
	title = "TextFixture"
	desktop.startApp(desktop.fixture("textapp", "gtk3"), title=title)
	desktopSession.activate(title)
	nvda = desktop.startNVDA(SESSION_CONFIG)
	desktop.expect("entry edit…", since=0, timeout=STARTUP_BOUND)
	for line, forward, back in ENTRY_WORDS:
		desktop.press("ctrl+a")
		mark = desktop.mark()
		desktop.typeText(line)
		desktop.expect(line[-1], since=mark)
		desktop.press("Home")
		time.sleep(SETTLE)
		moveByWord(desktop, forward, back)
	desktop.close()
	assert nvda.process.returncode == 0, nvda.describe()
