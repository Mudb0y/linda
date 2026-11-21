"""Linda's acceptance on GNOME, with a real kernel, seat and helper, in one boot: Linda started with no
helper and then given one; its commands and kept keys on a uhid keyboard the helper holds; the
Polish layout's AltGr letters; two input sources switched with Super+Space; the helper killed while a
chord is held; Linda stopped while letters are typed; a switch to a text console and back; a keyboard
that is also a pointer; the emergency chord and the way back by Alt+F2; and nothing typed in any log.

Along the way it runs experiment E8 (Linda holds no X connection, though GNOME runs Xwayland from the
start), the GNOME half of E5 (the layout feed's keymap against the compositor's) and E4 on the guest
(window events against the keys that cause them).
"""

import json
from typing import TYPE_CHECKING

from ground import KERNEL_ROUTE, VIRTUAL_KEYBOARD, WINDOW_TITLE, Mark, keyEvents
from wl import lindaAcceptance as acceptance
from wl.lindaAcceptance import (
	APART,
	HAS_KEYBOARD,
	HINT,
	LET_GO,
	LOST,
	NO_HELPER,
	ONCE,
	STARTED_FROM_PATH,
	UHID,
	UNHEARD,
	WITHOUT_KEYS,
	lindaKeyWorks,
	said,
	saidCount,
	saidOnce,
	typedThrough,
)

if TYPE_CHECKING:
	from ground import FakeKeyboard, Ground

POINTER_KEYBOARD = "Ground pointer keyboard"
KEYD = "linda-keyd.service"
#: AltGr letters of the Polish layout: (GDK's name for what it types, the letter).
ALTGR_LETTERS = {
	"KEY_A": ("aogonek", "\N{LATIN SMALL LETTER A WITH OGONEK}"),
	"KEY_L": ("lstroke", "\N{LATIN SMALL LETTER L WITH STROKE}"),
	"KEY_Z": ("zabovedot", "\N{LATIN SMALL LETTER Z WITH DOT ABOVE}"),
}
OGONEK = ALTGR_LETTERS["KEY_A"][1]
TWO_SOURCES = "[('xkb', 'pl'), ('xkb', 'us')]"
ONE_SOURCE = "[('xkb', 'pl')]"
CONSOLE_TEXT = "/tmp/ground/console.txt"
NVDA_LOG = "/tmp/ground/linda/nvda.log"
OLD_LOG = "/tmp/ground/linda/nvda-old.log"
STOPPED_LETTERS = "abcdefghijklmnop"


def noHelper(ground: "Ground", keyboard: "FakeKeyboard") -> int:
	with ground.subtest("with no helper Linda starts, speaks, and says once that it has no keys"):
		ground.shell(f"systemctl stop {KEYD}")
		ground.startLinda()
		start = Mark(0, 0.0)
		saidOnce(ground, start, NO_HELPER)
		ground.machine.log(f"Linda said: {said(ground, start)}")
		ground.waitForLindaLog(WITHOUT_KEYS)
		for _t, record in ground.lindaLogRecords("by rule"):
			ground.machine.log(f"Linda's log: {record.splitlines()[-1]}")
		assert not ground.lindaLogRecords(KERNEL_ROUTE)
		pid = acceptance.lindaPid(ground)
		acceptance.assertNoXConnection(ground, pid, "with no helper")

	with ground.subtest("systemctl start linda-keyd gives Linda the keyboard, which it says once"):
		mark = ground.mark()
		ground.shell(f"systemctl start {KEYD}")
		_took, record = ground.waitForLindaLog(KERNEL_ROUTE, timeout=30)
		ground.machine.log(f"Linda took the keyboard: {record.splitlines()[-1]}")
		saidOnce(ground, mark, HAS_KEYBOARD)
		assert saidCount(ground, mark, HINT) == 1, said(ground, mark)
		assert saidCount(ground, Mark(0, 0.0), NO_HELPER) == 1
		lindaKeyWorks(ground, keyboard)
		acceptance.assertNoXConnection(ground, pid, "with the helper holding the keyboard")
	return pid


