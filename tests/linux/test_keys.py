"""Keys read from the X display: NVDA commands reach NVDA alone, other keys reach the application and
NVDA both, and the key map names keys as NVDA on Windows does.

Every test that starts NVDA runs once with Insert as the NVDA key and the desktop layout, and once
with Caps Lock as the NVDA key and the laptop layout. Run under the rig:

	nix develop . -c tests/linux/rig python3 -m pytest tests/linux/test_keys.py
"""

import json
import os
import signal
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import desktopSession
import harness
import pytest
from desktopSession import DesktopSession
from Xlib import XK, display
from Xlib.ext import xinput
from Xlib.protocol import rq

#: Expected key names, recorded from NVDA's own KeyboardInputGesture fed by the key map.
KEY_NAMES = harness.HERE / "fixtures" / "keyNames"
#: Seconds NVDA has to start and speak the focus.
STARTUP_TIMEOUT = 60.0
#: What the fixture's text field holds at the start, all of it selected once Tab reaches it.
ENTRY_TEXT = "Alice"


def configText(layout: str, nvdaKey: int) -> str:
	return "\n".join(
		(
			"schemaVersion = 25",
			"[general]",
			"\tlanguage = en",
			"\tplayStartAndExitSounds = False",
			"\tshowWelcomeDialogAtStartup = False",
			"[speech]",
			"\tsynth = transcript",
			"[braille]",
			"\tdisplay = noBraille",
			"[keyboard]",
			f"\tkeyboardLayout = {layout}",
			f"\tNVDAModifierKeys = {nvdaKey}",
			"[featureFlag]",
			"\tplayErrorSound = 2",
			"[addonStore]",
			"\tautomaticUpdates = disabled",
			"\tbaseServerURL = http://127.0.0.1:1",
			"",
		),
	)


#: (keyboard layout, NVDAModifierKeys, the NVDA key as xdotool names it).
LAYOUTS = {
	"desktop": ("desktop", 6, "Insert"),
	"laptop": ("laptop", 1, "Caps_Lock"),
}


class KeySession:
	"""The GTK fixture with its text field focused, and NVDA started beside it."""

	def __init__(self, desktop: DesktopSession, tmpPath: Path, layoutName: str, env: dict | None = None):
		self.desktop = desktop
		self.layout, nvdaKey, self.nvda = LAYOUTS[layoutName]
		config = tmpPath / "nvda.ini"
		config.write_text(configText(self.layout, nvdaKey))
		desktop.startApp(desktop.fixture("gtkFixture"), title="Fixture window")
		desktop.startNVDA(config, env=env)
		desktop.expect("Fixture window", since=0, timeout=STARTUP_TIMEOUT)
		mark = desktop.mark()
		desktop.press("Tab", "Tab", "Tab", "Tab")
		desktop.expect("Name: edit\N{HORIZONTAL ELLIPSIS}", since=mark)
		assert entryState() == (ENTRY_TEXT, len(ENTRY_TEXT))

	def chord(self, key: str) -> str:
		return f"{self.nvda}+{key}"

	def quit(self) -> harness.Launch:
		launch = self.desktop.nvda
		launch.process.terminate()
		launch.waitForExit(20)
		return launch


def entryState() -> tuple[str, int]:
	"""The text of the fixture's text field and where its caret is, read through AT-SPI."""
	Atspi = desktopSession._atspi()
	desktop = Atspi.get_desktop(0)
	for appIndex in range(desktop.get_child_count()):
		app = desktop.get_child_at_index(appIndex)
		for index in range(app.get_child_count() if app else 0):
			window = app.get_child_at_index(index)
			if window is not None and window.get_name() == "Fixture window":
				entry = _findEntry(window, Atspi)
				assert entry is not None, "the fixture has no text field"
				return Atspi.Text.get_text(entry, 0, -1), Atspi.Text.get_caret_offset(entry)
	raise AssertionError("no fixture window")


def _findEntry(node, Atspi):
	if node.get_role_name() == "text" and node.get_state_set().contains(Atspi.StateType.EDITABLE):
		return node
	for index in range(node.get_child_count()):
		child = node.get_child_at_index(index)
		if child is not None and (found := _findEntry(child, Atspi)) is not None:
			return found
	return None


