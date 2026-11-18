"""A keyboard made through uinput, as a remapper such as keyd makes its own, is left alone: the helper
never grabs it, so that two such programs cannot feed each other's keys round for ever. The client is
told so, and its keys reach the desktop straight from it, without the client hearing them.
"""

from typing import TYPE_CHECKING

from ground import VIRTIO_KEYBOARD

if TYPE_CHECKING:
	from ground import Ground

FAKE = "Ground fake keyboard"


def run(ground: "Ground") -> None:
	ground.openWindow()
	ground.waitForHelper()
	fake = ground.keyboard(FAKE)
	client = ground.keydClient("main")

	with ground.subtest("a uinput keyboard is left alone and its keys reach the desktop"):
		mark = ground.mark()
		client.capture(VIRTIO_KEYBOARD, since=mark)
		ignored = client.waitForDevice(FAKE, "IGNORED", mark)
		assert ignored["ignored"] == "VIRTUAL", ignored
		mark = ground.mark()
		fake.tap("KEY_A")
		ground.waitForKeys("evdev", [("KEY_A", 1), ("KEY_A", 0)], mark, device=FAKE)
		ground.waitForKeys("x", [("KEY_A", 1), ("KEY_A", 0)], mark, device=FAKE)
		app = ground.waitForKeys("app", [("KEY_A", 1), ("KEY_A", 0)], mark)
		assert app[0]["keyval"] == "a", app
		assert not client.keys(mark), client.keys(mark)
