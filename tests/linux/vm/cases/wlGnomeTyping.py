"""Typing, Remote Access and Linda's own windows on GNOME, with the real helper, in one boot: letters
typed fast and at a person's pace reach the program once each and in order and are echoed, as
cases/lindaTyping.py holds the X session to; a key held down repeats, passed on and kept; keys Linda's
caret scripts send on reach the field once and in order with the letters around them; a Remote Access
leader types through Linda as its follower, and its Linda key chords are Linda's; the menu opens with
the Linda key and N, the settings dialog with Linda's command, and comes forward again from behind
another window; and nothing typed is in Linda's log or the helper's journal.

Where Linda's window is not given the keyboard, the case asserts that Linda says how to reach it and
writes the limitation down after "AT limitation".

The file has two halves. On the host, run(ground) is the case. In the guest, as ground-typing-guest,
which cases/wlGnomeTyping.nix installs with tests/linux/remotePeer.py named by GROUND_REMOTE_PEER:

	wlGnomeTyping.py lead STEPS
	wlGnomeTyping.py watch OUT

lead joins the Remote Access channel of the Linda running in the guest as the leader, runs STEPS, a JSON
list of {"key": VK, "pressed": BOOL, "extended": BOOL}, {"sleep": SECONDS} and {"speak": TEXT,
"timeout": SECONDS}, which waits for the follower to send speech holding TEXT, and prints as JSON
whether each speak step was heard. watch writes each window created and activated to OUT, as
watch's own text says.
"""

import difflib
import importlib.util
import itertools
import json
import os
import random
import shlex
import statistics
import string
import sys
import time
from typing import TYPE_CHECKING

if __name__ != "__main__":
	from ground import (
		BASE_SETTINGS,
		KERNEL_ROUTE,
		LINDA_DIRECTORY,
		LOG_HEADER,
		TRANSCRIPT,
		USER,
		VIRTUAL_KEYBOARD,
		WINDOW_TITLE,
		Mark,
		keyEvents,
	)
	from wl import lindaAcceptance as acceptance
	from wl.lindaAcceptance import APART, UHID, lindaKeyWorks, said, saidOnce

if TYPE_CHECKING:
	from ground import FakeKeyboard, Ground

DESKTOP = "gnome"
KEYD = "linda-keyd.service"
NVDA_LOG = "/tmp/ground/linda/nvda.log"

# Typing, as cases/lindaTyping.py has it.

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
#: Seconds by which the delay and the interval of Linda's own repeat of a key it keeps may miss
#: GNOME's: speech is given to the synth a few milliseconds late, and the guest's clock is busy.
REPEAT_DELAY_SLACK = 0.06
REPEAT_INTERVAL_SLACK = 0.005

# Remote Access: Linda hosts the relay server itself and joins it as the follower, as
# tests/linux/test_remoteAccess.py's remoteConfig has it, and the leader joins from the guest.

REMOTE_PORT = 16839
REMOTE_CHANNEL = "groundchannel"
REMOTE_SECTION = (
	"[remote]\n\tenabled = True\n\t[[controlServer]]\n\t\tautoconnect = True\n\t\tselfHosted = True\n"
	f"\t\tconnectionMode = 0\n\t\tport = {REMOTE_PORT}\n\t\tkey = {REMOTE_CHANNEL}\n"
	"\t[[ui]]\n\t\tconfirmDisconnectAsFollower = False\n"
)
#: The guest half of this file, as cases/wlGnomeTyping.nix installs it.
GUEST_TOOL = "ground-typing-guest"
#: Windows virtual-key codes, as NVDA's keyboard hook on Windows reports them to a leader.
VK_INSERT = 0x2D
VK_NUMPAD5 = 0x65
VK_LSHIFT = 0xA0
#: Seconds between the events of a key the leader types, as a person types.
LEADER_GAP = 0.03

