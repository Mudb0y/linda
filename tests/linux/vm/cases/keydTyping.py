"""Typing through the helper, FEATURE_TYPING granted, against the guest's own kernel: a typed key goes
out on the virtual keyboard in its place in line behind the keys read before it; one typed to be
decided comes back to the client as a key, and is kept, passed, or passed on at its bound; the power
keys are never typed; no typed press is taken by the kernel's SysRq handler, which the user's own
key under Alt and SysRq is seen to reach; typing is refused after the emergency chord, and a typed
key held down is released by it; and the line's limit and the rate hold against a client that sends
TYPE faster than any test call can. Experiment E14 is run first: udev's tags on the virtual
keyboard's node, and whether logind watches it as a keyboard with power keys.

The file has two halves. On the host, run(ground) drives the guest. In the guest, as ground-typist,
which cases/keydTyping.nix installs, it is a client of the helper's of its own, written from
protocol.rs, for what guest/keydClient.py cannot do, many TYPEs within a millisecond, each call of
that client costing a third of a second:

	keydTyping.py order NAME READY
	keydTyping.py caps NAME
	keydTyping.py lost NAME READY

Each connects as a program of the session in front, asks for FEATURE_TYPING and FEATURE_WAIT, sends
START, and waits until the keyboard named NAME is held. order then creates the file READY, holds the
T the test then taps with WAIT, and types behind it before passing it; caps fills the line and
empties the bucket; lost, for cases/keydTypingBackground.py, holds a typed Shift down and a typed key
in line, creates READY for a root shell to switch consoles at, and types again once the helper has
let go. Each prints one JSON object of what it saw and exits.
"""

import json
import os
import shlex
import socket
import struct
import sys
import threading
import time
from collections import Counter
from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING

if __name__ != "__main__":
	from ground import VIRTUAL_KEYBOARD

if TYPE_CHECKING:
	from ground import Ground, KeydClient

UHID = "Ground uhid keyboard"
#: FEATURE_TYPING and FEATURE_WAIT, TYPED_DEVICE, VERDICT_TIMEOUT and the typing bounds in
#: protocol.rs.
FEATURE_TYPING, FEATURE_WAIT = 1, 2
TYPED_DEVICE = 0xFFFFFFFF
BOUND = 0.1
TYPED_WAITING_LIMIT = 64
TYPED_PER_SECOND = 100
TYPED_BURST = 200
#: KEY_POWER, KEY_SLEEP, KEY_SUSPEND, KEY_POWER2 and KEY_RESTART.
POWER_CODES = (116, 142, 205, 0x164, 0x198)
#: Keys that type into the test window's field and nothing more, as many as the line holds:
#: digits, letters, punctuation, space, the numpad and the key beside left Shift on a 105-key board.
LINE_CODES = (
	*range(2, 12),
	*range(16, 26),
	*range(30, 39),
	*range(44, 51),
	*(12, 13, 26, 27, 39, 40, 41, 43, 51, 52, 53, 57),
	*range(71, 84),
	*(86, 98, 55),
)
SYSRQ_TEXT = "sysrq"
#: The file the guest half creates once the keyboard is held.
READY = "/tmp/ground/typist-ready"


def typeKey(client: "KeydClient", tag: int, key: str | int, down: bool, mode: str = "inject") -> None:
	fields = {"code": key} if isinstance(key, int) else {"key": key}
	client.call("type", tag=tag, down=down, mode=mode, **fields)


def answers(client: "KeydClient", since: object, tags: Iterable[int], timeout: float = 5) -> dict[int, dict]:
	"""The TYPED that answered each tag, once every one has come."""
	wanted = set(tags)
	found: dict[int, dict] = {}

	def arrived() -> bool:
		found.update({r["tag"]: r for r in client.records(since, "TYPED") if r["tag"] in wanted})
		return wanted <= set(found)

	try:
		client.ground.waitFor(arrived, f"TYPED for {sorted(wanted)}", timeout)
	except AssertionError as e:
		raise AssertionError(f"{e}; the client recorded {client.records(since)[-30:]}") from None
	return found


