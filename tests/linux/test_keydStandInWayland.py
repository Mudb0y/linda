"""The stand-in for linda-keyd under the rig's compositors, and its typing and late pass.

TYPE, TYPED and LATE_PASS are held to protocol.rs's text by a client written here from it, with
NullSink, in any session. Under Mutter and sway, MutterSink and SwaySink put the keys out on the
session's keyboard, and the GTK 4 key probe shows what a program is given. Under the X rig one test
runs this file again in a rig of each compositor:

	nix develop . -c tests/linux/rig python3 -m pytest tests/linux/test_keydStandInWayland.py
"""

import os
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import harness
import keydStandIn
import pytest
from keydStandIn import (
	DEVICE,
	FEATURE_LATE_PASS,
	FEATURE_TYPING,
	FEATURE_WAIT,
	GOODBYE,
	GOODBYE_MALFORMED,
	HELLO,
	KEY,
	KEY_ASK,
	KEY_TYPED,
	LATE_PASS,
	LOCKS,
	PRESS,
	RELEASE,
	RELEASED,
	RELEASED_EMERGENCY,
	RELEASED_STOPPED,
	START,
	STOP,
	TYPE,
	TYPE_DECIDE,
	TYPE_INJECT,
	TYPED,
	TYPED_BURST,
	TYPED_DEVICE,
	TYPED_KEPT,
	TYPED_LOST,
	TYPED_NOT_DOWN,
	TYPED_OUT,
	TYPED_PER_SECOND,
	TYPED_REFUSED_FULL,
	TYPED_REFUSED_KEY,
	TYPED_REFUSED_STATE,
	TYPED_WAITING_LIMIT,
	VERDICT,
	VERDICT_KEEP,
	VERDICT_PASS,
	WELCOME,
	NullSink,
	StandIn,
	code,
)

ALL = FEATURE_TYPING | FEATURE_WAIT | FEATURE_LATE_PASS
CAPS = 2
A, B, C, X, Q, W = (code(name) for name in "abcxqw")
SHIFT, ALT, ALTGR, TAB, INSERT = (
	code(name) for name in ("shift", "alt", "ISO_Level3_Shift", "Tab", "Insert")
)
SYSRQ, POWER, SLEEP, POWER2, BTN_LEFT = 99, 116, 142, 0x164, 0x110

compositors = pytest.mark.rigSession("wayland")

#: How a client decides a press: the verdict, or None to leave it to the test.
Decide = Callable[[int, int], int | None]


def passAll(_code: int, _flags: int) -> int | None:
	return VERDICT_PASS


class Client:
	"""linda-keyd's client, written from protocol.rs's text, which answers each press it is asked about
	with decide(code, flags) and records every message."""

	def __init__(self, path: str, features: int = ALL, decide: Decide = passAll, start: bool = True) -> None:
		self.decide = decide
		self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
		self.sock.connect(path)
		self.sock.send(struct.pack("<BHI", HELLO, 1, features))
		self.sock.settimeout(5)
		welcome = self.sock.recv(256)
		assert welcome[0] == WELCOME, welcome
		self.granted = struct.unpack("<BHIHHHH", welcome)[2]
		self.sock.settimeout(0.05)
		#: Each message, as (type, fields...), in order.
		self.messages: list[tuple] = []
		self.held = threading.Event()
		self.closed = threading.Event()
		self._stopping = False
		self._thread = threading.Thread(target=self._read, daemon=True)
		self._thread.start()
		if start:
			self.start()

	def _read(self) -> None:
		while not self._stopping:
			try:
				data = self.sock.recv(256)
			except TimeoutError:
				continue
			except OSError:
				break
			if not data:
				break
			kind = data[0]
			if kind == KEY:
				_kind, seq, device, evdev, value, flags = struct.unpack("<BQIHBB", data)
				self.messages.append((KEY, seq, device, evdev, value, flags))
				if flags & KEY_ASK and (verdict := self.decide(evdev, flags)) is not None:
					self.verdict(seq, verdict)
			elif kind == TYPED:
				self.messages.append(struct.unpack("<BIQB", data))
			elif kind == LOCKS:
				self.messages.append(struct.unpack("<BH", data))
			elif kind == RELEASED:
				self.messages.append(struct.unpack("<BB", data))
			elif kind == GOODBYE:
				self.messages.append(struct.unpack("<BBH", data[:4]))
			elif kind == DEVICE:
				self.held.set()
		self.closed.set()

	def send(self, message: bytes) -> None:
		self.sock.send(message)

	def start(self) -> None:
		self.send(bytes((START,)))

	def verdict(self, seq: int, value: int) -> None:
		self.send(struct.pack("<BQB", VERDICT, seq, value))

	def type(self, tag: int, evdev: int, down: bool, mode: int = TYPE_INJECT) -> None:
		self.send(struct.pack("<BIHBB", TYPE, tag, evdev, int(down), mode))

	def latePass(self, kept: int, waiting: int) -> None:
		self.send(struct.pack("<BQQ", LATE_PASS, kept, waiting))

	def told(self) -> list[tuple[int, int, int, int, int]]:
		"""(seq, device, code, value, flags) of each KEY."""
		return [tuple(m[1:]) for m in list(self.messages) if m[0] == KEY]

	def pressSeq(self, evdev: int) -> int:
		"""The sequence number of the last press of evdev told."""
		return [seq for seq, _d, key, value, _f in self.told() if key == evdev and value == PRESS][-1]

	def typed(self) -> list[tuple[int, int, int]]:
		"""(tag, seq, outcome) of each TYPED."""
		return [tuple(m[1:]) for m in list(self.messages) if m[0] == TYPED]

	def outcome(self, tag: int) -> int | None:
		return next((outcome for t, _seq, outcome in self.typed() if t == tag), None)

	def locks(self) -> list[int]:
		return [m[1] for m in list(self.messages) if m[0] == LOCKS]

	def goodbye(self) -> int | None:
		return next((m[1] for m in list(self.messages) if m[0] == GOODBYE), None)

	def waitFor(self, condition: Callable[[], object], timeout: float = 5.0) -> None:
		end = time.monotonic() + timeout
		while not condition():
			assert time.monotonic() < end, f"the client waited in vain; it had {self.messages[-20:]}"
			time.sleep(0.01)

	def close(self) -> None:
		self._stopping = True
		self._thread.join(2)
		self.sock.close()