def keptKeys(ground: "Ground", keyboard: "FakeKeyboard") -> None:
	with ground.subtest(
		"Insert and Caps Lock are kept, a double Insert passes one, and Caps Lock's light stays"
	):
		lindaKeyWorks(ground, keyboard, "KEY_INSERT")
		mark = lindaKeyWorks(ground, keyboard, "KEY_CAPSLOCK")
		lit = [r for r in keyboard.records(mark) if r.get("leds", 0) & acceptance.CAPS_LIGHT]
		assert not lit, keyboard.records(mark)
		ground.settle(APART)
		mark = ground.mark()
		keyboard.tap("KEY_INSERT", "KEY_INSERT", hold=0.03, gap=0.1)
		ground.settle(1)
		inserts = [r for r in ground.keys("app", mark) if r["key"] == "KEY_INSERT"]
		virtual = [r for r in ground.keys("evdev", mark, device=VIRTUAL_KEYBOARD) if r["key"] == "KEY_INSERT"]
		ground.machine.log(
			f"a double Insert: the program had {keyEvents(inserts)}, the virtual keyboard {keyEvents(virtual)}"
		)
		assert keyEvents(inserts) == [("KEY_INSERT", 1), ("KEY_INSERT", 0)], inserts
		assert keyEvents(virtual) == [("KEY_INSERT", 1), ("KEY_INSERT", 0)], virtual
		assert not [r for r in keyboard.records(mark) if r.get("leds", 0) & acceptance.CAPS_LIGHT]

	with ground.subtest("letters reach the program through the virtual keyboard and are echoed"):
		for key, letter in (("KEY_A", "a"), ("KEY_B", "b")):
			typedThrough(ground, keyboard, key, letter, echo=letter)
		acceptance.typedMarker(ground, keyboard, NVDA_LOG)


def polish(ground: "Ground", keyboard: "FakeKeyboard") -> None:
	with ground.subtest("the pl layout's AltGr letters are typed, echoed and named"):
		for key, (keyval, letter) in ALTGR_LETTERS.items():
			mark = ground.mark()
			keyboard.chord("KEY_RIGHTALT", key)
			app = ground.waitForKeys(
				"app", [("KEY_RIGHTALT", 1), (key, 1), (key, 0), ("KEY_RIGHTALT", 0)], mark
			)
			assert app[1]["keyval"] == keyval, (key, app)
			ground.waitForSpeech(letter, mark)
		name = acceptance.gestureName(ground, keyboard, "KEY_RIGHTALT", "KEY_A")
		ground.machine.log(f"input help in pl names AltGr with A: {name}")
		assert any(OGONEK in text for text in name), name


def switchTo(ground: "Ground", keyboard: "FakeKeyboard", layout: str) -> None:
	before = acceptance.layoutChanges(ground, layout)
	ground.settle(APART)
	keyboard.chord("KEY_LEFTMETA", "KEY_SPACE")
	mru = []
	try:
		ground.waitFor(
			lambda: acceptance.layoutChanges(ground, layout) > before,
			f"Linda to follow the switch to {layout}",
			10,
		)
	finally:
		mru.append(acceptance.gsettings(ground, "get", "org.gnome.desktop.input-sources", "mru-sources"))
		ground.machine.log(f"E5 after Super+Space to {layout}: mru-sources {mru[0]}")


def layoutAfterSwitch(ground: "Ground", keyboard: "FakeKeyboard", layout: str) -> None:
	name = acceptance.gestureName(ground, keyboard, "KEY_RIGHTALT", "KEY_A")
	ground.machine.log(f"input help in {layout} names AltGr with A: {name}")
	ground.settle(APART)
	mark = ground.mark()
	keyboard.chord("KEY_RIGHTALT", "KEY_A")
	app = ground.waitForKeys(
		"app", [("KEY_RIGHTALT", 1), ("KEY_A", 1), ("KEY_A", 0), ("KEY_RIGHTALT", 0)], mark
	)
	if layout == "pl":
		assert any(OGONEK in text for text in name), name
		assert app[1]["keyval"] == "aogonek", app
		ground.waitForSpeech(OGONEK, mark)
	else:
		# In US English the right Alt is Alt, so AltGr with A is Alt with A, which types no letter.
		assert not any(OGONEK in text for text in name), name
		assert app[1]["keyval"] == "a", app
		ground.settle(0.5)
		assert not any(OGONEK in text for text in said(ground, mark)), said(ground, mark)
	ground.machine.log(
		f"in {layout} AltGr with A reached the program as {app[1]['keyval']}; Linda said {said(ground, mark)}"
	)


