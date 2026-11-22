"""Every safety layer of linda-keyd, proven in a Wayland guest with the helper's own test client and
no Linda, as the keyd cases prove them in the X guest, so that Linda's own acceptance on a desktop
stands on a helper seen to hold there: the grab, the virtual keyboard's settle, what the helper costs
a key, the lock lights, keyboards that come and go, the emergency chord, a switch to a text console
and back, the helper's own watchdog thread, and systemd's watchdog for a helper stopped outright.

The file has two halves. On the host, run(ground) runs it all as subtests of one boot, in an order in
which each leaves the helper as the next expects, and answers the figures it measured by name. A
Wayland guest has no recorder that sees every key, as the X server's does in the X guest: what the
helper passes is shown by the evdev recorder's records of the virtual keyboard, what reaches the
compositor from a keyboard itself by the evdev recorder's records of that keyboard, which no reader
has while the helper grabs it, and what reaches a program by the native GTK window's records. The
lock lights are read from the kernel, by ground-poke. A release the kernel makes itself as a device
goes, the virtual keyboard's at a helper's end among them, is timed by when the recorder read it,
which is no earlier than it happened, so that a bound it is held to is never met by a stamp. Where a
bound is the helper's and the host's scheduling would otherwise count in it, the guest kernel's
tracer times what the helper and systemd did, on the same clock: the helper's own lines and ungrabs
on a switch to a text console, and systemd's SIGKILL to a stopped helper.

In the guest, as ground-helper-safety, which a case's module installs (cases/wlGnomeHelper.nix), it
watches for the moment the compositor opens a keyboard:

	helperSafety.py opened NAME UID SECONDS OUT

It writes one JSON object a line to OUT: one of kind "watching" once it looks, then one of kind
"result" with "node", the event node of the input device named NAME and "appeared", when its sysfs
entry was first seen, and "opened", when a process of user UID was first seen to hold that node open,
with that process's "pid" and "comm"; each time on the guest's CLOCK_MONOTONIC, and null for what did
not happen within SECONDS.
"""

import json
import os
import re
import shlex
import statistics
import sys
import time
from contextlib import AbstractContextManager, nullcontext
from typing import TYPE_CHECKING

if __name__ != "__main__":
	from ground import (
		AT_KEYBOARD,
		KEYD_UNIT,
		USB_KEYBOARD,
		USER,
		VIRTIO_KEYBOARD,
		VIRTUAL_KEYBOARD,
		WINDOW_TITLE,
		Mark,
		keyEvents,
	)

if TYPE_CHECKING:
	from ground import FakeKeyboard, Ground, KeydClient

UHID = "Ground uhid keyboard"
#: A second uhid keyboard, made while the helper captures.
LATE_UHID = "Ground uhid keyboard 2"
CHORD = ["KEY_BACKSPACE", "KEY_ESC", "KEY_ENTER"]
#: The lights as bits of their LED codes: Num Lock 1, Caps Lock 2.
NUM, CAPS = 1, 2
LATENCY_KEYS = [f"KEY_{c}" for c in "QWERTYUIOPASDFGHJKLZ"]
#: STALL_LIMIT in protocol.rs.
STALL = 1.0
#: The exit status of a helper its watchdog thread ends, WATCHDOG_EXIT in main.rs.
WATCHDOG_EXIT = 70
#: WatchdogSec in linda-keyd.service.in, which counts from the loop's last feed, before any stop.
WATCHDOG = 2.0
#: What the journal's stamps and a loaded host add to a bound the helper keeps; the X cases allow the
#: same.
SLACK = 0.3
#: Seconds after the virtual keyboard's Shift is released by its end within which the desktop must
#: stop applying it to keys from a real keyboard.
SHIFT_CLEARS = 1.0
#: The emergency chord's promise: keys reach the program from the keyboard itself within this.
CHORD_BACK = 1.0
#: Where continueAtChord's watch writes when it woke the client, and the seconds it waits for the
#: chord at most.
CONTINUED = "/tmp/ground/continued"
CHORD_WATCH = 10
OPENED = "/tmp/ground/virtualOpened.jsonl"
CONSOLE_TEXT = "/tmp/ground/console.txt"
CONSOLE_LETTERS = "abcdefghijklmnopqrstuvwxyz"
#: Seconds between the letters typed across a switch. Under GNOME on a loaded host the kernel put the
#: console in front a second after chvt, where the X server lets it go at once, so the letters run
#: some five seconds, to go on well past it.
CONSOLE_GAP = 0.2
#: Seconds after the helper holds again on a switch back before the key console() asserts on is
#: typed. E15 found one second, the helper's VIRTUAL_SETTLE, enough in every trial but a loaded one;
#: in this suite on sway, where the switch back follows a switch away made while typing, a key typed
#: 1.01 s after the grab was lost (results/VX-guests.txt), so twice that.
RESETTLE = 2.0
#: HEARTBEAT_TIMEOUT in protocol.rs: a client silent this long is dropped.
HEARTBEAT = 2.0
#: Seconds the helper's own work may take between its line for the kernel's switch and its line for
#: the let-go, past the time the kernel spent in its ungrabs: reading what each keyboard holds,
#: putting it out and ending the watch for keyboards, some 15 ms on sway. Each ungrab waits for two
#: RCU grace periods, which a guest whose virtual processors the host does not run cannot end, so
#: they are left out of it.
LET_GO_OWN = 0.05
#: Seconds after systemd's SIGKILL within which the kernel, closing the dead helper's devices, must
#: release what its virtual keyboard held and give the keyboard back to every reader; the evdev
#: recorder's reading of it counts in it.
AFTER_KILL = 0.3
#: Taps typed while the helper is stopped, one every STREAM_GAP seconds: past systemd's watchdog.
STREAM_TAPS = 80
STREAM_GAP = 0.05
#: The guest kernel's tracer, which times the helper's system calls on CLOCK_MONOTONIC, the clock
#: everything else here is stamped with.
TRACING = "/sys/kernel/tracing"
#: The ioctl that grabs an evdev node, with 1, and lets it go, with 0.
EVIOCGRAB = 0x40044590
TRACE_LINE = re.compile(
	r"-(?P<tid>\d+)\s+(?:\(\s*\S+\)\s+)?\[\d+\]\s+(?:\S+\s+)?(?P<t>\d+\.\d+): (?P<what>.*)$"
)
#: Seconds between two looks of the guest half.
WATCH_INTERVAL = 0.002


def typedKeys(*keys: str) -> list[tuple[str, int]]:
	return [event for key in keys for event in ((key, 1), (key, 0))]


def summary(delays: list[float]) -> dict[str, float]:
	ms = sorted(d * 1000 for d in delays)
	return {
		"medianMs": round(statistics.median(ms), 3),
		"leastMs": round(ms[0], 3),
		"mostMs": round(ms[-1], 3),
		"count": len(ms),
	}


def lights(ground: "Ground", name: str) -> int:
	return next(iter(json.loads(ground.shell(f"ground-poke leds {shlex.quote(name)}")).values()))


def uhidLights(keyboard: "FakeKeyboard") -> list[int]:
	return [r["leds"] for r in keyboard.records() if "leds" in r]


def restartHelper(ground: "Ground", client: "KeydClient", how: str) -> int:
	"""Make the helper start again, by how, a shell command, and answer the new one's pid. The starts
	counted against the unit's limit are forgotten first, as the unit's comment allows, so that the
	limit, which this boot would otherwise reach, is not what a subtest measures."""
	old = ground.helperPid()
	ground.shell("systemctl reset-failed linda-keyd")
	mark = ground.mark()
	ground.shell(how)
	pid = ground.waitFor(
		lambda: (p := ground.helperPid()) not in (0, old) and p, "the helper to start again", 15
	)
	ground.waitForHelper()
	client.waitFor("CLOSED", mark)
	return pid


def deviceAdded(ground: "Ground", name: str, since: "Mark") -> float:
	return next(
		r["t"]
		for r in ground.records("evdev", since)
		if r["kind"] == "device" and r["device"] == name and r["change"] == "added"
	)


def appKeyvals(records: list[dict]) -> list[str]:
	return [r["keyval"] for r in records if r["value"] == 1]