#: Linda's own windows, as their titles are and as the transcript has what Linda says of them.
MENU_TITLE = "Linda"
SETTINGS_TITLE = "Linda Settings"
SETTINGS_SPOKEN = "Linda Settings: General (normal configuration) dialog"
NOT_REACHED = "Linda's window opened but did not get the keyboard. Press Alt plus Tab to reach it."
#: Seconds from the key to the window's activation, as the package asks of the menu window.
ACTIVATION_BOUND = 1.0
#: Seconds a window of Linda's is given to be activated at all before the case takes it as refused.
REFUSED_AFTER = 5.0
#: Why Linda's windows miss ACTIVATION_BOUND on GNOME. In runs 3 to 6 of the case the menu window was
#: activated 1.09 to 1.25 s after the key, created 0.88 to 1.01 s after it; in run 6 the settings dialog
#: was created 0.49 s after its key and activated 0.76 s after that. Under sway the same windows were
#: activated 0.32 to 0.43 s after their keys, and under Plasma 0.62 to 0.93 s.
WINDOWS_LATE = (
	"on GNOME Linda's menu window and settings dialog are active only some 1.1 to 1.25 s after their "
	"keys: Linda creates them 0.5 to 1 s after the key, and GNOME activates them 0.25 to 0.75 s after that"
)
#: Where the guest half's watch writes each window created and activated, with the process it is of.
OWN_WINDOWS = "/tmp/ground/ownWindows.jsonl"


def startLinda(ground: "Ground", settings: dict[str, dict[str, str]], extra: str = "") -> None:
	"""Ground.startLinda with extra, nested sections of nvda.ini after settings, which it cannot write."""
	merged = {section: dict(values) for section, values in BASE_SETTINGS.items()}
	for section, values in settings.items():
		merged.setdefault(section, {}).update(values)
	lines = ["schemaVersion = 25"]
	for section, values in merged.items():
		lines.append(f"[{section}]")
		lines += [f"\t{key} = {value}" for key, value in values.items()]
	config = f"{LINDA_DIRECTORY}/config"
	ground.machine.succeed(
		f"install -d -o {USER} {LINDA_DIRECTORY} {config}",
		f"echo {shlex.quote(chr(10).join(lines) + chr(10) + extra)} > {config}/nvda.ini",
		f"chown {USER} {config}/nvda.ini",
	)
	ground.start(
		"linda",
		[f"{ground.linda}/bin/linda", "-c", config, "-f", NVDA_LOG, "--debug-logging"],
		env={
			"LINDA_TEST_HOOKS_DIR": f"{ground.linda}/share/linda/tests/linux/testHooks",
			"LINDA_TRANSCRIPT_FILE": TRANSCRIPT,
			"LINDA_TRANSCRIPT_MODE": "immediate",
		},
	)
	ground.lindaStarted = True
	ground.expect(WINDOW_TITLE, since=Mark(0, 0.0), timeout=120)


def heardInOrder(ground: "Ground", since: "Mark", *texts: str) -> bool:
	records = iter(said(ground, since))
	return all(any(text in record for record in records) for text in texts)


def waitHeard(ground: "Ground", since: "Mark", *texts: str, timeout: float = 10) -> None:
	try:
		ground.waitFor(lambda: heardInOrder(ground, since, *texts), f"Linda to say {list(texts)}", timeout)
	except AssertionError as e:
		raise AssertionError(f"{e}; it said {said(ground, since)}") from None


def appKeys(ground: "Ground", since: "Mark", keys: tuple[str, ...]) -> list[tuple[str, int]]:
	"""The presses of keys the test window had. A release may come to it alone: the press of Escape
	closes Linda's window, and its release goes where the keyboard is then."""
	return [(r["key"], r["value"]) for r in ground.keys("app", since) if r["key"] in keys and r["value"]]


# Typing echo, as cases/lindaTyping.py has it on X.


def keyFor(c: str) -> str:
	return "KEY_SPACE" if c == " " else f"KEY_{c.upper()}"


def intervals(times: list[float]) -> str:
	gaps = [b - a for a, b in itertools.pairwise(times)]
	if not gaps:
		return "no repeats"
	ms = [g * 1000 for g in gaps]
	return f"{len(times)} events, first after the press, then every {statistics.median(ms[1:] or ms):.1f} ms by the median"


def medianGap(times: list[float]) -> float:
	"""The median of the gaps between times after the first, which is the delay, in seconds."""
	return statistics.median(b - a for a, b in itertools.pairwise(times[1:]))


