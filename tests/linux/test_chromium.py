"""The web session of test_browseUsable.py in Chromium: NVDA is running when Chromium opens a page,
and the page is read at once, by line, word and character, by say all and by quick navigation; links
are followed and the way back remembers the place; a form is filled in focus mode; the page changes
under the reader; find; a large page; and NVDA's own exit.

BASEDREADER_RIG_WM=marco nix develop . -c tests/linux/rig python3 -m pytest -q tests/linux/test_chromium.py

Chromium is started plainly, without a switch that forces accessibility on, and the accessibility
status is switched off before NVDA starts, as a desktop has it before any screen reader runs: what
lets Chromium join the accessibility bus is NVDA's switching it on, and NVDA switches it off again
when it exits.
"""

import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import chromiumSession
import harness
import pytest
import webSession
from desktopSession import DesktopSession
from test_browseUsable import (
	EXIT_BOUND,
	FREEZE_MARKS,
	LARGE_LAST_LINE,
	LARGE_TITLE,
	SAY_ALL_BOUND,
	SETTLE,
	STARTUP_BOUND,
	STEP_BOUND,
	TIMED_SPEECH,
	step,
	stopReading,
	typeEchoed,
	waitForRecord,
)

#: The address bar's name, which its focus announcement holds.
ADDRESS_BAR = "Address and search bar"
#: Seconds from Return in the address bar to the large page's first line; Chromium builds its
#: accessibility tree for the page only once the page has loaded.
LARGE_FIRST_LINE_BOUND = 8.0
#: Seconds after Return by which the whole large page is in the buffer.
LARGE_COMPLETE_DELAY = 25.0
#: What NVDA logs when it switched the accessibility status on, and off again.
STATUS_ON = "Accessibility status: switched IsEnabled on"
STATUS_OFF = "Accessibility status: switched IsEnabled off again"


@dataclass
class Session:
	desktop: DesktopSession
	nvda: harness.Launch
	browser: webSession.FirefoxProcess
	directory: Path


@pytest.fixture(scope="module")
def session(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Session]:
	webSession.requireBrowseLibraries()
	directory = tmp_path_factory.mktemp("chromium")
	with DesktopSession(directory / "desktop") as desktop:
		chromiumSession.clearAccessibilityStatus()
		nvda = desktop.startNVDA(webSession.BROWSE_CONFIG, env=TIMED_SPEECH)
		nvda.waitForLog("NVDA initialized", STARTUP_BOUND)
		browser = chromiumSession.startChromium(desktop, "usable.html")
		yield Session(desktop, nvda, browser, directory)


@pytest.fixture(autouse=True)
def recoverAfterAFailure(request: pytest.FixtureRequest, session: Session) -> Iterator[None]:
	failed = request.session.testsfailed
	yield
	if request.session.testsfailed > failed and session.nvda.process.poll() is None:
		session.desktop.press("Escape", "Escape", gap=SETTLE)


def test_nvdaSwitchedAccessibilityOn(session: Session):
	assert STATUS_ON in session.nvda.text("nvda.log")
	assert chromiumSession.accessibilityStatus() is True


def test_pageIsReadWhenItLoads(session: Session):
	session.desktop.expect(
		"heading level 1 Welcome",
		"This page tests browse mode.",
		since=0,
		timeout=STARTUP_BOUND,
	)
	first = waitForRecord(
		session,
		lambda spoken: spoken == "heading level 1 Welcome",
		since=0,
		timeout=0,
		what="the first line",
	)
	assert first["t"] - session.browser.shown <= SAY_ALL_BOUND, (
		f"say all began {first['t'] - session.browser.shown:.2f} s after the window appeared"
	)
	stopReading(session)


def test_linesWordsAndCharacters(session: Session):
	step(session, "ctrl+Home", "heading level 1 Welcome")
	step(session, "Down", "This page tests browse mode.")
	step(session, "Down", "Read link the news…")
	step(session, "Up", "This page tests browse mode.")
	step(session, "Right", "h")
	step(session, "ctrl+Right", "page")


def test_sayAllReadsThePageInOrder(session: Session):
	step(session, "ctrl+Home", "heading level 1 Welcome")
	step(
		session,
		"Insert+Down",
		"heading level 1 Welcome",
		"This page tests browse mode.",
		"Read link the news…",
		"heading level 2 News",
		"Nothing new today.",
		"form Your name…",
		"list…",
		"heading level 2 Contact",
		"Write to us.",
		timeout=STEP_BOUND * 4,
	)