@pytest.fixture
def socketPath() -> Iterator[str]:
	with tempfile.TemporaryDirectory(prefix="keyd", dir="/tmp") as socketDir:
		yield str(Path(socketDir) / "socket")


class Served:
	"""A stand-in and its client, closed at the end of the test."""

	def __init__(self) -> None:
		self.pairs: list[tuple[StandIn, Client | None]] = []

	def __call__(
		self,
		path: str,
		sink,
		decide: Decide = passAll,
		features: int | None = ALL,
		asked: int = ALL,
		start: bool = True,
		**options,
	) -> tuple[StandIn, Client]:
		""":param features: What the stand-in grants, or None for its default."""
		# A press left to the test waits for it, well within the line's bounds.
		options = {"heartbeatTimeout": 60, "verdictTimeout": 3.0, "unansweredSpan": 30.0, **options}
		if features is not None:
			options["features"] = features
		keyboard = StandIn(path, sink=sink, **options).start()
		client = Client(path, asked, decide, start=start)
		self.pairs.append((keyboard, client))
		if start:
			assert client.held.wait(5), "the stand-in never held the keyboard"
		return keyboard, client

	def close(self) -> None:
		for keyboard, client in self.pairs:
			if client is not None:
				client.close()
			keyboard.close()


@pytest.fixture
def served() -> Iterator[Served]:
	s = Served()
	yield s
	s.close()


def holding(*held: int) -> Decide:
	"""Leave the presses of held to the test, and pass the rest."""
	return lambda evdev, _flags: None if evdev in held else VERDICT_PASS


# Granted on request.


def test_theDefaultStandInGrantsWaitAlone(socketPath: str, served: Served):
	assert StandIn.__init__.__kwdefaults__["features"] == FEATURE_WAIT
	keyboard, client = served(socketPath, NullSink(), features=None)
	assert client.granted == keyboard.granted == FEATURE_WAIT
	client.type(1, A, True)
	client.waitFor(lambda: client.closed.is_set())
	assert client.goodbye() == GOODBYE_MALFORMED and client.typed() == []
	assert keyboard.emitted() == []


@pytest.mark.parametrize(
	("asked", "granted"),
	[(ALL, ALL), (FEATURE_WAIT, FEATURE_WAIT), (FEATURE_TYPING, FEATURE_TYPING), (0, 0)],
)
def test_typingAndLatePassAreGrantedWhenAsked(socketPath: str, served: Served, asked: int, granted: int):
	sink = NullSink()
	keyboard, client = served(socketPath, sink, asked=asked)
	assert client.granted == keyboard.granted == granted
	if granted & FEATURE_TYPING:
		client.type(1, A, True)
		client.type(2, A, False)
		client.waitFor(lambda: len(client.typed()) == 2)
		assert sink.passed() == [(A, True), (A, False)]
	else:
		client.type(1, A, True)
		client.waitFor(lambda: client.closed.is_set())
		assert client.goodbye() == GOODBYE_MALFORMED
		return
	if not granted & FEATURE_LATE_PASS:
		keyboard.press("a")
		client.waitFor(lambda: len(client.told()) == 2)
		client.latePass(client.pressSeq(A), client.pressSeq(A))
		client.waitFor(lambda: client.closed.is_set())
		assert client.goodbye() == GOODBYE_MALFORMED


