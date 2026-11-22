"""The delivery watch in the sway guest, with the real helper and a uhid keyboard it holds: sway listing
linda-keyd's keyboard with its events on proves that the keys Linda passes on reach the desktop, which
is remembered, so that a second start says no rescue hint. A switch to a text console and back, through
which sway takes every device away, is no word on the keys' delivery. With swaymsg switching that
keyboard's events off, Linda lets go within a second, says so, and typing reaches the program through
the real keyboard; nothing asks the helper again for ten seconds, linda --take-keyboard taken while the
events are still off lets go again, and taken once they are on gives Linda its keys back. Under
caps:escape, Linda's layout knowing it or not, and under ctrl:nocaps, twenty Caps Locks passed on prove
nothing. A layout sway gives linda-keyd's keyboard alone is said once. Nothing typed is in Linda's log,
the helper's journal or the record.
"""

import re
import shlex
from typing import TYPE_CHECKING

from ground import (
	BASE_SETTINGS,
	KERNEL_ROUTE,
	LINDA_DIRECTORY,
	TRANSCRIPT,
	USER,
	VIRTUAL_KEYBOARD,
	WINDOW_TITLE,
	Mark,
)
from wl import lindaAcceptance as acceptance
from wl.lindaAcceptance import APART, HINT, UHID, lindaKeyWorks, said, saidCount, saidOnce, typedThrough

if TYPE_CHECKING:
	from ground import FakeKeyboard, Ground

#: sway's identifier of linda-keyd's keyboard (swayLayout.VIRTUAL_IDENTIFIER).
IDENTIFIER = "19561:28260:Linda_virtual_keyboard"
ABSENT = (
	"Keys Linda passes on do not reach the desktop, so Linda has let go of the keyboard. "
	"Start Linda again once that is put right."
)
HAS_KEYBOARD_AGAIN = "Linda has the keyboard again"
OTHER_LAYOUT = "The desktop gives linda-keyd's keyboard another layout than your keyboard."
#: What Linda's log says of each proof, and of a start that leaves the hint out.
#: Any proof: the helper's light probe gives the first one now, and sway's listing the later ones.
PRESENT = "Keys NVDA passes on reach the desktop: "
DISABLED = "Keys NVDA passes on do not reach the desktop: sway takes no events from linda-keyd's keyboard"
NOT_HINTED = "The rescue hint is not said: keys passed on have reached this desktop before"
UNANSWERED = re.compile(r"Key delivery: \d+ lock keys passed on running were not answered")
DECLINED = "lock keys passed on running were not answered, which proves nothing"
#: The helper's journal as a client is admitted, and as it lets go when its client goes.
ADMITTED = "admitted process "
HELPER_LET_GO = "let go of every keyboard: the client went"
HELPER_BACKGROUND = "let go of every keyboard: Background"
ASKED_AGAIN = "Key delivery: sway lists no keyboard of libinput's yet, and is asked again"
#: Linda's state directory in the guest, and the record of proofs in it.
RECORD = f"/home/{USER}/.local/state/linda/keyDelivery"
NVDA_LOG = f"{LINDA_DIRECTORY}/nvda.log"
OLD_LOG = f"{LINDA_DIRECTORY}/nvda-old.log"
#: Caps Lock presses passed on under each remap, each pair further apart than the echo's wait.
CAPS_PRESSES = 20
CAPS_GAP = 0.7
#: The Linda keys without Caps Lock, which is 1: the numpad's Insert, 2, and Insert, 4; so that a Caps
#: Lock Linda's layout takes for one is passed on.
WITHOUT_CAPS = {"keyboard": {"NVDAModifierKeys": "6"}}


def swaymsg(ground: "Ground", *arguments: str) -> str:
	done = ground.run(["swaymsg", "-s", ground.guest["swaysock"], *arguments])
	assert done.returncode == 0, done
	return done.stdout


def takeKeyboard(ground: "Ground") -> str:
	done = ground.run([f"{ground.linda}/bin/linda", "--take-keyboard"])
	assert done.returncode == 0, done
	return done.stdout.strip()


