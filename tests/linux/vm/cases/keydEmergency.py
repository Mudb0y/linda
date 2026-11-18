"""The emergency chord, Backspace, Escape and Enter held together, with the client hung (stopped by
SIGSTOP): the helper reads it itself and lets go of every keyboard at once, the presses still waiting
for the hung client's verdict are dropped rather than typed, and the helper does not capture again
until it is asked. On a uhid keyboard, at exact intervals, and on QEMU's own keyboard.
"""

from typing import TYPE_CHECKING

from ground import VIRTIO_KEYBOARD

if TYPE_CHECKING:
	from ground import Ground

UHID = "Ground uhid keyboard"
CHORD = ["KEY_BACKSPACE", "KEY_ESC", "KEY_ENTER"]


def run(ground: "Ground") -> None:
	ground.openWindow()
	ground.waitForHelper()
	keyboard = ground.uhidKeyboard(UHID)
	client = ground.keydClient("main")
	client.capture(UHID, VIRTIO_KEYBOARD)

	with ground.subtest("the chord on a held keyboard lets go while the client is hung"):
		ground.session({"op": "signal", "name": "keyd-main", "signal": "SIGSTOP"})
		mark = ground.mark()
		written = keyboard.run(
			{"press": "KEY_BACKSPACE"},
			{"sleep": 0.02},
			{"press": "KEY_ESC"},
			{"sleep": 0.02},
			{"press": "KEY_ENTER"},
			{"sleep": 0.1},
			{"release": "KEY_ENTER"},
			{"release": "KEY_ESC"},
			{"release": "KEY_BACKSPACE"},
			{"sleep": 0.05},
			{"tap": "KEY_Q"},
		)
		said = ground.waitForHelperLog("the emergency chord", mark)
		enter = next(r for r in written if r["key"] == "KEY_ENTER")
		ground.machine.log(
			f"the helper said so {said[0] - enter['t']:+.3f} s from Enter, as the journal stamps it"
		)
		# Q, typed after the chord, reaches the program from the keyboard itself.
		ground.waitForKeys("x", [("KEY_Q", 1), ("KEY_Q", 0)], mark, device=UHID)
		app = ground.waitForKeys("app", [("KEY_Q", 1), ("KEY_Q", 0)], mark)
		assert app[0]["keyval"] == "q", app
		# Backspace and Escape were waiting for the hung client when Enter came: dropped, never typed.
		typed = [r["key"] for source in ("x", "app") for r in ground.keys(source, mark) if r["key"] in CHORD]
		assert not typed, typed
		ground.session({"op": "signal", "name": "keyd-main", "signal": "SIGCONT"})
		released = client.waitFor("RELEASED", mark)
		assert released["reason"] == "EMERGENCY", released

	with ground.subtest("nothing is captured again until the client asks"):
		ground.settle(2.5)
		mark = ground.mark()
		keyboard.tap("KEY_W")
		ground.press("e")
		ground.waitForKeys("x", [("KEY_W", 1), ("KEY_W", 0)], mark, device=UHID)
		ground.waitForKeys("x", [("KEY_E", 1), ("KEY_E", 0)], mark, device=VIRTIO_KEYBOARD)
		assert not client.keys(mark), client.keys(mark)
		assert client.call("state")["connected"], "the chord dropped the client"

	with ground.subtest("the chord on QEMU's own keyboard"):
		mark = ground.mark()
		client.capture(UHID, VIRTIO_KEYBOARD, since=mark)
		ground.session({"op": "signal", "name": "keyd-main", "signal": "SIGSTOP"})
		mark = ground.mark()
		ground.press("backspace-esc-ret")
		ground.waitForHelperLog("the emergency chord", mark)
		mark = ground.mark()
		ground.press("r")
		ground.waitForKeys("x", [("KEY_R", 1), ("KEY_R", 0)], mark, device=VIRTIO_KEYBOARD)
		ground.session({"op": "signal", "name": "keyd-main", "signal": "SIGCONT"})
		client.waitFor("RELEASED", mark, lambda r: r["reason"] == "EMERGENCY")