def waitForKey(ground: "Ground", source: str, key: str, value: int, since: object, device: str) -> dict:
	"""The first record of key with value from device, whatever came before it."""
	return ground.waitFor(
		lambda: next(
			(r for r in ground.keys(source, since, device=device) if (r["key"], r["value"]) == (key, value)),
			None,
		),
		f"{key} {value} from {device} at {source}",
	)


def sysrqLines(ground: "Ground") -> list[str]:
	return [line for line in ground.shell("dmesg").splitlines() if SYSRQ_TEXT in line.lower()]


def appKeyvals(ground: "Ground", since: object) -> list[str]:
	"""The keys the window had pressed, with the repeats of a key held, which GDK hands over as
	presses, folded into its press: a key typed by two test calls is held a third of a second."""
	down: set[str] = set()
	pressed = []
	for r in ground.keys("app", since):
		if r["value"] == 1 and r["key"] not in down:
			pressed.append(r["keyval"])
			down.add(r["key"])
		elif r["value"] == 0:
			down.discard(r["key"])
	return pressed


def waitForTyped(ground: "Ground", since: object, keyvals: list[str]) -> None:
	"""Wait until the window has had keyvals pressed and every key it had pressed released."""

	def arrived() -> bool:
		records = ground.keys("app", since)
		pressed = {r["key"] for r in records if r["value"] == 1}
		released = {r["key"] for r in records if r["value"] == 0}
		return appKeyvals(ground, since) == keyvals and pressed <= released

	try:
		ground.waitFor(arrived, f"the window to have {keyvals}", 5)
	except AssertionError as e:
		raise AssertionError(f"{e}; it had {ground.keys('app', since)}") from None


def virtualNode(ground: "Ground") -> str:
	return ground.shell(
		"for d in /sys/class/input/event*; do "
		f'[ "$(cat $d/device/name)" = {shlex.quote(VIRTUAL_KEYBOARD)} ] && echo /dev/input/${{d##*/}}; '
		"done; true"
	).strip()