def startLinda(ground: "Ground", settings: dict | None = None, env: dict[str, str] | None = None) -> Mark:
	"""Start Linda as ground.startLinda does, with settings over BASE_SETTINGS and env beside the test
	hooks', after the logs of any copy before it are put away; answers the mark of its start, once its
	log says the helper holds the keyboard for it."""
	merged = {section: dict(values) for section, values in BASE_SETTINGS.items()}
	for section, values in (settings or {}).items():
		merged.setdefault(section, {}).update(values)
	lines = ["schemaVersion = 25"]
	for section, values in merged.items():
		lines.append(f"[{section}]")
		lines += [f"\t{key} = {value}" for key, value in values.items()]
	config = f"{LINDA_DIRECTORY}/config"
	ground.machine.succeed(
		f"install -d -o {USER} {LINDA_DIRECTORY} {config}",
		f"echo {shlex.quote(chr(10).join(lines))} > {config}/nvda.ini",
		f"chown {USER} {config}/nvda.ini",
		f"rm -f {NVDA_LOG} {OLD_LOG}",
	)
	mark = ground.mark()
	ground.start(
		"linda",
		[f"{ground.linda}/bin/linda", "-c", config, "-f", NVDA_LOG, "--debug-logging"],
		env={
			"LINDA_TEST_HOOKS_DIR": f"{ground.linda}/share/linda/tests/linux/testHooks",
			"LINDA_TRANSCRIPT_FILE": TRANSCRIPT,
			"LINDA_TRANSCRIPT_MODE": "immediate",
			**(env or {}),
		},
	)
	ground.lindaStarted = True
	ground.waitForSpeech(WINDOW_TITLE, mark, timeout=120)
	ground.waitForLindaLog(KERNEL_ROUTE, timeout=60)
	return mark


def restart(ground: "Ground", settings: dict | None = None, env: dict[str, str] | None = None) -> Mark:
	ground.stopLinda()
	return startLinda(ground, settings, env)


def helperSince(ground: "Ground", mark: Mark, text: str) -> list[float]:
	return [t for t, line in ground.helperLog(mark) if text in line]


def firstStart(ground: "Ground", keyboard: "FakeKeyboard") -> None:
	with ground.subtest("the first start says no hint: the helper's light probe is a proof, remembered"):
		start = startLinda(ground)
		ground.waitForLindaLog(PRESENT)
		ground.settle(2)
		assert saidCount(ground, start, HINT) == 0, said(ground, start)
		record = ground.shell(f"cat {RECORD}")
		ground.machine.log(f"the record of proofs: {record!r}")
		assert re.fullmatch(r"sway \d{4}-\d\d-\d\d( probed)?\n", record), record
		lindaKeyWorks(ground, keyboard)
		typedThrough(ground, keyboard, "KEY_B", "b", echo="b")
		acceptance.typedMarker(ground, keyboard, NVDA_LOG)
		assert not acceptance.holdingMarker({"the record": ground.shell(f"cat {RECORD}")})


def consoleSwitch(ground: "Ground", keyboard: "FakeKeyboard") -> None:
	# Last of all: this guest's sway lists no input at all after a switch back, for forty seconds and more,
	# whether or not Linda holds the keyboard, so typing that reaches the program is not looked for after
	# it, and nothing that needs sway's inputs can follow it.
	with ground.subtest(
		"to a text console and back: the helper lets go and holds again, Linda keeps it and says nothing of "
		"the keys' delivery while sway lists no input, and its own keys work"
	):
		desktop = int(ground.shell("fgconsole").strip())
		text = next(n for n in (3, 4, 5) if n != desktop)
		asked = len(ground.lindaLogRecords(ASKED_AGAIN))
		mark = ground.mark()
		ground.shell(f"chvt {text}")
		ground.waitForHelperLog(HELPER_BACKGROUND, mark)
		ground.settle(3)
		back = ground.mark()
		ground.shell(f"chvt {desktop}")
		held = ground.waitForHelperLog(f"({UHID}): Held", back, timeout=15)[0]
		ground.machine.log(f"the helper held the keyboard again {held - back.t:.3f} s after the switch back")
		ground.waitFor(
			lambda: len(ground.lindaLogRecords(ASKED_AGAIN)) > asked, "sway's empty listing set aside", 10
		)
		ground.settle(3)
		# With no window focused in sway any more, the title Insert+T reads is the desktop's.
		keys = ground.mark()
		keyboard.chord("KEY_INSERT", "KEY_T")
		ground.waitForSpeech("Desktop", keys)
		assert saidCount(ground, mark, ABSENT) == 0, said(ground, mark)
		assert not ground.lindaLogRecords("do not reach the desktop")
		assert not helperSince(ground, mark, HELPER_LET_GO), ground.helperLog(mark)


