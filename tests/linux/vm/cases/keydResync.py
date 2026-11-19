"""Lost events: while the helper cannot read, a keyboard sends far more than the kernel keeps for it,
Shift's release among them, so the kernel drops what it holds and says so. The helper looks at what
the kernel has down, releases Shift on the virtual keyboard, telling the client of the release, made
by the helper, before anything read after the loss could go out under it, and goes on with what
follows.

The helper cannot read because it is stopped with SIGSTOP, for less time than either watchdog
allows.
"""

from typing import TYPE_CHECKING

from ground import VIRTUAL_KEYBOARD

if TYPE_CHECKING:
	from ground import Ground

UHID = "Ground uhid keyboard"
#: Some two hundred events, several times what the kernel keeps for a reader of a keyboard. Forty
#: seven taps leave five presses after the last loss, read with it; fifty two left none, so that
#: nothing showed what becomes of them.
FLOOD = [f"KEY_{c}" for c in "QWERTYUIOPASDFGHJKLZXCVBNM" * 2][:47]


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
		# Stopped and continued by the keyboard itself, between its keys, so that neither a loaded
		# host's round trips nor a program starting keep it stopped past STALL_LIMIT, after which its
		# watchdog thread rightly ends it.
		since = ground.mark()
		keyboard.run(
			{"signal": [pid, "SIGSTOP"]},
			{"release": "KEY_LEFTSHIFT"},
			*({"tap": key, "hold": 0} for key in FLOOD),
			{"signal": [pid, "SIGCONT"]},
		)
		stopped, continued = (r["t"] for r in keyboard.records(since) if "signal" in r)
		ground.machine.log(
			f"the helper was stopped {continued - stopped:.3f} s, {len(FLOOD)} keys typed meanwhile"
		)
		shift = client.waitFor("KEY", mark, lambda r: r["key"] == "KEY_LEFTSHIFT" and r["value"] == 0)
		assert shift["synthetic"], shift
		release = ground.waitForKeys(
			"x", [("KEY_LEFTSHIFT", 1), ("KEY_LEFTSHIFT", 0)], mark, device=VIRTUAL_KEYBOARD
		)
		events = [(r["key"], r["value"]) for r in ground.keys("x", mark, device=VIRTUAL_KEYBOARD)]
		under = [key for key, value in events[: events.index(("KEY_LEFTSHIFT", 0))] if value == 1]
		assert under == ["KEY_LEFTSHIFT"], f"keys went out under a Shift let go of in the loss: {under}"
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
		assert continued - stopped < 0.8, "stopped too near STALL_LIMIT for the helper to be judged"
		assert pid == ground.helperPid(), "the helper was ended"

	with ground.subtest("typing goes on with nothing down"):
		mark = ground.mark()
		keyboard.tap("KEY_Q")
		app = ground.waitForKeys("app", [("KEY_Q", 1), ("KEY_Q", 0)], mark)
		assert app[0]["keyval"] == "q", app
		ground.waitForKeys("x", [("KEY_Q", 1), ("KEY_Q", 0)], mark, device=VIRTUAL_KEYBOARD)
