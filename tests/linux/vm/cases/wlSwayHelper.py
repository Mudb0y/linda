"""linda-keyd's safety on sway, with no Linda: every subtest of wl/helperSafety.py, as wlGnomeHelper
runs them under GNOME, here in the sway guest's own session, with Xwayland on demand. Its figures go
to the log as one JSON object, after "VH sway figures".

The lock lights are the one layer sway does differently. sway takes the lights off every keyboard
whenever one is added, the helper's virtual keyboard among them, and keeps its locks (experiment E6,
cases/wlSwayProbes.py), where mutter writes the seat's locks to the new keyboard. helperSafety's locks
subtests expect mutter's lights, so this case runs them as sway has it, and holds what the client is
told to what the program then gets, Num Lock by the numpad's 1 and Caps Lock by a letter. The helper
takes the locks from the lights it finds at its first grab, so on sway it tells the client they are
off while they are on: the two subtests that show it assert the truth as an expected failure.
"""

import json
from typing import TYPE_CHECKING

from ground import AT_KEYBOARD, VIRTIO_KEYBOARD, VIRTUAL_KEYBOARD, WINDOW_TITLE
from wl import helperSafety
from wl.helperSafety import CAPS, NUM, appKeyvals, lights, restartHelper, typedKeys, uhidLights

if TYPE_CHECKING:
	from ground import FakeKeyboard, Ground, KeydClient

UHID = helperSafety.UHID
REAL = (VIRTIO_KEYBOARD, AT_KEYBOARD, UHID)
#: The fault the lock subtests find on sway, which they assert against so that its going is seen.
LIGHTS_GONE = (
	"sway takes the lights off every keyboard as the helper's virtual keyboard is added and keeps its "
	"locks, and the helper, which takes the locks from the lights it finds at its first grab, tells "
	"the client they are off"
)


def allLights(ground: "Ground") -> dict[str, int]:
	return {name: lights(ground, name) for name in REAL}


def keypad(ground: "Ground") -> list[str]:
	"""What the program had the numpad's 1 as, from QEMU's keyboard: KP_1 with Num Lock on."""
	mark = ground.mark()
	ground.press("kp_1")
	return appKeyvals(ground.waitForKeys("app", typedKeys("KEY_KP1"), mark))


def letterA(ground: "Ground", keyboard: "FakeKeyboard") -> list[str]:
	mark = ground.mark()
	keyboard.tap("KEY_A")
	return appKeyvals(ground.waitForKeys("app", typedKeys("KEY_A"), mark))


