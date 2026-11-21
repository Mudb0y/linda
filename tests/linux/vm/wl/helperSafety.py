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
goes, the virtual keyboard's at a helper's end among them, carries the stamp of that device's last
event, which was seen to be the press it releases, so such a release is timed by when the recorder
read it.

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
import shlex
import statistics
import sys
import time
from typing import TYPE_CHECKING

if __name__ != "__main__":
	from ground import (
		AT_KEYBOARD,
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
#: The emergency chord's promise: keys reach the program from the keyboard itself within this.
CHORD_BACK = 1.0
OPENED = "/tmp/ground/virtualOpened.jsonl"
CONSOLE_TEXT = "/tmp/ground/console.txt"
CONSOLE_LETTERS = "abcdefghijklmnopqrstuvwxyz"
#: Seconds between the letters typed across a switch. Under GNOME on a loaded host the kernel put the
#: console in front a second after chvt, where the X server lets it go at once, so the letters run
#: some five seconds, to go on well past it.
CONSOLE_GAP = 0.2
#: Taps typed while the helper is stopped, one every STREAM_GAP seconds: past systemd's watchdog.
STREAM_TAPS = 80
STREAM_GAP = 0.05
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


def locks(ground: "Ground", keyboard: "FakeKeyboard", client: "KeydClient", figures: dict) -> None:
	seen: dict[str, object] = {}
	figures["locks"] = seen

	def allLights() -> dict[str, int]:
		return {name: lights(ground, name) for name in (VIRTIO_KEYBOARD, AT_KEYBOARD, UHID)}

	with ground.subtest("Num Lock on in the desktop before a new helper makes its virtual keyboard"):
		restartHelper(ground, client, "systemctl restart linda-keyd")
		before = lights(ground, VIRTIO_KEYBOARD)
		mark = ground.mark()
		ground.press("num_lock")
		ground.waitForKeys("evdev", [("KEY_NUMLOCK", 1), ("KEY_NUMLOCK", 0)], mark, device=VIRTIO_KEYBOARD)
		ground.waitFor(
			lambda: lights(ground, VIRTIO_KEYBOARD) == before ^ NUM, "the virtio keyboard's Num Lock light"
		)
		seen["numLockBefore"] = {"before": before, **allLights(), "uhidReports": uhidLights(keyboard)}
		ground.machine.log(f"Num Lock pressed with no virtual keyboard: lights {seen['numLockBefore']}")
		assert before == 0, f"the desktop started with Num Lock on: {before}"

	with ground.subtest("the locks at the start of capture"):
		mark = ground.mark()
		client.capture(UHID, VIRTIO_KEYBOARD, AT_KEYBOARD, since=mark)
		ground.settle(1)
		told = [r["leds"] for r in client.records(mark, "LOCKS")]
		seen["atStart"] = {"told": told, "virtual": lights(ground, VIRTUAL_KEYBOARD), **allLights()}
		ground.machine.log(f"at the start of capture: {seen['atStart']}")
		assert told and told[-1] == NUM, told
		mark = ground.mark()
		ground.press("kp_1")
		app = ground.waitForKeys("app", typedKeys("KEY_KP1"), mark)
		assert appKeyvals(app) == ["KP_1"], app

	with ground.subtest("Caps Lock through the helper is locked, lit everywhere and told"):
		mark = ground.mark()
		keyboard.tap("KEY_CAPSLOCK")
		ground.waitForKeys("evdev", typedKeys("KEY_CAPSLOCK"), mark, device=VIRTUAL_KEYBOARD)
		told = client.waitFor("LOCKS", mark, lambda r: r["leds"] == NUM | CAPS)
		ground.waitFor(
			lambda: lights(ground, VIRTUAL_KEYBOARD) == NUM | CAPS, "the virtual keyboard's lights"
		)
		for name in (UHID, VIRTIO_KEYBOARD, AT_KEYBOARD):
			ground.waitFor(lambda name=name: lights(ground, name) == NUM | CAPS, f"{name}'s lights")
		ground.waitFor(lambda: NUM | CAPS in uhidLights(keyboard), "the uhid keyboard to be sent its lights")
		seen["capsToldAfter"] = round(told["t"] - mark.t, 3)
		ground.machine.log(f"Caps Lock told {seen['capsToldAfter']} s after the mark")
		mark = ground.mark()
		keyboard.tap("KEY_A")
		app = ground.waitForKeys("app", typedKeys("KEY_A"), mark)
		assert appKeyvals(app) == ["A"], app

	with ground.subtest("letting go leaves the keyboards with the console's lights"):
		mark = ground.mark()
		client.stop()
		client.waitFor("RELEASED", mark)
		ground.settle(0.5)
		after = allLights()
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
		seen["newHelper"] = {"told": told, "virtual": lights(ground, VIRTUAL_KEYBOARD), **allLights()}
		ground.machine.log(f"a new helper: {seen['newHelper']}")
		assert told and told[-1] == NUM | CAPS, told
		for name in (UHID, VIRTIO_KEYBOARD, AT_KEYBOARD):
			assert lights(ground, name) == NUM | CAPS, name

	with ground.subtest("let go again, the desktop lights the keyboards itself at the next change"):
		mark = ground.mark()
		client.stop()
		client.waitFor("RELEASED", mark)
		mark = ground.mark()
		keyboard.tap("KEY_CAPSLOCK")
		ground.waitForKeys("evdev", typedKeys("KEY_CAPSLOCK"), mark, device=UHID)
		ground.waitFor(
			lambda: lights(ground, UHID) == NUM, "the desktop to light the uhid keyboard itself again"
		)
		mark = ground.mark()
		keyboard.tap("KEY_A")
		app = ground.waitForKeys("app", typedKeys("KEY_A"), mark)
		assert appKeyvals(app) == ["a"], app


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
			"shiftHeld": round(release[1]["received"] - pressed[0]["received"], 3),
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


def emergency(ground: "Ground", keyboard: "FakeKeyboard", client: "KeydClient", figures: dict) -> None:
	"""With the client capturing, as hotplug leaves it."""
	with ground.subtest("the chord on a held keyboard lets go while the client is hung"):
		ground.session({"op": "signal", "name": "keyd-main", "signal": "SIGSTOP"})
		mark = ground.mark()
		written = keyboard.run(
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
		)
		said = ground.waitForHelperLog("the emergency chord", mark)
		enter = next(r for r in written if r["key"] == "KEY_ENTER")
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
		ground.session({"op": "signal", "name": "keyd-main", "signal": "SIGCONT"})
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
		assert client.call("state")["connected"], "the chord dropped the client"

	with ground.subtest("the chord on QEMU's own keyboard"):
		mark = ground.mark()
		client.capture(UHID, VIRTIO_KEYBOARD, since=mark)
		ground.session({"op": "signal", "name": "keyd-main", "signal": "SIGSTOP"})
		mark = ground.mark()
		ground.press("backspace-esc-ret")
		ground.waitForHelperLog("the emergency chord", mark)
		mark = ground.mark()
		ground.press("r")
		waitForKeyFrom(ground, "KEY_R", mark, VIRTIO_KEYBOARD)
		app = ground.waitForKeys("app", typedKeys("KEY_R"), mark)
		assert appKeyvals(app) == ["r"], app
		ground.session({"op": "signal", "name": "keyd-main", "signal": "SIGCONT"})
		client.waitFor("RELEASED", mark, lambda r: r["reason"] == "EMERGENCY")


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
		kernel = ground.waitForHelperLog(f"console {text} is in front", mark)[0]
		logind = ground.waitForHelperLog("in front on seat0", mark)[0]
		switch.update(
			releasedAfterSwitch=round(released["t"] - switched, 3),
			kernelToldAfterSwitch=round(kernel - switched, 3),
			logindToldAfterSwitch=round(logind - switched, 3),
		)
		ground.machine.log(f"the switch away, seconds from chvt: {switch}")
		# Let go as the kernel switched, not when logind said so.
		assert released["t"] < logind, (released, logind)
		assert released["t"] - kernel < 0.1, (released, kernel)
		# Every key typed once the helper had let go went to the console, and none to the client.
		after = [r["key"][4:].lower() for r in presses if r["t"] > released["t"]]
		assert after and all(letter in reached for letter in after), (after, reached)
		assert not [r for r in presses if r["key"] in heard and r["t"] > released["t"]]
		# Nor to the program, whose compositor's keyboards logind had paused.
		late = [r for r in ground.keys("app", mark) if r["t"] > released["t"] + SLACK]
		assert not late, late

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
		app = ground.waitForKeys("app", typedKeys("KEY_B"), typedAt)
		switch["firstKeyTypedAfterHeld"] = round(written[0]["t"] - held["t"], 3)
		switch["firstKeyAtProgramAfterTyped"] = round(app[0]["t"] - written[0]["t"], 3)
		ground.machine.log(f"the switch back: {switch}")
		assert appKeyvals(app) == ["b"], app


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
		assert appKeyvals(app) == ["q"], app
		assert back[0]["t"] - stoppedAt > STALL, back
		ended = ground.waitForHelperLog("Main process exited", mark)
		systemd = [t for t, line in ground.helperLog(mark) if "Watchdog timeout" in line]
		figures["watchdogThread"] = {
			"saidAfterStall": round(said[0] - stoppedAt, 3),
			"shiftReleasedAfterStall": round(release[1]["received"] - stoppedAt, 3),
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
		steps = json.dumps([{"tap": "KEY_Q", "hold": 0.01}, {"sleep": STREAM_GAP - 0.01}] * STREAM_TAPS)
		# Stopped first and typed on at once, from the same shell, so that every tap comes after the
		# stop and the first one the keyboard gives a reader again says when the kernel let go.
		output = ground.shell(
			f"ground-record now; kill -STOP {pid}; "
			f"ground-keyboard call {shlex.quote(UHID)} {shlex.quote(steps)}"
		)
		stoppedAt = float(output.split("\n", 1)[0])
		written = json.loads(output.split("\n", 1)[1])["written"]
		ground.waitForHelperLog("Watchdog timeout", mark)
		ground.waitForHelperLog("with signal SIGKILL", mark)
		ground.waitForHelperLog("status=9/KILL", mark)
		release = ground.waitForKeys(
			"evdev", [("KEY_LEFTSHIFT", 1), ("KEY_LEFTSHIFT", 0)], mark, device=VIRTUAL_KEYBOARD
		)
		fromKeyboard = [r for r in ground.keys("evdev", mark, device=UHID) if r["key"] == "KEY_Q"]
		assert fromKeyboard, "no key reached a reader from the keyboard itself after the stop"
		back = fromKeyboard[0]["t"]
		app = [r for r in ground.keys("app", mark) if r["key"] == "KEY_Q"]
		assert app and set(appKeyvals(app)) == {"q"}, app
		for t, line in ground.helperLog(mark):
			ground.machine.log(f"{t - stoppedAt:+.3f} s: {line}")
		typedAfterBack = [r for r in written if r["value"] == 1 and r["t"] >= back - STREAM_GAP]
		figures["stopped"] = {
			"shiftReleasedAfterStop": round(release[1]["received"] - stoppedAt, 3),
			"keyboardBackAfterStop": round(back - stoppedAt, 3),
			"firstKeyAtProgramAfterStop": round(app[0]["t"] - stoppedAt, 3),
			"tapsTyped": len([r for r in written if r["value"] == 1]),
			"tapsTypedFromBack": len(typedAfterBack),
			"tapsAtProgram": len(appKeyvals(app)),
		}
		ground.machine.log(f"a stopped helper, seconds from the stop: {figures['stopped']}")
		# Keys typed while the helper was stopped were in its hands, and died with it: none is typed
		# late.
		assert len(appKeyvals(app)) <= len(typedAfterBack), (figures["stopped"], app)
		# The watchdog's two seconds count from the loop's last feed, before the stop.
		assert release[1]["received"] - stoppedAt < WATCHDOG + SLACK, release
		assert back - stoppedAt < WATCHDOG + SLACK, back - stoppedAt

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
