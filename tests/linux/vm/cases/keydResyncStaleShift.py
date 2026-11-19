"""Keys read after a loss of events never go out under a Shift let go of in the loss (the fourth
review's case).

As keydResync: Shift goes down and out on the virtual keyboard, the helper is stopped, Shift comes up
and a flood of letters overflows the kernel's 64 events for the helper. The flood is each length
from 40 to 52 in turn, most of which leave presses after the report that ends the last loss, read
with it. The helper leaves what it read after the loss alone and believes only the kernel's answer,
so Shift comes up before anything read after the loss could go out, and nothing does under it.
"""

from typing import TYPE_CHECKING

from ground import VIRTUAL_KEYBOARD

if TYPE_CHECKING:
	from ground import Ground

UHID = "Ground uhid keyboard"
LETTERS = "QWERTYUIOPASDFGHJKLZXCVBNM" * 2


def run(ground: "Ground") -> None:
	ground.openWindow()
	pid = ground.waitForHelper()
	keyboard = ground.uhidKeyboard(UHID)
	client = ground.keydClient("main")
	client.capture(UHID)
	shifted = []
	for size in range(40, 53):
		mark = ground.mark()
		keyboard.run({"press": "KEY_LEFTSHIFT"})
		ground.waitForKeys("x", [("KEY_LEFTSHIFT", 1)], mark, device=VIRTUAL_KEYBOARD)
		flood = [f"KEY_{c}" for c in LETTERS[:size]]
		keyboard.run(
			{"signal": [pid, "SIGSTOP"]},
			{"release": "KEY_LEFTSHIFT"},
			*({"tap": key, "hold": 0} for key in flood),
			{"signal": [pid, "SIGCONT"]},
		)
		client.waitFor("KEY", mark, lambda r: r["key"] == "KEY_LEFTSHIFT" and r["value"] == 0)
		ground.settle(0.5)
		events = [(r["key"], r["value"]) for r in ground.keys("x", mark, device=VIRTUAL_KEYBOARD)]
		up = events.index(("KEY_LEFTSHIFT", 0))
		under = [key for key, value in events[:up] if value == 1 and key != "KEY_LEFTSHIFT"]
		typed = [r.get("keyval") for r in ground.keys("app", mark) if r["value"] == 1]
		ground.machine.log(
			f"flood of {size}: went down on the virtual keyboard before Shift came up: {under}; "
			f"the program had {typed}"
		)
		if under:
			shifted.append((size, under, typed))
		assert pid == ground.helperPid(), "the helper was ended"
	assert not shifted, f"keys went out under a Shift let go of while events were lost: {shifted}"
