"""The emergency chord, Backspace, Escape and Enter held together, under a running Linda: the helper
lets go, Linda says it has, and the X route works; Linda does not take the keyboard back by itself, and
the command to take it back brings the kernel route back, which Linda says.
"""

from typing import TYPE_CHECKING

from ground import KERNEL_ROUTE, VIRTUAL_KEYBOARD, WINDOW_TITLE, X_ROUTE

if TYPE_CHECKING:
	from ground import FakeKeyboard, Ground

UHID = "Ground uhid keyboard"
CHORD_KEYS = ("KEY_INSERT", "KEY_T")
APART = 0.8
LET_GO = "Linda has let go of the keyboard\N{HORIZONTAL ELLIPSIS}"


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
	lindaKeyWorks(ground, keyboard)

	with ground.subtest("the chord lets go, and Linda says so"):
		mark = ground.mark()
		written = keyboard.run(
			{"press": "KEY_BACKSPACE"},
			{"sleep": 0.05},
			{"press": "KEY_ESC"},
			{"sleep": 0.05},
			{"press": "KEY_ENTER"},
			{"sleep": 0.1},
			{"release": "KEY_ENTER"},
			{"release": "KEY_ESC"},
			{"release": "KEY_BACKSPACE"},
		)
		said = ground.waitForSpeech(LET_GO, mark)
		enter = next(r for r in written if r["key"] == "KEY_ENTER")
		ground.waitForHelperLog("the emergency chord", mark)
		fell = ground.waitForLindaLog(X_ROUTE, count=2)[1]
		assert "linda-keyd was given up: it let go of the keyboard" in fell, fell
		ground.machine.log(f"Linda said it had let go {said - enter['t']:.3f} s after Enter went down")
		ground.settle(0.5)
		chord = {"KEY_BACKSPACE", "KEY_ESC", "KEY_ENTER"}
		typed = [r["key"] for r in ground.keys("app", mark) if r["key"] in chord and r["value"] == 1]
		ground.machine.log(f"of the chord the program had: {typed}")

	with ground.subtest("the X route works, and Linda does not take the keyboard back by itself"):
		lindaKeyWorks(ground, keyboard)
		mark = ground.mark()
		keyboard.tap("KEY_A")
		ground.waitForKeys("x", [("KEY_A", 1), ("KEY_A", 0)], mark, device=UHID)
		app = ground.waitForKeys("app", [("KEY_A", 1), ("KEY_A", 0)], mark)
		assert app[0]["keyval"] == "a", app
		ground.expect("a", since=mark)
		ground.settle(3)
		assert len(ground.lindaLogRecords(KERNEL_ROUTE)) == 1
		mark = ground.mark()
		keyboard.tap("KEY_B")
		ground.waitForKeys("x", [("KEY_B", 1), ("KEY_B", 0)], mark, device=UHID)

	with ground.subtest("the command to take it back brings the kernel route back, and Linda says so"):
		ground.settle(APART)
		mark = ground.mark()
		keyboard.chord("KEY_INSERT", "KEY_LEFTSHIFT", "KEY_F12")
		ground.expect("Linda has the keyboard again", since=mark, timeout=15)
		ground.waitForLindaLog(KERNEL_ROUTE, count=2)
		lindaKeyWorks(ground, keyboard)
		mark = ground.mark()
		keyboard.tap("KEY_C")
		ground.waitForKeys("x", [("KEY_C", 1), ("KEY_C", 0)], mark, device=VIRTUAL_KEYBOARD)
		ground.expect("c", since=mark)

	with ground.subtest("asked again with the kernel route in place, Linda says it has the keyboard"):
		ground.settle(APART)
		mark = ground.mark()
		keyboard.chord("KEY_INSERT", "KEY_LEFTSHIFT", "KEY_F12")
		ground.expect("Linda has the keyboard already", since=mark)
