"""Keyboards coming and going under a running Linda on the kernel route. A second keyboard plugged in
is taken by the helper and its Linda key is Linda's; one unplugged in the middle of a chord, Shift
passed on and the Linda key kept, leaves no key held, to the X server or to Linda.

A keyboard the helper leaves alone, as it leaves one made through uinput by a remapper, one that is
also a pointer, and one another program holds, reaches the X server straight, where Linda reads it as
on the X route beside the kernel route: its Linda key is Linda's, and what is typed on it is echoed
once. A modifier held on it counts for a key typed on a keyboard the helper holds, as the X server
has it in force for that key.
"""

from typing import TYPE_CHECKING

from ground import KERNEL_ROUTE, VIRTUAL_KEYBOARD, WINDOW_TITLE, X_ROUTE

if TYPE_CHECKING:
	from ground import FakeKeyboard, Ground

UHID = "Ground uhid keyboard"
SECOND = "Ground second keyboard"
CHORD_KEYS = ("KEY_INSERT", "KEY_T")
APART = 0.8


def lindaKeyWorks(ground: "Ground", keyboard: "FakeKeyboard") -> None:
	ground.settle(APART)
	mark = ground.mark()
	keyboard.chord(*CHORD_KEYS)
	ground.expect(WINDOW_TITLE, since=mark)
	ground.settle(0.5)
	reached = [r for r in ground.keys("app", mark) if r["key"] in CHORD_KEYS]
	assert not reached, reached


def run(ground: "Ground") -> None:
	ground.openWindow()
	ground.waitForHelper()
	keyboard = ground.uhidKeyboard(UHID)
	ground.startLinda()
	ground.waitForLindaLog(KERNEL_ROUTE, timeout=30)

	with ground.subtest("a keyboard plugged in is taken, and its Linda key is Linda's"):
		mark = ground.mark()
		second = ground.uhidKeyboard(SECOND)
		held = ground.waitForHelperLog(f"({SECOND}): Held", mark)[0]
		added = next(
			r for r in ground.records("evdev", mark) if r["kind"] == "device" and r["device"] == SECOND
		)
		ground.machine.log(f"the helper held the new keyboard {held - added['t']:.3f} s after it appeared")
		lindaKeyWorks(ground, second)
		mark = ground.mark()
		second.tap("KEY_N")
		ground.waitForKeys("x", [("KEY_N", 1), ("KEY_N", 0)], mark, device=VIRTUAL_KEYBOARD)
		ground.expect("n", since=mark)

	with ground.subtest("a keyboard unplugged with Shift and the Linda key held leaves nothing held"):
		mark = ground.mark()
		second.run({"press": "KEY_LEFTSHIFT"}, {"sleep": 0.05}, {"press": "KEY_INSERT"})
		ground.waitForKeys("x", [("KEY_LEFTSHIFT", 1)], mark, device=VIRTUAL_KEYBOARD)
		ground.settle(0.2)
		second.vanish()
		ground.waitForKeys("x", [("KEY_LEFTSHIFT", 1), ("KEY_LEFTSHIFT", 0)], mark, device=VIRTUAL_KEYBOARD)
		ground.waitForHelperLog(f"({SECOND}) is gone", mark)
		ground.settle(0.3)
		mark = ground.mark()
		keyboard.tap("KEY_T")
		app = ground.waitForKeys("app", [("KEY_T", 1), ("KEY_T", 0)], mark)
		assert app[0]["keyval"] == "t", app
		ground.expect("t", since=mark)
		ground.settle(0.5)
		assert WINDOW_TITLE not in [text for _t, text in ground.speech(mark)], ground.speech(mark)
		lindaKeyWorks(ground, keyboard)
		assert len(ground.lindaLogRecords(X_ROUTE)) == 1, "Linda left the kernel route"

	with ground.subtest("a keyboard unplugged in the middle of the Linda key with T"):
		third = ground.uhidKeyboard("Ground third keyboard")
		ground.waitForHelperLog("(Ground third keyboard): Held")
		ground.settle(APART)
		mark = ground.mark()
		third.run({"press": "KEY_INSERT"}, {"sleep": 0.05}, {"press": "KEY_T"}, {"sleep": 0.05})
		ground.expect(WINDOW_TITLE, since=mark)
		third.vanish()
		ground.settle(0.5)
		mark = ground.mark()
		keyboard.tap("KEY_E")
		app = ground.waitForKeys("app", [("KEY_E", 1), ("KEY_E", 0)], mark)
		assert app[0]["keyval"] == "e", app
		ground.expect("e", since=mark)
		lindaKeyWorks(ground, keyboard)

	with ground.subtest("a keyboard the helper leaves alone, here one made through uinput"):
		remapped = ground.keyboard("Ground uinput keyboard")
		ground.waitForHelperLog("leaving alone")
		ground.settle(APART)
		mark = ground.mark()
		remapped.chord(*CHORD_KEYS)
		ground.expect(WINDOW_TITLE, since=mark)
		ground.settle(0.5)
		reached = [(r["key"], r["value"]) for r in ground.keys("app", mark) if r["key"] in CHORD_KEYS]
		assert not reached, reached
		assert [text for _t, text in ground.speech(mark)].count(WINDOW_TITLE) == 1, ground.speech(mark)
		mark = ground.mark()
		remapped.tap("KEY_R", "KEY_E")
		app = ground.waitForKeys("app", [("KEY_R", 1), ("KEY_R", 0), ("KEY_E", 1), ("KEY_E", 0)], mark)
		assert [r["keyval"] for r in app if r["value"] == 1] == ["r", "e"], app
		ground.expect("r", "e", since=mark)
		ground.settle(0.5)
		spoken = [text for _t, text in ground.speech(mark)]
		assert spoken.count("r") == 1 and spoken.count("e") == 1, spoken
		assert len(ground.lindaLogRecords(X_ROUTE)) == 1, "Linda left the kernel route"

	with ground.subtest("Shift held on a keyboard the helper leaves alone counts for a key of one it holds"):
		ground.settle(APART)
		mark = ground.mark()
		keyboard.chord("KEY_INSERT", "KEY_1")
		ground.expect("input help on…", since=mark)
		mark = ground.mark()
		remapped.run({"press": "KEY_LEFTSHIFT"}, {"sleep": 0.1})
		keyboard.tap("KEY_T")
		remapped.run({"sleep": 0.1}, {"release": "KEY_LEFTSHIFT"})
		# Input help says the gesture; without the Shift the X server had, it said t.
		ground.expect("shift plus t", since=mark)
		mark = ground.mark()
		keyboard.chord("KEY_INSERT", "KEY_1")
		ground.expect("input help off", since=mark)
		assert len(ground.lindaLogRecords(X_ROUTE)) == 1, "Linda left the kernel route"
