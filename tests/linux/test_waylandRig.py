"""The rig's Wayland mode: headless Mutter, its accessibility manager, and keys pressed on its virtual
keyboard, measured the way a screen reader would use them, through libatspi's device as Orca does.

Under the X rig the one test here runs this file again in a rig of its own in Wayland mode.

nix develop . -c tests/linux/rig python3 -m pytest tests/linux/test_waylandRig.py
"""

import os
import queue
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import harness
import pytest
import waylandKeyboard

WAYLAND = os.environ.get("XDG_SESSION_TYPE") == "wayland"
MANAGER = "org.freedesktop.a11y.Manager"
MANAGER_PATH = "/org/freedesktop/a11y/Manager"
KEYBOARD_MONITOR = "org.freedesktop.a11y.KeyboardMonitor"
POINTER_LOCATOR = "org.freedesktop.a11y.PointerLocator"
#: The name Mutter, KWin and COSMIC let call their keyboard interface, the one libatspi's device takes
#: for the application id Orca gives it.
ORCA_NAME = "org.gnome.Orca.KeyboardMonitor"
XK_INSERT = 0xFF63
XK_H = 0x68


if not WAYLAND:

	def test_waylandMode(tmp_path: Path) -> None:
		env = dict(os.environ, LINDA_RIG_SESSION="wayland")
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
			check=False,
		)
		assert ran.returncode == 0, ran.stdout[-6000:] + ran.stderr[-3000:]

else:
	import gi

	gi.require_version("Atspi", "2.0")
	from gi.repository import Atspi, Gio, GLib

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

	def _press(probe: Probe, reader: Reader, *chords: str) -> tuple[list[tuple[bool, int, str]], list[str]]:
		"""What the reader heard and the probe was given while chords were pressed."""
		reader.heard.clear()
		probe.drain()
		waylandKeyboard.press(*chords, gap=0.05)
		_pump(0.4)
		return list(reader.heard), [line for line in probe.drain() if not line.startswith("text ")]

	def test_compositorOffersTheAccessibilityManager() -> None:
		assert os.environ["WAYLAND_DISPLAY"] == "wayland-rig"
		if os.environ.get("LINDA_RIG_XWAYLAND") != "1":
			assert "DISPLAY" not in os.environ
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
		assert window.x == 40 and window.y > 60 and window.width > 0

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
				assert "org.freedesktop.DBus.Error.AccessDenied: Access denied" in refused.value.message, (
					method
				)
		finally:
			stranger.close_sync(None)

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
			probe.drain()
			waylandKeyboard.press("Insert", "Insert", gap=0.05)
			_pump(0.6)
			assert [line for line in probe.drain() if not line.startswith("text ")] == [
				"pressed Insert",
				"released Insert",
			]
		finally:
			reader.device.unmap_keysym_modifier(XK_INSERT)
			_pump(0.2)

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