def run(ground: "Ground") -> None:
	ground.openWindow()
	ground.waitForHelper()
	keyboard = ground.uhidKeyboard(UHID)
	client = ground.keydClient("main")
	answer = client.connect(features=FEATURE_TYPING | FEATURE_WAIT)
	assert answer["welcome"]["features"] == FEATURE_TYPING | FEATURE_WAIT, answer
	client.capture(UHID)

	with ground.subtest("E14: what udev and logind make of the virtual keyboard"):
		node = virtualNode(ground)
		assert node, "the virtual keyboard has no event node"
		properties = ground.shell(f"udevadm info --query=property --name={node}")
		tags = [line for line in properties.splitlines() if line.startswith(("TAGS=", "CURRENT_TAGS="))]
		logind = [
			line
			for line in ground.shell("journalctl -o cat --no-pager -u systemd-logind").splitlines()
			if node in line or VIRTUAL_KEYBOARD in line
		]
		tagged = any("power-switch" in line for line in tags)
		ground.machine.log(
			f"E14: the virtual keyboard is {node}; udev's {tags}; power-switch "
			f"{'among them' if tagged else 'not among them'}; logind said of it: {logind}"
		)

	with ground.subtest("a typed key goes out in order behind the keys read before it"):
		client.call("close")
		ground.shell(f"rm -f {READY}")
		mark = ground.mark()
		ground.start("typist-order", ["ground-typist", "order", UHID, READY])
		ground.waitFor(lambda: ground.machine.execute(f"test -e {READY}")[0] == 0, "the typist to be held")
		keyboard.tap("KEY_T", hold=0.02)
		ground.waitFor(lambda: not ground.running("typist-order"), "the typist to end", 20)
		seen = json.loads(ground.shell("cat /tmp/ground/jobs/typist-order.out"))
		# T's release, read twenty milliseconds after its press, comes in line after the typed keys,
		# which the typist sent as soon as it heard of the press.
		expected = [("KEY_T", 1), ("KEY_A", 1), ("KEY_A", 0), ("KEY_B", 1), ("KEY_B", 0)]

		def virtual() -> list[dict]:
			return [
				r
				for r in ground.keys("x", mark, device=VIRTUAL_KEYBOARD)
				if r["key"] in ("KEY_T", "KEY_A", "KEY_B")
			]

		ground.waitFor(lambda: len(virtual()) >= 6, "T, A and B on the virtual keyboard")
		x = virtual()
		events = [(r["key"], r["value"]) for r in x]
		assert [e for e in events if e != ("KEY_T", 0)] == expected and ("KEY_T", 0) in events, events
		waitForTyped(ground, mark, ["t", "a", "b"])
		user, typed = seen["user"], seen["typed"]
		ground.machine.log(
			f"from the press of T: every TYPE sent by {seen['typedBy'] - user['t']:+.3f} s, T passed "
			f"{seen['passedAt'] - user['t']:+.3f} s, A on the virtual keyboard {x[1]['t'] - user['t']:+.3f} s; the order there {events}; "
			f"answers {json.dumps(typed)}"
		)
		# What the test claims rests on each TYPE having come while T waited for its verdict.
		assert seen["typedBy"] < seen["passedAt"], seen
		assert not seen["answeredWhileHeld"], seen
		assert x[1]["t"] >= seen["passedAt"], (x, seen)
		assert [(k["value"], k["flags"]) for k in seen["typedKeys"]] == [(1, 0x05), (0, 0x04)], seen
		assert [(typed[tag]["outcome"], typed[tag]["seq"]) for tag in ("1", "2", "3", "4")] == [
			("OUT", 0),
			("OUT", 0),
			("OUT", seen["typedKeys"][0]["seq"]),
			("OUT", seen["typedKeys"][1]["seq"]),
		], typed
		assert all(typed[tag]["t"] >= seen["passedAt"] for tag in typed), seen
		mark = ground.mark()
		answer = client.connect(features=FEATURE_TYPING | FEATURE_WAIT)
		assert answer["welcome"]["features"] == FEATURE_TYPING | FEATURE_WAIT, answer
		client.capture(UHID, since=mark)

	with ground.subtest("a key typed to be decided is kept, passed, or passed on at its bound"):
		client.verdicts("pass", KEY_K="keep", KEY_M="never")
		mark = ground.mark()
		typeKey(client, 10, "KEY_K", True, "decide")
		typeKey(client, 11, "KEY_K", False, "decide")
		kept = answers(client, mark, (10, 11))
		assert [kept[tag]["outcome"] for tag in (10, 11)] == ["KEPT", "KEPT"], kept
		typeKey(client, 12, "KEY_L", True, "decide")
		typeKey(client, 13, "KEY_L", False, "decide")
		ground.waitForKeys("x", [("KEY_L", 1), ("KEY_L", 0)], mark, device=VIRTUAL_KEYBOARD)
		typeKey(client, 14, "KEY_M", True, "decide")
		pressed = client.waitFor("KEY", mark, lambda r: r["key"] == "KEY_M")
		expired = client.waitFor("EXPIRED", mark)
		typeKey(client, 15, "KEY_M", False, "decide")
		ground.waitForKeys(
			"x", [("KEY_L", 1), ("KEY_L", 0), ("KEY_M", 1), ("KEY_M", 0)], mark, device=VIRTUAL_KEYBOARD
		)
		got = answers(client, mark, range(12, 16))
		ground.machine.log(f"M unanswered, EXPIRED {expired['t'] - pressed['t']:.3f} s after its KEY")
		assert expired["seq"] == pressed["seq"], (expired, pressed)
		assert got[14]["seq"] == pressed["seq"] and got[14]["outcome"] == "OUT", got
		assert expired["t"] - pressed["t"] >= BOUND - 0.01, (expired, pressed)
		assert all(got[tag]["outcome"] == "OUT" for tag in range(12, 16)), got
		ground.settle(0.3)
		assert appKeyvals(ground, mark) == ["l", "m"], ground.keys("app", mark)
		assert not [r for r in ground.keys("x", mark) if r["key"] == "KEY_K"]
		client.verdicts("pass")

	with ground.subtest("the power keys are never typed"):
		before = [
			line
			for line in ground.shell("journalctl -o cat --no-pager -u systemd-logind").splitlines()
			if "key pressed" in line.lower()
		]
		mark = ground.mark()
		for n, code in enumerate(POWER_CODES):
			typeKey(client, 20 + 2 * n, code, True)
			typeKey(client, 21 + 2 * n, code, False)
		got = answers(client, mark, range(20, 20 + 2 * len(POWER_CODES)))
		outcomes = [got[tag]["outcome"] for tag in sorted(got)]
		assert outcomes == ["REFUSED_KEY", "NOT_DOWN"] * len(POWER_CODES), got
		ground.settle(0.5)
		assert not [
			r for r in ground.keys("evdev", mark, device=VIRTUAL_KEYBOARD) if r["code"] in POWER_CODES
		]
		after = [
			line
			for line in ground.shell("journalctl -o cat --no-pager -u systemd-logind").splitlines()
			if "key pressed" in line.lower()
		]
		assert after == before, after
		assert client.call("state")["connected"]

	with ground.subtest("no typed press is taken by the kernel's SysRq handler"):
		before = len(sysrqLines(ground))
		mark = ground.mark()
		# AltGr in the pl layout, which the window manager binds to nothing, and an Alt to the kernel.
		keyboard.run({"press": "KEY_RIGHTALT"})
		ground.waitForKeys("x", [("KEY_RIGHTALT", 1)], mark, device=VIRTUAL_KEYBOARD)
		typeKey(client, 30, "KEY_SYSRQ", True)
		assert answers(client, mark, [30])[30]["outcome"] == "REFUSED_KEY"
		keyboard.run({"press": "KEY_SYSRQ"}, {"sleep": 0.05}, {"release": "KEY_SYSRQ"})
		client.waitFor("KEY", mark, lambda r: r["key"] == "KEY_SYSRQ" and r["value"] == 0)
		typeKey(client, 31, "KEY_H", True)
		typeKey(client, 32, "KEY_H", False)
		got = answers(client, mark, (31, 32))
		assert (got[31]["outcome"], got[32]["outcome"]) == ("REFUSED_KEY", "NOT_DOWN"), got
		ground.settle(0.3)
		assert len(sysrqLines(ground)) == before, sysrqLines(ground)[before:]
		# The handler is active for the virtual keyboard: the user's own h is a command to it.
		keyboard.tap("KEY_H")
		said = ground.waitFor(lambda: sysrqLines(ground)[before:], "the kernel's SysRq handler to act")
		ground.machine.log(f"the user's h under AltGr and SysRq reached the kernel's handler: {said}")
		keyboard.run({"release": "KEY_RIGHTALT"})
		waitForKey(ground, "x", "KEY_RIGHTALT", 0, mark, VIRTUAL_KEYBOARD)
		after = len(sysrqLines(ground))
		mark = ground.mark()
		typeKey(client, 33, "KEY_H", True)
		typeKey(client, 34, "KEY_H", False)
		got = answers(client, mark, (33, 34))
		assert (got[33]["outcome"], got[34]["outcome"]) == ("OUT", "OUT"), got
		waitForTyped(ground, mark, ["h"])
		# A typed Alt counts as one held.
		typeKey(client, 35, "KEY_RIGHTALT", True)
		typeKey(client, 36, "KEY_SYSRQ", True)
		typeKey(client, 37, "KEY_RIGHTALT", False)
		got = answers(client, mark, (35, 36, 37))
		assert [got[tag]["outcome"] for tag in (35, 36, 37)] == ["OUT", "REFUSED_KEY", "OUT"], got
		ground.settle(0.3)
		assert len(sysrqLines(ground)) == after, sysrqLines(ground)[after:]

	with ground.subtest("typing is refused after the emergency chord, which releases a typed key"):
		mark = ground.mark()
		typeKey(client, 40, "KEY_LEFTSHIFT", True)
		assert answers(client, mark, [40])[40]["outcome"] == "OUT"
		ground.waitForKeys("x", [("KEY_LEFTSHIFT", 1)], mark, device=VIRTUAL_KEYBOARD)
		keyboard.run({"press": "KEY_BACKSPACE"}, {"press": "KEY_ESC"}, {"press": "KEY_ENTER"})
		client.waitFor("RELEASED", mark, lambda r: r["reason"] == "EMERGENCY")
		waitForKey(ground, "x", "KEY_LEFTSHIFT", 0, mark, VIRTUAL_KEYBOARD)
		typeKey(client, 41, "KEY_A", True)
		typeKey(client, 42, "KEY_B", True, "decide")
		got = answers(client, mark, (41, 42))
		assert (got[41]["outcome"], got[42]["outcome"]) == ("REFUSED_STATE", "REFUSED_STATE"), got
		assert not [k for k in client.keys(mark) if k["device"] == TYPED_DEVICE]
		keyboard.run({"release": "KEY_ENTER"}, {"release": "KEY_ESC"}, {"release": "KEY_BACKSPACE"})
		ground.settle(0.3)
		assert not [r for r in ground.keys("x", mark) if r["key"] in ("KEY_A", "KEY_B")]
		mark = ground.mark()
		client.capture(UHID, since=mark)
		typeKey(client, 43, "KEY_C", True)
		typeKey(client, 44, "KEY_C", False)
		waitForTyped(ground, mark, ["c"])

	with ground.subtest("the line's limit and the rate hold against a client that types at once"):
		client.call("close")
		mark = ground.mark()
		done = ground.run(["ground-typist", "caps", UHID], timeout=60)
		assert done.returncode == 0, done
		seen = json.loads(done.stdout)
		ground.machine.log(f"the typist saw: {json.dumps(seen)}")
		line = seen["line"]
		assert line["refused"] == "REFUSED_FULL", line
		assert line["answeredWhileHeld"] == 0, line
		# The press holding the line takes the last place in it.
		assert line["presses"] == {"OUT": TYPED_WAITING_LIMIT - 1}, line
		assert line["releases"] == {"OUT": TYPED_WAITING_LIMIT - 1}, line
		assert line["head"] == "OUT", line
		# The paced burst outlasts the bucket by much more than a key's worth of time, which a burst
		# sent at once does only in a guest slowed by load.
		assert seen["paced"]["seconds"] > 0.5, seen["paced"]
		for rate in (seen["rate"], seen["paced"]):
			# The bucket was full, and earns one key in each hundredth of a second the burst took. A
			# release takes a key from it only when one is there and goes out when none is, so once it
			# is empty each key earned lets a whole tap out.
			earned = TYPED_PER_SECOND * rate["seconds"] + 2
			assert TYPED_BURST // 2 <= rate["pressesOut"] <= TYPED_BURST // 2 + earned, rate
			assert rate["pressesFull"] > 0, rate
			assert set(rate["releases"]) <= {"OUT", "NOT_DOWN"}, rate
			assert rate["releases"].get("OUT", 0) == rate["pressesOut"], rate
		ground.settle(0.5)
		# A is among the keys of the line too. A burst this fast can overrun the kernel's queue for a
		# reader of the virtual keyboard, which then loses events, so what each reader had is told and
		# only the X server's state at the end is held to.
		seenBy = {
			source: sum(
				1
				for r in ground.keys(source, mark, device=VIRTUAL_KEYBOARD)
				if (r["key"], r["value"]) == ("KEY_A", 1)
			)
			- 1
			for source in ("evdev", "x")
		}
		out = seen["rate"]["pressesOut"] + seen["paced"]["pressesOut"]
		ground.machine.log(f"of {out} typed A taps put out, the readers had {seenBy}")
		keyboard = ground.xinputDevice(VIRTUAL_KEYBOARD)
		assert keyboard is not None
		state = ground.run(["xinput", "query-state", keyboard])
		assert state.returncode == 0, state
		assert "=down" not in state.stdout, state.stdout


