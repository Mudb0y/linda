"""Faults found in the fifth review, of Linda's side of the kernel route as a whole, each shown against
the stand-in for linda-keyd and, where the X route is the reference, against the X route in the same
harness. Each test fails while the fault is there. Run under the rig:

	nix develop . -c tests/linux/rig python3 -m pytest tests/linux/test_kernelKeysReview5.py
"""

from pathlib import Path

import pytest
from test_kernelKeys import LET_GO_MESSAGE, runKernelChild

#: Keys NVDA was told are down and was never told are up, by virtual-key code, from what it was
#: delivered.
STILL_DOWN = r"""
def stillDown():
	down = {}
	for e in delivered:
		if e["repeat"]:
			continue
		if e["up"]:
			down.pop(e["vk"], None)
		else:
			down[e["vk"]] = True
	return sorted(down)
"""

#: With the Polish layout, AltGr goes down while the keyboard is held, as for input help or while
#: Remote Access sends the keys to another computer, the hold ends, and AltGr comes up: which keys
#: NVDA is left believing are down.
ALTGR_ACROSS_HOLD_CHILD = (
	STILL_DOWN
	+ r"""
import subprocess
subprocess.run(["setxkbmap", "pl"], check=True)
time.sleep(0.3)
source = startSource(ROUTE)
source.holdKeyboard(True)
time.sleep(0.3)
delivered.clear()
keyboard.down("ISO_Level3_Shift")
time.sleep(0.1)
source.holdKeyboard(False)
time.sleep(0.3)
keyboard.up("ISO_Level3_Shift")
time.sleep(0.3)
afterAltGr = stillDown()
keyboard.press("h", hold=0.03)
time.sleep(0.3)
result = {
	"kernel": source.isKernel,
	"events": [[e["vk"], e["up"]] for e in delivered if not e["repeat"]],
	"stillDown": afterAltGr,
}
subprocess.run(["setxkbmap", "us"], check=True)
source.stop()
keyboard.close()
"""
)


@pytest.mark.parametrize("route", ("x11", "auto"))
def test_fault_altGrHeldAcrossTheEndOfAHoldLeavesNVDAWithControlAndAltDown(tmp_path: Path, route: str):
	value = runKernelChild(tmp_path, f"ROUTE = {route!r}\n" + ALTGR_ACROSS_HOLD_CHILD)
	assert value["kernel"] == (route == "auto")
	# NVDA was told of AltGr's press, as Windows' hook reports it, a left Control and a right Alt.
	assert [0xA5, False] in value["events"], value
	assert value["stillDown"] == [], (
		f"on the {route} route AltGr went down while the keyboard was held and came up after: NVDA was "
		f"never told of its release, and takes the keys {[hex(vk) for vk in value['stillDown']]} for held, "
		f"so that every key after is a chord with Control and Alt: {value}"
	)


#: With the layouts US and Polish, right Alt goes down in the US group, where it is Alt, the group is
#: locked to the Polish one, where it is AltGr, as a panel or a group switching chord does, and right
#: Alt comes up: which keys NVDA is left believing are down.
ALT_ACROSS_GROUP_CHILD = (
	STILL_DOWN
	+ r"""
import subprocess
subprocess.run(["setxkbmap", "-layout", "us,pl"], check=True)
time.sleep(0.3)
source = startSource(ROUTE)
info = app.query_extension("XKEYBOARD")
x11._XkbUseExtension(display=app.display, opcode=info.major_opcode, wantedMajor=1, wantedMinor=0)


def lockGroup(group):
	x11._XkbLatchLockState(
		display=app.display,
		opcode=info.major_opcode,
		deviceSpec=x11.XKB_CORE_KEYBOARD,
		affectModLocks=0,
		modLocks=0,
		lockGroup=True,
		groupLock=group,
		affectModLatches=0,
		modLatches=0,
		latchGroup=False,
		groupLatch=0,
	)
	app.sync()
	time.sleep(0.3)


delivered.clear()
keyboard.down("ISO_Level3_Shift")
time.sleep(0.1)
lockGroup(1)
keyboard.up("ISO_Level3_Shift")
time.sleep(0.3)
afterAlt = stillDown()
lockGroup(0)
result = {
	"kernel": source.isKernel,
	"events": [[e["vk"], e["up"]] for e in delivered if not e["repeat"]],
	"stillDown": afterAlt,
}
subprocess.run(["setxkbmap", "us"], check=True)
source.stop()
keyboard.close()
"""
)


