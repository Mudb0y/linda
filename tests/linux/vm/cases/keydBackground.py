"""The client's session going to the background: a switch to a text console lets go of every
keyboard, so that what is typed there reaches the console and not the client, and the switch back
captures again by itself, without the client asking.
"""

from typing import TYPE_CHECKING

from ground import VIRTIO_KEYBOARD, VIRTUAL_KEYBOARD

if TYPE_CHECKING:
	from ground import Ground

UHID = "Ground uhid keyboard"
CONSOLE = 2


def run(ground: "Ground") -> None:
	ground.openWindow()
	ground.waitForHelper()
	keyboard = ground.uhidKeyboard(UHID)
	client = ground.keydClient("main")
	client.capture(UHID, VIRTIO_KEYBOARD)
	desktop = int(ground.shell("fgconsole").strip())
	ground.machine.log(f"the X session is on console {desktop}")

	with ground.subtest("a switch to a text console lets go"):
		mark = ground.mark()
		ground.shell(f"chvt {CONSOLE}")
		released = client.waitFor("RELEASED", mark)
		assert released["reason"] == "BACKGROUND", released
		ground.machine.log(f"let go {released['t'] - mark.t:.3f} s after the switch")
		ground.waitForHelperLog("in front on seat0", mark)
		mark = ground.mark()
		keyboard.tap("KEY_A")
		# A reader of the keyboard, as the console is, has it; the client does not.
		ground.waitForKeys("evdev", [("KEY_A", 1), ("KEY_A", 0)], mark, device=UHID)
		ground.settle(0.5)
		assert not client.keys(mark), client.keys(mark)
		assert not ground.keys("x", mark, device=VIRTUAL_KEYBOARD)

	with ground.subtest("the switch back captures again by itself"):
		mark = ground.mark()
		ground.shell(f"chvt {desktop}")
		held = client.waitForDevice(UHID, "HELD", mark)
		client.waitForDevice(VIRTIO_KEYBOARD, "HELD", mark)
		ground.machine.log(f"held again {held['t'] - mark.t:.3f} s after the switch back")
		assert not [r for r in client.records(mark) if r["dir"] == "out" and r["type"] == "START"]
		mark = ground.mark()
		keyboard.tap("KEY_B")
		app = ground.waitForKeys("app", [("KEY_B", 1), ("KEY_B", 0)], mark)
		assert app[0]["keyval"] == "b", app
		ground.waitForKeys("x", [("KEY_B", 1), ("KEY_B", 0)], mark, device=VIRTUAL_KEYBOARD)