# The guest half.

SOCKET_PATH = "/run/linda-keyd/socket"
HELLO, START, STOP, VERDICT, HEARTBEAT, TYPE = range(1, 7)
WELCOME, GOODBYE, KEY, EXPIRED, RELEASED, DEVICE = range(0x81, 0x87)
TYPED = 0x89
VERDICT_WAIT = 2
DECIDE, INJECT = 0, 1
HELD = 1
KEY_A, KEY_B, KEY_C, KEY_T, KEY_LEFTSHIFT = 30, 48, 46, 20, 42
OUTCOMES = {
	1: "OUT",
	2: "KEPT",
	3: "REFUSED_STATE",
	4: "REFUSED_KEY",
	5: "REFUSED_FULL",
	6: "NOT_DOWN",
	7: "LOST",
}
RELEASED_REASONS = {
	1: "STOPPED",
	2: "EMERGENCY",
	3: "BACKGROUND",
	4: "VIRTUAL_KEYBOARD",
	5: "SESSION_UNKNOWN",
}


def decode(message: bytes) -> dict:
	kind = message[0]
	if kind == WELCOME:
		return {"type": "WELCOME", "features": struct.unpack("<BHIHHHH", message)[2]}
	if kind == GOODBYE:
		return {"type": "GOODBYE", "reason": message[1], "text": message[4:].decode(errors="replace")}
	if kind == KEY:
		_, seq, device, code, value, flags = struct.unpack("<BQIHBB", message)
		return {"type": "KEY", "seq": seq, "device": device, "code": code, "value": value, "flags": flags}
	if kind == EXPIRED:
		return {"type": "EXPIRED", "seq": struct.unpack("<BQ", message)[1]}
	if kind == RELEASED:
		return {"type": "RELEASED", "reason": RELEASED_REASONS.get(message[1], message[1])}
	if kind == DEVICE:
		_, device, status, _detail = struct.unpack_from("<BIBB", message)
		return {"type": "DEVICE", "device": device, "status": status, "name": message[7:].decode()}
	if kind == TYPED:
		_, tag, seq, outcome = struct.unpack("<BIQB", message)
		return {"type": "TYPED", "tag": tag, "seq": seq, "outcome": OUTCOMES.get(outcome, outcome)}
	return {"type": f"{kind:#04x}"}