# TYPE and TYPED.


def test_typeInjectGoesOutBehindThePressesBeforeIt(socketPath: str, served: Served):
	sink = NullSink("pl")
	keyboard, client = served(socketPath, sink, decide=holding(X))
	keyboard.down("x")
	client.waitFor(lambda: client.told())
	for tag, (evdev, down) in enumerate([(SHIFT, True), (A, True), (A, False), (SHIFT, False)], 1):
		client.type(tag, evdev, down)
	time.sleep(0.3)
	# Nothing goes out while the press read before them waits for its verdict.
	assert sink.sent == [] and client.typed() == []
	client.verdict(client.pressSeq(X), VERDICT_PASS)
	client.waitFor(lambda: len(client.typed()) == 4)
	keyboard.up("x")
	keyboard.waitFor(lambda: len(sink.sent) == 6)
	client.waitFor(lambda: len(client.told()) == 2)
	assert sink.passed() == [(X, True), (SHIFT, True), (A, True), (A, False), (SHIFT, False), (X, False)]
	# Answered OUT, under no KEY.
	assert client.typed() == [(1, 0, TYPED_OUT), (2, 0, TYPED_OUT), (3, 0, TYPED_OUT), (4, 0, TYPED_OUT)]
	assert [key for _seq, _device, key, _value, _flags in client.told()] == [X, X]
	# Each TYPED comes once its key is out: after the press it waited behind.
	xOut = next(i for i, e in enumerate(keyboard.events) if e[1] == "x" and e[2] == X)
	firstTyped = next(i for i, e in enumerate(keyboard.events) if e[1] == "typed")
	assert xOut < firstTyped


def test_aTypedReleaseOvertakesAWaitingPressAsAReleaseDoes(socketPath: str, served: Served):
	sink = NullSink()
	keyboard, client = served(socketPath, sink, decide=holding(X))
	client.type(1, A, True)
	client.waitFor(lambda: client.typed() == [(1, 0, TYPED_OUT)])
	keyboard.down("x")
	client.waitFor(lambda: client.told())
	client.type(2, A, False)
	client.waitFor(lambda: client.outcome(2) == TYPED_OUT)
	assert sink.passed() == [(A, True), (A, False)]
	client.verdict(client.pressSeq(X), VERDICT_PASS)
	keyboard.up("x")
	keyboard.waitFor(lambda: len(sink.sent) == 4)


def test_typeDecideComesBackAsAKeyToDecide(socketPath: str, served: Served):
	sink = NullSink()
	keyboard, client = served(
		socketPath, sink, decide=lambda evdev, _f: VERDICT_KEEP if evdev == Q else VERDICT_PASS
	)
	client.type(1, Q, True, TYPE_DECIDE)
	client.type(2, Q, False, TYPE_DECIDE)
	client.type(3, W, True, TYPE_DECIDE)
	client.type(4, W, False, TYPE_DECIDE)
	client.waitFor(lambda: len(client.typed()) == 4)
	keys = client.told()
	assert [(device, key, value, flags) for _seq, device, key, value, flags in keys] == [
		(TYPED_DEVICE, Q, PRESS, KEY_ASK | KEY_TYPED),
		(TYPED_DEVICE, Q, RELEASE, KEY_TYPED),
		(TYPED_DEVICE, W, PRESS, KEY_ASK | KEY_TYPED),
		(TYPED_DEVICE, W, RELEASE, KEY_TYPED),
	]
	seqs = [seq for seq, *_rest in keys]
	assert client.typed() == [
		(1, seqs[0], TYPED_KEPT),
		(2, seqs[1], TYPED_KEPT),
		(3, seqs[2], TYPED_OUT),
		(4, seqs[3], TYPED_OUT),
	]
	assert sink.passed() == [(W, True), (W, False)]
	assert keyboard.given[seqs[0]] == [VERDICT_KEEP]


def test_anUnansweredTypedPressIsPassedAtItsBound(socketPath: str, served: Served):
	sink = NullSink()
	keyboard, client = served(socketPath, sink, decide=holding(Q), verdictTimeout=0.1)
	client.type(1, Q, True, TYPE_DECIDE)
	client.type(2, Q, False, TYPE_DECIDE)
	client.waitFor(lambda: len(client.typed()) == 2)
	assert [outcome for _tag, _seq, outcome in client.typed()] == [TYPED_OUT, TYPED_OUT]
	assert sink.passed() == [(Q, True), (Q, False)]
	assert ("expired", client.pressSeq(Q)) in [e[1:3] for e in keyboard.events]