def transcript(launch: harness.Launch) -> list[dict]:
	path = launch.directory / "transcript.jsonl"
	return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


@pytest.fixture
def desktop(tmp_path: Path) -> Iterator[DesktopSession]:
	with DesktopSession(tmp_path / "desktop") as session:
		yield session


@pytest.mark.parametrize("layoutName", LAYOUTS)
def test_nvdaChordReachesNVDAAlone(desktop: DesktopSession, tmp_path: Path, layoutName: str):
	keys = KeySession(desktop, tmp_path, layoutName)
	mark = desktop.mark()
	desktop.press(keys.chord("t"))
	desktop.expect("Fixture window", since=mark)
	time.sleep(0.3)
	assert entryState()[0] == ENTRY_TEXT
	# A letter typed after the chord reaches the field as typed: with Caps Lock as the NVDA key, the
	# lock the X server toggled is back as it was.
	desktop.press("a")
	time.sleep(0.3)
	assert entryState()[0] == "a"
	launch = keys.quit()
	launch.assertClean(logNames=("nvda.log",))
	log = launch.text("nvda.log")
	assert f"Input: kb({keys.layout}):NVDA+t" in log
	assert f"Input: kb({keys.layout}):(typing key)" in log
	assert f"Input: kb({keys.layout}):a\n" not in log


class _DeviceFakeInput(rq.Request):
	"""XTEST's FakeInput for an event of a given input device, which python-xlib's leaves out."""

	_request = rq.Struct(
		rq.Card8("opcode"),
		rq.Opcode(2),
		rq.RequestLength(),
		rq.Card8("eventType"),
		rq.Card8("detail"),
		rq.Pad(2),
		rq.Card32("time"),
		rq.Card32("root"),
		rq.Pad(8),
		rq.Int16("x"),
		rq.Int16("y"),
		rq.Pad(7),
		rq.Card8("deviceid"),
	)


#: xdotool's names for the modifiers, as X names their left keys.
MODIFIER_KEYS = {"control": "Control_L", "shift": "Shift_L", "alt": "Alt_L", "super": "Super_L"}
#: Seconds a person holds a key, and leaves between the keys of a chord.
HOLD = 0.1
GAP = 0.03


class Keyboard:
	"""Keys typed on the rig display's own keyboard, as a person's keyboard types them.

	xdotool types through XTEST's device, which NVDA sends its own keys through too, so NVDA can let go
	of a key xdotool holds. A person's keyboard is a device of its own: the X server then drops a
	press NVDA sends of a key that keyboard holds, as a key is held while NVDA decides on it. This
	types on the X server's own keyboard device instead, through XTEST's request for device events.
	"""

	def __init__(self) -> None:
		self._display = display.Display()
		devices = self._display.xinput_query_device(xinput.AllDevices).devices
		self._device = next(
			device.deviceid
			for device in devices
			if device.use == xinput.SlaveKeyboard and "XTEST" not in str(device.name)
		)
		self._firstEvent = self._display.query_extension("XInputExtension").first_event
		self._xtest = self._display.query_extension("XTEST").major_opcode

	def _send(self, key: str, down: bool) -> None:
		keysym = XK.string_to_keysym(MODIFIER_KEYS.get(key, key))
		keycode = self._display.keysym_to_keycode(keysym)
		assert keycode, f"no key on the rig's keyboard is {key}"
		_DeviceFakeInput(
			display=self._display.display,
			opcode=self._xtest,
			# DeviceKeyPress and DeviceKeyRelease, as XInput numbers them.
			eventType=self._firstEvent + (1 if down else 2),
			detail=keycode,
			time=0,
			root=0,
			x=0,
			y=0,
			deviceid=self._device,
		)
		self._display.sync()

	def down(self, key: str) -> None:
		self._send(key, True)
		time.sleep(GAP)

	def up(self, key: str) -> None:
		self._send(key, False)
		time.sleep(GAP)

	def press(self, *chords: str, hold: float = HOLD) -> None:
		"""Press each chord in turn, as xdotool names them: its keys down in order, held, then up."""
		for chord in chords:
			keys = chord.split("+")
			for key in keys:
				self.down(key)
			time.sleep(hold)
			for key in reversed(keys):
				self.up(key)

	def type(self, text: str) -> None:
		self.press(*text)

	def close(self) -> None:
		self._display.close()