class Typist:
	"""The client, which reads on a thread of its own and beats its heart on another."""

	def __init__(self, keyboard: str) -> None:
		self.records: list[dict] = []
		self.changed = threading.Condition()
		self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
		self.sock.connect(SOCKET_PATH)
		threading.Thread(target=self._read, daemon=True).start()
		self.send(struct.pack("<BHI", HELLO, 1, FEATURE_TYPING | FEATURE_WAIT))
		welcome = self.waitFor(lambda r: r["type"] in ("WELCOME", "GOODBYE"), 10)
		assert welcome["type"] == "WELCOME", welcome
		assert welcome["features"] == FEATURE_TYPING | FEATURE_WAIT, welcome
		threading.Thread(target=self._beat, daemon=True).start()
		self.send(bytes([START]))
		self.waitFor(lambda r: r["type"] == "DEVICE" and r["name"] == keyboard and r["status"] == HELD, 10)

	def _read(self) -> None:
		while True:
			try:
				message = self.sock.recv(256)
			except OSError:
				message = b""
			record = {"t": time.monotonic(), **(decode(message) if message else {"type": "CLOSED"})}
			with self.changed:
				self.records.append(record)
				self.changed.notify_all()
			if not message:
				return

	def _beat(self) -> None:
		counter = 0
		while True:
			time.sleep(0.4)
			counter += 1
			self.send(struct.pack("<BI", HEARTBEAT, counter))

	def send(self, message: bytes) -> None:
		self.sock.send(message)

	def type(self, tag: int, code: int, down: bool, mode: int) -> None:
		self.send(struct.pack("<BIHBB", TYPE, tag, code, int(down), mode))

	def verdict(self, seq: int, value: int) -> None:
		self.send(struct.pack("<BQB", VERDICT, seq, value))

	def waitFor(self, where: Callable[[dict], bool], timeout: float = 5) -> dict:
		with self.changed:
			if not self.changed.wait_for(lambda: any(where(r) for r in self.records), timeout):
				raise AssertionError(f"waited {timeout} s; the helper sent {self.records[-20:]}")
			return next(r for r in self.records if where(r))

	def typed(self, tags: Iterable[int], timeout: float = 5) -> dict[int, dict]:
		wanted = set(tags)
		with self.changed:
			done = self.changed.wait_for(
				lambda: wanted <= {r["tag"] for r in self.records if r["type"] == "TYPED"}, timeout
			)
			found = {r["tag"]: r for r in self.records if r["type"] == "TYPED" and r["tag"] in wanted}
		if not done:
			raise AssertionError(f"no TYPED for {sorted(wanted - set(found))}; {self.records[-20:]}")
		return found

	def typedKey(self, tag: int, code: int, down: bool) -> dict:
		"""TYPE DECIDE, and the KEY it came back as."""
		self.type(tag, code, down, DECIDE)
		return self.waitFor(
			lambda r: (
				r["type"] == "KEY"
				and r["device"] == TYPED_DEVICE
				and r["code"] == code
				and r["value"] == down
			)
		)