def test_typingOnlyWhileCapturing(socketPath: str, served: Served):
	sink = NullSink()
	_keyboard, client = served(socketPath, sink, start=False)
	client.type(1, A, True)
	client.waitFor(lambda: client.typed())
	client.start()
	assert client.held.wait(5)
	client.type(2, A, True)
	client.waitFor(lambda: len(client.typed()) == 2)
	client.send(bytes((STOP,)))
	client.waitFor(lambda: any(m[0] == RELEASED for m in client.messages))
	client.type(3, A, False)
	client.waitFor(lambda: len(client.typed()) == 3)
	assert client.typed() == [(1, 0, TYPED_REFUSED_STATE), (2, 0, TYPED_OUT), (3, 0, TYPED_REFUSED_STATE)]
	# The key typed down is let go of with the rest.
	assert sink.passed() == [(A, True), (A, False)]


def test_typingRefusesKeysByTheRules(socketPath: str, served: Served):
	sink = NullSink()
	_keyboard, client = served(socketPath, sink)
	for tag, evdev in enumerate((POWER, SLEEP, POWER2, 205, 0x198, 0x200), 1):
		client.type(tag, evdev, True)
	client.type(10, B, False)
	client.waitFor(lambda: len(client.typed()) == 7)
	assert client.typed() == [(tag, 0, TYPED_REFUSED_KEY) for tag in range(1, 7)] + [(10, 0, TYPED_NOT_DOWN)]
	assert sink.sent == [] and not client.closed.is_set()
	assert (
		keydStandIn.typableCodes() >= {A, SHIFT, ALTGR, INSERT, TAB, 82}
		and SLEEP not in keydStandIn.typableCodes()
	)


def test_typingRefusesSysRqWithAlt(socketPath: str, served: Served):
	sink = NullSink()
	keyboard, client = served(socketPath, sink)
	keyboard.down("alt")
	keyboard.waitFor(lambda: sink.passed() == [(ALT, True)])
	client.type(1, SYSRQ, True)
	client.waitFor(lambda: client.outcome(1) is not None)
	keyboard.up("alt")
	client.type(2, SYSRQ, True)
	client.waitFor(lambda: client.outcome(2) is not None)
	keyboard.down("alt")
	client.type(3, ALT, True)
	client.type(4, SYSRQ, False)
	client.waitFor(lambda: client.outcome(4) is not None)
	keyboard.up("alt")
	assert [client.outcome(tag) for tag in (1, 2, 3, 4)] == [
		TYPED_REFUSED_KEY,
		TYPED_OUT,
		TYPED_REFUSED_KEY,
		TYPED_OUT,
	]


def test_noTypedPressWhileTheSysRqHandlerMayBeActive(socketPath: str, served: Served):
	sink = NullSink()
	keyboard, client = served(socketPath, sink)
	client.type(1, B, True)
	client.waitFor(lambda: client.outcome(1) == TYPED_OUT)
	# The user's Alt+SysRq makes it active, until that Alt is let go of.
	keyboard.down("alt")
	keyboard.down(SYSRQ)
	keyboard.up(SYSRQ)
	keyboard.waitFor(lambda: (SYSRQ, False) in sink.passed())
	client.type(2, A, True)
	# A release is no command.
	client.type(3, B, False)
	client.waitFor(lambda: len(client.typed()) == 3)
	keyboard.up("alt")
	keyboard.waitFor(lambda: (ALT, False) in sink.passed())
	client.type(4, A, True)
	client.type(5, A, False)
	client.waitFor(lambda: len(client.typed()) == 5)
	assert [client.outcome(tag) for tag in range(1, 6)] == [
		TYPED_OUT,
		TYPED_REFUSED_KEY,
		TYPED_OUT,
		TYPED_OUT,
		TYPED_OUT,
	]
	assert (A, True) not in sink.passed()[: sink.passed().index((ALT, False))]


def test_typedSysRqRefusedAsItWasToGoOut(socketPath: str, served: Served):
	"""The rules are checked again as the typed press goes out, since keys ahead of it may change what is
	down; its release then finds it not typed down."""
	sink = NullSink()
	keyboard, client = served(socketPath, sink, decide=holding(X))
	keyboard.down("x")
	client.waitFor(lambda: client.told())
	keyboard.down("alt")
	client.type(1, SYSRQ, True)
	client.type(2, SYSRQ, False)
	time.sleep(0.2)
	assert client.typed() == []
	client.verdict(client.pressSeq(X), VERDICT_PASS)
	client.waitFor(lambda: len(client.typed()) == 2)
	keyboard.up("alt")
	keyboard.up("x")
	assert client.typed() == [(1, 0, TYPED_REFUSED_KEY), (2, 0, TYPED_NOT_DOWN)]
	assert SYSRQ not in [evdev for evdev, _down in sink.passed()]


