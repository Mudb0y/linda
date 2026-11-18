"""The helper killed outright, with SIGKILL, holding a keyboard with Shift passed on and held: the
kernel lets go of the keyboard and destroys the virtual keyboard, releasing Shift as it does, and
systemd starts the helper again a second later.
"""

from typing import TYPE_CHECKING

from ground import VIRTUAL_KEYBOARD

if TYPE_CHECKING:
	from ground import Ground

UHID = "Ground uhid keyboard"


def run(ground: "Ground") -> None:
	ground.openWindow()
	pid = ground.waitForHelper()
	keyboard = ground.uhidKeyboard(UHID)
	client = ground.keydClient("main")
	client.capture(UHID)

	with ground.subtest("the kernel lets go of a killed helper's keyboard and releases its keys"):
		mark = ground.mark()
		keyboard.run({"press": "KEY_LEFTSHIFT"})
		ground.waitForKeys("x", [("KEY_LEFTSHIFT", 1)], mark, device=VIRTUAL_KEYBOARD)
		killed = ground.now()
		ground.shell(f"kill -KILL {pid}")
		release = ground.waitForKeys(
			"x", [("KEY_LEFTSHIFT", 1), ("KEY_LEFTSHIFT", 0)], mark, device=VIRTUAL_KEYBOARD
		)
		ground.waitForDevice("x", VIRTUAL_KEYBOARD, change="removed")
		ground.machine.log(f"Shift released by the kernel {release[1]['t'] - killed:.3f} s after the kill")
		after = ground.mark()
		written = keyboard.run({"release": "KEY_LEFTSHIFT"}, {"tap": "KEY_Q"})
		app = ground.waitForKeys("app", [("KEY_Q", 1), ("KEY_Q", 0)], after)
		assert app[0]["keyval"] == "q", app
		x = ground.waitForKeys("x", [("KEY_Q", 1), ("KEY_Q", 0)], after, device=UHID)
		ground.machine.log(
			f"Q reached the X server from the keyboard itself {x[0]['t'] - written[1]['t']:.4f} s after it was typed"
		)
		client.waitFor("CLOSED", mark)

	with ground.subtest("systemd starts it again"):
		ground.waitFor(lambda: ground.helperPid() not in (0, pid), "the helper to start again", 10)
		mark = ground.mark()
		client.capture(UHID, since=mark)
		mark = ground.mark()
		keyboard.tap("KEY_W")
		ground.waitForKeys("x", [("KEY_W", 1), ("KEY_W", 0)], mark, device=VIRTUAL_KEYBOARD)
