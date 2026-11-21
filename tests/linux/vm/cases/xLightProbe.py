"""The Xorg half of experiment E6, in the X guest the keyd cases boot: what the X server writes to a
new keyboard's lock lights, by guest/ledProbe.py's experiment, the same as the Wayland guests' probe
cases make it, so that a desktop's answer can be set beside the owner's own X session's; and whether a
keyboard added takes the desktop's Num Lock or only the lights. Its figures go to the log as one JSON
object, after "E6 x".
"""

import json
import shlex
from typing import TYPE_CHECKING

from ground import VIRTIO_KEYBOARD, keyEvents

if TYPE_CHECKING:
	from ground import Ground

#: The X server's process, by its comm: NixOS starts it as X, Xorg's other name.
READER = "^X(org)?$"


def lights(ground: "Ground", name: str) -> int:
	return next(iter(json.loads(ground.shell(f"ground-poke leds {shlex.quote(name)}")).values()))


def keypad(ground: "Ground") -> list[str]:
	"""What the program had the numpad's 1 as, from QEMU's keyboard: KP_1 with Num Lock on."""
	mark = ground.mark()
	ground.press("kp_1")
	expected = [("KEY_KP1", 1), ("KEY_KP1", 0)]
	assert keyEvents(ground.waitForKeys("app", expected, mark)) == expected
	return [r["keyval"] for r in ground.keys("app", mark) if r["value"] == 1]


def numLock(ground: "Ground", on: bool) -> None:
	"""Put the desktop's Num Lock as asked, judged by what the numpad types, not by the lights."""
	for _press in range(2):
		if (keypad(ground) == ["KP_1"]) == on:
			return
		ground.press("num_lock")
		ground.settle(0.3)
	assert (keypad(ground) == ["KP_1"]) == on, f"Num Lock would not go {'on' if on else 'off'}"


def run(ground: "Ground") -> None:
	with ground.subtest("E6: what the X server writes to a new keyboard's lights"):
		result = json.loads(ground.shell(f"ground-led-probe experiment --reader {shlex.quote(READER)}"))
		ground.machine.log("E6 x " + json.dumps(result))
		assert result["presserOpened"], f"the X server never opened the presser: {result}"
		for run in result["runs"]:
			ground.machine.log(
				f"E6 x, Num Lock {'on' if run['numLock'] else 'off'}, preset {run['preset']} "
				f"({run['presetBits']}): the X server opened it {run['desktop'] and run['desktop']['after']} s "
				f"after it was made and wrote {[(e['code'], e['value'], e['after']) for e in run['ledsByDesktop']]}, "
				f"changing bits {run['desktopChangedBits']}; before it, {run['ledsBeforeDesktop']}; echoed "
				f"{run['echoed']}; the probe ended with {run['final']}; the reference keyboard had "
				f"{run['referenceBefore']} before and {run['referenceAfter']} after"
			)
			assert run["desktop"] is not None, f"the X server never opened the probe: {run['openers']}"

	with ground.subtest("E6: a keyboard added, and the desktop's own Num Lock"):
		ground.openWindow()
		numLock(ground, True)
		before = lights(ground, VIRTIO_KEYBOARD)
		ground.keyboard("Ground added keyboard")
		ground.settle(0.5)
		seen = {
			"lightsBefore": before,
			"lightsAfter": lights(ground, VIRTIO_KEYBOARD),
			"keypadAfter": keypad(ground),
		}
		ground.machine.log(f"E6 x a keyboard added with Num Lock on: {json.dumps(seen)}")
		numLock(ground, False)
