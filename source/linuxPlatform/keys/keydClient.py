"""The client's half of the protocol linda-keyd speaks, the helper that takes the keyboards in the
kernel for Linda: engine/crates/keyd/src/protocol.rs is the whole of it, with each message's layout
as the struct format used here, and why every time bound is what it is.

Nothing here imports NVDA, so the protocol can be tested on its own.
"""

import socket
import struct
import time
from dataclasses import dataclass

SOCKET_PATH = "/run/linda-keyd/socket"
PROTOCOL_VERSION = 1
#: The largest message either side sends.
MAX_MESSAGE = 256

HELLO, START, STOP, VERDICT, HEARTBEAT = 0x01, 0x02, 0x03, 0x04, 0x05
WELCOME, GOODBYE, KEY, EXPIRED, RELEASED, DEVICE, LOCKS, HEARTBEAT_ECHO = range(0x81, 0x89)

_HELLO = struct.Struct("<BHI")
_VERDICT = struct.Struct("<BQB")
_HEARTBEAT = struct.Struct("<BI")
_WELCOME = struct.Struct("<BHIHHHH")
_GOODBYE = struct.Struct("<BBH")
_KEY = struct.Struct("<BQIHBB")
_EXPIRED = struct.Struct("<BQ")
_RELEASED = struct.Struct("<BB")
_DEVICE = struct.Struct("<BIBB")
_LOCKS = struct.Struct("<BH")

KEY_RELEASE, KEY_PRESS, KEY_REPEAT = 0, 1, 2
#: KEY's flags: the press wants a verdict; the helper made the event, as the release of a key held on
#: a keyboard that vanished; the client typed the key.
KEY_ASK, KEY_SYNTHETIC, KEY_TYPED = 0x01, 0x02, 0x04
#: LOCKS's bits, the kernel's LED codes.
LOCK_NUM, LOCK_CAPS, LOCK_SCROLL = 1 << 0, 1 << 1, 1 << 2

#: RELEASED's reasons.
RELEASED_STOPPED, RELEASED_EMERGENCY, RELEASED_BACKGROUND, RELEASED_VIRTUAL, RELEASED_SESSION_UNKNOWN = range(
	1, 6
)
#: GOODBYE's reasons.
(
	GOODBYE_VERSION,
	GOODBYE_NOT_IN_FRONT,
	GOODBYE_NOT_AUTHORIZED,
	GOODBYE_BUSY,
	GOODBYE_NO_ANSWER,
	GOODBYE_SILENT,
	GOODBYE_MALFORMED,
	GOODBYE_UNANSWERED,
	GOODBYE_REPLACED,
	GOODBYE_OVERFLOW,
	GOODBYE_TOO_MANY,
) = range(1, 12)
#: DEVICE's statuses.
DEVICE_HELD, DEVICE_WAITING, DEVICE_BUSY, DEVICE_GONE, DEVICE_IGNORED = range(1, 6)

GOODBYE_NAMES = {
	GOODBYE_VERSION: "it speaks another version of the protocol",
	GOODBYE_NOT_IN_FRONT: "NVDA is not a process of the session in front",
	GOODBYE_NOT_AUTHORIZED: "polkit does not allow it",
	GOODBYE_BUSY: "it serves another client of this session",
	GOODBYE_NO_ANSWER: "logind or polkit did not answer it in time",
	GOODBYE_SILENT: "NVDA was silent too long",
	GOODBYE_MALFORMED: "NVDA sent it a message the protocol does not allow",
	GOODBYE_UNANSWERED: "NVDA left keys unanswered",
	GOODBYE_REPLACED: "a client of the session in front took NVDA's place",
	GOODBYE_OVERFLOW: "NVDA did not read what it sent",
	GOODBYE_TOO_MANY: "too many clients were waiting",
}
RELEASED_NAMES = {
	RELEASED_STOPPED: "NVDA asked it to",
	RELEASED_EMERGENCY: "the emergency chord, Backspace, Escape and Enter, was pressed",
	RELEASED_BACKGROUND: "this session is no longer the one in front",
	RELEASED_VIRTUAL: "its virtual keyboard failed",
	RELEASED_SESSION_UNKNOWN: "it cannot tell which session is in front",
}
DEVICE_NAMES = {
	DEVICE_HELD: "held",
	DEVICE_WAITING: "waiting for its keys to be up",
	DEVICE_BUSY: "held by another program",
	DEVICE_GONE: "gone",
	DEVICE_IGNORED: "left alone",
}


@dataclass(frozen=True)
class Welcome:
	version: int
	features: int
	#: Seconds: the most a press waits for its verdict, how often to send something, the silence after
	#: which the helper lets go, and the span of unanswered presses after which it does.
	verdictTimeout: float
	heartbeatInterval: float
	heartbeatTimeout: float
	unansweredSpan: float


@dataclass(frozen=True)
class Goodbye:
	reason: int
	version: int
	text: str

	def describe(self) -> str:
		why = GOODBYE_NAMES.get(self.reason, f"reason {self.reason}")
		return f"{why} ({self.text})" if self.text else why