def released(records: list[dict]) -> dict:
	"""The first release among records, past the kernel's repeats of a key held on the virtual
	keyboard, which it repeats as it does a real one's."""
	return next(r for r in records if r["value"] == 0)


def logTimeline(ground: "Ground", client: "KeydClient", since: "Mark", zero: float, what: str) -> None:
	"""Every key the test typed since, as each layer had it, in seconds from zero: the evdev recorder's
	by the kernel's stamp and by its own reading, the client's and the program's."""
	lines = [
		f"evdev {r['device']}: {r['key']} {r['value']} {r['t'] - zero:+.3f} read {r['received'] - zero:+.3f}"
		for r in ground.keys("evdev", since)
	]
	lines += [f"client: {r['key']} {r['value']} {r['t'] - zero:+.3f}" for r in client.keys(since)]
	lines += [
		f"program: {r['key']} {r['value']} {r['keyval']} {r['t'] - zero:+.3f}"
		for r in ground.keys("app", since)
	]
	ground.machine.log(f"{what}, each key in seconds from it:\n" + "\n".join(lines))


def waitForKeyFrom(ground: "Ground", key: str, since: "Mark", device: str) -> list[dict]:
	"""Wait until the evdev recorder has a tap of key from device, whatever else came between: once
	the helper lets go, the releases of the keys it held reach every reader of the keyboard, where in
	the X guest the X server dropped them, never having had their presses."""
	found: list[dict] = []

	def arrived() -> bool:
		found[:] = [r for r in ground.keys("evdev", since, device=device) if r["key"] == key]
		return keyEvents(found)[:2] == typedKeys(key)

	ground.waitFor(arrived, f"{key} from {device} at the evdev recorder", 5)
	return found


def startTrace(ground: "Ground", events: dict[str, str], pids: list[str] | None = None) -> None:
	"""Have the guest kernel's tracer record events, each a tracepoint under events/ with its filter,
	"" for none, of the threads pids alone if given, until readTrace."""
	ground.shell(
		f"set -e; [ -e {TRACING}/trace ] || mount -t tracefs nodev {TRACING}; "
		f"echo 0 > {TRACING}/tracing_on; echo mono > {TRACING}/trace_clock; "
		+ (f"echo {' '.join(pids)} > {TRACING}/set_event_pid; " if pids else "")
		+ "".join(
			(f"echo {shlex.quote(where)} > {TRACING}/events/{event}/filter; " if where else "")
			+ f"echo 1 > {TRACING}/events/{event}/enable; "
			for event, where in events.items()
		)
		+ f"echo > {TRACING}/trace; echo 1 > {TRACING}/tracing_on"
	)


def traceHelper(ground: "Ground", pid: int) -> dict[str, str]:
	"""Trace, for every thread of the helper's, each EVIOCGRAB it asks and the end of each ioctl, and
	each line it sends the journal; answers what readTrace is to be given."""
	events = {
		"syscalls/sys_enter_ioctl": f"cmd == {EVIOCGRAB}",
		"syscalls/sys_exit_ioctl": "",
		"syscalls/sys_enter_sendto": "fd == 2",
	}
	startTrace(ground, events, ground.shell(f"ls /proc/{pid}/task").split())
	return events


def traceKill(ground: "Ground", pid: int) -> dict[str, str]:
	"""Trace the SIGKILL anyone sends pid; answers what readTrace is to be given."""
	events = {"signal/signal_generate": f"sig == 9 && pid == {pid}"}
	startTrace(ground, events)
	return events


def readTrace(ground: "Ground", events: dict[str, str]) -> list[dict]:
	"""What the tracer recorded of events since startTrace, each a dict with "tid", "t", and "call":
	"grab" with "arg", "ended", "send" with "len", or "kill"; the tracer is then put back as it was."""
	text = ground.shell(
		f"echo 0 > {TRACING}/tracing_on; cat {TRACING}/trace; "
		+ "".join(
			f"echo 0 > {TRACING}/events/{event}/enable; echo 0 > {TRACING}/events/{event}/filter; "
			for event in events
		)
		+ f"echo > {TRACING}/set_event_pid; echo > {TRACING}/trace; echo local > {TRACING}/trace_clock"
	)
	found = []
	for line in text.splitlines():
		match = TRACE_LINE.search(line)
		if not match or line.startswith("#"):
			continue
		what = match["what"]
		args = {name: int(value, 16) for name, value in re.findall(r"(\w+): (?:0x)?([0-9a-f]+)\b", what)}
		record = {"tid": int(match["tid"]), "t": float(match["t"])}
		if what.startswith("sys_ioctl("):
			found.append({**record, "call": "grab", "arg": args.get("arg")})
		elif what.startswith("sys_ioctl ->"):
			found.append({**record, "call": "ended"})
		elif what.startswith("sys_sendto("):
			found.append({**record, "call": "send", "len": args.get("len")})
		elif what.startswith("signal_generate:"):
			found.append({**record, "call": "kill"})
	return found


def ungrabs(traced: list[dict], start: float, end: float) -> list[tuple[float, float]]:
	"""Each EVIOCGRAB that let a keyboard go, begun between start and end, as (begun, ended): a
	thread makes one call at a time, so its next ioctl's end is that call's."""
	spans = []
	for index, record in enumerate(traced):
		if record["call"] != "grab" or record["arg"] != 0 or not start <= record["t"] <= end:
			continue
		ended = next(
			(r["t"] for r in traced[index + 1 :] if r["tid"] == record["tid"] and r["call"] == "ended"), None
		)
		assert ended is not None, f"an ungrab of the helper's at {record['t']} never ended: {traced}"
		spans.append((record["t"], ended))
	return spans


def sent(traced: list[dict], line: str, after: float = 0.0) -> float:
	"""When the helper sent the journal line, its text as the journal has it, at or after after: each
	log line is one send of the text and its newline."""
	size = len(line.encode()) + 1
	found = [r["t"] for r in traced if r["call"] == "send" and r["len"] == size and r["t"] >= after]
	assert found, f"no send of {size} bytes, for {line!r}, was traced: {traced}"
	return found[0]


# The grab, and the virtual keyboard's settle.


