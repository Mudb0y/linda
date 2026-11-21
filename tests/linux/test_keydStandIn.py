"""The stand-in for linda-keyd with a sink in place of the X display: with NullSink it opens no X
connection, puts out what its client passed and nothing it kept, records each verdict by sequence
number, and reports the lights its own layout gives. A client written here from the protocol's text
drives it, so no Linda runs. Run under the rig:

	nix develop . -c tests/linux/rig python3 -m pytest tests/linux/test_keydStandIn.py
"""

import socket
import struct
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import keydStandIn
import pytest
from keydStandIn import (
	DEVICE,
	HELLO,
	KEY,
	LOCKS,
	START,
	VERDICT,
	VERDICT_KEEP,
	VERDICT_PASS,
	WELCOME,
	NullSink,
	StandIn,
	code,
)
from Xlib import display

#: The lights as LOCKS carries them.
NUM, CAPS, SCROLL = 1, 2, 4


class Client:
	"""The helper's client, answering each press it is asked about with decide(code)."""

	def __init__(self, path: str, decide: Callable[[int], int]) -> None:
		self.decide = decide
		self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
		self.sock.connect(path)
		self.sock.send(struct.pack("<BHI", HELLO, 1, 0))
		self.sock.settimeout(5)
		welcome = self.sock.recv(256)
		assert welcome[0] == WELCOME, welcome
		self.sock.settimeout(0.05)
		#: (seq, code, value) of each KEY.
		self.keys: list[tuple[int, int, int]] = []
		#: Each LOCKS, in order.
		self.locks: list[int] = []
		self.held = threading.Event()
		self._stopping = False
		self._thread = threading.Thread(target=self._read, daemon=True)
		self._thread.start()
		self.sock.send(bytes((START,)))

	def _read(self) -> None:
		while not self._stopping:
			try:
				data = self.sock.recv(256)
			except TimeoutError:
				continue
			except OSError:
				return
			if not data:
				return
			if data[0] == KEY:
				_kind, seq, _device, evdev, value, flags = struct.unpack("<BQIHBB", data)
				self.keys.append((seq, evdev, value))
				if flags & keydStandIn.KEY_ASK:
					self.sock.send(struct.pack("<BQB", VERDICT, seq, self.decide(evdev)))
			elif data[0] == LOCKS:
				self.locks.append(struct.unpack("<BH", data)[1])
			elif data[0] == DEVICE:
				self.held.set()

	def pressSeq(self, evdev: int) -> list[int]:
		return [seq for seq, key, value in self.keys if key == evdev and value == keydStandIn.PRESS]

	def close(self) -> None:
		self._stopping = True
		self._thread.join(2)
		self.sock.close()


@pytest.fixture
def noX(monkeypatch: pytest.MonkeyPatch) -> list[tuple]:
	"""No X display to be had: every attempt to open one is recorded, and fails."""
	tried = []

	def refuse(*args, **kwargs):
		tried.append(args)
		raise AssertionError("the stand-in opened an X connection")

	monkeypatch.delenv("DISPLAY", raising=False)
	monkeypatch.setattr(display, "Display", refuse)
	return tried


@pytest.fixture
def socketPath() -> Iterator[str]:
	with tempfile.TemporaryDirectory(prefix="keyd", dir="/tmp") as socketDir:
		yield str(Path(socketDir) / "socket")


def serve(path: str, sink: NullSink, decide: Callable[[int], int]) -> tuple[StandIn, Client]:
	keyboard = StandIn(path, sink=sink, heartbeatTimeout=60).start()
	client = Client(path, decide)
	assert client.held.wait(5), "the stand-in never held the keyboard"
	return keyboard, client


