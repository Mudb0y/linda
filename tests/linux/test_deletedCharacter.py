"""What NVDA says for Backspace and Delete in plain text fields, and for the caret keys in plain
multi-line ones: GTK 3 and Qt 6 fields, and an input and a textarea in Firefox and in Chromium.

Every key reaches the application as it reaches NVDA, so the application has often deleted a
character before NVDA's script could read it. Each test deletes many characters, one at a time, in a
quick burst and with the key held so that it repeats, and compares each announcement with the
character that really went. The text is made of characters that all differ, Polish ones among them,
so that a neighbour announced in place of the deleted character shows.

LINDA_RIG_WM=marco nix develop . -c tests/linux/rig python3 -m pytest -q tests/linux/test_deletedCharacter.py
"""

import subprocess
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import chromiumSession
import desktopSession
import pytest
import webSession
from desktopSession import DesktopSession
from test_atspiText import WINDOW_TITLE, TextSession
from test_keys import Keyboard

#: Characters that all differ, so that every announcement names one place in the text.
DISTINCT = "abcdefghijklmnopqrstuvwxyząćęłńóśźż0123456789"
#: Keys pressed one at a time, each once the one before has been answered.
ONE_AT_A_TIME = 20
#: Keys pressed in a burst, and the milliseconds between them.
BURST = 20
BURST_DELAY_MS = 25
#: Seconds a key is held, so that it repeats.
HOLD_SECONDS = 1.0
#: Seconds for an answer to a key, and for anything more to come after it.
ANSWER_BOUND = 2.0
SETTLE = 0.6
#: The transcript synth takes 10 ms a character, so that a burst cuts speech short as a person's would.
TIMED_SPEECH = {"LINDA_TRANSCRIPT_MODE": "timed", "LINDA_TRANSCRIPT_MS_PER_CHAR": "10"}
STARTUP_BOUND = 60.0
PAGE = "plainFields.html"
PAGE_TITLE = "Plain fields"


def _atspi():
	return desktopSession._atspi()


def _find(node, name: str, depth: int = 0):
	if node is None or depth > 40:
		return None
	Atspi = _atspi()
	if node.get_name() == name and node.get_state_set().contains(Atspi.StateType.EDITABLE):
		return node
	for index in range(node.get_child_count()):
		found = _find(node.get_child_at_index(index), name, depth + 1)
		if found is not None:
			return found
	return None


def findField(windowPrefix: str, name: str):
	"""The editable object named name in the top-level window whose name starts with windowPrefix."""
	desktop = _atspi().get_desktop(0)
	for appIndex in range(desktop.get_child_count()):
		app = desktop.get_child_at_index(appIndex)
		for index in range(app.get_child_count() if app else 0):
			window = app.get_child_at_index(index)
			if window is not None and (window.get_name() or "").startswith(windowPrefix):
				found = _find(window, name)
				if found is not None:
					return found
	raise AssertionError(f"no field {name!r} in a window named {windowPrefix!r}…")


def spelled(character: str) -> str:
	"""What NVDA says for a character it spells."""
	return {" ": "space", "\n": "line feed"}.get(character, character)


