"""A helper ended at every capture, as a fault that comes back each time would end it: after the unit's
start limit systemd leaves it stopped, and the keyboard is the desktop's, through X, with nothing
grabbing it.
"""

from typing import TYPE_CHECKING

from ground import VIRTUAL_KEYBOARD

if TYPE_CHECKING:
	from ground import Ground

UHID = "Ground uhid keyboard"
UNIT = "linda-keyd.service"


def run(ground: "Ground") -> None:
	ground.openWindow()
	keyboard = ground.uhidKeyboard(UHID)
	client = ground.keydClient("main")

	with ground.subtest("ended at every capture, it is left stopped"):
		ends = 0
		while ground.shell(f"systemctl show -P Result {UNIT}").strip() != "start-limit-hit":
			assert ends < 8, f"still started again after {ends} ends"
			pid = ground.waitForHelper()
			mark = ground.mark()
			client.capture(UHID, since=mark)
			ground.shell(f"kill -KILL {pid}")
			ends += 1
			client.waitFor("CLOSED", mark)
			ground.waitFor(
				lambda pid=pid: (
					ground.helperPid() not in (0, pid)
					or ground.shell(f"systemctl show -P Result {UNIT}").strip() == "start-limit-hit"
				),
				"systemd to start the helper again or give up",
				10,
			)
		state = ground.shell(f"systemctl show -P ActiveState {UNIT}").strip()
		ground.machine.log(f"left {state} after {ends} ends")
		assert state == "failed", state
		assert ground.helperPid() == 0

	with ground.subtest("the keyboard is the desktop's"):
		ground.settle(2)
		assert ground.helperPid() == 0
		mark = ground.mark()
		keyboard.tap("KEY_A")
		ground.waitForKeys("x", [("KEY_A", 1), ("KEY_A", 0)], mark, device=UHID)
		assert not ground.keys("x", mark, device=VIRTUAL_KEYBOARD)
