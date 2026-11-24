"""Linda uses the kernel route only while the X server has the helper's virtual keyboard as an enabled
keyboard of a master: otherwise every key the helper passes on would go nowhere, with the helper and
Linda both answering as they should. Disabled or floated under a running Linda, the virtual keyboard
makes Linda give the helper up within a bound, say once that keys come from the X display, and stay
there without asking the helper again, which would take the keyboard each time; enabled or attached
again, Linda takes the kernel route back by itself and says nothing. Asked to take the keyboard back
while it is disabled, Linda never stays on the kernel route. The X server makes a slave pointer of
the virtual keyboard too, of the same name, for the wheel of the consumer controls it carries; that
half disabled, Linda stays on the kernel route. Typing reaches the program throughout, but for the
moment of each change.

How long the X server takes to have the virtual keyboard once the helper has made it goes to the log.
"""

from typing import TYPE_CHECKING

from ground import KERNEL_ROUTE, VIRTUAL_KEYBOARD, WINDOW_TITLE, X_ROUTE

if TYPE_CHECKING:
	from ground import FakeKeyboard, Ground

UHID = "Ground uhid keyboard"
CHORD_KEYS = ("KEY_INSERT", "KEY_T")
APART = 0.8
FELL_BACK = "Linda now reads keys from the X display"
#: Seconds from the X server's change to Linda's word that it reads keys from the X display: the half
#: second Linda allows the X server, and Linda's own turn.
FALL_BACK_BOUND = 1.5


def half(ground: "Ground", kind: str = "keyboard") -> str:
	"""The X server's number for the virtual keyboard's keyboard half, or its pointer half, found while
	that half is attached and kept for changing it after."""
	device = ground.xinputDevice(VIRTUAL_KEYBOARD, kind)
	assert device is not None, kind
	return device


def lindaKeyWorks(ground: "Ground", keyboard: "FakeKeyboard") -> None:
	ground.settle(APART)
	mark = ground.mark()
	keyboard.chord(*CHORD_KEYS)
	ground.expect(WINDOW_TITLE, since=mark)
	ground.settle(0.5)
	reached = [r for r in ground.keys("app", mark) if r["key"] in CHORD_KEYS]
	assert not reached, reached


def typingReaches(ground: "Ground", keyboard: "FakeKeyboard", key: str, keyval: str, device: str) -> None:
	mark = ground.mark()
	keyboard.tap(key)
	ground.waitForKeys("x", [(key, 1), (key, 0)], mark, device=device)
	app = ground.waitForKeys("app", [(key, 1), (key, 0)], mark)
	assert app[0]["keyval"] == keyval, app
	ground.expect(keyval, since=mark)


def fallsBack(ground: "Ground", change: list[str], routes: int) -> None:
	"""Make change with xinput, and hold Linda to giving the helper up within FALL_BACK_BOUND."""
	mark = ground.mark()
	result = ground.run(["xinput", *change])
	assert result.returncode == 0, result.stderr
	said = ground.waitForSpeech(FELL_BACK, mark)
	ground.machine.log(
		f"xinput {' '.join(change)}: Linda said it reads the X display {said - mark.t:.3f} s later"
	)
	assert said - mark.t < FALL_BACK_BOUND, said - mark.t
	record = ground.waitForLindaLog(X_ROUTE, count=routes)[1]
	assert "is not an enabled keyboard of a master" in record, record
	ground.waitForHelperLog("let go of every keyboard", mark)


def staysOnX(ground: "Ground", keyboard: "FakeKeyboard", kernelRoutes: int) -> None:
	"""Several of Linda's looks for the helper go by without its asking."""
	mark = ground.mark()
	ground.settle(7)
	asked = [line for _t, line in ground.helperLog(mark) if "capturing" in line]
	assert not asked, asked
	assert len(ground.lindaLogRecords(KERNEL_ROUTE)) == kernelRoutes
	typingReaches(ground, keyboard, "KEY_U", "u", UHID)
	lindaKeyWorks(ground, keyboard)


def comesBack(ground: "Ground", change: list[str], kernelRoutes: int) -> None:
	mark = ground.mark()
	result = ground.run(["xinput", *change])
	assert result.returncode == 0, result.stderr
	ground.waitForLindaLog(KERNEL_ROUTE, count=kernelRoutes, timeout=10)
	ground.settle(0.5)
	assert FELL_BACK not in [text for _t, text in ground.speech(mark)], ground.speech(mark)


