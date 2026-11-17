"""Browse mode's keys on a web page: NVDA takes them alone in browse mode, and gives them to the page in
focus mode, through synchronous grabs on the rig's display, which a slow or stopped NVDA must never
hold for long.

BASEDREADER_VBUF_LIB=ROOT/engine/target/vbuf/libnvdaVbuf.so \
BASEDREADER_VBUF_ENGINE=ROOT/engine/target/release/libbre_vbuf.so \
BASEDREADER_RIG_WM=marco nix develop . -c tests/linux/rig python3 -m pytest -q tests/linux/test_browseKeys.py
"""

import json
import os
import re
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import desktopSession
import harness
import pytest
import webSession
from desktopSession import PREFIX_MARK, DesktopSession
from test_keys import Keyboard

#: Seconds from NVDA's start to the page's buffer being built.
BUFFER_BOUND = 60.0
#: Seconds for anything a key starts to settle before the next key.
SETTLE = 0.5
#: Seconds the test hook blocks NVDA's main thread for.
BLOCK_SECONDS = 5
#: Seconds the X server held a captured key for before it was replayed without NVDA's answer, as the
#: key source has it, and how much later a key may arrive than that.
CAPTURE_TIMEOUT = 0.1
ARRIVAL_SLACK = 0.15
#: How Firefox's address bar is announced, as far as it is always the same: Firefox names the search
#: engine in it, "Search with Google or enter address", only once it has loaded its search engines.
ADDRESS_BAR = "combo box Search"

#: A global plugin for these tests only: a command that blocks NVDA's main thread, and, in place of
#: Windows' default sound for a key browse mode traps, a log record.
PROBE_PLUGIN = f"""
import time

import browseMode
import globalPluginHandler
import virtualBuffers.atspi
from logHandler import log
from scriptHandler import script


def _trap(self, gesture):
	log.info("Trapped a key in browse mode")


# On the buffer class too, which may have a sound of its own for it.
browseMode.BrowseModeTreeInterceptor.script_trapNonCommandGesture = _trap
virtualBuffers.atspi.AtSpiBuffer.script_trapNonCommandGesture = _trap


class GlobalPlugin(globalPluginHandler.GlobalPlugin):
	@script(gesture="kb:NVDA+shift+f11")
	def script_blockMainThread(self, gesture):
		log.info("Blocking the main thread")
		time.sleep({BLOCK_SECONDS})
		log.info("The main thread is free again")
"""

#: A GTK entry that prints each change of its text with the time it came, for the X-only test.
ENTRY_APP = """
import time
import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk
window = Gtk.Window(title="Held keys entry")
entry = Gtk.Entry()
window.add(entry)
entry.connect("changed", lambda e: print(f"{time.monotonic()} {e.get_text()}", flush=True))
window.connect("destroy", Gtk.main_quit)
window.show_all()
print("ready", flush=True)
Gtk.main()
"""

#: Run with the key source alone: a decider that takes 300 ms over each captured key, then one that
#: answers at once, dropping x and passing y.
SLOW_DECIDER_CHILD = r"""
import subprocess
import time

from linuxPlatform.keys import x11

decided = []
slow = {"on": True}


def deliver(event):
	if not event.captured:
		return True
	decided.append(event.vkCode)
	if slow["on"]:
		time.sleep(0.3)
		return False
	return chr(event.vkCode) != "X"


source = x11.X11KeySource()
source.start(deliver)
source.setCapture("browse")
time.sleep(0.5)
pressed = []
for key in "hjk":
	pressed.append(time.monotonic())
	subprocess.run(["xdotool", "key", key], check=True)
	time.sleep(0.5)
subprocess.run(["xdotool", "type", "--delay", "20", "abc"], check=True)
time.sleep(1.5)
slow["on"] = False
subprocess.run(["xdotool", "key", "x", "y"], check=True)
time.sleep(0.5)
source.stop()
result = {"pressed": pressed, "decided": [chr(vk) for vk in decided]}
"""


def browseConfig(directory: Path) -> Path:
	"""webSession's browse configuration, without say all as a page loads, so that nothing reads while
	keys are pressed."""
	text = webSession.BROWSE_CONFIG.read_text()
	text = text.replace("[virtualBuffers]\n", "[virtualBuffers]\n\tautoSayAllOnPageLoad = False\n")
	assert "autoSayAllOnPageLoad" in text
	path = directory / "nvda.ini"
	path.write_text(text)
	return path


