"""Words joined by an apostrophe in Firefox's fields: a field of one line, a text area, and an editable
element of the kind YouTube's comment box is, each holding "This isn't a test".

Firefox's caret stops inside such a word, at "isn" and at "t" of "isn't", and Firefox makes each part
a word of its own; on Linux Control+Right stops at the end of each word it moves over. NVDA on Windows
reads each part on the side of the apostrophe the caret is on, and so must Linda: "isn't" read at both
stops gave no way to tell which part the caret was in.

LINDA_RIG_WM=marco nix develop . -c tests/linux/rig python3 -m pytest -q tests/linux/test_firefoxContractions.py
"""

import time
from collections.abc import Iterator

import pytest
import webSession
from desktopSession import DesktopSession
from test_browseUsable import EXIT_BOUND, SETTLE, STARTUP_BOUND, TIMED_SPEECH, Session, step

PAGE = "contractions.html"
#: What each field says as it takes the focus, before its text.
FIELDS = ("Entry edit has auto complete", "Area edit multi line", "Box section multi line editable")
TEXT = "This isn't a test"
#: What each Control+Right says from the start of the text, and each Control+Left from its end. The
#: apostrophe goes with "isn", as in Firefox's word and NVDA's on Windows; at the punctuation level
#: "some" it is left to the synthesizer, which does not say it.
FORWARD = ("This", "isn'", "t", "a", "test")
BACK = ("test", "a", "t", "isn'", "This")


@pytest.fixture(scope="module")
def session(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Session]:
	webSession.requireBrowseLibraries()
	directory = tmp_path_factory.mktemp("firefoxContractions")
	with DesktopSession(directory / "desktop") as desktop:
		nvda = desktop.startNVDA(webSession.BROWSE_CONFIG, env=TIMED_SPEECH)
		nvda.waitForLog("NVDA initialized", STARTUP_BOUND)
		firefox = webSession.startFirefox(desktop, PAGE)
		session = Session(desktop, nvda, firefox, directory)
		desktop.expect("main landmark heading level 1 Contractions", since=0, timeout=STARTUP_BOUND)
		desktop.press("Control_L", "ctrl+Home")
		time.sleep(SETTLE)
		yield session


@pytest.fixture(autouse=True)
def recoverAfterAFailure(request: pytest.FixtureRequest, session: Session) -> Iterator[None]:
	failed = request.session.testsfailed
	yield
	if request.session.testsfailed > failed and session.nvda.process.poll() is None:
		# Back to browse mode, on the field the failure left, for the next field's "e".
		session.desktop.press("Escape")
		time.sleep(SETTLE)


@pytest.mark.parametrize("field", FIELDS)
def test_eachPartOfAWordJoinedByAnApostrophe(session: Session, field: str):
	step(session, "e", f"{field} {TEXT}")
	step(session, "Return", "Focus mode")
	step(session, "ctrl+Home", TEXT)
	for word in FORWARD:
		step(session, "ctrl+Right", word)
	for word in BACK:
		step(session, "ctrl+Left", word)
	step(session, "Escape", "Browse mode")


def test_exitIsClean(session: Session):
	step(session, "Insert+q", "Exit Linda\N{HORIZONTAL ELLIPSIS}")
	session.desktop.press("Return")
	session.nvda.waitForExit(EXIT_BOUND)
	assert session.nvda.process.returncode == 0, session.nvda.describe()
	session.nvda.assertClean(logNames=("nvda.log",))
