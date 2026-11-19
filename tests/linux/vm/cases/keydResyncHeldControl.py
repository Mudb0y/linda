"""A Control that goes down while the kernel is dropping events, and is still held when the helper
has caught up, is told to the client and put out on the virtual keyboard just before C, typed under it
afterwards, so that the program has Control+C and not C alone. Found in the fifth review.

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
	keyboard.run({"tap": "KEY_C"})
	ground.settle(0.5)
	keyboard.run({"release": "KEY_LEFTCTRL"})
	ground.settle(0.5)
	events = [(r["key"], r["value"]) for r in ground.keys("x", stall, device=VIRTUAL_KEYBOARD)]
	app = [(r["key"], r["value"]) for r in ground.keys("app", stall)]
	flood = [key for key, value in events[:before] if value == 1]
	ground.machine.log(
		f"of {len(FLOOD)} taps in the stall, {len(flood)} presses went out; the virtual keyboard "
		f"then put out {events[before:]}; the program had {app}"
	)
	assert pid == ground.helperPid(), "the helper was ended"
	assert len(flood) < len(FLOOD), "no events were lost, so this shows nothing"
	# Held back until a key goes out under it, Control goes out just before C.
	assert events[before:] == [
		("KEY_LEFTCTRL", 1),
		("KEY_C", 1),
		("KEY_C", 0),
		("KEY_LEFTCTRL", 0),
	], f"Control and C did not go out on the virtual keyboard as Control+C: {events[before:]}"
	held: set[str] = set()
	underControl = None
	for key, value in app:
		if key == "KEY_C" and value == 1:
			underControl = "KEY_LEFTCTRL" in held
		(held.add if value == 1 else held.discard)(key)
	assert underControl, f"the program did not have C under Control: {app}"
