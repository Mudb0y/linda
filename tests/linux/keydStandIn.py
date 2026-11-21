"""A stand-in for linda-keyd, for tests in the rig, which has no input devices.

It speaks the helper's protocol, as engine/crates/keyd/src/protocol.rs gives it, on a socket of its
own, and is the keyboard as well: a test presses keys on it as a person would on a real one. While
its client has it capture, each key goes to the client and in line, and what the client passes, or
does not answer in time, goes on to the X display on the display's own keyboard device, which stands
for the helper's virtual keyboard; the rest goes nowhere. While it does not capture, keys go straight
to the X display through XTEST, the display's one other keyboard device, as they do from a keyboard
nothing holds: Linda's X source tells the two apart by device, as it tells the virtual keyboard from
the real keyboards, and a test names the first to Linda as the virtual keyboard. It sends the lights of the locks as the X
display sets them, reads the emergency chord itself, lets go of everything when its client falls
silent, and can be made to refuse, say goodbye, let go, lose its keyboard or vanish. It grants the
feature WAIT, a verdict that holds a press longer, unless told not to, and TYPE and LATE_PASS only
when a test names them in features, so that a client can be run with them and without.

The X display is its default sink. A test of a session with no X keyboard behind Linda gives it
another: NullSink, which puts keys nowhere and takes the lights from a layout of its own, or under
the rig's compositors MutterSink and SwaySink, which put them on the session's keyboard.

It is written from the protocol's documentation, not from Linda's client, so that the two are held
to the same text rather than to each other.
"""

import functools
import importlib.util
import os
import select
import socket
import struct
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, Self

from xkbcommon import xkb
from Xlib import XK, X, display
from Xlib.ext import xinput, xtest
from Xlib.protocol import rq

HELLO, START, STOP, VERDICT, HEARTBEAT, TYPE, LATE_PASS = range(0x01, 0x08)
WELCOME, GOODBYE, KEY, EXPIRED, RELEASED, DEVICE, LOCKS, HEARTBEAT_ECHO, TYPED = range(0x81, 0x8A)
KEY_ASK, KEY_SYNTHETIC, KEY_TYPED = 0x01, 0x02, 0x04
#: TYPE and TYPED; VERDICT_WAIT; LATE_PASS. The stand-in grants WAIT unless told otherwise, and the
#: others only when a test names them in features.
FEATURE_TYPING, FEATURE_WAIT, FEATURE_LATE_PASS = 1, 2, 4
#: VERDICT's values: pass, keep, and with FEATURE_WAIT, hold the press longer.
VERDICT_PASS, VERDICT_KEEP, VERDICT_WAIT = 0, 1, 2
RELEASE, PRESS, REPEAT = 0, 1, 2
RELEASED_STOPPED, RELEASED_EMERGENCY, RELEASED_BACKGROUND, RELEASED_VIRTUAL, RELEASED_SESSION_UNKNOWN = range(
	1, 6
)
GOODBYE_BUSY, GOODBYE_SILENT, GOODBYE_MALFORMED, GOODBYE_UNANSWERED = 4, 6, 7, 8
DEVICE_HELD, DEVICE_GONE = 1, 4
#: TYPE's modes: back as KEY flagged KEY_TYPED, to be decided; or in line already passed.
TYPE_DECIDE, TYPE_INJECT = 0, 1
#: TYPED's outcomes.
(
	TYPED_OUT,
	TYPED_KEPT,
	TYPED_REFUSED_STATE,
	TYPED_REFUSED_KEY,
	TYPED_REFUSED_FULL,
	TYPED_NOT_DOWN,
	TYPED_LOST,
) = range(1, 8)
#: The device number of keys typed with TYPE_DECIDE.
TYPED_DEVICE = 0xFFFFFFFF
TYPED_WAITING_LIMIT = 64
TYPED_PER_SECOND = 100
TYPED_BURST = 200
#: KEY_SLEEP, which keyMap.py names, and the codes logind takes as the machine's power keys.
NEVER_TYPED = frozenset((142, 116, 205, 0x164, 0x198))
SYSRQ = 99
ALTS = frozenset((56, 100))
#: Backspace, Escape and Enter.
CHORD = frozenset((14, 1, 28))
#: The helper's MODIFIERS (engine/crates/keyd/src/keys.rs): the Controls, Shifts, Alts, Metas and Caps
#: Lock, whose releases keep their place in line.
MODIFIERS = frozenset((29, 42, 54, 56, 97, 100, 125, 126, 58))
#: The helper's READER_MODIFIERS: Insert and the numpad's Insert.
READER_MODIFIERS = frozenset((110, 82))
CAPS_LOCK = 58
KEY_MAP = Path(__file__).resolve().parent.parent.parent / "source" / "linuxPlatform" / "keys" / "keyMap.py"
#: The stand-in's keyboard, as the kernel would number it; a test may press keys on others too.
DEVICE_NUMBER = 3
DEVICE_NAME = "Stand-in keyboard"

#: evdev codes of the keys tests press, by xdotool's names for them.
CODES = {
	"Escape": 1,
	"BackSpace": 14,
	"Tab": 15,
	"Return": 28,
	"Control_L": 29,
	"control": 29,
	"ctrl": 29,
	"Shift_L": 42,
	"shift": 42,
	"Alt_L": 56,
	"alt": 56,
	"space": 57,
	"Caps_Lock": 58,
	"Num_Lock": 69,
	"Scroll_Lock": 70,
	"KP_Home": 71,
	"KP_Up": 72,
	"KP_Prior": 73,
	"KP_Left": 75,
	"KP_Begin": 76,
	"KP_Right": 77,
	"KP_Add": 78,
	"KP_End": 79,
	"KP_Down": 80,
	"KP_Next": 81,
	"KP_Insert": 82,
	"KP_Enter": 96,
	"ISO_Level3_Shift": 100,
	"AltGr": 100,
	"Home": 102,
	"Up": 103,
	"Prior": 104,
	"Left": 105,
	"Right": 106,
	"End": 107,
	"Down": 108,
	"Next": 109,
	"Insert": 110,
	"Delete": 111,
	"Super_L": 125,
	"super": 125,
	"Super_R": 126,
	"F11": 87,
	"F12": 88,
}
CODES.update({f"F{number}": 58 + number for number in range(1, 11)})
CODES.update({str(digit): 2 + (digit - 1) % 10 for digit in range(1, 11)})
CODES["0"] = 11
for _row, _first in (("qwertyuiop", 16), ("asdfghjkl", 30), ("zxcvbnm", 44)):
	CODES.update({letter: _first + index for index, letter in enumerate(_row)})