def gnomeRepeat(ground: "Ground") -> tuple[float, float]:
	"""GNOME's repeat delay and interval, in seconds, which Linda's layout feed reads."""
	values = [
		acceptance.gsettings(ground, "get", "org.gnome.desktop.peripherals.keyboard", key).split()[-1]
		for key in ("delay", "repeat-interval")
	]
	return int(values[0]) / 1000, int(values[1]) / 1000


def holdPassed(ground: "Ground", keyboard: "FakeKeyboard") -> list[float]:
	"""Hold A, which the program has, and answer when each press of A came to it."""
	mark = ground.mark()
	written = keyboard.run({"press": "KEY_A"}, {"sleep": HOLD}, {"release": "KEY_A"})
	ground.waitForKeys("app", [("KEY_A", 1)], mark)
	ground.settle(0.5)
	presses = [r for r in ground.keys("app", mark) if r["key"] == "KEY_A" and r["value"] == 1]
	start = written[0]["t"]
	ground.machine.log(
		f"A held {HOLD} s: {intervals([r['t'] for r in presses])}, the first "
		f"{(presses[0]['t'] - start) * 1000:.1f} ms after the write, the second "
		f"{(presses[1]['t'] - start) * 1000:.1f} ms after it"
	)
	keyboard.chord("KEY_LEFTCTRL", "KEY_BACKSPACE")
	return [r["t"] for r in presses]


def holdKept(ground: "Ground", keyboard: "FakeKeyboard") -> list[float]:
	"""Hold numpad 1, Linda's review key for the previous character, and answer when it spoke each
	character."""
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
		f"numpad 1 held {HOLD} s: {intervals(spoken)}, the first {(spoken[0] - start) * 1000:.1f} ms "
		f"after the write, the second {(spoken[1] - start) * 1000:.1f} ms after it"
	)
	assert not ground.keys("app", mark), ground.keys("app", mark)
	keyboard.chord("KEY_LEFTCTRL", "KEY_BACKSPACE")
	return spoken


def echoesMissed(ground: "Ground", mark: "Mark", text: str) -> list[tuple[str, int, str]]:
	"""The echoes of text, typed after mark, that never reached the synth, as ("letter" or "word", the
	position in text of the key that should have had it, what should have been said)."""
	# A space's own echo, "space", comes for some spaces and not others while typing fast, and the word
	# a held key typed before mark may come with the first space: neither is counted.
	spoken = [said for t, said in ground.speech(Mark(0, 0.0)) if t >= mark.t and said not in ("", "space")]
	expected = []
	for position, c in enumerate(text):
		if c != " ":
			expected.append(("letter", position, c))
		elif position and text[position - 1] != " ":
			expected.append(("word", position, text[:position].split()[-1]))
	matcher = difflib.SequenceMatcher(None, [wanted for _k, _p, wanted in expected], spoken, autojunk=False)
	heard = {i for a, _b, size in matcher.get_matching_blocks() for i in range(a, a + size)}
	return [echo for i, echo in enumerate(expected) if i not in heard]


def reportMissed(
	ground: "Ground", missed: list[tuple[str, int, str]], written: list[dict], mark: Mark
) -> None:
	"""Log each echo missed, with what Linda's log holds from its key's press to 150 ms after."""
	if not missed:
		return
	text = ground.shell(f"tail -c {LOG_TAIL} {NVDA_LOG}")
	headers = list(LOG_HEADER.finditer(text))
	records = []
	for index, header in enumerate(headers):
		hours, minutes, seconds = header.group("time").split(":")
		end = headers[index + 1].start() if index + 1 < len(headers) else len(text)
		records.append(
			(int(hours) * 3600 + int(minutes) * 60 + float(seconds), text[header.start() : end].strip())
		)
	# The log is stamped with the time of day, put on the guest's monotonic clock by the last thing said.
	spokenSince = [(t, words) for t, words in ground.speech(Mark(0, 0.0)) if t >= mark.t and words]
	saidAt, words = spokenSince[-1]
	spokenAt = [t for t, record in records if "Speaking [" in record and f"{words!r}" in record]
	if not spokenAt:
		ground.machine.log(f"{len(missed)} echoes missed, and the log does not have {words!r}")
		return
	offset = spokenAt[-1] - saidAt
	presses = [r for r in written if r["value"] == 1]
	for kind, position, wanted in missed:
		at = presses[position]["t"]
		near = [record for t, record in records if at - 0.005 <= t - offset <= at + 0.15]
		ground.machine.log(
			f"the {kind} {wanted!r} was not echoed, its key the {position}th typed, at {at:.4f}; Linda's "
			"log from then:\n" + "\n".join(near)
		)