@pytest.fixture
def keyboard() -> Iterator[Keyboard]:
	board = Keyboard()
	yield board
	board.close()


def waitForEntry(text: str, timeout: float = 3) -> None:
	deadline = time.monotonic() + timeout
	while (found := entryState()[0]) != text:
		assert time.monotonic() < deadline, f"the field holds {found!r}, not {text!r}"
		time.sleep(0.05)


@pytest.mark.parametrize("layoutName", LAYOUTS)
def test_nvdaChordWithoutACommandReachesTheApplication(
	desktop: DesktopSession, keyboard: Keyboard, tmp_path: Path, layoutName: str
):
	keys = KeySession(desktop, tmp_path, layoutName)
	# NVDA has no command on z: the field has the z, without the NVDA key, as on Windows. Caps Lock
	# held down is the Lock modifier to the X server, which the key comes with, so with Caps Lock as the
	# NVDA key it may come in capitals.
	keyboard.press(keys.chord("z"))
	waitForEntry("z" if layoutName == "desktop" else "Z")
	# NVDA's own commands are still NVDA's alone, the next one with the NVDA key still held too.
	mark = desktop.mark()
	keyboard.press(keys.chord("t"))
	desktop.expect("Fixture window", since=mark)
	mark = desktop.mark()
	keyboard.down(keys.nvda)
	keyboard.press("y")
	time.sleep(0.3)
	keyboard.press("t")
	time.sleep(0.3)
	keyboard.up(keys.nvda)
	desktop.expect("Fixture window", since=mark)
	time.sleep(0.3)
	assert entryState()[0].lower() == "zy"
	assert not desktopSession.capsLockIsOn()
	# The keys after a chord are grabbed again as they were: NVDA+t is NVDA's, and a letter typing.
	mark = desktop.mark()
	keyboard.press(keys.chord("t"))
	desktop.expect("Fixture window", since=mark)
	keyboard.press("a")
	time.sleep(0.3)
	assert entryState()[0].lower() == "zya"
	launch = keys.quit()
	launch.assertClean(logNames=("nvda.log",))
	assert f"Input: kb({keys.layout}):NVDA+z" in launch.text("nvda.log")


def passedTwice(keyboard: Keyboard, key: str) -> None:
	"""Press key twice, as quickly as a person does to have NVDA pass it on."""
	keyboard.press(key)
	time.sleep(0.15)
	keyboard.press(key)
	time.sleep(0.3)


def test_insertPassedThrough(desktop: DesktopSession, keyboard: Keyboard, tmp_path: Path):
	keys = KeySession(desktop, tmp_path, "desktop")
	keyboard.press("Home")
	mark = desktop.mark()
	keyboard.press(keys.chord("F2"))
	desktop.expect("Pass next key through", since=mark)
	# Insert reaches the field, which then types over its text.
	keyboard.press("Insert")
	keyboard.type("x")
	time.sleep(0.3)
	assert entryState() == ("xlice", 1)
	# The NVDA key pressed twice quickly reaches the field too, which types between letters again.
	passedTwice(keyboard, "Insert")
	keyboard.type("y")
	time.sleep(0.3)
	assert entryState() == ("xylice", 2)
	# Passed through, a chord with the NVDA key reaches the field whole: Insert, then the letter.
	mark = desktop.mark()
	keyboard.press(keys.chord("F2"))
	desktop.expect("Pass next key through", since=mark)
	keyboard.press(keys.chord("t"))
	time.sleep(0.3)
	assert entryState() == ("xytice", 3)
	assert "Fixture window" not in desktop.spokenSince(mark)
	keys.quit().assertClean(logNames=("nvda.log",))


