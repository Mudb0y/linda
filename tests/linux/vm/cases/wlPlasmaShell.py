"""Plasma's own interfaces with Linda, its keys taken by linda-keyd: KWin's Alt+Tab switcher speaks the
window each Tab selects and then the window Alt's release activates; Alt+F2 speaks KRunner; Plasma's
polkit agent, raised by pkexec, is read and speaks none of a password typed into it through the
helper, which is in neither Linda's log nor the helper's journal. Neither the switcher's frame nor the
launcher's is said. The launcher and a notification are logged as heard.

What the desktop sent along the way is written to the log as each step ends, by a recorder that reads
the accessibility bus and asks nothing of the programs; it records no text event, so nothing typed.
"""

import json
import shlex
from typing import TYPE_CHECKING

from ground import GROUND, KERNEL_ROUTE, LOGS, PREFIX_MARK, USER, WINDOW_TITLE, Mark
from wl.lockScreen import MARKER, assertPasswordAbsent, passwordHeldIn, passwordUnspoken, typePassword

if TYPE_CHECKING:
	from ground import FakeKeyboard, Ground

#: Ground.records finds a recorder by its name in LOGS; the Qt window is this guest's alone.
LOGS.setdefault("appqt", f"{GROUND}/appqt.jsonl")
UHID = "Ground uhid keyboard"
QT_TITLE = "Ground Qt window"
#: Each window as Linda says it once it is active, and the text field it gives the focus to.
GTK3_WINDOW = (WINDOW_TITLE, "Name edit Alt plus N blank")
QT_WINDOW = (f"{QT_TITLE} panel", "Name edit Alt plus N blank")
#: Seconds the desktop may take to show what a key asked for, and Linda to say it. KWin builds its
#: switcher the first time it is asked for, which took 2.2 s in the guest.
SHOWN = 10
RUNNER = "Runner"
POLKIT_TITLE = "Authentication Required"
PROTECTED = "protected"
RAN = f"{GROUND}/pkexec-ran"
#: A wrong password, typed first, held to the same rules as the right one.
WRONG = "pqzvbn4ty"

EVENTS = f"{GROUND}/shellEvents.jsonl"
RECORDER = f"{GROUND}/shellRecorder.py"
#: The recorder, run in the session by the Python cases/wlPlasmaShell.nix installs. It subscribes to the
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
#: Events left out of the log, of which the desktop sends hundreds a step: plasmashell's clock among
#: them, renamed every second.
NOISE = (
	"BoundsChanged",
	"ChildrenChanged",
	"accessible-parent",
	"sensitive",
	"enabled",
	"visible",
	"description",
)


def startRecorder(ground: "Ground") -> None:
	ground.machine.succeed(f"echo {shlex.quote(RECORDER_SOURCE)} > {RECORDER}")
	ground.start("shellEvents", ["ground-shell-python", RECORDER, EVENTS])
	ground.waitFor(
		lambda: any(r.get("kind") == "ready" for r in ground.readJsonLines(EVENTS)), "the recorder", 30
	)


def logEvents(ground: "Ground", what: str, since: Mark) -> None:
	lines = [
		f"{r['t'] - since.t:.3f} {r['pid']} {r['path'].rsplit('/', 1)[-1]} {r['event']} {r['d1']} {r['data'] or ''}"
		for r in ground.readJsonLines(EVENTS)
		if r.get("t", 0) >= since.t and "event" in r and not any(n in r["event"] for n in NOISE)
	]
	ground.machine.log(f"what the desktop sent, {what}:\n" + "\n".join(lines[:200]))


def said(ground: "Ground", since: Mark) -> list[str]:
	return [text for _t, text in ground.speech(since)]


def saidIn(ground: "Ground", since: Mark, part: str, timeout: float = SHOWN) -> None:
	"""Wait until a speak record after since holds part."""
	try:
		ground.waitFor(
			lambda: any(part in text for text in said(ground, since)), f"Linda to say {part!r}", timeout
		)
	except AssertionError as e:
		raise AssertionError(f"{e}; it said {said(ground, since)}") from None


