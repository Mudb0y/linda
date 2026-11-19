"""The outermost watchdog: a helper stopped by SIGSTOP, holding a keyboard and with Shift passed on and
held, can do nothing at all, its own watchdog thread included. Within two seconds of its last sign of
life systemd's watchdog sends SIGKILL, which ends a stopped process as it would a running one, with
no core dump; the kernel then lets go of the keyboard and destroys the virtual keyboard, releasing
Shift, and systemd starts the helper again.
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

	with ground.subtest("a stopped helper is killed by systemd's watchdog, and the keyboard comes back"):
		mark = ground.mark()
		keyboard.run({"press": "KEY_LEFTSHIFT"})
		ground.waitForKeys("x", [("KEY_LEFTSHIFT", 1)], mark, device=VIRTUAL_KEYBOARD)
		ground.shell(f"kill -STOP {pid}")
		stopped = ground.now()
		# A key every quarter of a second until one reaches the X server from the keyboard itself.
		back = None
		for _ in range(40):
			tapped = keyboard.run({"tap": "KEY_Q", "hold": 0.01})
			ground.settle(0.2)
			if ground.keys("x", mark, device=UHID):
				back = tapped[0]["t"]
				break
		assert back is not None, "the keyboard did not come back within ten seconds"
		ground.machine.log(f"the keyboard was the desktop's again within {back - stopped:.2f} s of the stop")
		release = [r for r in ground.keys("x", mark, device=VIRTUAL_KEYBOARD) if r["key"] == "KEY_LEFTSHIFT"]
		assert [r["value"] for r in release] == [1, 0], release
		ground.machine.log(f"Shift released by the kernel {release[1]['t'] - stopped:.2f} s after the stop")
		ground.waitForHelperLog("Watchdog timeout", mark)
		ground.waitForHelperLog("with signal SIGKILL", mark)
		ground.waitForHelperLog("status=9/KILL", mark)
		for t, line in ground.helperLog(mark):
			ground.machine.log(f"{t - stopped:+.3f} s: {line}")
		# The watchdog's two seconds count from the last time the loop fed it, before the stop.
		assert release[1]["t"] - stopped < 2.3, release
		assert back - stopped < 3.0, back - stopped

	with ground.subtest("systemd starts it again, and it serves a client"):
		ground.waitFor(lambda: ground.helperPid() not in (0, pid), "the helper to start again", 10)
		assert ground.helperRestarts() == 1, ground.helperRestarts()
		client.waitFor("CLOSED", mark)
		mark = ground.mark()
		keyboard.run({"release": "KEY_LEFTSHIFT"})
		client.capture(UHID, since=mark)
		mark = ground.mark()
		keyboard.tap("KEY_W")
		app = ground.waitForKeys("app", [("KEY_W", 1), ("KEY_W", 0)], mark)
		assert app[0]["keyval"] == "w", app
		ground.waitForKeys("x", [("KEY_W", 1), ("KEY_W", 0)], mark, device=VIRTUAL_KEYBOARD)
