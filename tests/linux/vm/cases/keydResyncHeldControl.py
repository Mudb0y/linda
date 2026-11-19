"""A Control that goes down while the kernel is dropping events, and is still held when the helper
has caught up, is told to the client and put out on the virtual keyboard, so that C typed under it
afterwards reaches the program as Control+C and not alone. Found in the fifth review.

The helper is stopped with SIGSTOP, as in keydResync, while a flood of taps overflows the kernel's 64
events for it; Control goes down last and stays down. Once the helper runs again and has caught up,
C is tapped and Control let go of.
"""

from typing import TYPE_CHECKING

from ground import VIRTUAL_KEYBOARD

if TYPE_CHECKING:
	from ground import Ground

UHID = "Ground uhid keyboard"
FLOOD = [f"KEY_{c}" for c in "QWERTYUIOPASDFGHJKLZXCVBNM" * 2][:47]


def run(ground: "Ground") -> None:
	ground.openWindow()
	pid = ground.waitForHelper()
	keyboard = ground.uhidKeyboard(UHID)
	client = ground.keydClient("main")
	client.capture(UHID)
	stall = ground.mark()
	keyboard.run(
		{"signal": [pid, "SIGSTOP"]},
		*({"tap": key, "hold": 0} for key in FLOOD),
		{"press": "KEY_LEFTCTRL"},
		{"signal": [pid, "SIGCONT"]},
	)
	ground.settle(1.0)
	before = len(ground.keys("x", stall, device=VIRTUAL_KEYBOARD))
	appBefore = len(ground.keys("app", stall))
	keyboard.run({"tap": "KEY_C"})
	ground.settle(0.5)
	keyboard.run({"release": "KEY_LEFTCTRL"})
	ground.settle(0.5)
	events = [(r["key"], r["value"]) for r in ground.keys("x", stall, device=VIRTUAL_KEYBOARD)]
	app = [(r["key"], r["value"]) for r in ground.keys("app", stall)]
	flood = [key for key, value in events[:before] if value == 1 and key != "KEY_LEFTCTRL"]
	ground.machine.log(
		f"of {len(FLOOD)} taps in the stall, {len(flood)} presses went out; the virtual keyboard "
		f"then put out {events[before:]}, Control being down: {('KEY_LEFTCTRL', 1) in events[:before]}; "
		f"the program then had {app[appBefore:]}"
	)
	assert pid == ground.helperPid(), "the helper was ended"
	assert len(flood) < len(FLOOD), "no events were lost, so this shows nothing"
	down: set[str] = set()
	bare = []
	for i, (key, value) in enumerate(events):
		if value == 1:
			if i >= before and key == "KEY_C" and "KEY_LEFTCTRL" not in down:
				bare.append(key)
			down.add(key)
		else:
			down.discard(key)
	assert ("KEY_C", 1) in events[before:], f"C never went out: {events[before:]}"
	assert not bare, (
		f"Control was held throughout, and C went out on the virtual keyboard without it: {events}; "
		f"the program had {app}"
	)
	held = set()
	for key, value in app[:appBefore]:
		(held.add if value == 1 else held.discard)(key)
	assert "KEY_LEFTCTRL" in held and app[appBefore:][:2] == [("KEY_C", 1), ("KEY_C", 0)], (
		f"the program did not have C under Control: {app}"
	)
