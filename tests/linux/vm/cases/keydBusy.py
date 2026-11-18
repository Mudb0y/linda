"""A keyboard another program holds: the helper's grab of it fails with EBUSY, the client is told, the
helper holds the others meanwhile, and once the other program lets go the helper takes it within the
two seconds it waits between tries.
"""

from typing import TYPE_CHECKING

from ground import VIRTIO_KEYBOARD, VIRTUAL_KEYBOARD

if TYPE_CHECKING:
	from ground import Ground

UHID = "Ground uhid keyboard"
#: BUSY_RETRY in protocol.rs.
RETRY = 2.0


def run(ground: "Ground") -> None:
	ground.openWindow()
	ground.waitForHelper()
	keyboard = ground.uhidKeyboard(UHID)
	ground.shell(f"systemd-run --collect --unit=ground-grab ground-poke grab '{UHID}'")
	ground.waitFor(
		lambda: "grabbed" in ground.shell("journalctl -o cat -u ground-grab || true"), "the other grab"
	)
	client = ground.keydClient("main")

	with ground.subtest("a keyboard another program holds is busy, and the others are held"):
		mark = ground.mark()
		client.capture(VIRTIO_KEYBOARD, since=mark)
		client.waitForDevice(UHID, "BUSY", mark)
		said = [line for _t, line in ground.helperLog(mark) if "keyboard" in line]
		ground.machine.log(f"the helper said: {said}")
		mark = ground.mark()
		keyboard.tap("KEY_A")
		ground.press("b")
		ground.waitForKeys("x", [("KEY_B", 1), ("KEY_B", 0)], mark, device=VIRTUAL_KEYBOARD)
		ground.settle(0.3)
		# The other program has A; nothing else does.
		assert not [r for r in client.keys(mark) if r["key"] == "KEY_A"]
		for source in ("evdev", "x", "app"):
			assert not [r for r in ground.keys(source, mark) if r["key"] == "KEY_A"], source

	with ground.subtest("once the other program lets go, the helper takes it"):
		mark = ground.mark()
		ground.shell("systemctl stop ground-grab")
		held = client.waitForDevice(UHID, "HELD", mark, timeout=RETRY + 3)
		ground.machine.log(f"held {held['t'] - mark.t:.3f} s after the other program let go")
		assert held["t"] - mark.t < RETRY + 0.5, held
		mark = ground.mark()
		keyboard.tap("KEY_C")
		ground.waitForKeys("x", [("KEY_C", 1), ("KEY_C", 0)], mark, device=VIRTUAL_KEYBOARD)
