"""WAIT, with FEATURE_WAIT granted: a press the client answers WAIT is held past the usual tenth of a
second, and then kept or passed as the client answers, or passed on at the longer bound with the keys
behind it in their order; the emergency chord goes past a press so held.
"""

from typing import TYPE_CHECKING

from ground import VIRTUAL_KEYBOARD

if TYPE_CHECKING:
	from ground import Ground

UHID = "Ground uhid keyboard"
#: FEATURE_WAIT, VERDICT_TIMEOUT and VERDICT_WAIT_TIMEOUT in protocol.rs.
FEATURE_WAIT = 2
BOUND = 0.1
WAIT_BOUND = 0.6


def firstAt(records: list[dict], key: str, value: int) -> dict:
	return next(r for r in records if r["key"] == key and r["value"] == value)


def run(ground: "Ground") -> None:
	ground.openWindow()
	ground.waitForHelper()
	keyboard = ground.uhidKeyboard(UHID)
	client = ground.keydClient("main")
	answer = client.connect(features=FEATURE_WAIT)
	assert answer["welcome"]["features"] == FEATURE_WAIT, answer
	client.capture(UHID)

	with ground.subtest("held by WAIT, then kept"):
		client.verdicts("pass", KEY_A={"wait": True, "after": 0.4, "keep": True})
		mark = ground.mark()
		keyboard.tap("KEY_A")
		kept = client.waitFor("VERDICT", mark, lambda r: r["keep"])
		ground.settle(WAIT_BOUND)
		assert client.records(mark, "WAIT"), client.records(mark)
		assert not client.records(mark, "EXPIRED"), client.records(mark)
		assert not ground.keys("x", mark, device=VIRTUAL_KEYBOARD), ground.keys("x", mark)
		ground.machine.log(f"A kept {kept['t'] - mark.t:.3f} s after the mark, and never passed on")

	with ground.subtest("held by WAIT, then passed"):
		client.verdicts("pass", KEY_B={"wait": True, "after": 0.4, "keep": False})
		mark = ground.mark()
		written = keyboard.tap("KEY_B")
		x = ground.waitForKeys("x", [("KEY_B", 1), ("KEY_B", 0)], mark, device=VIRTUAL_KEYBOARD)
		held = firstAt(x, "KEY_B", 1)["t"] - firstAt(written, "KEY_B", 1)["t"]
		ground.machine.log(f"B passed on {held:.3f} s after it was typed")
		assert 0.35 <= held < WAIT_BOUND, held
		assert not client.records(mark, "EXPIRED"), client.records(mark)

	with ground.subtest("held by WAIT and unanswered, passed at the longer bound with the keys behind it"):
		client.verdicts("pass", KEY_C={"wait": True})
		mark = ground.mark()
		written = keyboard.run(
			{"tap": "KEY_C", "hold": 0.02},
			{"sleep": 0.05},
			{"tap": "KEY_D", "hold": 0.02},
		)
		expected = [("KEY_C", 1), ("KEY_C", 0), ("KEY_D", 1), ("KEY_D", 0)]
		x = ground.waitForKeys("x", expected, mark, device=VIRTUAL_KEYBOARD)
		app = ground.waitForKeys("app", expected, mark)
		assert [r["keyval"] for r in app if r["value"] == 1] == ["c", "d"], app
		held = firstAt(x, "KEY_C", 1)["t"] - firstAt(written, "KEY_C", 1)["t"]
		ground.machine.log(f"C passed on {held:.3f} s after it was typed, D behind it")
		assert WAIT_BOUND <= held < WAIT_BOUND + 0.1, held
		cKey = client.waitFor("KEY", mark, lambda r: r["key"] == "KEY_C" and r["value"] == 1)
		expired = client.waitFor("EXPIRED", mark)
		assert expired["seq"] == cKey["seq"], (expired, cKey)

	with ground.subtest("the emergency chord goes past a press held by WAIT"):
		client.verdicts("pass", KEY_A={"wait": True})
		mark = ground.mark()
		written = keyboard.run(
			{"press": "KEY_A"},
			{"sleep": 0.05},
			{"press": "KEY_BACKSPACE"},
			{"press": "KEY_ESC"},
			{"press": "KEY_ENTER"},
		)
		released = client.waitFor("RELEASED", mark, lambda r: r["reason"] == "EMERGENCY")
		took = released["t"] - firstAt(written, "KEY_ENTER", 1)["t"]
		ground.machine.log(f"let go {took:.3f} s after the chord's last key, A held by WAIT before it")
		assert took < BOUND, took
		keyboard.run(
			{"release": "KEY_ENTER"},
			{"release": "KEY_ESC"},
			{"release": "KEY_BACKSPACE"},
			{"release": "KEY_A"},
		)
		ground.settle(WAIT_BOUND)
		assert not [r for r in ground.keys("x", mark, device=VIRTUAL_KEYBOARD) if r["value"] == 1]
