"""The keyboard and pointer of the rig's Wayland session.

Headless Mutter reads no input device. A remote desktop session of its own gives it a virtual keyboard
and pointer, whose events enter its seat where libinput's would and so take the road a real keyboard's
take: the seat's XKB state, the accessibility manager's grabs, then the focused client. The rig starts
one server with `waylandKeyboard.py serve SOCKET` and keeps it for the whole run, because the seat has a
keyboard only while a virtual one exists, and a window without the keyboard never becomes active.

Tests press keys with press(), as desktopSession.DesktopSession.press does with xdotool on X.
"""

import os
import socket
import socketserver
import sys
import time

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
	"kp_enter": 96,
	"control_r": 97,
	"kp_divide": 98,
	"alt_r": 100,
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

REMOTE_DESKTOP = "org.gnome.Mutter.RemoteDesktop"


def socketPath() -> str:
	return os.path.join(os.environ["XDG_RUNTIME_DIR"], "keyboard")


def code(name: str) -> int:
	try:
		return CODES[name.lower()]
	except KeyError:
		raise ValueError(f"no key code for {name!r}") from None


def _send(line: str) -> None:
	with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
		s.connect(socketPath())
		s.sendall(line.encode() + b"\n")
		answer = s.makefile().readline().strip()
	if answer != "ok":
		raise RuntimeError(f"the rig's virtual keyboard answered {answer!r} to {line!r}")


def key(keyCode: int, pressed: bool) -> None:
	_send(f"key {keyCode} {int(pressed)}")


def press(*chords: str, gap: float = 0.15, hold: float = 0.03) -> None:
	"""Press each chord in turn, as "Tab", "Insert+t" or "ctrl+alt+t", with gap seconds after each."""
	for chord in chords:
		codes = [code(name) for name in chord.split("+")]
		for c in codes:
			key(c, True)
			time.sleep(hold)
		for c in reversed(codes):
			key(c, False)
			time.sleep(hold)
		time.sleep(gap)


def moveBy(dx: float, dy: float) -> None:
	_send(f"motion {dx} {dy}")


def button(name: str, pressed: bool) -> None:
	_send(f"button {BUTTONS[name]} {int(pressed)}")


def serve(path: str) -> None:
	from gi.repository import Gio, GLib

	bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)

	def call(objectPath, interface, method, args=None, reply=None):
		return bus.call_sync(
			REMOTE_DESKTOP,
			objectPath,
			interface,
			method,
			args,
			GLib.VariantType(reply) if reply else None,
			Gio.DBusCallFlags.NONE,
			5000,
			None,
		)

	(session,) = call(
		"/org/gnome/Mutter/RemoteDesktop", REMOTE_DESKTOP, "CreateSession", reply="(o)"
	).unpack()
	sessionInterface = REMOTE_DESKTOP + ".Session"
	call(session, sessionInterface, "Start")

	def notify(method, signature, *values):
		call(session, sessionInterface, method, GLib.Variant(signature, values))

	# The virtual keyboard joins the seat with its first key.
	notify("NotifyKeyboardKeycode", "(ub)", CODES["shift_l"], True)
	notify("NotifyKeyboardKeycode", "(ub)", CODES["shift_l"], False)

	class Handler(socketserver.StreamRequestHandler):
		def handle(self):
			for raw in self.rfile:
				words = raw.decode().split()
				try:
					if words[0] == "key":
						notify("NotifyKeyboardKeycode", "(ub)", int(words[1]), words[2] == "1")
					elif words[0] == "keysym":
						notify("NotifyKeyboardKeysym", "(ub)", int(words[1]), words[2] == "1")
					elif words[0] == "motion":
						notify("NotifyPointerMotionRelative", "(dd)", float(words[1]), float(words[2]))
					elif words[0] == "button":
						notify("NotifyPointerButton", "(ib)", int(words[1]), words[2] == "1")
					else:
						raise ValueError(f"unknown request {words[0]!r}")
					self.wfile.write(b"ok\n")
				except Exception as e:  # noqa: BLE001
					self.wfile.write(f"error {e}\n".encode())

	class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
		daemon_threads = True

	server = Server(path, Handler)
	server.serve_forever()


if __name__ == "__main__":
	if sys.argv[1:2] == ["serve"] and len(sys.argv) == 3:
		serve(sys.argv[2])
	else:
		sys.exit("usage: waylandKeyboard.py serve SOCKET")