def grab(ground: "Ground", keyboard: "FakeKeyboard", client: "KeydClient", figures: dict) -> None:
	with ground.subtest("before START nothing is grabbed"):
		assert client.connect()["welcome"]
		mark = ground.mark()
		keyboard.tap("KEY_A")
		ground.waitForKeys("evdev", typedKeys("KEY_A"), mark, device=UHID)
		app = ground.waitForKeys("app", typedKeys("KEY_A"), mark)
		assert appKeyvals(app) == ["a"], app
		ground.settle(0.3)
		assert not client.keys(mark), client.keys(mark)

	with ground.subtest("the compositor has the virtual keyboard before the helper takes it for ready"):
		uid = ground.shell(f"id -u {USER}").strip()
		ground.shell(
			f"rm -f {OPENED}; systemd-run --collect --unit=ground-opened ground-helper-safety opened "
			f"{shlex.quote(VIRTUAL_KEYBOARD)} {uid} 30 {OPENED}"
		)
		ground.waitFor(lambda: ground.readJsonLines(OPENED), "the watch on the virtual keyboard")
		mark = ground.mark()
		client.start()
		held = client.waitForDevice(UHID, "HELD", mark)
		written = keyboard.tap("KEY_A", hold=0.02)
		made = ground.waitForHelperLog("made the virtual keyboard", mark)[0]
		ready = ground.waitForHelperLog("the virtual keyboard is ready", mark)[0]
		watched = ground.waitFor(
			lambda: [r for r in ground.readJsonLines(OPENED) if r["kind"] == "result"],
			"the watch to end",
			40,
		)[0]
		node = deviceAdded(ground, VIRTUAL_KEYBOARD, mark)
		ground.machine.log(f"the watch on the virtual keyboard: {watched}")
		assert watched["opened"] is not None, f"the compositor never opened the virtual keyboard: {watched}"
		settle = {
			"made": made - mark.t,
			"sysfs": watched["appeared"] - mark.t,
			"evdevRecorder": node - mark.t,
			"compositorOpened": watched["opened"] - mark.t,
			"ready": ready - mark.t,
			"held": held["t"] - mark.t,
			"compositorAheadOfReady": ready - watched["opened"],
		}
		figures["settle"] = {name: round(value, 3) for name, value in settle.items()}
		ground.machine.log(
			f"from START, seconds: {figures['settle']}; the helper's stamps are the journal's, when it "
			"read each line, so a little late"
		)
		assert watched["opened"] < ready, (watched, ready)
		app = ground.waitForKeys("app", typedKeys("KEY_A"), mark)
		ground.waitForKeys("evdev", typedKeys("KEY_A"), mark, device=VIRTUAL_KEYBOARD)
		ground.machine.log(
			f"A, typed {written[0]['t'] - held['t']:.3f} s after the client heard of the grab, reached "
			f"the program {app[0]['t'] - written[0]['t']:.3f} s after it was typed"
		)
		assert appKeyvals(app) == ["a"], app

	with ground.subtest("START holds every keyboard of the seat"):
		for name in (VIRTIO_KEYBOARD, AT_KEYBOARD):
			client.waitForDevice(name, "HELD", mark)

	with ground.subtest("a passed key reaches the program through the virtual keyboard alone"):
		for name, press in (
			(UHID, lambda: keyboard.tap("KEY_B")),
			(VIRTIO_KEYBOARD, lambda: ground.press("b")),
		):
			mark = ground.mark()
			press()
			typed = typedKeys("KEY_B")
			ground.waitForKeys("evdev", typed, mark, device=VIRTUAL_KEYBOARD)
			app = ground.waitForKeys("app", typed, mark)
			assert appKeyvals(app) == ["b"], app
			keys = client.keys(mark)
			assert [(k["key"], k["value"], k["ask"]) for k in keys] == [
				("KEY_B", 1, True),
				("KEY_B", 0, False),
			], keys
			ground.settle(0.3)
			# Nothing from the keyboard itself to any reader, the compositor among them.
			assert not ground.keys("evdev", mark, device=name), (
				name,
				ground.keys("evdev", mark, device=name),
			)
			ground.machine.log(f"{name}'s B went through the virtual keyboard")

	with ground.subtest("a kept key reaches nobody but the client"):
		client.verdicts("pass", KEY_K="keep")
		mark = ground.mark()
		keyboard.tap("KEY_K")
		client.waitFor("KEY", mark, lambda r: r["key"] == "KEY_K" and r["value"] == 0)
		ground.settle(0.5)
		for source in ("evdev", "app"):
			assert not ground.keys(source, mark), (source, ground.keys(source, mark))
		client.verdicts("pass")

	with ground.subtest("a keyboard with a key down is grabbed only once the key is up"):
		mark = ground.mark()
		client.stop()
		client.waitFor("RELEASED", mark, lambda r: r["reason"] == "STOPPED")
		keyboard.run({"press": "KEY_LEFTSHIFT"})
		ground.waitForKeys("evdev", [("KEY_LEFTSHIFT", 1)], mark, device=UHID)
		ground.waitForKeys("app", [("KEY_LEFTSHIFT", 1)], mark)
		mark = ground.mark()
		client.start()
		client.waitForDevice(UHID, "WAITING", mark)
		client.waitForDevice(VIRTIO_KEYBOARD, "HELD", mark)
		ground.settle(0.5)
		held = [r for r in client.records(mark, "DEVICE") if r["name"] == UHID and r["status"] == "HELD"]
		assert not held, held
		release = ground.mark()
		keyboard.run({"release": "KEY_LEFTSHIFT"})
		# The release reaches the compositor from the keyboard, which gave it the press, so no Shift is
		# left down there.
		ground.waitForKeys("evdev", [("KEY_LEFTSHIFT", 0)], release, device=UHID)
		ground.waitForKeys("app", [("KEY_LEFTSHIFT", 0)], release)
		client.waitForDevice(UHID, "HELD", release)
		mark = ground.mark()
		keyboard.tap("KEY_X")
		app = ground.waitForKeys("app", typedKeys("KEY_X"), mark)
		assert appKeyvals(app) == ["x"], app

	with ground.subtest("STOP lets go of every keyboard"):
		mark = ground.mark()
		client.stop()
		client.waitFor("RELEASED", mark, lambda r: r["reason"] == "STOPPED")
		mark = ground.mark()
		keyboard.tap("KEY_C")
		ground.press("d")
		ground.waitForKeys("evdev", typedKeys("KEY_C"), mark, device=UHID)
		ground.waitForKeys("evdev", typedKeys("KEY_D"), mark, device=VIRTIO_KEYBOARD)
		app = ground.waitForKeys("app", typedKeys("KEY_C", "KEY_D"), mark)
		assert appKeyvals(app) == ["c", "d"], app
		assert not client.keys(mark), client.keys(mark)


# What the helper costs a key, as keydLatency measures it in the X guest.


def latency(ground: "Ground", keyboard: "FakeKeyboard", client: "KeydClient", figures: dict) -> None:
	measured: dict[str, object] = {}
	figures["latency"] = measured

	def typed(written: list[dict]) -> list[tuple[str, int]]:
		return [(r["key"], r["value"]) for r in written]

	with ground.subtest("a keyboard nothing holds: from the write to the kernel's event and the program"):
		mark = ground.mark()
		written = keyboard.tap(*LATENCY_KEYS, hold=0.02, gap=0.03)
		evdev = ground.waitForKeys("evdev", typed(written), mark, device=UHID)
		app = ground.waitForKeys("app", typed(written), mark)
		alone = [d for *_, d in ground.delays(written, evdev)]
		measured["aloneWriteToEvent"] = summary(alone)
		measured["aloneWriteToProgram"] = summary([d for *_, d in ground.delays(written, app)])

	client.capture(UHID)

	with ground.subtest("held, with a client that answers at once: from the write to the virtual keyboard"):
		mark = ground.mark()
		written = keyboard.tap(*LATENCY_KEYS, hold=0.02, gap=0.03)
		evdev = ground.waitForKeys("evdev", typed(written), mark, device=VIRTUAL_KEYBOARD)
		app = ground.waitForKeys("app", typed(written), mark)
		held = [d for *_, d in ground.delays(written, evdev)]
		measured["heldWriteToVirtualEvent"] = summary(held)
		measured["virtualEventToProgram"] = summary([d for *_, d in ground.delays(evdev, app)])
		measured["heldWriteToProgram"] = summary([d for *_, d in ground.delays(written, app)])
		records = client.records(mark)
		asked = {r["seq"]: r for r in records if r["type"] == "KEY" and r["ask"]}
		answered = {r["seq"]: r for r in records if r["type"] == "VERDICT"}
		presses = [r for r in written if r["value"] == 1]
		measured["writeToClient"] = summary(
			[a["t"] - w["t"] for w, a in zip(presses, asked.values(), strict=True)]
		)
		measured["clientAnswer"] = summary([answered[seq]["t"] - a["t"] for seq, a in asked.items()])
		virtualPresses = [r for r in evdev if r["value"] == 1]
		measured["verdictToVirtualEvent"] = summary(
			[v["t"] - answered[seq]["t"] for seq, v in zip(asked, virtualPresses, strict=True)]
		)
		measured["addedByMedianMs"] = round((statistics.median(held) - statistics.median(alone)) * 1000, 3)
		ground.machine.log(f"what the helper costs a key: {json.dumps(measured)}")
		assert not client.records(mark, "EXPIRED"), "a key the client answered at once expired"
		# Far under the verdict's bound, which a key reaches only when its client is in trouble.
		assert max(held) < 0.05, held

	with ground.subtest("a kept key reaches nobody"):
		client.verdicts("keep")
		mark = ground.mark()
		keyboard.tap(*LATENCY_KEYS[:5], hold=0.02, gap=0.03)
		client.waitFor("KEY", mark, lambda r: r["key"] == LATENCY_KEYS[4] and r["value"] == 0)
		ground.settle(0.5)
		for source in ("evdev", "app"):
			assert not ground.keys(source, mark), (source, ground.keys(source, mark))
		kept = client.records(mark, "VERDICT")
		assert len(kept) == 5 and all(r["keep"] for r in kept), kept
		client.verdicts("pass")


# The lock lights, as keydLocks proves them in the X guest, the compositor standing for the X server.

