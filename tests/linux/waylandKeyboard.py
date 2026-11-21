"""The keyboard and pointer of the rig's Wayland session.

Neither headless compositor reads an input device, so the rig starts one server with
`waylandKeyboard.py serve SOCKET` and keeps it for the whole run: the seat has a keyboard only while a
virtual one exists, and a window without the keyboard never becomes active. Both servers type in the
pl layout, the one their owner types in, and keep an XKB state of their own beside the compositor's, so
that a test can ask which locks are on.

Under Mutter a remote desktop session gives it a virtual keyboard and pointer, whose events enter its
seat where libinput's would and so take the road a real keyboard's take: the seat's XKB state, the
accessibility manager's grabs, then the focused client. Plain Mutter compiles a US keymap of its own,
and only gnome-shell applies the desktop's input sources, so the session sets the pl keymap itself.

Under sway it is a zwp_virtual_keyboard_v1. wlroots keeps no state for such a keyboard and sway gives it
neither the configured layout nor a group of other keyboards, so the keymap it uploads is the layout,
and it sends the modifiers itself after every key that changes them.

Tests press keys with press(), as desktopSession.DesktopSession.press does with xdotool on X.
"""

import fcntl
import importlib.util
import os
import socket
import socketserver
import sys
import tempfile
import threading
import time
from pathlib import Path

#: Linux input event codes, by the names xdotool and NVDA's gestures use, lowercased.
CODES = {
	"escape": 1,
	"minus": 12,
	"equal": 13,
	"backspace": 14,
	"tab": 15,
	"bracketleft": 26,
	"bracketright": 27,
	"return": 28,
	"enter": 28,
	"control_l": 29,
	"ctrl": 29,
	"control": 29,
	"semicolon": 39,
	"apostrophe": 40,
	"grave": 41,
	"shift_l": 42,
	"shift": 42,
	"backslash": 43,
	"comma": 51,
	"period": 52,
	"slash": 53,
	"shift_r": 54,
	"kp_multiply": 55,
	"alt_l": 56,
	"alt": 56,
	"space": 57,
	"caps_lock": 58,
	"capslock": 58,
	"num_lock": 69,
	"scroll_lock": 70,
	"kp_home": 71,
	"kp_7": 71,
	"kp_up": 72,
	"kp_8": 72,
	"kp_prior": 73,
	"kp_9": 73,
	"kp_subtract": 74,
	"kp_left": 75,
	"kp_4": 75,
	"kp_begin": 76,
	"kp_5": 76,
	"kp_right": 77,
	"kp_6": 77,
	"kp_add": 78,
	"kp_end": 79,
	"kp_1": 79,
	"kp_down": 80,
	"kp_2": 80,
	"kp_next": 81,
	"kp_3": 81,
	"kp_insert": 82,
	"kp_0": 82,
	"kp_delete": 83,
	"kp_decimal": 83,
	"less": 86,
	"kp_enter": 96,
	"control_r": 97,
	"kp_divide": 98,
	"alt_r": 100,
	"altgr": 100,
	"iso_level3_shift": 100,
	"home": 102,
	"up": 103,
	"prior": 104,
	"page_up": 104,
	"left": 105,
	"right": 106,
	"end": 107,
	"down": 108,
	"next": 109,
	"page_down": 109,
	"insert": 110,
	"delete": 111,
	"super_l": 125,
	"super": 125,
	"super_r": 126,
	"menu": 127,
}
for _row, _first in (("qwertyuiop", 16), ("asdfghjkl", 30), ("zxcvbnm", 44)):
	for _i, _letter in enumerate(_row):
		CODES[_letter] = _first + _i
for _i, _digit in enumerate("1234567890"):
	CODES[_digit] = 2 + _i
for _i in range(10):
	CODES[f"f{_i + 1}"] = 59 + _i
CODES["f11"], CODES["f12"] = 87, 88

#: BTN_LEFT, BTN_RIGHT and BTN_MIDDLE.
BUTTONS = {"left": 0x110, "right": 0x111, "middle": 0x112}