@dataclass
class Fields:
	"""The plain fields of one application, with NVDA reading them."""

	desktop: DesktopSession
	window: str
	focusField: Callable[[str], None]
	kind: str

	def node(self, name: str):
		return findField(self.window, name)

	def set(self, name: str, text: str, caret: int) -> None:
		Atspi = _atspi()
		node = self.node(name)
		if self.kind in ("gtk3", "qt6"):
			assert Atspi.EditableText.set_text_contents(node, text), name
		else:
			self._type(name, text)
		deadline = time.monotonic() + 10
		while self.state(name)[0] != text:
			assert time.monotonic() < deadline, f"{name} holds {self.state(name)[0]!r}, not {text!r}"
			time.sleep(0.02)
		Atspi.Text.set_caret_offset(node, caret)
		moved = time.monotonic() + 1
		while self.state(name)[1] != caret and time.monotonic() < moved:
			time.sleep(0.02)
		if self.state(name)[1] != caret:
			subprocess.run(["xdotool", "key", "ctrl+Home", *["Right"] * caret], check=True)
		while self.state(name)[1] != caret:
			assert time.monotonic() < deadline, f"{name}'s caret is at {self.state(name)[1]}, not {caret}"
			time.sleep(0.02)
		time.sleep(SETTLE)

	def _type(self, name: str, text: str) -> None:
		"""Type text over all of a field's: neither browser changes it through AT-SPI. xdotool types a
		letter the layout lacks by mapping it to a spare key for a moment, which Chromium sometimes
		misses, so a text that did not arrive whole is typed again."""
		for _ in range(4):
			subprocess.run(["xdotool", "key", "ctrl+a", "BackSpace"], check=True)
			for index, line in enumerate(text.split("\n")):
				if index:
					subprocess.run(["xdotool", "key", "Return"], check=True)
				subprocess.run(["xdotool", "type", "--delay", "30", line], check=True)
			time.sleep(0.3)
			if self.state(name)[0] == text:
				return

	def state(self, name: str) -> tuple[str, int]:
		Atspi = _atspi()
		node = self.node(name)
		text = Atspi.Text.get_text(node, 0, -1).replace(" ", "\n")
		return text, Atspi.Text.get_caret_offset(node)

	def spoken(self, mark: int) -> list[str]:
		return [s for s in self.desktop.spokenSince(mark) if s.strip()]

	def answer(self, key: str) -> list[str]:
		"""Press key, and what NVDA said for it once it has said something and then nothing for a while."""
		mark = self.desktop.mark()
		subprocess.run(["xdotool", "key", key], check=True)
		deadline = time.monotonic() + ANSWER_BOUND
		while not self.spoken(mark) and time.monotonic() < deadline:
			time.sleep(0.01)
		time.sleep(0.15)
		return self.spoken(mark)


def textappFields(desktop: DesktopSession, directory: Path, toolkit: str) -> Fields:
	session = TextSession(desktop, directory, toolkit)

	def focus(name: str) -> None:
		if name == "multi":
			session.focus("multi", "multi edit multi line…")

	return Fields(desktop, WINDOW_TITLE, focus, toolkit)


def browserFields(desktop: DesktopSession, browser: str) -> Fields:
	webSession.requireBrowseLibraries()
	if browser == "chromium":
		chromiumSession.clearAccessibilityStatus()
	nvda = desktop.startNVDA(webSession.BROWSE_CONFIG, env=TIMED_SPEECH)
	nvda.waitForLog("NVDA initialized", STARTUP_BOUND)
	if browser == "chromium":
		chromiumSession.startChromium(desktop, PAGE)
	else:
		webSession.startFirefox(desktop, PAGE)
	desktop.expect("Two plain fields.", since=0, timeout=STARTUP_BOUND)

	def focus(name: str) -> None:
		mark = desktop.mark()
		Atspi = _atspi()
		node = findField(PAGE_TITLE, name)
		assert Atspi.Component.grab_focus(node), name
		desktop.expect(f"{name}…", since=mark)
		time.sleep(SETTLE)

	return Fields(desktop, PAGE_TITLE, focus, browser)


KINDS = ["gtk3", "qt6", "firefox", "chromium"]


@pytest.fixture(scope="module")
def windowManager(tmp_path_factory: pytest.TempPathFactory) -> Iterator[DesktopSession]:
	with DesktopSession(tmp_path_factory.mktemp("windowManager")) as session:
		yield session


@pytest.fixture(params=KINDS)
def fields(request: pytest.FixtureRequest, windowManager: DesktopSession, tmp_path: Path) -> Iterator[Fields]:
	with DesktopSession(tmp_path / "desktop") as desktop:
		if request.param in ("firefox", "chromium"):
			yield browserFields(desktop, request.param)
		else:
			yield textappFields(desktop, tmp_path / "text", request.param)
		nvda = desktop.nvda
	assert nvda.process.returncode == 0, nvda.describe()