#: The fault the lock subtests find on sway, which they assert against so that its going is seen.
LIGHTS_GONE = (
	"sway takes the lights off every keyboard as the helper's virtual keyboard is added and keeps its "
	"locks, and the helper, which takes the locks from the lights it finds at its first grab, tells "
	"the client they are off (results/E6-sway.md)"
)
#: The deeper fault under it, which telling the client the right locks would not end.
NUM_DROPPED = (
	"sway takes the virtual keyboard's own state, in which Num Lock was never on, for the seat's at the "
	"first lock key passed through the helper, and so turns off the Num Lock the user had on"
)
#: The faults of the helper's that a desktop shows, by the guest's kind: each is the owner's to decide,
#: and the subtest that finds it asserts the truth as an expected failure.
LOCK_FAULTS = {"sway": (LIGHTS_GONE, NUM_DROPPED)}


def faultOn(ground: "Ground", fault: str) -> AbstractContextManager:
	"""An expected failure where this desktop shows fault, and an ordinary assertion elsewhere."""
	if fault in LOCK_FAULTS.get(ground.guest["kind"], ()):
		return ground.expectedFailure(fault)
	return nullcontext()


def keypad(ground: "Ground") -> list[str]:
	"""What the program had the numpad's 1 as, from QEMU's keyboard: KP_1 with Num Lock on."""
	mark = ground.mark()
	ground.press("kp_1")
	return appKeyvals(ground.waitForKeys("app", typedKeys("KEY_KP1"), mark))


def letterA(ground: "Ground", keyboard: "FakeKeyboard") -> list[str]:
	"""What the program had A as, from the uhid keyboard: A with Caps Lock on."""
	mark = ground.mark()
	keyboard.tap("KEY_A")
	return appKeyvals(ground.waitForKeys("app", typedKeys("KEY_A"), mark))


def desktopLocks(ground: "Ground", keyboard: "FakeKeyboard") -> dict[str, object]:
	"""The locks as the desktop applies them, as bits of the lights: Num Lock by the numpad's 1 and
	Caps Lock by a letter, with what the program had each as."""
	pad, letter = keypad(ground), letterA(ground, keyboard)
	bits = (NUM if pad == ["KP_1"] else 0) | (CAPS if letter == ["A"] else 0)
	return {"locks": bits, "keypad": pad, "letter": letter}


def realLights(ground: "Ground") -> dict[str, int]:
	return {name: lights(ground, name) for name in (VIRTIO_KEYBOARD, AT_KEYBOARD, UHID)}


def waitForLights(ground: "Ground", leds: int, what: str) -> dict[str, int]:
	"""Every real keyboard's lights once each is lit with leds; the desktop or the helper may be a
	moment writing them."""
	try:
		return ground.waitFor(
			lambda: (found := realLights(ground)) and set(found.values()) == {leds} and found, what, 3
		)
	except AssertionError as e:
		raise AssertionError(f"{e}: {leds} wanted, lit {realLights(ground)}") from None


def locks(ground: "Ground", keyboard: "FakeKeyboard", client: "KeydClient", figures: dict) -> None:
	"""The client must be told the locks the desktop applies, and every real keyboard lit with what it
	was told, whatever a desktop writes to the lights itself: mutter and KWin write the seat's locks to
	a new keyboard, sway writes every light off (E6-gnome.md, E6-plasma.md, E6-sway.md). Leaves the
	helper let go, with Caps Lock off."""
	seen: dict[str, object] = {}
	figures["locks"] = seen

	with ground.subtest("Num Lock on in the desktop before a new helper makes its virtual keyboard"):
		restartHelper(ground, client, "systemctl restart linda-keyd")
		before = lights(ground, VIRTIO_KEYBOARD)
		mark = ground.mark()
		ground.press("num_lock")
		ground.waitForKeys("evdev", [("KEY_NUMLOCK", 1), ("KEY_NUMLOCK", 0)], mark, device=VIRTIO_KEYBOARD)
		ground.waitFor(
			lambda: lights(ground, VIRTIO_KEYBOARD) == before ^ NUM, "the virtio keyboard's Num Lock light"
		)
		seen["numLockBefore"] = {
			"before": before,
			**realLights(ground),
			"uhidReports": uhidLights(keyboard),
			**desktopLocks(ground, keyboard),
		}
		ground.machine.log(f"Num Lock pressed with no virtual keyboard: {seen['numLockBefore']}")
		assert before == 0, f"the desktop started with Num Lock on: {before}"
		assert seen["numLockBefore"]["locks"] == NUM, seen["numLockBefore"]

	with ground.subtest("the locks at the start of capture are the desktop's, told and lit"):
		mark = ground.mark()
		client.capture(UHID, VIRTIO_KEYBOARD, AT_KEYBOARD, since=mark)
		ground.settle(1)
		told = [r["leds"] for r in client.records(mark, "LOCKS")]
		seen["atStart"] = {
			"told": told,
			"virtual": lights(ground, VIRTUAL_KEYBOARD),
			**realLights(ground),
			**desktopLocks(ground, keyboard),
		}
		ground.machine.log(f"at the start of capture: {seen['atStart']}")
		assert told, seen["atStart"]
		waitForLights(ground, told[-1], "every keyboard lit with what the client was told")
		# The desktop keeps the user's locks as the helper takes the keyboards, so that what follows
		# means something.
		assert seen["atStart"]["locks"] == NUM, seen["atStart"]
		with faultOn(ground, LIGHTS_GONE):
			assert told[-1] == seen["atStart"]["locks"], seen["atStart"]

	with ground.subtest("Caps Lock through the helper is locked, lit everywhere and told"):
		mark = ground.mark()
		keyboard.tap("KEY_CAPSLOCK")
		ground.waitForKeys("evdev", typedKeys("KEY_CAPSLOCK"), mark, device=VIRTUAL_KEYBOARD)
		toldCaps = client.waitFor("LOCKS", mark, lambda r: r["leds"] & CAPS)
		# sway tells twice within the second, the second time without Num Lock (E6-sway.md).
		ground.settle(1)
		told = [r["leds"] for r in client.records(mark, "LOCKS")]
		lit = waitForLights(ground, told[-1], "every keyboard lit with what the client was told")
		seen["caps"] = {
			"told": told,
			"toldAfter": round(toldCaps["t"] - mark.t, 3),
			"virtual": lights(ground, VIRTUAL_KEYBOARD),
			**lit,
			"uhidReports": uhidLights(keyboard),
			**desktopLocks(ground, keyboard),
		}
		ground.machine.log(f"Caps Lock through the helper: {seen['caps']}")
		assert seen["caps"]["letter"] == ["A"], seen["caps"]
		assert told[-1] == seen["caps"]["locks"], seen["caps"]
		assert seen["caps"]["uhidReports"][-1:] == [told[-1]], "the uhid keyboard was not sent its lights"
		with faultOn(ground, NUM_DROPPED):
			assert seen["caps"]["keypad"] == ["KP_1"], seen["caps"]

	with ground.subtest("letting go leaves the keyboards with the console's lights"):
		mark = ground.mark()
		client.stop()
		client.waitFor("RELEASED", mark)
		ground.settle(0.5)
		after = realLights(ground)
		seen["afterStop"] = after
		ground.machine.log(f"after STOP: lights {after}")
		# The kernel's console handler puts its own locks on a keyboard let go of, and the compositor
		# writes the lights again only when a lock changes.
		assert set(after.values()) == {0}, after

	with ground.subtest("a new helper takes the locks from the desktop, not from stale lights"):
		restartHelper(ground, client, "systemctl restart linda-keyd")
		mark = ground.mark()
		client.capture(UHID, VIRTIO_KEYBOARD, AT_KEYBOARD, since=mark)
		ground.settle(0.5)
		told = [r["leds"] for r in client.records(mark, "LOCKS")]
		seen["newHelper"] = {
			"told": told,
			"virtual": lights(ground, VIRTUAL_KEYBOARD),
			**realLights(ground),
			**desktopLocks(ground, keyboard),
		}
		ground.machine.log(f"a new helper: {seen['newHelper']}")
		assert told, seen["newHelper"]
		waitForLights(ground, told[-1], "every keyboard lit with what the client was told")
		assert seen["newHelper"]["locks"] & CAPS, seen["newHelper"]
		# Were the user's Num Lock lost across the restart, the desktop, the client and the lights would
		# all agree on Caps Lock alone, and the check above would pass.
		with faultOn(ground, NUM_DROPPED):
			assert seen["newHelper"]["locks"] == NUM | CAPS, seen["newHelper"]
		with faultOn(ground, LIGHTS_GONE):
			assert told[-1] == seen["newHelper"]["locks"], seen["newHelper"]

	with ground.subtest("let go again, the desktop lights the keyboards itself as a lock turns on"):
		mark = ground.mark()
		client.stop()
		client.waitFor("RELEASED", mark)
		again: dict[str, object] = {"lightsAtLetGo": realLights(ground)}
		seen["letGoAgain"] = again
		# Turning a lock off where every light is off already changes no light, so the tap that shows
		# whether the desktop lights the keyboards is one that turns Caps Lock on.
		mark = ground.mark()
		keyboard.tap("KEY_CAPSLOCK")
		ground.waitForKeys("evdev", typedKeys("KEY_CAPSLOCK"), mark, device=UHID)
		again["off"] = desktopLocks(ground, keyboard)
		assert again["off"]["letter"] == ["a"], again
		mark = ground.mark()
		keyboard.tap("KEY_CAPSLOCK")
		ground.waitForKeys("evdev", typedKeys("KEY_CAPSLOCK"), mark, device=UHID)
		again["on"] = desktopLocks(ground, keyboard)
		assert again["on"]["letter"] == ["A"], again
		again["lightsOn"] = waitForLights(ground, again["on"]["locks"], "the desktop to light the keyboards")
		ground.machine.log(f"let go again: {again}")
		keyboard.tap("KEY_CAPSLOCK")
		ground.waitFor(lambda: not lights(ground, UHID) & CAPS, "Caps Lock's light off again")
		assert letterA(ground, keyboard) == ["a"]
		with faultOn(ground, NUM_DROPPED):
			assert again["on"]["locks"] == NUM | CAPS, again