def order(keyboard: str, ready: str) -> dict:
	typist = Typist(keyboard)
	with open(ready, "w"):
		pass
	user = typist.waitFor(
		lambda r: (
			r["type"] == "KEY" and r["device"] != TYPED_DEVICE and r["code"] == KEY_T and r["value"] == 1
		),
		10,
	)
	typist.verdict(user["seq"], VERDICT_WAIT)
	typist.type(1, KEY_A, True, INJECT)
	typist.type(2, KEY_A, False, INJECT)
	press = typist.typedKey(3, KEY_B, True)
	typist.verdict(press["seq"], 0)
	release = typist.typedKey(4, KEY_B, False)
	typedBy = time.monotonic()
	# Long enough for any of them to have gone out, had it not waited for T.
	time.sleep(0.2)
	answeredWhileHeld = [r for r in typist.records if r["type"] == "TYPED"]
	passedAt = time.monotonic()
	typist.verdict(user["seq"], 0)
	typed = typist.typed([1, 2, 3, 4])
	typist.send(bytes([STOP]))
	typist.waitFor(lambda r: r["type"] == "RELEASED")
	return {
		"user": user,
		"typedBy": typedBy,
		"passedAt": passedAt,
		"answeredWhileHeld": answeredWhileHeld,
		"typedKeys": [press, release],
		"typed": {str(tag): r for tag, r in typed.items()},
	}


