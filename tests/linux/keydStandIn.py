"""A stand-in for linda-keyd, for tests in the rig, which has no input devices.

It speaks the helper's protocol, as engine/crates/keyd/src/protocol.rs gives it, on a socket of its
own, and is the keyboard as well: a test presses keys on it as a person would on a real one. While
its client has it capture, each key goes to the client and in line, and what the client passes, or
does not answer in time, goes on to the X display on the display's own keyboard device, as the
helper's virtual keyboard would; the rest goes nowhere. While it does not capture, keys go straight
to the X display, as they do from a keyboard nothing holds. It sends the lights of the locks as the X
display sets them, reads the emergency chord itself, lets go of everything when its client falls
silent, and can be made to refuse, say goodbye, let go, lose its keyboard or vanish.

It is written from the protocol's documentation, not from Linda's client, so that the two are held
to the same text rather than to each other.
"""

import os
import select
import socket
import struct
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Self

from Xlib import XK, display
from Xlib.ext import xinput
from Xlib.protocol import rq

HELLO, START, STOP, VERDICT, HEARTBEAT = 0x01, 0x02, 0x03, 0x04, 0x05
WELCOME, GOODBYE, KEY, EXPIRED, RELEASED, DEVICE, LOCKS, HEARTBEAT_ECHO = range(0x81, 0x89)
KEY_ASK, KEY_SYNTHETIC = 0x01, 0x02
RELEASE, PRESS, REPEAT = 0, 1, 2
RELEASED_STOPPED, RELEASED_EMERGENCY, RELEASED_BACKGROUND, RELEASED_VIRTUAL, RELEASED_SESSION_UNKNOWN = range(
	1, 6
)
GOODBYE_BUSY, GOODBYE_SILENT, GOODBYE_UNANSWERED = 4, 6, 8
DEVICE_HELD, DEVICE_GONE = 1, 4
#: Backspace, Escape and Enter.
CHORD = frozenset((14, 1, 28))
#: The stand-in's keyboard, as the kernel would number it.
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
	"ISO_Level3_Shift": 100,
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
	"F11": 87,
	"F12": 88,
}
CODES.update({f"F{number}": 58 + number for number in range(1, 11)})
CODES.update({str(digit): 2 + (digit - 1) % 10 for digit in range(1, 11)})
CODES["0"] = 11
for _row, _first in (("qwertyuiop", 16), ("asdfghjkl", 30), ("zxcvbnm", 44)):
	CODES.update({letter: _first + index for index, letter in enumerate(_row)})


def code(name: str) -> int:
	return CODES[name] if name in CODES else CODES[name.lower()]


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


class _XKeyboard:
	"""The X display's own keyboard device, which keys reach the X display on: the real keyboard's
	keys while nothing holds it, and the virtual keyboard's otherwise."""

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
		self._lock = threading.Lock()

	def send(self, evdev: int, down: bool) -> None:
		with self._lock:
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


