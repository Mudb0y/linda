"""Experiment E11: Orca running beside Linda on GNOME, with linda-keyd holding the keyboard for Linda.
Do Linda's kept commands still work, is the Insert a double Insert passes on taken by Orca's grab in
the compositor, and does Orca act on keys Linda passes? Linda says once that both will speak.

What Orca saw of each key, and what it said, comes from its own debug output, which it writes as it
handles each key from the compositor; Orca's is the only log a typed key reaches here, and it stays
in the guest.
"""

import json
import re
from typing import TYPE_CHECKING

from ground import KERNEL_ROUTE, VIRTUAL_KEYBOARD, Mark, keyEvents
from wl import lindaAcceptance as acceptance
from wl.lindaAcceptance import APART, UHID, lindaKeyWorks, said, saidOnce

if TYPE_CHECKING:
	from ground import Ground

ORCA_DEBUG = "/tmp/ground/orca.debug"
ORCA_SERVICE = "org.gnome.Orca1.Service"
#: Linda's notice as the synthesizer is given it, with the plus signs spoken.
NOTICE = "Orca is running too, so both will speak.\N{HORIZONTAL ELLIPSIS}"
#: input_event.py's record of each key event Orca is given: its type and its keysym's name.
ORCA_KEY = re.compile(r"KEYBOARD_EVENT:\s+type=(\S+)\n(?:.*\n){0,5}?\s*keyval_name='([^']*)'")
ORCA_SPEECH = re.compile(r"SPEECH OUTPUT: '([^']*)'")


def orcaSize(ground: "Ground") -> int:
	return int(ground.shell(f"stat -c %s {ORCA_DEBUG} 2>/dev/null || echo 0").strip())


def orcaSince(ground: "Ground", offset: int) -> dict:
	"""What Orca handled and said since offset in its debug output."""
	text = ground.shell(f"tail -c +{offset + 1} {ORCA_DEBUG} 2>/dev/null || true")
	return {
		"keys": ORCA_KEY.findall(text),
		"speech": ORCA_SPEECH.findall(text),
		"commands": re.findall(r"COMMAND: (.*)", text),
	}


def orcaHas(ground: "Ground") -> bool:
	answer = ground.run(
		[
			"gdbus",
			"call",
			"--session",
			"--dest",
			"org.freedesktop.DBus",
			"--object-path",
			"/org/freedesktop/DBus",
			"--method",
			"org.freedesktop.DBus.NameHasOwner",
			ORCA_SERVICE,
		]
	)
	return answer.returncode == 0 and "true" in answer.stdout


def keysOf(orca: dict, name: str) -> list[str]:
	return [kind for kind, key in orca["keys"] if key == name]


def run(ground: "Ground") -> None:
	figures: dict[str, object] = {}
	ground.openWindow()
	ground.waitForHelper()
	keyboard = ground.uhidKeyboard(UHID)

	with ground.subtest("Orca runs in the session"):
		ground.start("orca", ["orca", "--replace", "--debug-file", ORCA_DEBUG])
		ground.waitFor(lambda: orcaHas(ground), "Orca to take its name on the session bus", 60)
		ground.settle(3)
		figures["orcaVersion"] = ground.run(["orca", "--version"]).stdout.strip()

	with ground.subtest("Linda starts beside it, takes the keyboard, and says once that both will speak"):
		ground.startLinda()
		ground.waitForLindaLog(KERNEL_ROUTE, timeout=30)
		start = Mark(0, 0.0)
		saidOnce(ground, start, NOTICE)
		figures["notice"] = next(t for t in said(ground, start) if acceptance.matches(NOTICE, t))

	with ground.subtest("Linda's kept commands work, and Orca hears neither key"):
		offset = orcaSize(ground)
		for nvdaKey in ("KEY_INSERT", "KEY_CAPSLOCK"):
			lindaKeyWorks(ground, keyboard, nvdaKey)
		# A letter Linda passes, which shows that Orca's record of keys is being read at all.
		mark = ground.mark()
		keyboard.tap("KEY_E")
		ground.waitForKeys("app", [("KEY_E", 1), ("KEY_E", 0)], mark)
		ground.settle(1)
		orca = orcaSince(ground, offset)
		figures["keptCommands"] = orca
		assert keysOf(orca, "e"), orca
		assert not keysOf(orca, "Insert") and not keysOf(orca, "t") and not keysOf(orca, "Caps_Lock"), orca

	with ground.subtest("the Insert a double Insert passes on"):
		ground.settle(APART)
		offset = orcaSize(ground)
		mark = ground.mark()
		keyboard.tap("KEY_INSERT", "KEY_INSERT", hold=0.03, gap=0.1)
		ground.settle(1.5)
		virtual = [r for r in ground.keys("evdev", mark, device=VIRTUAL_KEYBOARD) if r["key"] == "KEY_INSERT"]
		app = [r for r in ground.keys("app", mark) if r["key"] == "KEY_INSERT"]
		orca = orcaSince(ground, offset)
		figures["doubleInsert"] = {
			"virtualKeyboard": keyEvents(virtual),
			"program": keyEvents(app),
			"orca": orca,
			"lindaSaid": said(ground, mark),
		}
		ground.machine.log(f"E11 a double Insert: {figures['doubleInsert']}")
		assert keyEvents(virtual) == [("KEY_INSERT", 1), ("KEY_INSERT", 0)], virtual

	with ground.subtest("letters Linda passes reach the program, and what Orca does with them"):
		ground.settle(APART)
		offset = orcaSize(ground)
		mark = ground.mark()
		keyboard.tap("KEY_O", "KEY_R")
		app = ground.waitForKeys("app", [("KEY_O", 1), ("KEY_O", 0), ("KEY_R", 1), ("KEY_R", 0)], mark)
		ground.waitForSpeech("o", mark)
		ground.settle(1.5)
		orca = orcaSince(ground, offset)
		figures["passedLetters"] = {
			"program": [r["keyval"] for r in app if r["value"] == 1],
			"orca": orca,
			"lindaSaid": said(ground, mark),
		}
		ground.machine.log(f"E11 letters Linda passed: {figures['passedLetters']}")

	with ground.subtest("Orca's own command keys, which Linda keeps or passes as its own layout says"):
		# Orca's Insert with H, its learn mode: Linda takes Insert chords for its own.
		ground.settle(APART)
		offset = orcaSize(ground)
		mark = ground.mark()
		keyboard.chord("KEY_INSERT", "KEY_H")
		ground.settle(1.5)
		figures["insertH"] = {
			"orca": orcaSince(ground, offset),
			"lindaSaid": said(ground, mark),
			"program": keyEvents(ground.keys("app", mark)),
		}
		ground.machine.log(f"E11 Insert with H: {figures['insertH']}")
		if figures["insertH"]["orca"]["commands"]:
			keyboard.tap("KEY_ESC")
		lindaKeyWorks(ground, keyboard)

	ground.machine.log("E11 figures " + json.dumps(figures, sort_keys=True, ensure_ascii=False))
	ground.session({"op": "signal", "name": "orca", "signal": "SIGTERM"})