def test_typingRefusesALineTooLong(socketPath: str, served: Served):
	sink = NullSink()
	keyboard, client = served(socketPath, sink, decide=holding(X))
	keyboard.down("x")
	client.waitFor(lambda: client.told())
	for tag in range(TYPED_WAITING_LIMIT):
		client.type(tag, A, tag % 2 == 0)
	client.type(TYPED_WAITING_LIMIT, B, True)
	client.waitFor(lambda: client.typed())
	assert client.typed() == [(TYPED_WAITING_LIMIT, 0, TYPED_REFUSED_FULL)]
	client.verdict(client.pressSeq(X), VERDICT_PASS)
	client.waitFor(lambda: len(client.typed()) == TYPED_WAITING_LIMIT + 1)
	assert all(outcome == TYPED_OUT for _tag, _seq, outcome in client.typed()[1:])


def test_typingRefusesKeysTooFast(socketPath: str, served: Served):
	sink = NullSink()
	_keyboard, client = served(socketPath, sink)
	sent = TYPED_BURST + 60
	began = time.monotonic()
	for tag in range(sent):
		client.type(tag, A, tag % 2 == 0)
	client.waitFor(lambda: len(client.typed()) == sent)
	took = time.monotonic() - began
	outcomes = [outcome for _tag, _seq, outcome in client.typed()]
	out = outcomes.count(TYPED_OUT)
	assert outcomes.count(TYPED_REFUSED_FULL) > 0
	assert TYPED_BURST <= out <= TYPED_BURST + took * TYPED_PER_SECOND + 1, (out, took)
	assert set(outcomes) <= {TYPED_OUT, TYPED_REFUSED_FULL, TYPED_NOT_DOWN}


@pytest.mark.parametrize(
	"message",
	[
		struct.pack("<BIHBB", TYPE, 1, A, 2, TYPE_INJECT),
		struct.pack("<BIHBB", TYPE, 1, A, 1, 2),
		struct.pack("<BIHBB", TYPE, 1, BTN_LEFT, 1, TYPE_INJECT),
		struct.pack("<BIHBB", TYPE, 1, 0, 1, TYPE_INJECT),
		struct.pack("<BIH", TYPE, 1, A),
	],
	ids=["value", "mode", "button", "zero", "short"],
)
def test_aMalformedTypeEndsTheConnection(socketPath: str, served: Served, message: bytes):
	sink = NullSink()
	_keyboard, client = served(socketPath, sink)
	client.send(message)
	client.waitFor(lambda: client.closed.is_set())
	assert client.goodbye() == GOODBYE_MALFORMED and client.typed() == []
	assert sink.sent == []


def test_typedKeysNotYetOutAreLostWhenItLetsGo(socketPath: str, served: Served):
	sink = NullSink()
	keyboard, client = served(socketPath, sink, decide=holding(X))
	client.type(1, B, True)
	client.waitFor(lambda: client.outcome(1) == TYPED_OUT)
	keyboard.down("x")
	client.waitFor(lambda: client.told())
	client.type(2, A, True)
	client.type(3, Q, True, TYPE_DECIDE)
	time.sleep(0.2)
	client.send(bytes((STOP,)))
	client.waitFor(lambda: any(m[0] == RELEASED for m in client.messages))
	kinds = [m[0] for m in client.messages if m[0] in (TYPED, RELEASED)]
	assert kinds == [TYPED, TYPED, TYPED, RELEASED]
	assert client.typed()[1:] == [(2, 0, TYPED_LOST), (3, client.pressSeq(Q), TYPED_LOST)]
	# The user's own press waiting is passed on, and every key down let go of; the typed ones never.
	assert sink.passed() == [(B, True), (X, True), (X, False), (B, False)]


def test_typedKeysAreLostOnTheEmergencyChord(socketPath: str, served: Served):
	sink = NullSink()
	keyboard, client = served(socketPath, sink, decide=holding(X, 14, 1, 28))
	keyboard.down("x")
	client.waitFor(lambda: client.told())
	client.type(1, A, True)
	client.type(2, A, False)
	time.sleep(0.2)
	for name in ("BackSpace", "Escape", "Return"):
		keyboard.down(name)
	client.waitFor(lambda: any(m[0] == RELEASED for m in client.messages))
	assert [m for m in client.messages if m[0] in (TYPED, RELEASED)] == [
		(TYPED, 1, 0, TYPED_LOST),
		(TYPED, 2, 0, TYPED_LOST),
		(RELEASED, RELEASED_EMERGENCY),
	]
	assert sink.passed() == []
	for name in ("BackSpace", "Escape", "Return", "x"):
		keyboard.up(name)


def test_typedKeysDoNotMakeTheChord(socketPath: str, served: Served):
	sink = NullSink()
	keyboard, client = served(socketPath, sink)
	for tag, evdev in enumerate((14, 1), 1):
		client.type(tag, evdev, True)
	keyboard.down("Return")
	client.waitFor(lambda: len(client.typed()) == 2 and client.told())
	keyboard.up("Return")
	for tag, evdev in enumerate((14, 1), 3):
		client.type(tag, evdev, False)
	client.waitFor(lambda: len(client.typed()) == 4)
	assert not any(m[0] == RELEASED for m in client.messages) and keyboard.capturing


