"""Browse mode's single letters in native Wayland browsers, Firefox, Chromium and an Electron program, on
the Wayland route: each letter is Linda's and never reaches the compositor, the whole page is in the
buffer, and in focus mode what is typed reaches the page and is echoed, with the marker absent from
Linda's log. Experiment E18 is here too: Chromium and Electron are on the accessibility desktop and read
whole with no process named orca, and how far a character's window coordinates sit from its paragraph's
is printed, for results/E18.md.

Under Mutter and sway, each with and without Xwayland, through tests/linux/wayland-gate SCRATCH, or one
of them:

	LINDA_RIG_COMPOSITOR=mutter nix develop . -c tests/linux/rig python3 -m pytest -q -s tests/linux/test_waylandBrowse.py
"""

import os
import time
from collections.abc import Iterator
from pathlib import Path

import chromiumSession
import desktopSession
import harness
import pytest
import test_browseKeys as browseKeys
import webSession
from keydStandIn import code
from test_waylandKeys import MARKER, STARTUP_BOUND, WaylandLinda, socketDir, waylandLinda  # noqa: F401

pytestmark = pytest.mark.rigSession("wayland")

#: Seconds from Linda's start, or the program's, to the page's buffer being built and browse mode's keys
#: captured.
BUFFER_BOUND = 60.0
#: The Electron fixture's window.
CHAT_TITLE = "Chat fixture"
#: Each program's walk through its page: a chord and the start of what Linda says to it. The letters
#: among them are browse mode's, and the last steps reach the page's end, which only a whole buffer
#: has.
USABLE_WALK = (
	("control+Home", "heading level 1 Welcome"),
	("k", "the news link"),
	("k", "the second page link"),
	("h", "News heading level 2"),
	("h", "Contact heading level 2"),
	("shift+h", "News heading level 2"),
	("control+End", "Write to us."),
	("shift+h", "Contact heading level 2"),
)
CHAT_WALK = (
	("control+Home", "bullet link general"),
	("h", "main landmark general heading level 1"),
	("l", "Messages list with 2 items 1. Ala: Hello everyone"),
	("i", "2. Bartek: Good morning"),
	("b", "New message button"),
	("b", "Mute channel button"),
	("shift+b", "New message button"),
)
#: How each program is read: the walk, the letter that moves to its text field, and what Linda says
#: of the field.
PROGRAMS = {
	"firefox": (USABLE_WALK, "e", "Your name edit"),
	"chromium": (USABLE_WALK, "e", "Your name edit"),
	"electron": (CHAT_WALK, "e", "main landmark form Message general edit"),
}
#: The paragraph whose window coordinates are compared with those of its first character.
PARAGRAPH = "This page tests browse mode."


def sessionName() -> str:
	"""The rig's session as the gate names it."""
	return harness.rigSession() + ("-xwayland" if harness.rigHasXwayland() else "")


def processNamed(name: str) -> list[int]:
	found = []
	for entry in os.scandir("/proc"):
		try:
			if entry.name.isdecimal() and Path(entry.path, "comm").read_text().strip() == name:
				found.append(int(entry.name))
		except OSError:
			continue
	return found


def startProgram(session: WaylandLinda, program: str) -> webSession.FirefoxProcess:
	desktop = session.desktop
	if program == "firefox":
		return webSession.startFirefox(desktop, "usable.html")
	if program == "chromium":
		return chromiumSession.startChromium(desktop, "usable.html")
	return chromiumSession.startElectron(desktop, "electronChat", window=CHAT_TITLE)


@pytest.fixture
def session(tmp_path: Path, socketDir: Path) -> Iterator[WaylandLinda]:  # noqa: F811
	webSession.requireBrowseLibraries()
	with waylandLinda(tmp_path, socketDir, probe=False) as found:
		yield found


def startReading(session: WaylandLinda, program: str, tmp_path: Path) -> webSession.FirefoxProcess:
	"""Linda first, with the accessibility status off as a desktop has it before a screen reader runs, then
	the program, plainly; then its buffer built and browse mode's keys captured."""
	chromiumSession.clearAccessibilityStatus()
	nvda = session.start(
		browseKeys.browseConfig(tmp_path), plugins={"browseKeysProbe": browseKeys.PROBE_PLUGIN}
	)
	browser = startProgram(session, program)
	nvda.waitForLog("Buffer rendered", BUFFER_BOUND)
	browseKeys.waitForCapture(nvda, "browse", timeout=BUFFER_BOUND)
	time.sleep(browseKeys.SETTLE)
	return browser