#: The layout both servers type in, as XKB names it.
LAYOUT = "pl"
#: The lights lights() reports, by the names XKB gives them.
LIGHTS = {"caps": "Caps Lock", "num": "Num Lock", "scroll": "Scroll Lock"}

REMOTE_DESKTOP = "org.gnome.Mutter.RemoteDesktop"
#: The values SetKeymap takes for an XKB keymap in the text format, as Mutter's
#: org.gnome.Mutter.RemoteDesktop.xml lists them under KeymapCapabilities.
KEYMAP_TYPE_XKB = 0
XKB_KEYMAP_FORMAT_TEXT_V1 = 1
#: wl_keyboard.keymap_format.xkb_v1, which the virtual keyboard's keymap request takes as well.
WL_KEYMAP_FORMAT_XKB_V1 = 1
#: The protocol's evdev key states.
RELEASED, PRESSED = 0, 1


def socketPath() -> str:
	return os.path.join(os.environ["XDG_RUNTIME_DIR"], "keyboard")


def code(name: str) -> int:
	try:
		return CODES[name.lower()]
	except KeyError:
		raise ValueError(f"no key code for {name!r}") from None


def _send(line: str) -> str:
	"""What the server answered after its ok, which is all it answers when the request worked."""
	with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
		# A stalled compositor fails the test that pressed, instead of hanging it and every one after.
		s.settimeout(10)
		s.connect(socketPath())
		s.sendall(line.encode() + b"\n")
		answer = s.makefile().readline().strip()
	if answer != "ok" and not answer.startswith("ok "):
		raise RuntimeError(f"the rig's virtual keyboard answered {answer!r} to {line.split()[0]!r}")
	return answer[3:]


def key(keyCode: int, pressed: bool) -> None:
	_send(f"key {keyCode} {int(pressed)}")


def press(*chords: str, gap: float = 0.15, hold: float = 0.03) -> None:
	"""Press each chord in turn, as "Tab", "Insert+t" or "ctrl+alt+t", with gap seconds after each.

	A key that fails to go down still lets go of those before it: the rig's keyboard outlives the test,
	and a Shift or Alt left held would change every key a later test presses."""
	for chord in chords:
		codes = [code(name) for name in chord.split("+")]
		held: list[int] = []
		try:
			for c in codes:
				key(c, True)
				held.append(c)
				time.sleep(hold)
		finally:
			while held:
				key(held.pop(), False)
				time.sleep(hold)
		time.sleep(gap)


def lights() -> dict[str, bool]:
	"""Which of caps, num and scroll are lit in the server's own XKB state, which follows key() alone: a
	lock pressed through keysym() reaches the compositor but not this."""
	values = _send("lights").split()
	return {name: value == "1" for name, value in zip(LIGHTS, values, strict=True)}


def moveBy(dx: float, dy: float) -> None:
	_send(f"motion {dx} {dy}")


def button(name: str, pressed: bool) -> None:
	_send(f"button {BUTTONS[name]} {int(pressed)}")


def _keymap():
	"""The pl keymap the servers type in, and an XKB state for it."""
	from xkbcommon import xkb

	keymap = xkb.Context().keymap_new_from_names(layout=LAYOUT)
	return keymap, keymap.state_new()


def _keymapFile(text: str) -> int:
	"""A descriptor of an unlinked file holding text, NUL-terminated as wl_keyboard.keymap's is, and
	sealed against change, because Mutter takes a keymap only from such a file.

	Not against more seals: Mutter adds the ones it needs itself, and fails when it cannot, without
	saying so, and then crashes (meta_sealed_fd_new_take_memfd and handle_set_keymap in 51.0)."""
	fd = os.memfd_create("rig-keymap", os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)
	os.write(fd, text.encode() + b"\0")
	os.lseek(fd, 0, os.SEEK_SET)
	fcntl.fcntl(fd, fcntl.F_ADD_SEALS, fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_GROW | fcntl.F_SEAL_WRITE)
	return fd


