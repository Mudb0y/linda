"""Consoles switched back and forth many times in quick succession, so that the kernel's word and
logind's on each switch reach the helper in either order: the helper ends holding the keyboard again
by itself, with the client's session in front, and no key typed then reaches the text console.
"""

from typing import TYPE_CHECKING

from ground import VIRTIO_KEYBOARD, VIRTUAL_KEYBOARD

if TYPE_CHECKING:
	from ground import Ground

UHID = "Ground uhid keyboard"
CONSOLE = 2
CONSOLE_TEXT = "/tmp/ground/console.txt"
SWITCHES = 20


def run(ground: "Ground") -> None:
	ground.openWindow()
	ground.waitForHelper()
	keyboard = ground.uhidKeyboard(UHID)
	client = ground.keydClient("main")
	client.capture(UHID, VIRTIO_KEYBOARD)
	desktop = int(ground.shell("fgconsole").strip())
	ground.shell(
		f"openvt -c {CONSOLE} -- sh -c 'stty raw -echo; exec cat > {CONSOLE_TEXT}'; "
		f"until [ -e {CONSOLE_TEXT} ]; do sleep 0.05; done"
	)

	with ground.subtest("switched back and forth, it holds the keyboard again by itself"):
		mark = ground.mark()
		# Pauses from none to a third of a second, shorter and longer than logind takes to follow.
		ground.shell(
			f"for i in $(seq {SWITCHES}); do chvt {CONSOLE}; sleep 0.$((i * 7 % 4)); "
			f"chvt {desktop}; sleep 0.$((i * 3 % 4)); done"
		)
		assert int(ground.shell("fgconsole").strip()) == desktop
		released = [r for r in client.records(mark, "RELEASED")]
		ground.machine.log(f"let go {len(released)} times over {SWITCHES} switches away")
		assert released and all(r["reason"] == "BACKGROUND" for r in released), released
		done = ground.mark()

		def heldAtLast() -> bool:
			said = [
				r
				for r in client.records(mark)
				if r["type"] == "RELEASED" or (r["type"] == "DEVICE" and r["name"] == UHID)
			]
			return bool(said) and said[-1]["type"] == "DEVICE" and said[-1]["status"] == "HELD"

		ground.waitFor(heldAtLast, "the keyboard to be held again", 15)
		ground.machine.log(f"held again within {ground.now() - done.t:.3f} s of the last switch")
		assert not [r for r in client.records(mark) if r["dir"] == "out" and r["type"] == "START"]

	with ground.subtest("what is typed then goes to the window and not to the console"):
		ground.settle(3)
		mark = ground.mark()
		keyboard.tap("KEY_Q", "KEY_Z")
		typed = [("KEY_Q", 1), ("KEY_Q", 0), ("KEY_Z", 1), ("KEY_Z", 0)]
		ground.waitForKeys("x", typed, mark, device=VIRTUAL_KEYBOARD)
		app = ground.waitForKeys("app", typed, mark)
		assert [r["keyval"] for r in app if r["value"] == 1] == ["q", "z"], app
		console = ground.shell(f"cat {CONSOLE_TEXT}")
		assert "q" not in console and "z" not in console, console
