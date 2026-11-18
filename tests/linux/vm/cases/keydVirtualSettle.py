"""Whether the desktop has really taken up the virtual keyboard by the time the helper first grabs: the
helper waits a second after the keyboard's node has appeared, and the X server must have added it as
a device of its own before that second is out, or the first keys passed on would be lost. The times
of each step go to the log, and the first key passed on right after the grab must reach the program.
"""

from typing import TYPE_CHECKING

from ground import VIRTUAL_KEYBOARD

if TYPE_CHECKING:
	from ground import Ground

UHID = "Ground uhid keyboard"


def deviceAdded(ground: "Ground", source: str, name: str) -> float:
	return next(
		r["t"]
		for r in ground.records(source)
		if r["kind"] == "device" and r["device"] == name and r["change"] == "added"
	)


def run(ground: "Ground") -> None:
	ground.openWindow()
	ground.waitForHelper()
	keyboard = ground.uhidKeyboard(UHID)
	client = ground.keydClient("main")
	assert client.connect()["welcome"]

	with ground.subtest("the X server has the virtual keyboard before the first grab"):
		mark = ground.mark()
		client.start()
		held = client.waitForDevice(UHID, "HELD", mark)
		written = keyboard.tap("KEY_A", hold=0.02)
		made = ground.waitForHelperLog("made the virtual keyboard", mark)[0]
		ready = ground.waitForHelperLog("the virtual keyboard is ready", mark)[0]
		ground.waitForDevice("x", VIRTUAL_KEYBOARD)
		node = deviceAdded(ground, "evdev", VIRTUAL_KEYBOARD)
		x = deviceAdded(ground, "x", VIRTUAL_KEYBOARD)
		ground.machine.log(
			f"from START: made {made - mark.t:.3f} s, its node read by the evdev recorder {node - mark.t:.3f} s, "
			f"added by the X server {x - mark.t:.3f} s, ready {ready - mark.t:.3f} s, held {held['t'] - mark.t:.3f} s; "
			f"the X server had it {ready - x:.3f} s before the helper took it for ready"
		)
		assert x < ready, (x, ready)
		app = ground.waitForKeys("app", [("KEY_A", 1), ("KEY_A", 0)], mark)
		ground.waitForKeys("x", [("KEY_A", 1), ("KEY_A", 0)], mark, device=VIRTUAL_KEYBOARD)
		ground.machine.log(
			f"A, typed {written[0]['t'] - held['t']:.3f} s after the client heard of the grab, reached the program"
		)
		assert app[0]["keyval"] == "a", app
