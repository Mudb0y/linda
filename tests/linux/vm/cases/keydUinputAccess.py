"""/dev/uinput tagged for the user in front, as Steam's rules tag it, so that udev and logind take every
other user's entry off its access list whenever they pass over it. The helper has the node from
systemd, opened before its user is changed, and needs no entry: started again and again while udev
passes over the node without pause and the guest is kept busy, it makes its virtual keyboard every
time.
"""

from typing import TYPE_CHECKING

from ground import VIRTUAL_KEYBOARD

if TYPE_CHECKING:
	from ground import Ground

UHID = "Ground uhid keyboard"
UNIT = "linda-keyd.service"
STARTS = 10


def run(ground: "Ground") -> None:
	ground.openWindow()
	pid = ground.waitForHelper()
	keyboard = ground.uhidKeyboard(UHID)
	client = ground.keydClient("main")
	assert "uaccess" in ground.shell("udevadm info -q property -p /sys/devices/virtual/misc/uinput")
	assert "linda-keyd" not in ground.shell("getfacl -cp /dev/uinput")

	with ground.subtest("made at every start, with udev passing over the node and the guest busy"):
		ground.shell(
			"systemd-run --unit=storm --collect sh -c "
			"'while :; do udevadm trigger --action=change /sys/devices/virtual/misc/uinput; done'"
		)
		ground.shell(
			"for i in 1 2 3 4; do systemd-run --unit=busy$i --collect sh -c 'while :; do :; done'; done"
		)
		for start in range(STARTS):
			mark = ground.mark()
			ground.shell(f"systemctl reset-failed {UNIT}; systemctl restart {UNIT}")
			ground.waitFor(lambda pid=pid: ground.helperPid() not in (0, pid), "the helper to start again")
			pid = ground.helperPid()
			if start:
				client.waitFor("CLOSED", mark)
			client.capture(UHID, since=mark)
			typed = ground.mark()
			keyboard.tap("KEY_A")
			ground.waitForKeys("x", [("KEY_A", 1), ("KEY_A", 0)], typed, device=VIRTUAL_KEYBOARD)
			said = [line for _t, line in ground.helperLog(mark)]
			assert not [line for line in said if "/dev/uinput" in line], said
		ground.shell("systemctl stop storm busy1 busy2 busy3 busy4")
		ground.machine.log(f"the virtual keyboard was made at each of {STARTS} starts")