def probeHooks(directory: Path) -> dict[str, str]:
	"""The environment that gives NVDA the transcript synth and this file's probe plugin."""
	hooks = directory / "hooks"
	(hooks / "globalPlugins").mkdir(parents=True)
	(hooks / "synthDrivers").symlink_to(harness.HERE / "synthDrivers")
	(hooks / "globalPlugins" / "browseKeysProbe.py").write_text(PROBE_PLUGIN)
	return {"BASEDREADER_TEST_HOOKS_DIR": str(hooks)}


def expect(desktop: DesktopSession, *texts: str, since: int, timeout: float = 5) -> None:
	"""desktop.expect, read again when it met a transcript line the synth was still writing."""
	for _ in range(50):
		try:
			return desktop.expect(*texts, since=since, timeout=timeout)
		except json.JSONDecodeError:
			time.sleep(0.01)
	return desktop.expect(*texts, since=since, timeout=timeout)


def step(desktop: DesktopSession, chord: str, *expected: str, timeout: float = 5) -> int:
	"""Press chord, wait for what NVDA says to it, and let it settle; returns the mark before it."""
	mark = desktop.mark()
	desktop.press(chord)
	expect(desktop, *expected, since=mark, timeout=timeout)
	time.sleep(SETTLE)
	return mark


def captureMode(nvda: harness.Launch) -> str | None:
	"""The keys NVDA captures now, as its log last said."""
	found = re.findall(r"Keys captured for (\w+) mode", nvda.text("nvda.log"))
	return found[-1] if found else None


def waitForCapture(nvda: harness.Launch, mode: str, timeout: float = 5) -> None:
	deadline = time.monotonic() + timeout
	while captureMode(nvda) != mode:
		assert time.monotonic() < deadline, f"NVDA did not capture {mode} mode's keys:\n{nvda.describe()}"
		time.sleep(0.05)


def _matches(Atspi, root, role):
	rule = Atspi.MatchRule.new(
		None,
		Atspi.CollectionMatchType.ALL,
		None,
		Atspi.CollectionMatchType.ALL,
		[role],
		Atspi.CollectionMatchType.ANY,
		None,
		Atspi.CollectionMatchType.ALL,
		False,
	)
	return Atspi.Collection.get_matches(root, rule, Atspi.CollectionSortOrder.CANONICAL, 0, True) or []


def _byId(node, wanted: str, depth: int = 0):
	if (node.get_attributes() or {}).get("id") == wanted:
		return node
	if depth > 20:
		return None
	for index in range(node.get_child_count()):
		child = node.get_child_at_index(index)
		if child is not None and (found := _byId(child, wanted, depth + 1)) is not None:
			return found
	return None


class KeysPage:
	"""The keys page's text field and key log, found once and then read straight from Firefox.

	Nothing here asks another application anything once found, so it reads on while NVDA, whose own
	windows are on the accessibility desktop too, does not answer.
	"""

	def __init__(self) -> None:
		Atspi = self._Atspi = desktopSession._atspi()
		desktop = Atspi.get_desktop(0)
		desktop.clear_cache()
		for index in range(desktop.get_child_count()):
			app = desktop.get_child_at_index(index)
			if app is None or app.get_toolkit_name() != "Gecko":
				continue
			documents = [
				d for d in _matches(Atspi, app, Atspi.Role.DOCUMENT_WEB) if d.get_name() == "Keys page"
			]
			if documents:
				break
		else:
			raise AssertionError("no Firefox shows the keys page")
		self._field = next(
			e for e in _matches(Atspi, documents[0], Atspi.Role.ENTRY) if e.get_name() == "Field"
		)
		self._log = _byId(documents[0], "log")
		assert self._log is not None, "the keys page has no key log"
		self._focusLog = _byId(documents[0], "focusLog")
		assert self._focusLog is not None, "the keys page has no focus log"
		# Firefox fills the text part of its cache the first time a client asks for it.
		self.log()
		self.field()
		self.focusLog()
		time.sleep(webSession.CACHE_FILL_SECONDS)

	def log(self) -> list[str]:
		"""Each key press that reached the page, as webSession.pageKeyLog names it."""
		return self._Atspi.Text.get_text(self._log, 0, -1).split()

	def field(self) -> str:
		return self._Atspi.Text.get_text(self._field, 0, -1)

	def focusLog(self) -> list[str]:
		"""Each time the page's window lost or gained the focus, or its document was hidden or shown."""
		return self._Atspi.Text.get_text(self._focusLog, 0, -1).split()

	def waitForLog(self, expected: list[str], timeout: float = 3) -> float:
		"""Wait until the page has logged expected; returns when it had."""
		deadline = time.monotonic() + timeout
		while (logged := self.log()) != expected:
			assert time.monotonic() < deadline, f"the page logged {logged}, not {expected}"
			time.sleep(0.01)
		return time.monotonic()

	def waitForField(self, text: str, timeout: float = 3) -> float:
		deadline = time.monotonic() + timeout
		while (found := self.field()) != text:
			assert time.monotonic() < deadline, f"the field holds {found!r}, not {text!r}"
			time.sleep(0.01)
		return time.monotonic()


