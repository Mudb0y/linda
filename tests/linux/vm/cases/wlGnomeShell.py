"""GNOME Shell's own interfaces with Linda, its keys taken by linda-keyd: the Alt+Tab switcher speaks the
program, or with the window switcher on Alt+Tab the window, that each Tab selects and then the window
Alt's release activates; Alt+F2 speaks the run dialog; polkit's authentication dialog, raised by
pkexec, and the keyring's prompt, raised by secret-tool, are read, polkit's says when a password was
wrong, and neither speaks a password typed into it through the helper, which is in neither Linda's
log nor the helper's journal. The overview and a notification are logged as heard.

What the shell sent along the way is written to the log as each step ends, by a recorder that reads
the accessibility bus and asks nothing of the programs; it records no text event, so nothing typed.
"""

import json
import shlex
from typing import TYPE_CHECKING

from ground import GROUND, KERNEL_ROUTE, LOG_HEADER, USER, WINDOW_TITLE
from wl.lockScreen import MARKER, assertPasswordAbsent, passwordHeldIn, passwordUnspoken, typePassword

if TYPE_CHECKING:
	from ground import FakeKeyboard, Ground, Mark

UHID = "Ground uhid keyboard"
GTK4_TITLE = "Ground window 4"
#: What Linda says for the two test windows' programs in the switcher, which names a window that comes
#: with no desktop file after its program, testWindow.py and testWindow4.py, as the synthesizer is
#: given them.
GTK3_PROGRAM = "test Window dot py"
GTK4_PROGRAM = "test Window 4 dot py"
#: The text field each window gives the focus to, as Linda reads it.
GTK3_FIELD = "Name edit Alt plus N blank"
GTK4_FIELD = "Name edit sub Menu Alt plus N blank"
RUN_DIALOG = "Run a Command"
#: polkit's dialog, its message, and what it says of a wrong password.
POLKIT_TITLE = "Authentication Required"
POLKIT_MESSAGE = "Authentication is needed to run"
POLKIT_WRONG = "Sorry, that didn\N{RIGHT SINGLE QUOTATION MARK}t work. Please try again."
PROTECTED = "protected"
RAN = f"{GROUND}/pkexec-ran"
#: A wrong password, typed first, held to the same rules as the right one.
WRONG = "pqzvbn4ty"
#: Seconds the shell may take to show what a key asked for, and Linda to say it.
SHOWN = 10
#: The keyring's prompt, which secret-tool raises by storing a secret while no keyring exists yet, and
#: the file written once the keyring is made and the secret stored in it.
KEYRING_DONE = f"{GROUND}/keyring-done"
KEYRING_COMMAND = f"echo groundsecret | secret-tool store --label=Ground ground item && touch {KEYRING_DONE}"
#: The window switcher on Alt+Tab in place of the program switcher, by GNOME's own keys.
WINDOW_SWITCHER = (
	("org.gnome.desktop.wm.keybindings", "switch-applications", "[]"),
	("org.gnome.desktop.wm.keybindings", "switch-windows", "['<Alt>Tab']"),
)

EVENTS = f"{GROUND}/shellEvents.jsonl"
RECORDER = f"{GROUND}/shellRecorder.py"
#: The recorder, run in the session by the Python cases/wlGnomeShell.nix installs. It subscribes to the
#: accessibility bus's event signals and writes each with the process that sent it, never asking a
#: program anything; text events, which carry what was typed, are not written.
RECORDER_SOURCE = r"""
import json
import sys
import time

from gi.repository import Gio, GLib

session = Gio.bus_get_sync(Gio.BusType.SESSION)
address = session.call_sync(
	"org.a11y.Bus", "/org/a11y/bus", "org.a11y.Bus", "GetAddress", None, None, 0, 5000, None
).unpack()[0]
bus = Gio.DBusConnection.new_for_address_sync(
	address,
	Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
	None,
	None,
)
out = open(sys.argv[1], "a", buffering=1)
processes = {}


def processOf(sender):
	if sender not in processes:
		try:
			processes[sender] = bus.call_sync(
				"org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
				"GetConnectionUnixProcessID", GLib.Variant("(s)", (sender,)), None, 0, 2000, None,
			).unpack()[0]
		except GLib.Error:
			processes[sender] = 0
	return processes[sender]


def onSignal(_connection, sender, path, interface, member, parameters):
	if member.startswith("Text"):
		return
	detail, detail1, _detail2, data = parameters.unpack()[:4]
	if member == "StateChanged" and detail == "defunct":
		return
	out.write(json.dumps({
		"t": time.monotonic(), "pid": processOf(sender), "path": path,
		"event": f"{interface.rsplit('.', 1)[-1]}:{member}:{detail}", "d1": detail1,
		"data": data if isinstance(data, (str, int)) else None,
	}) + "\n")


for interface in ("org.a11y.atspi.Event.Object", "org.a11y.atspi.Event.Window"):
	bus.signal_subscribe(None, interface, None, None, None, Gio.DBusSignalFlags.NONE, onSignal)
out.write(json.dumps({"kind": "ready", "t": time.monotonic()}) + "\n")
GLib.MainLoop().run()
"""
#: Events left out of the log, of which the shell sends hundreds a step.
NOISE = ("BoundsChanged", "ChildrenChanged", "accessible-parent", "sensitive", "enabled", "visible")
#: What a record of Linda's log holds to be copied into the build's log.
LINDA_WORDS = ("AT-SPI focus", "AT-SPI: window", "Speaking", "ERROR", "Traceback", "appModule")