def assertBackspaceRun(fields: Fields, name: str, mark: int, before: str, caret: int) -> None:
	"""What NVDA said since mark, after Backspace was pressed some times at caret in before, names only
	characters that went, in the order they went, and the last one it names is the last that went."""
	after, newCaret = fields.state(name)
	assert after == before[:newCaret] + before[caret:], (before, after)
	gone = before[newCaret:caret][::-1]
	assert gone, "nothing was deleted"
	spoken = fields.spoken(mark)
	assert spoken, f"nothing was said for {gone!r}"
	expected = [spelled(c) for c in gone]
	wrong = [s for s in spoken if s not in expected]
	assert not wrong, f"said {wrong} of {spoken}, but deleted {gone!r}"
	positions = [expected.index(s) for s in spoken]
	assert positions == sorted(set(positions)), f"said {spoken} for {gone!r}"
	assert positions[-1] == len(gone) - 1, f"said {spoken} last for {gone!r}"


@pytest.mark.parametrize("name", ["entry", "multi"])
def test_backspaceOneAtATime(fields: Fields, name: str):
	fields.focusField(name)
	fields.set(name, DISTINCT, len(DISTINCT))
	misses = []
	for _ in range(ONE_AT_A_TIME):
		before, caret = fields.state(name)
		spoken = fields.answer("BackSpace")
		if spoken != [spelled(before[caret - 1])]:
			misses.append((before[caret - 1], spoken))
	assert not misses, f"{len(misses)} of {ONE_AT_A_TIME} wrong: {misses}"


@pytest.mark.parametrize("name", ["entry", "multi"])
def test_deleteOneAtATime(fields: Fields, name: str):
	fields.focusField(name)
	fields.set(name, DISTINCT, 0)
	misses = []
	for _ in range(ONE_AT_A_TIME):
		spoken = fields.answer("Delete")
		after, caret = fields.state(name)
		expected = spelled(after[caret]) if caret < len(after) else "blank"
		if spoken != [expected]:
			misses.append((expected, spoken))
	assert not misses, f"{len(misses)} of {ONE_AT_A_TIME} wrong: {misses}"


@pytest.mark.parametrize("name", ["entry", "multi"])
def test_backspaceBurst(fields: Fields, name: str):
	fields.focusField(name)
	fields.set(name, DISTINCT, len(DISTINCT))
	mark = fields.desktop.mark()
	subprocess.run(
		["xdotool", "key", "--delay", str(BURST_DELAY_MS), *["BackSpace"] * BURST],
		check=True,
	)
	time.sleep(SETTLE * 2)
	assertBackspaceRun(fields, name, mark, DISTINCT, len(DISTINCT))


@pytest.mark.parametrize("name", ["entry", "multi"])
def test_backspaceHeld(fields: Fields, name: str):
	fields.focusField(name)
	fields.set(name, DISTINCT, len(DISTINCT))
	keyboard = Keyboard()
	try:
		mark = fields.desktop.mark()
		keyboard.down("BackSpace")
		time.sleep(HOLD_SECONDS)
		keyboard.up("BackSpace")
	finally:
		keyboard.close()
	time.sleep(SETTLE * 2)
	assertBackspaceRun(fields, name, mark, DISTINCT, len(DISTINCT))


def test_deleteHeld(fields: Fields):
	fields.focusField("entry")
	fields.set("entry", DISTINCT, 0)
	keyboard = Keyboard()
	try:
		mark = fields.desktop.mark()
		keyboard.down("Delete")
		time.sleep(HOLD_SECONDS)
		keyboard.up("Delete")
	finally:
		keyboard.close()
	time.sleep(SETTLE * 2)
	after, caret = fields.state("entry")
	assert caret == 0 and DISTINCT.endswith(after) and after != DISTINCT, after
	spoken = fields.spoken(mark)
	assert spoken and spoken[-1] == spelled(after[0]), (after, spoken)
	assert all(s in DISTINCT for s in spoken), spoken