@pytest.mark.parametrize("route", ("x11", "auto"))
def test_fault_rightAltHeldAcrossAGroupChangeStaysDownForNVDA(tmp_path: Path, route: str):
	value = runKernelChild(tmp_path, f"ROUTE = {route!r}\n" + ALT_ACROSS_GROUP_CHILD)
	assert value["kernel"] == (route == "auto")
	assert [0xA5, False] in value["events"], value
	assert value["stillDown"] == [], (
		f"on the {route} route right Alt went down as Alt and came up as AltGr, after a change of group: "
		f"NVDA was never told of its release, and takes Alt for held from then on: {value}"
	)


#: Numpad Insert is the NVDA key, Num Lock is off, and numpad Insert goes down; Num Lock is turned on
#: while it is held, as by another keyboard or a program, and it comes up: which keys NVDA is left
#: believing are down.
NUMPAD_INSERT_ACROSS_NUM_LOCK_CHILD = (
	STILL_DOWN
	+ r"""
info = app.query_extension("XKEYBOARD")
x11._XkbUseExtension(display=app.display, opcode=info.major_opcode, wantedMajor=1, wantedMinor=0)


def numLock(on):
	x11._XkbLatchLockState(
		display=app.display,
		opcode=info.major_opcode,
		deviceSpec=x11.XKB_CORE_KEYBOARD,
		affectModLocks=x11.NUM_LOCK_MASK,
		modLocks=x11.NUM_LOCK_MASK if on else 0,
		lockGroup=False,
		groupLock=0,
		affectModLatches=0,
		modLatches=0,
		latchGroup=False,
		groupLatch=0,
	)
	app.sync()
	time.sleep(0.3)


numLock(False)
source = startSource(ROUTE, consumed=x11.NUMPAD_INSERT)
delivered.clear()
keyboard.down("KP_Insert")
time.sleep(0.1)
numLock(True)
keyboard.up("KP_Insert")
time.sleep(0.3)
left = stillDown()
numLock(False)
result = {
	"kernel": source.isKernel,
	"events": [[e["vk"], e["up"], e["nvda"]] for e in delivered if not e["repeat"]],
	"stillDown": left,
}
source.stop()
keyboard.close()
"""
)


@pytest.mark.parametrize("route", ("x11", "auto"))
def test_fault_numpadInsertHeldAcrossNumLockStaysTheNVDAKeyForNVDA(tmp_path: Path, route: str):
	value = runKernelChild(tmp_path, f"ROUTE = {route!r}\n" + NUMPAD_INSERT_ACROSS_NUM_LOCK_CHILD)
	assert value["kernel"] == (route == "auto")
	assert [0x2D, False, True] in value["events"], value
	assert value["stillDown"] == [], (
		f"on the {route} route numpad Insert, the NVDA key, went down as Insert with Num Lock off and came up "
		f"as numpad 0 with it on: NVDA was never told the NVDA key it holds went up, and takes every key "
		f"after for a command: {value}"
	)


#: The thread that answers keys is held up while Shift goes down and the helper says goodbye and
#: closes, so that the verdict on Shift, sent as its press is read, finds the connection closed: in
#: what order NVDA is told of Shift going down and up.
PRESS_AFTER_LOSS_CHILD = r"""
source = startSource()
kernel = source._kernel
held = threading.Event()
kernel._post(lambda: held.wait(5))
time.sleep(0.05)
delivered.clear()
keyboard.down("Shift_L")
time.sleep(0.05)
keyboard.goodbye(8, "presses went unanswered")
time.sleep(0.2)
held.set()
deadline = time.monotonic() + 5
while source.isKernel and time.monotonic() < deadline:
	time.sleep(0.02)
time.sleep(0.5)
keyboard.up("Shift_L")
time.sleep(0.2)
shift = [[e["up"], e["t"]] for e in delivered if e["vk"] == 0xA0 and not e["repeat"]]
result = {
	"kernelAfter": source.isKernel,
	"shift": [up for up, _t in shift],
	"downFor": next((shift[i + 1][1] - t for i, (up, t) in enumerate(shift[:-1]) if not up), None),
}
source.stop()
keyboard.close()
"""


