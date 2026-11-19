"""Every process frozen for five seconds, as when the machine suspends and resumes, while the helper
holds a keyboard: the kernel's suspend test mode freezes them all and thaws them with the monotonic
clock run on. The helper takes the freeze for what it is and goes on holding the keyboard: its own
watchdog does not end it. systemd's, which would, is set long for this case in keydSuspend.nix.
"""

from typing import TYPE_CHECKING

from ground import VIRTUAL_KEYBOARD

if TYPE_CHECKING:
	from ground import Ground

UHID = "Ground uhid keyboard"
FROZEN_FOR = 5


def run(ground: "Ground") -> None:
	ground.openWindow()
	pid = ground.waitForHelper()
	keyboard = ground.uhidKeyboard(UHID)
	client = ground.keydClient("main")
	client.capture(UHID)

	with ground.subtest("frozen whole and thawed, it holds on"):
		mark = ground.mark()
		before = float(ground.shell("cut -d' ' -f1 /proc/uptime"))
		ground.shell(
			f"echo {FROZEN_FOR} > /sys/module/suspend/parameters/pm_test_delay && "
			"echo freezer > /sys/power/pm_test && echo freeze > /sys/power/state; "
			"echo none > /sys/power/pm_test"
		)
		after = float(ground.shell("cut -d' ' -f1 /proc/uptime"))
		ground.machine.log(f"the monotonic clock ran {after - before:.1f} s across the freeze")
		assert after - before >= FROZEN_FOR, (before, after)
		ground.settle(3)
		said = [line for _t, line in ground.helperLog(mark)]
		ground.machine.log("the helper said:\n" + "\n".join(said))
		assert ground.helperPid() == pid, "the helper was ended"
		assert ground.helperRestarts() == 0
		assert not client.records(mark, "RELEASED") and not client.records(mark, "CLOSED")
		mark = ground.mark()
		keyboard.tap("KEY_A")
		ground.waitForKeys("x", [("KEY_A", 1), ("KEY_A", 0)], mark, device=VIRTUAL_KEYBOARD)