def openWindow(ground: "Ground", title: str, log: str, name: str, argv: list[str]) -> None:
	"""Start a window and wait until its field has the focus, given by the guest's driver when KWin
	does not give it."""
	mark = Mark(0, ground.now())
	ground.start(name, argv, env={"GDK_BACKEND": "wayland"})

	def ready() -> bool:
		return any(r["kind"] == "ready" for r in ground.records(log, mark))

	try:
		ground.waitFor(ready, title, 10)
	except AssertionError:
		ground.activate(title)
		ground.waitFor(ready, f"{title} to have the focus", 30)


def assertNoUnnamedFrame(spoken: list[str], what: str) -> None:
	"""KWin's switcher and plasmashell's popups are unnamed frames, which kwin_wayland.py and
	plasmashell.py keep from being said."""
	frames = [text for text in spoken if text == "frame" or text.startswith("frame ")]
	assert not frames, f"Linda said an unnamed frame for {what}: {spoken}"


def item(title: str) -> str:
	"""A switcher item as Linda says it: the window's title and where it is among the items."""
	return f"{title} {PREFIX_MARK}"


def altTab(
	ground: "Ground", keyboard: "FakeKeyboard", chosen: list[str], window: tuple[str, str]
) -> list[str]:
	"""Alt held, a Tab for each window titled in chosen, waiting each time for Linda to say the item, then
	Alt released: Linda says window and its field. Answers what Linda said."""
	mark = ground.mark()
	keyboard.run({"press": "KEY_LEFTALT"}, {"sleep": 0.1})
	try:
		for choice in chosen:
			step = ground.mark()
			keyboard.run({"tap": "KEY_TAB", "hold": 0.03})
			ground.waitForSpeech(item(choice), step, SHOWN)
			ground.settle(0.5)
	finally:
		released = ground.mark()
		keyboard.run({"release": "KEY_LEFTALT"})
	ground.expect(*window, since=released, timeout=SHOWN)
	ground.settle(1)
	logEvents(ground, f"for Alt+Tab to {window[0]}", mark)
	return said(ground, mark)


def switcher(ground: "Ground", keyboard: "FakeKeyboard", figures: dict) -> None:
	with ground.subtest("Alt+Tab speaks the window each Tab selects, and the window Alt's release activates"):
		figures["three Tabs"] = altTab(ground, keyboard, [QT_TITLE, WINDOW_TITLE, QT_TITLE], QT_WINDOW)
		figures["one Tab"] = altTab(ground, keyboard, [WINDOW_TITLE], GTK3_WINDOW)
		assertNoUnnamedFrame(figures["three Tabs"], "three Tabs")
		assertNoUnnamedFrame(figures["one Tab"], "one Tab")

	with ground.subtest("a quick Alt+Tab speaks the window it activates"):
		mark = ground.mark()
		keyboard.run(
			{"press": "KEY_LEFTALT"},
			{"sleep": 0.1},
			{"tap": "KEY_TAB", "hold": 0.03},
			{"sleep": 0.3},
			{"release": "KEY_LEFTALT"},
		)
		ground.expect(*QT_WINDOW, since=mark, timeout=SHOWN)
		ground.settle(1)
		figures["a quick Tab"] = said(ground, mark)
	ground.machine.log(f"Alt+Tab, as Linda said it: {json.dumps(figures)}")


def runner(ground: "Ground", keyboard: "FakeKeyboard", figures: dict) -> None:
	with ground.subtest("Alt+F2 speaks KRunner, and Escape the window again"):
		mark = ground.mark()
		keyboard.chord("KEY_LEFTALT", "KEY_F2")
		saidIn(ground, mark, RUNNER)
		saidIn(ground, mark, "edit")
		ground.settle(1)
		figures["KRunner"] = said(ground, mark)
		logEvents(ground, "for Alt+F2", mark)
		closed = ground.mark()
		keyboard.tap("KEY_ESC")
		ground.expect(*QT_WINDOW, since=closed, timeout=SHOWN)