# Keyboards that come and go, as keydHotplug proves them.


def hotplug(ground: "Ground", client: "KeydClient", figures: dict) -> None:
	mark = ground.mark()
	client.capture(VIRTIO_KEYBOARD, since=mark)

	with ground.subtest("a USB keyboard plugged in is held, and has QEMU's keys"):
		mark = ground.mark()
		ground.plugUsbKeyboard()
		usb = client.waitForDevice(USB_KEYBOARD, "HELD", mark)
		mark = ground.mark()
		ground.press("e")
		ground.waitForKeys("evdev", typedKeys("KEY_E"), mark, device=VIRTUAL_KEYBOARD)
		app = ground.waitForKeys("app", typedKeys("KEY_E"), mark)
		assert appKeyvals(app) == ["e"], app
		keys = client.keys(mark)
		assert {k["device"] for k in keys} == {usb["device"]}, keys
		assert not ground.keys("evdev", mark, device=USB_KEYBOARD)

	with ground.subtest("a uhid keyboard made meanwhile is held"):
		mark = ground.mark()
		late = ground.uhidKeyboard(LATE_UHID)
		client.waitForDevice(LATE_UHID, "HELD", mark)
		mark = ground.mark()
		late.tap("KEY_B")
		ground.waitForKeys("evdev", typedKeys("KEY_B"), mark, device=VIRTUAL_KEYBOARD)
		ground.waitForKeys("app", typedKeys("KEY_B"), mark)
		assert not ground.keys("evdev", mark, device=LATE_UHID)

	with ground.subtest("the USB keyboard unplugged with Shift held has Shift released"):
		mark = ground.mark()
		ground.qemuKey("shift", True)
		pressed = ground.waitForKeys("evdev", [("KEY_LEFTSHIFT", 1)], mark, device=VIRTUAL_KEYBOARD)
		ground.settle(0.5)
		assert keyEvents(ground.keys("evdev", mark, device=VIRTUAL_KEYBOARD)) == [("KEY_LEFTSHIFT", 1)]
		unplugging = ground.now()
		ground.unplugUsbKeyboard()
		release = ground.waitForKeys(
			"evdev", [("KEY_LEFTSHIFT", 1), ("KEY_LEFTSHIFT", 0)], mark, device=VIRTUAL_KEYBOARD
		)
		ground.waitForKeys("app", [("KEY_LEFTSHIFT", 1), ("KEY_LEFTSHIFT", 0)], mark)
		gone = client.waitForDevice(USB_KEYBOARD, "GONE", mark)
		releases = [r for r in client.keys(mark) if r["device"] == gone["device"] and r["value"] == 0]
		figures["hotplug"] = {
			"shiftHeld": round(released(release)["received"] - pressed[0]["received"], 3),
			"clientToldOfReleaseAfterUnplugAsked": round(releases[0]["t"] - unplugging, 3)
			if releases
			else None,
			"goneAfterUnplugAsked": round(gone["t"] - unplugging, 3),
			"releaseMadeByHelper": [r["synthetic"] for r in releases],
		}
		ground.machine.log(f"the USB keyboard unplugged with Shift held: {figures['hotplug']}")
		# The kernel's own release, which it sends as it takes a device away, reaches the helper before
		# the device goes, so the helper need not make one; either way Shift is let go of.
		assert [r["key"] for r in releases] == ["KEY_LEFTSHIFT"], releases
		ground.qemuKey("shift", False)
		mark = ground.mark()
		late.tap("KEY_Q")
		app = ground.waitForKeys("app", typedKeys("KEY_Q"), mark)
		assert appKeyvals(app) == ["q"], app


# The emergency chord, as keydEmergency proves it.


def continueAtChord(ground: "Ground", pid: int) -> None:
	"""Start a watch in the guest that sends the stopped client pid SIGCONT as soon as the helper's log
	shows the emergency chord, and then makes CONTINUED. The helper drops a client silent for
	HEARTBEAT_TIMEOUT, and on a loaded host the host's own round trips to see the chord and signal took
	the client past it (results/VH-plasma-helper.md, run 3). The watch follows the journal from its
	cursor of now, so that no earlier chord wakes it, and wakes the client after CHORD_WATCH even with
	no chord, so that a failed subtest does not leave it stopped for the next."""
	shown = ground.shell(f"journalctl -u {KEYD_UNIT} -n 1 --show-cursor -o cat")
	cursor = next(line for line in shown.splitlines() if line.startswith("-- cursor: "))[11:]
	script = (
		"export PATH=/run/current-system/sw/bin:$PATH; "
		f"timeout {CHORD_WATCH} journalctl -u {KEYD_UNIT} --after-cursor={shlex.quote(cursor)} -f -o cat "
		f"| {{ grep -q 'the emergency chord'; kill -CONT {pid}; touch {CONTINUED}; }}"
	)
	ground.shell(
		f"rm -f {CONTINUED}; systemd-run --collect --unit=ground-continue sh -c {shlex.quote(script)}"
	)


def continued(ground: "Ground", client: "KeydClient", stoppedAt: float) -> float:
	"""When the client stopped at stoppedAt went on, once the watch continueAtChord started has woken
	it: its first record after the stop, of the messages the helper queued meanwhile, which it reads
	as it wakes. The watch's own stamp would come after a program's start, which takes up to a second
	on a loaded host."""
	ground.waitFor(lambda: ground.machine.execute(f"test -e {CONTINUED}")[0] == 0, "the client woken")
	return ground.waitFor(
		lambda: min((r["t"] for r in client.records() if r["t"] > stoppedAt), default=None),
		"the woken client to read",
	)


def assertNotDropped(ground: "Ground", client: "KeydClient", since: "Mark", hung: dict) -> None:
	"""The client stopped for a chord is still connected; if the helper dropped it, the failure says
	why from the helper's own lines and how long the client was stopped, which past HEARTBEAT is the
	test's or the host's doing and not the helper's."""
	if client.call("state")["connected"]:
		return
	said = [f"{t - since.t:+.3f} s: {line}" for t, line in ground.helperLog(since)]
	silent = [line for line in said if "no message for" in line]
	raise AssertionError(
		f"the chord dropped the client; stopped for the chord, seconds: {hung}, against the helper's "
		f"{HEARTBEAT} s for silence; the helper said why: {silent or 'nothing of silence'}\n"
		+ "\n".join(said)
	)


