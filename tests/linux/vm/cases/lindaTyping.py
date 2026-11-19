"""Typing on the kernel route: letters typed fast reach the program exactly once and in order, typed
characters and words are echoed, and a key held down repeats at the X server's rate, both a key passed
on, which the X server repeats, and one Linda keeps, which Linda repeats itself; each compared with
the X route in the same guest, with the helper stopped.
"""

import difflib
import itertools
import random
import statistics
import string
from typing import TYPE_CHECKING

from ground import KERNEL_ROUTE, LINDA_DIRECTORY, LOG_HEADER, VIRTUAL_KEYBOARD, X_ROUTE, Mark

if TYPE_CHECKING:
	from ground import FakeKeyboard, Ground

UHID = "Ground uhid keyboard"
HOLD = 2.0
#: Typed fast: a key every 30 ms, held 15 ms, some thirty-three keys a second.
FAST = {"hold": 0.015, "gap": 0.015}
TEXT = "the quick brown fox jumps over the lazy dog"
RUNS = 5
#: Typed at a person's pace: from one press to the next 70 to 130 ms, ten keys a second on the whole,
#: each key held 30 to 60 ms.
HUMAN_TEXT = "pack my box with five dozen liquor jugs "
HUMAN_PERIOD = (0.07, 0.13)
HUMAN_HOLD = (0.03, 0.06)
HUMAN_RUNS = 2
#: Bytes of the end of Linda's log read for what it says around an echo missed.
LOG_TAIL = 2_000_000
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


def echoesMissed(ground: "Ground", mark: "Mark", text: str) -> list[tuple[str, int, str]]:
	"""The echoes of text, typed after mark, that never reached the synth, as ("letter" or "word", the
	position in text of the key that should have had it, what should have been said), found by going
	through what was said in order."""
	# A space's own echo, "space", comes for some spaces and not others while typing fast, and the word
	# a held key typed before mark may come with the first space: neither is counted. What was said is
	# taken by its time rather than by its count at mark.
	spoken = [said for t, said in ground.speech(Mark(0, 0.0)) if t >= mark.t and said not in ("", "space")]
	expected = []
	for position, c in enumerate(text):
		if c != " ":
			expected.append(("letter", position, c))
		elif position and text[position - 1] != " ":
			expected.append(("word", position, text[:position].split()[-1]))
	matcher = difflib.SequenceMatcher(None, [wanted for _k, _p, wanted in expected], spoken, autojunk=False)
	said = {i for a, _b, size in matcher.get_matching_blocks() for i in range(a, a + size)}
	return [echo for i, echo in enumerate(expected) if i not in said]


def reportMissed(
	ground: "Ground", route: str, missed: list[tuple[str, int, str]], written: list[dict], mark: Mark
) -> None:
	"""Log each echo missed, with what Linda's log holds from its key's press to 150 ms after."""
	if not missed:
		return
	# The end of the log, which holds the typing of a few seconds ago many times over.
	text = ground.shell(f"tail -c {LOG_TAIL} {LINDA_DIRECTORY}/nvda.log")
	headers = list(LOG_HEADER.finditer(text))
	records = []
	for index, header in enumerate(headers):
		hours, minutes, seconds = header.group("time").split(":")
		end = headers[index + 1].start() if index + 1 < len(headers) else len(text)
		records.append(
			(int(hours) * 3600 + int(minutes) * 60 + float(seconds), text[header.start() : end].strip())
		)
	# The log is stamped with the time of day, which a guest's clock steps now and then: it is put on
	# the guest's monotonic clock by the last thing said, which the transcript stamps on that clock.
	said = [(t, words) for t, words in ground.speech(Mark(0, 0.0)) if t >= mark.t and words]
	saidAt, words = said[-1]
	spokenAt = [t for t, record in records if "Speaking [" in record and f"{words!r}" in record]
	if not spokenAt:
		ground.machine.log(f"{route} route: {len(missed)} echoes missed, and the log does not have {words!r}")
		return
	offset = spokenAt[-1] - saidAt
	presses = [r for r in written if r["value"] == 1]
	for kind, position, wanted in missed:
		at = presses[position]["t"]
		near = [record for t, record in records if at - 0.005 <= t - offset <= at + 0.15]
		ground.machine.log(
			f"{route} route: the {kind} {wanted!r} was not echoed, its key the {position}th typed, at "
			f"{at:.4f}; Linda's log from then:\n" + "\n".join(near)
		)