def test_nullSinkPutsOutWhatIsPassedAndRecordsTheKeeps(noX: list, socketPath: str):
	sink = NullSink("pl")
	insert, a = code("Insert"), code("a")
	keyboard, client = serve(
		socketPath, sink, lambda evdev: VERDICT_KEEP if evdev == insert else VERDICT_PASS
	)
	try:
		keyboard.press("Insert+a", "a")
		keyboard.waitFor(lambda: len(sink.sent) == 4 and len(sink.kept) == 2)
		time.sleep(0.2)
	finally:
		client.close()
		keyboard.close()
	assert noX == []
	assert keyboard.emitted() == [(a, True), (a, False), (a, True), (a, False)]
	assert sink.passed() == keyboard.emitted()
	assert all(virtual for _code, _down, virtual in sink.sent)
	assert sink.kept == [(insert, True), (insert, False)]
	[insertSeq] = client.pressSeq(insert)
	assert keyboard.given[insertSeq] == [VERDICT_KEEP]
	assert all(keyboard.given[seq] == [VERDICT_PASS] for seq in client.pressSeq(a))
	assert sink.closed


def test_nullSinkLightsFollowThePassedLocks(noX: list, socketPath: str):
	sink = NullSink("pl")
	keptOnce = {"Caps": True}

	def decide(evdev: int) -> int:
		# The first Caps Lock is kept, which a desktop never sees, so it flips nothing.
		if evdev == code("Caps_Lock") and keptOnce.pop("Caps", False):
			return VERDICT_KEEP
		return VERDICT_PASS

	keyboard, client = serve(socketPath, sink, decide)
	try:
		keyboard.waitFor(lambda: client.locks == [0])
		keyboard.press("Caps_Lock")
		time.sleep(0.3)
		keptLocks = list(client.locks)
		keyboard.press("Caps_Lock")
		keyboard.waitFor(lambda: client.locks[-1] == CAPS)
		keyboard.press("Num_Lock")
		keyboard.waitFor(lambda: client.locks[-1] == CAPS | NUM)
		keyboard.press("Caps_Lock")
		keyboard.waitFor(lambda: client.locks[-1] == NUM)
		keyboard.press("Scroll_Lock")
		keyboard.waitFor(lambda: client.locks[-1] == NUM | SCROLL)
	finally:
		client.close()
		keyboard.close()
	assert noX == []
	assert keptLocks == [0]
	assert client.locks == [0, CAPS, CAPS | NUM, NUM, NUM | SCROLL]
	assert sink.lights() == NUM | SCROLL


def test_nullSinkTakesItsLocksFromItsOwnLayout(noX: list, socketPath: str):
	sink = NullSink("pl", options="caps:escape")
	keyboard, client = serve(socketPath, sink, lambda evdev: VERDICT_PASS)
	try:
		keyboard.waitFor(lambda: client.locks == [0])
		keyboard.press("Caps_Lock")
		keyboard.waitFor(lambda: sink.passed() == [(code("Caps_Lock"), True), (code("Caps_Lock"), False)])
		time.sleep(0.3)
	finally:
		client.close()
		keyboard.close()
	assert noX == []
	# Under caps:escape the key is Escape, which lights nothing.
	assert client.locks == [0]
	assert sink.lights() == 0


def test_nullSinkHasTheKeysOfAKeyboardNothingHolds(noX: list, socketPath: str):
	sink = NullSink()
	with StandIn(socketPath, sink=sink) as keyboard:
		keyboard.press("Caps_Lock", "a")
	assert noX == []
	assert sink.sent == [
		(code("Caps_Lock"), True, False),
		(code("Caps_Lock"), False, False),
		(code("a"), True, False),
		(code("a"), False, False),
	]
	assert sink.lights() == CAPS


def test_aSinkIsRefusedWithoutX(socketPath: str):
	with pytest.raises(ValueError):
		StandIn(socketPath, sink=NullSink(), toX=False)


def test_theDefaultSinkIsTheXDisplay(socketPath: str):
	keyboard = StandIn(socketPath)
	try:
		assert isinstance(keyboard._sink, keydStandIn._XKeyboard)
	finally:
		keyboard.close()
	keyboard = StandIn(socketPath, toX=False)
	try:
		assert keyboard._sink is None
	finally:
		keyboard.close()