# LATE_PASS.


def lateChord(keyboard: StandIn, client: Client, releaseAltEarly: bool = False) -> tuple[int, int]:
	"""Insert and Alt kept, then Tab read and left to the test: the sequence numbers of Alt and Tab."""
	keyboard.down("Insert")
	keyboard.down("alt")
	client.waitFor(lambda: len(keyboard.verdicts) == 2)
	keyboard.down("Tab")
	client.waitFor(lambda: len(client.told()) == 3)
	if releaseAltEarly:
		keyboard.up("alt")
	return client.pressSeq(ALT), client.pressSeq(TAB)


def keepReaderKeys(evdev: int, _flags: int) -> int | None:
	if evdev in (INSERT, ALT):
		return VERDICT_KEEP
	return None if evdev == TAB else VERDICT_PASS


@pytest.mark.parametrize("releaseAltEarly", [False, True])
def test_latePassPutsTheKeptModifierJustBeforeThePress(
	socketPath: str, served: Served, releaseAltEarly: bool
):
	sink = NullSink()
	keyboard, client = served(socketPath, sink, decide=keepReaderKeys)
	alt, tab = lateChord(keyboard, client, releaseAltEarly)
	client.latePass(alt, tab)
	client.verdict(tab, VERDICT_PASS)
	keyboard.waitFor(lambda: len(sink.passed()) >= 2)
	keyboard.up("Tab")
	if not releaseAltEarly:
		keyboard.up("alt")
	keyboard.up("Insert")
	keyboard.waitFor(lambda: len(sink.passed()) == 4)
	assert keyboard.latePasses == [(alt, tab, True)]
	if releaseAltEarly:
		# A modifier's release waits in line behind the press read before it.
		assert sink.passed() == [(ALT, True), (TAB, True), (ALT, False), (TAB, False)]
	else:
		assert sink.passed() == [(ALT, True), (TAB, True), (TAB, False), (ALT, False)]
	assert sink.kept == [(INSERT, True), (ALT, True), (INSERT, False)]
	assert not client.closed.is_set() and not any(m[0] == RELEASED for m in client.messages)


def test_twoLatePassesPutBothKeptModifiersBeforeThePress(socketPath: str, served: Served):
	sink = NullSink()
	ctrl = code("Control_L")
	keyboard, client = served(
		socketPath,
		sink,
		decide=lambda evdev, _f: (
			VERDICT_KEEP if evdev in (INSERT, ctrl, ALT) else None if evdev == TAB else VERDICT_PASS
		),
	)
	keyboard.down("Insert")
	keyboard.down("Control_L")
	keyboard.down("alt")
	client.waitFor(lambda: len(keyboard.verdicts) == 3)
	keyboard.down("Tab")
	client.waitFor(lambda: len(client.told()) == 4)
	tab = client.pressSeq(TAB)
	client.latePass(client.pressSeq(ctrl), tab)
	client.latePass(client.pressSeq(ALT), tab)
	client.verdict(tab, VERDICT_PASS)
	keyboard.waitFor(lambda: len(sink.passed()) >= 3)
	for name in ("Tab", "alt", "Control_L", "Insert"):
		keyboard.up(name)
	keyboard.waitFor(lambda: len(sink.passed()) == 6)
	# In the order their LATE_PASSes came, each just before the press.
	assert sink.passed() == [
		(ctrl, True),
		(ALT, True),
		(TAB, True),
		(TAB, False),
		(ALT, False),
		(ctrl, False),
	]
	assert sink.kept == [(INSERT, True), (ctrl, True), (ALT, True), (INSERT, False)]


def test_latePassWhenTheWaitingPressIsKept(socketPath: str, served: Served):
	sink = NullSink()
	keyboard, client = served(socketPath, sink, decide=keepReaderKeys)
	alt, tab = lateChord(keyboard, client)
	client.latePass(alt, tab)
	client.verdict(tab, VERDICT_KEEP)
	keyboard.waitFor(lambda: sink.passed() == [(ALT, True)])
	for name in ("Tab", "alt", "Insert"):
		keyboard.up(name)
	keyboard.waitFor(lambda: sink.passed() == [(ALT, True), (ALT, False)])