_CODES_BY_FOLDED = {name.casefold(): evdev for name, evdev in CODES.items()}


def code(name: str) -> int:
	"""The code of the key named name, whatever its case; KeyError for a name it has no key by."""
	return _CODES_BY_FOLDED[name.casefold()]


def isVirtualKey(evdev: int) -> bool:
	"""Whether the virtual keyboard can send the code, as protocol.rs's is_virtual_key has it."""
	return 1 <= evdev <= 0xFF or 0x160 <= evdev <= 0x21F or 0x224 <= evdev <= 0x2BF or 0x2E8 <= evdev <= 0x2FF


@functools.cache
def typableCodes() -> frozenset[int]:
	"""The codes the helper types: those keyMap.py's FIXED, NUMPAD and LAYOUT tables name, read from its
	file alone, less the ones it never types."""
	spec = importlib.util.spec_from_file_location("lindaKeyMapTables", KEY_MAP)
	tables = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(tables)
	return frozenset(set(tables.FIXED) | set(tables.NUMPAD) | set(tables.LAYOUT)) - NEVER_TYPED


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


def _displayKeyboard(d: display.Display):
	return next(
		device
		for device in d.xinput_query_device(xinput.AllDevices).devices
		if device.use == xinput.SlaveKeyboard and "XTEST" not in str(device.name)
	)


def virtualKeyboardName() -> str:
	"""The name of the X display's device the stand-in puts out passed keys on, which a test names to
	Linda as the helper's virtual keyboard."""
	d = display.Display()
	try:
		return str(_displayKeyboard(d).name)
	finally:
		d.close()


class _XKeyboard:
	"""The X display's keyboard devices, which keys reach the X display on: its own keyboard, which
	stands for the helper's virtual keyboard, and XTEST, for the real keyboard while nothing holds it."""

	def __init__(self) -> None:
		self._display = display.Display()
		self._device = _displayKeyboard(self._display).deviceid
		self._firstEvent = self._display.query_extension("XInputExtension").first_event
		self._xtest = self._display.query_extension("XTEST").major_opcode
		self._lock = threading.Lock()

	def send(self, evdev: int, down: bool, virtual: bool) -> None:
		with self._lock:
			if not virtual:
				xtest.fake_input(self._display, X.KeyPress if down else X.KeyRelease, evdev + 8)
				self._display.flush()
				return
			_DeviceFakeInput(
				display=self._display.display,
				opcode=self._xtest,
				eventType=self._firstEvent + (1 if down else 2),
				detail=evdev + 8,
				time=0,
				root=0,
				x=0,
				y=0,
				deviceid=self._device,
			)
			self._display.flush()

	def lights(self) -> int:
		"""The lock lights as the protocol has them: bit n is the kernel's LED code n."""
		with self._lock:
			mask = self._display.get_keyboard_control().led_mask
		# The X display numbers Caps Lock's light first and Num Lock's second; the kernel the other way.
		return (2 if mask & 1 else 0) | (1 if mask & 2 else 0) | (4 if mask & 4 else 0)

	def keysymCode(self, name: str) -> int:
		return self._display.keysym_to_keycode(XK.string_to_keysym(name)) - 8

	def close(self) -> None:
		self._display.close()


class Sink(Protocol):
	"""Where the stand-in puts keys out, and reads the lights of the locks from. A sink may also have
	keep(evdev, down), which the stand-in calls for each press and release the client kept."""

	def send(self, evdev: int, down: bool, virtual: bool) -> None:
		"""A key put out: virtual on the helper's virtual keyboard, otherwise on the real keyboard while
		nothing holds it."""

	def lights(self) -> int:
		"""The lock lights as the protocol has them: bit n is the kernel's LED code n."""

	def close(self) -> None: ...


#: The kernel's LED codes, as the protocol numbers the lights, by xkbcommon's names for them.
_LED_BITS = (("Num Lock", 1), ("Caps Lock", 2), ("Scroll Lock", 4))


class NullSink:
	"""A sink with nothing behind it, for a session where Linda has no X keyboard: it records each key
	put out and each key the client kept, and follows the keys put out in an xkbcommon state of its own
	for a named layout, as a desktop would, so the lights it reports are those the layout gives."""

	def __init__(
		self, layout: str = "us", variant: str = "", options: str = "", model: str = "pc105"
	) -> None:
		self._keymap = xkb.Context().keymap_new_from_names(
			rules="evdev",
			model=model or None,
			layout=layout or None,
			variant=variant or None,
			options=options or None,
		)
		self._state = self._keymap.state_new()
		self._lock = threading.Lock()
		#: (code, down, virtual) of each key put out, in order.
		self.sent: list[tuple[int, bool, bool]] = []
		#: (code, down) of each press and release the client kept, in order.
		self.kept: list[tuple[int, bool]] = []
		self.closed = False

	def send(self, evdev: int, down: bool, virtual: bool) -> None:
		with self._lock:
			self.sent.append((evdev, down, virtual))
			self._state.update_key(
				evdev + 8, xkb.KeyDirection.XKB_KEY_DOWN if down else xkb.KeyDirection.XKB_KEY_UP
			)

	def keep(self, evdev: int, down: bool) -> None:
		with self._lock:
			self.kept.append((evdev, down))

	def lights(self) -> int:
		lights = 0
		with self._lock:
			for name, bit in _LED_BITS:
				try:
					if self._state.led_name_is_active(name):
						lights |= bit
				except xkb.XKBLEDDoesNotExist:
					pass
		return lights

	def passed(self) -> list[tuple[int, bool]]:
		"""(code, down) of each key put out on the virtual keyboard."""
		with self._lock:
			return [(evdev, down) for evdev, down, virtual in self.sent if virtual]

	def close(self) -> None:
		self.closed = True


