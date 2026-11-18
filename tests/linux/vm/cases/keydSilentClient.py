"""The heartbeat: a client that stops saying anything while it holds a key passed on is dropped two
seconds after its last message, the helper lets go of every keyboard, and the key it held down on the
virtual keyboard is released there, so that nothing is left down in the desktop.
"""

from typing import TYPE_CHECKING

from ground import VIRTUAL_KEYBOARD

if TYPE_CHECKING:
	from ground import Ground

UHID = "Ground uhid keyboard"
#: HEARTBEAT_TIMEOUT in protocol.rs.
SILENCE = 2.0


def run(ground: "Ground") -> None:
	ground.openWindow()
	ground.waitForHelper()
	keyboard = ground.uhidKeyboard(UHID)
	client = ground.keydClient("main")
	client.capture(UHID)

	with ground.subtest("a silent client is dropped and its passed Shift released"):
		mark = ground.mark()
		keyboard.run({"press": "KEY_LEFTSHIFT"})
		ground.waitForKeys("x", [("KEY_LEFTSHIFT", 1)], mark, device=VIRTUAL_KEYBOARD)
		client.heartbeats(False)
		goodbye = client.waitFor("GOODBYE", mark, timeout=SILENCE + 3)
		assert goodbye["reason"] == "SILENT", goodbye
		last = [r for r in client.records(mark) if r["dir"] == "out"][-1]
		# The last thing the client sent was the answer to Shift, or a heartbeat, which is not recorded.
		ground.machine.log(
			f"dropped {goodbye['t'] - last['t']:.3f} s after the client's last recorded message"
		)
		assert goodbye["t"] - last["t"] >= SILENCE - 0.6, (goodbye, last)
		release = ground.waitForKeys(
			"x", [("KEY_LEFTSHIFT", 1), ("KEY_LEFTSHIFT", 0)], mark, device=VIRTUAL_KEYBOARD
		)
		ground.machine.log(
			f"Shift released on the virtual keyboard {release[-1]['t'] - goodbye['t']:+.3f} s from the GOODBYE"
		)
		client.waitFor("CLOSED", mark)

	with ground.subtest("the keyboard is the desktop's again"):
		mark = ground.mark()
		keyboard.run({"release": "KEY_LEFTSHIFT"}, {"tap": "KEY_Q"})
		app = ground.waitForKeys("app", [("KEY_Q", 1), ("KEY_Q", 0)], mark)
		assert app[0]["keyval"] == "q", app
		ground.waitForKeys("x", [("KEY_Q", 1), ("KEY_Q", 0)], mark, device=UHID)
