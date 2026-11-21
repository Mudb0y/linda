"""The rig's Wayland sessions: headless Mutter and headless sway, keys pressed in the pl layout on
their virtual keyboards, and, under Mutter, its accessibility manager, measured the way a screen
reader would use it, through libatspi's device as Orca does.

Under the X rig the one test for it runs this file again in a rig of each compositor;
tests/linux/wayland-gate runs it in each, with and without Xwayland:

nix develop . -c tests/linux/rig python3 -m pytest tests/linux/test_waylandRig.py
"""

import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import gi
import harness
import pytest
import waylandKeyboard
import waylandKeymapDump

gi.require_version("Atspi", "2.0")
from gi.repository import Atspi, Gio, GLib

MANAGER = "org.freedesktop.a11y.Manager"
MANAGER_PATH = "/org/freedesktop/a11y/Manager"
KEYBOARD_MONITOR = "org.freedesktop.a11y.KeyboardMonitor"
POINTER_LOCATOR = "org.freedesktop.a11y.PointerLocator"
#: The name Mutter, KWin and COSMIC let call their keyboard interface, the one libatspi's device takes
#: for the application id Orca gives it.
ORCA_NAME = "org.gnome.Orca.KeyboardMonitor"
XK_INSERT = 0xFF63
XK_H = 0x68
KEYMAP_DUMP = harness.HERE / "waylandKeymapDump.py"

wayland = pytest.mark.rigSession("wayland")
mutterOnly = pytest.mark.rigSession("mutter")


@pytest.mark.rigSession("x11")
@pytest.mark.parametrize("compositor", ["mutter", "sway"])
def test_waylandSessions(tmp_path: Path, compositor: str) -> None:
	# The X session's choices, which a Wayland rig refuses; many tests' usage lines set LINDA_RIG_WM.
	env = {
		name: value
		for name, value in os.environ.items()
		if not name.startswith(("LINDA_RIG_SESSION", "LINDA_RIG_WM"))
	}
	env.update(LINDA_RIG_COMPOSITOR=compositor)
	ran = subprocess.run(
		[
			str(harness.HERE / "rig"),
			"--bind",
			str(tmp_path),
			sys.executable,
			"-m",
			"pytest",
			"-q",
			"-p",
			"no:cacheprovider",
			f"--basetemp={tmp_path / 'pt'}",
			__file__,
		],
		env=env,
		capture_output=True,
		text=True,
		timeout=300,
		check=False,
	)
	assert ran.returncode == 0, ran.stdout[-6000:] + ran.stderr[-3000:]


def test_markerCheckFindsWhatWasTyped() -> None:
	texts = {"nvda.log": "DEBUG - typed\nk\nq\nz\nm\nx\nj\nw\nv\n", "keyd": "nothing here"}
	assert harness.textsHoldingMarker("KQZMXJWV", texts) == ["nvda.log"]
	assert harness.textsHoldingMarker("kqzmxjwv", {"a": "x KQZMXJWV y"}) == ["a"]
	harness.assertMarkerAbsent("kqzmxjwv", {"keyd": "nothing here"})
	with pytest.raises(AssertionError, match="written in nvda.log$"):
		harness.assertMarkerAbsent("kqzmxjwv", texts)
	with pytest.raises(ValueError):
		harness.textsHoldingMarker("short", texts)
	with pytest.raises(ValueError):
		harness.textsHoldingMarker("kqzmmxjwv", texts)


@pytest.mark.parametrize(
	"line",
	[
		# NVDA's own line for each gesture, under the record header whose thread number ends in a digit.
		"IO - inputCore.InputManager.executeGesture (12:00:00.000) - MainThread (1):\nInput: kb(desktop):{}",
		"DEBUG - key pressed {}",
		"key 107 {}",
		"key down {0}\nkey up {0}",
		"Input: kb(desktop):shift+{}",
	],
)
def test_markerCheckFindsAKeyPerLine(line: str) -> None:
	marker = "kqzmxjwv"
	log = (
		"INFO - started\n" + "".join(line.format(character) + "\n" for character in marker) + "INFO - done\n"
	)
	assert harness.textsHoldingMarker(marker, {"nvda.log": log, "keyd": "key 30 a\nkey 48 b\n"}) == [
		"nvda.log"
	]


def _pump(seconds: float) -> None:
	context = GLib.MainContext.default()
	end = time.monotonic() + seconds
	while time.monotonic() < end:
		while context.iteration(False):
			pass
		time.sleep(0.005)


def _newConnection() -> "Gio.DBusConnection":
	address = Gio.dbus_address_get_for_bus_sync(Gio.BusType.SESSION, None)
	return Gio.DBusConnection.new_for_address_sync(
		address,
		Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
		None,
		None,
	)


