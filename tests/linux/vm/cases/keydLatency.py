"""What the helper costs a key: the delay between a keyboard's key and the virtual keyboard's, with a
client that answers at once, against the same keyboard's own delay when nothing holds it; and a kept
key, which never reaches the X server at all. The figures go to the log.

The keyboard is a uhid one, so that each key's moment is known: it is written in the guest, and the
kernel stamps the virtual keyboard's event with the same clock.
"""

import statistics
from typing import TYPE_CHECKING

from ground import VIRTUAL_KEYBOARD

if TYPE_CHECKING:
	from ground import Ground

UHID = "Ground uhid keyboard"
KEYS = [f"KEY_{c}" for c in "QWERTYUIOPASDFGHJKLZ"]


def summary(delays: list[float]) -> str:
	ms = sorted(d * 1000 for d in delays)
	return f"median {statistics.median(ms):.3f} ms, least {ms[0]:.3f} ms, most {ms[-1]:.3f} ms over {len(ms)}"


def typed(written: list[dict]) -> list[tuple[str, int]]:
	return [(r["key"], r["value"]) for r in written]


def run(ground: "Ground") -> None:
	ground.openWindow()
	ground.waitForHelper()
	keyboard = ground.uhidKeyboard(UHID)
	client = ground.keydClient("main")

	with ground.subtest("a keyboard nothing holds: from the write to the kernel's event"):
		mark = ground.mark()
		written = keyboard.tap(*KEYS, hold=0.02, gap=0.03)
		evdev = ground.waitForKeys("evdev", typed(written), mark, device=UHID)
		ground.waitForKeys("app", typed(written), mark)
		alone = [d for _k, _v, d in ground.delays(written, evdev)]
		ground.machine.log(f"uhid write to its own event, nothing holding it: {summary(alone)}")

	client.capture(UHID)

	with ground.subtest(
		"held, with a client that answers at once: from the write to the virtual keyboard's event"
	):
		mark = ground.mark()
		written = keyboard.tap(*KEYS, hold=0.02, gap=0.03)
		evdev = ground.waitForKeys("evdev", typed(written), mark, device=VIRTUAL_KEYBOARD)
		x = ground.waitForKeys("x", typed(written), mark, device=VIRTUAL_KEYBOARD)
		ground.waitForKeys("app", typed(written), mark)
		held = [d for _k, _v, d in ground.delays(written, evdev)]
		ground.machine.log(f"uhid write to the virtual keyboard's event, held: {summary(held)}")
		ground.machine.log(
			f"the virtual keyboard's event to the X server: {summary([d for *_, d in ground.delays(evdev, x)])}"
		)
		records = client.records(mark)
		asked = {r["seq"]: r for r in records if r["type"] == "KEY" and r["ask"]}
		answered = {r["seq"]: r for r in records if r["type"] == "VERDICT"}
		presses = [r for r in written if r["value"] == 1]
		toClient = [a["t"] - w["t"] for w, a in zip(presses, asked.values(), strict=True)]
		clientTook = [answered[seq]["t"] - a["t"] for seq, a in asked.items()]
		virtualPresses = [r for r in evdev if r["value"] == 1]
		afterVerdict = [v["t"] - answered[seq]["t"] for seq, v in zip(asked, virtualPresses, strict=True)]
		ground.machine.log(f"a press, from the write to the client's reading of it: {summary(toClient)}")
		ground.machine.log(f"the client's own time to answer: {summary(clientTook)}")
		ground.machine.log(
			f"from the client's answer to the virtual keyboard's event: {summary(afterVerdict)}"
		)
		added = statistics.median(held) - statistics.median(alone)
		ground.machine.log(f"the helper adds {added * 1000:.3f} ms to a passed key, by the medians")
		assert not client.records(mark, "EXPIRED"), "a key the client answered at once expired"
		# Far under the verdict's bound, which a key reaches only when its client is in trouble.
		assert max(held) < 0.05, held

	with ground.subtest("a kept key never reaches the X server"):
		client.verdicts("keep")
		mark = ground.mark()
		written = keyboard.tap(*KEYS[:5], hold=0.02, gap=0.03)
		client.waitFor("KEY", mark, lambda r: r["key"] == KEYS[4] and r["value"] == 0)
		ground.settle(0.5)
		for source in ("evdev", "x", "app"):
			assert not ground.keys(source, mark), (source, ground.keys(source, mark))
		kept = client.records(mark, "VERDICT")
		assert len(kept) == 5 and all(r["keep"] for r in kept), kept
		ground.machine.log(
			f"{len(written)} events kept: none reached a reader of the keyboards, the X server or the program"
		)