def waitForTheWholePage(session: WaylandLinda, top: tuple[str, str]) -> None:
	"""Press the walk's first chord until Linda says what it says at the page's top: a buffer rendered
	while the browser was still exposing the page can read it as one line, its heading without its
	level, until the browser's changes come in."""
	chord, said = top
	deadline = time.monotonic() + BUFFER_BOUND / 4
	while True:
		mark = session.desktop.mark()
		session.desktop.press(chord, gap=browseKeys.SETTLE * 2)
		spoken = session.desktop.spokenSince(mark)
		if any(said in text for text in spoken):
			return
		assert time.monotonic() < deadline, f"the page's top was never read as {said!r}: {spoken}"


def lettersKept(session: WaylandLinda, walk: tuple[tuple[str, str], ...]) -> None:
	"""Walk the page, each step's words anywhere in what Linda says to it; no letter of a step reaches
	the session."""
	for chord, said in walk:
		passed = session.passedMark()
		pressed = time.monotonic()
		session.stepHolding(chord, said, timeout=browseKeys.BUFFER_BOUND / 4)
		letter = chord.rsplit("+", 1)[-1]
		if len(letter) == 1:
			assert code(letter) not in session.pressesPassed(passed), (
				chord,
				session.sink.passed()[passed:],
				session.answers(code(letter), pressed),
			)


def windowCoordinates() -> dict[str, tuple[int, int, int, int]] | None:
	"""The window coordinates, x, y, width and height, of the document, the paragraph and the paragraph's first
	character, read from this process over the accessibility bus, or None if the paragraph cannot be
	found."""
	Atspi = desktopSession._atspi()
	desktop = Atspi.get_desktop(0)
	desktop.clear_cache()
	for index in range(desktop.get_child_count()):
		app = desktop.get_child_at_index(index)
		if app is None:
			continue
		for document in browseKeys._matches(Atspi, app, Atspi.Role.DOCUMENT_WEB):
			for paragraph in browseKeys._matches(Atspi, document, Atspi.Role.PARAGRAPH):
				if not Atspi.Text.get_text(paragraph, 0, -1).startswith(PARAGRAPH):
					continue
				found = {
					"document": Atspi.Component.get_extents(document, Atspi.CoordType.WINDOW),
					"paragraph": Atspi.Component.get_extents(paragraph, Atspi.CoordType.WINDOW),
					"character": Atspi.Text.get_character_extents(paragraph, 0, Atspi.CoordType.WINDOW),
				}
				return {name: (box.x, box.y, box.width, box.height) for name, box in found.items()}
	return None


@pytest.mark.parametrize("program", list(PROGRAMS))
def test_browseLettersAreLindasAndTheWholePageIsRead(session: WaylandLinda, tmp_path: Path, program: str):
	walk, toField, field = PROGRAMS[program]
	browser = startReading(session, program, tmp_path)
	nvda = session.nvda
	if program != "firefox":
		# Experiment E18: the browser joined the desktop for Linda's switching the status on alone.
		assert processNamed("orca") == []
	waitForTheWholePage(session, walk[0])
	lettersKept(session, walk)
	if program != "electron":
		print(f"{sessionName()}: {program}'s window coordinates: {windowCoordinates()}")
	# The text field, in focus mode: what is typed reaches the program, and each letter is echoed.
	lettersKept(session, (walk[0], (toField, field)))
	session.step("Return", "Focus mode")
	browseKeys.waitForCapture(nvda, "focus")
	passed = session.passedMark()
	mark = session.desktop.mark()
	session.desktop.typeText(MARKER)
	time.sleep(browseKeys.SETTLE)
	assert session.pressesPassed(passed) == [code(letter) for letter in MARKER]
	assert [text for text in session.desktop.spokenSince(mark) if len(text) == 1] == list(MARKER)
	session.step("Escape", "Browse mode")
	browseKeys.waitForCapture(nvda, "browse")
	assert browser.poll() is None
	session.quit()
	assert "Trapped a key in browse mode" not in nvda.text("nvda.log")