def startPage(desktop: DesktopSession, tmp_path: Path) -> tuple[harness.Launch, KeysPage]:
	"""Firefox on the keys page, then NVDA, with its buffer built and browse mode's keys captured."""
	webSession.requireBrowseLibraries()
	webSession.startFirefox(desktop, "keys.html")
	nvda = desktop.startNVDA(browseConfig(tmp_path), env=probeHooks(tmp_path))
	nvda.waitForLog("Buffer rendered", BUFFER_BOUND)
	waitForCapture(nvda, "browse", timeout=BUFFER_BOUND)
	time.sleep(SETTLE)
	page = KeysPage()
	assert page.log() == []
	return nvda, page


def enterField(desktop: DesktopSession, nvda: harness.Launch) -> None:
	"""From the top of the page, Tab into its text field, which turns focus mode on."""
	mark = step(desktop, "Tab", "Field edit" + PREFIX_MARK, "Focus mode")
	waitForCapture(nvda, "focus")
	# A grab that took the focus from Firefox, however briefly, would have NVDA announce its window.
	spoken = desktop.spokenSince(mark)
	assert not [text for text in spoken if "Mozilla Firefox" in text], spoken


def quitNVDA(desktop: DesktopSession, nvda: harness.Launch, *, ignoring: tuple[str, ...] = ()) -> None:
	step(desktop, "Insert+q", "Exit NVDA" + PREFIX_MARK)
	desktop.press("Return")
	nvda.waitForExit(10)
	nvda.assertClean(ignoring=ignoring, logNames=("nvda.log",))


@pytest.fixture
def desktop(tmp_path: Path) -> Iterator[DesktopSession]:
	with DesktopSession(tmp_path / "desktop") as session:
		yield session


def test_heldKeysAreReplayedWithoutAnAnswer(desktop: DesktopSession, tmp_path: Path):
	app = tmp_path / "entry.py"
	app.write_text(ENTRY_APP)
	entry = desktop.startApp([sys.executable, str(app)], title="Held keys entry")
	assert entry.stdout.readline().strip() == "ready"
	desktopSession.activate("Held keys entry")
	result = harness.runChild(tmp_path, SLOW_DECIDER_CHILD, prepare="config", timeout=60)
	result.assertClean()
	os.set_blocking(entry.stdout.fileno(), False)
	changes = [line.split(" ", 1) for line in (entry.stdout.read() or "").splitlines()]
	texts = [text for _when, text in changes]
	assert texts == ["h", "hj", "hjk", "hjka", "hjkab", "hjkabc", "hjkabcy"], texts
	for (when, _text), pressed in zip(changes, result.value["pressed"]):
		late = float(when) - pressed
		assert CAPTURE_TIMEOUT <= late <= CAPTURE_TIMEOUT + ARRIVAL_SLACK, (
			f"replayed {late:.3f} s after the press"
		)
	# b and c reached the entry while the decider was still over a, so it never asked about them.
	assert result.value["decided"] == list("HJKAXY")