def startRecorder(ground: "Ground") -> None:
	ground.machine.succeed(f"echo {shlex.quote(RECORDER_SOURCE)} > {RECORDER}")
	ground.start("shellEvents", ["ground-shell-python", RECORDER, EVENTS])
	ground.waitFor(
		lambda: any(r.get("kind") == "ready" for r in ground.readJsonLines(EVENTS)), "the recorder", 30
	)


def logEvents(ground: "Ground", what: str, since: "Mark") -> None:
	lines = [
		f"{r['t'] - since.t:.3f} {r['pid']} {r['path'].rsplit('/', 1)[-1]} {r['event']} {r['d1']} {r['data'] or ''}"
		for r in ground.readJsonLines(EVENTS)
		if r.get("t", 0) >= since.t and "event" in r and not any(n in r["event"] for n in NOISE)
	]
	ground.machine.log(f"what the desktop sent, {what}:\n" + "\n".join(lines[:200]))


def logLinda(ground: "Ground", what: str, offset: int) -> None:
	"""The records of Linda's log from offset on that say what its AT-SPI handler did and what it spoke,
	none of which names a key."""
	text = ground.lindaLog()[offset:]
	headers = list(LOG_HEADER.finditer(text))
	records = [
		text[header.start() : headers[i + 1].start() if i + 1 < len(headers) else len(text)].strip()
		for i, header in enumerate(headers)
	]
	kept = [r for r in records if any(w in r for w in LINDA_WORDS) and "Input:" not in r]
	ground.machine.log(f"what Linda did, {what}:\n" + "\n".join(kept[-80:]))


def said(ground: "Ground", since: "Mark") -> list[str]:
	return [text for _t, text in ground.speech(since)]


def saidIn(ground: "Ground", since: "Mark", part: str, timeout: float = SHOWN) -> None:
	"""Wait until a speak record after since holds part."""
	try:
		ground.waitFor(
			lambda: any(part in text for text in said(ground, since)), f"Linda to say {part!r}", timeout
		)
	except AssertionError as e:
		raise AssertionError(f"{e}; it said {said(ground, since)}") from None


def altTab(
	ground: "Ground", keyboard: "FakeKeyboard", chosen: list[str], window: str, field: str
) -> list[str]:
	"""Alt held, a Tab for each of chosen, waiting each time for Linda to say the choice, then Alt
	released: Linda says window and its field. Answers what Linda said."""
	mark = ground.mark()
	offset = len(ground.lindaLog())
	keyboard.run({"press": "KEY_LEFTALT"}, {"sleep": 0.1})
	try:
		for choice in chosen:
			step = ground.mark()
			keyboard.run({"tap": "KEY_TAB", "hold": 0.03})
			ground.waitForSpeech(choice, step, SHOWN)
			ground.settle(0.5)
		released = ground.mark()
	finally:
		keyboard.run({"release": "KEY_LEFTALT"})
		ground.settle(1)
		logEvents(ground, f"for Alt+Tab to {window}", mark)
		logLinda(ground, f"for Alt+Tab to {window}", offset)
	ground.expect(window, field, since=released, timeout=SHOWN)
	return said(ground, mark)