def _call(connection, interface: str, method: str, args=None, reply: str | None = None):
	return connection.call_sync(
		MANAGER,
		MANAGER_PATH,
		interface,
		method,
		args,
		GLib.VariantType(reply) if reply else None,
		Gio.DBusCallFlags.NONE,
		5000,
		None,
	)


def _find(node, predicate, depth: int = 0):
	"""The first node under node that predicate accepts; GTK 4 has no Collection interface."""
	if predicate(node):
		return node
	if depth < 20:
		for index in range(node.get_child_count()):
			found = _find(node.get_child_at_index(index), predicate, depth + 1)
			if found is not None:
				return found
	return None


class Probe:
	"""The key probe fixture and what it printed."""

	def __init__(self) -> None:
		self.process = subprocess.Popen(
			[sys.executable, str(harness.HERE / "fixtures" / "apps" / "waylandKeyProbe.py")],
			stdout=subprocess.PIPE,
			text=True,
		)
		self._lines: queue.Queue[str] = queue.Queue()
		threading.Thread(target=self._read, daemon=True).start()
		self.backend = self.waitFor(lambda line: line.startswith("backend ")).split()[1]
		self.waitFor(lambda line: line == "active")

	def _read(self) -> None:
		for line in self.process.stdout:
			self._lines.put(line.rstrip("\n"))

	def waitFor(self, predicate, timeout: float = 10) -> str:
		end = time.monotonic() + timeout
		while time.monotonic() < end:
			try:
				line = self._lines.get(timeout=0.1)
			except queue.Empty:
				continue
			if predicate(line):
				return line
		raise AssertionError("the key probe never printed what was waited for")

	def drain(self) -> list[str]:
		lines = []
		while True:
			try:
				lines.append(self._lines.get_nowait())
			except queue.Empty:
				return lines

	def close(self) -> None:
		self.process.terminate()
		self.process.wait(5)


@pytest.fixture(scope="module")
def probe() -> Iterator[Probe]:
	p = Probe()
	yield p
	p.close()


class Reader:
	"""libatspi's device for Orca's application id, and the keys it heard."""

	def __init__(self) -> None:
		self.device = Atspi.Device.new_full("org.gnome.Orca")
		self.heard: list[tuple[bool, int, str]] = []
		self.device.connect(
			"key-pressed", lambda _d, _c, keysym, _s, text: self.heard.append((True, keysym, text))
		)
		self.device.connect(
			"key-released", lambda _d, _c, keysym, _s, text: self.heard.append((False, keysym, text))
		)


@pytest.fixture(scope="module")
def reader(probe: Probe) -> Reader:
	return Reader()


def _given(probe: Probe, *chords: str) -> tuple[list[str], str | None]:
	"""The keys the probe was given while chords were pressed, and its text after them, if it changed."""
	probe.drain()
	waylandKeyboard.press(*chords, gap=0.05)
	_pump(0.4)
	lines = probe.drain()
	texts = [line.removeprefix("text ") for line in lines if line.startswith("text ")]
	return [line for line in lines if not line.startswith("text ")], (texts[-1] if texts else None)


def _press(probe: Probe, reader: Reader, *chords: str) -> tuple[list[tuple[bool, int, str]], list[str]]:
	"""What the reader heard and the probe was given while chords were pressed."""
	reader.heard.clear()
	given, _text = _given(probe, *chords)
	return list(reader.heard), given


@wayland
def test_sessionIsTheCompositorsAlone() -> None:
	"""The command has the compositor's socket and no X display but the compositor's own Xwayland's, when
	that was asked for: a test that passed through Xvfb would prove nothing about Wayland."""
	runtime = Path(os.environ["XDG_RUNTIME_DIR"])
	assert (runtime / os.environ["WAYLAND_DISPLAY"]).is_socket()
	assert os.environ["XDG_SESSION_TYPE"] == "wayland"
	names = set()
	for entry in Path("/proc").iterdir():
		try:
			names.add((entry / "comm").read_text(encoding="utf-8").strip())
		except OSError:
			pass
	# Nix's wrapper makes Mutter .mutter-wrapped.
	assert "Xvfb" not in names and any(harness.rigSession() in name for name in names), names
	if harness.rigHasXwayland():
		assert re.fullmatch(r":\d+", os.environ["DISPLAY"]) and os.environ["DISPLAY"] != ":99"
		assert Path("/tmp/.X11-unix", "X" + os.environ["DISPLAY"][1:]).is_socket()
	else:
		assert "DISPLAY" not in os.environ and "XAUTHORITY" not in os.environ
		assert list(Path("/tmp/.X11-unix").iterdir()) == []