def test_browseAndFocusModeKeys(desktop: DesktopSession, tmp_path: Path):
	nvda, page = startPage(desktop, tmp_path)

	enterField(desktop, nvda)
	desktop.typeText("ab")
	page.waitForLog(["a", "b"])
	page.waitForField("ab")
	step(desktop, "Escape", "Browse mode")
	waitForCapture(nvda, "browse")

	step(desktop, "control+Home", "heading level 1 Keys page")
	step(desktop, "Down", "Every key that reaches this page is written to the log at its end.")
	step(desktop, "h", "Field and link heading level 2")
	# Space activates the heading, which does nothing to be heard; that NVDA had it, the page not, is
	# what counts.
	typed = nvda.text("nvda.log").count("Input: kb(desktop):(typing key)")
	desktop.press("space", gap=SETTLE)
	assert nvda.text("nvda.log").count("Input: kb(desktop):(typing key)") == typed + 1
	step(desktop, "1", "No next heading at level 1")
	assert page.log() == ["a", "b"]
	assert page.field() == "ab"

	# Control tapped to stop speech, then held again with Home: xdotool releases Control twice, the
	# second time with only a raw event, which must not reach NVDA between Control and Home.
	mark = desktop.mark()
	subprocess.run(
		["xdotool", "key", "Control_L", "sleep", "0.005", "keydown", "Control_L", "sleep", "0.02"]
		+ ["key", "Home", "keyup", "Control_L"],
		check=True,
	)
	expect(desktop, "heading level 1 Keys page", since=mark)
	time.sleep(SETTLE)
	step(desktop, "h", "Field and link heading level 2")

	mark = desktop.mark()
	subprocess.run(["xdotool", "keydown", "Down"], check=True)
	time.sleep(1)
	subprocess.run(["xdotool", "keyup", "Down"], check=True)
	time.sleep(SETTLE)
	assert len(desktop.spokenSince(mark)) >= 3, desktop.spokenSince(mark)
	assert page.log() == ["a", "b"]

	step(desktop, "Insert+shift+space", "Single letter navigation off")
	desktop.press("h")
	page.waitForLog(["a", "b", "h"])
	step(desktop, "Insert+shift+space", "Single letter navigation on")
	assert page.log() == ["a", "b", "h"]

	step(desktop, "control+l", ADDRESS_BAR + PREFIX_MARK)
	waitForCapture(nvda, "off")
	# Firefox shows a page the shortcuts it does not reserve before acting on them, as it does with no
	# screen reader running; once is right.
	assert page.log() == ["a", "b", "h", "Control+l"]
	quitNVDA(desktop, nvda)


def gtkEntryText() -> str:
	"""What the GTK fixture's text field holds."""
	Atspi = desktopSession._atspi()
	desktop = Atspi.get_desktop(0)
	for appIndex in range(desktop.get_child_count()):
		app = desktop.get_child_at_index(appIndex)
		for index in range(app.get_child_count() if app else 0):
			window = app.get_child_at_index(index)
			if window is not None and window.get_name() == "Fixture window":
				entries = _matches(Atspi, window, Atspi.Role.TEXT)
				assert entries, "the fixture has no text field"
				return Atspi.Text.get_text(entries[0], 0, -1)
	raise AssertionError("no fixture window")


def test_altTabLeavesThePageItsKeys(desktop: DesktopSession, tmp_path: Path):
	webSession.requireBrowseLibraries()
	desktop.startApp(desktop.fixture("gtkFixture"), title="Fixture window")
	desktopSession.activate("Fixture window")
	# To the text field, which holds "Alice".
	desktop.press("Tab", "Tab", "Tab", "Tab")
	nvda, page = startPage(desktop, tmp_path)

	step(desktop, "alt+Tab", "Fixture window" + PREFIX_MARK)
	waitForCapture(nvda, "off")
	desktop.typeText("x")
	deadline = time.monotonic() + 3
	while "x" not in (text := gtkEntryText()):
		assert time.monotonic() < deadline, f"x did not reach the fixture's field, which holds {text!r}"
		time.sleep(0.05)

	step(desktop, "alt+Tab", "Keys page" + PREFIX_MARK)
	waitForCapture(nvda, "browse")
	step(desktop, "control+Home", "heading level 1 Keys page")
	step(desktop, "h", "Field and link heading level 2")
	assert page.log() == []

	# The first key after Alt+Tab is the new window's, though NVDA's focus follows the window only once
	# its application has told it: typing in the fixture, and NVDA's on the page.
	mark = desktop.mark()
	subprocess.run(["xdotool", "key", "alt+Tab", "sleep", "0.03", "key", "y"], check=True)
	expect(desktop, "Fixture window" + PREFIX_MARK, since=mark)
	waitForCapture(nvda, "off")
	deadline = time.monotonic() + 3
	while "y" not in (text := gtkEntryText()):
		assert time.monotonic() < deadline, f"y did not reach the fixture's field, which holds {text!r}"
		time.sleep(0.05)
	time.sleep(SETTLE)
	mark = desktop.mark()
	subprocess.run(["xdotool", "key", "alt+Tab", "sleep", "0.03", "key", "h"], check=True)
	expect(desktop, "no next heading", since=mark)
	time.sleep(SETTLE)
	assert page.log() == []
	quitNVDA(desktop, nvda)