def twoSources(ground: "Ground", keyboard: "FakeKeyboard") -> dict:
	figures: dict[str, object] = {}
	with ground.subtest("E5: the feed's keymap against the compositor's, pl alone"):
		figures["pl alone"] = acceptance.logLayouts(ground, "pl alone")

	with ground.subtest("two input sources, pl and us, switched with Super+Space"):
		acceptance.gsettings(ground, "set", "org.gnome.desktop.input-sources", "sources", TWO_SOURCES)
		ground.settle(2)
		mru = acceptance.gsettings(ground, "get", "org.gnome.desktop.input-sources", "mru-sources")
		ground.machine.log(f"E5 with two sources: mru-sources {mru}")
		figures["two sources, before a switch"] = acceptance.logLayouts(
			ground, "two sources, before a switch"
		)
		for layout in ("us", "pl"):
			switchTo(ground, keyboard, layout)
			figures[f"switched to {layout}"] = acceptance.logLayouts(ground, f"switched to {layout}")
			layoutAfterSwitch(ground, keyboard, layout)
		acceptance.gsettings(ground, "set", "org.gnome.desktop.input-sources", "sources", ONE_SOURCE)
	return figures


def helperKilled(ground: "Ground", keyboard: "FakeKeyboard") -> None:
	with ground.subtest("the helper killed while Shift and the Linda key are held"):
		without = len(ground.lindaLogRecords(WITHOUT_KEYS))
		routes = len(ground.lindaLogRecords(KERNEL_ROUTE))
		mark = ground.mark()
		keyboard.run({"press": "KEY_LEFTSHIFT"}, {"sleep": 0.05}, {"press": "KEY_INSERT"})
		ground.waitForKeys("evdev", [("KEY_LEFTSHIFT", 1)], mark, device=VIRTUAL_KEYBOARD)
		ground.settle(0.2)
		ground.shell(f"systemctl kill --signal=SIGKILL {KEYD}")
		ground.waitForKeys(
			"evdev", [("KEY_LEFTSHIFT", 1), ("KEY_LEFTSHIFT", 0)], mark, device=VIRTUAL_KEYBOARD
		)
		ground.waitForLindaLog(WITHOUT_KEYS, count=without + 1)
		keyboard.run({"release": "KEY_INSERT"}, {"sleep": 0.05}, {"release": "KEY_LEFTSHIFT"})
		saidOnce(ground, mark, LOST)
		mark = ground.mark()
		keyboard.tap("KEY_U")
		app = ground.waitForKeys("app", [("KEY_U", 1), ("KEY_U", 0)], mark)
		ground.machine.log(f"the first letter after the kill reached the program as {app[0]['keyval']}")
		assert app[0]["keyval"] == "u", app
		ground.waitForLindaLog(KERNEL_ROUTE, count=routes + 1, timeout=30)
		assert ground.helperRestarts() >= 1
		lindaKeyWorks(ground, keyboard)
		typedThrough(ground, keyboard, "KEY_V", "v", echo="v")


def lindaStopped(ground: "Ground", keyboard: "FakeKeyboard") -> None:
	with ground.subtest(
		"Linda stopped: letters typed from three seconds on reach the program once, in order"
	):
		routes = len(ground.lindaLogRecords(KERNEL_ROUTE))
		mark = ground.mark()
		ground.signalLinda("SIGSTOP")
		stopped = ground.now()
		ground.settle(3)
		steps = [
			s for c in STOPPED_LETTERS for s in ({"tap": f"KEY_{c.upper()}", "hold": 0.02}, {"sleep": 0.1})
		]
		written = keyboard.run(*steps)
		typed = [(r["key"], r["value"]) for r in written]
		app = ground.waitForKeys("app", typed, mark, timeout=10)
		ground.settle(0.5)
		assert keyEvents(ground.keys("app", mark)) == typed, keyEvents(ground.keys("app", mark))
		assert [r["keyval"] for r in app if r["value"] == 1] == list(STOPPED_LETTERS), app
		helper = "\n".join(message for _t, message in ground.helperLog(mark))
		ground.machine.log(
			f"typed {written[0]['t'] - stopped:.2f} to {written[-1]['t'] - stopped:.2f} s after the stop; "
			f"the helper said:\n{helper}"
		)
		mark = ground.mark()
		ground.signalLinda("SIGCONT")
		ground.waitForLindaLog(KERNEL_ROUTE, count=routes + 1, timeout=30)
		lindaKeyWorks(ground, keyboard)
		ground.machine.log(f"once continued Linda said {said(ground, mark)}")
		typedThrough(ground, keyboard, "KEY_Z", "z", echo="z")


