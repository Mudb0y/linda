"""The grab: from START the helper holds every keyboard of the seat, the guest's two and one plugged
in through uhid, and nothing of theirs reaches anyone else; what the client passes reaches the
program through the virtual keyboard alone, and what it keeps reaches nobody. A keyboard with a key
down is grabbed only once that key is up, and STOP lets go of all of them.
"""

from typing import TYPE_CHECKING

from ground import AT_KEYBOARD, VIRTIO_KEYBOARD, VIRTUAL_KEYBOARD

if TYPE_CHECKING:
	from ground import Ground

UHID = "Ground uhid keyboard"


def run(ground: "Ground") -> None:
	ground.openWindow()
	ground.waitForHelper()
	keyboard = ground.uhidKeyboard(UHID)
	client = ground.keydClient("main")

	with ground.subtest("before START nothing is grabbed"):
		assert client.connect()["welcome"]
		mark = ground.mark()
		keyboard.tap("KEY_A")
		ground.waitForKeys("evdev", [("KEY_A", 1), ("KEY_A", 0)], mark, device=UHID)
		ground.waitForKeys("x", [("KEY_A", 1), ("KEY_A", 0)], mark, device=UHID)
		ground.settle(0.3)
		assert not client.keys(mark), client.keys(mark)

	with ground.subtest("START holds every keyboard of the seat"):
		mark = ground.mark()
		client.capture(UHID, VIRTIO_KEYBOARD, AT_KEYBOARD, since=mark)

	with ground.subtest("a passed key reaches the program through the virtual keyboard alone"):
		for name, press in (
			(UHID, lambda: keyboard.tap("KEY_B")),
			(VIRTIO_KEYBOARD, lambda: ground.press("b")),
		):
			mark = ground.mark()
			press()
			typed = [("KEY_B", 1), ("KEY_B", 0)]
			ground.waitForKeys("evdev", typed, mark, device=VIRTUAL_KEYBOARD)
			ground.waitForKeys("x", typed, mark, device=VIRTUAL_KEYBOARD)
			app = ground.waitForKeys("app", typed, mark)
			assert app[0]["keyval"] == "b", app
			keys = client.keys(mark)
			assert [(k["key"], k["value"], k["ask"]) for k in keys] == [
				("KEY_B", 1, True),
				("KEY_B", 0, False),
			], keys
			ground.settle(0.3)
			# Nothing from the keyboard itself, to any reader or to the X server.
			assert not ground.keys("evdev", mark, device=name), name
			assert not ground.keys("x", mark, device=name), name
			ground.machine.log(f"{name}'s B went through the virtual keyboard")

	with ground.subtest("a kept key reaches nobody but the client"):
		client.verdicts("pass", KEY_K="keep")
		mark = ground.mark()
		keyboard.tap("KEY_K")
		client.waitFor("KEY", mark, lambda r: r["key"] == "KEY_K" and r["value"] == 0)
		ground.settle(0.5)
		for source in ("evdev", "x", "app"):
			assert not ground.keys(source, mark), (source, ground.keys(source, mark))
		client.verdicts("pass")

	with ground.subtest("a keyboard with a key down is grabbed only once the key is up"):
		mark = ground.mark()
		client.stop()
		client.waitFor("RELEASED", mark, lambda r: r["reason"] == "STOPPED")
		keyboard.run({"press": "KEY_LEFTSHIFT"})
		ground.waitForKeys("x", [("KEY_LEFTSHIFT", 1)], mark, device=UHID)
		mark = ground.mark()
		client.start()
		client.waitForDevice(UHID, "WAITING", mark)
		client.waitForDevice(VIRTIO_KEYBOARD, "HELD", mark)
		ground.settle(0.5)
		held = [r for r in client.records(mark, "DEVICE") if r["name"] == UHID and r["status"] == "HELD"]
		assert not held, held
		release = ground.mark()
		keyboard.run({"release": "KEY_LEFTSHIFT"})
		# The release reaches the X server from the keyboard, which saw the press, so no Shift is left
		# down there.
		ground.waitForKeys("x", [("KEY_LEFTSHIFT", 0)], release, device=UHID)
		client.waitForDevice(UHID, "HELD", release)
		mark = ground.mark()
		keyboard.tap("KEY_X")
		app = ground.waitForKeys("app", [("KEY_X", 1), ("KEY_X", 0)], mark)
		assert app[0]["keyval"] == "x", app

	with ground.subtest("STOP lets go of every keyboard"):
		mark = ground.mark()
		client.stop()
		client.waitFor("RELEASED", mark, lambda r: r["reason"] == "STOPPED")
		mark = ground.mark()
		keyboard.tap("KEY_C")
		ground.press("d")
		ground.waitForKeys("x", [("KEY_C", 1), ("KEY_C", 0)], mark, device=UHID)
		ground.waitForKeys("x", [("KEY_D", 1), ("KEY_D", 0)], mark, device=VIRTIO_KEYBOARD)
		assert not client.keys(mark)