def test_aLatePassThatCannotApplyChangesNothing(socketPath: str, served: Served):
	sink = NullSink()
	decisions = {INSERT: VERDICT_KEEP, ALT: VERDICT_PASS}
	keyboard, client = served(socketPath, sink, decide=lambda evdev, _f: decisions.get(evdev))
	# Alt was passed, not kept.
	alt, tab = lateChord(keyboard, client)
	client.latePass(alt, tab)
	client.verdict(tab, VERDICT_PASS)
	for name in ("Tab", "alt", "Insert"):
		keyboard.up(name)
	keyboard.waitFor(lambda: len(sink.passed()) == 4)
	# Alt kept, and let go of before Tab was read.
	decisions[ALT] = VERDICT_KEEP
	keyboard.down("alt")
	keyboard.up("alt")
	keyboard.down("Tab")
	client.waitFor(lambda: client.pressSeq(TAB) != tab)
	client.latePass(client.pressSeq(ALT), client.pressSeq(TAB))
	client.verdict(client.pressSeq(TAB), VERDICT_PASS)
	keyboard.up("Tab")
	keyboard.waitFor(lambda: len(sink.passed()) == 6)
	# Alt kept and down, and Tab answered already.
	tab = client.pressSeq(TAB)
	keyboard.down("alt")
	keyboard.down("Tab")
	client.waitFor(lambda: client.pressSeq(TAB) != tab)
	tab = client.pressSeq(TAB)
	client.verdict(tab, VERDICT_PASS)
	keyboard.waitFor(lambda: len(sink.passed()) == 7)
	client.latePass(client.pressSeq(ALT), tab)
	keyboard.up("Tab")
	keyboard.up("alt")
	keyboard.waitFor(lambda: len(sink.passed()) == 8)
	time.sleep(0.2)
	assert [applied for _kept, _waiting, applied in keyboard.latePasses] == [False, False, False]
	assert sink.passed()[4:] == [(TAB, True), (TAB, False), (TAB, True), (TAB, False)]
	assert not client.closed.is_set()


@pytest.mark.parametrize(
	"named",
	["zero", "never sent", "not a modifier", "Caps Lock", "a release", "typed", "waiting not asked", "twice"],
)
def test_aMalformedLatePassEndsTheConnection(socketPath: str, served: Served, named: str):
	sink = NullSink()
	keep = {INSERT, ALT, A, code("Caps_Lock")}
	keyboard, client = served(
		socketPath,
		sink,
		decide=lambda evdev, _f: VERDICT_KEEP if evdev in keep else None if evdev == TAB else VERDICT_PASS,
	)
	keyboard.down("a")
	keyboard.up("a")
	client.type(1, ALT, True, TYPE_DECIDE)
	client.waitFor(lambda: client.outcome(1) == TYPED_KEPT)
	typedAlt = client.pressSeq(ALT)
	keyboard.down("Caps_Lock")
	keyboard.down("alt")
	keyboard.down("Tab")
	client.waitFor(lambda: len(client.told()) == 6)
	alt, tab = client.pressSeq(ALT), client.pressSeq(TAB)
	assert alt != typedAlt
	aRelease = next(seq for seq, _d, key, value, _f in client.told() if key == A and value == RELEASE)
	kept, waiting = {
		"zero": (0, tab),
		"never sent": (alt, tab + 100),
		"not a modifier": (client.pressSeq(A), tab),
		"Caps Lock": (client.pressSeq(code("Caps_Lock")), tab),
		"a release": (aRelease, tab),
		"typed": (typedAlt, tab),
		"waiting not asked": (alt, aRelease),
		"twice": (alt, tab),
	}[named]
	if named == "twice":
		client.latePass(alt, tab)
		time.sleep(0.2)
		assert not client.closed.is_set()
	client.latePass(kept, waiting)
	client.waitFor(lambda: client.closed.is_set())
	assert client.goodbye() == GOODBYE_MALFORMED


def test_theKeptKeyNeverGoesOutWhenItLetsGoFirst(socketPath: str, served: Served):
	sink = NullSink()
	keyboard, client = served(socketPath, sink, decide=keepReaderKeys)
	alt, tab = lateChord(keyboard, client)
	client.latePass(alt, tab)
	time.sleep(0.2)
	client.send(bytes((STOP,)))
	client.waitFor(lambda: any(m == (RELEASED, RELEASED_STOPPED) for m in client.messages))
	assert keyboard.latePasses == [(alt, tab, True)]
	assert sink.passed() == [(TAB, True), (TAB, False)]
	for name in ("Tab", "alt", "Insert"):
		keyboard.up(name)


# Under the rig's compositors.


@pytest.mark.rigSession("x11")
@pytest.mark.parametrize("compositor", ["mutter", "sway"])
def test_standInUnderCompositors(tmp_path: Path, compositor: str) -> None:
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
			"-m",
			"rigSession",
			__file__,
		],
		env=env,
		capture_output=True,
		text=True,
		timeout=300,
		check=False,
	)
	assert ran.returncode == 0, ran.stdout[-6000:] + ran.stderr[-3000:]
	assert " passed" in ran.stdout and " failed" not in ran.stdout, ran.stdout[-2000:]


