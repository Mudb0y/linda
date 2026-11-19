"""Typing on the kernel route: letters typed fast reach the program exactly once and in order, typed
characters and words are echoed, and a key held down repeats at the X server's rate, both a key passed
on, which the X server repeats, and one Linda keeps, which Linda repeats itself; each compared with
the X route in the same guest, with the helper stopped.
"""

import itertools
import statistics
import string
from typing import TYPE_CHECKING

from ground import KERNEL_ROUTE, VIRTUAL_KEYBOARD, X_ROUTE

if TYPE_CHECKING:
	from ground import FakeKeyboard, Ground

UHID = "Ground uhid keyboard"
HOLD = 2.0
#: Typed fast: a key every 30 ms, held 15 ms, some thirty-three keys a second.
FAST = {"hold": 0.015, "gap": 0.015}
TEXT = "the quick brown fox jumps over the lazy dog"
RUNS = 3
#: For the review cursor to walk back over while numpad 1 is held.
LONG_WORD = string.ascii_lowercase * 3


def keyFor(c: str) -> str:
	return "KEY_SPACE" if c == " " else f"KEY_{c.upper()}"


def intervals(times: list[float]) -> str:
	gaps = [b - a for a, b in itertools.pairwise(times)]
	if not gaps:
		return "no repeats"
	ms = [g * 1000 for g in gaps]
	return f"{len(times)} events, first after the press, then every {statistics.median(ms[1:] or ms):.1f} ms by the median"


def holdPassed(ground: "Ground", keyboard: "FakeKeyboard", route: str) -> int:
	"""Hold A, which the program has, and answer how many presses of A it got."""
	mark = ground.mark()
	written = keyboard.run({"press": "KEY_A"}, {"sleep": HOLD}, {"release": "KEY_A"})
	ground.waitForKeys("app", [("KEY_A", 1)], mark)
	ground.settle(0.5)
	presses = [r for r in ground.keys("app", mark) if r["key"] == "KEY_A" and r["value"] == 1]
	start = written[0]["t"]
	ground.machine.log(
		f"A held {HOLD} s on the {route} route: {intervals([r['t'] for r in presses])}, the first "
		f"{(presses[0]['t'] - start) * 1000:.1f} ms after the write, the second "
		f"{(presses[1]['t'] - start) * 1000:.1f} ms after it"
	)
	mark = ground.mark()
	# Clear the field of them, a word at a time.
	keyboard.chord("KEY_LEFTCTRL", "KEY_BACKSPACE")
	return len(presses)


def holdKept(ground: "Ground", keyboard: "FakeKeyboard", route: str) -> int:
	"""Hold numpad 1, Linda's review key for the previous character, and answer how many characters it
	spoke."""
	mark = ground.mark()
	written = keyboard.tap(*[keyFor(c) for c in LONG_WORD], hold=0.01, gap=0.01)
	ground.waitForKeys("app", [(r["key"], r["value"]) for r in written], mark)
	ground.settle(1.5)
	mark = ground.mark()
	written = keyboard.run({"press": "KEY_KP1"}, {"sleep": HOLD}, {"release": "KEY_KP1"})
	ground.settle(0.5)
	spoken = [t for t, text in ground.speech(mark) if len(text) == 1]
	start = written[0]["t"]
	ground.machine.log(
		f"numpad 1 held {HOLD} s on the {route} route: {intervals(spoken)}, the first "
		f"{(spoken[0] - start) * 1000:.1f} ms after the write, the second {(spoken[1] - start) * 1000:.1f} ms after it"
	)
	assert not ground.keys("app", mark), ground.keys("app", mark)
	keyboard.chord("KEY_LEFTCTRL", "KEY_BACKSPACE")
	return len(spoken)


def fastRun(ground: "Ground", keyboard: "FakeKeyboard", route: str) -> int:
	"""Type TEXT and a space fast; check the program had each key once, in order; answer how many
	letters and words went unechoed, which the log names."""
	mark = ground.mark()
	written = keyboard.tap(*[keyFor(c) for c in TEXT + " "], **FAST)
	typed = [(r["key"], r["value"]) for r in written]
	ground.waitForKeys("app", typed, mark, timeout=10)
	ground.settle(1)
	app = ground.keys("app", mark)
	assert [(r["key"], r["value"]) for r in app] == typed, [(r["key"], r["value"]) for r in app]
	if route == "kernel":
		virtual = ground.keys("x", mark, device=VIRTUAL_KEYBOARD)
		assert [(r["key"], r["value"]) for r in virtual] == typed
	span = written[-1]["t"] - written[0]["t"]
	# A space's own echo, "space", comes for some spaces and not others at this rate, and is not
	# counted.
	spoken = [text for _t, text in ground.speech(mark) if text not in ("", "space")]
	letters = "".join(text for text in spoken if len(text) == 1)
	words = [text for text in spoken if len(text) > 1]
	missing = len(TEXT.replace(" ", "")) - len(letters) + len(TEXT.split()) - len(words)
	ground.machine.log(
		f"{route} route: {len(written) // 2} keys typed in {span:.3f} s reached the program once each; "
		f"echoed letters {letters!r}, words {words}"
	)
	keyboard.chord("KEY_LEFTCTRL", "KEY_A")
	keyboard.tap("KEY_DELETE")
	ground.settle(0.5)
	return missing


def run(ground: "Ground") -> None:
	ground.openWindow()
	ground.waitForHelper()
	keyboard = ground.uhidKeyboard(UHID)
	ground.startLinda({"keyboard": {"speakTypedCharacters": "1", "speakTypedWords": "1"}})
	ground.waitForLindaLog(KERNEL_ROUTE, timeout=30)

	with ground.subtest("letters typed fast reach the program once each, in order, and are echoed"):
		kernelMissing = sum(fastRun(ground, keyboard, "kernel") for _ in range(RUNS))

	with ground.subtest("a held key repeats at the X server's rate, passed or kept, on both routes"):
		ground.settle(1)
		kernelPassed = holdPassed(ground, keyboard, "kernel")
		kernelKept = holdKept(ground, keyboard, "kernel")
		ground.shell("systemctl stop linda-keyd.service")
		ground.waitForLindaLog(X_ROUTE, count=2)
		ground.settle(1)
		xPassed = holdPassed(ground, keyboard, "X")
		xKept = holdKept(ground, keyboard, "X")
		ground.machine.log(
			f"presses of A held: {kernelPassed} on the kernel route, {xPassed} on the X route; characters "
			f"spoken with numpad 1 held: {kernelKept} on the kernel route, {xKept} on the X route"
		)
		# At 25 a second after 660 ms, some 34 in two seconds; a repeat or two either way is timing.
		assert abs(kernelPassed - xPassed) <= 2, (kernelPassed, xPassed)
		assert abs(kernelKept - xKept) <= 3, (kernelKept, xKept)
		assert kernelPassed > 20 and kernelKept > 15, (kernelPassed, kernelKept)

	with ground.subtest("the same fast typing on the X route"):
		xMissing = sum(fastRun(ground, keyboard, "X") for _ in range(RUNS))
		ground.machine.log(
			f"over {RUNS} runs each, echoes missing: {kernelMissing} on the kernel route, {xMissing} on the X route"
		)
		# At this rate Linda leaves out an echo now and then on either route, some two in a hundred: the
		# speech of a typed character, while the word it is in is still spoken. The kernel route is to do
		# no worse than that.
		echoes = RUNS * (len(TEXT.replace(" ", "")) + len(TEXT.split()))
		assert kernelMissing <= max(xMissing, echoes // 25) + 2, (kernelMissing, xMissing)