def typeAndCount(
	ground: "Ground", keyboard: "FakeKeyboard", route: str, text: str, steps: list[dict]
) -> list[tuple[str, int, str]]:
	"""Type text by steps; check the program had each key once, in order; answer the echoes missed,
	which the log names."""
	# A space first, in an empty field: the letters of a word typed and then deleted with Control
	# Backspace stay in NVDA's typed word, which it would say with the first word typed here.
	keyboard.tap("KEY_SPACE")
	keyboard.chord("KEY_LEFTCTRL", "KEY_A")
	keyboard.tap("KEY_DELETE")
	ground.settle(0.5)
	mark = ground.mark()
	written = keyboard.run(*steps)
	typed = [(r["key"], r["value"]) for r in written]
	ground.waitForKeys("app", typed, mark, timeout=10)
	ground.settle(1)
	app = ground.keys("app", mark)
	assert [(r["key"], r["value"]) for r in app] == typed, [(r["key"], r["value"]) for r in app]
	if route == "kernel":
		virtual = ground.keys("x", mark, device=VIRTUAL_KEYBOARD)
		assert [(r["key"], r["value"]) for r in virtual] == typed
	span = written[-1]["t"] - written[0]["t"]
	missed = echoesMissed(ground, mark, text)
	ground.machine.log(
		f"{route} route: {len(written) // 2} keys typed in {span:.3f} s reached the program once each; "
		f"{len(missed)} echoes missed"
	)
	reportMissed(ground, route, missed, written, mark)
	keyboard.chord("KEY_LEFTCTRL", "KEY_A")
	keyboard.tap("KEY_DELETE")
	ground.settle(0.5)
	return missed


def fastRun(ground: "Ground", keyboard: "FakeKeyboard", route: str) -> int:
	"""Type TEXT and a space fast; answer how many echoes were missed."""
	steps: list[dict] = []
	for c in TEXT + " ":
		steps += [{"tap": keyFor(c), "hold": FAST["hold"]}, {"sleep": FAST["gap"]}]
	return len(typeAndCount(ground, keyboard, route, TEXT + " ", steps))


def humanRun(ground: "Ground", keyboard: "FakeKeyboard", route: str, seed: int) -> int:
	"""Type HUMAN_TEXT at a person's pace, each key held and followed by a gap of its own; answer how
	many echoes were missed."""
	pace = random.Random(seed)
	steps: list[dict] = []
	for c in HUMAN_TEXT:
		hold = pace.uniform(*HUMAN_HOLD)
		steps += [{"tap": keyFor(c), "hold": hold}, {"sleep": pace.uniform(*HUMAN_PERIOD) - hold}]
	return len(typeAndCount(ground, keyboard, route, HUMAN_TEXT, steps))


def run(ground: "Ground") -> None:
	ground.openWindow()
	ground.waitForHelper()
	keyboard = ground.uhidKeyboard(UHID)
	ground.startLinda({"keyboard": {"speakTypedCharacters": "1", "speakTypedWords": "1"}})
	ground.waitForLindaLog(KERNEL_ROUTE, timeout=30)

	with ground.subtest("letters typed fast reach the program once each, in order, and are echoed"):
		kernelMissing = [fastRun(ground, keyboard, "kernel") for _ in range(RUNS)]

	with ground.subtest("typed at a person's pace, every letter and word is echoed"):
		kernelHuman = [humanRun(ground, keyboard, "kernel", seed) for seed in range(HUMAN_RUNS)]

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

	with ground.subtest("the same typing on the X route"):
		xMissing = [fastRun(ground, keyboard, "X") for _ in range(RUNS)]
		xHuman = [humanRun(ground, keyboard, "X", seed) for seed in range(HUMAN_RUNS)]
		ground.machine.log(
			f"echoes missed typing fast, run by run: {kernelMissing} on the kernel route, {xMissing} on the "
			f"X route; at a person's pace: {kernelHuman} on the kernel route, {xHuman} on the X route"
		)
		# Each echo of a key typed cuts the one before it, as on Windows the key does, so that it is spoken
		# at once rather than wait behind it for the next key's interrupt to throw it away: none is
		# missed, at either pace, on either route.
		assert not any(kernelMissing + kernelHuman), (kernelMissing, kernelHuman)
		assert not any(xMissing + xHuman), (xMissing, xHuman)
