"""YouTube's comment box in Firefox, read while Firefox is still moving the caret: on a page as busy as
YouTube, the box itself can still point at the paragraph an arrow is leaving after that paragraph has
said it no longer holds the caret. The plugin fixtures/globalPlugins/staleCaret.py makes that happen
on every move, which on YouTube happens now and then; each line must still be read where the caret
lands, the blank line between two lines included.

LINDA_RIG_WM=marco nix develop . -c tests/linux/rig python3 -m pytest -q tests/linux/test_webCommentBoxRace.py

The tests are steps of one session, each starting where the one before left the page.
"""

from collections.abc import Iterator

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
from test_webCommentBox import BOX, COMMENT, EMBEDDED_OBJECT, FIRST, PAGE, SECOND, boxText


@pytest.fixture(scope="module")
def session(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Session]:
	webSession.requireBrowseLibraries()
	directory = tmp_path_factory.mktemp("firefox")
	with DesktopSession(directory / "desktop") as desktop:
		nvda = desktop.startNVDA(webSession.BROWSE_CONFIG, env={**TIMED_SPEECH, "LINDA_STALE_CARET": COMMENT})
		nvda.waitForLog("NVDA initialized", STARTUP_BOUND)
		browser = webSession.startFirefox(desktop, PAGE)
		yield Session(desktop, nvda, browser, directory)


def test_twoLinesAndABlankOneAreTyped(session: Session):
	session.desktop.expect("main landmark heading level 1 A video", since=0, timeout=STARTUP_BOUND)
	session.desktop.press("Control_L", "ctrl+Home")
	step(session, "ctrl+Home", "heading level 1 A video")
	step(session, "e", BOX)
	step(session, "Return", "Focus mode", f"{BOX} blank")
	typeEchoed(session, FIRST)
	session.desktop.press("Return", "Return")
	typeEchoed(session, SECOND)
	assert boxText() == EMBEDDED_OBJECT * 3


def test_eachLineIsReadWhereTheCaretLands(session: Session):
	step(session, "ctrl+Home", FIRST)
	step(session, "Down", "blank")
	step(session, "Down", SECOND)
	step(session, "Up", "blank")
	step(session, "Up", FIRST)
	step(session, "Down", "blank")
	step(session, "Down", SECOND)


def test_fromTheEndOfTheLastLine(session: Session):
	# Down on the last line goes to its end, and Up from there keeps that column: into the blank line,
	# then to the end of the first line, as in the user's report.
	step(session, "ctrl+End", SECOND)
	step(session, "Up", "blank")
	step(session, "Up", FIRST)
	step(session, "Down", "blank")
	step(session, "Down", SECOND)


def test_charactersOnEitherSide(session: Session):
	step(session, "ctrl+End", SECOND)
	step(session, "Home", SECOND[0])
	step(session, "Right", SECOND[1])
	step(session, "ctrl+Home", FIRST)
	step(session, "Right", FIRST[1])
	step(session, "Insert+Up", FIRST)


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


#: A box of LONG_LINES lines, more than the 500 embedded objects once asked whether they held the caret.
LONG_PAGE = "longCommentBox.html"
LONG_COMMENT = "Edit a comment..."
LONG_BOX = f"{LONG_COMMENT} section multi line editable"
LONG_LINES = 600


def test_aLongBoxIsReadWhereTheCaretLands(tmp_path_factory: pytest.TempPathFactory):
	# Each key asks the paragraphs nearest the one the caret left, and the first and the last, so that the
	# line the caret lands on is found in a few calls however long the box.
	webSession.requireBrowseLibraries()
	directory = tmp_path_factory.mktemp("longFirefox")
	with DesktopSession(directory / "desktop") as desktop:
		nvda = desktop.startNVDA(
			webSession.BROWSE_CONFIG, env={**TIMED_SPEECH, "LINDA_STALE_CARET": LONG_COMMENT}
		)
		nvda.waitForLog("NVDA initialized", STARTUP_BOUND)
		browser = webSession.startFirefox(desktop, LONG_PAGE)
		session = Session(desktop, nvda, browser, directory)
		desktop.expect("main landmark heading level 1 A long comment", since=0, timeout=STARTUP_BOUND)
		desktop.press("Control_L", "ctrl+Home")
		step(session, "ctrl+Home", "heading level 1 A long comment")
		step(session, "e", f"{LONG_BOX}\N{HORIZONTAL ELLIPSIS}")
		step(session, "Return", "Focus mode")
		step(session, "ctrl+End", f"Line {LONG_LINES}")
		step(session, "Up", f"Line {LONG_LINES - 1}")
		step(session, "Up", f"Line {LONG_LINES - 2}")
		step(session, "Down", f"Line {LONG_LINES - 1}")
		step(session, "ctrl+Home", "Line 1")
		step(session, "Down", "Line 2")
		step(session, "Escape", "Browse mode")
		step(session, "Insert+q", "Exit Linda\N{HORIZONTAL ELLIPSIS}")
		desktop.press("Return")
		nvda.waitForExit(EXIT_BOUND)
		assert nvda.process.returncode == 0, nvda.describe()
		nvda.assertClean(logNames=("nvda.log",))
