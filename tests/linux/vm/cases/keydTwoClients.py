"""Two clients of the session in front: the second is turned away as busy while the first is served,
the first goes on capturing undisturbed, and once the first has gone the second is welcomed.
"""

from typing import TYPE_CHECKING

from ground import VIRTUAL_KEYBOARD

if TYPE_CHECKING:
	from ground import Ground

UHID = "Ground uhid keyboard"


def run(ground: "Ground") -> None:
	ground.openWindow()
	ground.waitForHelper()
	keyboard = ground.uhidKeyboard(UHID)
	first = ground.keydClient("first")
	second = ground.keydClient("second")
	first.capture(UHID)

	with ground.subtest("a second client is busy, and the first goes on"):
		answer = second.connect()
		assert answer["goodbye"] and answer["goodbye"]["reason"] == "BUSY", answer
		ground.machine.log(f"the second: {answer['goodbye']['text']!r}")
		mark = ground.mark()
		keyboard.tap("KEY_A")
		ground.waitForKeys("x", [("KEY_A", 1), ("KEY_A", 0)], mark, device=VIRTUAL_KEYBOARD)
		assert [r["key"] for r in first.keys(mark)] == ["KEY_A", "KEY_A"]
		assert not second.keys(mark)

	with ground.subtest("once the first has gone, the second is welcomed and captures"):
		mark = ground.mark()
		first.call("close")
		# At once, as the connection closes, not after the silence a client is dropped for.
		gone = ground.waitForHelperLog("has gone", mark, timeout=1)
		ground.machine.log(f"the helper heard the first client go {gone[0] - mark.t:.3f} s after the mark")
		ground.waitForHelperLog("let go of every keyboard", mark, timeout=1)
		second.capture(UHID, since=mark)
		mark = ground.mark()
		keyboard.tap("KEY_B")
		ground.waitForKeys("x", [("KEY_B", 1), ("KEY_B", 0)], mark, device=VIRTUAL_KEYBOARD)
		assert [r["key"] for r in second.keys(mark)] == ["KEY_B", "KEY_B"]
