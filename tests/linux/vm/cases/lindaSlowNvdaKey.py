"""The Linda key pressed twice, which passes it on to the program the second time, with Linda taking
60 ms over every key, as it does while its other threads keep the interpreter busy: on the kernel route
the program has the second press, as on the X route, which gives Linda 100 ms. Linda asks the helper
to hold the press longer, which needs a helper that grants WAIT.
"""

from typing import TYPE_CHECKING

from ground import GROUND, KERNEL_ROUTE

if TYPE_CHECKING:
	from ground import Ground

UHID = "Ground uhid keyboard"
#: tests/linux/fixtures/globalPlugins/slowKeys.py's file.
SLOW_KEYS_FILE = f"{GROUND}/linda-slow-keys"
SLOWNESS = 0.06
#: What the kernel source logs when the helper grants WAIT.
WAITS = "linda-keyd holds a press longer when asked"
#: Within NVDA's double press.
APART = 0.15


def run(ground: "Ground") -> None:
	ground.openWindow()
	ground.waitForHelper()
	keyboard = ground.uhidKeyboard(UHID)
	ground.startLinda()
	ground.waitForLindaLog(KERNEL_ROUTE, timeout=30)
	ground.settle(1)

	with ground.subtest("the Linda key pressed twice with Linda slow reaches the program"):
		ground.machine.succeed(f"echo {SLOWNESS} > {SLOW_KEYS_FILE}")
		try:
			mark = ground.mark()
			keyboard.run(
				{"tap": "KEY_INSERT", "hold": 0.03},
				{"sleep": APART},
				{"tap": "KEY_INSERT", "hold": 0.03},
			)
			ground.settle(1)
		finally:
			ground.machine.succeed(f"rm -f {SLOW_KEYS_FILE}")
		app = ground.keys("app", mark)
		defaulted = len(ground.lindaLogRecords("answered without NVDA"))
		ground.machine.log(f"Linda's own thread answered {defaulted} presses without NVDA")
		if ground.lindaLogRecords(WAITS):
			assert [(r["key"], r["value"]) for r in app] == [("KEY_INSERT", 1), ("KEY_INSERT", 0)], app
		else:
			with ground.expectedFailure("this linda-keyd cannot hold a press for a slow Linda"):
				assert [(r["key"], r["value"]) for r in app] == [("KEY_INSERT", 1), ("KEY_INSERT", 0)], app
		waited = [m for _t, m in ground.helperLog(mark) if "WAIT" in m or "wait" in m]
		ground.machine.log(f"the helper on WAIT: {waited or 'nothing logged'}")