def emergency(ground: "Ground", keyboard: "FakeKeyboard", client: "KeydClient", figures: dict) -> None:
	"""With the client capturing, as hotplug leaves it."""
	with ground.subtest("the chord on a held keyboard lets go while the client is hung"):
		pid = client.pid
		continueAtChord(ground, pid)
		mark = chordAt = ground.mark()
		# Stopped by the keyboard's own server, between keys, so that the client is hung for the chord
		# alone: stopped from a shell before the call, it once lost 1.2 s to the call's start on a
		# loaded host, and the helper dropped it.
		steps = [
			{"signal": [pid, "SIGSTOP"]},
			{"press": "KEY_BACKSPACE"},
			{"sleep": 0.02},
			{"press": "KEY_ESC"},
			{"sleep": 0.02},
			{"press": "KEY_ENTER"},
			{"sleep": 0.1},
			{"release": "KEY_ENTER"},
			{"release": "KEY_ESC"},
			{"release": "KEY_BACKSPACE"},
			{"sleep": 0.05},
			{"tap": "KEY_Q"},
		]
		written = keyboard.run(*steps)
		stoppedAt = next(r["t"] for r in keyboard.records(mark) if r.get("signal") == "SIGSTOP")
		said = ground.waitForHelperLog("the emergency chord", mark)
		enter = next(r for r in written if r["key"] == "KEY_ENTER")
		woken = continued(ground, client, stoppedAt)
		figures["chordClientHung"] = {
			"stoppedToChord": round(said[0] - stoppedAt, 3),
			"stoppedToClientRead": round(woken - stoppedAt, 3),
		}
		ground.machine.log(f"the client hung for the chord, seconds: {figures['chordClientHung']}")
		# Q, typed after the chord, reaches the program from the keyboard itself.
		waitForKeyFrom(ground, "KEY_Q", mark, UHID)
		app = ground.waitForKeys("app", typedKeys("KEY_Q"), mark)
		assert appKeyvals(app) == ["q"], app
		figures["chord"] = {
			"helperSaidAfterEnter": round(said[0] - enter["t"], 3),
			"keyAtProgramAfterEnter": round(app[0]["t"] - enter["t"], 3),
		}
		ground.machine.log(f"the chord, seconds from Enter: {figures['chord']}")
		assert app[0]["t"] - enter["t"] < CHORD_BACK, (app, enter)
		# Backspace and Escape were waiting for the hung client when Enter came: dropped, never typed.
		typed = [r for r in ground.keys("evdev", mark, device=VIRTUAL_KEYBOARD) if r["key"] in CHORD]
		typed += [r for r in ground.keys("app", mark) if r["key"] in CHORD]
		assert not typed, typed
		released = client.waitFor("RELEASED", mark)
		assert released["reason"] == "EMERGENCY", released

	with ground.subtest("nothing is captured again until the client asks"):
		ground.settle(2.5)
		mark = ground.mark()
		keyboard.tap("KEY_W")
		ground.press("e")
		ground.waitForKeys("evdev", typedKeys("KEY_W"), mark, device=UHID)
		ground.waitForKeys("evdev", typedKeys("KEY_E"), mark, device=VIRTIO_KEYBOARD)
		app = ground.waitForKeys("app", typedKeys("KEY_W", "KEY_E"), mark)
		assert appKeyvals(app) == ["w", "e"], app
		assert not client.keys(mark), client.keys(mark)
		assertNotDropped(ground, client, chordAt, figures["chordClientHung"])

	with ground.subtest("the chord on QEMU's own keyboard"):
		mark = ground.mark()
		client.capture(UHID, VIRTIO_KEYBOARD, since=mark)
		pid = client.pid
		continueAtChord(ground, pid)
		hung = ground.mark()
		stoppedAt = float(ground.shell(f"ground-record now; kill -STOP {pid}"))
		ground.press("backspace-esc-ret")
		said = ground.waitForHelperLog("the emergency chord", hung)
		figures["qemuChordClientHung"] = {
			"stoppedToChord": round(said[0] - stoppedAt, 3),
			"stoppedToClientRead": round(continued(ground, client, stoppedAt) - stoppedAt, 3),
		}
		ground.machine.log(f"the client hung for QEMU's chord, seconds: {figures['qemuChordClientHung']}")
		mark = ground.mark()
		ground.press("r")
		waitForKeyFrom(ground, "KEY_R", mark, VIRTIO_KEYBOARD)
		app = ground.waitForKeys("app", typedKeys("KEY_R"), mark)
		assert appKeyvals(app) == ["r"], app
		client.waitFor("RELEASED", hung, lambda r: r["reason"] == "EMERGENCY")
		assertNotDropped(ground, client, hung, figures["qemuChordClientHung"])


# A switch to a text console and back, as keydBackground proves it.


