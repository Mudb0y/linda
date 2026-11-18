"""The helper's own watchdog thread: with the helper's loop stopped, and that thread alone, while it
holds a keyboard with Shift passed on and held, the watchdog thread revokes the grab itself within a
little over a second and ends the helper, before systemd's watchdog would; the kernel destroys the
virtual keyboard, releasing Shift, and systemd starts the helper again.
"""

import json
from typing import TYPE_CHECKING

from ground import VIRTUAL_KEYBOARD

if TYPE_CHECKING:
	from ground import Ground

UHID = "Ground uhid keyboard"
#: STALL_LIMIT in protocol.rs.
STALL = 1.0
#: The exit status of a helper the watchdog thread ends, WATCHDOG_EXIT in main.rs.
WATCHDOG_EXIT = 70


def run(ground: "Ground") -> None:
	ground.openWindow()
	pid = ground.waitForHelper()
	keyboard = ground.uhidKeyboard(UHID)
	client = ground.keydClient("main")
	client.capture(UHID)

	with ground.subtest("the watchdog thread lets go of a keyboard whose loop has stopped"):
		mark = ground.mark()
		keyboard.run({"press": "KEY_LEFTSHIFT"})
		ground.waitForKeys("x", [("KEY_LEFTSHIFT", 1)], mark, device=VIRTUAL_KEYBOARD)
		ground.shell(f"systemd-run --collect --unit=ground-stall ground-poke stall {pid} 3")
		stopped = ground.waitFor(
			lambda: [
				line
				for line in ground.shell("journalctl -o cat -u ground-stall || true").splitlines()
				if line.startswith("{")
			],
			"the loop to be stopped",
		)
		stoppedAt = json.loads(stopped[0])["stopped"]
		said = ground.waitForHelperLog("watchdog:", mark, timeout=5)
		ground.machine.log(f"{said[0] - stoppedAt:.3f} s after the loop stopped: {said[1]}")
		release = ground.waitForKeys(
			"x", [("KEY_LEFTSHIFT", 1), ("KEY_LEFTSHIFT", 0)], mark, device=VIRTUAL_KEYBOARD
		)
		ground.machine.log(f"Shift released {release[1]['t'] - stoppedAt:.3f} s after the loop stopped")
		after = ground.mark()
		keyboard.run({"release": "KEY_LEFTSHIFT"}, {"tap": "KEY_Q"})
		x = ground.waitForKeys("x", [("KEY_Q", 1), ("KEY_Q", 0)], after, device=UHID)
		assert STALL < x[0]["t"] - stoppedAt, x
		ground.waitForHelperLog(f"status={WATCHDOG_EXIT}", mark)
		assert not [line for _t, line in ground.helperLog(mark) if "Watchdog timeout" in line]

	with ground.subtest("systemd starts it again"):
		ground.waitFor(lambda: ground.helperPid() not in (0, pid), "the helper to start again", 10)
		client.waitFor("CLOSED", mark)
		mark = ground.mark()
		client.capture(UHID, since=mark)