class _CompositorSink:
	"""The keyboard of the rig's Wayland session, the one its key server (waylandKeyboard.py) holds for
	the whole run, which types in the pl layout: keys put out go to the compositor on it, virtual or not,
	since Linda tells no devices apart there and a second keyboard would have the seat send its keymap
	again at every switch. The lights are those of the server's own XKB state, which follows every key
	put out on it, the stand-in's and a test's own presses alike."""

	#: The rig's LINDA_RIG_COMPOSITOR this sink is for.
	compositor = ""

	def __init__(self) -> None:
		import waylandKeyboard

		running = os.environ.get("LINDA_RIG_COMPOSITOR")
		if running != self.compositor:
			raise RuntimeError(f"{type(self).__name__} needs the rig's {self.compositor}, not {running}")
		self._lightNames = list(waylandKeyboard.LIGHTS)
		self._lock = threading.Lock()
		# One connection for the stand-in's life, which the server answers line by line, so that a key costs
		# a round trip and not a connection.
		self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
		# A stalled compositor fails the test, instead of hanging it and every one after.
		self._server.settimeout(10)
		try:
			self._server.connect(waylandKeyboard.socketPath())
		except OSError:
			self._server.close()
			raise
		self._answers = self._server.makefile("r", encoding="utf-8")
		#: (code, down, virtual) of each key put out, in order.
		self.sent: list[tuple[int, bool, bool]] = []
		#: (code, down) of each press and release the client kept, in order.
		self.kept: list[tuple[int, bool]] = []

	def _ask(self, request: str) -> str:
		with self._lock:
			self._server.sendall(request.encode() + b"\n")
			answer = self._answers.readline().strip()
		if answer != "ok" and not answer.startswith("ok "):
			# The request's first word alone: the rest names a key.
			raise RuntimeError(f"the rig's key server answered {answer!r} to {request.split()[0]!r}")
		return answer[3:]

	def send(self, evdev: int, down: bool, virtual: bool) -> None:
		self._ask(f"key {evdev} {int(down)}")
		self.sent.append((evdev, down, virtual))

	def keep(self, evdev: int, down: bool) -> None:
		self.kept.append((evdev, down))

	def lights(self) -> int:
		lit = dict(
			zip(self._lightNames, (value == "1" for value in self._ask("lights").split()), strict=True)
		)
		return (1 if lit["num"] else 0) | (2 if lit["caps"] else 0) | (4 if lit["scroll"] else 0)

	def passed(self) -> list[tuple[int, bool]]:
		"""(code, down) of each key put out on the virtual keyboard."""
		return [(evdev, down) for evdev, down, virtual in self.sent if virtual]

	def close(self) -> None:
		with self._lock:
			self._answers.close()
			self._server.close()


class MutterSink(_CompositorSink):
	"""Keys put out under rig Mutter, through its RemoteDesktop session's virtual keyboard, which the key
	server gave the pl keymap with SetKeymap."""

	compositor = "mutter"


class SwaySink(_CompositorSink):
	"""Keys put out under rig sway, through the key server's zwp_virtual_keyboard_v1, which carries the pl
	keymap and sends its own modifiers, since wlroots keeps no XKB state for such a keyboard."""

	compositor = "sway"


def compositorSink() -> MutterSink | SwaySink:
	"""The sink of the rig's compositor."""
	sinks = {sink.compositor: sink for sink in (MutterSink, SwaySink)}
	running = os.environ.get("LINDA_RIG_COMPOSITOR")
	if running not in sinks:
		raise RuntimeError(f"the rig runs no compositor a sink is made for: {running}")
	return sinks[running]()


@dataclass
class _Entry:
	seq: int
	code: int
	press: bool
	arrival: float
	#: Seconds from its arrival after which it is passed on without a verdict.
	bound: float = 0.0
	verdict: bool | None = None
	waited: bool = False
	device: int = DEVICE_NUMBER
	#: The TYPE's tag, for a typed key.
	tag: int | None = None
	#: Typed with TYPE_INJECT: in line already passed, never asked about and never unanswered.
	injected: bool = False
	#: (device, code) of each kept press that LATE_PASS puts out just before this one, in the order the
	#: LATE_PASSes came.
	lateKept: list[tuple[int, int]] = field(default_factory=list)