def run(ground: "Ground") -> None:
	ground.openWindow()
	ground.waitForHelper()
	keyboard = ground.uhidKeyboard(UHID)
	ground.startLinda()
	ground.waitForLindaLog(KERNEL_ROUTE, timeout=30)

	with ground.subtest("how long the X server takes to have the virtual keyboard"):
		made = ground.waitForHelperLog("made the virtual keyboard")[0]
		ready = ground.waitForHelperLog("the virtual keyboard is ready")[0]
		added = next(
			r for r in ground.records("x") if r["kind"] == "device" and r["device"] == VIRTUAL_KEYBOARD
		)
		ground.machine.log(
			f"the X server had the virtual keyboard {(added['t'] - made) * 1000:.1f} ms after the helper made "
			f"it; the helper took it for ready {(ready - made) * 1000:.1f} ms after making it"
		)
		typingReaches(ground, keyboard, "KEY_A", "a", VIRTUAL_KEYBOARD)
		lindaKeyWorks(ground, keyboard)

	with ground.subtest("disabled under a running Linda, then enabled again"):
		keyboardHalf = half(ground)
		fallsBack(ground, ["disable", keyboardHalf], routes=2)
		staysOnX(ground, keyboard, kernelRoutes=1)
		comesBack(ground, ["enable", keyboardHalf], kernelRoutes=2)
		typingReaches(ground, keyboard, "KEY_B", "b", VIRTUAL_KEYBOARD)
		lindaKeyWorks(ground, keyboard)

	with ground.subtest("floated under a running Linda, then attached again"):
		keyboardHalf = half(ground)
		fallsBack(ground, ["float", keyboardHalf], routes=3)
		staysOnX(ground, keyboard, kernelRoutes=2)
		comesBack(ground, ["reattach", keyboardHalf, "Virtual core keyboard"], kernelRoutes=3)
		typingReaches(ground, keyboard, "KEY_C", "c", VIRTUAL_KEYBOARD)
		lindaKeyWorks(ground, keyboard)

	with ground.subtest("asked to take the keyboard back while it is disabled, Linda stays on X"):
		keyboardHalf = half(ground)
		fallsBack(ground, ["disable", keyboardHalf], routes=4)
		ground.settle(APART)
		mark = ground.mark()
		keyboard.chord("KEY_INSERT", "KEY_LEFTSHIFT", "KEY_F12")
		ground.waitForHelperLog("capturing", mark)
		said = ground.waitForSpeech(FELL_BACK, mark)
		ground.machine.log(f"taken back and given up again {said - mark.t:.3f} s after the command")
		ground.settle(1)
		assert len(ground.lindaLogRecords(KERNEL_ROUTE)) == 3
		staysOnX(ground, keyboard, kernelRoutes=3)
		comesBack(ground, ["enable", keyboardHalf], kernelRoutes=4)
		typingReaches(ground, keyboard, "KEY_D", "d", VIRTUAL_KEYBOARD)

	with ground.subtest("its pointer half disabled under a running Linda, Linda stays on the kernel route"):
		pointerHalf = half(ground, "pointer")
		mark = ground.mark()
		routes = len(ground.lindaLogRecords(X_ROUTE)), len(ground.lindaLogRecords(KERNEL_ROUTE))
		result = ground.run(["xinput", "disable", pointerHalf])
		assert result.returncode == 0, result.stderr
		ground.settle(FALL_BACK_BOUND)
		assert FELL_BACK not in [text for _t, text in ground.speech(mark)], ground.speech(mark)
		assert (len(ground.lindaLogRecords(X_ROUTE)), len(ground.lindaLogRecords(KERNEL_ROUTE))) == routes
		let = [line for _t, line in ground.helperLog(mark) if "let go of every keyboard" in line]
		assert not let, let
		typingReaches(ground, keyboard, "KEY_E", "e", VIRTUAL_KEYBOARD)
		lindaKeyWorks(ground, keyboard)
		result = ground.run(["xinput", "enable", pointerHalf])
		assert result.returncode == 0, result.stderr