def typeAndCount(
	ground: "Ground", keyboard: "FakeKeyboard", text: str, steps: list[dict]
) -> list[tuple[str, int, str]]:
	"""Type text by steps; check the program had each key once, in order, through the virtual keyboard;
	answer the echoes missed, which the log names."""
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
	virtual = ground.keys("evdev", mark, device=VIRTUAL_KEYBOARD)
	assert keyEvents(virtual) == typed, keyEvents(virtual)
	span = written[-1]["t"] - written[0]["t"]
	missed = echoesMissed(ground, mark, text)
	ground.machine.log(
		f"{len(written) // 2} keys typed in {span:.3f} s reached the program once each; "
		f"{len(missed)} echoes missed"
	)
	reportMissed(ground, missed, written, mark)
	keyboard.chord("KEY_LEFTCTRL", "KEY_A")
	keyboard.tap("KEY_DELETE")
	ground.settle(0.5)
	return missed


def fastRun(ground: "Ground", keyboard: "FakeKeyboard") -> int:
	steps: list[dict] = []
	for c in TEXT + " ":
		steps += [{"tap": keyFor(c), "hold": FAST["hold"]}, {"sleep": FAST["gap"]}]
	return len(typeAndCount(ground, keyboard, TEXT + " ", steps))


def humanRun(ground: "Ground", keyboard: "FakeKeyboard", seed: int) -> int:
	pace = random.Random(seed)
	steps: list[dict] = []
	for c in HUMAN_TEXT:
		hold = pace.uniform(*HUMAN_HOLD)
		steps += [{"tap": keyFor(c), "hold": hold}, {"sleep": pace.uniform(*HUMAN_PERIOD) - hold}]
	return len(typeAndCount(ground, keyboard, HUMAN_TEXT, steps))


def typing(ground: "Ground", keyboard: "FakeKeyboard") -> dict:
	figures: dict = {}
	with ground.subtest("letters typed fast reach the program once each, in order, and are echoed"):
		figures["fast"] = [fastRun(ground, keyboard) for _ in range(RUNS)]
		ground.machine.log(f"echoes missed typing fast, run by run: {figures['fast']}")
		assert not any(figures["fast"]), figures["fast"]

	with ground.subtest("typed at a person's pace, every letter and word is echoed"):
		figures["human"] = [humanRun(ground, keyboard, seed) for seed in range(HUMAN_RUNS)]
		ground.machine.log(f"echoes missed at a person's pace, run by run: {figures['human']}")
		assert not any(figures["human"]), figures["human"]

	with ground.subtest("a held key repeats: passed on by GTK, and kept by Linda at GNOME's own rate"):
		ground.settle(1)
		delay, interval = gnomeRepeat(ground)
		passed = holdPassed(ground, keyboard)
		kept = holdKept(ground, keyboard)
		figures["repeat"] = {
			"gnome": [delay, interval],
			"passed": [len(passed), round(medianGap(passed), 4)],
			"kept": [len(kept), round(kept[1] - kept[0], 4), round(medianGap(kept), 4)],
		}
		ground.machine.log(f"repeats, as (count, delay, interval) in seconds: {figures['repeat']}")
		# GTK repeats a key itself, from the compositor's repeat information, on a timer the guest's load
		# stretches; Linda repeats the keys it keeps from what its layout feed read of GNOME's settings.
		assert len(passed) > 20, figures["repeat"]
		assert abs(kept[1] - kept[0] - delay) <= REPEAT_DELAY_SLACK, figures["repeat"]
		assert abs(medianGap(kept) - interval) <= REPEAT_INTERVAL_SLACK, figures["repeat"]
	return figures