def test_capsLockPassedThrough(desktop: DesktopSession, keyboard: Keyboard, tmp_path: Path):
	keys = KeySession(desktop, tmp_path, "laptop")
	keyboard.press("Home")
	mark = desktop.mark()
	keyboard.press(keys.chord("F2"))
	desktop.expect("Pass next key through", since=mark)
	keyboard.press("Caps_Lock")
	time.sleep(0.3)
	assert desktopSession.capsLockIsOn()
	keyboard.type("x")
	time.sleep(0.3)
	assert entryState() == ("XAlice", 1)
	mark = desktop.mark()
	passedTwice(keyboard, "Caps_Lock")
	desktop.expect("caps lock off", since=mark)
	assert not desktopSession.capsLockIsOn()
	passedTwice(keyboard, "Caps_Lock")
	desktop.expect("caps lock off", "caps lock on", since=mark)
	assert desktopSession.capsLockIsOn()
	passedTwice(keyboard, "Caps_Lock")
	assert not desktopSession.capsLockIsOn()
	# One press used as the NVDA key leaves the lock as it was.
	keyboard.press(keys.chord("t"))
	keyboard.type("y")
	time.sleep(0.3)
	assert entryState() == ("XyAlice", 2)
	keys.quit().assertClean(logNames=("nvda.log",))


#: A global plugin for these tests only: a command that blocks NVDA's main thread.
BLOCK_PLUGIN = """
import time

import globalPluginHandler
from logHandler import log
from scriptHandler import script


class GlobalPlugin(globalPluginHandler.GlobalPlugin):
	@script(gesture="kb:NVDA+shift+f11")
	def script_blockMainThread(self, gesture):
		log.info("Blocking the main thread")
		time.sleep(3)
		log.info("The main thread is free again")
"""


def blockHooks(directory: Path) -> dict[str, str]:
	hooks = directory / "hooks"
	(hooks / "globalPlugins").mkdir(parents=True)
	(hooks / "synthDrivers").symlink_to(harness.HERE / "synthDrivers")
	(hooks / "globalPlugins" / "blockProbe.py").write_text(BLOCK_PLUGIN)
	return {"LINDA_TEST_HOOKS_DIR": str(hooks)}


def test_blockedMainThreadLeavesTheNVDAKeysWorking(
	desktop: DesktopSession, keyboard: Keyboard, tmp_path: Path
):
	keys = KeySession(desktop, tmp_path, "desktop", env=blockHooks(tmp_path))
	keyboard.press(keys.chord("shift+F11"))
	desktop.nvda.waitForLog("Blocking the main thread", 5)
	blocked = time.monotonic()
	# The NVDA key's grabs are answered off the main thread, so they hold the keyboard no longer while
	# it is blocked: the chord is NVDA's, and the letters after it reach the field at once.
	mark = desktop.mark()
	keyboard.press(keys.chord("t"))
	keyboard.type("xy")
	waitForEntry("xy", timeout=1)
	assert time.monotonic() - blocked < 2.5, "the keys were not all typed while NVDA was blocked"
	desktop.nvda.waitForLog("The main thread is free again", 5)
	desktop.expect("Fixture window", since=mark)
	keys.quit().assertClean(logNames=("nvda.log",))


@pytest.mark.parametrize("chord", (False, True), ids=("nvdaKey", "chordAfterAPassedKey"))
def test_stoppedNVDALetsTheKeyboardGo(
	desktop: DesktopSession, keyboard: Keyboard, tmp_path: Path, chord: bool
):
	keys = KeySession(desktop, tmp_path, "desktop")
	launch = desktop.nvda
	if chord:
		# z has no command, so the field has it, and every key is grabbed while Insert is still held.
		keyboard.down("Insert")
		keyboard.press("z")
		waitForEntry("z")
		typed = "zxy"
	else:
		typed = "xy"
	os.kill(launch.process.pid, signal.SIGSTOP)
	try:
		pressed = time.monotonic()
		if chord:
			# Held by the X server for NVDA's answer, with y behind it, until the guard lets them go.
			keyboard.type("xy")
		else:
			# Insert held for NVDA's answer, with x and y behind it; the field gets all three, Insert
			# turning on typing over.
			keyboard.press("Insert")
			keyboard.type("xy")
		waitForEntry(typed, timeout=pressed + 3 - time.monotonic())
	finally:
		if chord:
			keyboard.up("Insert")
		os.kill(launch.process.pid, signal.SIGCONT)
	launch.waitForLog("Keys are read from the X display again", 10)
	time.sleep(0.5)
	mark = desktop.mark()
	keyboard.press(keys.chord("t"))
	desktop.expect("Fixture window", since=mark)
	time.sleep(0.3)
	assert entryState()[0] == typed
	keys.quit().assertClean(logNames=("nvda.log",))