def gsettings(ground: "Ground", *arguments: str) -> str:
	answer = ground.run(["gsettings", *arguments])
	assert answer.returncode == 0, answer
	return answer.stdout.strip()


def switcher(ground: "Ground", keyboard: "FakeKeyboard", figures: dict) -> None:
	with ground.subtest(
		"Alt+Tab speaks the program each Tab selects, and the window Alt's release activates"
	):
		figures["programs, three Tabs"] = altTab(
			ground, keyboard, [GTK4_PROGRAM, GTK3_PROGRAM, GTK4_PROGRAM], GTK4_TITLE, GTK4_FIELD
		)
		figures["programs, one Tab"] = altTab(ground, keyboard, [GTK3_PROGRAM], WINDOW_TITLE, GTK3_FIELD)

	with ground.subtest("a quick Alt+Tab speaks the window it activates"):
		mark = ground.mark()
		keyboard.run(
			{"press": "KEY_LEFTALT"},
			{"sleep": 0.1},
			{"tap": "KEY_TAB", "hold": 0.03},
			{"sleep": 0.3},
			{"release": "KEY_LEFTALT"},
		)
		ground.expect(GTK4_TITLE, GTK4_FIELD, since=mark, timeout=SHOWN)
		ground.settle(1)
		figures["programs, a quick Tab"] = said(ground, mark)

	with ground.subtest("with the window switcher on Alt+Tab, each Tab speaks the window it selects"):
		before = {
			(schema, key): gsettings(ground, "get", schema, key) for schema, key, _value in WINDOW_SWITCHER
		}
		for schema, key, value in WINDOW_SWITCHER:
			gsettings(ground, "set", schema, key, value)
		ground.settle(1)
		try:
			figures["windows, two Tabs"] = altTab(
				ground, keyboard, [WINDOW_TITLE, GTK4_TITLE], GTK4_TITLE, GTK4_FIELD
			)
			figures["windows, one Tab"] = altTab(ground, keyboard, [WINDOW_TITLE], WINDOW_TITLE, GTK3_FIELD)
		finally:
			for (schema, key), value in before.items():
				gsettings(ground, "set", schema, key, value)
	ground.machine.log(f"Alt+Tab, as Linda said it: {json.dumps(figures)}")


def runDialog(ground: "Ground", keyboard: "FakeKeyboard", figures: dict) -> None:
	with ground.subtest("Alt+F2 speaks the run dialog, and Escape the window again"):
		mark = ground.mark()
		keyboard.chord("KEY_LEFTALT", "KEY_F2")
		saidIn(ground, mark, RUN_DIALOG)
		ground.settle(1)
		figures["the run dialog"] = said(ground, mark)
		logEvents(ground, "for Alt+F2", mark)
		assert not [t for t in figures["the run dialog"] if "unavailable" in t], figures["the run dialog"]
		closed = ground.mark()
		keyboard.tap("KEY_ESC")
		ground.expect(WINDOW_TITLE, GTK3_FIELD, since=closed, timeout=SHOWN)


def heardOnly(ground: "Ground", keyboard: "FakeKeyboard", figures: dict) -> None:
	with ground.subtest("the overview and a notification, as Linda hears them"):
		mark = ground.mark()
		keyboard.tap("KEY_LEFTMETA")
		ground.settle(3)
		figures["the overview"] = said(ground, mark)
		logEvents(ground, "for the overview", mark)
		closed = ground.mark()
		keyboard.tap("KEY_LEFTMETA")
		ground.expect(WINDOW_TITLE, since=closed, timeout=SHOWN)
		mark = ground.mark()
		ground.run(["notify-send", "Ground title", "Ground body"])
		ground.settle(4)
		figures["a notification"] = said(ground, mark)
		logEvents(ground, "for a notification", mark)
		ground.machine.log(f"the overview and a notification, as Linda said them: {json.dumps(figures)}")


def typedUnspoken(ground: "Ground", keyboard: "FakeKeyboard", password: str) -> list[str]:
	mark = ground.mark()
	typePassword(ground, keyboard, password)
	ground.settle(2)
	return passwordUnspoken(ground, mark, password)