def console(ground: "Ground", keyboard: "FakeKeyboard", client: "KeydClient", figures: dict) -> None:
	mark = ground.mark()
	client.capture(UHID, VIRTIO_KEYBOARD, since=mark)
	desktop = int(ground.shell("fgconsole").strip())
	text = next(n for n in (3, 4, 5) if n != desktop)
	ground.machine.log(f"the session is on console {desktop}; the text console is {text}")
	# What reaches the text console, raw, as a program reading it there would have it.
	ground.shell(
		f"rm -f {CONSOLE_TEXT}; openvt -c {text} -- sh -c 'stty raw -echo; exec cat > {CONSOLE_TEXT}'; "
		f"until [ -e {CONSOLE_TEXT} ]; do sleep 0.05; done"
	)
	switch: dict[str, object] = {}
	figures["console"] = switch

	with ground.subtest("a switch to a text console lets go as the kernel makes it"):
		tracing = traceHelper(ground, ground.helperPid())
		mark = ground.mark()
		steps = json.dumps(
			[
				s
				for c in CONSOLE_LETTERS
				for s in ({"tap": f"KEY_{c.upper()}", "hold": 0.01}, {"sleep": CONSOLE_GAP})
			]
		)
		switched = float(
			ground.shell(
				f"ground-keyboard call {shlex.quote(UHID)} {shlex.quote(steps)} > /tmp/ground/typed.json & "
				f"sleep 0.6; ground-record now; chvt {text}; wait"
			).split()[0]
		)
		written = json.loads(ground.shell("cat /tmp/ground/typed.json"))["written"]
		released = client.waitFor("RELEASED", mark)
		assert released["reason"] == "BACKGROUND", released
		ground.settle(1)
		traced = readTrace(ground, tracing)
		heard = {r["key"] for r in client.keys(mark) if r["value"] == 1}
		reached = ground.shell(f"cat {CONSOLE_TEXT}")
		presses = [r for r in written if r["value"] == 1]
		lines = []
		for r in presses:
			letter = r["key"][4:].lower()
			where = [
				name for name, got in (("client", r["key"] in heard), ("console", letter in reached)) if got
			]
			lines.append(f"{letter} {r['t'] - switched:+.3f} s: {', '.join(where) or 'nowhere'}")
		ground.machine.log("each key, by when it was typed from the switch:\n" + "\n".join(lines))
		kernel, kernelLine = ground.waitForHelperLog(f"console {text} is in front", mark)
		logind = ground.waitForHelperLog("in front on seat0", mark)[0]
		letGo, letGoLine = ground.waitForHelperLog("let go of every keyboard: Background", mark)
		# The helper's own moments, from the tracer: the journal stamps each line when it reads it.
		sawSwitch = sent(traced, kernelLine)
		hadLetGo = sent(traced, letGoLine, sawSwitch)
		spans = ungrabs(traced, sawSwitch, hadLetGo)
		inKernel = sum(end - begun for begun, end in spans)
		own = hadLetGo - sawSwitch - inKernel
		switch.update(
			releasedAfterSwitch=round(released["t"] - switched, 3),
			kernelToldAfterSwitch=round(kernel - switched, 3),
			letGoAfterSwitch=round(letGo - switched, 3),
			logindToldAfterSwitch=round(logind - switched, 3),
			letGoLinesApart=round(letGo - kernel, 4),
			letGoSentAfterSwitchSeen=round(hadLetGo - sawSwitch, 4),
			ungrabsMs=[round((end - begun) * 1000, 2) for begun, end in spans],
			# The helper's own stretches: to the first ungrab, between each two, and to its line.
			ownGapsMs=[
				round((later - earlier) * 1000, 2)
				for earlier, later in zip(
					[sawSwitch, *(end for _begun, end in spans)],
					[*(begun for begun, _end in spans), hadLetGo],
					strict=True,
				)
			],
			letGoOwnMs=round(own * 1000, 2),
		)
		ground.machine.log(f"the switch away, seconds from chvt: {switch}")
		# Let go as the kernel switched, not when logind said so. Judged by the helper's own line, which
		# it writes once every keyboard is let go of: the client reads RELEASED after it, and once, with
		# logind quick, after logind's line too, the helper having let go first. Lines of one stream
		# can share a stamp, so the order is the journal's.
		said = [line for _t, line in ground.helperLog(mark)]
		letGoAt = next(i for i, line in enumerate(said) if "let go of every keyboard: Background" in line)
		logindAt = next(i for i, line in enumerate(said) if "in front on seat0" in line)
		kernelAt = said.index(kernelLine)
		assert kernelAt < letGoAt < logindAt, said
		# What the helper did itself from seeing the switch to having let go is its own few
		# milliseconds. The ungrabs' waits are the kernel's, and on a loaded host they took the two
		# lines past 0.1 s apart (results/VX-guests.txt), so they are taken out, and they can be only
		# because the tracer saw them: the uhid keyboard and QEMU's at the least were held.
		assert len(spans) >= 2, (spans, traced)
		assert own < LET_GO_OWN, switch
		# Every key typed once the helper had let go went to the console, and none to the client.
		after = [r["key"][4:].lower() for r in presses if r["t"] > released["t"]]
		assert after and all(letter in reached for letter in after), (after, reached)
		assert not [r for r in presses if r["key"] in heard and r["t"] > released["t"]]
		# Nor to the program, whose compositor's keyboards logind had paused.
		late = [r for r in ground.keys("app", mark) if r["t"] > released["t"] + SLACK]
		assert not late, late

	# The helper grabs again as soon as the session is in front, and every desktop E15 measured takes no
	# key from the virtual keyboard for some 0.25 to 0.45 s after (E15-gnome.md, E15-sway.md), and
	# longer under load, so a key passed at once is lost; that fault is held by E15 and package HL's
	# work, and here what became of such a key is only written down. The key asserted on is typed
	# RESETTLE after the grab.
	with ground.subtest("the switch back captures again by itself"):
		mark = ground.mark()
		ground.shell(f"chvt {desktop}")
		held = client.waitForDevice(UHID, "HELD", mark)
		client.waitForDevice(VIRTIO_KEYBOARD, "HELD", mark)
		switch["heldAfterReturn"] = round(held["t"] - mark.t, 3)
		assert not [r for r in client.records(mark) if r["dir"] == "out" and r["type"] == "START"]
		typedAt = ground.mark()
		written = keyboard.tap("KEY_B")
		ground.waitForKeys("evdev", typedKeys("KEY_B"), typedAt, device=VIRTUAL_KEYBOARD)
		switch["firstKeyTypedAfterHeld"] = round(written[0]["t"] - held["t"], 3)
		ground.settle(max(0.0, held["t"] + RESETTLE - ground.now()))
		switch["firstKeyAtProgram"] = keyEvents(ground.keys("app", typedAt))
		typedAt = ground.mark()
		written = keyboard.tap("KEY_C")
		# Only C is looked for: B, had it come late, would be no fault of this subtest's.
		app = ground.waitFor(
			lambda: (
				(found := [r for r in ground.keys("app", typedAt) if r["key"] == "KEY_C"])
				and keyEvents(found) == typedKeys("KEY_C")
				and found
			),
			"C at the program",
			5,
		)
		switch["settledKeyTypedAfterHeld"] = round(written[0]["t"] - held["t"], 3)
		switch["settledKeyAtProgramAfterTyped"] = round(app[0]["t"] - written[0]["t"], 3)
		ground.machine.log(f"the switch back: {switch}")
		assert appKeyvals(app) == ["c"], app


# The helper's own watchdog thread, as keydWatchdogThread proves it.


def watchdogThread(ground: "Ground", keyboard: "FakeKeyboard", client: "KeydClient", figures: dict) -> None:
	"""With the client capturing, as console leaves it."""
	ground.shell("systemctl reset-failed linda-keyd")
	pid = ground.waitForHelper()

	with ground.subtest("the watchdog thread lets go of a keyboard whose loop has stopped"):
		mark = ground.mark()
		keyboard.run({"press": "KEY_LEFTSHIFT"})
		ground.waitForKeys("evdev", [("KEY_LEFTSHIFT", 1)], mark, device=VIRTUAL_KEYBOARD)
		ground.shell(f"systemd-run --collect --unit=ground-stall ground-poke stall {pid} 3")
		stopped = ground.waitFor(
			lambda: [
				line
				for line in ground.shell("journalctl -o cat -u ground-stall || true").splitlines()
				if line.startswith("{")
			],
			"the loop to be stopped",
		)
		stoppedAt = json.loads(stopped[0])["stopped"]
		said = ground.waitForHelperLog("watchdog:", mark, timeout=5)
		release = ground.waitForKeys(
			"evdev", [("KEY_LEFTSHIFT", 1), ("KEY_LEFTSHIFT", 0)], mark, device=VIRTUAL_KEYBOARD
		)
		after = ground.mark()
		keyboard.run({"release": "KEY_LEFTSHIFT"}, {"tap": "KEY_Q"})
		back = waitForKeyFrom(ground, "KEY_Q", after, UHID)
		app = ground.waitForKeys("app", typedKeys("KEY_Q"), after)
		logTimeline(ground, client, mark, stoppedAt, "the loop stopped")
		assert appKeyvals(app) == ["q"], app
		assert back[0]["t"] - stoppedAt > STALL, back
		ended = ground.waitForHelperLog("Main process exited", mark)
		systemd = [t for t, line in ground.helperLog(mark) if "Watchdog timeout" in line]
		figures["watchdogThread"] = {
			"saidAfterStall": round(said[0] - stoppedAt, 3),
			"shiftReleasedAfterStall": round(released(release)["received"] - stoppedAt, 3),
			"endedAfterStall": round(ended[0] - stoppedAt, 3),
			"systemdWatchdogAfterStall": round(systemd[0] - stoppedAt, 3) if systemd else None,
		}
		ground.machine.log(f"the watchdog thread, seconds from the stall: {figures['watchdogThread']}")
		# The thread let go first. On a starved host systemd's watchdog may still fire while the helper
		# is ending, which ends it all the same; before the thread, it would mean the thread failed.
		assert not systemd or said[0] < systemd[0], (said, systemd)
		assert systemd or f"status={WATCHDOG_EXIT}" in ended[1], ended

	with ground.subtest("systemd starts it again after the watchdog thread"):
		ground.waitFor(lambda: ground.helperPid() not in (0, pid), "the helper to start again", 10)
		client.waitFor("CLOSED", mark)
		mark = ground.mark()
		client.capture(UHID, since=mark)


# A helper stopped outright, as keydSigstop proves it.