@pytest.fixture(scope="module")
def probe():
	from test_waylandRig import Probe

	p = Probe()
	yield p
	p.close()


def given(probe, act: Callable[[], None]) -> tuple[list[str], str | None]:
	"""The keys the probe was given while act ran, and its text after, if it changed."""
	probe.drain()
	act()
	time.sleep(0.4)
	lines = probe.drain()
	texts = [line.removeprefix("text ") for line in lines if line.startswith("text ")]
	return [line for line in lines if not line.startswith("text ")], (texts[-1] if texts else None)


@compositors
def test_theRigsSinkIsItsCompositors(socketPath: str) -> None:
	sink = keydStandIn.compositorSink()
	try:
		assert sink.compositor == harness.rigSession()
		other = keydStandIn.SwaySink if sink.compositor == "mutter" else keydStandIn.MutterSink
		with pytest.raises(RuntimeError):
			other()
	finally:
		sink.close()


@compositors
def test_sinkTypesInThePlLayout(socketPath: str, served: Served, probe) -> None:
	sink = keydStandIn.compositorSink()
	keyboard, client = served(
		socketPath, sink, decide=lambda evdev, _f: VERDICT_KEEP if evdev == INSERT else VERDICT_PASS
	)
	keys, text = given(probe, lambda: keyboard.press("shift+a"))
	assert keys == ["pressed Shift_L", "pressed A", "released A", "released Shift_L"]
	assert text is not None and text.endswith("A")
	keys, text = given(probe, lambda: keyboard.press("ISO_Level3_Shift+a"))
	assert keys == [
		"pressed ISO_Level3_Shift",
		"pressed aogonek",
		"released aogonek",
		"released ISO_Level3_Shift",
	]
	assert text is not None and text.endswith("Aą")
	# A kept key never reaches the program.
	keys, text = given(probe, lambda: keyboard.press("Insert+b"))
	assert keys == ["pressed b", "released b"] and text.endswith("Aąb")
	assert all(virtual for _code, _down, virtual in sink.sent)
	assert sink.kept == [(INSERT, True), (INSERT, False)]
	# Let go of, the keys come as from a keyboard nothing holds, to the same seat.
	client.send(bytes((STOP,)))
	client.waitFor(lambda: any(m[0] == RELEASED for m in client.messages))
	keys, text = given(probe, lambda: keyboard.press("shift+c"))
	assert keys == ["pressed Shift_L", "pressed C", "released C", "released Shift_L"]
	assert text.endswith("AąbC")
	assert not any(virtual for _code, _down, virtual in sink.sent[-4:])


@compositors
def test_typeInjectReachesTheProgramBehindThePressBeforeIt(socketPath: str, served: Served, probe) -> None:
	sink = keydStandIn.compositorSink()
	keyboard, client = served(socketPath, sink, decide=holding(B))

	def act() -> None:
		keyboard.down("b")
		client.waitFor(lambda: client.told())
		for tag, (evdev, down) in enumerate(
			[
				(SHIFT, True),
				(C, True),
				(C, False),
				(SHIFT, False),
				(ALTGR, True),
				(A, True),
				(A, False),
				(ALTGR, False),
			],
			1,
		):
			client.type(tag, evdev, down)
		time.sleep(0.2)
		assert client.typed() == []
		client.verdict(client.pressSeq(B), VERDICT_PASS)
		client.waitFor(lambda: len(client.typed()) == 8)
		keyboard.up("b")

	keys, text = given(probe, act)
	assert keys == [
		"pressed b",
		"pressed Shift_L",
		"pressed C",
		"released C",
		"released Shift_L",
		"pressed ISO_Level3_Shift",
		"pressed aogonek",
		"released aogonek",
		"released ISO_Level3_Shift",
		"released b",
	]
	assert text is not None and text.endswith("bCą")
	assert client.typed() == [(tag, 0, TYPED_OUT) for tag in range(1, 9)]


@compositors
def test_sinkLightsAreTheCompositorKeyboardsLocks(socketPath: str, served: Served, probe) -> None:
	sink = keydStandIn.compositorSink()
	keyboard, client = served(socketPath, sink)
	client.waitFor(lambda: client.locks())
	assert not client.locks()[-1] & CAPS
	try:
		keys, _text = given(probe, lambda: keyboard.press("Caps_Lock"))
		client.waitFor(lambda: client.locks()[-1] & CAPS)
		keys, _text = given(probe, lambda: keyboard.press("z"))
		assert keys == ["pressed Z", "released Z"]
	finally:
		if sink.lights() & CAPS:
			given(probe, lambda: keyboard.press("Caps_Lock"))
	client.waitFor(lambda: not client.locks()[-1] & CAPS)
	assert sink.lights() == client.locks()[-1]