class StandIn:
	"""linda-keyd's stand-in, listening at path."""

	def __init__(
		self,
		path: Path | str,
		*,
		verdictTimeout: float = 0.1,
		heartbeatInterval: float = 0.5,
		heartbeatTimeout: float = 2.0,
		unansweredSpan: float = 1.0,
		unansweredGap: float = 1.0,
		toX: bool = True,
		announcedVerdictTimeout: float | None = None,
		settle: float = 0.0,
		features: int = FEATURE_WAIT,
		waitTimeout: float = 0.6,
		announcedHeartbeatTimeout: float | None = None,
		sink: Sink | None = None,
	) -> None:
		"""
		:param toX: Whether keys go on to the X display; without, they go nowhere, and only what the
			client did with them is recorded.
		:param sink: Where keys go on to in place of the X display, which opens no X connection.
		:param announcedVerdictTimeout: The bound WELCOME gives, when it is to be another than the one
			kept, as for a client that cannot keep it.
		:param features: Those it grants of the features a client asks for.
		:param waitTimeout: The bound of a press the client answered with WAIT.
		:param announcedHeartbeatTimeout: The silence WELCOME says the stand-in lets go after, when it is
			to be another than the one kept, as for a stand-in frozen with its client, which has no
			means of its own to tell that it was.
		"""
		self.features = features
		self.waitTimeout = waitTimeout
		#: The features granted to the client served now.
		self.granted = 0
		self.announcedVerdictTimeout = announcedVerdictTimeout
		self.announcedHeartbeatTimeout = announcedHeartbeatTimeout
		#: Seconds from the first START to the first grab, as the helper's virtual keyboard settles.
		self.settle = settle
		self._captureAt = 0.0
		self.path = str(path)
		self.verdictTimeout = verdictTimeout
		self.heartbeatInterval = heartbeatInterval
		self.heartbeatTimeout = heartbeatTimeout
		self.unansweredSpan = unansweredSpan
		self.unansweredGap = unansweredGap
		if sink is not None and not toX:
			raise ValueError("a stand-in with a sink puts keys out on it; toX=False puts them nowhere")
		self._sink: Sink | None = sink if sink is not None else _XKeyboard() if toX else None
		self._lock = threading.RLock()
		self._wakeRead, self._wakeWrite = os.pipe()
		self._listener: socket.socket | None = None
		self._client: socket.socket | None = None
		self._waiting: list[socket.socket] = []
		self._thread: threading.Thread | None = None
		self._stopping = False
		self._seq = 0
		self._line: deque[_Entry] = deque()
		#: (device, code) of the keys physically down, and of those, the ones whose press was kept; and
		#: how many keyboards hold each code down on the virtual keyboard, which has it down from the
		#: first to the last.
		self._down: set[tuple[int, int]] = set()
		self._kept: set[tuple[int, int]] = set()
		#: (device, code) of each key whose press went out passed and whose release has not.
		self._passedOut: set[tuple[int, int]] = set()
		self._virtualDown: dict[int, int] = {}
		self._lastHeard = 0.0
		#: (reading of the first press, reading of the last) of the run of unanswered presses.
		self._run: tuple[float, float] | None = None
		self._lights: int | None = None
		self.capturing = False
		#: Asked to capture while keys were down: the helper grabs once every key is up.
		self._startWaiting = False
		self._nextLights = 0.0
		#: What the next HELLO is answered with: None to admit, or (reason, text) to say goodbye.
		self.refusal: tuple[int, str] | None = None
		#: Whether a HELLO is answered at all.
		self.answering = True
		#: Each thing that happened, as (time.monotonic(), what, details...).
		self.events: list[tuple] = []
		#: (seq, keep, seconds from the key's reading to its verdict) of each verdict in time.
		self.verdicts: list[tuple[int, bool, float]] = []
		#: Every verdict the client gave, by the sequence number it named: VERDICT_PASS, VERDICT_KEEP and
		#: VERDICT_WAIT in the order they came, whether or not they came in time to count.
		self.given: dict[int, list[int]] = {}
		#: (seq, seconds from the key's reading) of each WAIT that held a press longer.
		self.waits: list[tuple[int, float]] = []
		self.heartbeats: list[float] = []
		self.starts = 0
		self.admitted = threading.Event()
		self.gone = threading.Event()
		#: (device, code, value, flags) of each KEY sent the client served now, by its sequence number.
		self._told: dict[int, tuple[int, int, int, int]] = {}
		#: The sequence number of each press told whose release has been told, and of that release.
		self._releaseOf: dict[int, int] = {}
		self._pressOf: dict[tuple[int, int], int] = {}
		#: The presses kept by a verdict that counted, by sequence number.
		self._keptSeqs: set[int] = set()
		#: The kept presses a LATE_PASS has named.
		self._latePassNamed: set[int] = set()
		#: (kept, waiting, whether it applied) of each LATE_PASS well formed.
		self.latePasses: list[tuple[int, int, bool]] = []
		#: The codes the client has typed down, with the typed SysRq rules' state of the virtual keyboard.
		self._typedDown: set[int] = set()
		self._sysrqActive = False
		self._typedTokens = float(TYPED_BURST)
		self._typedFilled = 0.0
		#: (tag, seq, outcome) of each TYPED sent, in order.
		self.typed: list[tuple[int, int, int]] = []

	# Running.

	def start(self) -> Self:
		self._listen()
		self._thread = threading.Thread(target=self._serve, name="keyd stand-in", daemon=True)
		self._thread.start()
		return self

	def _listen(self) -> None:
		listener = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
		if os.path.exists(self.path):
			os.unlink(self.path)
		listener.bind(self.path)
		listener.listen(8)
		listener.setblocking(False)
		self._listener = listener

	def close(self) -> None:
		self._stopping = True
		self._wake()
		if self._thread is not None:
			self._thread.join(5)
		with self._lock:
			self._dropClient(sendGoodbye=None)
			if self._listener is not None:
				self._listener.close()
				self._listener = None
		if os.path.exists(self.path):
			os.unlink(self.path)
		if self._sink is not None:
			for evdev in sorted({evdev for _device, evdev in self._down}):
				self._sink.send(evdev, False, virtual=False)
			self._sink.close()
		os.close(self._wakeRead)
		os.close(self._wakeWrite)

	def __enter__(self) -> Self:
		return self.start()

	def __exit__(self, *exception: object) -> None:
		self.close()

	def _wake(self) -> None:
		try:
			os.write(self._wakeWrite, b"\0")
		except OSError:
			pass

	def _note(self, *what: object) -> None:
		self.events.append((time.monotonic(), *what))

	# The keyboard.

	def down(self, name: str | int, device: int = DEVICE_NUMBER) -> None:
		evdev = name if isinstance(name, int) else code(name)
		with self._lock:
			if (device, evdev) in self._down:
				return
			self._down.add((device, evdev))
			if not self.capturing:
				self._toX(evdev, True)
				return
			if CHORD <= {held for _device, held in self._down}:
				self._note("emergency")
				self._letGo(RELEASED_EMERGENCY, dropUndecided=True)
				return
			self._enter(evdev, PRESS, KEY_ASK, device)
		self._wake()

	def up(self, name: str | int, device: int = DEVICE_NUMBER) -> None:
		evdev = name if isinstance(name, int) else code(name)
		with self._lock:
			if (device, evdev) not in self._down:
				return
			self._down.discard((device, evdev))
			if not self.capturing:
				self._toX(evdev, False)
				self._tryCapture(time.monotonic())
				return
			self._enter(evdev, RELEASE, 0, device)
		self._wake()

	def repeat(self, name: str | int, device: int = DEVICE_NUMBER) -> None:
		"""One repeat of a key held down, as the kernel's own repeat sends them."""
		evdev = name if isinstance(name, int) else code(name)
		with self._lock:
			if (device, evdev) in self._down and self.capturing:
				self._enter(evdev, REPEAT, 0, device)

	def press(self, *chords: str, hold: float = 0.1, gap: float = 0.03) -> None:
		"""Press each chord in turn, as xdotool names them: its keys down in order, held, then up."""
		for chord in chords:
			keys = chord.split("+")
			for key in keys:
				self.down(key)
				time.sleep(gap)
			time.sleep(hold)
			for key in reversed(keys):
				self.up(key)
				time.sleep(gap)

	def type(self, text: str) -> None:
		self.press(*text)

	def _toX(self, evdev: int, down: bool, virtual: bool = False) -> None:
		self._note("x", evdev, down)
		if self._sink is not None:
			self._sink.send(evdev, down, virtual)

	def _keep(self, evdev: int, down: bool) -> None:
		keep = getattr(self._sink, "keep", None)
		if keep is not None:
			keep(evdev, down)

	# What the stand-in does as the helper.

	def keyboardGone(self, device: int = DEVICE_NUMBER) -> None:
		"""The keyboard vanishes with keys down: each gets a release made by the helper, in line."""
		with self._lock:
			for held, evdev in sorted(self._down):
				if held == device:
					self._enter(evdev, RELEASE, KEY_SYNTHETIC, device)
					self._down.discard((device, evdev))
			self._sendDevice(DEVICE_GONE, device)
		self._wake()

	def letGo(self, reason: int, taps: tuple[str | int, ...] = (), device: int = DEVICE_NUMBER) -> None:
		"""Let go, as the helper does for reason.

		:param taps: Keys read too late for the client to be asked, which the helper puts out on its
			virtual keyboard as taps as it lets go, after the presses waiting for a verdict; it tells
			the client of each as a KEY press and a KEY release without flags, before RELEASED, on
			STOPPED, EMERGENCY and BACKGROUND for a sleep, and of none for any other reason.
		"""
		with self._lock:
			self._letGo(reason, dropUndecided=reason == RELEASED_EMERGENCY, taps=taps, device=device)

	def madeUpPress(self, name: str | int, device: int = DEVICE_NUMBER) -> None:
		"""A modifier the kernel has down after a loss of events that the helper did not see go down: a
		press flagged ASK and SYNTHETIC, decided as any press is, whose release comes as an ordinary one."""
		evdev = name if isinstance(name, int) else code(name)
		with self._lock:
			self._down.add((device, evdev))
			self._enter(evdev, PRESS, KEY_ASK | KEY_SYNTHETIC, device)
		self._wake()

	def goodbye(self, reason: int, text: str = "") -> None:
		with self._lock:
			self._dropClient(sendGoodbye=(reason, text))

	def vanish(self) -> None:
		"""The helper dies: its socket goes, and the kernel releases what its virtual keyboard held."""
		with self._lock:
			if self._listener is not None:
				self._listener.close()
				self._listener = None
			os.unlink(self.path)
			self._dropClient(sendGoodbye=None, answerTyped=False)
		self._note("vanished")

	def comeBack(self) -> None:
		with self._lock:
			self._listen()
		self._wake()
		self._note("back")

	def waitFor(self, condition, timeout: float = 5.0) -> None:
		deadline = time.monotonic() + timeout
		while not condition():
			assert time.monotonic() < deadline, f"the stand-in waited in vain; it saw {self.events[-20:]}"
			time.sleep(0.01)

	def emitted(self) -> list[tuple[int, bool]]:
		"""(code, down) of each key the sink had from the stand-in, captured or not."""
		return [(evdev, down) for _t, what, *rest in self.events if what == "x" for evdev, down in [rest]]

	# The protocol.

	def _send(self, sock: socket.socket, message: bytes) -> None:
		try:
			sock.send(message)
		except OSError:
			pass

	def _sendClient(self, message: bytes) -> None:
		if self._client is not None:
			self._send(self._client, message)

	def _sendDevice(self, status: int, device: int = DEVICE_NUMBER) -> None:
		self._sendClient(struct.pack("<BIBB", DEVICE, device, status, 0) + DEVICE_NAME.encode())

	def _enter(
		self, evdev: int, value: int, flags: int, device: int = DEVICE_NUMBER, tag: int | None = None
	) -> None:
		self._seq += 1
		now = time.monotonic()
		if value != REPEAT:
			self._line.append(
				_Entry(self._seq, evdev, value == PRESS, now, self.verdictTimeout, device=device, tag=tag)
			)
		self._tell(self._seq, device, evdev, value, flags)
		self._advance(now)

	def _tell(self, seq: int, device: int, evdev: int, value: int, flags: int) -> None:
		self._told[seq] = (device, evdev, value, flags)
		if value == PRESS:
			self._pressOf[(device, evdev)] = seq
		elif value == RELEASE and (press := self._pressOf.pop((device, evdev), None)) is not None:
			self._releaseOf[press] = seq
		self._note("key", seq, evdev, value, flags)
		self._sendClient(struct.pack("<BQIHBB", KEY, seq, device, evdev, value, flags))

	def _advance(self, now: float) -> None:
		"""Put out every key at the head of the line that is decided or overdue, then every release
		that may overtake the presses still waiting."""
		self._advanceHead(now)
		self._releaseEarly()

	def _releaseEarly(self) -> None:
		"""As the helper's release_early: a release whose press has gone out goes out at once, ahead of
		presses still waiting for their verdict, unless its key is a modifier, or its own press or a later
		press of the same key is still in line."""
		waiting = set()
		for entry in list(self._line):
			key = (entry.device, entry.code)
			if entry.press:
				waiting.add(key)
			elif key not in waiting and entry.code not in MODIFIERS and key in self._passedOut:
				self._line.remove(entry)
				self._releaseOut(entry)

	def _advanceHead(self, now: float) -> None:
		while self._line:
			head = self._line[0]
			if head.press and head.verdict is None:
				if now - head.arrival < head.bound:
					return
				head.verdict = False
				self._note("expired", head.seq)
				self._sendClient(struct.pack("<BQ", EXPIRED, head.seq))
				# A run of unanswered presses goes on only while each is read within UNANSWERED_GAP of the
				# one before it, in reading order.
				if self._run is None or head.arrival - self._run[1] > self.unansweredGap:
					self._run = (head.arrival, head.arrival)
				else:
					self._run = (self._run[0], head.arrival)
				if head.arrival + head.bound - self._run[0] >= self.unansweredSpan:
					self._line.popleft()
					self._pressOut(head)
					self._dropClient(sendGoodbye=(GOODBYE_UNANSWERED, "presses went unanswered"))
					return
			elif head.press and not head.injected:
				# Answered in time, which ends any run.
				self._run = None
			self._line.popleft()
			if head.press:
				self._pressOut(head)
			else:
				self._releaseOut(head)

	def _pressOut(self, head: _Entry) -> None:
		"""A decided press leaves the line: kept, or put on the virtual keyboard."""
		held = (head.device, head.code)
		typed = head.tag is not None
		for late in head.lateKept:
			if late in self._kept:
				# Whatever becomes of the press, the kept key goes out in its place, and from now its release
				# is passed as though its press had been.
				self._kept.discard(late)
				self._passedOut.add(late)
				self._note("late pass out", head.seq)
				self._virtual(late[1], True)
		if head.verdict:
			self._kept.add(held)
			self._passedOut.discard(held)
			if typed:
				self._sendTyped(head, TYPED_KEPT)
			else:
				self._keep(head.code, True)
		elif typed and self._sysrqRefuses(head.code):
			# Keys ahead of it changed what is down since its TYPE came.
			self._typedDown.discard(head.code)
			self._sendTyped(head, TYPED_REFUSED_KEY)
		elif typed and held in self._passedOut:
			# Typed down again while down: the virtual keyboard has it down already, as the kernel's input
			# core drops a press of a key that is down.
			self._sendTyped(head, TYPED_OUT)
		else:
			self._kept.discard(held)
			self._passedOut.add(held)
			self._virtual(head.code, True)
			if typed:
				self._sendTyped(head, TYPED_OUT)

	def _releaseOut(self, entry: _Entry) -> None:
		held = (entry.device, entry.code)
		typed = entry.tag is not None
		if held in self._kept:
			self._kept.discard(held)
			if typed:
				self._sendTyped(entry, TYPED_KEPT)
			else:
				self._keep(entry.code, False)
		elif typed and held not in self._passedOut:
			# Its press was refused as it was to go out.
			self._sendTyped(entry, TYPED_NOT_DOWN)
		else:
			self._passedOut.discard(held)
			self._virtual(entry.code, False)
			if typed:
				self._sendTyped(entry, TYPED_OUT)

	def _virtual(self, evdev: int, down: bool) -> None:
		holders = self._virtualDown.get(evdev, 0)
		if not down and not holders:
			return
		holders += 1 if down else -1
		if holders:
			self._virtualDown[evdev] = holders
		else:
			del self._virtualDown[evdev]
		if holders == (1 if down else 0):
			# The kernel's SysRq handler for the virtual keyboard, which typed presses are checked against.
			if down and evdev == SYSRQ and ALTS & self._virtualDown.keys():
				self._sysrqActive = True
			elif not down and evdev in ALTS and not ALTS & self._virtualDown.keys():
				self._sysrqActive = False
			self._toX(evdev, down, virtual=True)

	def _letGo(
		self,
		reason: int | None,
		dropUndecided: bool,
		taps: tuple[str | int, ...] = (),
		device: int = DEVICE_NUMBER,
		answerTyped: bool = True,
	) -> None:
		"""
		:param answerTyped: Whether typed keys dropped are answered TYPED_LOST, as they are unless the
			helper dies.
		"""
		# A key typed for one moment must not land in the next, unlike the user's own keys still waiting.
		for entry in [entry for entry in self._line if entry.tag is not None]:
			self._line.remove(entry)
			if answerTyped:
				self._sendTyped(entry, TYPED_LOST)
		for entry in self._line:
			# The kept key of a LATE_PASS goes out only at its press's turn, which letting go is not.
			entry.lateKept.clear()
		if dropUndecided:
			self._line.clear()
		else:
			for entry in self._line:
				if entry.press and entry.verdict is None:
					# Passed on, as no verdict is coming.
					entry.verdict = False
			self._advance(time.monotonic())
		told = reason in (RELEASED_STOPPED, RELEASED_EMERGENCY, RELEASED_BACKGROUND)
		for name in taps:
			evdev = name if isinstance(name, int) else code(name)
			for value in (PRESS, RELEASE):
				self._toX(evdev, value == PRESS, virtual=True)
				if told:
					self._seq += 1
					self._told[self._seq] = (device, evdev, value, 0)
					self._note("tap", self._seq, evdev, value)
					self._sendClient(struct.pack("<BQIHBB", KEY, self._seq, device, evdev, value, 0))
		for evdev in sorted(self._virtualDown):
			self._toX(evdev, False, virtual=True)
		self._virtualDown.clear()
		self._kept.clear()
		# Capture waits for every key to be up, so nothing kept now is down when it captures again.
		self._keptSeqs.clear()
		self._passedOut.clear()
		self._typedDown.clear()
		self._sysrqActive = False
		self._run = None
		self._startWaiting = False
		if self.capturing:
			self.capturing = False
			self._note("let go", reason)
		if reason is not None:
			self._sendClient(struct.pack("<BB", RELEASED, reason))

	def _dropClient(self, sendGoodbye: tuple[int, str] | None, answerTyped: bool = True) -> None:
		self._letGo(None, dropUndecided=False, answerTyped=answerTyped)
		if self._client is None:
			return
		if sendGoodbye is not None:
			reason, text = sendGoodbye
			self._sendClient(struct.pack("<BBH", GOODBYE, reason, 1) + text.encode())
			self._note("goodbye", reason)
		self._client.close()
		self._client = None
		self.admitted.clear()
		self.gone.set()

	def _serve(self) -> None:
		while not self._stopping:
			with self._lock:
				now = time.monotonic()
				self._tryCapture(now)
				self._advance(now)
				if self._client is not None and now - self._lastHeard > self.heartbeatTimeout:
					self._dropClient(sendGoodbye=(GOODBYE_SILENT, "no heartbeat"))
				if (
					self._client is not None
					and self.capturing
					and self._sink is not None
					and now >= self._nextLights
				):
					self._nextLights = now + 0.05
					lights = self._sink.lights()
					if lights != self._lights:
						self._lights = lights
						self._sendClient(struct.pack("<BH", LOCKS, lights))
				watched = [self._wakeRead, *self._waiting]
				if self._listener is not None:
					watched.append(self._listener)
				if self._client is not None:
					watched.append(self._client)
				timeout = 0.02 if self._client is not None else 0.2
				# Only the head's bound puts anything out; a press behind it past its own waits for it.
				head = self._line[0] if self._line else None
				if head is not None and head.press and head.verdict is None:
					timeout = max(0.0, min(timeout, head.arrival + head.bound - now))
			readable, _w, _x = select.select(watched, [], [], timeout)
			with self._lock:
				if self._wakeRead in readable:
					os.read(self._wakeRead, 4096)
				if self._listener is not None and self._listener in readable:
					try:
						caller, _address = self._listener.accept()
						caller.setblocking(False)
						self._waiting.append(caller)
					except OSError:
						pass
				client = self._client
				for caller in [c for c in self._waiting if c in readable]:
					self._waiting.remove(caller)
					self._admit(caller)
				if client is not None and client is self._client and client in readable:
					self._readClient()

	def _admit(self, caller: socket.socket) -> None:
		try:
			data = caller.recv(256)
		except BlockingIOError:
			self._waiting.append(caller)
			return
		except OSError:
			caller.close()
			return
		if len(data) != 7 or data[0] != HELLO:
			caller.close()
			return
		asked = struct.unpack("<BHI", data)[2]
		if not self.answering:
			self._waiting.append(caller)
			return
		if self.refusal is not None:
			reason, text = self.refusal
			self._send(caller, struct.pack("<BBH", GOODBYE, reason, 1) + text.encode())
			self._note("refused", reason)
			caller.close()
			return
		if self._client is not None:
			self._send(caller, struct.pack("<BBH", GOODBYE, GOODBYE_BUSY, 1) + b"another client")
			self._note("refused", GOODBYE_BUSY)
			caller.close()
			return
		self._client = caller
		self.granted = asked & self.features
		self._told.clear()
		self._releaseOf.clear()
		self._pressOf.clear()
		self._keptSeqs.clear()
		self._latePassNamed.clear()
		self._lastHeard = time.monotonic()
		self._lights = None
		self.gone.clear()
		ms = [
			int(x * 1000)
			for x in (
				self.announcedVerdictTimeout or self.verdictTimeout,
				self.heartbeatInterval,
				self.announcedHeartbeatTimeout or self.heartbeatTimeout,
				self.unansweredSpan,
			)
		]
		self._sendClient(struct.pack("<BHIHHHH", WELCOME, 1, self.granted, *ms))
		self._note("admitted")
		self.admitted.set()

	def _tryCapture(self, now: float) -> None:
		# A key down as the grab is taken would stay down in the desktop for ever.
		if self._startWaiting and not self._down and now >= self._captureAt:
			self._capture()

	def _capture(self) -> None:
		self._startWaiting = False
		self.capturing = True
		self._sendDevice(DEVICE_HELD)
		self._lights = None
		self._nextLights = 0.0

	def _readClient(self) -> None:
		try:
			data = self._client.recv(256)
		except BlockingIOError:
			return
		except OSError:
			data = b""
		if not data:
			self._note("client closed")
			self._dropClient(sendGoodbye=None)
			return
		now = time.monotonic()
		self._lastHeard = now
		kind = data[0]
		if kind == HEARTBEAT and len(data) == 5:
			self.heartbeats.append(now)
			self._sendClient(struct.pack("<BI", HEARTBEAT_ECHO, struct.unpack("<BI", data)[1]))
		elif kind == START and len(data) == 1:
			self.starts += 1
			self._note("start")
			if not self.capturing:
				self._startWaiting = True
				# The virtual keyboard settles for the desktop to take it up, once in the helper's life.
				self._captureAt = now + self.settle
				self.settle = 0.0
				self._tryCapture(now)
		elif kind == STOP and len(data) == 1:
			self._note("stop")
			self._letGo(RELEASED_STOPPED, dropUndecided=False)
		elif (
			kind == VERDICT
			and len(data) == 10
			and (
				data[9] in (VERDICT_PASS, VERDICT_KEEP)
				or (data[9] == VERDICT_WAIT and self.granted & FEATURE_WAIT)
			)
		):
			_kind, seq, value = struct.unpack("<BQB", data)
			self.given.setdefault(seq, []).append(value)
			for entry in self._line:
				# A press waits for its verdict until it is taken out of line: at its bound, or later,
				# behind a press before it that holds the line; until then a verdict counts, as the helper
				# reads what came before it looks at the bounds.
				if entry.seq == seq and entry.press and entry.verdict is None:
					if value == VERDICT_WAIT:
						# Only the first counts.
						if not entry.waited:
							entry.waited = True
							entry.bound = self.waitTimeout
							self.waits.append((seq, now - entry.arrival))
							self._note("wait", seq)
						break
					entry.verdict = bool(value)
					if entry.verdict:
						self._keptSeqs.add(seq)
					self.verdicts.append((seq, bool(value), now - entry.arrival))
					self._note("verdict", seq, bool(value))
					break
			else:
				self._note("late verdict", seq, value)
			self._advance(now)
		elif kind == TYPE and len(data) == 9 and self.granted & FEATURE_TYPING:
			_kind, tag, evdev, value, mode = struct.unpack("<BIHBB", data)
			if value not in (0, 1) or mode not in (TYPE_DECIDE, TYPE_INJECT) or not isVirtualKey(evdev):
				self._malformed(data)
				return
			self._type(tag, evdev, value == 1, mode, now)
		elif kind == LATE_PASS and len(data) == 17 and self.granted & FEATURE_LATE_PASS:
			_kind, kept, waiting = struct.unpack("<BQQ", data)
			if not self._latePassWellFormed(kept, waiting):
				self._malformed(data)
				return
			self._latePass(kept, waiting)
			self._advance(now)
		else:
			self._malformed(data)

	def _malformed(self, data: bytes) -> None:
		# The type alone: the rest of a TYPE names a key.
		self._note("malformed", data[:1])
		self._dropClient(sendGoodbye=(GOODBYE_MALFORMED, "malformed"))

	# Typing, with FEATURE_TYPING.

	def _sendTyped(self, entry: _Entry | None, outcome: int, tag: int | None = None) -> None:
		"""TYPED for the typed key entry, or for a refused one, which never had an entry, by its tag."""
		tag = entry.tag if entry is not None else tag
		seq = 0 if entry is None or entry.injected else entry.seq
		self.typed.append((tag, seq, outcome))
		self._note("typed", tag, seq, outcome)
		self._sendClient(struct.pack("<BIQB", TYPED, tag, seq, outcome))

	def _sysrqRefuses(self, evdev: int) -> bool:
		"""Whether the SysRq rules refuse a typed press of evdev now."""
		down = self._virtualDown.keys()
		return (
			self._sysrqActive or (evdev == SYSRQ and bool(ALTS & down)) or (evdev in ALTS and SYSRQ in down)
		)

	def _typeRefusal(self, evdev: int, down: bool, now: float) -> int | None:
		if not self.capturing or self._client is None:
			return TYPED_REFUSED_STATE
		if evdev not in typableCodes() or (down and self._sysrqRefuses(evdev)):
			return TYPED_REFUSED_KEY
		if not down and evdev not in self._typedDown:
			return TYPED_NOT_DOWN
		self._typedTokens = min(
			float(TYPED_BURST), self._typedTokens + (now - self._typedFilled) * TYPED_PER_SECOND
		)
		self._typedFilled = now
		waiting = sum(1 for entry in self._line if entry.tag is not None)
		if waiting >= TYPED_WAITING_LIMIT or self._typedTokens < 1:
			return TYPED_REFUSED_FULL
		return None

	def _type(self, tag: int, evdev: int, down: bool, mode: int, now: float) -> None:
		refusal = self._typeRefusal(evdev, down, now)
		if refusal is not None:
			self._sendTyped(None, refusal, tag=tag)
			return
		self._typedTokens -= 1
		if down:
			self._typedDown.add(evdev)
		else:
			self._typedDown.discard(evdev)
		if mode == TYPE_DECIDE:
			self._enter(
				evdev, PRESS if down else RELEASE, KEY_TYPED | (KEY_ASK if down else 0), TYPED_DEVICE, tag
			)
			return
		self._note("inject", tag)
		self._line.append(
			_Entry(
				0,
				evdev,
				down,
				now,
				verdict=False if down else None,
				device=TYPED_DEVICE,
				tag=tag,
				injected=True,
			)
		)
		self._advance(now)

	# The late pass, with FEATURE_LATE_PASS.

	def _latePassWellFormed(self, kept: int, waiting: int) -> bool:
		first, second = self._told.get(kept), self._told.get(waiting)
		if first is None or second is None:
			return False
		_device, evdev, value, flags = first
		if value != PRESS or evdev not in MODIFIERS - {CAPS_LOCK} or flags & KEY_TYPED:
			return False
		if kept in self._latePassNamed:
			return False
		_device, _evdev, value, flags = second
		if value != PRESS or not flags & KEY_ASK:
			return False
		self._latePassNamed.add(kept)
		return True

	def _latePass(self, kept: int, waiting: int) -> None:
		release = self._releaseOf.get(kept)
		downAtWaiting = kept < waiting and (release is None or release > waiting)
		entry = next((e for e in self._line if e.seq == waiting and e.press and e.verdict is None), None)
		applies = kept in self._keptSeqs and downAtWaiting and entry is not None
		if applies:
			device, evdev, _value, _flags = self._told[kept]
			entry.lateKept.append((device, evdev))
		self.latePasses.append((kept, waiting, applies))
		self._note("late pass", kept, waiting, applies)
