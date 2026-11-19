"""/dev/uinput tagged for the user in front, as Steam's rules tag it: udev and logind take the helper's
entry off the node's access list whenever they pass over it, as when a session comes to the front.
The helper, which opened the node as it started, makes its virtual keyboard all the same; started
again, it has its entry back from its unit, and makes it again after the entry is taken off once more.
"""

from typing import TYPE_CHECKING

from ground import VIRTUAL_KEYBOARD

if TYPE_CHECKING:
	from ground import Ground

UHID = "Ground uhid keyboard"


def hasEntry(ground: "Ground") -> bool:
	return ground.machine.execute("getfacl -cp /dev/uinput | grep -q '^user:linda-keyd:'")[0] == 0


def takeEntryOff(ground: "Ground") -> None:
	"""Have udev pass over /dev/uinput, as logind has it do whenever a session comes to the front."""
	ground.shell("udevadm trigger --settle --action=change /sys/devices/virtual/misc/uinput")
	ground.waitFor(lambda: not hasEntry(ground), "udev to take the helper's entry off /dev/uinput")
	ground.machine.log(ground.shell("getfacl -cp /dev/uinput"))


def run(ground: "Ground") -> None:
	ground.openWindow()
	pid = ground.waitForHelper()
	keyboard = ground.uhidKeyboard(UHID)
	client = ground.keydClient("main")

	with ground.subtest("udev takes the helper's entry off a node tagged for the user in front"):
		assert "uaccess" in ground.shell("udevadm info -q property -p /sys/devices/virtual/misc/uinput")
		takeEntryOff(ground)

	with ground.subtest("the helper makes its virtual keyboard all the same"):
		mark = ground.mark()
		client.capture(UHID, since=mark)
		mark = ground.mark()
		keyboard.tap("KEY_A")
		ground.waitForKeys("x", [("KEY_A", 1), ("KEY_A", 0)], mark, device=VIRTUAL_KEYBOARD)

	with ground.subtest("started again, it has its entry back and makes it again"):
		mark = ground.mark()
		ground.shell("systemctl restart linda-keyd")
		ground.waitFor(lambda: ground.helperPid() not in (0, pid), "the helper to start again")
		client.waitFor("CLOSED", mark)
		assert hasEntry(ground), ground.shell("getfacl -cp /dev/uinput")
		takeEntryOff(ground)
		mark = ground.mark()
		client.capture(UHID, since=mark)
		mark = ground.mark()
		keyboard.tap("KEY_B")
		ground.waitForKeys("x", [("KEY_B", 1), ("KEY_B", 0)], mark, device=VIRTUAL_KEYBOARD)
