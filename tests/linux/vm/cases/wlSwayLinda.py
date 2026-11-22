"""Linda's acceptance on sway, with a real kernel, seat and helper, in one boot: what wlGnomeLinda holds
Linda to under GNOME, here in the sway guest's session with Xwayland on demand, and Linda's own
windows reached from the keyboard. Linda is started with no helper and then given one; its commands
and kept keys work on a uhid keyboard the helper holds; the Polish layout's AltGr letters are typed,
echoed and named; two layouts are switched with Super+Space, bound for the case to sway's own switch;
the helper is killed while a chord is held; Linda is stopped while letters are typed; a text console
is switched to and back; a keyboard that is also a pointer is left alone; the menu opens with the
Linda key and N, a dialog opens with Linda's command and is brought forward again from behind another
window; the emergency chord lets go, and linda --take-keyboard typed in a terminal, which is sway's
way back as Linda says it, gives Linda the keyboard again; and nothing typed is in any log.

Where Linda's window is not given the keyboard, the case asserts that Linda says how to reach it and
writes the limitation down after "AT limitation".
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

DESKTOP = "sway"
POINTER_KEYBOARD = "Ground pointer keyboard"
KEYD = "linda-keyd.service"
KEYD_LET_GO = "linda-keyd let go of the keyboard"
#: AltGr letters of the Polish layout: (GDK's name for what it types, the letter).
ALTGR_LETTERS = {
	"KEY_A": ("aogonek", "\N{LATIN SMALL LETTER A WITH OGONEK}"),
	"KEY_L": ("lstroke", "\N{LATIN SMALL LETTER L WITH STROKE}"),
	"KEY_Z": ("zabovedot", "\N{LATIN SMALL LETTER Z WITH DOT ABOVE}"),
}
OGONEK = ALTGR_LETTERS["KEY_A"][1]
#: What Linda logs as it follows the desktop's layout, before the layout's name.
LAYOUT_LOG = "Keyboard layout from sway: "
CONSOLE_TEXT = "/tmp/ground/console.txt"
NVDA_LOG = "/tmp/ground/linda/nvda.log"
STOPPED_LETTERS = "abcdefghijklmnop"
#: Seconds after the helper holds again on the switch back from a console before a key is typed: sway
#: loses keys from the virtual keyboard for up to some 0.9 s after the grab (E15-sway.md), which
#: package HL's hold is to end.
RESETTLE = 1.0
#: The two faults the switch back from a text console meets on sway, held so that their going is seen.
LAYOUT_AFTER_CONSOLE = (
	"sway lists no keyboard while a text console is in front, so Linda's sway feed takes localed's "
	"layout, and it does not take sway's layout again when the session is back in front"
)
FOCUS_AFTER_CONSOLE = (
	"under sway the focused window says it is deactivated as a text console comes in front, and no "
	"window says it is activated when the session is in front again, a new one included, so Linda's "
	"focus stays on the desktop"
)

#: Linda's own windows, as their titles are and as the transcript has what Linda says of them.
MENU_TITLE = "Linda"
SETTINGS_TITLE = "Linda Settings"
SETTINGS_SPOKEN = "Linda Settings: General (normal configuration) dialog"
NOT_REACHED = "Linda's window opened but did not get the keyboard. Press Alt plus Tab to reach it."
#: Seconds from the key to the window's activation, as the package asks of the menu window.
ACTIVATION_BOUND = 1.0
#: Seconds a window of Linda's is given to be activated at all before the case takes it as refused.
REFUSED_AFTER = 5.0
#: The way back sway's line gives, typed in a terminal, and the terminal, by its application ID.
TAKE_KEYBOARD = "linda --take-keyboard"
TERMINAL = "foot"


# What is said and heard, by containment: a tree item, a dialog and its first control may come in one
# speak record or several.


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


# AG's subtests, as wlGnomeLinda runs them.


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


def swaymsg(ground: "Ground", command: str) -> str:
	answer = ground.run(["swaymsg", command])
	assert answer.returncode == 0, answer
	return answer.stdout.strip()


def layoutChanges(ground: "Ground", layout: str) -> int:
	return len(ground.lindaLogRecords(f"{LAYOUT_LOG}{layout}\n"))


def switchTo(ground: "Ground", keyboard: "FakeKeyboard", layout: str) -> None:
	before = layoutChanges(ground, layout)
	ground.settle(APART)
	keyboard.chord("KEY_LEFTMETA", "KEY_SPACE")
	try:
		ground.waitFor(
			lambda: layoutChanges(ground, layout) > before, f"Linda to follow the switch to {layout}", 10
		)
	finally:
		keyboards = ground.focus("facts")["keyboards"]
		ground.machine.log(f"after Super+Space to {layout}, sway's keyboards: {json.dumps(keyboards)}")


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
		# sway keeps the right Alt key as AltGr with US English beside Polish, and A's third level
		# there is a, which the program gets; Linda, compiling the same names, names and echoes it so.
		assert not any(OGONEK in text for text in name), name
		assert "a" in name, name
		assert app[1]["keyval"] == "a", app
		ground.waitForSpeech("a", mark, timeout=3)
		assert not any(OGONEK in text for text in said(ground, mark)), said(ground, mark)
	ground.machine.log(
		f"in {layout} AltGr with A reached the program as {app[1]['keyval']}; Linda said {said(ground, mark)}"
	)


def twoLayouts(ground: "Ground", keyboard: "FakeKeyboard") -> None:
	with ground.subtest("two layouts, pl and us, switched with Super+Space"):
		swaymsg(ground, "bindsym Mod4+space input type:keyboard xkb_switch_layout next")
		swaymsg(ground, "input type:keyboard xkb_layout 'pl,us'")
		ground.settle(2)
		ground.machine.log(f"with pl,us, sway's keyboards: {json.dumps(ground.focus('facts')['keyboards'])}")
		for layout in ("us", "pl"):
			switchTo(ground, keyboard, layout)
			layoutAfterSwitch(ground, keyboard, layout)
		swaymsg(ground, "input type:keyboard xkb_layout pl")
		swaymsg(ground, "unbindsym Mod4+space")
		ground.settle(1)
		typedThrough(ground, keyboard, "KEY_Q", "q", echo="q")


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
	letGo = len(ground.lindaLogRecords(KEYD_LET_GO))

	with ground.subtest("Control, Alt and a function key, passed by Linda, switch to a text console"):
		keyboard.chord("KEY_LEFTCTRL", "KEY_LEFTALT", f"KEY_F{text}")
		ground.waitFor(lambda: ground.shell("fgconsole").strip() == str(text), "the switch to the console")
		ground.waitForLindaLog(KEYD_LET_GO, count=letGo + 1)
		# Under load the kernel has taken a second to put the console in front (V2-gnome-helper.md).
		ground.settle(1.5)
		mark = ground.mark()
		keyboard.tap("KEY_H", "KEY_I")
		ground.waitFor(lambda: "hi" in ground.shell(f"cat {CONSOLE_TEXT}"), "hi on the console")
		ground.settle(0.5)
		assert not [t for t in said(ground, mark) if t in ("h", "i")], said(ground, mark)

	with ground.subtest("back on the desktop, the helper holds the keyboard again and Linda's keys work"):
		layouts = layoutChanges(ground, "pl")
		mark = ground.mark()
		ground.shell(f"chvt {desktop}")
		held = ground.waitForHelperLog(f"({UHID}): Held", mark)[0]
		ground.machine.log(f"the helper held the keyboard again {held - mark.t:.3f} s after the switch back")
		ground.settle(RESETTLE)
		with ground.expectedFailure(LAYOUT_AFTER_CONSOLE):
			ground.waitFor(
				lambda: layoutChanges(ground, "pl") > layouts, "Linda to follow sway's layout again", 5
			)
		with ground.expectedFailure(FOCUS_AFTER_CONSOLE):
			lindaKeyWorks(ground, keyboard)
		ground.machine.log(
			f"after the switch back Linda said {said(ground, mark)}; the window events: "
			f"{acceptance.windowEvents(ground, mark.t)}; Linda's layout: "
			f"{[r.splitlines()[-1] for _t, r in ground.lindaLogRecords('Keyboard layout from')][-3:]}"
		)
		ground.machine.log(f"after the switch back sway's windows: {ground.focus('windows')}")
		# Nothing the case tried brought a window's activation back: neither a new window (run 3) nor
		# another workspace and back (run 4). So this subtest is the case's last, and what still works is
		# written down: whether a letter reaches the program, and whether Linda still has the route.
		typed = ground.mark()
		keyboard.tap("KEY_J")
		ground.settle(1)
		ground.machine.log(
			f"after the switch back J reached the program as {keyEvents(ground.keys('app', typed))}, the "
			f"virtual keyboard as {keyEvents(ground.keys('evdev', typed, device=VIRTUAL_KEYBOARD))}; Linda "
			f"said {said(ground, typed)}"
		)
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
		figures[what] = {"activated": delay}
		ground.machine.log(f"{what}: activated {delay} s after the key")
		assert delay < ACTIVATION_BOUND, f"{what} was activated {delay} s after the key"
		assert NOT_REACHED not in said(ground, mark), said(ground, mark)
		return True
	saidOnce(ground, mark, NOT_REACHED)
	figures[what] = {"activated": None, "said": NOT_REACHED}
	ground.machine.log(
		f"AT limitation on {DESKTOP}: {what} was not given the keyboard within {REFUSED_AFTER} s, and Linda "
		f"said {NOT_REACHED!r} once; the windows: {acceptance.windowEvents(ground, pressed)}"
	)
	return False


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
			("KEY_RIGHT", "View log 1 of 6"),
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
		assert not appKeys(ground, mark, ("KEY_INSERT", "KEY_G", "KEY_ESC")), appKeys(
			ground, mark, ("KEY_INSERT", "KEY_G", "KEY_ESC")
		)


def windows(ground: "Ground", keyboard: "FakeKeyboard") -> dict:
	figures: dict = {}
	menu(ground, keyboard, figures)
	dialogs(ground, keyboard, figures)
	ground.session({"op": "signal", "name": "windowEvents", "signal": "SIGTERM"})
	return figures


# The way back.


def emergency(ground: "Ground", keyboard: "FakeKeyboard") -> None:
	with ground.subtest(
		"the emergency chord lets go, Linda says the way back once, and keys reach programs directly"
	):
		ground.activate(WINDOW_TITLE)
		ground.settle(APART)
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
		assert "start Linda again, or run linda" in whole, whole
		assert "Alt plus F 2" not in whole, whole
		mark = ground.mark()
		typedThrough(ground, keyboard, "KEY_K", "k", through=UHID)
		ground.settle(0.5)
		assert not ground.keys("evdev", mark, device=VIRTUAL_KEYBOARD), ground.keys("evdev", mark)
		assert "k" not in said(ground, mark), said(ground, mark)

	with ground.subtest("linda --take-keyboard typed in a terminal gives Linda the keyboard again"):
		pid = acceptance.lindaPid(ground)
		routes = len(ground.lindaLogRecords(KERNEL_ROUTE))
		# The installed launcher, as a user's PATH has it; the case's linda on PATH adds the test's
		# settings, which --take-keyboard refuses beside it.
		path = f"{ground.linda}/bin:/run/wrappers/bin:/run/current-system/sw/bin"
		ground.start("terminal", ["foot", "sh"], env={"PATH": path})
		ground.waitFor(
			lambda: (ground.activeWindow() or {}).get("application") == TERMINAL,
			"the terminal to have the keyboard",
			30,
		)
		ground.settle(1)
		mark = ground.mark()
		keys = []
		for c in TAKE_KEYBOARD:
			keys.append({"-": "KEY_MINUS", " ": "KEY_SPACE"}.get(c, f"KEY_{c.upper()}"))
		keyboard.tap(*keys, "KEY_ENTER")
		_took, record = ground.waitForLindaLog(KERNEL_ROUTE, count=routes + 1, timeout=30)
		ground.machine.log(f"Linda took the keyboard back: {record.splitlines()[-1]}")
		ground.settle(ONCE)
		ground.machine.log(f"as it took the keyboard back Linda said {said(ground, mark)}")
		assert acceptance.lindaPid(ground) == pid
		ground.session({"op": "signal", "name": "terminal", "signal": "SIGTERM"})
		ground.waitFor(lambda: not ground.running("terminal"), "the terminal to close")
		ground.activate(WINDOW_TITLE)
		lindaKeyWorks(ground, keyboard)
		typedThrough(ground, keyboard, "KEY_W", "w", echo="w")
		acceptance.assertNoXConnection(ground, pid, "after the way back")


def nothingLogged(ground: "Ground", keyboard: "FakeKeyboard") -> None:
	with ground.subtest("what was typed is in neither Linda's log nor the helper's journal"):
		acceptance.typedMarker(ground, keyboard, NVDA_LOG)
		journal = ground.shell(f"journalctl -b -o cat --no-pager -u {KEYD} || true")
		assert not acceptance.holdingMarker({"journal": journal}), "the helper's journal holds what was typed"
		log = ground.shell(f"cat {NVDA_LOG}")
		lines = acceptance.linesHoldingMarker(log)
		ground.machine.log(f"nvda.log: {len(lines)} lines hold the marker whole, the first: {lines[:1]}")
		acceptance.assertLogClean(ground, {"nvda.log": log})


def run(ground: "Ground") -> None:
	ground.machine.log(f"the desktop: {json.dumps(ground.focus('facts'))}")
	ground.openWindow()
	acceptance.startListening(ground)
	ground.waitForHelper()
	keyboard = ground.uhidKeyboard(UHID)
	noHelper(ground, keyboard)
	keptKeys(ground, keyboard)
	polish(ground, keyboard)
	twoLayouts(ground, keyboard)
	helperKilled(ground, keyboard)
	lindaStopped(ground, keyboard)
	pointer(ground, keyboard)
	figures = windows(ground, keyboard)
	ground.machine.log("AT windows figures " + json.dumps({DESKTOP: figures}, sort_keys=True))
	emergency(ground, keyboard)
	nothingLogged(ground, keyboard)
	# Last, as no window is activated again after it under sway.
	console(ground, keyboard)
