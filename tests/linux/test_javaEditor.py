"""Backspace in a Java program's text, read through the Java ATK wrapper, as in a JetBrains IDE's editor
(issue 25): the wrapper's text-changed event for a deletion carries the text that follows it once it is
made, not the text it removed, so NVDA said the character after the one deleted, or nothing at the end.

LINDA_JAVA and LINDA_JAVA_ATK_WRAPPER are the dev shell's, and LINDA_INTELLIJ_IDEA, for the test of
IntelliJ IDEA itself, as for tests/linux/test_javaMenus.py:

nix develop . -c tests/linux/rig python3 -m pytest -q tests/linux/test_javaEditor.py
"""

import os
import time
from collections.abc import Iterator
from pathlib import Path

import desktopSession
import harness
import pytest
from desktopSession import DesktopSession
from test_javaMenus import JAVA_START_BOUND, STARTUP_BOUND, _waitForQuiet, needsIdea, startIdea

SESSION_CONFIG = harness.HERE / "fixtures" / "sessionConfig" / "nvda.ini"
FIXTURE = harness.HERE / "fixtures" / "apps" / "javaEditor.java"
TITLE = "Java editor"
#: Seconds after the IDE answered NVDA too late before NVDA asks it anything again, with some to spare.
IDEA_UNANSWERED = 3.0

pytestmark = [pytest.mark.needsVariable("LINDA_JAVA"), pytest.mark.needsVariable("LINDA_JAVA_ATK_WRAPPER")]


@pytest.fixture
def desktop(tmp_path: Path) -> Iterator[DesktopSession]:
	with DesktopSession(tmp_path / "desktop") as session:
		yield session


def _start(desktop: DesktopSession) -> None:
	wrapper = Path(os.environ["LINDA_JAVA_ATK_WRAPPER"])
	command = [
		os.environ["LINDA_JAVA"],
		"-cp",
		str(wrapper / "share" / "java" / "java-atk-wrapper.jar"),
		f"-Djava.library.path={wrapper / 'lib'}",
		"-Djavax.accessibility.assistive_technologies=org.GNOME.Accessibility.AtkWrapper",
		str(FIXTURE),
	]
	app = desktop.startApp(command, title=TITLE, timeout=JAVA_START_BOUND)
	assert app.stdout.readline() == "ready\n"
	desktopSession.activate(TITLE)
	desktop.startNVDA(SESSION_CONFIG)
	desktop.expect(TITLE, "Notes…", since=0, timeout=STARTUP_BOUND)
	_waitForQuiet(desktop)


def _type(desktop: DesktopSession, text: str) -> None:
	desktop.typeText(text)
	_waitForQuiet(desktop)


def _step(desktop: DesktopSession, key: str, *expected: str) -> None:
	"""Press key; expected, and nothing more, is what NVDA says. A character spelt ends in an empty speak
	record, which is left out, as in tests/linux/test_deletedCharacter.py."""
	mark = desktop.mark()
	desktop.press(key)
	if expected:
		desktop.expect(*expected, since=mark)
	_waitForQuiet(desktop)
	assert [s for s in desktop.spokenSince(mark) if s.strip()] == list(expected), desktop.spokenSince(mark)


def _closeClean(desktop: DesktopSession) -> None:
	nvda = desktop.nvda
	desktop.close()
	assert nvda.process.returncode == 0, nvda.describe()
	nvda.assertClean(logNames=("nvda.log",))


def test_backspaceAtTheEnd(desktop: DesktopSession):
	"""Each Backspace at the end of the text says the character it deleted, where there is none after it."""
	_start(desktop)
	_type(desktop, "kot")
	_step(desktop, "BackSpace", "t")
	_step(desktop, "BackSpace", "o")
	_closeClean(desktop)


def test_backspaceInsideTheText(desktop: DesktopSession):
	"""Backspace with text after the caret says the character it deleted, not the one after it, and at the
	start of a line the line break it joined."""
	_start(desktop)
	_type(desktop, "kot")
	_step(desktop, "Left", "t")
	_step(desktop, "BackSpace", "o")
	_step(desktop, "End", "blank")
	_type(desktop, "\npies")
	_step(desktop, "Home", "p")
	_step(desktop, "BackSpace", "line feed")
	_closeClean(desktop)


def test_backspaceAfterTypingOverASelection(desktop: DesktopSession):
	"""Backspace after typing over a selection says the character typed, not the one it replaced: the
	wrapper reports a change as the difference in length, so the replacement of one character by one
	reaches NVDA as nothing, and that of three by one as a deletion of two."""
	_start(desktop)
	_type(desktop, "kot")
	_step(desktop, "shift+Left", "t selected")
	_type(desktop, "x")
	_step(desktop, "BackSpace", "x")
	_step(desktop, "BackSpace", "o")
	_type(desktop, "abcd")
	_step(desktop, "shift+Left", "d selected")
	_step(desktop, "shift+Left", "c selected")
	_step(desktop, "shift+Left", "b selected")
	_type(desktop, "y")
	_step(desktop, "BackSpace", "y")
	_step(desktop, "BackSpace", "a")
	_closeClean(desktop)


def _moves(desktop: DesktopSession, key: str, *expected: str) -> None:
	"""Press a key that moves the caret; NVDA says expected, in order, among what it says."""
	mark = desktop.mark()
	desktop.press(key)
	desktop.expect(*expected, since=mark)
	_waitForQuiet(desktop)


@needsIdea
def test_ideaEditorBackspace(desktop: DesktopSession, tmp_path: Path):
	"""Backspace in IntelliJ IDEA's editor says the character it deleted (issue 25), at the end of the
	text, at the end of a line and inside one. The file holds "hello world" and a line break."""
	startIdea(desktop, tmp_path)
	# The IDE answers NVDA's first question about a key in its editor too late, on main as well, and NVDA
	# then says nothing of the key and leaves the IDE alone for two seconds.
	desktop.press("ctrl+End")
	time.sleep(IDEA_UNANSWERED)
	_type(desktop, "kot")
	_step(desktop, "BackSpace", "t")
	_step(desktop, "BackSpace", "o")
	_moves(desktop, "Up", "hello world")
	_moves(desktop, "End", "line feed")
	_step(desktop, "BackSpace", "d")
	_moves(desktop, "Home", "h")
	_moves(desktop, "Right", "e")
	_moves(desktop, "Right", "l")
	_step(desktop, "BackSpace", "e")
	_closeClean(desktop)


@needsIdea
def test_ideaBackspaceAfterTypingOverASelection(desktop: DesktopSession, tmp_path: Path):
	"""Backspace in IntelliJ IDEA's editor after typing over a selection says the character typed."""
	startIdea(desktop, tmp_path)
	desktop.press("ctrl+End")
	time.sleep(IDEA_UNANSWERED)
	_type(desktop, "kot")
	_step(desktop, "shift+Left", "t selected")
	_type(desktop, "x")
	_step(desktop, "BackSpace", "x")
	_step(desktop, "BackSpace", "o")
	_closeClean(desktop)
