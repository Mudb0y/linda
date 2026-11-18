"""The moment of the grab, made exact by following the helper's loop through ptrace (guest/poke.py
race): a key pressed after the helper's look found every key up but before its grab took hold.

Held across the grab, the helper's second look finds it down, and it lets go again and waits, so the
key's release reaches the desktop as its press did. Released in the instant between the grab and that
second look, which the helper accepts as too narrow to guard, the press went to the desktop and the
release to the helper alone, which drops a release whose press it never saw: the desktop is left with
the key down. What that does, and what ends it, goes to the log.
"""

import json
import shlex
from typing import TYPE_CHECKING

from ground import FakeKeyboard, unitName

if TYPE_CHECKING:
	from ground import Ground, KeydClient


def race(ground: "Ground", client: "KeydClient", mode: str, name: str) -> tuple[FakeKeyboard, object]:
	"""START with the tracer waiting for the grab of a new keyboard; answers the keyboard and the mark."""
	unit = unitName("ground-race-", name)
	ground.shell(
		f"systemd-run --collect --unit={unit} ground-poke race {mode} {ground.helperPid()} {shlex.quote(name)} KEY_A"
	)

	def steps() -> list[dict]:
		lines = ground.shell(f"journalctl -o cat -u {unit} || true").splitlines()
		return [json.loads(line) for line in lines if line.startswith("{")]

	ground.waitFor(lambda: any(s["step"] == "attached" for s in steps()), "the tracer to attach", 20)
	ground.waitForDevice("x", name)
	mark = ground.mark()
	client.start()
	ground.waitFor(lambda: any(s["step"] == "detached" for s in steps()), "the race to be run", 20)
	ground.machine.log(f"{mode}: {[(round(s['t'] - mark.t, 4), s['step']) for s in steps()]}")
	return FakeKeyboard(ground, name, "uhid", started=True), mark


def presses(records: list[dict], key: str) -> int:
	return len([r for r in records if r["key"] == key and r["value"] == 1])


def run(ground: "Ground") -> None:
	ground.openWindow()
	ground.waitForHelper()
	client = ground.keydClient("main")
	# The virtual keyboard is made once, beforehand, so that START grabs at once.
	client.capture()
	ground.waitForHelperLog("capturing")
	mark = ground.mark()
	client.stop()
	client.waitFor("RELEASED", mark)

	with ground.subtest(
		"a key that goes down as the grab is taken and stays down: the helper lets go and waits"
	):
		name = "Ground race keyboard"
		keyboard, mark = race(ground, client, "back", name)
		client.waitForDevice(name, "WAITING", mark)
		ground.waitForKeys("x", [("KEY_A", 1)], mark, device=name)
		ground.settle(0.2)
		assert not [r for r in client.records(mark, "DEVICE") if r["name"] == name and r["status"] == "HELD"]
		release = ground.mark()
		keyboard.run({"release": "KEY_A"})
		ground.waitForKeys("x", [("KEY_A", 0)], release, device=name)
		client.waitForDevice(name, "HELD", release)
		assert not [r for r in client.keys(mark) if r["key"] == "KEY_A"]
		mark = ground.mark()
		client.stop()
		client.waitFor("RELEASED", mark)

	with ground.subtest("a key released between the grab and the second look: the race the helper accepts"):
		name = "Ground second race keyboard"
		keyboard, mark = race(ground, client, "across", name)
		client.waitForDevice(name, "HELD", mark)
		ground.waitForKeys("x", [("KEY_A", 1)], mark, device=name)
		ground.settle(1.5)
		x = [r for r in ground.keys("x", mark) if r["key"] == "KEY_A"]
		app = [r for r in ground.keys("app", mark) if r["key"] == "KEY_A"]
		ground.machine.log(
			f"the X server had {[(r['device'], r['value']) for r in x]} of A; the program had {presses(app, 'KEY_A')} "
			f"presses and {len(app) - presses(app, 'KEY_A')} releases of it in 1.5 s; the client had "
			f"{[(r['key'], r['value']) for r in client.keys(mark)]}"
		)
		# The press reached the desktop and its release did not: A is down there, repeating.
		assert [r["value"] for r in x] == [1], x
		assert presses(app, "KEY_A") > 1 and all(r["value"] == 1 for r in app), app
		tap = ground.mark()
		keyboard.tap("KEY_A")
		ground.settle(1.5)
		after = ground.mark()
		ground.settle(1)
		x = [(r["device"], r["value"]) for r in ground.keys("x", tap) if r["key"] == "KEY_A"]
		repeats = presses(ground.keys("app", after), "KEY_A")
		ground.machine.log(
			f"A typed again through the helper: the X server had {x}; the program had {repeats} presses of A in the second after"
		)
		assert repeats == 0, "typing the key again does not end it"
