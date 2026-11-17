"""A rich text editor in Firefox, of the kind chat sites write prompts and messages in: an editable
element with the textbox role holding a paragraph per paragraph, built as ProseMirror builds claude.ai's
prompt field. Firefox gives the editor an embedded object character for each paragraph and each
paragraph its own text and caret; NVDA reads it as one text, as it does on Windows.

LINDA_RIG_WM=marco nix develop . -c tests/linux/rig python3 -m pytest -q tests/linux/test_firefoxEditor.py

The tests are steps of one session on one NVDA and one Firefox, each starting where the one before left
the editor.
"""

import time
from collections.abc import Iterator

import pytest
import webSession
from desktopSession import DesktopSession
from test_browseUsable import (
	EXIT_BOUND,
	SETTLE,
	STARTUP_BOUND,
	STEP_BOUND,
	TIMED_SPEECH,
	Session,
	step,
	typeEchoed,
)

#: What NVDA says as the editor takes the focus, before its current line.
EDITOR = "Write your prompt edit multi line"
#: The editor's paragraphs as the fixture fills it, and the lines Firefox wraps the long one into.
FIRST = "Alpha beta gamma"
BROKEN = ("Second line", "after a break")
WRAPPED = (
	"one two three four five six seven eight nine",
	"ten eleven twelve thirteen fourteen fifteen",
	"sixteen seventeen eighteen nineteen",
	"twenty",
)
LAST = "Last words here"
#: What stands in an object's text for each object inside it, which must never be spoken.
EMBEDDED_OBJECT = "\N{OBJECT REPLACEMENT CHARACTER}"


@pytest.fixture(scope="module")
def session(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Session]:
	webSession.requireBrowseLibraries()
	directory = tmp_path_factory.mktemp("firefoxEditor")
	with DesktopSession(directory / "desktop") as desktop:
		nvda = desktop.startNVDA(webSession.BROWSE_CONFIG, env=TIMED_SPEECH)
		nvda.waitForLog("NVDA initialized", STARTUP_BOUND)
		firefox = webSession.startFirefox(desktop, "firefoxEditor.html")
		yield Session(desktop, nvda, firefox, directory)


@pytest.fixture(autouse=True)
def recoverAfterAFailure(request: pytest.FixtureRequest, session: Session) -> Iterator[None]:
	failed = request.session.testsfailed
	yield
	if request.session.testsfailed > failed and session.nvda.process.poll() is None:
		session.desktop.press("Escape", "Escape", gap=SETTLE)
		time.sleep(SETTLE)
		session.desktop.press("ctrl+Home", "e", "Return", gap=SETTLE)
		time.sleep(SETTLE)


def test_enterOnTheEditorSwitchesToFocusMode(session: Session):
	# The page is read as it loads, the editor's paragraphs among it.
	session.desktop.expect(f"{EDITOR} {FIRST}", f"{LAST} out of edit", since=0, timeout=STARTUP_BOUND)
	session.desktop.press("Control_L", "ctrl+Home")
	time.sleep(SETTLE)
	step(session, "ctrl+Home", "heading level 1 New chat")
	step(session, "e", f"{EDITOR} {FIRST}")
	# The editor takes the focus and says its name, its role and the line the caret is on.
	step(session, "Return", "Focus mode", f"{EDITOR} {FIRST}")


def test_upAndDownReadEachLine(session: Session):
	step(session, "ctrl+Home", FIRST)
	# Into a paragraph, across the line break inside it, into the empty paragraph, through the lines
	# Firefox wraps a long paragraph into, and back.
	for line in (*BROKEN, "blank", *WRAPPED, LAST):
		step(session, "Down", line)
	for line in (*reversed(WRAPPED), "blank", *reversed(BROKEN), FIRST):
		step(session, "Up", line)


def test_charactersAcrossAParagraphBreak(session: Session):
	step(session, "ctrl+Home", FIRST)
	step(session, "Right", "l")
	step(session, "End", "blank")
	step(session, "Right", "S")
	step(session, "Left", "blank")
	step(session, "Home", "A")
	step(session, "ctrl+End", LAST)
	step(session, "Home", "L")
	step(session, "ctrl+Home", FIRST)


