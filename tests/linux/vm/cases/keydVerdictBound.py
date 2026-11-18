"""The bound on each key's verdict: a press the client does not answer is passed on a tenth of a second
after it was read, the keys behind it wait for it and keep their order, a verdict that comes later
changes nothing, and a client that leaves presses unanswered through a second of typing is dropped,
while one that misses two presses more than a second apart is not.
"""

from typing import TYPE_CHECKING

from ground import VIRTUAL_KEYBOARD

if TYPE_CHECKING:
	from ground import Ground

UHID = "Ground uhid keyboard"
#: VERDICT_TIMEOUT in protocol.rs.
BOUND = 0.1


def firstAt(records: list[dict], key: str, value: int) -> dict:
	return next(r for r in records if r["key"] == key and r["value"] == value)


def run(ground: "Ground") -> None:
	ground.openWindow()
	ground.waitForHelper()
	keyboard = ground.uhidKeyboard(UHID)
	client = ground.keydClient("main")
	client.capture(UHID)

	with ground.subtest("an unanswered press goes out after the bound, and the key behind it waits"):
		client.verdicts("pass", KEY_A="never")
		mark = ground.mark()
		written = keyboard.run(
			{"tap": "KEY_A", "hold": 0.02},
			{"sleep": 0.01},
			{"tap": "KEY_B", "hold": 0.02},
		)
		expected = [("KEY_A", 1), ("KEY_A", 0), ("KEY_B", 1), ("KEY_B", 0)]
		evdev = ground.waitForKeys("evdev", expected, mark, device=VIRTUAL_KEYBOARD)
		ground.waitForKeys("x", expected, mark, device=VIRTUAL_KEYBOARD)
		app = ground.waitForKeys("app", expected, mark)
		assert [r["keyval"] for r in app if r["value"] == 1] == ["a", "b"], app
		aWritten, bWritten = firstAt(written, "KEY_A", 1), firstAt(written, "KEY_B", 1)
		aOut, bOut = firstAt(evdev, "KEY_A", 1), firstAt(evdev, "KEY_B", 1)
		aKey = client.waitFor("KEY", mark, lambda r: r["key"] == "KEY_A" and r["value"] == 1)
		bKey = client.waitFor("KEY", mark, lambda r: r["key"] == "KEY_B" and r["value"] == 1)
		expired = client.waitFor("EXPIRED", mark)
		bVerdict = client.waitFor("VERDICT", mark, lambda r: r["seq"] == bKey["seq"])
		assert expired["seq"] == aKey["seq"], (expired, aKey)
		waited = aOut["t"] - aWritten["t"]
		ground.machine.log(
			f"A unanswered went out {waited * 1000:.1f} ms after it was written; B, answered "
			f"{(bVerdict['t'] - bKey['t']) * 1000:.2f} ms after the client had it, went out "
			f"{(bOut['t'] - bWritten['t']) * 1000:.1f} ms after it was written, behind A"
		)
		assert BOUND <= waited < BOUND + 0.05, waited
		assert bOut["t"] >= aOut["t"] and bOut["t"] - bWritten["t"] > 0.05, (aOut, bOut, bWritten)

	with ground.subtest("a verdict after the bound changes nothing"):
		client.verdicts("pass", KEY_C={"after": 0.3, "keep": True})
		mark = ground.mark()
		keyboard.tap("KEY_C")
		app = ground.waitForKeys("app", [("KEY_C", 1), ("KEY_C", 0)], mark)
		client.waitFor("EXPIRED", mark)
		late = client.waitFor("VERDICT", mark, lambda r: r["keep"], timeout=2)
		ground.settle(0.3)
		assert client.call("state")["connected"], "a late verdict dropped the client"
		ground.machine.log(f"C went to the program; its verdict to keep it came at {late['t']:.3f}, too late")

	with ground.subtest("unanswered presses more than a second apart are separate, and keep the client"):
		# C went unanswered above; F, read well over a second later, starts a run of its own.
		client.verdicts("never")
		ground.settle(1.2)
		mark = ground.mark()
		keyboard.tap("KEY_F")
		client.waitFor("EXPIRED", mark)
		ground.waitForKeys("x", [("KEY_F", 1), ("KEY_F", 0)], mark, device=VIRTUAL_KEYBOARD)
		ground.settle(0.3)
		assert client.call("state")["connected"], client.records(mark)

	with ground.subtest("presses left unanswered for a second of typing drop the client and let go"):
		ground.settle(1.2)
		mark = ground.mark()
		steps: list[dict] = []
		for key in ("KEY_D", "KEY_E", "KEY_F", "KEY_G", "KEY_H", "KEY_J", "KEY_K", "KEY_L"):
			steps += [{"tap": key, "hold": 0.02}, {"sleep": 0.18}]
		keyboard.run(*steps)
		goodbye = client.waitFor("GOODBYE", mark, timeout=5)
		assert goodbye["reason"] == "UNANSWERED", goodbye
		first = client.records(mark, "KEY")[0]
		ground.machine.log(
			f"dropped {goodbye['t'] - first['t']:.3f} s after the first unanswered key: {goodbye['text']}"
		)
		assert 1.0 <= goodbye["t"] - first["t"] < 1.5, (first, goodbye)
		mark = ground.mark()
		keyboard.tap("KEY_M")
		ground.waitForKeys("x", [("KEY_M", 1), ("KEY_M", 0)], mark, device=UHID)