def caretScripts(ground: "Ground", keyboard: "FakeKeyboard") -> None:
	with ground.subtest("arrows Linda's caret scripts send on reach the field once, in order with letters"):
		keyboard.chord("KEY_LEFTCTRL", "KEY_A")
		keyboard.tap("KEY_DELETE")
		ground.settle(0.5)
		mark = ground.mark()
		keys = ["KEY_A", "KEY_B", "KEY_C", "KEY_LEFT", "KEY_LEFT", "KEY_X", "KEY_RIGHT", "KEY_Y"]
		steps: list[dict] = []
		for key in keys:
			steps += [{"tap": key, "hold": FAST["hold"]}, {"sleep": FAST["gap"]}]
		written = keyboard.run(*steps)
		typed = [(r["key"], r["value"]) for r in written]
		ground.waitForKeys("app", typed, mark)
		ground.settle(1)
		assert keyEvents(ground.keys("app", mark)) == typed, keyEvents(ground.keys("app", mark))
		virtual = keyEvents(ground.keys("evdev", mark, device=VIRTUAL_KEYBOARD))
		assert virtual == typed, virtual
		ground.machine.log(f"the caret keys: Linda said {said(ground, mark)}")
		keyboard.chord("KEY_LEFTCTRL", "KEY_A")
		keyboard.tap("KEY_DELETE")
		ground.settle(0.5)


# Remote Access.


def lead(ground: "Ground", steps: list[dict], timeout: float = 60) -> dict:
	answer = ground.shell(f"timeout {timeout} {GUEST_TOOL} lead {shlex.quote(json.dumps(steps))}")
	return json.loads(answer.strip().splitlines()[-1])


def tap(vk: int, extended: bool = False, mods: tuple[tuple[int, bool], ...] = ()) -> list[dict]:
	"""A key as the leader's hook reports one, with mods held around it."""
	steps: list[dict] = []
	for mod, modExtended in mods:
		steps += [{"key": mod, "pressed": True, "extended": modExtended}, {"sleep": LEADER_GAP}]
	steps += [
		{"key": vk, "pressed": True, "extended": extended},
		{"sleep": LEADER_GAP},
		{"key": vk, "pressed": False, "extended": extended},
		{"sleep": LEADER_GAP},
	]
	for mod, modExtended in reversed(mods):
		steps += [{"key": mod, "pressed": False, "extended": modExtended}, {"sleep": LEADER_GAP}]
	return steps


def typeText(text: str) -> list[dict]:
	return [s for c in text for s in tap(ord(c.upper()), mods=((VK_LSHIFT, False),) if c.isupper() else ())]


def remoteAccess(ground: "Ground", keyboard: "FakeKeyboard") -> None:
	with ground.subtest("a Remote Access leader types through Linda, and its Linda key chord is Linda's"):
		ground.waitForLindaLog("Control connector connected", timeout=60)
		ground.activate(WINDOW_TITLE)
		keyboard.chord("KEY_LEFTCTRL", "KEY_A")
		keyboard.tap("KEY_DELETE")
		ground.settle(APART)
		mark = ground.mark()
		offset = acceptance.logSize(ground, NVDA_LOG)
		steps = [
			{"sleep": 1.0},
			*typeText("Ab"),
			{"sleep": 1.0},
			*tap(ord("T"), mods=((VK_INSERT, True),)),
			{"speak": WINDOW_TITLE, "timeout": 10},
			{"sleep": 0.5},
			*tap(VK_NUMPAD5),
			{"sleep": 0.5},
			*typeText(acceptance.MARKER),
			{"sleep": 1.0},
		]
		answer = lead(ground, steps)
		ground.machine.log(f"the leader: {answer}")
		assert answer["heard"] == [True], answer
		ground.settle(1)
		app = ground.keys("app", mark)
		presses = [r["keyval"] for r in app if r["value"] == 1 and r["key"] != "KEY_LEFTSHIFT"]
		numLocks = [r for r in app if r["key"] == "KEY_NUMLOCK" and r["value"] == 1]
		letters = [k for k in presses if k != "Num_Lock"]
		ground.machine.log(
			f"through Remote Access the program had {letters}, Num Lock pressed {len(numLocks)} times; "
			f"Linda's log on its typed keys: {[r.splitlines()[-1] for _t, r in ground.lindaLogRecords('typed key')]}"
		)
		assert letters == ["A", "b", "KP_5", *acceptance.MARKER], letters
		assert len(numLocks) % 2 == 0, numLocks
		assert not [r for r in app if r["key"] in ("KEY_INSERT", "KEY_T")], app
		virtual = [
			r
			for r in ground.keys("evdev", mark, device=VIRTUAL_KEYBOARD)
			if r["key"] in ("KEY_INSERT", "KEY_T")
		]
		assert not virtual, virtual
		assert ground.lindaLogRecords("Input: kb(desktop):NVDA+t"), "Linda did not take the leader's chord"
		acceptance.assertLogClean(
			ground, {"Linda's log as the leader typed": acceptance.logSince(ground, NVDA_LOG, offset)}
		)
		journal = "\n".join(m for _t, m in ground.helperLog(mark))
		assert not acceptance.holdingMarker({"journal": journal}), "the helper's journal holds what was typed"
		keyboard.chord("KEY_LEFTCTRL", "KEY_A")
		keyboard.tap("KEY_DELETE")
		lindaKeyWorks(ground, keyboard)


