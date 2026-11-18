"""A keyboard that vanishes in the middle of a chord, as a Bluetooth keyboard does when it goes out of
range: a uhid keyboard destroyed with Shift passed on and Control kept, both held. The helper puts a
release of each in line, so Shift is released on the virtual keyboard and Control, which the desktop
never had, stays unseen; the client is told of both releases, made by the helper, and of the
keyboard's going. A Shift held on another keyboard at the same time stays down until that keyboard
lets it go.
"""

from typing import TYPE_CHECKING

from ground import VIRTIO_KEYBOARD, VIRTUAL_KEYBOARD

if TYPE_CHECKING:
	from ground import Ground

UHID = "Ground uhid keyboard"


def run(ground: "Ground") -> None:
	ground.openWindow()
	ground.waitForHelper()
	keyboard = ground.uhidKeyboard(UHID)
	client = ground.keydClient("main")
	client.capture(UHID, VIRTIO_KEYBOARD)
	client.verdicts("pass", KEY_LEFTCTRL="keep")

	with ground.subtest("a keyboard that vanishes with Shift passed and Control kept"):
		mark = ground.mark()
		keyboard.run({"press": "KEY_LEFTSHIFT"}, {"sleep": 0.05}, {"press": "KEY_LEFTCTRL"})
		client.waitFor("VERDICT", mark, lambda r: r["keep"])
		ground.waitForKeys("x", [("KEY_LEFTSHIFT", 1)], mark, device=VIRTUAL_KEYBOARD)
		vanished = ground.now()
		keyboard.vanish()
		release = ground.waitForKeys(
			"x", [("KEY_LEFTSHIFT", 1), ("KEY_LEFTSHIFT", 0)], mark, device=VIRTUAL_KEYBOARD
		)
		ground.machine.log(
			f"Shift released on the virtual keyboard {release[1]['t'] - vanished:.3f} s after the keyboard went"
		)
		gone = client.waitForDevice(UHID, "GONE", mark)
		releases = [r for r in client.keys(mark) if r["value"] == 0 and r["device"] == gone["device"]]
		ground.machine.log(f"the client was told: {[(r['key'], r['synthetic']) for r in releases]}")
		assert sorted(r["key"] for r in releases) == ["KEY_LEFTCTRL", "KEY_LEFTSHIFT"], releases
		controls = [
			r for source in ("x", "app") for r in ground.keys(source, mark) if r["key"] == "KEY_LEFTCTRL"
		]
		assert not controls, controls

	with ground.subtest("typing on another keyboard afterwards has nothing held down"):
		mark = ground.mark()
		ground.press("q")
		app = ground.waitForKeys("app", [("KEY_Q", 1), ("KEY_Q", 0)], mark)
		assert app[0]["keyval"] == "q", app

	with ground.subtest("a Shift held on two keyboards stays down until both let it go"):
		second = ground.uhidKeyboard("Ground second uhid keyboard")
		client.waitForDevice(second.name, "HELD")
		virtio = client.waitForDevice(VIRTIO_KEYBOARD, "HELD")["device"]
		mark = ground.mark()
		second.run({"press": "KEY_LEFTSHIFT"})
		ground.qemuKey("shift", True)
		client.waitFor("KEY", mark, lambda r: r["key"] == "KEY_LEFTSHIFT" and r["device"] == virtio)
		ground.waitForKeys("x", [("KEY_LEFTSHIFT", 1)], mark, device=VIRTUAL_KEYBOARD)
		mark = ground.mark()
		second.vanish()
		ground.press("w")
		app = ground.waitForKeys("app", [("KEY_W", 1), ("KEY_W", 0)], mark)
		assert app[0]["keyval"] == "W", app
		ground.qemuKey("shift", False)
		ground.waitForKeys(
			"x", [("KEY_W", 1), ("KEY_W", 0), ("KEY_LEFTSHIFT", 0)], mark, device=VIRTUAL_KEYBOARD
		)
		mark = ground.mark()
		ground.press("e")
		app = ground.waitForKeys("app", [("KEY_E", 1), ("KEY_E", 0)], mark)
		assert app[0]["keyval"] == "e", app
