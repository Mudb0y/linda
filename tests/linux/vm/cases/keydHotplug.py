"""Keyboards that come and go while the helper captures: a USB keyboard plugged in through QEMU's
monitor and a uhid keyboard made in the guest are each found and held, and their keys go to the
client; the USB keyboard unplugged with Shift held down on it, passed on, has Shift released on the
virtual keyboard, and the client is told of the release, made by the helper, and of its going.
"""

from typing import TYPE_CHECKING

from ground import USB_KEYBOARD, VIRTIO_KEYBOARD, VIRTUAL_KEYBOARD

if TYPE_CHECKING:
	from ground import Ground

UHID = "Ground uhid keyboard"


def run(ground: "Ground") -> None:
	ground.openWindow()
	ground.waitForHelper()
	client = ground.keydClient("main")
	client.capture(VIRTIO_KEYBOARD)

	with ground.subtest("a USB keyboard plugged in is held, and has QEMU's keys"):
		mark = ground.mark()
		ground.plugUsbKeyboard()
		usb = client.waitForDevice(USB_KEYBOARD, "HELD", mark)
		said = [line for _t, line in ground.helperLog(mark)]
		ground.machine.log(f"the helper on the new keyboard: {said}")
		mark = ground.mark()
		ground.press("e")
		ground.waitForKeys("x", [("KEY_E", 1), ("KEY_E", 0)], mark, device=VIRTUAL_KEYBOARD)
		keys = client.keys(mark)
		assert {k["device"] for k in keys} == {usb["device"]}, keys

	with ground.subtest("a uhid keyboard made meanwhile is held"):
		mark = ground.mark()
		keyboard = ground.uhidKeyboard(UHID)
		client.waitForDevice(UHID, "HELD", mark)
		mark = ground.mark()
		keyboard.tap("KEY_B")
		ground.waitForKeys("x", [("KEY_B", 1), ("KEY_B", 0)], mark, device=VIRTUAL_KEYBOARD)
		assert not ground.keys("evdev", mark, device=UHID)

	with ground.subtest("the USB keyboard unplugged with Shift held has Shift released"):
		mark = ground.mark()
		ground.qemuKey("shift", True)
		ground.waitForKeys("x", [("KEY_LEFTSHIFT", 1)], mark, device=VIRTUAL_KEYBOARD)
		unplugged = ground.now()
		ground.unplugUsbKeyboard()
		release = ground.waitForKeys(
			"x", [("KEY_LEFTSHIFT", 1), ("KEY_LEFTSHIFT", 0)], mark, device=VIRTUAL_KEYBOARD
		)
		ground.machine.log(
			f"Shift released on the virtual keyboard {release[1]['t'] - unplugged:.3f} s after the unplug"
		)
		gone = client.waitForDevice(USB_KEYBOARD, "GONE", mark)
		releases = [r for r in client.keys(mark) if r["device"] == gone["device"] and r["value"] == 0]
		ground.machine.log(f"the client was told of the release: {releases}")
		assert [r["key"] for r in releases] == ["KEY_LEFTSHIFT"], releases
		ground.qemuKey("shift", False)
		mark = ground.mark()
		keyboard.tap("KEY_Q")
		app = ground.waitForKeys("app", [("KEY_Q", 1), ("KEY_Q", 0)], mark)
		assert app[0]["keyval"] == "q", app
