"""A keyboard with a consumer control collection, as many USB keyboards have, which the kernel's HID
layer puts on the keyboard's own node: its horizontal wheel is REL_HWHEEL with REL_HWHEEL_HI_RES,
its volume ABS_VOLUME, and its Function Buttons BTN_0. udev calls it a keyboard and nothing else. The
helper takes it: its keys go to the client and, passed, reach the program through the virtual
keyboard, and its consumer controls reach readers of the virtual keyboard alone, the volume in the
virtual keyboard's own range, without the client being told of them.
"""

import shlex
from typing import TYPE_CHECKING

from ground import GROUND, VIRTUAL_KEYBOARD

if TYPE_CHECKING:
	from ground import Ground

#: Ending in "Keyboard", so that the kernel, which names each application of a HID device after the
#: device with the application's name added (hidinput_allocate), adds nothing to it.
UHID = "Ground Consumer Keyboard"
#: Consumer page usages: Function Buttons, Volume and AC Pan.
FUNCTION_BUTTONS = 0x036
VOLUME = 0x0E0
AC_PAN = 0x238
BTN_0 = 0x100
REL_HWHEEL = 0x06
REL_HWHEEL_HI_RES = 0x0C
ABS_VOLUME = 0x20


#: The ID_INPUT_* properties udev gives a keyboard and nothing else.
KEYBOARD_ALONE = ["ID_INPUT_KEY=1", "ID_INPUT_KEYBOARD=1"]


def inputKinds(ground: "Ground", node: str) -> list[str]:
	properties = ground.shell(f"udevadm info --query=property --name={node}").split()
	return sorted(p for p in properties if p.startswith("ID_INPUT_") and p.endswith("=1"))


def axes(ground: "Ground", since: object, device: str) -> list[tuple[str, int, int]]:
	return [
		(r["type"], r["code"], r["value"])
		for r in ground.records("evdev", since)
		if r["kind"] == "axis" and r["device"] == device
	]


def buttons(ground: "Ground", since: object, device: str) -> list[tuple[int, int]]:
	return [(r["code"], r["value"]) for r in ground.keys("evdev", since, device=device) if r["code"] == BTN_0]


def run(ground: "Ground") -> None:
	ground.openWindow()
	ground.waitForHelper()
	keyboard = ground.keyboard(UHID, "uhidConsumer")
	client = ground.keydClient("main")

	with ground.subtest("the keyboard's node has the consumer controls, and is a keyboard alone to udev"):
		node = ground.shell(f"cat {shlex.quote(f'{GROUND}/keyboards/{UHID}.ready')}").strip()
		event = node.rsplit("/", 1)[1]
		capabilities = {
			name: ground.shell(f"cat /sys/class/input/{event}/device/capabilities/{name}").strip()
			for name in ("rel", "abs", "key")
		}
		ground.machine.log(f"the consumer keyboard's capabilities: {capabilities}")
		assert capabilities["rel"] == "1040", capabilities
		assert capabilities["abs"] == "100000000", capabilities
		# BTN_0, bit 0 of the fifth 64-bit word.
		assert int(capabilities["key"].split()[-5], 16) & 1, capabilities
		assert inputKinds(ground, node) == KEYBOARD_ALONE

	with ground.subtest(
		"START holds it, and the virtual keyboard with its consumer controls is a keyboard alone"
	):
		mark = ground.mark()
		client.capture(UHID, since=mark)
		ignored = [r for r in client.records(mark, "DEVICE") if r["status"] == "IGNORED"]
		assert not ignored, ignored
		named = ground.shell(f"grep -lx {shlex.quote(VIRTUAL_KEYBOARD)} /sys/class/input/event*/device/name")
		virtual = "/dev/input/" + named.split()[0].split("/")[4]
		# Not a tablet pad, as BTN_0 and BTN_1 with a wheel would make it, nor a pointer.
		assert inputKinds(ground, virtual) == KEYBOARD_ALONE

	with ground.subtest("its keys go to the client and, passed, reach the program"):
		mark = ground.mark()
		keyboard.tap("KEY_B")
		typed = [("KEY_B", 1), ("KEY_B", 0)]
		ground.waitForKeys("evdev", typed, mark, device=VIRTUAL_KEYBOARD)
		app = ground.waitForKeys("app", typed, mark)
		assert app[0]["keyval"] == "b", app
		keys = client.keys(mark)
		assert [(k["key"], k["value"], k["ask"]) for k in keys] == [
			("KEY_B", 1, True),
			("KEY_B", 0, False),
		], keys
		ground.settle(0.3)
		assert not ground.keys("evdev", mark, device=UHID)

	with ground.subtest("its consumer controls reach the virtual keyboard alone, and the client is not told"):
		mark = ground.mark()
		keyboard.run(
			{"consumer": FUNCTION_BUTTONS, "hold": 0.05},
			{"consumer": AC_PAN},
			{"consumer": VOLUME},
		)
		ground.waitFor(
			lambda: buttons(ground, mark, VIRTUAL_KEYBOARD) == [(BTN_0, 1), (BTN_0, 0)],
			"BTN_0 on the virtual keyboard",
		)
		# Volume 1 of 0 to 0xFFF is 16 of the virtual keyboard's 0 to 0xFFFF.
		expected = [
			("rel", REL_HWHEEL, 1),
			("rel", REL_HWHEEL_HI_RES, 120),
			("abs", ABS_VOLUME, 16),
			("abs", ABS_VOLUME, 0),
		]
		ground.waitFor(
			lambda: axes(ground, mark, VIRTUAL_KEYBOARD) == expected, "the axes on the virtual keyboard"
		)
		ground.settle(0.3)
		assert not client.keys(mark), client.keys(mark)
		assert not axes(ground, mark, UHID) and not buttons(ground, mark, UHID)

	with ground.subtest("STOP gives it back"):
		mark = ground.mark()
		client.stop()
		client.waitFor("RELEASED", mark, lambda r: r["reason"] == "STOPPED")
		mark = ground.mark()
		keyboard.run({"consumer": FUNCTION_BUTTONS})
		ground.waitFor(
			lambda: buttons(ground, mark, UHID) == [(BTN_0, 1), (BTN_0, 0)], "BTN_0 on the keyboard"
		)
		assert not buttons(ground, mark, VIRTUAL_KEYBOARD)
