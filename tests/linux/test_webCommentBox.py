"""A comment box built as YouTube's is, in Firefox and in Chromium: an editable element with a label and
no role, which both browsers present as a section, and which the browser edits itself. Its text is
its own until Return is pressed in it; from then on each line is in a block of its own and the box's
text holds an embedded object character for it. Every line is reviewed by character and line before
and after, as NVDA reviews the same box on Windows.

LINDA_RIG_WM=marco nix develop . -c tests/linux/rig python3 -m pytest -q tests/linux/test_webCommentBox.py

The tests are steps of one session per browser, each starting where the one before left the page.
"""

from collections.abc import Iterator

import chromiumSession
import desktopSession
import pytest
import webSession
from desktopSession import DesktopSession
from test_browseUsable import (
	EXIT_BOUND,
	STARTUP_BOUND,
	TIMED_SPEECH,
	Session,
	step,
	typeEchoed,
)

PAGE = "commentBox.html"
COMMENT = "Add a comment..."
#: What the box is said to be as it takes the focus.
BOX = f"{COMMENT} section multi line editable"
#: The lines typed into the box, each a word so that the typing echo is the word's letters.
FIRST = "first"
SECOND = "second"
#: What stands in an object's text for each object inside it, which must never be spoken.
EMBEDDED_OBJECT = "\N{OBJECT REPLACEMENT CHARACTER}"
#: How deep the walk for the comment box goes from a browser's application object.
SEARCH_DEPTH = 40


@pytest.fixture(scope="module", params=["firefox", "chromium"])
def session(request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory) -> Iterator[Session]:
	webSession.requireBrowseLibraries()
	directory = tmp_path_factory.mktemp(request.param)
	with DesktopSession(directory / "desktop") as desktop:
		if request.param == "chromium":
			chromiumSession.clearAccessibilityStatus()
		nvda = desktop.startNVDA(webSession.BROWSE_CONFIG, env=TIMED_SPEECH)
		nvda.waitForLog("NVDA initialized", STARTUP_BOUND)
		if request.param == "chromium":
			browser = chromiumSession.startChromium(desktop, PAGE)
		else:
			browser = webSession.startFirefox(desktop, PAGE)
		yield Session(desktop, nvda, browser, directory)


def _named(node, name: str, depth: int = 0):
	if node is None or depth > SEARCH_DEPTH:
		return None
	if node.get_name() == name:
		return node
	for index in range(node.get_child_count()):
		if (found := _named(node.get_child_at_index(index), name, depth + 1)) is not None:
			return found
	return None


def commentBox():
	"""The comment box, read straight from the browser."""
	Atspi = desktopSession._atspi()
	desktop = Atspi.get_desktop(0)
	desktop.clear_cache()
	for index in range(desktop.get_child_count()):
		app = desktop.get_child_at_index(index)
		if app is None or app.get_toolkit_name() not in ("Gecko", "Chromium"):
			continue
		box = _named(app, COMMENT)
		if box is not None:
			return box
	raise AssertionError(f"no browser shows {COMMENT!r}")


def boxText() -> str:
	Atspi = desktopSession._atspi()
	return Atspi.Text.get_text(commentBox(), 0, -1)


def test_theBoxIsAnEditableSection(session: Session):
	session.desktop.expect("main landmark heading level 1 A video", since=0, timeout=STARTUP_BOUND)
	Atspi = desktopSession._atspi()
	box = commentBox()
	states = box.get_state_set()
	assert box.get_role() == Atspi.Role.SECTION, box.get_role_name()
	assert "xml-roles" not in (box.get_attributes() or {})
	assert states.contains(Atspi.StateType.EDITABLE)
	assert states.contains(Atspi.StateType.FOCUSABLE)


def test_returnOnTheBoxSwitchesToFocusMode(session: Session):
	session.desktop.press("Control_L", "ctrl+Home")
	step(session, "ctrl+Home", "heading level 1 A video")
	step(session, "e", BOX)
	step(session, "Return", "Focus mode", f"{BOX} blank")


def test_theFirstLineIsReviewed(session: Session):
	typeEchoed(session, FIRST)
	assert boxText() == FIRST
	step(session, "Left", FIRST[-1])
	step(session, "Insert+Up", FIRST)
	step(session, "Home", FIRST[0])
	step(session, "End", "blank")


def test_linesAfterReturnAreReviewed(session: Session):
	session.desktop.press("Return")
	typeEchoed(session, SECOND)
	# The structure that left NVDA reading nothing in the box: the second line in a block of its own,
	# and the first in the box's text or in a block too.
	assert EMBEDDED_OBJECT in boxText()
	step(session, "Up", FIRST)
	step(session, "Down", SECOND)
	step(session, "Left", SECOND[-1])
	step(session, "Insert+Up", SECOND)
	step(session, "ctrl+Home", FIRST)
	step(session, "Right", FIRST[1])
	step(session, "ctrl+End", SECOND)
	step(session, "Home", SECOND[0])
	step(session, "Up", FIRST)


def test_backspaceSaysTheCharacterThatWent(session: Session):
	step(session, "ctrl+End", SECOND)
	step(session, "BackSpace", SECOND[-1])
	step(session, "Insert+Up", SECOND[:-1])


def test_noObjectReplacementCharacterWasSpoken(session: Session):
	spoken = session.desktop.spokenSince(0)
	assert not [record for record in spoken if EMBEDDED_OBJECT in record], spoken


def test_exitIsClean(session: Session):
	step(session, "Escape", "Browse mode")
	step(session, "Insert+q", "Exit Linda…")
	session.desktop.press("Return")
	nvda = session.nvda
	nvda.waitForExit(EXIT_BOUND)
	assert nvda.process.returncode == 0, nvda.describe()
	nvda.assertClean(logNames=("nvda.log",))