def caps(keyboard: str) -> dict:
	typist = Typist(keyboard)
	# A press to be decided holds the line, WAIT holding it for longer than the burst takes.
	head = typist.typedKey(1000, KEY_LEFTSHIFT, True)
	typist.verdict(head["seq"], VERDICT_WAIT)
	sent = time.monotonic()
	# The press holding the line is a typed key in line too, so the last of these is one too many.
	for tag, code in enumerate(LINE_CODES, 1):
		typist.type(tag, code, True, INJECT)
	over = typist.typed([len(LINE_CODES)])[len(LINE_CODES)]
	held = {r["tag"] for r in typist.records if r["type"] == "TYPED" and r["tag"] < len(LINE_CODES)}
	typist.verdict(head["seq"], 0)
	inLine = range(1, TYPED_WAITING_LIMIT)
	presses = typist.typed([*inLine, 1000])
	for tag, code in zip(inLine, LINE_CODES, strict=False):
		typist.type(100 + tag, code, False, INJECT)
	typist.type(1001, KEY_LEFTSHIFT, False, DECIDE)
	releases = typist.typed([100 + tag for tag in inLine])
	line = {
		"refused": over["outcome"],
		"refusedAfterMs": round((over["t"] - sent) * 1000, 3),
		"answeredWhileHeld": len(held),
		"head": presses[1000]["outcome"],
		"presses": Counter(presses[tag]["outcome"] for tag in inLine),
		"releases": Counter(r["outcome"] for r in releases.values()),
	}
	typist.typed([1001])
	# The bucket full again before each burst.
	time.sleep(2.5)
	rate = tapA(typist, range(2000, 2300), 0)
	time.sleep(2.5)
	# Six hundred a second for a second: the bucket is empty after about 0.4 s.
	paced = tapA(typist, range(3000, 3600), 1 / 600)
	typist.send(bytes([STOP]))
	typist.waitFor(lambda r: r["type"] == "RELEASED")
	return {"line": line, "rate": rate, "paced": paced}


