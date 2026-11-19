"""The start order's first rule: when the virtual keyboard cannot be made, here because the helper
started without access to /dev/uinput, nothing is grabbed at all; the client is told, and the
keyboards stay the desktop's. With the access back, a new START makes it and captures.

The unit has systemd hand the helper /dev/uinput opened, so the helper is started again with a
drop-in that hands it nothing, and it opens the node itself, which its user may not.
"""

from typing import TYPE_CHECKING

from ground import VIRTIO_KEYBOARD, VIRTUAL_KEYBOARD

if TYPE_CHECKING:
	from ground import Ground

UHID = "Ground uhid keyboard"


def run(ground: "Ground") -> None:
	ground.openWindow()
	pid = ground.waitForHelper()
	ground.shell(
		"mkdir -p /run/systemd/system/linda-keyd.service.d && "
		"printf '[Service]\\nStandardInput=null\\nStandardOutput=journal\\n' "
		"> /run/systemd/system/linda-keyd.service.d/no-uinput.conf && "
		"systemctl daemon-reload && systemctl restart linda-keyd"
	)
	ground.waitFor(lambda: ground.helperPid() not in (0, pid), "the helper to start again")
	ground.waitForHelperLog("cannot open /dev/uinput")
	keyboard = ground.uhidKeyboard(UHID)
	client = ground.keydClient("main")
	assert client.connect()["welcome"]

	with ground.subtest("without the virtual keyboard nothing is grabbed"):
		mark = ground.mark()
		client.start()
		released = client.waitFor("RELEASED", mark)
		assert released["reason"] == "VIRTUAL_KEYBOARD", released
		said = ground.waitForHelperLog("could not make the virtual keyboard", mark)[1]
		ground.machine.log(f"the helper: {said}")
		ground.settle(1)
		assert not client.records(mark, "DEVICE"), client.records(mark, "DEVICE")
		assert "capturing" not in " ".join(line for _t, line in ground.helperLog(mark))
		mark = ground.mark()
		keyboard.tap("KEY_A")
		ground.press("b")
		ground.waitForKeys("x", [("KEY_A", 1), ("KEY_A", 0)], mark, device=UHID)
		ground.waitForKeys("x", [("KEY_B", 1), ("KEY_B", 0)], mark, device=VIRTIO_KEYBOARD)
		assert not client.keys(mark)

	with ground.subtest("with uinput back, START captures"):
		ground.shell("setfacl -m u:linda-keyd:rw /dev/uinput")
		mark = ground.mark()
		client.capture(UHID, since=mark)
		mark = ground.mark()
		keyboard.tap("KEY_C")
		ground.waitForKeys("x", [("KEY_C", 1), ("KEY_C", 0)], mark, device=VIRTUAL_KEYBOARD)
