"""The late pass, FEATURE_LATE_PASS granted, against the guest's own kernel: Alt held and kept, then
Tab, which the client passes after a LATE_PASS naming the two, reaches the virtual keyboard just ahead
of Tab and comes up when the hand lets it go; a LATE_PASS naming an Alt no longer held puts nothing
out and keeps the client; one naming a key that is not a modifier ends the connection; and a client
asking for FEATURE_WAIT alone, as Linda's X route does, is granted that alone, and is dropped for a
LATE_PASS.

The file has two halves. On the host, run(ground) drives the guest. In the guest, as
ground-late-passer, which cases/keydLatePass.nix installs, it is a client of the helper's of its own,
written from protocol.rs, which answers within a millisecond of each key, as Linda does and as
guest/keydClient.py, at a third of a second a call, cannot:

	keydLatePass.py MODE NAME READY

It connects as a program of the session in front, asks for FEATURE_WAIT and FEATURE_LATE_PASS, sends
START, waits until the keyboard named NAME is held, creates the file READY, and then keeps the first
press of KEPT and passes every other press, sending LATE_PASS for the kept one just before the
VERDICT that passes Tab. MODE says which key KEPT is: Alt for "alt", A for "letter". Once Tab and the
kept key are both up, or the helper has said goodbye, it sends STOP, prints one JSON object of what it
was sent and exits.
"""

import json
import os
import socket
import struct
import sys
import threading
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

if __name__ != "__main__":
	from ground import VIRTUAL_KEYBOARD

if TYPE_CHECKING:
	from ground import FakeKeyboard, Ground

UHID = "Ground uhid keyboard"
#: FEATURE_WAIT and FEATURE_LATE_PASS in protocol.rs.
FEATURE_WAIT, FEATURE_LATE_PASS = 2, 4
#: The file the guest half creates once the keyboard is held.
READY = "/tmp/ground/late-passer-ready"
#: How long after the hand lets Alt go its release may reach the virtual keyboard: the helper reads
#: it and puts it out within milliseconds, and the guest's clock and a loaded host allow the rest.
RELEASE_SLACK = 0.1


def latePasser(ground: "Ground", name: str, mode: str, keyboard: "FakeKeyboard", steps: list[dict]) -> dict:
	"""Start the guest half in mode, type steps once the keyboard is held, and answer what it saw, with
	"written", what the keyboard wrote."""
	ground.shell(f"rm -f {READY}")
	ground.start(name, ["ground-late-passer", mode, UHID, READY])
	ground.waitFor(lambda: ground.machine.execute(f"test -e {READY}")[0] == 0, "the late passer to be held")
	written = keyboard.run(*steps)
	ground.waitFor(lambda: not ground.running(name), "the late passer to end", 20)
	seen = json.loads(ground.shell(f"cat /tmp/ground/jobs/{name}.out"))
	seen["written"] = written
	return seen


def virtualKeys(ground: "Ground", source: str, since: object) -> list[dict]:
	"""Presses and releases of the keys these tests use on the virtual keyboard, repeats of an Alt
	held left out."""
	return [
		r
		for r in ground.keys(source, since, device=VIRTUAL_KEYBOARD)
		if r["key"] in ("KEY_LEFTALT", "KEY_TAB", "KEY_A") and r["value"] in (0, 1)
	]


def writtenAt(written: list[dict], key: str, value: int) -> float:
	return next(r["t"] for r in written if (r["key"], r["value"]) == (key, value))