def locks(ground: "Ground", keyboard: "FakeKeyboard", client: "KeydClient", figures: dict) -> None:
	"""helperSafety.locks as sway has it, leaving the helper as it leaves it: let go, with Caps Lock off."""
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
		seen["numLockBefore"] = {"before": before, **allLights(ground), "keypad": keypad(ground)}
		ground.machine.log(f"Num Lock pressed with no virtual keyboard: {seen['numLockBefore']}")
		assert before == 0, f"the desktop started with Num Lock on: {before}"
		assert seen["numLockBefore"]["keypad"] == ["KP_1"], seen

	with ground.subtest("the locks at the start of capture are the desktop's, told and lit"):
		mark = ground.mark()
		client.capture(UHID, VIRTIO_KEYBOARD, AT_KEYBOARD, since=mark)
		ground.settle(1)
		told = [r["leds"] for r in client.records(mark, "LOCKS")]
		seen["atStart"] = {
			"told": told,
			"virtual": lights(ground, VIRTUAL_KEYBOARD),
			**allLights(ground),
			"keypad": keypad(ground),
		}
		ground.machine.log(f"at the start of capture: {seen['atStart']}")
		assert told, seen["atStart"]
		for name in REAL:
			assert seen["atStart"][name] == told[-1], (name, seen["atStart"])
		num = NUM if seen["atStart"]["keypad"] == ["KP_1"] else 0
		with ground.expectedFailure(LIGHTS_GONE):
			assert told[-1] & NUM == num, seen["atStart"]

	with ground.subtest("Caps Lock through the helper is locked, lit everywhere and told"):
		was = num
		mark = ground.mark()
		keyboard.tap("KEY_CAPSLOCK")
		ground.waitForKeys("evdev", typedKeys("KEY_CAPSLOCK"), mark, device=VIRTUAL_KEYBOARD)
		toldCaps = client.waitFor("LOCKS", mark, lambda r: r["leds"] & CAPS)
		ground.settle(1)
		seen["caps"] = {
			"told": [r["leds"] for r in client.records(mark, "LOCKS")],
			"toldAfter": round(toldCaps["t"] - mark.t, 3),
			"virtual": lights(ground, VIRTUAL_KEYBOARD),
			**allLights(ground),
			"uhidReports": uhidLights(keyboard),
			"letter": letterA(ground, keyboard),
			"keypad": keypad(ground),
		}
		ground.machine.log(f"Caps Lock through the helper: {seen['caps']}, Num Lock was {was}")
		assert seen["caps"]["letter"] == ["A"], seen["caps"]
		told = seen["caps"]["told"]
		assert told[-1] & CAPS, seen["caps"]
		for name in REAL:
			assert seen["caps"][name] == told[-1], (name, seen["caps"])
		assert bool(told[-1] & NUM) == (seen["caps"]["keypad"] == ["KP_1"]), seen["caps"]

	with ground.subtest("letting go leaves the keyboards with the console's lights"):
		mark = ground.mark()
		client.stop()
		client.waitFor("RELEASED", mark)
		ground.settle(0.5)
		seen["afterStop"] = allLights(ground)
		ground.machine.log(f"after STOP: lights {seen['afterStop']}")
		assert set(seen["afterStop"].values()) == {0}, seen["afterStop"]

	with ground.subtest("a new helper takes the locks from the desktop, not from stale lights"):
		restartHelper(ground, client, "systemctl restart linda-keyd")
		mark = ground.mark()
		client.capture(UHID, VIRTIO_KEYBOARD, AT_KEYBOARD, since=mark)
		ground.settle(0.5)
		told = [r["leds"] for r in client.records(mark, "LOCKS")]
		seen["newHelper"] = {
			"told": told,
			"virtual": lights(ground, VIRTUAL_KEYBOARD),
			**allLights(ground),
			"letter": letterA(ground, keyboard),
		}
		ground.machine.log(f"a new helper: {seen['newHelper']}")
		assert told, seen["newHelper"]
		for name in REAL:
			assert lights(ground, name) == told[-1], (name, seen["newHelper"])
		with ground.expectedFailure(LIGHTS_GONE):
			assert bool(told[-1] & CAPS) == (seen["newHelper"]["letter"] == ["A"]), seen["newHelper"]

	with ground.subtest("let go again, the desktop lights the keyboards itself at the next change"):
		mark = ground.mark()
		client.stop()
		client.waitFor("RELEASED", mark)
		capsOn = seen["newHelper"]["letter"] == ["A"]
		mark = ground.mark()
		keyboard.tap("KEY_CAPSLOCK")
		ground.waitForKeys("evdev", typedKeys("KEY_CAPSLOCK"), mark, device=UHID)
		ground.waitFor(
			lambda: bool(lights(ground, UHID) & CAPS) != capsOn,
			"the desktop to light the uhid keyboard itself",
		)
		if not capsOn:
			assert letterA(ground, keyboard) == ["A"]
			keyboard.tap("KEY_CAPSLOCK")
			ground.waitFor(lambda: not lights(ground, UHID) & CAPS, "Caps Lock's light off again")
		assert letterA(ground, keyboard) == ["a"]


def run(ground: "Ground") -> None:
	ground.machine.log(f"start-up, seconds from starting the guest: {ground.bootTimes}")
	ground.machine.log(f"the compositor: {json.dumps(ground.focus('facts'))}")
	figures: dict[str, object] = {}
	ground.openWindow()
	ground.activate(WINDOW_TITLE)
	ground.waitForHelper()
	keyboard = ground.uhidKeyboard(UHID)
	client = ground.keydClient("main")
	helperSafety.grab(ground, keyboard, client, figures)
	helperSafety.latency(ground, keyboard, client, figures)
	locks(ground, keyboard, client, figures)
	helperSafety.hotplug(ground, client, figures)
	helperSafety.emergency(ground, keyboard, client, figures)
	helperSafety.console(ground, keyboard, client, figures)
	helperSafety.watchdogThread(ground, keyboard, client, figures)
	helperSafety.stopped(ground, keyboard, client, figures)
	ground.machine.log("VH sway figures " + json.dumps(figures, sort_keys=True))
