"""A session on the web as a person using Linda as their only screen reader has one: NVDA is
running when Firefox opens a page, and the page is read at once, by line, word and character, by
say all and by quick navigation; links are followed and the way back remembers the place; a form is
filled in focus mode; the page changes under the reader; find; a page too large to read in one go;
and NVDA's own exit, with every key pressed on the rig's display.

LINDA_RIG_WM=marco nix develop . -c tests/linux/rig python3 -m pytest -q tests/linux/test_browseUsable.py

The tests are the session's steps, in order, on one NVDA and one Firefox; each starts from where the
one before left the page. Firefox starts after NVDA, as on a desktop where the screen reader is
already running, so its accessibility bridge is active before the page loads. Browse mode's two
libraries are the ones the dev shell names, or the caller's own builds named by the same variables.
"""

import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import harness
import pytest
import webSession
from desktopSession import DesktopSession

#: The transcript synth takes 10 ms a character, so that reading takes long enough to be cut short.
TIMED_SPEECH = {"LINDA_TRANSCRIPT_MODE": "timed", "LINDA_TRANSCRIPT_MS_PER_CHAR": "10"}
#: Seconds from starting NVDA to its being ready; a start under the rig takes about three.
STARTUP_BOUND = 60.0
#: Seconds from the page's window appearing to say all reading it.
SAY_ALL_BOUND = 3.0
#: Seconds from a key that cuts speech short to the synth being told to stop.
CANCEL_BOUND = 0.1
#: Seconds a step's speech has to come; a key answered on the rig takes well under one.
STEP_BOUND = 5.0
#: Seconds to wait after a step for anything more it starts.
SETTLE = 0.5
#: Seconds NVDA has to exit once its exit dialog is answered.
EXIT_BOUND = 10.0
#: The end of the address bar's name, which its focus announcement holds. Firefox names the search
#: engine before it, "Search with Google or enter address", only once it has loaded its search engines.
ADDRESS_BAR = "or enter address"
#: What the large page's title is, as tests/linux/bench/makePage.py writes it.
LARGE_TITLE = "Linda bench large page"
#: How every title of Firefox's window ends.
FIREFOX_TITLE_SUFFIX = "Mozilla Firefox"
#: The large page's last line.
LARGE_LAST_LINE = "End of page."
#: Seconds from Return in the address bar to the large page's name, and to its first line.
LARGE_NAME_BOUND = 3.0
LARGE_FIRST_LINE_BOUND = 4.0
#: When NVDA+t is pressed after Return, and how soon it must be answered while the page loads.
TITLE_PRESS_DELAY = 1.0
TITLE_BOUND = 0.5
#: Seconds after Return by which the whole large page is in the buffer.
LARGE_COMPLETE_DELAY = 20.0
#: What the watchdog logs when NVDA's main thread stops answering.
FREEZE_MARKS = ("Core frozen in stack", "Starting freeze recovery")


@dataclass
class Session:
	desktop: DesktopSession
	nvda: harness.Launch
	firefox: webSession.FirefoxProcess
	directory: Path


@pytest.fixture(scope="module")
def session(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Session]:
	webSession.requireBrowseLibraries()
	directory = tmp_path_factory.mktemp("browseUsable")
	with DesktopSession(directory / "desktop") as desktop:
		nvda = desktop.startNVDA(webSession.BROWSE_CONFIG, env=TIMED_SPEECH)
		nvda.waitForLog("NVDA initialized", STARTUP_BOUND)
		firefox = webSession.startFirefox(desktop, "usable.html")
		yield Session(desktop, nvda, firefox, directory)


@pytest.fixture(autouse=True)
def recoverAfterAFailure(request: pytest.FixtureRequest, session: Session) -> Iterator[None]:
	"""Close what a failed step left open, a dialog or focus mode, so that the next step starts on
	the page and says whether its own part works."""
	failed = request.session.testsfailed
	yield
	if request.session.testsfailed > failed and session.nvda.process.poll() is None:
		session.desktop.press("Escape", "Escape", gap=SETTLE)


def transcript(nvda: harness.Launch) -> list[dict]:
	"""Every record the transcript synth made: speak, cancel and pause."""
	path = nvda.directory / "transcript.jsonl"
	return harness.transcriptRecords().readRecords(path) if path.exists() else []


def speakRecords(nvda: harness.Launch, since: int) -> list[dict]:
	return nvda.spoken()[since:]


def text(record: dict) -> str:
	return harness.transcriptRecords().spokenText(record)


def waitForRecord(
	session: Session,
	matches: Callable[[str], bool],
	*,
	since: int,
	timeout: float,
	what: str,
	other: dict | None = None,
) -> dict:
	"""The first speak record after the mark since whose text matches, other than other."""
	nvda = session.nvda
	deadline = time.monotonic() + timeout
	while True:
		exited = nvda.process.poll() is not None
		records = speakRecords(nvda, since)
		for record in records:
			if record != other and matches(text(record)):
				return record
		if exited or time.monotonic() >= deadline:
			raise AssertionError(
				f"NVDA did not speak {what} within {timeout} s; it spoke {[text(r) for r in records]}\n"
				f"{nvda.describe()}",
			)
		time.sleep(0.02)