def polkit(ground: "Ground", keyboard: "FakeKeyboard", figures: dict) -> None:
	with ground.subtest("pkexec raises polkit's dialog, which Linda reads"):
		mark = ground.mark()
		ground.start("pkexec", ["/run/wrappers/bin/pkexec", "/run/current-system/sw/bin/touch", RAN])
		saidIn(ground, mark, POLKIT_MESSAGE)
		saidIn(ground, mark, PROTECTED)
		ground.settle(1)
		figures["polkit's dialog"] = said(ground, mark)
		logEvents(ground, "for pkexec", mark)
		assert any(POLKIT_TITLE in t for t in figures["polkit's dialog"]), figures["polkit's dialog"]

	with ground.subtest("a wrong password typed there is not spoken, and Linda says it was wrong"):
		figures["typing the wrong password"] = typedUnspoken(ground, keyboard, WRONG)
		mark = ground.mark()
		keyboard.tap("KEY_ENTER")
		try:
			ground.waitForSpeech(POLKIT_WRONG, mark, 15)
		finally:
			ground.settle(1)
			figures["after the wrong password"] = said(ground, mark)
			logEvents(ground, "for the wrong password", mark)
		assert ground.machine.execute(f"test -e {RAN}")[0] != 0, "pkexec ran with a wrong password"

	with ground.subtest("the right password typed there is not spoken, and pkexec runs"):
		figures["typing the password"] = typedUnspoken(ground, keyboard, MARKER)
		mark = ground.mark()
		keyboard.tap("KEY_ENTER")
		ground.waitFor(lambda: ground.machine.execute(f"test -e {RAN}")[0] == 0, "pkexec to run", 20)
		ground.expect(WINDOW_TITLE, GTK3_FIELD, since=mark, timeout=SHOWN)
		ground.settle(1)
		figures["after the password"] = said(ground, mark)


def keyring(ground: "Ground", keyboard: "FakeKeyboard", figures: dict) -> None:
	with ground.subtest("secret-tool raises the keyring's prompt, which Linda reads"):
		mark = ground.mark()
		ground.start("keyring", ["sh", "-c", KEYRING_COMMAND])
		saidIn(ground, mark, PROTECTED)
		ground.settle(1)
		figures["the keyring's prompt"] = said(ground, mark)
		logEvents(ground, "for the keyring's prompt", mark)

	with ground.subtest("a new keyring's password typed twice there is not spoken, and the secret is stored"):
		figures["typing the keyring's password"] = typedUnspoken(ground, keyboard, MARKER)
		mark = ground.mark()
		keyboard.tap("KEY_TAB")
		saidIn(ground, mark, PROTECTED)
		figures["typing it again"] = typedUnspoken(ground, keyboard, MARKER)
		mark = ground.mark()
		keyboard.tap("KEY_ENTER")
		ground.waitFor(
			lambda: ground.machine.execute(f"test -e {KEYRING_DONE}")[0] == 0, "the secret to be stored", 20
		)
		ground.expect(WINDOW_TITLE, GTK3_FIELD, since=mark, timeout=SHOWN)
		ground.settle(1)
		figures["after the keyring's password"] = said(ground, mark)


def run(ground: "Ground") -> None:
	facts = ground.focus("facts")
	ground.machine.log(f"the shell: {facts}")
	assert facts["unsafeMode"] is False, facts
	ground.shell(f"echo {USER}:{MARKER} | chpasswd")
	ground.openWindow(GTK4_TITLE, name="window4", gtk4=True)
	ground.openWindow()
	ground.waitForHelper()
	keyboard = ground.uhidKeyboard(UHID)
	ground.startLinda()
	ground.waitForLindaLog(KERNEL_ROUTE, timeout=30)
	startRecorder(ground)
	ground.settle(2)
	figures: dict = {}
	try:
		switcher(ground, keyboard, figures)
		ground.activate(WINDOW_TITLE)
		ground.settle(2)
		runDialog(ground, keyboard, figures)
		heardOnly(ground, keyboard, figures)
		polkit(ground, keyboard, figures)
		keyring(ground, keyboard, figures)
		with ground.subtest("neither password is in Linda's log or the helper's journal"):
			figures["the password's occurrences"] = assertPasswordAbsent(ground)
			texts = {
				"lindaLog": ground.lindaLog(),
				"helperJournal": "\n".join(message for _t, message in ground.helperLog()),
			}
			wrong = {name: passwordHeldIn(WRONG, text) for name, text in texts.items()}
			assert not any(wrong.values()), wrong
	finally:
		ground.machine.log("SH figures " + json.dumps(figures, sort_keys=True, ensure_ascii=False))