def test_fault_pressReadAsTheConnectionFailsReachesNVDAAfterItsRelease(tmp_path: Path):
	value = runKernelChild(tmp_path, PRESS_AFTER_LOSS_CHILD)
	assert not value["kernelAfter"], value
	assert value["shift"] == [False, True], (
		"Shift's verdict found the connection closed: the release the loss gives every key down went to NVDA "
		"before the press itself, and NVDA took Shift for held until the route released what the kernel "
		f"source had left, {value['downFor']} s later: {value}"
	)


#: The X display does not have the virtual keyboard, so the route waits half a second after the
#: helper holds the keyboard before it takes the kernel route or gives the helper up. The emergency
#: chord is pressed in that half second: whether NVDA lets go until the user asks, as the chord
#: promises, and says so.
EMERGENCY_BEFORE_THE_ROUTE_CHILD = r"""
source = route.RoutedKeySource("auto", path, said.append, "No such keyboard")
source.start(deliver, lambda: mode["now"])
source.setConsumed(x11.EXTENDED_INSERT | x11.CAPS_LOCK, True)
keyboard.waitFor(lambda: keyboard.capturing, 10)
time.sleep(0.05)
for name in ("BackSpace", "Escape", "Return"):
	keyboard.down(name)
	time.sleep(0.02)
time.sleep(0.1)
for name in ("Return", "Escape", "BackSpace"):
	keyboard.up(name)
time.sleep(5)
result = {
	"emergency": len([e for e in keyboard.events if e[1] == "emergency"]),
	"starts": keyboard.starts,
	"said": list(said),
}
source.stop()
keyboard.close()
"""


def test_fault_emergencyChordBeforeTheRouteIsTakenIsNotHonoured(tmp_path: Path):
	value = runKernelChild(tmp_path, EMERGENCY_BEFORE_THE_ROUTE_CHILD)
	assert value["emergency"] == 1, value
	assert value["starts"] == 1, (
		"the emergency chord was pressed while the helper held the keyboard and before the route had seen "
		"the X display take up the virtual keyboard; NVDA asked the helper to capture again by itself "
		f"two seconds later: {value}"
	)
	assert value["said"] == [LET_GO_MESSAGE], value


# The second pass, over the fixes.

#: As linda-keyd now does as it lets go: what it read last of a keyboard goes to the program as a tap,
#: pressed and released at once on the virtual keyboard, and is not sent to the client as KEY. Here the
#: last key read is Insert, the NVDA key, and the helper lets go as it cannot tell which session is in
#: front, the person's own still being: whether NVDA heard of Insert, and whether the program had it.
TAP_AS_THE_HELPER_LETS_GO_CHILD = r"""
from keydStandIn import RELEASED_SESSION_UNKNOWN
source = startSource()
windowKeys()
delivered.clear()
with keyboard._lock:
	keyboard._toX(code("Insert"), True, virtual=True)
	keyboard._toX(code("Insert"), False, virtual=True)
	keyboard.letGo(RELEASED_SESSION_UNKNOWN)
time.sleep(0.5)
result = {
	"nvda": [[e["vk"], e["up"]] for e in delivered if e["vk"] == 0x2D],
	"window": [key for key in windowKeys() if key[0] == 110],
}
source.stop()
keyboard.close()
"""


def test_fault_keyTheHelperTapsAsItLetsGoReachesTheProgramAndNotNVDA(tmp_path: Path):
	value = runKernelChild(tmp_path, TAP_AS_THE_HELPER_LETS_GO_CHILD)
	assert value["nvda"] == [[0x2D, False], [0x2D, True]] and value["window"] == [], (
		"Insert, read by the helper as it let go and put out on the virtual keyboard as a tap without a KEY, "
		"was taken by the X source for a key the kernel source had decided and given to nobody: NVDA never "
		f"heard of it and the program had it: {value}"
	)
