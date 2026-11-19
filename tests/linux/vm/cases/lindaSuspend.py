"""Linda on the kernel route through the machine's sleep, with every process frozen five seconds
through logind's suspend by the kernel's suspend test mode (keydSuspend says why not a real one).
Afterwards Linda is still on the kernel route, has said nothing, and the Linda key with T reads the
title, kept from the program.
"""

from typing import TYPE_CHECKING

from ground import KERNEL_ROUTE, WINDOW_TITLE, X_ROUTE

if TYPE_CHECKING:
	from ground import Ground, Mark

UHID = "Ground uhid keyboard"
CHORD_KEYS = ("KEY_INSERT", "KEY_T")
FROZEN_FOR = 5


def afterwards(ground: "Ground", keyboard, mark: "Mark", before: int) -> None:
	"""before: how many records of Linda's log said it read keys from X, at mark."""
	ground.waitForHelperLog("the machine has woken", mark, timeout=60)
	ground.settle(4)
	fellBack = [text for _t, text in ground.lindaLogRecords(X_ROUTE)][before:]
	said = [text for _t, text in ground.speech(mark)]
	ground.machine.log(f"after the sleep Linda said {said} and logged {fellBack}")
	assert not fellBack, fellBack
	assert not said, said
	typed = ground.mark()
	keyboard.chord(*CHORD_KEYS)
	ground.expect(WINDOW_TITLE, since=typed)
	ground.settle(0.5)
	leaked = [r for r in ground.keys("app", typed) if r["key"] in CHORD_KEYS]
	assert not leaked, leaked
	assert not [r for r in ground.keys("x", typed) if r["key"] in CHORD_KEYS]


def run(ground: "Ground") -> None:
	ground.openWindow()
	ground.waitForHelper()
	keyboard = ground.uhidKeyboard(UHID)
	ground.startLinda()
	ground.waitForLindaLog(KERNEL_ROUTE, timeout=30)
	ground.settle(1)

	with ground.subtest("frozen five seconds through logind's suspend, Linda stays on the kernel route"):
		ground.shell(
			f"echo {FROZEN_FOR} > /sys/module/suspend/parameters/pm_test_delay && "
			"echo freezer > /sys/power/pm_test"
		)
		mark = ground.mark()
		before = len(ground.lindaLogRecords(X_ROUTE))
		ground.shell("systemctl suspend --no-block")
		try:
			afterwards(ground, keyboard, mark, before)
		finally:
			ground.shell("echo none > /sys/power/pm_test")