def console(ground: "Ground", keyboard: "FakeKeyboard") -> None:
	desktop = int(ground.shell("fgconsole").strip())
	text = next(n for n in (3, 4, 5) if n != desktop)
	ground.shell(
		f"rm -f {CONSOLE_TEXT}; openvt -c {text} -- sh -c 'stty raw -echo; exec cat > {CONSOLE_TEXT}'; "
		f"until [ -e {CONSOLE_TEXT} ]; do sleep 0.05; done"
	)
	without = len(ground.lindaLogRecords(WITHOUT_KEYS))

	with ground.subtest("Control, Alt and a function key, passed by Linda, switch to a text console"):
		mark = ground.mark()
		keyboard.chord("KEY_LEFTCTRL", "KEY_LEFTALT", f"KEY_F{text}")
		ground.waitFor(lambda: ground.shell("fgconsole").strip() == str(text), "the switch to the console")
		ground.waitForLindaLog("linda-keyd let go of the keyboard")
		# Under load the kernel has taken a second to put the console in front (V2-gnome-helper.md).
		ground.settle(1.5)
		mark = ground.mark()
		keyboard.tap("KEY_H", "KEY_I")
		ground.waitFor(lambda: "hi" in ground.shell(f"cat {CONSOLE_TEXT}"), "hi on the console")
		ground.settle(0.5)
		assert not [t for t in said(ground, mark) if t in ("h", "i")], said(ground, mark)

	with ground.subtest("back on the desktop, the helper holds the keyboard again and Linda's keys work"):
		mark = ground.mark()
		ground.shell(f"chvt {desktop}")
		held = ground.waitForHelperLog(f"({UHID}): Held", mark)[0]
		ground.machine.log(f"the helper held the keyboard again {held - mark.t:.3f} s after the switch back")
		lindaKeyWorks(ground, keyboard)
		typedThrough(ground, keyboard, "KEY_J", "j", echo="j")
		assert len(ground.lindaLogRecords(WITHOUT_KEYS)) == without, "Linda lost the route"


def pointer(ground: "Ground", keyboard: "FakeKeyboard") -> None:
	with ground.subtest("a uhid keyboard that is also a pointer is unheard, which Linda says once"):
		mark = ground.mark()
		first = acceptance.pointerKeyboard(ground, POINTER_KEYBOARD)
		ground.waitForHelperLog(f"({POINTER_KEYBOARD})", mark)
		saidOnce(ground, mark, UNHEARD)
		typed = ground.mark()
		typedThrough(ground, first, "KEY_P", "p", through=POINTER_KEYBOARD)
		ground.settle(0.5)
		assert "p" not in said(ground, typed), said(ground, typed)
		second = acceptance.pointerKeyboard(ground, f"{POINTER_KEYBOARD} 2")
		ground.waitForHelperLog(f"({POINTER_KEYBOARD} 2)", mark)
		ground.settle(ONCE)
		assert saidCount(ground, mark, UNHEARD) == 1, said(ground, mark)
		helper = [m for _t, m in ground.helperLog(mark) if POINTER_KEYBOARD in m]
		ground.machine.log(f"the helper said of the pointer keyboards: {helper}")
		first.vanish()
		second.vanish()
		lindaKeyWorks(ground, keyboard)


