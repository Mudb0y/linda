"""Lost events: while the helper cannot read, a keyboard sends far more than the kernel keeps for it,
Shift's release among them, so the kernel drops what it holds and says so. The helper looks at what
the kernel has down, releases Shift on the virtual keyboard, telling the client of the release, made
by the helper, and goes on with what follows.

The helper cannot read because it is stopped with SIGSTOP, for less time than either watchdog
allows.
"""

from typing import TYPE_CHECKING

from ground import VIRTUAL_KEYBOARD

if TYPE_CHECKING:
	from ground import Ground

UHID = "Ground uhid keyboard"
FLOOD = [f"KEY_{c}" for c in "QWERTYUIOPASDFGHJKLZXCVBNM"] * 12


def run(ground: "Ground") -> None:
	ground.openWindow()
	pid = ground.waitForHelper()
	keyboard = ground.uhidKeyboard(UHID)
	client = ground.keydClient("main")
	client.capture(UHID)

	with ground.subtest("Shift released while the kernel dropped events is released"):
		mark = ground.mark()
		keyboard.run({"press": "KEY_LEFTSHIFT"})
		ground.waitForKeys("x", [("KEY_LEFTSHIFT", 1)], mark, device=VIRTUAL_KEYBOARD)
		ground.shell(f"kill -STOP {pid}")
		stopped = ground.now()
		keyboard.run({"release": "KEY_LEFTSHIFT"}, *({"tap": key, "hold": 0} for key in FLOOD))
		ground.shell(f"kill -CONT {pid}")
		ground.machine.log(
			f"the helper was stopped {ground.now() - stopped:.3f} s, {len(FLOOD)} keys typed meanwhile"
		)
		shift = client.waitFor("KEY", mark, lambda r: r["key"] == "KEY_LEFTSHIFT" and r["value"] == 0)
		assert shift["synthetic"], shift
		release = ground.waitForKeys(
			"x", [("KEY_LEFTSHIFT", 1), ("KEY_LEFTSHIFT", 0)], mark, device=VIRTUAL_KEYBOARD
		)
		passed = [r for r in ground.keys("x", mark, device=VIRTUAL_KEYBOARD) if r["key"] != "KEY_LEFTSHIFT"]
		ground.machine.log(
			f"Shift released at {release[1]['t']:.3f}; of the keys typed meanwhile, {len(passed)} events went on: "
			f"{[(r['key'], r['value']) for r in passed[:12]]}"
		)
		# Every key that went down on the virtual keyboard came up again.
		down: dict[str, int] = {}
		for r in passed:
			down[r["key"]] = down.get(r["key"], 0) + (1 if r["value"] else -1)
		assert all(v == 0 for v in down.values()), down
		assert pid == ground.helperPid(), "the helper was ended"

	with ground.subtest("typing goes on with nothing down"):
		mark = ground.mark()
		keyboard.tap("KEY_Q")
		app = ground.waitForKeys("app", [("KEY_Q", 1), ("KEY_Q", 0)], mark)
		assert app[0]["keyval"] == "q", app
		ground.waitForKeys("x", [("KEY_Q", 1), ("KEY_Q", 0)], mark, device=VIRTUAL_KEYBOARD)