def heardOnly(ground: "Ground", keyboard: "FakeKeyboard", figures: dict) -> None:
	with ground.subtest("the launcher and a notification, as Linda hears them"):
		mark = ground.mark()
		keyboard.tap("KEY_LEFTMETA")
		ground.settle(3)
		figures["the launcher"] = said(ground, mark)
		logEvents(ground, "for the launcher", mark)
		closed = ground.mark()
		keyboard.tap("KEY_ESC")
		ground.expect(QT_WINDOW[0], since=closed, timeout=SHOWN)
		assertNoUnnamedFrame(figures["the launcher"], "the launcher")
		mark = ground.mark()
		ground.run(["notify-send", "Ground title", "Ground body"])
		ground.settle(4)
		figures["a notification"] = said(ground, mark)
		logEvents(ground, "for a notification", mark)
		ground.machine.log(f"the launcher and a notification, as Linda said them: {json.dumps(figures)}")


def typedUnspoken(ground: "Ground", keyboard: "FakeKeyboard", password: str) -> list[str]:
	mark = ground.mark()
	typePassword(ground, keyboard, password)
	ground.settle(2)
	return passwordUnspoken(ground, mark, password)


def polkit(ground: "Ground", keyboard: "FakeKeyboard", figures: dict) -> None:
	with ground.subtest("pkexec raises Plasma's polkit agent, which Linda reads"):
		mark = ground.mark()
		ground.start("pkexec", ["/run/wrappers/bin/pkexec", "/run/current-system/sw/bin/touch", RAN])
		saidIn(ground, mark, POLKIT_TITLE)
		saidIn(ground, mark, PROTECTED)
		ground.settle(1)
		figures["polkit's dialog"] = said(ground, mark)
		logEvents(ground, "for pkexec", mark)

	with ground.subtest("a wrong password typed there is not spoken, and pkexec does not run"):
		figures["typing the wrong password"] = typedUnspoken(ground, keyboard, WRONG)
		mark = ground.mark()
		keyboard.tap("KEY_ENTER")
		ground.settle(5)
		figures["after the wrong password"] = said(ground, mark)
		logEvents(ground, "for the wrong password", mark)
		assert ground.machine.execute(f"test -e {RAN}")[0] != 0, "pkexec ran with a wrong password"

	with ground.subtest("the right password typed there is not spoken, and pkexec runs"):
		figures["typing the password"] = typedUnspoken(ground, keyboard, MARKER)
		mark = ground.mark()
		keyboard.tap("KEY_ENTER")
		ground.waitFor(lambda: ground.machine.execute(f"test -e {RAN}")[0] == 0, "pkexec to run", 20)
		ground.expect(QT_WINDOW[0], since=mark, timeout=SHOWN)
		ground.settle(1)
		figures["after the password"] = said(ground, mark)


def run(ground: "Ground") -> None:
	ground.machine.log(f"the desktop: {json.dumps(ground.focus('facts'))}")
	ground.shell(f"echo {USER}:{MARKER} | chpasswd")
	openWindow(ground, QT_TITLE, "appqt", "windowQt", ["ground-window-qt", LOGS["appqt"], QT_TITLE])
	openWindow(ground, WINDOW_TITLE, "app", "window", ["ground-window", LOGS["app"], WINDOW_TITLE])
	ground.waitForHelper()
	keyboard = ground.uhidKeyboard(UHID)
	ground.startLinda()
	ground.waitForLindaLog(KERNEL_ROUTE, timeout=30)
	startRecorder(ground)
	ground.settle(2)
	figures: dict = {}
	try:
		switcher(ground, keyboard, figures)
		runner(ground, keyboard, figures)
		heardOnly(ground, keyboard, figures)
		polkit(ground, keyboard, figures)
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
