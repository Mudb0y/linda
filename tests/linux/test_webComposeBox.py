"""A message box built as WhatsApp Web's is, in Firefox and in Chromium: an editable element with the
textbox role, a label and no aria-multiline, whose own text is one embedded object character and whose
paragraph holds what was typed and the caret. What was typed is reviewed by character, word and line,
as NVDA reviews the same box on Windows, and the plain fields beside it read as they did.

LINDA_RIG_WM=marco nix develop . -c tests/linux/rig python3 -m pytest -q tests/linux/test_webComposeBox.py

The tests are steps of one session per browser, each starting where the one before left the page.
"""

import time
from collections.abc import Iterator

import chromiumSession
import desktopSession
import pytest
import webSession
from desktopSession import DesktopSession
from test_browseUsable import (
	EXIT_BOUND,
	SETTLE,
	STARTUP_BOUND,
	TIMED_SPEECH,
	Session,
	step,
	typeEchoed,
)

PAGE = "composeBox.html"
COMPOSE = "Type a message to Ala"
#: What is typed into the message box, word by word.
WORDS = ("see", "you", "soon")
#: What stands in an object's text for each object inside it, which must never be spoken.
EMBEDDED_OBJECT = "\N{OBJECT REPLACEMENT CHARACTER}"
#: How deep the walk for the message box goes from a browser's application object.
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


def composeTexts() -> tuple[str, str]:
	"""The message box's own text and its paragraph's, read straight from the browser."""
	Atspi = desktopSession._atspi()
	desktop = Atspi.get_desktop(0)
	desktop.clear_cache()
	for index in range(desktop.get_child_count()):
		app = desktop.get_child_at_index(index)
		if app is None or app.get_toolkit_name() not in ("Gecko", "Chromium"):
			continue
		entry = _named(app, COMPOSE)
		if entry is None:
			continue
		paragraph = entry.get_child_at_index(0)
		assert paragraph.get_role() == Atspi.Role.PARAGRAPH, paragraph.get_role_name()
		return Atspi.Text.get_text(entry, 0, -1), Atspi.Text.get_text(paragraph, 0, -1)
	raise AssertionError(f"no browser shows {COMPOSE!r}")


def test_theBoxIsBuiltAsWhatsAppsIs(session: Session):
	session.desktop.expect("heading level 1 Chat with Ala", since=0, timeout=STARTUP_BOUND)
	assert composeTexts() == (EMBEDDED_OBJECT, "\n")


def test_returnOnTheBoxSwitchesToFocusMode(session: Session):
	session.desktop.press("Control_L", "ctrl+Home")
	time.sleep(SETTLE)
	step(session, "e", f"{COMPOSE} edit…")
	step(session, "Return", "Focus mode")


def typeWords(session: Session, words: tuple[str, ...]) -> None:
	for index, word in enumerate(words):
		if index:
			step(session, "space", "space")
		typeEchoed(session, word)


def test_whatWasTypedIsReviewed(session: Session):
	typeWords(session, WORDS)
	assert composeTexts() == (EMBEDDED_OBJECT, " ".join(WORDS))
	step(session, "Left", WORDS[-1][-1])
	step(session, "ctrl+Left", WORDS[-1])
	step(session, "Insert+Up", " ".join(WORDS))
	step(session, "Home", WORDS[0][0])
	step(session, "Right", WORDS[0][1])


@pytest.mark.parametrize("field", ["Notes", "Plain box"])
def test_plainFieldsReadAsBefore(session: Session, field: str):
	step(session, "Escape", "Browse mode")
	step(session, "e", f"{field} edit…")
	step(session, "Return", "Focus mode")
	words = ("ab", "cd")
	typeWords(session, words)
	step(session, "Left", "d")
	step(session, "ctrl+Left", "cd")
	step(session, "Insert+Up", "ab cd")


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