def test_keysFollowTheFocusToAnotherProgramsPage(desktop: DesktopSession, tmp_path: Path):
	"""A page in another program, such as an Electron program beside Firefox, has its keys captured once
	NVDA's focus is in it, though the page left before still holds its window. A second Firefox is a
	process of its own."""
	webSession.requireBrowseLibraries()
	webSession.startFirefox(desktop, "usable.html")
	nvda = desktop.startNVDA(browseConfig(tmp_path), env=probeHooks(tmp_path))
	nvda.waitForLog("Buffer rendered", BUFFER_BOUND)
	waitForCapture(nvda, "browse", timeout=BUFFER_BOUND)
	mark = desktop.mark()
	webSession.startFirefox(desktop, "keys.html")
	expect(desktop, "Keys page" + PREFIX_MARK, since=mark, timeout=BUFFER_BOUND)
	page = KeysPage()
	step(desktop, "control+Home", "heading level 1 Keys page")
	step(desktop, "h", "Field and link heading level 2")
	assert captureMode(nvda) == "browse", nvda.describe()
	assert page.log() == []
	quitNVDA(desktop, nvda)


def inputs(log: harness.Launch | str, *names: str) -> int:
	"""How many times NVDA's log says a gesture was input, by any of its names: the order of the
	modifiers in a name is the order they were pressed in."""
	text = log.text("nvda.log") if isinstance(log, harness.Launch) else log
	return sum(text.count(f"Input: kb(desktop):{name}\n") for name in names)


@pytest.mark.parametrize("key", ("Insert", "Caps_Lock"))
def test_bothNVDAKeysOnAPage(desktop: DesktopSession, tmp_path: Path, key: str):
	nvda, page = startPage(desktop, tmp_path)
	enterField(desktop, nvda)
	desktop.typeText("ab")
	page.waitForLog(["a", "b"])
	step(desktop, "Escape", "Browse mode")
	waitForCapture(nvda, "browse")

	find = ("control+NVDA+f", "NVDA+control+f")
	for mode in ("browse", "focus"):
		# NVDA+space turns focus mode on and off.
		step(desktop, f"{key}+space", "Focus mode")
		waitForCapture(nvda, "focus")
		if mode == "browse":
			step(desktop, f"{key}+space", "Browse mode")
			waitForCapture(nvda, "browse")
			log = nvda.text("nvda.log")
			# F5 reaching Firefox would load the page again, and empty its log. In focus mode NVDA+F5 has
			# no command, and Firefox has F5, as on Windows.
			desktop.press(f"{key}+F5", gap=1)
			assert inputs(nvda, "NVDA+f5") == inputs(log, "NVDA+f5") + 1
		step(desktop, f"{key}+t", "Keys page" + PREFIX_MARK)
		if mode == "focus":
			step(desktop, f"{key}+space", "Browse mode")
			waitForCapture(nvda, "browse")
		assert page.log() == ["a", "b"]
	assert not desktopSession.capsLockIsOn()

	# Last, as closing NVDA's find dialog destroys it from inside its own modal loop, which wxGTK
	# survives only some of the time; NVDA is stopped with it open instead.
	log = nvda.text("nvda.log")
	mark = desktop.mark()
	desktop.press(f"{key}+control+f")
	expect(desktop, "Find dialog" + PREFIX_MARK, since=mark)
	assert inputs(nvda, *find) == inputs(log, *find) + 1
	assert page.log() == ["a", "b"]
	nvda.process.terminate()
	nvda.waitForExit(20)
	# Opening the dialog has GTK complain of a window that is not one; the dialog's own, and nothing to
	# do with keys.
	nvda.assertClean(ignoring=("stderr",), logNames=("nvda.log",))
	unexpected = harness.unexpectedStandardError(nvda.text("stderr.txt"))
	assert all(
		"gtk_window_set_modal: assertion 'GTK_IS_WINDOW (window)' failed" in line
		for line in unexpected.splitlines()
		if line.strip()
	), unexpected


@pytest.fixture
def keyboard() -> Iterator[Keyboard]:
	board = Keyboard()
	yield board
	board.close()


def stepOn(keyboard: Keyboard, desktop: DesktopSession, chord: str, *expected: str) -> None:
	"""step, with the chord typed on the rig's keyboard rather than by xdotool."""
	mark = desktop.mark()
	keyboard.press(chord)
	expect(desktop, *expected, since=mark)
	time.sleep(SETTLE)