def emergency(ground: "Ground", keyboard: "FakeKeyboard") -> int:
	with ground.subtest(
		"the emergency chord lets go, Linda says the way back once, and keys reach programs directly"
	):
		mark = ground.mark()
		written = keyboard.run(
			{"press": "KEY_BACKSPACE"},
			{"sleep": 0.05},
			{"press": "KEY_ESC"},
			{"sleep": 0.05},
			{"press": "KEY_ENTER"},
			{"sleep": 0.1},
			{"release": "KEY_ENTER"},
			{"release": "KEY_ESC"},
			{"release": "KEY_BACKSPACE"},
		)
		ground.waitForHelperLog("the emergency chord", mark)
		at = saidOnce(ground, mark, LET_GO)
		enter = next(r for r in written if r["key"] == "KEY_ENTER")
		whole = next(text for text in said(ground, mark) if acceptance.matches(LET_GO, text))
		ground.machine.log(f"Linda said {whole!r} {at - enter['t']:.3f} s after Enter went down")
		# As the synthesizer is given it, with the plus sign spoken.
		assert "Alt plus F 2, type linda" in whole, whole
		mark = ground.mark()
		typedThrough(ground, keyboard, "KEY_K", "k", through=UHID)
		ground.settle(0.5)
		assert not ground.keys("evdev", mark, device=VIRTUAL_KEYBOARD), ground.keys("evdev", mark)
		assert "k" not in said(ground, mark), said(ground, mark)

	with ground.subtest("Alt+F2, linda and Enter start Linda again, and it has the keyboard"):
		old = acceptance.lindaPid(ground)
		ground.shell(f"rm -f {STARTED_FROM_PATH} {OLD_LOG}")
		ground.settle(APART)
		keyboard.chord("KEY_LEFTALT", "KEY_F2")
		ground.settle(1.5)
		keyboard.tap("KEY_L", "KEY_I", "KEY_N", "KEY_D", "KEY_A")
		ground.settle(0.3)
		keyboard.tap("KEY_ENTER")
		ground.waitFor(
			lambda: ground.machine.execute(f"test -s {STARTED_FROM_PATH}")[0] == 0, "linda to be run", 20
		)
		pid = int(ground.shell(f"cat {STARTED_FROM_PATH}").strip())
		ground.waitFor(lambda: not acceptance.processRunning(ground, old), "the old copy to end", 60)
		# The new copy moves the old one's log aside as it starts.
		ground.waitFor(lambda: ground.machine.execute(f"test -e {OLD_LOG}")[0] == 0, "the new copy's log", 60)
		_took, record = ground.waitForLindaLog(KERNEL_ROUTE, timeout=60)
		ground.machine.log(f"the copy Alt+F2 started, {pid}, took the keyboard: {record.splitlines()[-1]}")
		lindaKeyWorks(ground, keyboard)
		typedThrough(ground, keyboard, "KEY_W", "w", echo="w")
		acceptance.assertNoXConnection(ground, pid, "the copy Alt+F2 started")
	return pid


def nothingLogged(ground: "Ground", keyboard: "FakeKeyboard", pid: int) -> None:
	with ground.subtest("what was typed is in neither copy's log nor the helper's journal"):
		acceptance.typedMarker(ground, keyboard, NVDA_LOG)
		ground.shell(f"kill -TERM {pid}")
		ground.waitFor(lambda: not acceptance.processRunning(ground, pid), "Linda to exit", 30)
		journal = ground.shell(f"journalctl -b -o cat --no-pager -u {KEYD} || true")
		assert not acceptance.holdingMarker({"journal": journal}), "the helper's journal holds what was typed"
		logs = {"nvda.log": ground.shell(f"cat {NVDA_LOG}"), "nvda-old.log": ground.shell(f"cat {OLD_LOG}")}
		for name, text in logs.items():
			lines = acceptance.linesHoldingMarker(text)
			ground.machine.log(f"{name}: {len(lines)} lines hold the marker whole, the first: {lines[:1]}")
		acceptance.assertLogClean(ground, logs)
		# ground.main holds nvda.log to a clean log; the first copy's is held to the same here.
		errors = acceptance.logErrors(logs["nvda-old.log"])
		assert not errors, f"the first copy's log holds {len(errors)} records at ERROR or above: {errors}"


def run(ground: "Ground") -> None:
	ground.openWindow()
	ground.waitForHelper()
	keyboard = ground.uhidKeyboard(UHID)
	noHelper(ground, keyboard)
	keptKeys(ground, keyboard)
	polish(ground, keyboard)
	figures = twoSources(ground, keyboard)
	ground.machine.log("E5 figures " + json.dumps(figures, sort_keys=True, ensure_ascii=False))
	helperKilled(ground, keyboard)
	lindaStopped(ground, keyboard)
	console(ground, keyboard)
	pointer(ground, keyboard)
	timings = acceptance.windowTimings(ground, keyboard)
	ground.machine.log("E4 figures " + json.dumps(timings, sort_keys=True))
	ground.activate(WINDOW_TITLE)
	pid = emergency(ground, keyboard)
	nothingLogged(ground, keyboard, pid)