def test_wordsAcrossAParagraphBreak(session: Session):
	step(session, "ctrl+Home", FIRST)
	# Firefox on Linux stops at the end of each word.
	step(session, "ctrl+Right", "Alpha")
	step(session, "ctrl+Right", "beta")
	step(session, "ctrl+Right", "blank")
	step(session, "ctrl+Right", "Second")
	step(session, "ctrl+Left", "Second")
	step(session, "ctrl+Left", "gamma")


def spokenAfter(session: Session, mark: int, seconds: float) -> list[str]:
	time.sleep(seconds)
	return session.desktop.spokenSince(mark)


def test_readCurrentLineAndSayAll(session: Session):
	step(session, "ctrl+End", LAST)
	step(session, "Insert+Up", LAST)
	step(session, "ctrl+Home", FIRST)
	mark = session.desktop.mark()
	session.desktop.press("Insert+Down")
	session.desktop.expect(f"{FIRST}…", since=mark, timeout=STEP_BOUND)
	spoken = " ".join(spokenAfter(session, mark, STEP_BOUND))
	expected = " ".join((FIRST, *BROKEN, *WRAPPED, LAST))
	assert " ".join(spoken.split()) == expected, spoken


def test_selectionIsReported(session: Session):
	step(session, "ctrl+Home", FIRST)
	step(session, "shift+Down", f"{FIRST} selected")
	step(session, "shift+Down", f"{BROKEN[0]}  selected")
	step(session, "shift+Up", f"{BROKEN[0]}  unselected")
	step(session, "shift+Right", "S selected")
	step(session, "shift+Left", "S unselected")
	step(session, "ctrl+Home", FIRST, f"{FIRST} unselected")


def test_typingIntoNewParagraphsAndLines(session: Session):
	step(session, "ctrl+End", LAST)
	session.desktop.press("Return")
	time.sleep(SETTLE)
	typeEchoed(session, "on")
	session.desktop.press("shift+Return")
	time.sleep(SETTLE)
	typeEchoed(session, "it")
	step(session, "Up", "on")
	step(session, "Up", LAST)
	step(session, "Down", "on")
	step(session, "Down", "it")
	# An empty paragraph after it, which ProseMirror fills with its trailing break.
	session.desktop.press("Return")
	time.sleep(SETTLE)
	step(session, "Up", "it")
	step(session, "Down", "blank")


def test_deleteSaysTheCharacterThatComesUnderTheCaret(session: Session):
	step(session, "ctrl+Home", FIRST)
	# As NVDA does in every field: the character now at the caret.
	step(session, "Delete", "l")
	step(session, "Insert+Up", FIRST[1:])


#: Why Backspace says the wrong character, here as in a plain field: the key reaches the application
#: before NVDA's script reads the character before the caret, so the script reads the text as it is
#: after the deletion.
BACKSPACE_COMES_FIRST = (
	"Backspace reaches the application before NVDA's script reads the character before the caret, "
	"in every field and not only in this editor"
)


@pytest.mark.xfail(reason=BACKSPACE_COMES_FIRST, strict=False)
def test_backspaceInAnEmptyParagraphSaysTheCharacterThatWent(session: Session):
	# As in the user's log of claude.ai's prompt field: one character typed into an empty paragraph,
	# which then said "blank".
	step(session, "ctrl+End", "blank")
	typeEchoed(session, "z")
	step(session, "BackSpace", "z")


@pytest.mark.xfail(reason=BACKSPACE_COMES_FIRST, strict=False)
def test_backspaceInTheMiddleOfALineSaysTheCharacterThatWent(session: Session):
	step(session, "ctrl+End", "blank")
	step(session, "Up", "it")
	step(session, "Home", "i")
	step(session, "Right", "t")
	typeEchoed(session, "z")
	step(session, "BackSpace", "z")


def test_noObjectReplacementCharacterWasSpoken(session: Session):
	spoken = session.desktop.spokenSince(0)
	assert not [record for record in spoken if EMBEDDED_OBJECT in record], spoken


def test_tabIntoTheEditorReadsItsLine(session: Session):
	step(session, "Escape", "Browse mode")
	step(session, "b", "Send button")
	step(session, "shift+Tab", f"{EDITOR}…", "Focus mode")


def test_exitIsClean(session: Session):
	step(session, "Escape", "Browse mode")
	step(session, "Insert+q", "Exit Linda…")
	session.desktop.press("Return")
	nvda = session.nvda
	nvda.waitForExit(EXIT_BOUND)
	assert nvda.process.returncode == 0, nvda.describe()
	nvda.assertClean(logNames=("nvda.log",))