def stopped(ground: "Ground", keyboard: "FakeKeyboard", client: "KeydClient", figures: dict) -> None:
	"""With the client capturing, as watchdogThread leaves it."""
	ground.shell("systemctl reset-failed linda-keyd")
	pid = ground.waitForHelper()
	restarts = ground.helperRestarts()

	with ground.subtest("a stopped helper is killed by systemd's watchdog, and the keyboard comes back"):
		mark = ground.mark()
		keyboard.run({"press": "KEY_LEFTSHIFT"})
		ground.waitForKeys("evdev", [("KEY_LEFTSHIFT", 1)], mark, device=VIRTUAL_KEYBOARD)
		tracing = traceKill(ground, pid)
		steps = json.dumps([{"tap": "KEY_Q", "hold": 0.01}, {"sleep": STREAM_GAP - 0.01}] * STREAM_TAPS)
		# Stopped first and typed on at once, from the same shell, so that every tap comes after the
		# stop and the first one the keyboard gives a reader again says when the kernel let go.
		output = ground.shell(
			f"ground-record now; kill -STOP {pid}; "
			f"ground-keyboard call {shlex.quote(UHID)} {shlex.quote(steps)}"
		)
		stoppedAt = float(output.split("\n", 1)[0])
		written = json.loads(output.split("\n", 1)[1])["written"]
		timedOut = ground.waitForHelperLog("Watchdog timeout", mark)[0]
		ground.waitForHelperLog("with signal SIGKILL", mark)
		ground.waitForHelperLog("status=9/KILL", mark)
		kills = [r["t"] for r in readTrace(ground, tracing) if r["call"] == "kill"]
		assert kills, "the tracer saw no SIGKILL sent to the helper"
		killed = kills[0]
		release = ground.waitForKeys(
			"evdev", [("KEY_LEFTSHIFT", 1), ("KEY_LEFTSHIFT", 0)], mark, device=VIRTUAL_KEYBOARD
		)
		fromKeyboard = [r for r in ground.keys("evdev", mark, device=UHID) if r["key"] == "KEY_Q"]
		assert fromKeyboard, "no key reached a reader from the keyboard itself after the stop"
		back = fromKeyboard[0]["t"]
		app = [r for r in ground.keys("app", mark) if r["key"] == "KEY_Q"]
		logTimeline(ground, client, mark, stoppedAt, "the helper stopped")
		for t, line in ground.helperLog(mark):
			ground.machine.log(f"{t - stoppedAt:+.3f} s: {line}")
		# Shift was down on the virtual keyboard when the kernel destroyed it, and GNOME may apply it to
		# a key from the real keyboard that comes just after; it must not stay applied.
		shifted = [r for r in app if r["value"] == 1 and r["keyval"] == "Q"]
		assert app and set(appKeyvals(app)) <= {"q", "Q"}, app
		assert not [r for r in shifted if r["t"] > released(release)["received"] + SHIFT_CLEARS], shifted
		typedAfterBack = [r for r in written if r["value"] == 1 and r["t"] >= back - STREAM_GAP]
		figures["stopped"] = {
			"watchdogLineAfterStop": round(timedOut - stoppedAt, 3),
			"killAfterStop": round(killed - stoppedAt, 3),
			"shiftReleasedAfterKill": round(released(release)["received"] - killed, 3),
			"keyboardBackAfterKill": round(back - killed, 3),
			"shiftReleasedAfterStop": round(released(release)["received"] - stoppedAt, 3),
			"keyboardBackAfterStop": round(back - stoppedAt, 3),
			"firstKeyAtProgramAfterStop": round(app[0]["t"] - stoppedAt, 3),
			"tapsTyped": len([r for r in written if r["value"] == 1]),
			"tapsTypedFromBack": len(typedAfterBack),
			"tapsAtProgram": len(appKeyvals(app)),
			"shiftedAtProgram": len(shifted),
			"lastShiftedAfterShiftReleaseRead": (
				round(shifted[-1]["t"] - released(release)["received"], 3) if shifted else None
			),
		}
		ground.machine.log(f"a stopped helper, seconds from the stop: {figures['stopped']}")
		# Keys typed while the helper was stopped were in its hands, and died with it: none is typed
		# late.
		assert len(appKeyvals(app)) <= len(typedAfterBack), (figures["stopped"], app)
		# systemd's watchdog, as the helper's unit sets it, counts its two seconds from the loop's last
		# feed, before the stop, and the kill is timed by the kernel as systemd sends it; SLACK is
		# systemd's own lateness in sending it.
		assert killed - stoppedAt < WATCHDOG + SLACK, figures["stopped"]
		# Then the kernel lets go as the killed helper's devices close, which is all the helper's end
		# may take. The release, which the kernel stamps with the virtual keyboard's last repeat,
		# counts from when the recorder read it, so later rather than earlier; the key from the
		# keyboard by its own stamp, so a tap's gap later at most. Each is measured from the kill, so
		# that a host slow to run systemd at its limit, as on GNOME 51 with four guests running, counts
		# against systemd's bound alone, and not twice.
		assert released(release)["received"] - killed < AFTER_KILL, figures["stopped"]
		assert back - killed < AFTER_KILL + STREAM_GAP, figures["stopped"]

	with ground.subtest("systemd starts it again, and it serves a client"):
		ground.waitFor(lambda: ground.helperPid() not in (0, pid), "the helper to start again", 10)
		assert ground.helperRestarts() == restarts + 1, (restarts, ground.helperRestarts())
		client.waitFor("CLOSED", mark)
		mark = ground.mark()
		keyboard.run({"release": "KEY_LEFTSHIFT"})
		client.capture(UHID, since=mark)
		mark = ground.mark()
		keyboard.tap("KEY_W")
		ground.waitForKeys("evdev", typedKeys("KEY_W"), mark, device=VIRTUAL_KEYBOARD)
		app = ground.waitForKeys("app", typedKeys("KEY_W"), mark)
		assert appKeyvals(app) == ["w"], app


def run(ground: "Ground") -> dict[str, object]:
	"""Every subtest, in one boot, with no Linda; answers the figures measured, by subtest."""
	figures: dict[str, object] = {}
	ground.openWindow()
	ground.activate(WINDOW_TITLE)
	ground.waitForHelper()
	keyboard = ground.uhidKeyboard(UHID)
	client = ground.keydClient("main")
	grab(ground, keyboard, client, figures)
	latency(ground, keyboard, client, figures)
	locks(ground, keyboard, client, figures)
	hotplug(ground, client, figures)
	emergency(ground, keyboard, client, figures)
	console(ground, keyboard, client, figures)
	watchdogThread(ground, keyboard, client, figures)
	stopped(ground, keyboard, client, figures)
	return figures


# The guest half.


def _node(name: str) -> str | None:
	for entry in sorted(os.listdir("/sys/class/input")):
		if not entry.startswith("event"):
			continue
		try:
			with open(f"/sys/class/input/{entry}/device/name") as f:
				if f.read().strip() == name:
					return f"/dev/input/{entry}"
		except OSError:
			continue
	return None


def _processes(uid: int) -> list[int]:
	found = []
	for entry in os.listdir("/proc"):
		if entry.isdigit():
			try:
				if os.stat(f"/proc/{entry}").st_uid == uid:
					found.append(int(entry))
			except OSError:
				continue
	return found


def _holding(pid: int, prefix: str) -> bool:
	try:
		fds = os.listdir(f"/proc/{pid}/fd")
	except OSError:
		return False
	for fd in fds:
		try:
			if os.readlink(f"/proc/{pid}/fd/{fd}").startswith(prefix):
				return True
		except OSError:
			continue
	return False


def opened(name: str, uid: int, seconds: float, out: str) -> None:
	def write(kind: str, **fields: object) -> None:
		with open(out, "a") as f:
			f.write(json.dumps({"kind": kind, **fields}) + "\n")

	# The compositor is the process of the user's that holds keyboards open already; only it is
	# looked at closely, so that each look is quick.
	everyone = _processes(uid)
	readers = [pid for pid in everyone if _holding(pid, "/dev/input/event")] or everyone
	write("watching", t=time.monotonic(), readers=readers)
	deadline = time.monotonic() + seconds
	node = appeared = at = holder = None
	while time.monotonic() < deadline:
		now = time.monotonic()
		if node is None:
			node = _node(name)
			appeared = now if node else None
		if node is not None:
			holder = next((pid for pid in readers if _holding(pid, node)), None)
			if holder is not None:
				at = now
				break
		time.sleep(WATCH_INTERVAL)
	comm = None
	if holder is not None:
		with open(f"/proc/{holder}/comm") as f:
			comm = f.read().strip()
	write("result", node=node, appeared=appeared, opened=at, pid=holder, comm=comm)


if __name__ == "__main__":
	if len(sys.argv) == 6 and sys.argv[1] == "opened":
		opened(sys.argv[2], int(sys.argv[3]), float(sys.argv[4]), sys.argv[5])
	else:
		sys.exit("usage: helperSafety.py opened NAME UID SECONDS OUT")