def run(ground: "Ground") -> None:
	ground.openWindow()
	ground.waitForHelper()
	keyboard = ground.uhidKeyboard(UHID)
	# The X guest has the X server's own record of the virtual keyboard; a Wayland one has none.
	sources = ["evdev"] if ground.guest else ["evdev", "x"]

	with ground.subtest(
		"Alt kept and late-passed reaches the virtual keyboard ahead of Tab, up with the hand"
	):
		mark = ground.mark()
		steps = [
			{"press": "KEY_LEFTALT"},
			{"sleep": 0.15},
			{"tap": "KEY_TAB", "hold": 0.05},
			{"sleep": 0.3},
			{"release": "KEY_LEFTALT"},
		]
		seen = latePasser(ground, "late-alt", "alt", keyboard, steps)
		ground.machine.log(f"the late passer saw {json.dumps(seen)}")
		assert seen["welcome"]["features"] == FEATURE_WAIT | FEATURE_LATE_PASS, seen
		assert seen["goodbye"] is None, seen
		assert seen["latePass"], seen
		expected = [("KEY_LEFTALT", 1), ("KEY_TAB", 1), ("KEY_TAB", 0), ("KEY_LEFTALT", 0)]
		for source in sources:
			ground.waitFor(lambda s=source: len(virtualKeys(ground, s, mark)) >= 4, f"four keys at {source}")
			got = virtualKeys(ground, source, mark)
			assert [(r["key"], r["value"]) for r in got] == expected, (source, got)
		got = virtualKeys(ground, "evdev", mark)
		handUp = writtenAt(seen["written"], "KEY_LEFTALT", 0)
		tabAt = writtenAt(seen["written"], "KEY_TAB", 1)
		ground.machine.log(
			f"from the hand's Tab: Alt on the virtual keyboard {got[0]['t'] - tabAt:+.4f} s, Tab "
			f"{got[1]['t'] - tabAt:+.4f} s; from the hand letting Alt go, its release there "
			f"{got[3]['t'] - handUp:+.4f} s"
		)
		# Alt went out only once the client passed Tab, so never before the hand pressed Tab, and just
		# before Tab, in Tab's place.
		assert tabAt <= got[0]["t"] <= got[1]["t"], got
		assert got[1]["t"] - got[0]["t"] < 0.01, got
		# Up when the hand let it go, not when Tab went up.
		assert handUp <= got[3]["t"] < handUp + RELEASE_SLACK, (handUp, got)

	with ground.subtest("a late pass naming an Alt no longer held puts nothing out and keeps the client"):
		mark = ground.mark()
		steps = [
			{"tap": "KEY_LEFTALT", "hold": 0.05},
			{"sleep": 0.15},
			{"tap": "KEY_TAB", "hold": 0.05},
			{"sleep": 0.2},
		]
		seen = latePasser(ground, "late-gone", "alt", keyboard, steps)
		ground.machine.log(f"the late passer saw {json.dumps(seen)}")
		assert seen["latePass"] and seen["goodbye"] is None, seen
		assert seen["released"] == "STOPPED", seen
		for source in sources:
			ground.waitFor(lambda s=source: len(virtualKeys(ground, s, mark)) >= 2, f"Tab at {source}")
			ground.settle(0.2)
			got = virtualKeys(ground, source, mark)
			assert [(r["key"], r["value"]) for r in got] == [("KEY_TAB", 1), ("KEY_TAB", 0)], (source, got)

	with ground.subtest("a late pass naming a key that is not a modifier ends the connection"):
		mark = ground.mark()
		steps = [
			{"press": "KEY_A"},
			{"sleep": 0.15},
			{"tap": "KEY_TAB", "hold": 0.05},
			{"sleep": 0.2},
			{"release": "KEY_A"},
		]
		seen = latePasser(ground, "late-letter", "letter", keyboard, steps)
		ground.machine.log(f"the late passer saw {json.dumps(seen)}")
		assert seen["goodbye"] and seen["goodbye"]["reason"] == "MALFORMED", seen
		# The words go to the journal, so they name no key.
		assert "LATE_PASS" in seen["goodbye"]["text"] and not any(
			c.isdigit() for c in seen["goodbye"]["text"]
		)
		ground.settle(0.3)
		got = virtualKeys(ground, "evdev", mark)
		assert ("KEY_A", 1) not in [(r["key"], r["value"]) for r in got], got
		# Let go: the keyboard reaches a reader of its own node again.
		after = ground.mark()
		keyboard.tap("KEY_B")
		ground.waitForKeys("evdev", [("KEY_B", 1), ("KEY_B", 0)], after, device=UHID)

	with ground.subtest("a client asking for FEATURE_WAIT alone, as Linda's X route does, may not late-pass"):
		client = ground.keydClient("main")
		answer = client.connect(features=FEATURE_WAIT)
		assert answer["welcome"]["features"] == FEATURE_WAIT, answer
		client.capture(UHID)
		mark = ground.mark()
		client.call("latePass", kept=1, before=2)
		goodbye = client.waitFor("GOODBYE", mark)
		assert goodbye["reason"] == "MALFORMED" and "0x07" in goodbye["text"], goodbye
		after = ground.mark()
		keyboard.tap("KEY_B")
		ground.waitForKeys("evdev", [("KEY_B", 1), ("KEY_B", 0)], after, device=UHID)


# The guest half.

