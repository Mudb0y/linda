"""A typed key held down when the helper can no longer act: it stops repeating within the bound of the
watchdog that acts. With the helper stopped by SIGSTOP, its own watchdog thread stopped with it,
systemd's watchdog kills it within two seconds of its last sign of life, and the kernel, destroying
the virtual keyboard, releases the key. With the helper's loop alone stopped, holding no keyboard at
all since another program holds every one, its watchdog thread lets go of the typed key itself
within a little over a second, the key down on the virtual keyboard being reason enough. After each,
systemd starts the helper again, and typing works.
"""

import json
import re
from typing import TYPE_CHECKING

from ground import AT_KEYBOARD, VIRTIO_KEYBOARD, VIRTUAL_KEYBOARD

if TYPE_CHECKING:
	from ground import Ground, KeydClient, Mark

UHID = "Ground uhid keyboard"
#: FEATURE_TYPING in protocol.rs.
FEATURE_TYPING = 1
#: WatchdogSec in linda-keyd.service.in, which counts from the loop's last feed, before any stop.
WATCHDOG = 2.0
#: STALL_LIMIT in protocol.rs.
STALL = 1.0
#: What the journal's stamps and a loaded host add to a bound the helper keeps, as keydSigstop allows.
SLACK = 0.3
#: The exit status of a helper its watchdog thread ends, WATCHDOG_EXIT in main.rs.
WATCHDOG_EXIT = 70
#: Repeats the window must have had of the held key before the helper is stopped.
REPEATS = 3


def typeKey(client: "KeydClient", tag: int, key: str, down: bool) -> str:
	"""TYPE INJECT, and its outcome."""
	mark = client.ground.mark()
	client.call("type", tag=tag, key=key, down=down, mode="inject")
	return client.waitFor("TYPED", mark, lambda r: r["tag"] == tag)["outcome"]


def appPresses(ground: "Ground", since: "Mark", key: str) -> list[dict]:
	return [r for r in ground.keys("app", since) if r["key"] == key and r["value"] == 1]


def holdTyped(ground: "Ground", client: "KeydClient", tag: int) -> "Mark":
	"""Type A down and leave it, until the window has its repeats."""
	mark = ground.mark()
	assert typeKey(client, tag, "KEY_A", True) == "OUT"
	ground.waitFor(lambda: len(appPresses(ground, mark, "KEY_A")) >= REPEATS, "A to repeat in the window")
	return mark


def judge(ground: "Ground", since: "Mark", stopped: float, bound: float, what: str) -> None:
	"""The typed A comes up on the virtual keyboard within bound of stopped, and the window has no
	repeat of it after."""
	release = ground.waitFor(
		lambda: next(
			(
				r
				for r in ground.keys("x", since, device=VIRTUAL_KEYBOARD)
				if (r["key"], r["value"]) == ("KEY_A", 0)
			),
			None,
		),
		"A's release on the virtual keyboard",
		10,
	)
	ground.settle(0.5)
	presses = appPresses(ground, since, "KEY_A")
	during = [r for r in presses if r["t"] > stopped]
	late = [r for r in presses if r["t"] > release["t"] + 0.1]
	released = [r for r in ground.keys("app", since) if (r["key"], r["value"]) == ("KEY_A", 0)]
	ground.machine.log(
		f"{what}: A repeated {len(during)} times in the window after the stop, the last "
		f"{(during[-1]['t'] - stopped) if during else 0:+.3f} s from it; it came up on the virtual "
		f"keyboard {release['t'] - stopped:+.3f} s from it"
	)
	assert during, "A did not go on repeating while the helper was stopped"
	assert release["t"] - stopped < bound, (release, stopped)
	assert not late, late
	assert released, "the window never had A's release"


