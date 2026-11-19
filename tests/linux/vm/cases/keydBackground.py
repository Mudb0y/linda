"""The client's session going to the background: a switch to a text console lets go of every
keyboard as the kernel makes it, so that what is typed there, a password perhaps, reaches the console
and not the client, and the switch back captures again by itself, without the client asking, once
logind says the client's session is in front again.

Keys are typed on a uhid keyboard every 60 ms from before the switch to well after it, and a reader
on the text console records what reaches it, so that the window in which keys still went to the
client is measured, and where those keys went.
"""

import json
import string
from typing import TYPE_CHECKING

from ground import VIRTIO_KEYBOARD, VIRTUAL_KEYBOARD

if TYPE_CHECKING:
	from ground import Ground

UHID = "Ground uhid keyboard"
GAP = {"sleep": 0.05}
CONSOLE = 2
CONSOLE_TEXT = "/tmp/ground/console.txt"
LETTERS = string.ascii_lowercase


def run(ground: "Ground") -> None:
	ground.openWindow()
	ground.waitForHelper()
	keyboard = ground.uhidKeyboard(UHID)
	client = ground.keydClient("main")
	client.capture(UHID, VIRTIO_KEYBOARD)
	desktop = int(ground.shell("fgconsole").strip())
	ground.machine.log(f"the X session is on console {desktop}")
	# What reaches the text console, raw, as a program reading it there would have it.
	ground.shell(
		f"openvt -c {CONSOLE} -- sh -c 'stty raw -echo; exec cat > {CONSOLE_TEXT}'; "
		f"until [ -e {CONSOLE_TEXT} ]; do sleep 0.05; done"
	)

	with ground.subtest("a switch to a text console lets go as the kernel makes it"):
		mark = ground.mark()
		steps = json.dumps([s for c in LETTERS for s in ({"tap": f"KEY_{c.upper()}", "hold": 0.01}, GAP)])
		switched = float(
			ground.shell(
				f"ground-keyboard call '{UHID}' '{steps}' > /tmp/ground/typed.json & "
				f"sleep 0.6; ground-record now; chvt {CONSOLE}; wait"
			).split()[0]
		)
		written = json.loads(ground.shell("cat /tmp/ground/typed.json"))["written"]
		released = client.waitFor("RELEASED", mark)
		assert released["reason"] == "BACKGROUND", released
		ground.settle(1)
		heard = {r["key"] for r in client.keys(mark) if r["value"] == 1}
		console = ground.shell(f"cat {CONSOLE_TEXT}")
		presses = [r for r in written if r["value"] == 1]
		lines = []
		for r in presses:
			letter = r["key"][4:].lower()
			where = [
				name for name, got in (("client", r["key"] in heard), ("console", letter in console)) if got
			]
			lines.append(f"{letter} {r['t'] - switched:+.3f} s: {', '.join(where) or 'nowhere'}")
		ground.machine.log("each key, by when it was typed from the switch:\n" + "\n".join(lines))
		lastHeard = max((r["t"] for r in presses if r["key"] in heard), default=None)
		ground.machine.log(
			f"let go {released['t'] - switched:.3f} s after the switch began; the last key the client "
			f"heard was typed {lastHeard - switched:+.3f} s from it"
		)
		kernel = ground.waitForHelperLog(f"console {CONSOLE} is in front", mark)[0]
		logind = ground.waitForHelperLog("in front on seat0", mark)[0]
		ground.machine.log(
			f"the helper heard of the switch from the kernel {kernel - switched:+.3f} s and from logind "
			f"{logind - switched:+.3f} s after it began, and the client was told it was let go "
			f"{released['t'] - switched:+.3f} s after it began"
		)
		assert released["t"] < logind, (released, logind)
		# Every key typed once the switch was under way went to the console.
		after = [r["key"][4:].lower() for r in presses if r["t"] > switched + 0.15]
		assert after and all(letter in console for letter in after), (after, console)
		assert released["t"] - switched < 0.15, released
		assert not [r for r in presses if r["key"] in heard and r["t"] > switched + 0.15]

	with ground.subtest("the switch back captures again by itself"):
		mark = ground.mark()
		ground.shell(f"chvt {desktop}")
		held = client.waitForDevice(UHID, "HELD", mark)
		client.waitForDevice(VIRTIO_KEYBOARD, "HELD", mark)
		ground.machine.log(f"held again {held['t'] - mark.t:.3f} s after the switch back")
		assert not [r for r in client.records(mark) if r["dir"] == "out" and r["type"] == "START"]
		mark = ground.mark()
		keyboard.tap("KEY_B")
		app = ground.waitForKeys("app", [("KEY_B", 1), ("KEY_B", 0)], mark)
		assert app[0]["keyval"] == "b", app
		ground.waitForKeys("x", [("KEY_B", 1), ("KEY_B", 0)], mark, device=VIRTUAL_KEYBOARD)