@pytest.mark.parametrize("key", ("Insert", "Caps_Lock"))
def test_keysNVDAPassesReachThePage(desktop: DesktopSession, keyboard: Keyboard, tmp_path: Path, key: str):
	nvda, page = startPage(desktop, tmp_path)
	stepOn(keyboard, desktop, "control+Home", "heading level 1 Keys page")

	# Passed through, a letter browse mode would take reaches the page instead.
	stepOn(keyboard, desktop, f"{key}+F2", "Pass next key through")
	keyboard.press("h")
	page.waitForLog(["h"])
	# A chord with the NVDA key that NVDA has no command for reaches the page without the NVDA key;
	# Caps Lock held is the Lock modifier to the X server, which the letter comes with.
	z, y = ("z", "y") if key == "Insert" else ("Z", "Y")
	keyboard.press(f"{key}+z")
	page.waitForLog(["h", z])
	keyboard.press(f"{key}+control+y")
	page.waitForLog(["h", z, f"Control+{y}"])
	stepOn(keyboard, desktop, "Down", "Every key that reaches this page is written to the log at its end.")
	logged = page.log()

	# The NVDA key twice reaches the page as itself.
	if key == "Insert":
		keyboard.press("Insert")
		time.sleep(0.15)
		keyboard.press("Insert")
		logged.append("Insert")
		page.waitForLog(logged)
	else:
		mark = desktop.mark()
		keyboard.press("Caps_Lock")
		time.sleep(0.15)
		keyboard.press("Caps_Lock")
		expect(desktop, "caps lock on", since=mark)
		assert desktopSession.capsLockIsOn()
		keyboard.press("Caps_Lock")
		time.sleep(0.15)
		keyboard.press("Caps_Lock")
		expect(desktop, "caps lock on", "caps lock off", since=mark)
		assert not desktopSession.capsLockIsOn()
		time.sleep(SETTLE)
		assert page.log() == logged

	# Escape in browse mode is sent on to the page by NVDA's script for it, once the key is let go.
	keyboard.press("Escape")
	logged.append("Escape")
	page.waitForLog(logged)

	# In focus mode, Escape passed through stays with the page.
	enterField(desktop, nvda)
	stepOn(keyboard, desktop, f"{key}+F2", "Pass next key through")
	mark = desktop.mark()
	keyboard.press("Escape")
	logged.append("Escape")
	page.waitForLog(logged)
	time.sleep(SETTLE)
	assert "Browse mode" not in desktop.spokenSince(mark)
	stepOn(keyboard, desktop, "Escape", "Browse mode")
	waitForCapture(nvda, "browse")
	assert page.log() == logged
	assert not desktopSession.capsLockIsOn()
	quitNVDA(desktop, nvda)


def test_keyUnderAHeldBrowseKeyIsDecidedByItself(desktop: DesktopSession, keyboard: Keyboard, tmp_path: Path):
	nvda, page = startPage(desktop, tmp_path)
	stepOn(keyboard, desktop, "control+Home", "heading level 1 Keys page")
	# h is NVDA's, and held; Control+L, pressed while it is, has no command in browse mode and reaches
	# Firefox, which shows the page a shortcut it does not reserve before taking it.
	mark = desktop.mark()
	keyboard.down("h")
	expect(desktop, "Field and link heading level 2", since=mark)
	keyboard.press("control+l")
	keyboard.up("h")
	expect(desktop, ADDRESS_BAR + PREFIX_MARK, since=mark)
	waitForCapture(nvda, "off")
	assert page.log() == ["Control+l"]
	quitNVDA(desktop, nvda)


def test_keysFollowTheModeAtOnce(desktop: DesktopSession, tmp_path: Path):
	config = tmp_path / "config"
	config.mkdir()
	browseConfig(config)
	# Bound by the user, as the Input Gestures dialog would: F9 moves to the next heading.
	(config / "gestures.ini").write_text("[browseMode.BrowseModeTreeInterceptor]\n\tnextHeading = kb:f9\n")
	webSession.requireBrowseLibraries()
	webSession.startFirefox(desktop, "keys.html")
	nvda = desktop.startNVDA(config, env=probeHooks(tmp_path))
	nvda.waitForLog("Buffer rendered", BUFFER_BOUND)
	waitForCapture(nvda, "browse", timeout=BUFFER_BOUND)
	time.sleep(SETTLE)
	page = KeysPage()

	step(desktop, "control+Home", "heading level 1 Keys page")
	step(desktop, "F9", "Field and link heading level 2")
	assert page.log() == []

	# The first key after a change of mode goes the new mode's way, sooner than NVDA looks at its
	# focus.
	enterField(desktop, nvda)
	mark = desktop.mark()
	subprocess.run(["xdotool", "key", "Escape", "sleep", "0.03", "key", "h"], check=True)
	expect(desktop, "Browse mode", "no next heading", since=mark)
	assert page.log() == []
	mark = desktop.mark()
	subprocess.run(["xdotool", "key", "Return", "sleep", "0.03", "key", "x"], check=True)
	expect(desktop, "Focus mode", since=mark)
	page.waitForLog(["x"])
	page.waitForField("x")
	step(desktop, "Escape", "Browse mode")
	quitNVDA(desktop, nvda)