def step(session: Session, chord: str, *expected: str, timeout: float = STEP_BOUND) -> int:
	"""Press chord and wait for expected, in order; return the mark taken before the key."""
	desktop = session.desktop
	mark = desktop.mark()
	desktop.press(chord)
	desktop.expect(*expected, since=mark, timeout=timeout)
	time.sleep(SETTLE)
	return mark


def typeEchoed(session: Session, letters: str) -> None:
	"""Type each letter once the one before has been spoken."""
	for letter in letters:
		mark = session.desktop.mark()
		session.desktop.typeText(letter)
		session.desktop.expect(letter, since=mark)


def stopReading(session: Session) -> None:
	"""Press Control while NVDA is reading, and check that it stopped at once and said nothing more."""
	before = len(transcript(session.nvda))
	pressed = time.monotonic()
	session.desktop.press("Control_L", gap=0)
	time.sleep(SETTLE)
	records = transcript(session.nvda)[before:]
	cancels = [i for i, r in enumerate(records) if r["kind"] == "cancel" and r["t"] >= pressed]
	assert cancels, f"Control did not stop the reading: {records}"
	assert records[cancels[0]]["t"] - pressed <= CANCEL_BOUND, records
	assert not [r for r in records[cancels[0] :] if r["kind"] == "speak"], records


def test_pageIsReadWhenItLoads(session: Session):
	# Firefox's window comes first, named after the page or, before the page has set its title, after
	# Firefox alone; then the document.
	session.desktop.expect(
		"Usable page",
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
	assert first["t"] - session.firefox.shown <= SAY_ALL_BOUND, (
		f"say all began {first['t'] - session.firefox.shown:.2f} s after the window appeared"
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
		"clickable Your name…",
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
	# Firefox moves its caret to the target, and the buffer follows it.
	step(session, "Return", "heading level 2 News")


def test_linkToAnotherPageAndBack(session: Session):
	step(session, "ctrl+Home", "heading level 1 Welcome")
	# Firefox marks the link to a place on the page visited once the step before followed it.
	step(session, "k", "the news visited link")
	step(session, "k", "the second page link")
	step(
		session,
		"Return",
		"Second page",
		"heading level 1 Second page",
		"You followed the link.",
		timeout=STEP_BOUND * 2,
	)
	# Firefox gives the link back its focus, and the buffer's place is remembered by the page's
	# address; each puts the reader on the link.
	step(session, "alt+Left", "Usable page…", "the second page link…", timeout=STEP_BOUND * 2)
	stopReading(session)
	step(session, "Down", "heading level 2 News")


def test_formInFocusMode(session: Session):
	step(session, "ctrl+Home", "heading level 1 Welcome")
	step(session, "e", "Your name edit…")
	step(session, "Return", "Focus mode")
	typeEchoed(session, "Bob")
	step(session, "Escape", "Browse mode")
	step(session, "Insert+space", "Focus mode")
	step(session, "Insert+space", "Browse mode")
	step(session, "Tab", "Add item button")


def test_pageChangesUnderTheReader(session: Session):
	session.desktop.press("Return")
	time.sleep(SETTLE)
	step(session, "Down", "list…")
	# NVDA's symbols put a space either side of the bullet's name.
	step(session, "Down", "bullet  Added Bob")


def test_find(session: Session):
	step(session, "Insert+ctrl+f", "Type the text you wish to find…")
	session.desktop.typeText("Contact")
	# The caret leaves the list the step before left it in.
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

	time.sleep(max(0.0, returned + TITLE_PRESS_DELAY - time.monotonic()))
	titleMark = session.desktop.mark()
	pressed = time.monotonic()
	session.desktop.press("Insert+t", gap=0)
	# Whatever title the window has by then: Firefox may not yet have read the page's own.
	answer = waitForRecord(
		session,
		lambda spoken: spoken.endswith(FIREFOX_TITLE_SUFFIX),
		since=titleMark,
		timeout=STEP_BOUND,
		what="the title",
	)
	assert answer["t"] - pressed <= TITLE_BOUND, (
		f"NVDA+t was answered after {answer['t'] - pressed:.2f} s while the page loaded"
	)

	# The page's name as the document took focus, which is not NVDA+t's answer.
	name = waitForRecord(
		session,
		lambda spoken: spoken.startswith(LARGE_TITLE),
		since=mark,
		timeout=STEP_BOUND,
		what="the page's name",
		other=answer,
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


def test_exitIsClean(session: Session):
	step(session, "Insert+q", "Exit Linda…")
	session.desktop.press("Return")
	nvda = session.nvda
	nvda.waitForExit(EXIT_BOUND)
	assert nvda.process.returncode == 0, nvda.describe()
	nvda.assertClean(logNames=("nvda.log",))
	log = nvda.text("nvda.log")
	assert not [mark for mark in FREEZE_MARKS if mark in log], "the watchdog found NVDA frozen"