class _Injector:
	"""What both servers share: the request loop's lock and the XKB state kept beside the compositor's."""

	def __init__(self) -> None:
		from xkbcommon import xkb

		self._xkb = xkb
		self.keymap, self.state = _keymap()
		self.lock = threading.Lock()

	def track(self, keyCode: int, pressed: bool) -> None:
		direction = self._xkb.KeyDirection.XKB_KEY_DOWN if pressed else self._xkb.KeyDirection.XKB_KEY_UP
		self.state.update_key(keyCode + 8, direction)

	def lights(self) -> str:
		return " ".join(str(int(self.state.led_name_is_active(name))) for name in LIGHTS.values())

	def key(self, keyCode: int, pressed: bool) -> None:
		raise NotImplementedError

	def keysym(self, keysym: int, pressed: bool) -> None:
		raise ValueError("this compositor's virtual keyboard takes key codes only")

	def motion(self, dx: float, dy: float) -> None:
		raise ValueError("this compositor's injector has no pointer")

	def button(self, buttonCode: int, pressed: bool) -> None:
		raise ValueError("this compositor's injector has no pointer")


class _MutterInjector(_Injector):
	def __init__(self) -> None:
		super().__init__()
		from gi.repository import Gio, GLib

		self._gio, self._glib = Gio, GLib
		self._bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
		(self._session,) = self._call(
			"/org/gnome/Mutter/RemoteDesktop", REMOTE_DESKTOP, "CreateSession", reply="(o)"
		).unpack()
		self._interface = REMOTE_DESKTOP + ".Session"
		self._call(self._session, self._interface, "Start")
		fd = _keymapFile(self.keymap.get_as_string())
		fds = Gio.UnixFDList.new()
		index = fds.append(fd)
		os.close(fd)
		options = {
			"keymap-type": GLib.Variant("u", KEYMAP_TYPE_XKB),
			"xkb-keymap-format": GLib.Variant("u", XKB_KEYMAP_FORMAT_TEXT_V1),
			"xkb-keymap": GLib.Variant("h", index),
		}
		self._bus.call_with_unix_fd_list_sync(
			REMOTE_DESKTOP,
			self._session,
			self._interface,
			"SetKeymap",
			GLib.Variant("(a{sv})", (options,)),
			None,
			Gio.DBusCallFlags.NONE,
			5000,
			fds,
			None,
		)
		# The virtual keyboard joins the seat with its first key.
		self.key(CODES["shift_l"], True)
		self.key(CODES["shift_l"], False)

	def _call(self, objectPath, interface, method, args=None, reply=None):
		return self._bus.call_sync(
			REMOTE_DESKTOP,
			objectPath,
			interface,
			method,
			args,
			self._glib.VariantType(reply) if reply else None,
			self._gio.DBusCallFlags.NONE,
			5000,
			None,
		)

	def _notify(self, method, signature, *values):
		self._call(self._session, self._interface, method, self._glib.Variant(signature, values))

	def key(self, keyCode: int, pressed: bool) -> None:
		self._notify("NotifyKeyboardKeycode", "(ub)", keyCode, pressed)
		self.track(keyCode, pressed)

	def keysym(self, keysym: int, pressed: bool) -> None:
		self._notify("NotifyKeyboardKeysym", "(ub)", keysym, pressed)

	def motion(self, dx: float, dy: float) -> None:
		self._notify("NotifyPointerMotionRelative", "(dd)", dx, dy)

	def button(self, buttonCode: int, pressed: bool) -> None:
		self._notify("NotifyPointerButton", "(ib)", buttonCode, pressed)


def loadProtocol(xml: str, name: str):
	"""The pywayland module of the protocol in xml, generated as pywayland.protocol.NAME so that its
	references to the core protocol's interfaces are pywayland's own."""
	from pywayland.scanner.protocol import Protocol

	qualified = f"pywayland.protocol.{name}"
	if qualified in sys.modules:
		return sys.modules[qualified]
	protocol = Protocol.parse_file(xml)
	imports = {"wl_seat": "wayland", "wl_surface": "wayland", "wl_output": "wayland"}
	imports.update({interface.name: protocol.name for interface in protocol.interface})
	with tempfile.TemporaryDirectory() as directory:
		protocol.output(directory, imports)
		spec = importlib.util.spec_from_file_location(qualified, Path(directory) / f"{name}.py")
		module = importlib.util.module_from_spec(spec)
		sys.modules[qualified] = module
		spec.loader.exec_module(module)
	return module


