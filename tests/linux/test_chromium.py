"""Browse mode in Chromium and in an Electron program.

In Chromium, the web session of test_browseUsable.py: NVDA is running when Chromium opens a page, and
the page is read at once, by line, word and character, by say all and by quick navigation; links are
followed and the way back remembers the place; a form is filled in focus mode; the page changes under
the reader; find; a large page; and NVDA's own exit. In an Electron program shaped like a chat
program, its window read as a web page in browse mode, as NVDA reads such a program's on Windows.

BASEDREADER_RIG_WM=marco nix develop . -c tests/linux/rig python3 -m pytest -q tests/linux/test_chromium.py

Both are started plainly, without a switch that forces accessibility on, and the accessibility status
is switched off before NVDA starts, as a desktop has it before any screen reader runs: what lets them
join the accessibility bus is NVDA's switching it on, and it is off again once NVDA has exited.
"""

import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import chromiumSession
import desktopSession
import harness
import pytest
import webSession
from desktopSession import DesktopSession
from test_browseUsable import (
	EXIT_BOUND,
	FREEZE_MARKS,
	LARGE_FIRST_LINE_BOUND,
	LARGE_LAST_LINE,
	LARGE_NAME_BOUND,
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
#: Seconds after Return by which the whole large page is in the buffer. Chromium exposes a large page
#: as it parses it, so most of it comes as a change after the first render, which the engine then
#: walks for some 15 s.
LARGE_COMPLETE_DELAY = 25.0
#: What NVDA logs when it switched the accessibility status on, and off again.
STATUS_ON = "Accessibility status: switched IsEnabled on"
STATUS_OFF = "Accessibility status: switched IsEnabled off again"
#: The Electron fixture's window, and a window of another program to switch to and from it.
CHAT_TITLE = "Chat fixture"
OTHER_WINDOW = "Fixture window"
#: The window of the Electron fixture's sign-in page.
SIGN_IN_TITLE = "Sign in fixture"


@dataclass
class Session:
	desktop: DesktopSession
	nvda: harness.Launch
	browser: webSession.FirefoxProcess
	directory: Path


@pytest.fixture(scope="class")
def chromium(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Session]:
	webSession.requireBrowseLibraries()
	directory = tmp_path_factory.mktemp("chromium")
	with DesktopSession(directory / "desktop") as desktop:
		chromiumSession.clearAccessibilityStatus()
		nvda = desktop.startNVDA(webSession.BROWSE_CONFIG, env=TIMED_SPEECH)
		nvda.waitForLog("NVDA initialized", STARTUP_BOUND)
		browser = chromiumSession.startChromium(desktop, "usable.html")
		yield Session(desktop, nvda, browser, directory)


@pytest.fixture(scope="class")
def electron(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Session]:
	webSession.requireBrowseLibraries()
	directory = tmp_path_factory.mktemp("electron")
	with DesktopSession(directory / "desktop") as desktop:
		chromiumSession.clearAccessibilityStatus()
		nvda = desktop.startNVDA(webSession.BROWSE_CONFIG, env=TIMED_SPEECH)
		nvda.waitForLog("NVDA initialized", STARTUP_BOUND)
		program = chromiumSession.startElectron(desktop, "electronChat", window=CHAT_TITLE)
		yield Session(desktop, nvda, program, directory)


@pytest.fixture(scope="class")
def signIn(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Session]:
	webSession.requireBrowseLibraries()
	directory = tmp_path_factory.mktemp("signIn")
	with DesktopSession(directory / "desktop") as desktop:
		chromiumSession.clearAccessibilityStatus()
		nvda = desktop.startNVDA(webSession.BROWSE_CONFIG, env=TIMED_SPEECH)
		nvda.waitForLog("NVDA initialized", STARTUP_BOUND)
		program = chromiumSession.startElectron(
			desktop,
			"electronChat",
			"signin.html",
			window=SIGN_IN_TITLE,
		)
		yield Session(desktop, nvda, program, directory)


@pytest.fixture(scope="class")
def composer(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Session]:
	webSession.requireBrowseLibraries()
	directory = tmp_path_factory.mktemp("composer")
	with DesktopSession(directory / "desktop") as desktop:
		chromiumSession.clearAccessibilityStatus()
		nvda = desktop.startNVDA(webSession.BROWSE_CONFIG, env=TIMED_SPEECH)
		nvda.waitForLog("NVDA initialized", STARTUP_BOUND)
		browser = chromiumSession.startChromium(desktop, "chromiumComposer.html")
		yield Session(desktop, nvda, browser, directory)


class TestChromium:
	"""Chromium on the usable page, from NVDA's start to its exit."""

	@pytest.fixture
	def session(self, chromium: Session) -> Session:
		return chromium

	@pytest.fixture(autouse=True)
	def recoverAfterAFailure(self, request: pytest.FixtureRequest, session: Session) -> Iterator[None]:
		failed = request.session.testsfailed
		yield
		if request.session.testsfailed > failed and session.nvda.process.poll() is None:
			session.desktop.press("Escape", "Escape", gap=SETTLE)

	def test_nvdaSwitchedAccessibilityOn(self, session: Session):
		assert STATUS_ON in session.nvda.text("nvda.log")
		assert chromiumSession.accessibilityStatus() is True

	def test_pageIsReadWhenItLoads(self, session: Session):
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

	def test_linesWordsAndCharacters(self, session: Session):
		step(session, "ctrl+Home", "heading level 1 Welcome")
		step(session, "Down", "This page tests browse mode.")
		step(session, "Down", "Read link the news…")
		step(session, "Up", "This page tests browse mode.")
		step(session, "Right", "h")
		step(session, "ctrl+Right", "page")

	def test_sayAllReadsThePageInOrder(self, session: Session):
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

	def test_headings(self, session: Session):
		step(session, "ctrl+Home", "heading level 1 Welcome")
		step(session, "h", "News heading level 2")
		step(session, "h", "Contact heading level 2")
		step(session, "h", "no next heading")
		step(session, "shift+h", "News heading level 2")

	def test_samePageLinkMovesTheReader(self, session: Session):
		step(session, "ctrl+Home", "heading level 1 Welcome")
		step(session, "k", "the news link")
		step(session, "Return", "heading level 2 News")

	def test_linkToAnotherPageAndBack(self, session: Session):
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

	def test_formInFocusMode(self, session: Session):
		step(session, "ctrl+Home", "heading level 1 Welcome")
		step(session, "e", "form Your name edit…")
		step(session, "Return", "Focus mode")
		typeEchoed(session, "Bob")
		step(session, "Escape", "Browse mode")
		step(session, "Insert+space", "Focus mode")
		step(session, "Insert+space", "Browse mode")
		step(session, "Tab", "Add item button")

	def test_pageChangesUnderTheReader(self, session: Session):
		session.desktop.press("Return")
		time.sleep(SETTLE)
		# Chromium gives the form the form role, so leaving it is said.
		step(session, "Down", "out of form list…")
		step(session, "Down", "bullet  Added Bob")

	def test_find(self, session: Session):
		step(session, "Insert+ctrl+f", "Type the text you wish to find…")
		session.desktop.typeText("Contact")
		step(session, "Return", "out of list heading level 2 Contact")
		step(session, "Down", "Write to us.")

	def test_largePage(self, session: Session):
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
		name = waitForRecord(
			session,
			lambda spoken: spoken.startswith(LARGE_TITLE),
			since=mark,
			timeout=STEP_BOUND,
			what="the page's name",
		)
		assert name["t"] - returned <= LARGE_NAME_BOUND, (
			f"the page's name came {name['t'] - returned:.2f} s after Return"
		)
		firstLine = waitForRecord(
			session,
			lambda spoken: "Section 0" in spoken,
			since=mark,
			timeout=STEP_BOUND,
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

	def test_exitIsClean(self, session: Session):
		step(session, "Insert+q", "Exit NVDA…")
		session.desktop.press("Return")
		nvda = session.nvda
		nvda.waitForExit(EXIT_BOUND)
		assert nvda.process.returncode == 0, nvda.describe()
		nvda.assertClean(logNames=("nvda.log",))
		log = nvda.text("nvda.log")
		assert not [mark for mark in FREEZE_MARKS if mark in log], "the watchdog found NVDA frozen"
		if chromiumSession.launcherKeepsSettings():
			assert STATUS_OFF in log
		else:
			assert STATUS_OFF not in log, "NVDA wrote the desktop's setting where the launcher reports it"
		chromiumSession.waitForAccessibilityStatus(False)


def toTop(session: Session) -> None:
	"""Control+Home on the Electron fixture, wherever the reader was."""
	mark = session.desktop.mark()
	session.desktop.press("ctrl+Home")
	waitForRecord(
		session,
		lambda spoken: spoken.endswith("bullet link general"),
		since=mark,
		timeout=STEP_BOUND,
		what="the top of the page",
	)
	time.sleep(SETTLE)


class TestElectron:
	"""An Electron program shaped like a chat program, from NVDA's start to its exit: its window is a
	web page in browse mode, as such a program's is in NVDA on Windows."""

	@pytest.fixture
	def session(self, electron: Session) -> Session:
		return electron

	@pytest.fixture(autouse=True)
	def recoverAfterAFailure(self, request: pytest.FixtureRequest, session: Session) -> Iterator[None]:
		failed = request.session.testsfailed
		yield
		if request.session.testsfailed > failed and session.nvda.process.poll() is None:
			session.desktop.press("Escape", "Escape", gap=SETTLE)

	def test_windowIsReadWhenItOpens(self, session: Session):
		# The window, then the page, read from its top.
		session.desktop.expect(
			"Chat fixture",
			"Chat fixture",
			"Channels navigation landmark list with 2 items bullet link general",
			since=0,
			timeout=STARTUP_BOUND,
		)
		stopReading(session)

	def test_linesAndQuickNavigation(self, session: Session):
		step(session, "ctrl+Home", "bullet link general")
		step(session, "Down", "bullet link random")
		step(session, "Down", "out of list main landmark heading level 1 general")
		step(session, "Down", "Messages list with 2 items 1. Ala: Hello everyone")
		step(
			session,
			"ctrl+Home",
			"out of list Channels navigation landmark list with 2 items bullet link general",
		)
		step(session, "h", "main landmark general heading level 1")
		step(session, "l", "Messages list with 2 items 1. Ala: Hello everyone")
		step(session, "i", "2. Bartek: Good morning")
		step(session, "i", "no next list item")
		step(session, "b", "New message button")
		step(session, "b", "Mute channel button")
		step(session, "shift+b", "New message button")

	def test_aButtonAddsAMessage(self, session: Session):
		toTop(session)
		step(session, "b", "main landmark New message button")
		session.desktop.press("Return")
		time.sleep(SETTLE)
		step(session, "shift+i", "Messages list with 3 items 3. Ala: Message 1")

	def test_sendingAMessageInFocusMode(self, session: Session):
		toTop(session)
		step(session, "e", "main landmark form Message general edit")
		step(session, "Return", "Focus mode")
		typeEchoed(session, "hi")
		session.desktop.press("Return")
		time.sleep(SETTLE)
		step(session, "Escape", "Browse mode")
		step(session, "shift+i", "Messages list with 4 items 4. You: hi")

	def test_tabMovesBetweenControls(self, session: Session):
		toTop(session)
		step(session, "l", "main landmark Messages list…")
		step(session, "Tab", "Message general edit blank", "Focus mode")
		step(session, "Tab", "New message button", "Browse mode")
		step(session, "shift+Tab", "Message general edit blank", "Focus mode")

	def test_switchingBackToTheWindow(self, session: Session):
		session.desktop.startApp(session.desktop.fixture("gtkFixture"), title=OTHER_WINDOW)
		desktopSession.activate(OTHER_WINDOW)
		time.sleep(SETTLE)
		mark = session.desktop.mark()
		desktopSession.activate(CHAT_TITLE)
		# The window, the page, and the field that had the focus, still in focus mode.
		session.desktop.expect(
			"Chat fixture",
			"Chat fixture document",
			"Message general edit blank",
			since=mark,
			timeout=STEP_BOUND,
		)
		time.sleep(SETTLE)
		step(session, "Escape", "Browse mode")

	def test_exitIsClean(self, session: Session):
		step(session, "Insert+q", "Exit NVDA…")
		session.desktop.press("Return")
		nvda = session.nvda
		nvda.waitForExit(EXIT_BOUND)
		assert nvda.process.returncode == 0, nvda.describe()
		nvda.assertClean(logNames=("nvda.log",))
		chromiumSession.waitForAccessibilityStatus(False)


class TestElectronSignIn:
	"""A sign-in screen that a single-page program builds and focuses a while after its page has
	loaded, as Discord's is. The field's focus event comes before the buffer has the field, and the
	buffer takes the focus as its own once it has."""

	@pytest.fixture
	def session(self, signIn: Session) -> Session:
		return signIn

	@pytest.fixture(autouse=True)
	def recoverAfterAFailure(self, request: pytest.FixtureRequest, session: Session) -> Iterator[None]:
		failed = request.session.testsfailed
		yield
		if request.session.testsfailed > failed and session.nvda.process.poll() is None:
			session.desktop.press("Escape", "Escape", gap=SETTLE)

	def test_theFocusedFieldIsInFocusMode(self, session: Session):
		session.desktop.expect("Email edit…", "Focus mode", since=0, timeout=STARTUP_BOUND)
		time.sleep(SETTLE)

	def test_browseModeReadsTheScreen(self, session: Session):
		step(session, "Escape", "Browse mode")
		step(session, "ctrl+Home", "main landmark heading level 1 Welcome back")
		step(session, "e", "form Email edit…")
		step(session, "e", "Password edit…")
		step(session, "b", "Log in button")
		step(session, "shift+e", "Password edit…")
		step(session, "Return", "Focus mode")
		step(session, "Escape", "Browse mode")

	def test_exitIsClean(self, session: Session):
		step(session, "Insert+q", "Exit NVDA…")
		session.desktop.press("Return")
		nvda = session.nvda
		nvda.waitForExit(EXIT_BOUND)
		assert nvda.process.returncode == 0, nvda.describe()
		nvda.assertClean(logNames=("nvda.log",))


class TestChromiumComposer:
	"""A message written in a rich text editor, an editable element rather than a field, as chat
	programs have, which the channel focuses as it opens; Enter sends it."""

	@pytest.fixture
	def session(self, composer: Session) -> Session:
		return composer

	@pytest.fixture(autouse=True)
	def recoverAfterAFailure(self, request: pytest.FixtureRequest, session: Session) -> Iterator[None]:
		failed = request.session.testsfailed
		yield
		if request.session.testsfailed > failed and session.nvda.process.poll() is None:
			session.desktop.press("Escape", "Escape", gap=SETTLE)

	def test_theEditorIsInFocusModeWhenTheChannelOpens(self, session: Session):
		session.desktop.expect(
			"Message  number general edit multi line…", "Focus mode", since=0, timeout=STARTUP_BOUND
		)
		time.sleep(SETTLE)

	def test_typingIsEchoedAndReadBack(self, session: Session):
		typeEchoed(session, "hello")
		step(session, "Left", "o")
		step(session, "Insert+Up", "hello")

	def test_enterSendsTheMessage(self, session: Session):
		session.desktop.press("End", "Return")
		time.sleep(SETTLE)
		step(session, "Escape", "Browse mode")
		step(session, "shift+i", "Messages list with 2 items 2. You: hello")
		step(session, "e", "form Message  number general edit multi line…")
		step(session, "Return", "Focus mode")

	def test_exitIsClean(self, session: Session):
		step(session, "Insert+q", "Exit NVDA…")
		session.desktop.press("Return")
		nvda = session.nvda
		nvda.waitForExit(EXIT_BOUND)
		assert nvda.process.returncode == 0, nvda.describe()
		nvda.assertClean(logNames=("nvda.log",))