def test_headings(session: Session):
	step(session, "ctrl+Home", "heading level 1 Welcome")
	step(session, "h", "News heading level 2")
	step(session, "h", "Contact heading level 2")
	step(session, "h", "no next heading")
	step(session, "shift+h", "News heading level 2")


def test_samePageLinkMovesTheReader(session: Session):
	step(session, "ctrl+Home", "heading level 1 Welcome")
	step(session, "k", "the news link")
	step(session, "Return", "heading level 2 News")


def test_linkToAnotherPageAndBack(session: Session):
	step(session, "ctrl+Home", "heading level 1 Welcome")
	step(session, "k", "the news…")
	step(session, "k", "the second page link")
	step(
		session,
		"Return",
		"heading level 1 Second page",
		"You followed the link.",
		timeout=STEP_BOUND * 2,
	)
	# The buffer's place is remembered by the page's address, and say all starts there; Chromium does
	# not give the link the focus back, as Firefox does, so it is stopped before it reads on.
	mark = session.desktop.mark()
	session.desktop.press("alt+Left")
	session.desktop.expect(
		"Usable page",
		"visited link the second page…",
		since=mark,
		timeout=STEP_BOUND * 2,
	)
	stopReading(session)
	step(session, "Down", "heading level 2 News")


def test_formInFocusMode(session: Session):
	step(session, "ctrl+Home", "heading level 1 Welcome")
	step(session, "e", "form Your name edit…")
	step(session, "Return", "Focus mode")
	typeEchoed(session, "Bob")
	step(session, "Escape", "Browse mode")
	step(session, "Insert+space", "Focus mode")
	step(session, "Insert+space", "Browse mode")
	step(session, "Tab", "Add item button")


def test_pageChangesUnderTheReader(session: Session):
	session.desktop.press("Return")
	time.sleep(SETTLE)
	# Chromium gives the form the form role, so leaving it is said.
	step(session, "Down", "out of form list…")
	step(session, "Down", "bullet  Added Bob")


def test_find(session: Session):
	step(session, "Insert+ctrl+f", "Type the text you wish to find…")
	session.desktop.typeText("Contact")
	step(session, "Return", "out of list heading level 2 Contact")
	step(session, "Down", "Write to us.")


def test_largePage(session: Session):
	page = session.directory / "large.html"
	subprocess.run(
		[sys.executable, str(harness.HERE / "bench" / "makePage.py"), "large", str(page)],
		check=True,
		capture_output=True,
	)
	mark = session.desktop.mark()
	session.desktop.press("ctrl+l")
	waitForRecord(
		session,
		lambda spoken: ADDRESS_BAR in spoken,
		since=mark,
		timeout=STEP_BOUND,
		what="the address bar",
	)
	time.sleep(SETTLE)
	session.desktop.typeText(page.as_uri())
	time.sleep(SETTLE)
	mark = session.desktop.mark()
	session.desktop.press("Return", gap=0)
	returned = time.monotonic()
	waitForRecord(
		session,
		lambda spoken: spoken.startswith(LARGE_TITLE),
		since=mark,
		timeout=LARGE_FIRST_LINE_BOUND,
		what="the page's name",
	)
	firstLine = waitForRecord(
		session,
		lambda spoken: "Section 0" in spoken,
		since=mark,
		timeout=LARGE_FIRST_LINE_BOUND,
		what="the page's first line",
	)
	assert firstLine["t"] - returned <= LARGE_FIRST_LINE_BOUND, (
		f"the page's first line came {firstLine['t'] - returned:.2f} s after Return"
	)
	time.sleep(max(0.0, returned + LARGE_COMPLETE_DELAY - time.monotonic()))
	session.desktop.press("Control_L")
	mark = session.desktop.mark()
	session.desktop.press("ctrl+End", "Up")
	waitForRecord(
		session,
		lambda spoken: LARGE_LAST_LINE in spoken,
		since=mark,
		timeout=STEP_BOUND,
		what="the page's last line",
	)


def test_exitIsClean(session: Session):
	step(session, "Insert+q", "Exit NVDA…")
	session.desktop.press("Return")
	nvda = session.nvda
	nvda.waitForExit(EXIT_BOUND)
	assert nvda.process.returncode == 0, nvda.describe()
	nvda.assertClean(logNames=("nvda.log",))
	log = nvda.text("nvda.log")
	assert not [mark for mark in FREEZE_MARKS if mark in log], "the watchdog found NVDA frozen"
	assert STATUS_OFF in log
	assert chromiumSession.accessibilityStatus() is False
