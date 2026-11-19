"""The helper's virtual keyboard repeats keys, and has sticky keys and the rest of AccessX, as the
desktop set them on the core keyboard: before Linda started, which the virtual keyboard, coming later,
would not have from the X server, and again while it runs. A key held on a keyboard the helper holds
then repeats at the desktop's pace.
"""

import itertools
import json
import shlex
from typing import TYPE_CHECKING

from ground import GROUND, KERNEL_ROUTE, VIRTUAL_KEYBOARD

if TYPE_CHECKING:
	from ground import FakeKeyboard, Ground

UHID = "Ground uhid keyboard"
CONTROLS = f"{GROUND}/controls.py"
#: Run in the session with the ground tools' Python: "read" prints each keyboard's repeat delay and
#: interval and whether sticky keys are on, by device name; "set DELAY INTERVAL STICKY" sets the core
#: keyboard's repeat and turns sticky keys on or off, as a desktop does.
CONTROLS_SCRIPT = r"""
import json, sys
from Xlib import display
from Xlib.ext import xinput
from Xlib.protocol import rq


class GetControls(rq.ReplyRequest):
	_request = rq.Struct(rq.Card8("opcode"), rq.Opcode(6), rq.RequestLength(), rq.Card16("deviceSpec"), rq.Pad(2))
	_reply = rq.Struct(
		rq.ReplyCode(), rq.Card8("deviceID"), rq.Card16("sequence_number"), rq.ReplyLength(),
		rq.Card8("mkDfltBtn"), rq.Card8("numGroups"), rq.Card8("groupsWrap"), rq.Card8("internalMods"),
		rq.Card8("ignoreLockMods"), rq.Card8("internalRealMods"), rq.Card8("ignoreLockRealMods"), rq.Pad(1),
		rq.Card16("internalVMods"), rq.Card16("ignoreLockVMods"), rq.Card16("repeatDelay"),
		rq.Card16("repeatInterval"), rq.Card16("slowKeysDelay"), rq.Card16("debounceDelay"),
		rq.Card16("mkDelay"), rq.Card16("mkInterval"), rq.Card16("mkTimeToMax"), rq.Card16("mkMaxSpeed"),
		rq.Int16("mkCurve"), rq.Card16("axOptions"), rq.Card16("axTimeout"), rq.Card16("axtOptsMask"),
		rq.Card16("axtOptsValues"), rq.Pad(2), rq.Card32("axtCtrlsMask"), rq.Card32("axtCtrlsValues"),
		rq.Card32("enabledCtrls"), rq.FixedBinary("perKeyRepeat", 32),
	)


class SetControls(rq.Request):
	_request = rq.Struct(
		rq.Card8("opcode"), rq.Opcode(7), rq.RequestLength(), rq.Card16("deviceSpec"),
		rq.Card8("affectInternalRealMods"), rq.Card8("internalRealMods"), rq.Card8("affectIgnoreLockRealMods"),
		rq.Card8("ignoreLockRealMods"), rq.Card16("affectInternalVirtualMods"), rq.Card16("internalVirtualMods"),
		rq.Card16("affectIgnoreLockVirtualMods"), rq.Card16("ignoreLockVirtualMods"), rq.Card8("mouseKeysDfltBtn"),
		rq.Card8("groupsWrap"), rq.Card16("accessXOptions"), rq.Pad(2), rq.Card32("affectEnabledControls"),
		rq.Card32("enabledControls"), rq.Card32("changeControls"), rq.Card16("repeatDelay"),
		rq.Card16("repeatInterval"), rq.Card16("slowKeysDelay"), rq.Card16("debounceDelay"),
		rq.Card16("mouseKeysDelay"), rq.Card16("mouseKeysInterval"), rq.Card16("mouseKeysTimeToMax"),
		rq.Card16("mouseKeysMaxSpeed"), rq.Int16("mouseKeysCurve"), rq.Card16("accessXTimeout"),
		rq.Card32("accessXTimeoutMask"), rq.Card32("accessXTimeoutValues"), rq.Card16("accessXTimeoutOptionsMask"),
		rq.Card16("accessXTimeoutOptionsValues"), rq.FixedBinary("perKeyRepeat", 32),
	)


d = display.Display()
info = d.query_extension("XKEYBOARD")
op = info.major_opcode
# UseExtension
class Use(rq.ReplyRequest):
	_request = rq.Struct(rq.Card8("opcode"), rq.Opcode(0), rq.RequestLength(), rq.Card16("wantedMajor"), rq.Card16("wantedMinor"))
	_reply = rq.Struct(rq.ReplyCode(), rq.Bool("supported"), rq.Card16("sequence_number"), rq.ReplyLength(), rq.Card16("serverMajor"), rq.Card16("serverMinor"), rq.Pad(20))
Use(display=d.display, opcode=op, wantedMajor=1, wantedMinor=0)
if sys.argv[1] == "set":
	delay, interval, sticky = int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4])
	cur = GetControls(display=d.display, opcode=op, deviceSpec=0x100)
	SetControls(
		display=d.display, opcode=op, deviceSpec=0x100, affectInternalRealMods=0, internalRealMods=0,
		affectIgnoreLockRealMods=0, ignoreLockRealMods=0, affectInternalVirtualMods=0, internalVirtualMods=0,
		affectIgnoreLockVirtualMods=0, ignoreLockVirtualMods=0, mouseKeysDfltBtn=cur.mkDfltBtn,
		groupsWrap=cur.groupsWrap, accessXOptions=cur.axOptions, affectEnabledControls=8,
		enabledControls=8 if sticky else 0, changeControls=1 | (1 << 31), repeatDelay=delay,
		repeatInterval=interval, slowKeysDelay=cur.slowKeysDelay, debounceDelay=cur.debounceDelay,
		mouseKeysDelay=cur.mkDelay, mouseKeysInterval=cur.mkInterval, mouseKeysTimeToMax=cur.mkTimeToMax,
		mouseKeysMaxSpeed=cur.mkMaxSpeed, mouseKeysCurve=cur.mkCurve, accessXTimeout=cur.axTimeout,
		accessXTimeoutMask=cur.axtCtrlsMask, accessXTimeoutValues=cur.axtCtrlsValues,
		accessXTimeoutOptionsMask=cur.axtOptsMask, accessXTimeoutOptionsValues=cur.axtOptsValues,
		perKeyRepeat=cur.perKeyRepeat,
	)
	d.sync()
	print("set")
else:
	found = {}
	for dev in d.xinput_query_device(xinput.AllDevices).devices:
		if dev.use in (xinput.MasterKeyboard, xinput.SlaveKeyboard):
			c = GetControls(display=d.display, opcode=op, deviceSpec=dev.deviceid)
			found[str(dev.name)] = [c.repeatDelay, c.repeatInterval, bool(c.enabledCtrls & 8)]
	print(json.dumps(found))
"""
WITH_TOOLS_PYTHON = (
	'tool=$(readlink -f "$(command -v ground-keyboard)"); '
	'python=$(cat "$tool" "$(dirname "$tool")/.ground-keyboard-wrapped" 2>/dev/null | tr "\\000" "\\n" '
	"| grep -o '/nix/store/[a-z0-9]*-python3[^/]*/bin/python3[.0-9]*' | head -1); "
	'exec "$python" "$@"'
)