def test_pageKeepsTheFocusThroughNVDAKeys(desktop: DesktopSession, tmp_path: Path):
	nvda, page = startPage(desktop, tmp_path)
	settled = page.focusLog()
	noticed: dict[str, list[str]] = {}

	def note(label: str) -> None:
		noticed[label] = page.focusLog()[len(settled) + sum(len(v) for v in noticed.values()) :]

	step(desktop, "control+Home", "heading level 1 Keys page")
	step(desktop, "h", "Field and link heading level 2")
	note("browse mode keys")
	step(desktop, "Insert+t", "Keys page" + PREFIX_MARK)
	step(desktop, "Caps_Lock+t", "Keys page" + PREFIX_MARK)
	step(desktop, "Insert+Tab", "Keys page" + PREFIX_MARK)
	note("NVDA commands")
	# Num Lock is off on the rig's display: the review cursor's current line.
	step(desktop, "KP_Up", "heading level 2 Field and link")
	note("numpad review")
	enterField(desktop, nvda)
	desktop.typeText("ab")
	step(desktop, "Insert+Up", "ab")
	step(desktop, "Escape", "Browse mode")
	note("focus mode")
	assert all(not events for events in noticed.values()), noticed
	quitNVDA(desktop, nvda)


class PopupsPage:
	"""The keys popups page's logs: what its suggestion list and menu did, and what its window and
	document noticed of the focus."""

	def __init__(self) -> None:
		Atspi = self._Atspi = desktopSession._atspi()
		desktop = Atspi.get_desktop(0)
		desktop.clear_cache()
		for index in range(desktop.get_child_count()):
			app = desktop.get_child_at_index(index)
			if app is None or app.get_toolkit_name() != "Gecko":
				continue
			documents = [
				d for d in _matches(Atspi, app, Atspi.Role.DOCUMENT_WEB) if d.get_name() == "Keys popups page"
			]
			if documents:
				break
		else:
			raise AssertionError("no Firefox shows the keys popups page")
		self._popupLog = _byId(documents[0], "popupLog")
		self._focusLog = _byId(documents[0], "focusLog")
		self.popupLog()
		self.focusLog()
		time.sleep(webSession.CACHE_FILL_SECONDS)

	def popupLog(self) -> list[str]:
		return self._Atspi.Text.get_text(self._popupLog, 0, -1).split()

	def focusLog(self) -> list[str]:
		return self._Atspi.Text.get_text(self._focusLog, 0, -1).split()

	def waitForPopupLog(self, expected: list[str], timeout: float = 3) -> None:
		deadline = time.monotonic() + timeout
		while (logged := self.popupLog()) != expected:
			assert time.monotonic() < deadline, f"the page's popups logged {logged}, not {expected}"
			time.sleep(0.05)


#: NVDA commands that read without moving anything: the window's title, the focus, the review cursor's
#: line on the numpad, and the current line, with each NVDA key.
READING_CHORDS = ("Insert+t", "Caps_Lock+t", "Insert+Tab", "KP_Up", "Insert+Up")


def test_popupsStayOpenThroughNVDAKeys(desktop: DesktopSession, tmp_path: Path):
	webSession.requireBrowseLibraries()
	webSession.startFirefox(desktop, "keysPopups.html")
	nvda = desktop.startNVDA(browseConfig(tmp_path), env=probeHooks(tmp_path))
	nvda.waitForLog("Buffer rendered", BUFFER_BOUND)
	waitForCapture(nvda, "browse", timeout=BUFFER_BOUND)
	time.sleep(SETTLE)
	page = PopupsPage()
	settled = page.focusLog()

	step(desktop, "control+Home", "heading level 1 Keys popups page")
	step(desktop, "Tab", "Focus mode")
	desktop.typeText("ap")
	page.waitForPopupLog(["suggestions-open"])
	for chord in READING_CHORDS:
		desktop.press(chord, gap=SETTLE)
	assert page.popupLog() == ["suggestions-open"]
	# Back to browse mode, with the field keeping the focus and its list open, and browse mode's keys.
	step(desktop, "Escape", "Browse mode")
	for chord in ("Down", "Up", "h", "shift+h", *READING_CHORDS):
		desktop.press(chord, gap=SETTLE)
	assert page.popupLog() == ["suggestions-open"]

	# The menu takes the focus as it opens, which closes the list.
	step(desktop, "b", "Actions" + PREFIX_MARK)
	desktop.press("Return", gap=SETTLE)
	page.waitForPopupLog(["suggestions-open", "suggestions-closed", "menu-open"])
	time.sleep(SETTLE)
	for chord in READING_CHORDS:
		desktop.press(chord, gap=SETTLE)
	assert page.popupLog() == ["suggestions-open", "suggestions-closed", "menu-open"]
	assert page.focusLog() == settled, page.focusLog()
	# NVDA gives a menu its Escape, which closes it.
	desktop.press("Escape", gap=SETTLE)
	page.waitForPopupLog(["suggestions-open", "suggestions-closed", "menu-open", "menu-closed"])
	quitNVDA(desktop, nvda)


