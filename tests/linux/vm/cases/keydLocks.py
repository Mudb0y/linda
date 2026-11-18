"""The lock lights and lock state: with Num Lock on in the X server before the helper starts, what the
helper tells the client at the start, what the X server sets on the new virtual keyboard, and what the
real keyboards show; then Caps Lock pressed through the helper, which the X server locks and lights on
the virtual keyboard, and which the helper copies to every keyboard it holds and tells the client.

Each light is a bit of its LED code: Num Lock 1, Caps Lock 2.
"""

import json
from typing import TYPE_CHECKING

from ground import AT_KEYBOARD, VIRTIO_KEYBOARD, VIRTUAL_KEYBOARD

if TYPE_CHECKING:
	from ground import Ground

UHID = "Ground uhid keyboard"
NUM, CAPS = 1, 2


def lights(ground: "Ground", name: str) -> int:
	return next(iter(json.loads(ground.shell(f"ground-poke leds '{name}'")).values()))


def xLocks(ground: "Ground") -> str:
	output = ground.run(["xset", "q"]).stdout
	return next(line.strip() for line in output.splitlines() if "Caps Lock" in line)


def uhidLights(keyboard) -> list[int]:
	return [r["leds"] for r in keyboard.records() if "leds" in r]


def run(ground: "Ground") -> None:
	ground.openWindow()
	ground.waitForHelper()
	keyboard = ground.uhidKeyboard(UHID)
	client = ground.keydClient("main")

	with ground.subtest("Num Lock on in the X server before the helper starts"):
		mark = ground.mark()
		ground.press("num_lock")
		ground.waitForKeys("x", [("KEY_NUMLOCK", 1), ("KEY_NUMLOCK", 0)], mark)
		ground.waitFor(lambda: lights(ground, VIRTIO_KEYBOARD) == NUM, "the virtio keyboard's Num Lock light")
		ground.machine.log(
			f"before: X {xLocks(ground)!r}; lights: virtio {lights(ground, VIRTIO_KEYBOARD)}, "
			f"AT {lights(ground, AT_KEYBOARD)}, uhid {lights(ground, UHID)}, uhid's reports {uhidLights(keyboard)}"
		)

	with ground.subtest("the locks at the start of capture"):
		mark = ground.mark()
		client.capture(UHID, VIRTIO_KEYBOARD, AT_KEYBOARD, since=mark)
		ground.settle(1)
		locks = [r["leds"] for r in client.records(mark, "LOCKS")]
		virtual = lights(ground, VIRTUAL_KEYBOARD)
		ground.machine.log(
			f"at the start: the client was told {locks}; the virtual keyboard's lights {virtual}; "
			f"X {xLocks(ground)!r}; lights: virtio {lights(ground, VIRTIO_KEYBOARD)}, "
			f"AT {lights(ground, AT_KEYBOARD)}, uhid {lights(ground, UHID)}"
		)
		assert locks and locks[-1] == NUM, locks
		mark = ground.mark()
		ground.press("kp_1")
		app = ground.waitForKeys("app", [("KEY_KP1", 1), ("KEY_KP1", 0)], mark)
		ground.machine.log(f"the numpad's 1 through the virtual keyboard is {app[0]['keyval']}")
		assert app[0]["keyval"] == "KP_1", app

	with ground.subtest("Caps Lock through the helper is locked, lit everywhere and told"):
		mark = ground.mark()
		keyboard.tap("KEY_CAPSLOCK")
		ground.waitForKeys("x", [("KEY_CAPSLOCK", 1), ("KEY_CAPSLOCK", 0)], mark, device=VIRTUAL_KEYBOARD)
		told = client.waitFor("LOCKS", mark, lambda r: r["leds"] == NUM | CAPS)
		ground.waitFor(
			lambda: lights(ground, VIRTUAL_KEYBOARD) == NUM | CAPS, "the virtual keyboard's lights"
		)
		for name in (UHID, VIRTIO_KEYBOARD, AT_KEYBOARD):
			ground.waitFor(lambda name=name: lights(ground, name) == NUM | CAPS, f"{name}'s lights")
		ground.waitFor(lambda: NUM | CAPS in uhidLights(keyboard), "the uhid keyboard to be sent its lights")
		ground.machine.log(f"Caps Lock told {told['t'] - mark.t:.3f} s after the mark; X {xLocks(ground)!r}")
		mark = ground.mark()
		keyboard.tap("KEY_A")
		app = ground.waitForKeys("app", [("KEY_A", 1), ("KEY_A", 0)], mark)
		assert app[0]["keyval"] == "A", app

	with ground.subtest("after STOP the keyboards keep the lights the desktop set"):
		mark = ground.mark()
		client.stop()
		client.waitFor("RELEASED", mark)
		ground.machine.log(
			f"after STOP: lights: virtio {lights(ground, VIRTIO_KEYBOARD)}, AT {lights(ground, AT_KEYBOARD)}, "
			f"uhid {lights(ground, UHID)}; X {xLocks(ground)!r}"
		)
		mark = ground.mark()
		keyboard.tap("KEY_CAPSLOCK")
		ground.waitForKeys("x", [("KEY_CAPSLOCK", 1), ("KEY_CAPSLOCK", 0)], mark, device=UHID)
		ground.waitFor(
			lambda: lights(ground, UHID) == NUM, "the X server to light the uhid keyboard itself again"
		)