@pytest.mark.rigSession("sway")
def test_swaymsgAnswers() -> None:
	version = json.loads(
		subprocess.run(["swaymsg", "-t", "get_version"], capture_output=True, text=True, check=True).stdout
	)
	assert version["variant"] == "sway" and version["human_readable"].startswith("1.12")
	outputs = json.loads(
		subprocess.run(["swaymsg", "-t", "get_outputs"], capture_output=True, text=True, check=True).stdout
	)
	assert [(o["name"], o["rect"]["width"], o["rect"]["height"]) for o in outputs] == [
		("HEADLESS-1", 1280, 1024)
	]


@wayland
def test_compositorGivesClientsThePlKeymap() -> None:
	dumped = subprocess.run([sys.executable, str(KEYMAP_DUMP)], capture_output=True, text=True, check=True)
	lines = dumped.stdout.splitlines()
	assert lines[0] == "layouts\tPolish"
	rows = {tuple(line.split("\t")[:2]): line.split("\t")[2:] for line in lines[1:]}
	assert rows[("30", "0")] == ["a", "A", "aogonek", "Aogonek"]
	assert rows[("110", "0")] == ["Insert"]
	# Every code keyMap.py names that the keymap gives symbols, and no other.
	codes = {int(evdev) for evdev, _layout in rows}
	assert codes <= set(waylandKeymapDump.namedCodes(waylandKeymapDump.KEY_MAP))
	assert len(codes) > 120


@wayland
def test_gtk4IsNativeAndAnswersOnlyWindowCoordinates(probe: Probe) -> None:
	assert probe.backend == "GdkWaylandDisplay"
	desktop = Atspi.get_desktop(0)
	entry = None
	for index in range(desktop.get_child_count()):
		app = desktop.get_child_at_index(index)
		frame = app.get_child_at_index(0) if app and app.get_child_count() else None
		if frame is not None and frame.get_name() == "Key probe":
			entry = _find(frame, lambda node: node.get_role() in (Atspi.Role.ENTRY, Atspi.Role.TEXT))
	assert entry is not None, "the key probe's entry is not on the accessibility bus"
	screen = entry.get_extents(Atspi.CoordType.SCREEN)
	window = entry.get_extents(Atspi.CoordType.WINDOW)
	# A Wayland client does not know where its window is; GTK 4 says so with zero.
	assert (screen.x, screen.y) == (0, 0)
	# Below the title bar GTK draws under Mutter; sway tiles the window, and GTK draws none there.
	assert window.x == 40 and window.y >= 60 and window.width > 0


@wayland
def test_injectorTypesInThePlLayout(probe: Probe) -> None:
	given, text = _given(probe, "a")
	assert given == ["pressed a", "released a"]
	assert text is not None and text.endswith("a")
	given, text = _given(probe, "shift+a")
	assert given == ["pressed Shift_L", "pressed A", "released A", "released Shift_L"]
	assert text is not None and text.endswith("aA")
	given, text = _given(probe, "alt_r+a")
	assert given == [
		"pressed ISO_Level3_Shift",
		"pressed aogonek",
		"released aogonek",
		"released ISO_Level3_Shift",
	]
	assert text is not None and text.endswith("aAą")
	given, text = _given(probe, "alt_r+shift+l")
	assert "pressed Lstroke" in given
	assert text is not None and text.endswith("aAąŁ")


@wayland
def test_injectorKeepsTheLocksState(probe: Probe) -> None:
	assert waylandKeyboard.lights() == {"caps": False, "num": False, "scroll": False}
	try:
		_given(probe, "caps_lock")
		assert waylandKeyboard.lights()["caps"]
		given, text = _given(probe, "z")
		assert given == ["pressed Z", "released Z"]
		assert text is not None and text.endswith("Z")
	finally:
		if waylandKeyboard.lights()["caps"]:
			_given(probe, "caps_lock")
	assert not waylandKeyboard.lights()["caps"]
	given, _text = _given(probe, "z")
	assert given == ["pressed z", "released z"]


@mutterOnly
def test_compositorOffersTheAccessibilityManager() -> None:
	bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
	(owned,) = bus.call_sync(
		"org.freedesktop.DBus",
		"/org/freedesktop/DBus",
		"org.freedesktop.DBus",
		"NameHasOwner",
		GLib.Variant("(s)", (MANAGER,)),
		GLib.VariantType("(b)"),
		Gio.DBusCallFlags.NONE,
		5000,
		None,
	).unpack()
	assert owned