@pytest.mark.parametrize("layoutName", LAYOUTS)
def test_numpadReviewKeyLeavesTheCaret(desktop: DesktopSession, tmp_path: Path, layoutName: str):
	keys = KeySession(desktop, tmp_path, layoutName)
	desktop.press("End")
	before = entryState()
	mark = desktop.mark()
	# Num Lock is off on the rig's display, so this is the review cursor's current line.
	desktop.press("KP_Up")
	desktop.expect(ENTRY_TEXT, since=mark)
	time.sleep(0.3)
	assert entryState() == before
	keys.quit().assertClean(logNames=("nvda.log",))


@pytest.mark.parametrize("layoutName", LAYOUTS)
def test_inputHelpHoldsTheKeyboard(desktop: DesktopSession, tmp_path: Path, layoutName: str):
	keys = KeySession(desktop, tmp_path, layoutName)
	mark = desktop.mark()
	desktop.press(keys.chord("1"))
	on = "input help on\N{HORIZONTAL ELLIPSIS}"
	desktop.expect(on, since=mark)
	desktop.press("t")
	desktop.expect(on, "t\N{HORIZONTAL ELLIPSIS}", since=mark)
	desktop.press(keys.chord("1"))
	desktop.expect(on, "t\N{HORIZONTAL ELLIPSIS}", "input help off", since=mark)
	time.sleep(0.3)
	assert entryState()[0] == ENTRY_TEXT
	desktop.press("b")
	time.sleep(0.3)
	assert entryState()[0] == "b", "the keyboard was not let go after input help"
	keys.quit().assertClean(logNames=("nvda.log",))


@pytest.mark.parametrize("layoutName", LAYOUTS)
def test_controlCancelsReading(desktop: DesktopSession, tmp_path: Path, layoutName: str):
	keys = KeySession(
		desktop,
		tmp_path,
		layoutName,
		env={"LINDA_TRANSCRIPT_MODE": "timed", "LINDA_TRANSCRIPT_MS_PER_CHAR": "10"},
	)
	launch = desktop.nvda
	mark = desktop.mark()
	desktop.press(keys.chord("b"), gap=0)
	deadline = time.monotonic() + 10
	while len(desktop.spokenSince(mark)) == 0:
		assert time.monotonic() < deadline, "reading the window did not start"
		time.sleep(0.01)
	pressed = time.monotonic()
	subprocess.run(["xdotool", "key", "Control_L"], check=True)
	time.sleep(1.5)
	records = [r for r in transcript(launch) if r["t"] >= pressed]
	cancels = [r for r in records if r["kind"] == "cancel"]
	assert cancels, f"no cancel after Control: {records}"
	assert cancels[0]["t"] - pressed < 0.1, f"the cancel came {cancels[0]['t'] - pressed:.3f} s after Control"
	assert not [r for r in records if r["kind"] == "speak" and r["t"] > cancels[0]["t"]]
	keys.quit().assertClean(logNames=("nvda.log",))


def test_polishAltGrLettersAreTyping(desktop: DesktopSession, tmp_path: Path):
	subprocess.run(["setxkbmap", "pl"], check=True)
	try:
		keys = KeySession(desktop, tmp_path, "desktop")
		desktop.typeText("ął")
		time.sleep(0.3)
		assert entryState()[0] == "ął"
		launch = keys.quit()
		launch.assertClean(logNames=("nvda.log",))
		log = launch.text("nvda.log")
		assert log.count("Input: kb(desktop):(typing key)") >= 2
		assert "alt+a" not in log and "alt+l" not in log
	finally:
		subprocess.run(["setxkbmap", "us"], check=True)


