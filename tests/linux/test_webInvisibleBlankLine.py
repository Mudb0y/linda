"""An empty line of a message box built as Discord's is by Slate, in Firefox and in Chromium: Slate puts
a zero-width no-break space, U+FEFF, in an empty line so that it does not collapse, and the line then
holds that character and a line break. It shows nothing, so the line is read as blank, as a line of
nothing but spaces is.

LINDA_RIG_WM=marco nix develop . -c tests/linux/rig python3 -m pytest -q tests/linux/test_webInvisibleBlankLine.py

The tests are steps of one session per browser, each starting where the one before left the page.
"""

from collections.abc import Iterator

import chromiumSession
import pytest
import webSession
from desktopSession import DesktopSession
from test_browseUsable import (
	EXIT_BOUND,
	STARTUP_BOUND,
	TIMED_SPEECH,
	Session,
	step,
)

PAGE = "slateBox.html"
BOX = "Message to Ala"
FIRST = "Testing"
SECOND = "More test"
#: What Slate puts in an empty line, which must never reach the synthesizer as the line.
ZERO_WIDTH = "\N{ZERO WIDTH NO-BREAK SPACE}"


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


def test_theBoxIsFocused(session: Session):
	session.desktop.expect("main landmark heading level 1 Chat with Ala", since=0, timeout=STARTUP_BOUND)
	session.desktop.press("Control_L", "ctrl+Home")
	step(session, "e", f"{BOX} edit…")
	step(session, "Return", "Focus mode")


def test_theEmptyLineIsBlank(session: Session):
	step(session, "ctrl+Home", FIRST)
	step(session, "Down", "blank")
	step(session, "Down", SECOND)
	step(session, "Up", "blank")
	step(session, "Up", FIRST)


def test_theZeroWidthSpaceWasNeverSpoken(session: Session):
	spoken = session.desktop.spokenSince(0)
	assert not [record for record in spoken if ZERO_WIDTH in record], spoken


def test_inBrowseModeToo(session: Session, request: pytest.FixtureRequest):
	if request.node.callspec.params["session"] == "chromium":
		# Chromium gives the empty line's two runs, U+FEFF and the line break, an object each, which the
		# buffer renders as fields of their own: "edit", the character, "edit".
		request.applymarker(pytest.mark.xfail(strict=True, reason="Chromium's buffer says edit edit"))
	step(session, "Escape", "Browse mode")
	step(session, "ctrl+Home", "out of edit heading level 1 Chat with Ala")
	step(session, "Down", f"{BOX} edit…")
	step(session, "Down", "blank")
	step(session, "Down", SECOND)


def test_exitIsClean(session: Session):
	step(session, "Insert+q", "Exit Linda…")
	session.desktop.press("Return")
	nvda = session.nvda
	nvda.waitForExit(EXIT_BOUND)
	assert nvda.process.returncode == 0, nvda.describe()
	nvda.assertClean(logNames=("nvda.log",))