def test_polishAltGrLettersOnAPage(desktop: DesktopSession, tmp_path: Path):
	subprocess.run(["setxkbmap", "pl"], check=True)
	try:
		nvda, page = startPage(desktop, tmp_path)
		enterField(desktop, nvda)
		mark = desktop.mark()
		desktop.typeText("ął")
		page.waitForField("ął")
		time.sleep(SETTLE)
		spoken = desktop.spokenSince(mark)
		assert spoken.count("ą") == 1 and spoken.count("ł") == 1, spoken
		logged = page.log()
		assert len(logged) == 2, logged
		step(desktop, "Escape", "Browse mode")
		waitForCapture(nvda, "browse")

		mark = desktop.mark()
		trapped = nvda.text("nvda.log").count("Trapped a key in browse mode")
		desktop.typeText("ął")
		time.sleep(SETTLE)
		assert nvda.text("nvda.log").count("Trapped a key in browse mode") == trapped + 2
		assert page.log() == logged
		assert page.field() == "ął"
		# AltGr+a and AltGr+l are not a and l: no annotation or list was looked for.
		assert desktop.spokenSince(mark) == []
		quitNVDA(desktop, nvda)
	finally:
		subprocess.run(["setxkbmap", "us"], check=True)


def test_blockedMainThreadLetsKeysThrough(desktop: DesktopSession, tmp_path: Path):
	nvda, page = startPage(desktop, tmp_path)
	desktop.press("Insert+shift+F11", gap=0)
	nvda.waitForLog("Blocking the main thread", 5)
	blocked = time.monotonic()
	# The key source passes captured keys once the core has been stuck as long as the watchdog takes
	# for a possible freeze.
	time.sleep(0.7)
	logged: list[str] = []
	for key, name in (("h", "h"), ("Down", "ArrowDown"), ("j", "j"), ("space", "Space")):
		pressed = time.monotonic()
		subprocess.run(["xdotool", "key", key], check=True)
		logged.append(name)
		arrived = page.waitForLog(logged, timeout=1)
		assert arrived - pressed < CAPTURE_TIMEOUT + ARRIVAL_SLACK + 0.2, (
			f"{key} came {arrived - pressed:.3f} s late"
		)
	assert time.monotonic() - blocked < BLOCK_SECONDS, "the keys were not all pressed while NVDA was blocked"
	nvda.waitForLog("The main thread is free again", BLOCK_SECONDS + 5)
	time.sleep(SETTLE)
	step(desktop, "control+Home", "heading level 1 Keys page")
	step(desktop, "h", "Field and link heading level 2")
	assert page.log() == logged
	quitNVDA(desktop, nvda)


@pytest.mark.parametrize("mode", ("browse", "focus"))
def test_stoppedNVDALetsTheKeyboardGo(desktop: DesktopSession, tmp_path: Path, mode: str):
	nvda, page = startPage(desktop, tmp_path)
	enterField(desktop, nvda)
	if mode == "browse":
		step(desktop, "Escape", "Browse mode")
		waitForCapture(nvda, "browse")
	# Firefox's focus stays in the field, so keys that reach the page type there. h is captured in
	# either mode, so the X server holds the keyboard on it, with xyz behind it, until the guard lets
	# it go; none of them may be lost.
	os.kill(nvda.process.pid, signal.SIGSTOP)
	try:
		pressed = time.monotonic()
		subprocess.run(["xdotool", "key", "h"], check=True)
		desktop.typeText("xyz")
		page.waitForField("hxyz", timeout=pressed + 3 - time.monotonic())
	finally:
		os.kill(nvda.process.pid, signal.SIGCONT)
	nvda.waitForLog("Keys are read from the X display again", 10)
	if mode == "focus":
		step(desktop, "Escape", "Browse mode")
	waitForCapture(nvda, "browse")
	logged = page.log()
	step(desktop, "shift+h", "Field and link heading level 2")
	assert page.log() == logged
	quitNVDA(desktop, nvda)