def controls(ground: "Ground", *args: str) -> str:
	ran = ground.run(["sh", "-c", WITH_TOOLS_PYTHON, "controls", CONTROLS, *args])
	assert ran.returncode == 0, ran
	return ran.stdout


def held(ground: "Ground", keyboard: "FakeKeyboard") -> list[float]:
	"""Hold A for a second and a half: the gaps between the presses the window had."""
	mark = ground.mark()
	keyboard.run({"press": "KEY_A"}, {"sleep": 1.5}, {"release": "KEY_A"})
	ground.settle(0.5)
	presses = [r["t"] for r in ground.keys("app", mark) if r["key"] == "KEY_A" and r["value"] == 1]
	return [round(later - earlier, 3) for earlier, later in itertools.pairwise(presses)]


def run(ground: "Ground") -> None:
	ground.openWindow()
	ground.waitForHelper()
	keyboard = ground.uhidKeyboard(UHID)
	ground.machine.succeed(f"echo {shlex.quote(CONTROLS_SCRIPT)} > {CONTROLS}", f"chmod 644 {CONTROLS}")
	controls(ground, "set", "300", "20", "1")
	ground.startLinda()
	ground.waitForLindaLog(KERNEL_ROUTE, timeout=30)
	ground.settle(1)

	with ground.subtest("the virtual keyboard has the controls the desktop set before Linda started"):
		found = json.loads(controls(ground, "read"))
		ground.machine.log(f"each keyboard's repeat delay, interval and sticky keys: {found}")
		assert found[VIRTUAL_KEYBOARD] == found["Virtual core keyboard"] == [300, 20, True], found

	with ground.subtest("a held key repeats at the desktop's pace"):
		controls(ground, "set", "400", "30", "0")
		ground.settle(0.5)
		found = json.loads(controls(ground, "read"))
		assert found[VIRTUAL_KEYBOARD] == [400, 30, False], found
		gaps = held(ground, keyboard)
		ground.machine.log(f"A held: the gaps between its presses {gaps}")
		assert gaps and 0.35 <= gaps[0] <= 0.5, gaps
		assert all(0.02 <= gap <= 0.06 for gap in gaps[1:]), gaps