def eventsOff(ground: "Ground", keyboard: "FakeKeyboard") -> None:
	with ground.subtest(
		"with sway taking no events from linda-keyd's keyboard, Linda lets go within a second, says so, "
		"and typing reaches the program through the real keyboard"
	):
		mark = ground.mark()
		swaymsg(ground, "input", IDENTIFIER, "events", "disabled")
		letGo, _line = ground.waitForHelperLog(HELPER_LET_GO, mark)
		at = saidOnce(ground, mark, ABSENT)
		ground.machine.log(
			f"events off: the helper let go {letGo - mark.t:.3f} s after swaymsg was asked, and Linda said so "
			f"{at - mark.t:.3f} s after"
		)
		assert letGo - mark.t <= 1.0, letGo - mark.t
		ground.waitForLindaLog(DISABLED)
		typed = ground.mark()
		typedThrough(ground, keyboard, "KEY_X", "x", through=UHID)
		ground.settle(0.5)
		assert not ground.keys("evdev", typed, device=VIRTUAL_KEYBOARD), ground.keys("evdev", typed)

	with ground.subtest("nothing asks the helper for ten seconds after the let-go"):
		admitted = len(ground.lindaLogRecords("linda-keyd admitted NVDA"))
		ground.settle(10)
		assert not helperSince(ground, mark, ADMITTED), ground.helperLog(mark)
		assert len(ground.lindaLogRecords("linda-keyd admitted NVDA")) == admitted

	with ground.subtest("linda --take-keyboard while the events are still off: held, then let go again"):
		mark = ground.mark()
		ground.machine.log(f"linda --take-keyboard said: {takeKeyboard(ground)}")
		held, _line = ground.waitForHelperLog(f"({UHID}): Held", mark, timeout=15)
		letGo = ground.waitFor(
			lambda: [t for t in helperSince(ground, mark, HELPER_LET_GO) if t >= held],
			"the second let-go",
			15,
		)[0]
		saidOnce(ground, mark, ABSENT)
		ground.machine.log(
			f"taken back with the events off: let go {letGo - held:.3f} s after the helper held"
		)
		assert letGo - held <= 1.6, letGo - held

	with ground.subtest("events on again, linda --take-keyboard gives Linda its keys back"):
		swaymsg(ground, "input", IDENTIFIER, "events", "enabled")
		mark = ground.mark()
		takeKeyboard(ground)
		saidOnce(ground, mark, HAS_KEYBOARD_AGAIN, timeout=20)
		ground.waitFor(lambda: len(ground.lindaLogRecords(PRESENT)) >= 2, "the listing's proof again", 10)
		lindaKeyWorks(ground, keyboard)
		typedThrough(ground, keyboard, "KEY_V", "v", echo="v")
		assert saidCount(ground, mark, ABSENT) == 0, said(ground, mark)


def capsPresses(ground: "Ground", keyboard: "FakeKeyboard", keyval: str) -> None:
	"""CAPS_PRESSES Caps Locks typed, each reaching the program as keyval, with Linda keeping the
	keyboard throughout."""
	mark = ground.mark()
	keyboard.tap(*["KEY_CAPSLOCK"] * CAPS_PRESSES, gap=CAPS_GAP)
	ground.settle(1)
	presses = [r for r in ground.keys("app", mark) if r["key"] == "KEY_CAPSLOCK" and r["value"] == 1]
	assert [r["keyval"] for r in presses] == [keyval] * CAPS_PRESSES, presses
	assert saidCount(ground, mark, ABSENT) == 0, said(ground, mark)
	assert not helperSince(ground, mark, HELPER_LET_GO), ground.helperLog(mark)
	lindaKeyWorks(ground, keyboard)