# Linda's own windows.


def activations(ground: "Ground", since: float, title: str, *, exact: bool) -> list[dict]:
	return [
		e
		for e in acceptance.windowEvents(ground, since)
		if e.get("type") == "window:activate"
		and (e.get("title") == title if exact else e.get("title", "").startswith(title))
	]


def ownWindow(
	ground: "Ground",
	mark: "Mark",
	pressed: float,
	title: str,
	*,
	exact: bool,
	what: str,
	figures: dict,
) -> bool:
	"""Whether Linda's window titled title was activated, within ACTIVATION_BOUND of the key pressed at
	pressed. A window not activated within REFUSED_AFTER is the compositor's refusal: Linda must then
	have said once how to reach it, and the limitation is written down."""
	try:
		event = ground.waitFor(
			lambda: activations(ground, pressed, title, exact=exact), f"{what} to be activated", REFUSED_AFTER
		)[0]
	except AssertionError:
		event = None
	if event is not None:
		delay = round(event["t"] - pressed, 3)
		created = next(
			(e["t"] for e in ownWindowEvents(ground, pressed) if e["type"] == "window:create"), None
		)
		figures[what] = {
			"activated": delay,
			"created": None if created is None else round(created - pressed, 3),
		}
		ground.machine.log(f"{what}: {figures[what]} s after the key")
		assert NOT_REACHED not in said(ground, mark), said(ground, mark)
		return True
	saidOnce(ground, mark, NOT_REACHED)
	figures[what] = {"activated": None, "said": NOT_REACHED}
	ground.machine.log(
		f"AT limitation on {DESKTOP}: {what} was not given the keyboard within {REFUSED_AFTER} s, and Linda "
		f"said {NOT_REACHED!r} once; the windows: {acceptance.windowEvents(ground, pressed)}"
	)
	return False


def ownWindowEvents(ground: "Ground", since: float) -> list[dict]:
	return [r for r in ground.readJsonLines(OWN_WINDOWS) if r.get("t", 0) >= since]


def withinBound(figure: dict) -> None:
	"""A window of Linda's that was activated was activated within ACTIVATION_BOUND of its key; held at
	the end of a subtest, so that the window is used however long it took."""
	if figure.get("activated") is not None:
		assert figure["activated"] < ACTIVATION_BOUND, f"activated later than {ACTIVATION_BOUND} s: {figure}"


def reachRefused(ground: "Ground", keyboard: "FakeKeyboard", title: str) -> None:
	"""Close a window of Linda's the compositor did not give the keyboard, once it is reached."""
	whole = next((w["title"] for w in ground.focus("windows") if w["title"].startswith(title)), title)
	try:
		ground.activate(whole)
	except (AssertionError, RuntimeError, ValueError) as e:
		ground.machine.log(f"{whole!r} could not be reached to close it: {e}")
		return
	ground.settle(0.5)
	keyboard.tap("KEY_ESC")


def backToTheWindow(ground: "Ground", since: float) -> None:
	ground.waitFor(
		lambda: acceptance.activeTitle(ground) == WINDOW_TITLE, "the test window to be active again"
	)
	assert activations(ground, since, WINDOW_TITLE, exact=True), acceptance.windowEvents(ground, since)


