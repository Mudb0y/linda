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