def tapA(typist: Typist, tags: range, spacing: float) -> dict:
	"""Press and release A by turns, one TYPE each spacing seconds from the first, and what came of
	the presses and the releases."""
	start = time.monotonic()
	for n, tag in enumerate(tags):
		if spacing:
			time.sleep(max(0, start + n * spacing - time.monotonic()))
		typist.type(tag, KEY_A, tag % 2 == 0, INJECT)
	got = typist.typed(tags, 10)
	presses = [got[tag]["outcome"] for tag in tags if tag % 2 == 0]
	return {
		"pressesOut": presses.count("OUT"),
		"pressesFull": presses.count("REFUSED_FULL"),
		"pressesOther": len(presses) - presses.count("OUT") - presses.count("REFUSED_FULL"),
		"releases": Counter(got[tag]["outcome"] for tag in tags if tag % 2),
		"seconds": max(r["t"] for r in got.values()) - start,
	}


def lost(keyboard: str, ready: str) -> dict:
	typist = Typist(keyboard)
	typist.type(1, KEY_LEFTSHIFT, True, INJECT)
	typist.typed([1])
	head = typist.typedKey(2, KEY_T, True)
	typist.verdict(head["seq"], VERDICT_WAIT)
	typist.type(3, KEY_A, True, INJECT)
	typist.type(4, KEY_A, False, INJECT)
	with open(ready, "w"):
		pass
	readyAt = time.monotonic()
	released = typist.waitFor(lambda r: r["type"] == "RELEASED")
	waited = typist.typed([2, 3, 4])
	typist.type(5, KEY_B, True, INJECT)
	typist.type(6, KEY_C, True, DECIDE)
	after = typist.typed([5, 6])
	keysAfter = [r for r in typist.records if r["type"] == "KEY" and r["t"] > released["t"]]
	typist.sock.close()
	return {
		"head": head,
		"readyAt": readyAt,
		"released": released,
		"typed": {str(tag): r for tag, r in {**waited, **after}.items()},
		"order": [r["type"] for r in typist.records if r["type"] in ("TYPED", "RELEASED")],
		"typedKeysAfter": [r for r in keysAfter if r["device"] == TYPED_DEVICE],
	}


if __name__ == "__main__":
	if sys.argv[1:2] == ["order"] and len(sys.argv) == 4:
		print(json.dumps(order(sys.argv[2], sys.argv[3])))
	elif sys.argv[1:2] == ["caps"] and len(sys.argv) == 3:
		print(json.dumps(caps(sys.argv[2])))
	elif sys.argv[1:2] == ["lost"] and len(sys.argv) == 4:
		print(json.dumps(lost(sys.argv[2], sys.argv[3])))
	else:
		sys.exit("usage: keydTyping.py order NAME READY | caps NAME | lost NAME READY")
	sys.stdout.flush()
	os._exit(0)
