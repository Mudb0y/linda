"""The helper going wrong under a running Linda. Killed while a chord is held, Shift passed on and the
Linda key kept: Linda goes on through X with no key stuck, and comes back to the kernel route when
systemd has restarted the helper. Stopped with SIGSTOP: the keyboard is dead until systemd's watchdog
ends the helper, Linda goes on through X, and comes back once the helper is restarted.
"""

from typing import TYPE_CHECKING

from ground import KERNEL_ROUTE, VIRTUAL_KEYBOARD, WINDOW_TITLE, X_ROUTE

if TYPE_CHECKING:
	from ground import FakeKeyboard, Ground

UHID = "Ground uhid keyboard"
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


def plainLetter(ground: "Ground", keyboard: "FakeKeyboard", key: str, keyval: str) -> None:
	"""A letter typed alone reaches the program as itself and is no command of Linda's."""
	mark = ground.mark()
	keyboard.tap(key)
	app = ground.waitForKeys("app", [(key, 1), (key, 0)], mark)
	assert app[0]["keyval"] == keyval, app
	ground.expect(keyval, since=mark)
	ground.settle(0.3)
	assert WINDOW_TITLE not in [text for _t, text in ground.speech(mark)]


def run(ground: "Ground") -> None:
	ground.openWindow()
	ground.waitForHelper()
	keyboard = ground.uhidKeyboard(UHID)
	ground.startLinda()
	ground.waitForLindaLog(KERNEL_ROUTE, timeout=30)
	lindaKeyWorks(ground, keyboard)

	with ground.subtest("the helper killed with Shift and the Linda key held"):
		mark = ground.mark()
		keyboard.run({"press": "KEY_LEFTSHIFT"}, {"sleep": 0.05}, {"press": "KEY_INSERT"})
		ground.waitForKeys("x", [("KEY_LEFTSHIFT", 1)], mark, device=VIRTUAL_KEYBOARD)
		ground.settle(0.2)
		killed = ground.now()
		ground.shell("systemctl kill --signal=SIGKILL linda-keyd.service")
		# The kernel destroys the virtual keyboard with the helper, and releases what it held.
		ground.waitForKeys("x", [("KEY_LEFTSHIFT", 1), ("KEY_LEFTSHIFT", 0)], mark, device=VIRTUAL_KEYBOARD)
		ground.waitForLindaLog(X_ROUTE, count=2)
		gone = ground.lindaLogRecords(X_ROUTE)[1][1]
		assert "linda-keyd was given up" in gone, gone
		keyboard.run({"release": "KEY_INSERT"}, {"sleep": 0.05}, {"release": "KEY_LEFTSHIFT"})
		ground.machine.log(f"Linda fell back: {gone.splitlines()[-1]}")
		plainLetter(ground, keyboard, "KEY_U", "u")
		lindaKeyWorks(ground, keyboard)
		back, _record = ground.waitForLindaLog(KERNEL_ROUTE, count=2, timeout=20)
		first = ground.lindaLogRecords(X_ROUTE)[1][0]
		ground.machine.log(f"back on the kernel route {back - first:.3f} s after Linda fell back to X")
		assert ground.helperRestarts() >= 1
		lindaKeyWorks(ground, keyboard)
		plainLetter(ground, keyboard, "KEY_V", "v")
		ground.machine.log(f"killed at {killed:.3f}")

	with ground.subtest("the helper stopped with SIGSTOP"):
		restarts = ground.helperRestarts()
		pid = ground.helperPid()
		mark = ground.mark()
		ground.shell(f"kill -STOP {pid}")
		stopped = ground.now()
		# Typed into a dead keyboard: the keys come once the watchdog has ended the helper.
		written = keyboard.run(
			*[s for _ in range(40) for s in ({"tap": "KEY_W", "hold": 0.02}, {"sleep": 0.1})]
		)
		ground.waitForLindaLog(X_ROUTE, count=3, timeout=10)
		fell = ground.lindaLogRecords(X_ROUTE)[2][1]
		ground.waitFor(lambda: ground.helperRestarts() > restarts, "systemd to restart the helper", 15)
		ground.settle(0.5)
		app = [r for r in ground.keys("app", mark) if r["key"] == "KEY_W" and r["value"] == 1]
		lost = len(written) // 2 - len(app)
		firstThrough = app[0]["t"] - stopped if app else None
		ground.machine.log(
			f"Linda fell back: {fell.splitlines()[-1]}; of {len(written) // 2} W typed over "
			f"{written[-1]['t'] - stopped:.2f} s from the stop, {len(app)} reached the program, the first "
			f"{firstThrough:.3f} s after the stop; {lost} were lost to the stopped helper"
		)
		lindaKeyWorks(ground, keyboard)
		ground.waitForLindaLog(KERNEL_ROUTE, count=3, timeout=20)
		lindaKeyWorks(ground, keyboard)
		keyboard.chord("KEY_LEFTCTRL", "KEY_A")
		plainLetter(ground, keyboard, "KEY_X", "x")