def run(ground: "Ground") -> None:
	ground.openWindow()
	pid = ground.waitForHelper()
	keyboard = ground.uhidKeyboard(UHID)
	client = ground.keydClient("main")
	assert client.connect(features=FEATURE_TYPING)["welcome"]["features"] == FEATURE_TYPING
	client.capture(UHID)

	with ground.subtest("a typed key held by a helper stopped outright stops within systemd's watchdog"):
		mark = holdTyped(ground, client, 1)
		ground.shell(f"kill -STOP {pid}")
		stopped = ground.now()
		judge(ground, mark, stopped, WATCHDOG + SLACK, "stopped by SIGSTOP")
		ground.waitForHelperLog("Watchdog timeout", mark)
		ground.waitForHelperLog("status=9/KILL", mark)

	with ground.subtest("systemd starts it again, and it types"):
		pid = ground.waitFor(
			lambda: (p := ground.helperPid()) not in (0, pid) and p, "the helper to start again", 10
		)
		client.waitFor("CLOSED", mark)
		mark = ground.mark()
		assert client.connect(features=FEATURE_TYPING)["welcome"]["features"] == FEATURE_TYPING
		client.capture(UHID, since=mark)
		mark = ground.mark()
		assert typeKey(client, 2, "KEY_B", True) == "OUT"
		assert typeKey(client, 3, "KEY_B", False) == "OUT"
		# Typed by two test calls, B is held long enough to repeat.
		ground.waitFor(
			lambda: [r for r in ground.keys("app", mark) if (r["key"], r["value"]) == ("KEY_B", 0)],
			"B's release in the window",
		)
		app = ground.keys("app", mark)
		assert app[0]["keyval"] == "b", app

	with ground.subtest("with no keyboard held, the watchdog thread lets go of a typed key held"):
		# The starts counted against the unit's limit are forgotten, as its comment allows.
		ground.shell("systemctl reset-failed linda-keyd")
		mark = ground.mark()
		client.stop()
		client.waitFor("RELEASED", mark, lambda r: r["reason"] == "STOPPED")
		present = ground.shell("cat /sys/class/input/*/name").splitlines()
		others = [name for name in (UHID, VIRTIO_KEYBOARD, AT_KEYBOARD) if name in present]
		for n, name in enumerate(others):
			unit = f"ground-grab-{n}"
			ground.shell(f"systemd-run --collect --unit={unit} ground-poke grab '{name}'")
			ground.waitFor(
				lambda unit=unit: "grabbed" in ground.shell(f"journalctl -o cat -u {unit} || true"),
				f"the other grab of {name}",
			)
		mark = ground.mark()
		client.start()
		for name in others:
			client.waitForDevice(name, "BUSY", mark)
		ground.settle(0.5)
		held = [r for r in client.records(mark, "DEVICE") if r["status"] == "HELD"]
		assert not held, held
		mark = holdTyped(ground, client, 4)
		ground.shell(f"systemd-run --collect --unit=ground-stall ground-poke stall {pid} 3")
		line = ground.waitFor(
			lambda: [
				line
				for line in ground.shell("journalctl -o cat -u ground-stall || true").splitlines()
				if line.startswith("{")
			],
			"the loop to be stopped",
		)[0]
		stopped = json.loads(line)["stopped"]
		said = ground.waitForHelperLog("watchdog:", mark, timeout=5)
		ground.machine.log(f"{said[0] - stopped:.3f} s after the loop stopped: {said[1]}")
		judge(ground, mark, stopped, WATCHDOG, "the loop alone stopped")
		ended = ground.waitForHelperLog("Main process exited", mark)
		systemd = [t for t, text in ground.helperLog(mark) if "Watchdog timeout" in text]
		ground.machine.log(
			f"{ended[1]}, {ended[0] - stopped:.3f} s after the loop stopped; systemd's watchdog "
			+ (f"fired {systemd[0] - stopped:.3f} s after it" if systemd else "never fired")
		)
		# The loop's last turn before the stop may come a tick before it, so the stall is measured by
		# what the thread says.
		stall = re.search(r"has not turned for (\d+) ms", said[1])
		assert stall and int(stall.group(1)) / 1000 >= STALL, said
		assert not systemd or said[0] < systemd[0], (said, systemd)
		assert systemd or f"status={WATCHDOG_EXIT}" in ended[1], ended
		for n in range(len(others)):
			ground.shell(f"systemctl stop ground-grab-{n}")

	with ground.subtest("systemd starts it again"):
		ground.waitFor(lambda: ground.helperPid() not in (0, pid), "the helper to start again", 10)
		client.waitFor("CLOSED", mark)
		mark = ground.mark()
		assert client.connect(features=FEATURE_TYPING)["welcome"]["features"] == FEATURE_TYPING
		client.capture(UHID, since=mark)
		mark = ground.mark()
		keyboard.tap("KEY_W")
		app = ground.waitForKeys("app", [("KEY_W", 1), ("KEY_W", 0)], mark)
		assert app[0]["keyval"] == "w", app
