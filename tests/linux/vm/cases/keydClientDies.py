"""A client that dies in the middle of a chord, with Shift passed on and Insert kept, both held down:
the helper lets go at once, Shift is released on the virtual keyboard, Insert never reaches the
desktop, and the keyboard is the desktop's again with nothing stuck down.
"""

from typing import TYPE_CHECKING

from ground import VIRTUAL_KEYBOARD

if TYPE_CHECKING:
	from ground import Ground

UHID = "Ground uhid keyboard"


def run(ground: "Ground") -> None:
	ground.openWindow()
	ground.waitForHelper()
	keyboard = ground.uhidKeyboard(UHID)
	client = ground.keydClient("main")
	client.capture(UHID)
	client.verdicts("pass", KEY_INSERT="keep")

	with ground.subtest("the client dies holding Shift passed and Insert kept"):
		mark = ground.mark()
		keyboard.run({"press": "KEY_LEFTSHIFT"}, {"sleep": 0.05}, {"press": "KEY_INSERT"})
		client.waitFor("VERDICT", mark, lambda r: r["keep"])
		ground.waitForKeys("x", [("KEY_LEFTSHIFT", 1)], mark, device=VIRTUAL_KEYBOARD)
		died = ground.now()
		ground.session({"op": "signal", "name": "keyd-main", "signal": "SIGKILL"})
		ground.waitFor(lambda: not ground.running("keyd-main"), "the client to die")
		release = ground.waitForKeys(
			"x", [("KEY_LEFTSHIFT", 1), ("KEY_LEFTSHIFT", 0)], mark, device=VIRTUAL_KEYBOARD
		)
		ground.machine.log(
			f"Shift released on the virtual keyboard {release[-1]['t'] - died:.3f} s after the kill"
		)
		ground.waitForHelperLog("the client, process", mark)
		inserts = [
			r for source in ("x", "app") for r in ground.keys(source, mark) if r["key"] == "KEY_INSERT"
		]
		assert not inserts, inserts

	with ground.subtest("the keyboard is the desktop's again, with nothing down"):
		mark = ground.mark()
		keyboard.run({"release": "KEY_INSERT"}, {"release": "KEY_LEFTSHIFT"}, {"tap": "KEY_Q"})
		app = ground.waitForKeys("app", [("KEY_Q", 1), ("KEY_Q", 0)], mark)
		assert app[0]["keyval"] == "q", app
		ground.waitForKeys("x", [("KEY_Q", 1), ("KEY_Q", 0)], mark, device=UHID)
		# The releases of keys the X server never saw pressed on this keyboard do not reach programs.
		assert [r["key"] for r in app] == ["KEY_Q", "KEY_Q"], app