def connectWayland():
	"""A connection to the compositor and its globals, by interface name, as (name, version)."""
	from pywayland.client import Display

	display = Display()
	display.connect()
	registry = display.get_registry()
	found: dict[str, tuple[int, int]] = {}

	def onGlobal(_registry, name, interface, version):
		found.setdefault(interface, (name, version))

	registry.dispatcher["global"] = onGlobal
	display.roundtrip()
	return display, registry, found


class _SwayInjector(_Injector):
	def __init__(self) -> None:
		super().__init__()
		from pywayland.protocol.wayland import WlSeat

		protocol = loadProtocol(os.environ["LINDA_VIRTUAL_KEYBOARD_PROTOCOL"], "virtual_keyboard_unstable_v1")
		self._display, registry, found = connectWayland()
		manager = protocol.ZwpVirtualKeyboardManagerV1
		if manager.name not in found:
			raise RuntimeError(f"the compositor offers no {manager.name}")
		seatName, seatVersion = found["wl_seat"]
		seat = registry.bind(seatName, WlSeat, min(seatVersion, 7))
		self._keyboard = registry.bind(found[manager.name][0], manager, 1).create_virtual_keyboard(seat)
		text = self.keymap.get_as_string()
		fd = _keymapFile(text)
		self._keyboard.keymap(WL_KEYMAP_FORMAT_XKB_V1, fd, len(text.encode()) + 1)
		os.close(fd)
		self._sent = None
		self._sendModifiers()
		self._display.roundtrip()

	def _sendModifiers(self) -> None:
		xkb = self._xkb
		modifiers = (
			self.state.serialize_mods(xkb.StateComponent.XKB_STATE_MODS_DEPRESSED),
			self.state.serialize_mods(xkb.StateComponent.XKB_STATE_MODS_LATCHED),
			self.state.serialize_mods(xkb.StateComponent.XKB_STATE_MODS_LOCKED),
			self.state.serialize_layout(xkb.StateComponent.XKB_STATE_LAYOUT_EFFECTIVE),
		)
		if modifiers != self._sent:
			self._keyboard.modifiers(*modifiers)
			self._sent = modifiers

	def key(self, keyCode: int, pressed: bool) -> None:
		self._keyboard.key(
			int(time.monotonic() * 1000) & 0xFFFFFFFF, keyCode, PRESSED if pressed else RELEASED
		)
		self.track(keyCode, pressed)
		self._sendModifiers()
		# A round trip, so that a protocol error is this request's, and the key is in before ok.
		self._display.roundtrip()


def serve(path: str) -> None:
	compositor = os.environ.get("LINDA_RIG_COMPOSITOR", "mutter")
	injector = {"mutter": _MutterInjector, "sway": _SwayInjector}[compositor]()

	class Handler(socketserver.StreamRequestHandler):
		def handle(self):
			for raw in self.rfile:
				words = raw.decode().split()
				try:
					answer = "ok"
					with injector.lock:
						if words[0] == "key":
							injector.key(int(words[1]), words[2] == "1")
						elif words[0] == "keysym":
							injector.keysym(int(words[1]), words[2] == "1")
						elif words[0] == "lights":
							answer = f"ok {injector.lights()}"
						elif words[0] == "motion":
							injector.motion(float(words[1]), float(words[2]))
						elif words[0] == "button":
							injector.button(int(words[1]), words[2] == "1")
						else:
							raise ValueError(f"unknown request {words[0]!r}")
					self.wfile.write(answer.encode() + b"\n")
				except Exception as e:  # noqa: BLE001
					# The error alone: the request may name a key a test typed.
					self.wfile.write(f"error {type(e).__name__}\n".encode())

	class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
		daemon_threads = True

	server = Server(path, Handler)
	server.serve_forever()


if __name__ == "__main__":
	if sys.argv[1:2] == ["serve"] and len(sys.argv) == 3:
		serve(sys.argv[2])
	else:
		sys.exit("usage: waylandKeyboard.py serve SOCKET")