SOCKET_PATH = "/run/linda-keyd/socket"
HELLO, START, STOP, VERDICT, HEARTBEAT, TYPE, LATE_PASS = range(1, 8)
WELCOME, GOODBYE, KEY, EXPIRED, RELEASED, DEVICE = range(0x81, 0x87)
HELD = 1
KEY_ASK = 0x01
KEY_A, KEY_TAB, KEY_LEFTALT = 30, 15, 56
KEPT = {"alt": KEY_LEFTALT, "letter": KEY_A}
RELEASED_REASONS = {
	1: "STOPPED",
	2: "EMERGENCY",
	3: "BACKGROUND",
	4: "VIRTUAL_KEYBOARD",
	5: "SESSION_UNKNOWN",
}
GOODBYE_REASONS = {7: "MALFORMED"}


def decode(message: bytes) -> dict:
	kind = message[0]
	if kind == WELCOME:
		return {"type": "WELCOME", "features": struct.unpack("<BHIHHHH", message)[2]}
	if kind == GOODBYE:
		return {
			"type": "GOODBYE",
			"reason": GOODBYE_REASONS.get(message[1], message[1]),
			"text": message[4:].decode(errors="replace"),
		}
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
	return {"type": f"{kind:#04x}"}


class Passer:
	"""The client, which reads on a thread of its own and beats its heart on another."""

	def __init__(self, keyboard: str) -> None:
		self.records: list[dict] = []
		self.changed = threading.Condition()
		self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
		self.sock.connect(SOCKET_PATH)
		threading.Thread(target=self._read, daemon=True).start()
		self.send(struct.pack("<BHI", HELLO, 1, FEATURE_WAIT | FEATURE_LATE_PASS))
		self.welcome = self.waitFor(lambda r: r["type"] in ("WELCOME", "GOODBYE"), 10)
		assert self.welcome["type"] == "WELCOME", self.welcome
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
			try:
				self.send(struct.pack("<BI", HEARTBEAT, counter))
			except OSError:
				return

	def send(self, message: bytes) -> None:
		self.sock.send(message)

	def waitFor(self, where: Callable[[dict], bool], timeout: float = 5) -> dict:
		with self.changed:
			if not self.changed.wait_for(lambda: any(where(r) for r in self.records), timeout):
				raise AssertionError(f"waited {timeout} s; the helper sent {self.records[-20:]}")
			return next(r for r in self.records if where(r))


def serve(mode: str, keyboard: str, ready: str) -> dict:
	passer = Passer(keyboard)
	kept = KEPT[mode]
	with open(ready, "w"):
		pass
	keptSeq = None
	latePass = None
	up: set[int] = set()
	done = 0
	deadline = time.monotonic() + 10
	goodbye = None
	while time.monotonic() < deadline and not ({kept, KEY_TAB} <= up or goodbye):
		with passer.changed:
			passer.changed.wait_for(lambda done=done: len(passer.records) > done, 0.1)
			fresh = passer.records[done:]
		done += len(fresh)
		for r in fresh:
			if r["type"] in ("GOODBYE", "CLOSED"):
				goodbye = r
				break
			if r["type"] != "KEY":
				continue
			if r["value"] == 0:
				up.add(r["code"])
			if r["value"] != 1 or not r["flags"] & KEY_ASK:
				continue
			if r["code"] == kept and keptSeq is None:
				keptSeq = r["seq"]
				passer.send(struct.pack("<BQB", VERDICT, keptSeq, 1))
				continue
			if r["code"] == KEY_TAB and keptSeq is not None:
				latePass = (keptSeq, r["seq"])
				passer.send(struct.pack("<BQQ", LATE_PASS, keptSeq, r["seq"]))
			try:
				passer.send(struct.pack("<BQB", VERDICT, r["seq"], 0))
			except OSError:
				break
	released = None
	if goodbye is None:
		passer.send(bytes([STOP]))
		released = passer.waitFor(lambda r: r["type"] == "RELEASED")["reason"]
	else:
		# A GOODBYE comes before the helper closes, so wait for the close to see them both.
		passer.waitFor(lambda r: r["type"] == "CLOSED")
		goodbye = next((r for r in passer.records if r["type"] == "GOODBYE"), goodbye)
	passer.sock.close()
	return {
		"welcome": passer.welcome,
		"latePass": latePass,
		"goodbye": goodbye if goodbye and goodbye["type"] == "GOODBYE" else None,
		"released": released,
		"keys": [r for r in passer.records if r["type"] in ("KEY", "EXPIRED")],
	}


if __name__ == "__main__":
	if len(sys.argv) == 4 and sys.argv[1] in KEPT:
		print(json.dumps(serve(sys.argv[1], sys.argv[2], sys.argv[3])))
	else:
		sys.exit("usage: keydLatePass.py alt|letter NAME READY")
	sys.stdout.flush()
	os._exit(0)
