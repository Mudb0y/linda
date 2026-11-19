"""Modifiers pressed while the Linda key is held, on the kernel route: Shift outlasting the Linda key
types capitals in the program, as on the X route, where the X server's state follows a key a grab
keeps; a chord of the Linda key with Shift is still Linda's alone; and no key is left down in the X
server or the program.
"""

from typing import TYPE_CHECKING

from ground import KERNEL_ROUTE

if TYPE_CHECKING:
	from ground import Ground

UHID = "Ground uhid keyboard"
GAP = 0.05


def leftDown(records: list[dict]) -> list[str]:
	down: dict[str, bool] = {}
	for r in records:
		if r["value"] != 2:
			down[r["key"]] = r["value"] == 1
	return sorted(key for key, held in down.items() if held)


def run(ground: "Ground") -> None:
	ground.openWindow()
	ground.waitForHelper()
	keyboard = ground.uhidKeyboard(UHID)
	ground.startLinda()
	ground.waitForLindaLog(KERNEL_ROUTE, timeout=30)
	ground.settle(1)

	with ground.subtest("a capital typed with Shift still held after a chord of the Linda key with Shift"):
		mark = ground.mark()
		keyboard.run(
			{"press": "KEY_INSERT"},
			{"sleep": GAP},
			{"press": "KEY_LEFTSHIFT"},
			{"sleep": GAP},
			{"release": "KEY_INSERT"},
			{"sleep": GAP},
			{"tap": "KEY_A", "hold": 0.03},
			{"sleep": GAP},
			{"release": "KEY_LEFTSHIFT"},
		)
		ground.waitForKeys("app", [("KEY_LEFTSHIFT", 1)], mark)
		ground.settle(0.5)
		app = ground.keys("app", mark)
		typed = [r["keyval"] for r in app if r["key"] == "KEY_A" and r["value"] == 1]
		assert typed == ["A"], app
		assert not [r for r in app if r["key"] == "KEY_INSERT"], app
		# What Linda echoes is what the program typed.
		echoed = [text for _t, text in ground.speech(mark) if text.lower() == "a"]
		assert echoed == ["A"], ground.speech(mark)
		assert not leftDown(app), app
		assert not leftDown(ground.keys("x", mark)), ground.keys("x", mark)

	with ground.subtest("the Linda key with Shift and the up arrow is Linda's, and leaves nothing down"):
		ground.settle(0.8)
		mark = ground.mark()
		keyboard.chord("KEY_INSERT", "KEY_LEFTSHIFT", "KEY_UP")
		ground.waitFor(lambda: ground.speech(mark), "Linda to report the selection")
		ground.settle(0.5)
		app = ground.keys("app", mark)
		assert not [r for r in app if r["key"] in ("KEY_INSERT", "KEY_UP")], app
		assert not leftDown(app), app
		assert not leftDown(ground.keys("x", mark)), ground.keys("x", mark)