def menu(ground: "Ground", keyboard: "FakeKeyboard", figures: dict) -> None:
	with ground.subtest("the Linda key with N opens the menu, active within a second and used by keys"):
		ground.activate(WINDOW_TITLE)
		ground.settle(APART)
		mark = ground.mark()
		pressed = acceptance.written(keyboard.chord("KEY_INSERT", "KEY_N"), "KEY_N", 1)
		if not ownWindow(ground, mark, pressed, MENU_TITLE, exact=True, what="the menu", figures=figures):
			reachRefused(ground, keyboard, MENU_TITLE)
			return
		waitHeard(ground, mark, f"{MENU_TITLE} dialog", "Preferences collapsed 1 of 9")
		for key, text in (
			("KEY_DOWN", "Tools collapsed 2 of 9"),
			# GNOME's Tools menu has one item more than the rig's, and says its level first.
			("KEY_RIGHT", "View log"),
			("KEY_LEFT", "Tools collapsed 2 of 9"),
			("KEY_UP", "Preferences collapsed 1 of 9"),
		):
			step = ground.mark()
			keyboard.tap(key)
			waitHeard(ground, step, text)
		escaped = ground.now()
		keyboard.tap("KEY_ESC")
		backToTheWindow(ground, escaped)
		ground.settle(0.5)
		reached = appKeys(
			ground, mark, ("KEY_INSERT", "KEY_N", "KEY_DOWN", "KEY_RIGHT", "KEY_LEFT", "KEY_UP", "KEY_ESC")
		)
		assert not reached, f"the test window had {reached}"
		with ground.expectedFailure(WINDOWS_LATE):
			withinBound(figures["the menu"])


def dialogs(ground: "Ground", keyboard: "FakeKeyboard", figures: dict) -> None:
	with ground.subtest("Linda's command opens its settings dialog, active within a second"):
		ground.activate(WINDOW_TITLE)
		ground.settle(APART)
		mark = ground.mark()
		pressed = acceptance.written(keyboard.chord("KEY_INSERT", "KEY_LEFTCTRL", "KEY_G"), "KEY_G", 1)
		opened = ownWindow(
			ground, mark, pressed, SETTINGS_TITLE, exact=False, what="the settings dialog", figures=figures
		)
		if not opened:
			reachRefused(ground, keyboard, SETTINGS_TITLE)
			return
		waitHeard(ground, mark, SETTINGS_SPOKEN)
		step = ground.mark()
		keyboard.tap("KEY_TAB")
		ground.waitFor(lambda: ground.speech(step), "Linda to say where Tab went in the dialog")
		ground.machine.log(f"Tab in the settings dialog: Linda said {said(ground, step)}")
		reached = appKeys(ground, mark, ("KEY_INSERT", "KEY_G", "KEY_TAB"))
		assert not reached, f"the test window had {reached}"
		with ground.expectedFailure(WINDOWS_LATE):
			withinBound(figures["the settings dialog"])

	with ground.subtest("the settings dialog, behind another window, comes forward at the command again"):
		ground.activate(WINDOW_TITLE)
		ground.settle(APART)
		assert acceptance.activeTitle(ground) == WINDOW_TITLE, acceptance.activeTitle(ground)
		mark = ground.mark()
		pressed = acceptance.written(keyboard.chord("KEY_INSERT", "KEY_LEFTCTRL", "KEY_G"), "KEY_G", 1)
		if ownWindow(
			ground,
			mark,
			pressed,
			SETTINGS_TITLE,
			exact=False,
			what="the settings dialog again",
			figures=figures,
		):
			waitHeard(ground, mark, SETTINGS_SPOKEN)
			escaped = ground.now()
			keyboard.tap("KEY_ESC")
			backToTheWindow(ground, escaped)
		else:
			reachRefused(ground, keyboard, SETTINGS_TITLE)
		ground.settle(0.5)
		reached = appKeys(ground, mark, ("KEY_INSERT", "KEY_G", "KEY_ESC"))
		assert not reached, f"the test window had {reached}"
		withinBound(figures["the settings dialog again"])


def windows(ground: "Ground", keyboard: "FakeKeyboard") -> dict:
	figures: dict = {}
	acceptance.startListening(ground)
	ground.shell(f"rm -f {OWN_WINDOWS}")
	ground.start("ownWindows", [GUEST_TOOL, "watch", OWN_WINDOWS])
	ground.waitFor(
		lambda: any(r.get("kind") == "ready" for r in ground.readJsonLines(OWN_WINDOWS)),
		"the watcher of new windows",
		30,
	)
	try:
		menu(ground, keyboard, figures)
		dialogs(ground, keyboard, figures)
	finally:
		for job in ("windowEvents", "ownWindows"):
			ground.session({"op": "signal", "name": job, "signal": "SIGTERM"})
	return figures