@dataclass
class _Entry:
	seq: int
	code: int
	press: bool
	arrival: float
	verdict: bool | None = None


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
		toX: bool = True,
		announcedVerdictTimeout: float | None = None,
	) -> None:
		"""
		:param toX: Whether keys go on to the X display; without, they go nowhere, and only what the
			client did with them is recorded.
		:param announcedVerdictTimeout: The bound WELCOME gives, when it is to be another than the one
			kept, as for a client that cannot keep it.
		"""
		self.announcedVerdictTimeout = announcedVerdictTimeout
		self.path = str(path)
		self.verdictTimeout = verdictTimeout
		self.heartbeatInterval = heartbeatInterval
		self.heartbeatTimeout = heartbeatTimeout
		self.unansweredSpan = unansweredSpan
		self._x = _XKeyboard() if toX else None
		self._lock = threading.RLock()
		self._wakeRead, self._wakeWrite = os.pipe()
		self._listener: socket.socket | None = None
		self._client: socket.socket | None = None
		self._waiting: list[socket.socket] = []
		self._thread: threading.Thread | None = None
		self._stopping = False
		self._seq = 0
		self._line: deque[_Entry] = deque()
		#: Codes physically down, and of those, the ones whose press was kept, and those the virtual
		#: keyboard holds down.
		self._down: set[int] = set()
		self._kept: set[int] = set()
		self._virtualDown: set[int] = set()
		self._lastHeard = 0.0
		self._unansweredSince: float | None = None
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
		self.heartbeats: list[float] = []
		self.starts = 0
		self.admitted = threading.Event()
		self.gone = threading.Event()

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
		if self._x is not None:
			for evdev in sorted(self._down):
				self._x.send(evdev, False)
			self._x.close()
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

	def down(self, name: str | int) -> None:
		evdev = name if isinstance(name, int) else code(name)
		with self._lock:
			if evdev in self._down:
				return
			self._down.add(evdev)
			if not self.capturing:
				self._toX(evdev, True)
				return
			if CHORD <= self._down:
				self._note("emergency")
				self._letGo(RELEASED_EMERGENCY, dropUndecided=True)
				return
			self._enter(evdev, PRESS, KEY_ASK)
		self._wake()

	def up(self, name: str | int) -> None:
		evdev = name if isinstance(name, int) else code(name)
		with self._lock:
			if evdev not in self._down:
				return
			self._down.discard(evdev)
			if not self.capturing:
				self._toX(evdev, False)
				if self._startWaiting and not self._down:
					self._capture()
				return
			self._enter(evdev, RELEASE, 0)
		self._wake()

	def repeat(self, name: str | int) -> None:
		"""One repeat of a key held down, as the kernel's own repeat sends them."""
		evdev = name if isinstance(name, int) else code(name)
		with self._lock:
			if evdev in self._down and self.capturing:
				self._enter(evdev, REPEAT, 0)

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

	def _toX(self, evdev: int, down: bool) -> None:
		self._note("x", evdev, down)
		if self._x is not None:
			self._x.send(evdev, down)

	# What the stand-in does as the helper.

	def keyboardGone(self) -> None:
		"""The keyboard vanishes with keys down: each gets a release made by the helper, in line."""
		with self._lock:
			for evdev in sorted(self._down):
				self._enter(evdev, RELEASE, KEY_SYNTHETIC)
			self._down.clear()
			self._sendDevice(DEVICE_GONE)
		self._wake()

	def letGo(self, reason: int) -> None:
		with self._lock:
			self._letGo(reason, dropUndecided=False)

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
			self._dropClient(sendGoodbye=None)
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
		"""(code, down) of each key the X display had from the stand-in, captured or not."""
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

	def _sendDevice(self, status: int) -> None:
		self._sendClient(struct.pack("<BIBB", DEVICE, DEVICE_NUMBER, status, 0) + DEVICE_NAME.encode())

	def _enter(self, evdev: int, value: int, flags: int) -> None:
		self._seq += 1
		now = time.monotonic()
		if value != REPEAT:
			self._line.append(_Entry(self._seq, evdev, value == PRESS, now))
		self._note("key", self._seq, evdev, value, flags)
		self._sendClient(struct.pack("<BQIHBB", KEY, self._seq, DEVICE_NUMBER, evdev, value, flags))
		self._advance(now)

	def _advance(self, now: float) -> None:
		"""Put out every key at the head of the line that is decided or overdue."""
		while self._line:
			head = self._line[0]
			if head.press and head.verdict is None:
				if now - head.arrival < self.verdictTimeout:
					return
				head.verdict = False
				self._note("expired", head.seq)
				self._sendClient(struct.pack("<BQ", EXPIRED, head.seq))
				if self._unansweredSince is None:
					self._unansweredSince = head.arrival
				elif now - self._unansweredSince >= self.unansweredSpan:
					self._line.popleft()
					self._virtual(head.code, True)
					self._dropClient(sendGoodbye=(GOODBYE_UNANSWERED, "presses went unanswered"))
					return
			self._line.popleft()
			if head.press:
				if head.verdict:
					self._kept.add(head.code)
				else:
					self._kept.discard(head.code)
					self._virtual(head.code, True)
			elif head.code in self._kept:
				self._kept.discard(head.code)
			else:
				self._virtual(head.code, False)

	def _virtual(self, evdev: int, down: bool) -> None:
		if down == (evdev in self._virtualDown):
			return
		if down:
			self._virtualDown.add(evdev)
		else:
			self._virtualDown.discard(evdev)
		self._toX(evdev, down)

	def _letGo(self, reason: int | None, dropUndecided: bool) -> None:
		if dropUndecided:
			self._line.clear()
		else:
			for entry in self._line:
				if entry.press and entry.verdict is None:
					# Passed on, as no verdict is coming.
					entry.verdict = False
			self._advance(time.monotonic())
		for evdev in sorted(self._virtualDown):
			self._virtual(evdev, False)
		self._kept.clear()
		self._unansweredSince = None
		self._startWaiting = False
		if self.capturing:
			self.capturing = False
			self._note("let go", reason)
		if reason is not None:
			self._sendClient(struct.pack("<BB", RELEASED, reason))

	def _dropClient(self, sendGoodbye: tuple[int, str] | None) -> None:
		self._letGo(None, dropUndecided=False)
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
				self._advance(now)
				if self._client is not None and now - self._lastHeard > self.heartbeatTimeout:
					self._dropClient(sendGoodbye=(GOODBYE_SILENT, "no heartbeat"))
				if (
					self._client is not None
					and self.capturing
					and self._x is not None
					and now >= self._nextLights
				):
					self._nextLights = now + 0.05
					lights = self._x.lights()
					if lights != self._lights:
						self._lights = lights
						self._sendClient(struct.pack("<BH", LOCKS, lights))
				watched = [self._wakeRead, *self._waiting]
				if self._listener is not None:
					watched.append(self._listener)
				if self._client is not None:
					watched.append(self._client)
				timeout = 0.02 if self._client is not None else 0.2
				undecided = [e.arrival for e in self._line if e.press and e.verdict is None]
				if undecided:
					timeout = max(0.0, min(timeout, undecided[0] + self.verdictTimeout - now))
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
		self._lastHeard = time.monotonic()
		self._lights = None
		self.gone.clear()
		ms = [
			int(x * 1000)
			for x in (
				self.announcedVerdictTimeout or self.verdictTimeout,
				self.heartbeatInterval,
				self.heartbeatTimeout,
				self.unansweredSpan,
			)
		]
		self._sendClient(struct.pack("<BHIHHHH", WELCOME, 1, 0, *ms))
		self._note("admitted")
		self.admitted.set()

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
				if self._down:
					# A key down as the grab is taken would stay down in the desktop for ever.
					self._startWaiting = True
				else:
					self._capture()
		elif kind == STOP and len(data) == 1:
			self._note("stop")
			self._letGo(RELEASED_STOPPED, dropUndecided=False)
		elif kind == VERDICT and len(data) == 10:
			_kind, seq, keep = struct.unpack("<BQB", data)
			self._advance(now)
			for entry in self._line:
				# Each press's own bound runs from its reading, wherever it is in line.
				if (
					entry.seq == seq
					and entry.press
					and entry.verdict is None
					and now - entry.arrival < self.verdictTimeout
				):
					entry.verdict = bool(keep)
					self.verdicts.append((seq, bool(keep), now - entry.arrival))
					self._note("verdict", seq, bool(keep))
					self._unansweredSince = None
					break
			else:
				self._note("late verdict", seq, bool(keep))
			self._advance(now)
		else:
			self._note("malformed", data)
			self._dropClient(sendGoodbye=(7, "malformed"))