def remaps(ground: "Ground", keyboard: "FakeKeyboard") -> None:
	with ground.subtest("started again with the proof remembered, Linda says no hint"):
		start = restart(ground, WITHOUT_CAPS)
		ground.settle(3)
		assert saidCount(ground, start, HINT) == 0, said(ground, start)
		assert ground.lindaLogRecords(NOT_HINTED)
		lindaKeyWorks(ground, keyboard)

	with ground.subtest(
		"sway's caps:escape, which Linda's layout does not know: twenty Caps Locks passed on prove nothing"
	):
		# Caps Lock on and off, each answered by the helper's LOCKS, so that the light counts.
		keyboard.tap("KEY_CAPSLOCK", "KEY_CAPSLOCK", gap=CAPS_GAP)
		ground.settle(1)
		swaymsg(ground, "input", "type:keyboard", "xkb_options", "caps:escape")
		ground.settle(1)
		capsPresses(ground, keyboard, "Escape")
		declined = [record for _t, record in ground.lindaLogRecords(DECLINED)]
		ground.machine.log(f"Linda's layout without caps:escape: {declined}")
		# Once for the hold, and for the layout, which on sway cannot hold sway's own options.
		assert len(declined) == 1, declined
		assert "NVDA's layout comes from " in declined[0], declined

	with ground.subtest("Linda's layout with caps:escape: twenty Caps Locks are no lock keys"):
		# Caps Lock's key stays a Linda key by where it is whatever it types, and would be kept.
		restart(ground, WITHOUT_CAPS, {"XKB_DEFAULT_OPTIONS": "caps:escape"})
		capsPresses(ground, keyboard, "Escape")
		assert not UNANSWERED.search(ground.lindaLog())

	with ground.subtest("under ctrl:nocaps, twenty Caps Locks are no lock keys"):
		swaymsg(ground, "input", "type:keyboard", "xkb_options", "ctrl:nocaps")
		restart(ground, WITHOUT_CAPS, {"XKB_DEFAULT_OPTIONS": "ctrl:nocaps"})
		capsPresses(ground, keyboard, "Control_L")
		assert not UNANSWERED.search(ground.lindaLog())


def otherLayout(ground: "Ground", keyboard: "FakeKeyboard") -> None:
	with ground.subtest("a layout sway gives linda-keyd's keyboard alone is said once"):
		mark = ground.mark()
		swaymsg(ground, "input", IDENTIFIER, "xkb_layout", "us")
		saidOnce(ground, mark, OTHER_LAYOUT)
		swaymsg(ground, "input", IDENTIFIER, "xkb_layout", "pl")
		ground.settle(acceptance.ONCE)
		assert saidCount(ground, mark, OTHER_LAYOUT) == 1, said(ground, mark)


def nothingLogged(ground: "Ground", keyboard: "FakeKeyboard") -> None:
	with ground.subtest("nothing typed is in the helper's journal, and the record names sway alone"):
		journal = ground.shell("journalctl -b -o cat --no-pager -u linda-keyd.service || true")
		assert not acceptance.holdingMarker({"journal": journal}), "the helper's journal holds what was typed"
		record = ground.shell(f"cat {RECORD}")
		assert re.fullmatch(r"sway \d{4}-\d\d-\d\d\n", record), record


def run(ground: "Ground") -> None:
	ground.openWindow()
	ground.waitForHelper()
	keyboard = ground.uhidKeyboard(UHID)
	ground.settle(APART)
	firstStart(ground, keyboard)
	eventsOff(ground, keyboard)
	remaps(ground, keyboard)
	otherLayout(ground, keyboard)
	nothingLogged(ground, keyboard)
	consoleSwitch(ground, keyboard)