def test_edgesOfTheField(fields: Fields):
	fields.focusField("entry")
	fields.set("entry", "żółw", 1)
	assert fields.answer("BackSpace") == ["ż"]
	assert fields.state("entry") == ("ółw", 0)
	# Nothing before the caret: nothing goes and nothing is said.
	assert fields.answer("BackSpace") == []
	fields.set("entry", "żółw", 3)
	assert fields.answer("Delete") == ["blank"]
	assert fields.state("entry") == ("żół", 3)
	assert fields.answer("Delete") == []


def test_selectionAndWords(fields: Fields):
	fields.focusField("entry")
	fields.set("entry", "ala ma kota", 11)
	mark = fields.desktop.mark()
	fields.desktop.press("shift+Left", "shift+Left", "shift+Left")
	deadline = time.monotonic() + ANSWER_BOUND
	while len([s for s in fields.spoken(mark) if s.endswith("selected")]) < 3:
		assert time.monotonic() < deadline, fields.spoken(mark)
		time.sleep(0.05)
	time.sleep(SETTLE)
	assert "ota" in fields.answer("BackSpace")
	assert fields.state("entry") == ("ala ma k", 8)
	fields.set("entry", "ala ma kota", 11)
	assert fields.answer("ctrl+BackSpace") == ["kota"]
	assert fields.state("entry") == ("ala ma ", 7)
	fields.set("entry", "ala ma kota", 4)
	spoken = fields.answer("ctrl+Delete")
	after, caret = fields.state("entry")
	assert after in ("ala kota", "ala  kota") and caret == 4, after
	# The word now at the caret, as NVDA on Windows says it, and not the one that went.
	assert spoken and not [s for s in spoken if "ma" in s.split()], spoken


def test_lineBreak(fields: Fields):
	fields.focusField("multi")
	fields.set("multi", "pierwsza\nźdźbło", 9)
	assert fields.answer("BackSpace") == ["line feed"]
	assert fields.state("multi") == ("pierwszaźdźbło", 8)


@pytest.mark.parametrize("fields", ["firefox"], indirect=True)
def test_firefoxAddressBar(fields: Fields):
	mark = fields.desktop.mark()
	fields.desktop.press("ctrl+l")
	deadline = time.monotonic() + ANSWER_BOUND
	while not any("or enter address" in s for s in fields.spoken(mark)):
		assert time.monotonic() < deadline, fields.spoken(mark)
		time.sleep(0.05)
	field = _findAddressBar(findField(PAGE_TITLE, "entry").get_application())
	assert field is not None, "no address bar"
	fields.node = lambda name: field
	fields.set("bar", DISTINCT, len(DISTINCT))
	misses = []
	for _ in range(ONE_AT_A_TIME):
		before, caret = fields.state("bar")
		spoken = fields.answer("BackSpace")
		if not spoken or spoken[0] != spelled(before[caret - 1]):
			misses.append((before[caret - 1], spoken))
	assert not misses, f"{len(misses)} of {ONE_AT_A_TIME} wrong: {misses}"


def _findAddressBar(node, depth: int = 0):
	if node is None or depth > 30:
		return None
	Atspi = _atspi()
	states = node.get_state_set()
	if (node.get_name() or "").endswith("or enter address") and states.contains(Atspi.StateType.EDITABLE):
		return node
	if node.get_role_name() == "document web":
		return None
	for index in range(node.get_child_count()):
		found = _findAddressBar(node.get_child_at_index(index), depth + 1)
		if found is not None:
			return found
	return None


MULTI = "Zażółć gęślą jaźń\n\nśrodkowa linia\nostatnia"


def test_caretKeysInMultiLine(fields: Fields):
	fields.focusField("multi")
	fields.set("multi", MULTI, 0)
	assert fields.answer("Down") == ["blank"]
	assert fields.answer("Down") == ["środkowa linia"]
	assert fields.answer("End") == ["line feed"]
	assert fields.answer("Home") == ["ś"]
	assert fields.answer("Up") == ["blank"]
	assert fields.answer("Up") == ["Zażółć gęślą jaźń"]
	assert fields.answer("ctrl+End") == ["ostatnia"]
	assert fields.answer("End") == ["blank"]
	assert fields.answer("ctrl+Home") == ["Zażółć gęślą jaźń"]
	assert fields.answer("End") == ["line feed"]