@mutterOnly
def test_callerWithoutOrcasNameIsRefused() -> None:
	stranger = _newConnection()
	try:
		for interface, method, args in (
			(KEYBOARD_MONITOR, "WatchKeyboard", None),
			(KEYBOARD_MONITOR, "GrabKeyboard", None),
			(KEYBOARD_MONITOR, "SetKeyGrabs", GLib.Variant("(aua(uu))", ([XK_INSERT], []))),
			(POINTER_LOCATOR, "QueryPointer", None),
		):
			with pytest.raises(GLib.Error) as refused:
				_call(stranger, interface, method, args)
			assert "org.freedesktop.DBus.Error.AccessDenied: Access denied" in refused.value.message, method
	finally:
		stranger.close_sync(None)


@mutterOnly
def test_deviceTakesOrcasNameAndWatches(probe: Probe, reader: Reader) -> None:
	assert type(reader.device).__name__ == "DeviceA11yManager"
	bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
	(owner,) = bus.call_sync(
		"org.freedesktop.DBus",
		"/org/freedesktop/DBus",
		"org.freedesktop.DBus",
		"GetNameOwner",
		GLib.Variant("(s)", (ORCA_NAME,)),
		GLib.VariantType("(s)"),
		Gio.DBusCallFlags.NONE,
		5000,
		None,
	).unpack()
	assert owner == bus.get_unique_name()
	heard, given = _press(probe, reader, "a")
	assert heard == [(True, ord("a"), "a"), (False, ord("a"), "a")]
	assert given == ["pressed a", "released a"]


@mutterOnly
def test_modifierGrabKeepsItsKeysFromTheApplication(probe: Probe, reader: Reader) -> None:
	reader.device.map_keysym_modifier(XK_INSERT)
	try:
		_pump(0.2)
		heard, given = _press(probe, reader, "Insert+t")
		assert [keysym for _pressed, keysym, _text in heard] == [XK_INSERT, ord("t"), ord("t"), XK_INSERT]
		assert given == []
		# A lone press is swallowed; a second one soon after reaches the application, so that a
		# double press of Caps Lock can still lock it.
		time.sleep(0.6)
		heard, given = _press(probe, reader, "Insert")
		assert given == []
		time.sleep(0.6)
		reader.heard.clear()
		given, _text = _given(probe, "Insert", "Insert")
		assert given == ["pressed Insert", "released Insert"]
	finally:
		reader.device.unmap_keysym_modifier(XK_INSERT)
		_pump(0.2)


@mutterOnly
def test_keystrokeGrab(probe: Probe, reader: Reader) -> None:
	definition = Atspi.KeyDefinition()
	definition.keycode = waylandKeyboard.code("h") + 8
	definition.keysym = XK_H
	definition.modifiers = 0
	# With a Python callback remove_key_grab crashes: libatspi hands the destroy notify the callback
	# instead of its data. Orca passes none either.
	grab = reader.device.add_key_grab(definition, None)
	try:
		_pump(0.2)
		heard, given = _press(probe, reader, "h")
		assert len(heard) == 2 and given == []
		heard, given = _press(probe, reader, "b")
		assert given == ["pressed b", "released b"]
	finally:
		reader.device.remove_key_grab(grab)
		_pump(0.2)
	heard, given = _press(probe, reader, "h")
	assert given == ["pressed h", "released h"]


@mutterOnly
def test_wholeKeyboardGrab(probe: Probe, reader: Reader) -> None:
	reader.device.grab_keyboard()
	try:
		_pump(0.2)
		heard, given = _press(probe, reader, "b", "ctrl+a")
		assert len(heard) == 6 and given == []
	finally:
		reader.device.ungrab_keyboard()
		_pump(0.2)
	heard, given = _press(probe, reader, "b")
	assert given == ["pressed b", "released b"]


@mutterOnly
def test_pointerLocatorNamesTheGtk4Window(probe: Probe, reader: Reader) -> None:
	bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
	# Mutter opens the window in the middle of its monitor; the pointer starts in the top left.
	waylandKeyboard.moveBy(640, 512)
	_pump(0.3)
	_call(bus, POINTER_LOCATOR, "QueryPointer", None, "(a{sv}dd)")
	# Mutter answers the first query after the pointer enters a window as before it did.
	waylandKeyboard.moveBy(1, 1)
	_pump(0.3)
	details, x, y = _call(bus, POINTER_LOCATOR, "QueryPointer", None, "(a{sv}dd)").unpack()
	assert details.get("toplevel-object-path", "").startswith("/org/linda/WaylandKeyProbe/")
	assert 0 < x < 640 and 0 < y < 512