#: The key map fed into NVDA's KeyboardInputGesture, as the recorded names were made.
NAMES_CHILD = r"""
import NVDAHelper  # noqa: F401
import xkbcommon.xkb as xkb

import inputCore
import keyboardHandler
import languageHandler
import speech
import types
import winUser
from linuxPlatform import inputHook
from linuxPlatform.keys import keyMap

languageHandler.setLanguage("en")
inputCore.initialize()
# Speech is not running here; the shift keys ask whether it is paused.
speech.getState = lambda: types.SimpleNamespace(isPaused=False)

context = xkb.Context()
EXTRA = {224, 225, 238, 237, 190, 85, 117}
ALL = sorted(set(keyMap.FIXED) | set(keyMap.NUMPAD) | set(keyMap.LAYOUT) | EXTRA)
result = {}
for layout in LAYOUTS:
	keymap = context.keymap_new_from_names(rules="evdev", model="pc105", layout=layout)
	state = keymap.state_new()
	t = keyMap.Translator(keymap, state)
	numLock = {"on": False}
	inputHook.mapVirtualKeyToChar = t.mapVirtualKeyToChar
	inputHook.vkKeyScan = t.vkKeyScan
	inputHook.getKeyState = lambda vk: int(numLock["on"]) if vk == 0x90 else 0
	winUser.getKeyNameText = lambda scan, ext: "keyNameText_%02x%s" % (scan, "e" if ext else "")

	def describe(keycode, modifiers=()):
		vk, scan, ext = t.translate(keycode)
		return keyboardHandler.KeyboardInputGesture(set(modifiers), vk, scan, ext), (vk, scan, ext)

	lines = []
	for lockOn in (False, True):
		if lockOn:
			state.update_key(77, xkb.XKB_KEY_DOWN)
			state.update_key(77, xkb.XKB_KEY_UP)
			numLock["on"] = True
		for evdev in ALL:
			if lockOn and evdev not in keyMap.NUMPAD and evdev not in (55, 74, 78, 98):
				continue
			keycode = evdev + 8
			sym = xkb.keysym_get_name(state.key_get_one_sym(keycode)) if keymap.key_get_name(keycode) else "-"
			g, hook = describe(keycode)
			ident = g.identifiers[1]
			lines.append(
				f"evdev={evdev} xkeysym={sym} numlock={'on' if lockOn else 'off'} hook=(vk={hook[0]:#04x}, scan={hook[1]:#04x}, "
				f"ext={hook[2]}) -> {ident} character={g.isCharacter} command={g.shouldReportAsCommand} "
				f"speech={g.speechEffectWhenExecuted}"
			)
		if lockOn:
			state.update_key(77, xkb.XKB_KEY_DOWN)
			state.update_key(77, xkb.XKB_KEY_UP)
			numLock["on"] = False
	combos = {
		"insert+t (NVDA+t)": ({(0x2D, True)}, 20 + 8),
		"control+alt+n": ({(0xA2, False), (0xA4, False)}, 49 + 8),
		"altgr+a as Windows reports it (fake left control and right alt)": ({(0xA2, False), (0xA5, True)}, 30 + 8),
		"right alt+a without the fake control": ({(0xA5, True)}, 30 + 8),
		"shift+a": ({(0xA0, False)}, 30 + 8),
		"numpad insert+downArrow (NVDA+downArrow)": ({(0x2D, False)}, 108 + 8),
		"capsLock+t": ({(0x14, False)}, 20 + 8),
		"windows+d": ({(0x5B, True)}, 32 + 8),
	}
	for label, (mods, keycode) in combos.items():
		g, hook = describe(keycode, mods)
		lines.append(f"combo {label}: -> {g.identifiers[1]} display={g.displayName!r} character={g.isCharacter} speech={g.speechEffectWhenExecuted}")
	for ch in "aA1!ąĄł;'[":
		try:
			lines.append(f"fromName {ch!r}: {keyboardHandler.KeyboardInputGesture.fromName(ch).identifiers[1]}")
		except LookupError as e:
			lines.append(f"fromName {ch!r}: {type(e).__name__}: {e}")
	result[layout] = lines
"""


def test_keyNamesMatchTheRecordedOnes(tmp_path: Path):
	layouts = ("us", "pl")
	done = harness.runChild(tmp_path, f"LAYOUTS = {layouts!r}\n" + NAMES_CHILD)
	assert done.error is None, done.error
	done.assertClean()
	for layout in layouts:
		expected = (KEY_NAMES / f"names-{layout}.txt").read_text().splitlines()
		assert done.value[layout] == expected