@dataclass(frozen=True)
class Key:
	seq: int
	device: int
	code: int
	value: int
	flags: int


@dataclass(frozen=True)
class Expired:
	seq: int


@dataclass(frozen=True)
class Released:
	reason: int


@dataclass(frozen=True)
class Device:
	device: int
	status: int
	detail: int
	name: str


@dataclass(frozen=True)
class Locks:
	leds: int


@dataclass(frozen=True)
class HeartbeatEcho:
	counter: int


Message = Welcome | Goodbye | Key | Expired | Released | Device | Locks | HeartbeatEcho


class Malformed(ValueError):
	"""A message from the helper that breaks the protocol."""


class Refused(Exception):
	"""The helper turned NVDA away, or could not be asked."""

	def __init__(self, reason: str, goodbye: Goodbye | None = None) -> None:
		super().__init__(reason)
		self.goodbye = goodbye


def _fixed(layout: struct.Struct, data: bytes, name: str) -> tuple:
	if len(data) != layout.size:
		raise Malformed(f"{name} of {len(data)} bytes, which should be {layout.size}")
	return layout.unpack(data)


def _text(data: bytes, start: int, name: str) -> str:
	try:
		return data[start:].decode("utf-8")
	except UnicodeDecodeError:
		raise Malformed(f"{name} with text that is not UTF-8") from None


def decode(data: bytes) -> Message | None:
	"""The helper's message, or None for a type this client does not know, which it ignores.

	:raise Malformed: When the message breaks the protocol.
	"""
	if not data:
		raise Malformed("an empty message")
	kind = data[0]
	if kind == KEY:
		_kind, seq, device, code, value, flags = _fixed(_KEY, data, "KEY")
		return Key(seq, device, code, value, flags)
	if kind == EXPIRED:
		return Expired(_fixed(_EXPIRED, data, "EXPIRED")[1])
	if kind == HEARTBEAT_ECHO:
		return HeartbeatEcho(_fixed(_HEARTBEAT, data, "HEARTBEAT")[1])
	if kind == LOCKS:
		return Locks(_fixed(_LOCKS, data, "LOCKS")[1])
	if kind == RELEASED:
		return Released(_fixed(_RELEASED, data, "RELEASED")[1])
	if kind == DEVICE:
		if len(data) < _DEVICE.size:
			raise Malformed(f"DEVICE of {len(data)} bytes")
		_kind, device, status, detail = _DEVICE.unpack_from(data)
		return Device(device, status, detail, _text(data, _DEVICE.size, "DEVICE"))
	if kind == GOODBYE:
		if len(data) < _GOODBYE.size:
			raise Malformed(f"GOODBYE of {len(data)} bytes")
		_kind, reason, version = _GOODBYE.unpack_from(data)
		return Goodbye(reason, version, _text(data, _GOODBYE.size, "GOODBYE"))
	if kind == WELCOME:
		_kind, version, features, verdict, interval, timeout, span = _fixed(_WELCOME, data, "WELCOME")
		return Welcome(version, features, verdict / 1000, interval / 1000, timeout / 1000, span / 1000)
	return None


def hello(features: int = 0) -> bytes:
	return _HELLO.pack(HELLO, PROTOCOL_VERSION, features)


def start() -> bytes:
	return bytes((START,))


def stop() -> bytes:
	return bytes((STOP,))


def verdict(seq: int, keep: bool) -> bytes:
	return _VERDICT.pack(VERDICT, seq, int(keep))


def heartbeat(counter: int) -> bytes:
	return _HEARTBEAT.pack(HEARTBEAT, counter & 0xFFFFFFFF)


def connect(path: str, timeout: float) -> tuple[socket.socket, Welcome]:
	"""Connect to the helper and be admitted.

	:param timeout: Seconds to wait for its answer: logind and polkit are asked first.
	:return: The connection, non-blocking, and the helper's welcome.
	:raise Refused: When the helper is not there, turns NVDA away or does not answer in time.
	"""
	sock = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET | socket.SOCK_CLOEXEC)
	try:
		try:
			sock.settimeout(timeout)
			sock.connect(path)
			sock.send(hello())
			deadline = time.monotonic() + timeout
			while True:
				sock.settimeout(max(0.001, deadline - time.monotonic()))
				data = sock.recv(MAX_MESSAGE)
				if not data:
					raise Refused("it closed the connection without an answer")
				message = decode(data)
				if isinstance(message, Welcome):
					break
				if isinstance(message, Goodbye):
					raise Refused(message.describe(), message)
				# Nothing else comes before the answer; anything that did is no reason to give up.
		except TimeoutError:
			raise Refused(f"it did not answer within {timeout:g} seconds") from None
		except Malformed as e:
			raise Refused(f"its answer broke the protocol: {e}") from None
		except OSError as e:
			raise Refused(e.strerror or str(e)) from None
	except BaseException:
		sock.close()
		raise
	sock.setblocking(False)
	return sock, message