def nothingLogged(ground: "Ground", keyboard: "FakeKeyboard") -> None:
	with ground.subtest("what was typed is in neither Linda's log nor the helper's journal"):
		ground.activate(WINDOW_TITLE)
		acceptance.typedMarker(ground, keyboard, NVDA_LOG)
		journal = ground.shell(f"journalctl -b -o cat --no-pager -u {KEYD} || true")
		assert not acceptance.holdingMarker({"journal": journal}), "the helper's journal holds what was typed"
		log = ground.shell(f"cat {NVDA_LOG}")
		lines = acceptance.linesHoldingMarker(log)
		ground.machine.log(f"nvda.log: {len(lines)} lines hold the marker whole, the first: {lines[:1]}")
		acceptance.assertLogClean(ground, {"nvda.log": log})


def run(ground: "Ground") -> None:
	ground.openWindow()
	ground.waitForHelper()
	keyboard = ground.uhidKeyboard(UHID)
	startLinda(
		ground, {"keyboard": {"speakTypedCharacters": "1", "speakTypedWords": "1"}}, extra=REMOTE_SECTION
	)
	ground.waitForLindaLog(KERNEL_ROUTE, timeout=30)
	lindaKeyWorks(ground, keyboard)
	figures = {"typing": typing(ground, keyboard)}
	caretScripts(ground, keyboard)
	remoteAccess(ground, keyboard)
	figures["windows"] = windows(ground, keyboard)
	ground.machine.log("AT gnome typing figures " + json.dumps(figures, sort_keys=True))
	nothingLogged(ground, keyboard)


# The guest half.


def _load(name: str, path: str):
	spec = importlib.util.spec_from_file_location(name, path)
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module


def leadInGuest(steps: list[dict]) -> dict:
	remotePeer = _load("remotePeer", os.environ["GROUND_REMOTE_PEER"])
	peer = remotePeer.RemotePeer(REMOTE_PORT, REMOTE_CHANNEL, remotePeer.LEADER)
	heard = []
	try:
		for step in steps:
			if "key" in step:
				peer.key(step["key"], step["pressed"], step.get("extended", False))
			elif "sleep" in step:
				time.sleep(step["sleep"])
			elif "speak" in step:
				mark = peer.mark()
				try:
					peer.waitFor(
						lambda m, text=step["speak"]: (
							m["type"] == "speak" and text in remotePeer.spokenText(m)
						),
						step.get("timeout", 10),
						since=max(0, mark - 50),
					)
					heard.append(True)
				except AssertionError:
					heard.append(False)
	finally:
		peer.close()
	return {"joined": peer.joined.get("type"), "heard": heard}


def watch(out: str) -> None:
	"""Write each window:create and window:activate to out, one JSON object a line, on the guest's
	CLOCK_MONOTONIC, with the window's title and process, so that a window's mapping can be told from
	its activation."""
	import gi

	gi.require_version("Atspi", "2.0")
	from gi.repository import Atspi, GLib

	stream = open(out, "a", buffering=1)  # noqa: SIM115

	def onEvent(event: Atspi.Event) -> None:
		t = time.monotonic()
		try:
			title, pid = event.source.get_name(), event.source.get_process_id()
		except GLib.Error:
			title, pid = "?", 0
		stream.write(json.dumps({"t": t, "type": event.type, "title": title, "pid": pid}) + "\n")

	Atspi.init()
	listener = Atspi.EventListener.new(onEvent)
	for name in ("window:create", "window:activate"):
		listener.register(name)
	stream.write(json.dumps({"kind": "ready", "t": time.monotonic()}) + "\n")
	Atspi.event_main()


if __name__ == "__main__":
	if sys.argv[1:2] == ["lead"] and len(sys.argv) == 3:
		print(json.dumps(leadInGuest(json.loads(sys.argv[2]))))
	elif sys.argv[1:2] == ["watch"] and len(sys.argv) == 3:
		watch(sys.argv[2])
	else:
		sys.exit("usage: wlGnomeTyping.py lead STEPS | watch OUT")
