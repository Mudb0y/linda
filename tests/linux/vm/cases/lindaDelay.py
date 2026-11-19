"""The delay the owner feels, on the kernel route and on the X route in the same guest: from a key
written on a uhid keyboard to Linda's speech starting, for a Linda command and for a typed letter's
echo, and from the key to the program having it. Medians and worst cases go to the log.

The uhid keyboard's writes, the kernel's stamps, the transcript synth's records and the test window's
are all on the guest's CLOCK_MONOTONIC.
"""

import statistics
from typing import TYPE_CHECKING

from ground import KERNEL_ROUTE, WINDOW_TITLE, X_ROUTE

if TYPE_CHECKING:
	from ground import FakeKeyboard, Ground

UHID = "Ground uhid keyboard"
COMMANDS = 12
LETTERS = "qwertyuiopasdfghjklzxcvbnm"
APART = 0.8


def summary(delays: list[float]) -> str:
	ms = sorted(d * 1000 for d in delays)
	return f"median {statistics.median(ms):.1f} ms, best {ms[0]:.1f} ms, worst {ms[-1]:.1f} ms over {len(ms)}"


def measure(ground: "Ground", keyboard: "FakeKeyboard", route: str) -> dict[str, list[float]]:
	commands = []
	for _ in range(COMMANDS):
		ground.settle(APART)
		mark = ground.mark()
		written = keyboard.chord("KEY_INSERT", "KEY_T")
		spoken = ground.waitForSpeech(WINDOW_TITLE, mark)
		press = next(r for r in written if r["key"] == "KEY_T" and r["value"] == 1)
		commands.append(spoken - press["t"])
		ground.settle(0.3)
		assert not [r for r in ground.keys("app", mark) if r["key"] in ("KEY_INSERT", "KEY_T")]
	ground.settle(APART)
	mark = ground.mark()
	written = keyboard.tap(*[f"KEY_{c.upper()}" for c in LETTERS], hold=0.03, gap=0.25)
	typed = [(r["key"], r["value"]) for r in written]
	app = ground.waitForKeys("app", typed, mark, timeout=15)
	ground.expect(*LETTERS[-3:], since=mark)
	speech = ground.speech(mark)
	echoes = []
	presses = [r for r in written if r["value"] == 1]
	index = 0
	for press in presses:
		letter = press["key"][4:].lower()
		while index < len(speech) and not (speech[index][1] == letter and speech[index][0] >= press["t"]):
			index += 1
		assert index < len(speech), (letter, speech)
		echoes.append(speech[index][0] - press["t"])
	toProgram = [d for _k, value, d in ground.delays(written, app) if value == 1]
	keyboard.chord("KEY_LEFTCTRL", "KEY_A")
	keyboard.tap("KEY_DELETE")
	result = {"command": commands, "echo": echoes, "program": toProgram}
	for name, delays in result.items():
		ground.machine.log(f"{route} route, key to {name}: {summary(delays)}")
	return result


def run(ground: "Ground") -> None:
	ground.openWindow()
	ground.waitForHelper()
	keyboard = ground.uhidKeyboard(UHID)
	ground.startLinda()
	ground.waitForLindaLog(KERNEL_ROUTE, timeout=30)

	with ground.subtest("delays on the kernel route"):
		kernel = measure(ground, keyboard, "kernel")

	with ground.subtest("delays on the X route, with the helper stopped"):
		ground.shell("systemctl stop linda-keyd.service")
		ground.waitForLindaLog(X_ROUTE, count=2)
		x = measure(ground, keyboard, "X")

	with ground.subtest("the kernel route against the X route"):
		for name in ("command", "echo", "program"):
			added = statistics.median(kernel[name]) - statistics.median(x[name])
			ground.machine.log(
				f"the kernel route adds {added * 1000:+.1f} ms to key to {name}, by the medians"
			)
		# A key the program has only after the helper's bound means Linda answered late.
		assert max(kernel["program"]) < 0.1, kernel["program"]
