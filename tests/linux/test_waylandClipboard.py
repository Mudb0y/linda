"""The clipboard on a Wayland display: what Linda copies another program pastes, what another program
copies Linda reads, and where the desktop lets Linda do neither, it says so instead of claiming a copy
or reading stale text.

Each compositor is held to the one outcome experiment E17 found for it. sway offers data control, so
Linda copies and reads through it and the program with the keyboard keeps it throughout. Mutter offers
none, and gives a new window the keyboard as it maps, so Linda copies and reads from a window of its
own that it maps for the moment and closes, after which the keyboard is back where it was. Under sway
the window route is tried as well with data control taken out of Linda's reach, and then, with sway
told never to give Linda's windows the keyboard, the refusals.

The other program is a GTK 3 window of the test's own, which has the keyboard and pastes and copies
with GTK as any program would, and wl-paste and wl-copy beside it. Linda runs natively with a global
plugin of the test's own that copies as NVDA's commands do and reads as NVDA+c does, since the key route
on Wayland is not this file's.

What no real compositor can be made to do on cue, such as ignore a copy, change the clipboard in the
moment Linda reads it, or cancel a source Linda has already replaced, is held against a compositor of
the test's own, FakeCompositor, with the clipboard module loaded alone in the test's process.

tests/linux/wayland-gate SCRATCH runs this file under Mutter and sway, with and without Xwayland; the X
rig runs none of it, and test_clipboard.py holds the X display to what it always did.
"""

import array
import builtins
import importlib.util
import itertools
import json
import os
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
import traceback
import types
from collections.abc import Callable, Iterator
from pathlib import Path

import harness
import pytest

pytestmark = pytest.mark.rigSession("wayland")

SESSION_CONFIG = harness.HERE / "fixtures" / "sessionConfig" / "nvda.ini"
#: Seconds from starting Linda to its plugin's first record; a start under the rig takes about three.
STARTUP_BOUND = 60.0
#: Seconds Linda has to exit on SIGTERM before it is killed.
EXIT_BOUND = 10.0
#: Seconds a copy or a read may take, a window mapped and closed included.
CLIPBOARD_BOUND = 2.0
#: Seconds from NVDA+c to speech when the clipboard's owner does not answer: NVDA waits one.
HUNG_OWNER_BOUND = 3.0
#: Seconds wl-copy and wl-paste have.
WL_BOUND = 5.0
#: Seconds the other program has to be given the keyboard back after Linda's window closes.
KEYBOARD_BACK_BOUND = 2.0
#: Seconds to wait for anything more after what a step expects.
SETTLE = 0.5

#: What E17 found each compositor offers: the route Linda's clipboard must take there, and the data
#: control manager it must use.
E17_ROUTES = {
	"sway": ("_DataControlRoute", "ext_data_control_manager_v1"),
	"mutter": ("_WindowRoute", None),
}
COPIED = "Copied to clipboard: {}"
CANNOT_COPY = "Linda cannot copy to the clipboard in this session"
CANNOT_READ = "Linda cannot read the clipboard here"
NO_TEXT = "There is no text on the clipboard"
POLISH = "Zażółć gęślą jaźń"
#: Copied and read, and never to be found in Linda's log.
MARKER = "kqzmxjwvclp"

#: A global plugin that takes commands from files a test writes, copies and reads the clipboard as
#: NVDA's commands do, and records what came of it, as JSON lines with the monotonic clock every
#: process shares.
PLUGIN = """\
import json
import os
import time

import api
import globalCommands
import globalPluginHandler
import globalVars
import wx
from linuxPlatform import clipboard, ownGui

DIRECTORY = os.environ.get("LINDA_CLIPBOARD_PROBE")


def record(kind, **fields):
	with open(os.path.join(DIRECTORY, "records.jsonl"), "a", encoding="utf-8") as out:
		out.write(json.dumps({"kind": kind, "t": time.monotonic(), **fields}) + "\\n")


def route():
	chosen = clipboard._route
	if chosen is None:
		return None
	return {"kind": type(chosen).__name__, "manager": getattr(chosen, "managerName", None)}


class GlobalPlugin(globalPluginHandler.GlobalPlugin):
	def __init__(self):
		super().__init__()
		if not DIRECTORY:
			return
		self.timer = wx.Timer()
		self.timer.Bind(wx.EVT_TIMER, self.poll)
		self.timer.Start(50)
		record("ready", pid=globalVars.appPid, displayKind=ownGui.displayKind())

	def poll(self, evt):
		commands = os.path.join(DIRECTORY, "commands")
		for name in sorted(os.listdir(commands)):
			path = os.path.join(commands, name)
			with open(path, encoding="utf-8") as source:
				words = json.load(source)
			os.unlink(path)
			wx.CallAfter(self.run, *words)

	def run(self, command, *args):
		getattr(self, "do_" + command)(*args)

	def do_copy(self, text):
		started = time.monotonic()
		copied = api.copyToClip(text, notify=True)
		record("copied", copied=copied, seconds=time.monotonic() - started, route=route())

	def do_report(self):
		started = time.monotonic()
		globalCommands.commands.script_reportClipboardText(None)
		record("reported", seconds=time.monotonic() - started, route=route())

	def do_read(self):
		started = time.monotonic()
		try:
			text, error = api.getClipData(), None
		except OSError as e:
			text, error = None, type(e).__name__
		record("read", text=text, error=error, seconds=time.monotonic() - started, route=route())

	def do_withoutDataControl(self):
		clipboard.DATA_CONTROL_MANAGERS = ()
		if clipboard._route is not None:
			clipboard._route.wire.close()
			clipboard._route = None
		record("withoutDataControl")
"""

#: The other program: a GTK 3 window with a text field, which pastes and copies with GTK's clipboard
#: when told on standard input, and prints JSON lines of what it did and when it got or lost the
#: keyboard.
OTHER_PROGRAM = """\
import json
import sys
import time

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk


def out(kind, **fields):
	print(json.dumps({"kind": kind, "t": time.monotonic(), **fields}), flush=True)


clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
window = Gtk.Window(title="Clipboard window")
window.add(Gtk.Entry())
window.set_default_size(300, 200)
window.connect("notify::is-active", lambda w, _p: out("active", active=w.is_active()))
window.connect("destroy", lambda _w: Gtk.main_quit())


def pasted(_clipboard, text, *_data):
	out("pasted", text=text)


def onLine(source, _condition):
	line = source.readline()
	if not line:
		Gtk.main_quit()
		return False
	command, _, argument = line.rstrip("\\n").partition(" ")
	if command == "paste":
		clipboard.request_text(pasted)
	elif command == "copy":
		clipboard.set_text(argument, -1)
		Gdk.Display.get_default().sync()
		out("copied", text=argument)
	elif command == "state":
		out("state", active=window.is_active())
	return True


GLib.io_add_watch(sys.stdin, GLib.IO_IN | GLib.IO_HUP, onLine)
out("backend", display=type(Gdk.Display.get_default()).__name__)
window.show_all()
Gtk.main()
"""


class OtherProgram:
	"""The GTK 3 window, once it has the keyboard."""

	def __init__(self, directory: Path) -> None:
		with open(directory / "otherProgram.stderr.txt", "w") as errors:
			self.process = subprocess.Popen(
				[sys.executable, "-c", OTHER_PROGRAM],
				stdin=subprocess.PIPE,
				stdout=subprocess.PIPE,
				stderr=errors,
				text=True,
				env={**os.environ, "GDK_BACKEND": "wayland"},
			)
		self.records: list[dict] = []
		threading.Thread(target=self._read, daemon=True).start()
		self.waitFor(lambda record: record["kind"] == "active" and record["active"], 15)
		assert self.records[0] == {**self.records[0], "kind": "backend", "display": "GdkWaylandDisplay"}

	def _read(self) -> None:
		for line in self.process.stdout:
			self.records.append(json.loads(line))

	def waitFor(self, predicate: Callable[[dict], bool], timeout: float = 5, since: int = 0) -> dict:
		deadline = time.monotonic() + timeout
		while time.monotonic() < deadline:
			for record in self.records[since:]:
				if predicate(record):
					return record
			if self.process.poll() is not None:
				break
			time.sleep(0.02)
		raise AssertionError(f"the other program never did what was waited for; it printed {self.records}")

	def ask(self, line: str, kind: str) -> dict:
		since = len(self.records)
		self.process.stdin.write(line + "\n")
		self.process.stdin.flush()
		return self.waitFor(lambda record: record["kind"] == kind, since=since)

	def paste(self) -> str | None:
		return self.ask("paste", "pasted")["text"]

	def copy(self, text: str) -> None:
		import waylandKeyboard

		# A program copies on a key it was given, whose serial the compositor holds the copy to.
		waylandKeyboard.press("shift")
		self.ask(f"copy {text}", "copied")

	def hasKeyboard(self) -> bool:
		return self.ask("state", "state")["active"]

	def keyboardChanges(self, since: float) -> list[bool]:
		return [r["active"] for r in self.records if r["kind"] == "active" and r["t"] >= since]

	def stop(self) -> None:
		if self.process.poll() is None:
			self.process.stdin.close()
			try:
				self.process.wait(5)
			except subprocess.TimeoutExpired:
				self.process.kill()
				self.process.wait()


class Linda:
	"""Linda with the clipboard probe plugin: commands to it, what it recorded and what it spoke."""

	def __init__(self, directory: Path) -> None:
		self.probe = directory / "probe"
		(self.probe / "commands").mkdir(parents=True)
		(self.probe / "records.jsonl").touch()
		hooks = directory / "hooks"
		(hooks / "globalPlugins").mkdir(parents=True)
		(hooks / "globalPlugins" / "clipboardProbe.py").write_text(PLUGIN, encoding="utf-8")
		(hooks / "synthDrivers").symlink_to(harness.HERE / "synthDrivers")
		configDir = directory / "config"
		configDir.mkdir()
		(configDir / "nvda.ini").write_text(SESSION_CONFIG.read_text(encoding="utf-8"), encoding="utf-8")
		self.launch = harness.startLauncher(
			directory / "run",
			"-c",
			str(configDir),
			"-f",
			str(directory / "run" / "nvda.log"),
			"--debug-logging",
			env={"LINDA_TEST_HOOKS_DIR": str(hooks), "LINDA_CLIPBOARD_PROBE": str(self.probe)},
		)
		self._count = 0
		self.ready = self.waitFor("ready", timeout=STARTUP_BOUND)

	def command(self, *words: str) -> float:
		"""Give the plugin a command; the time it was given."""
		self._count += 1
		path = self.probe / "commands" / f"{self._count:04d}"
		temporary = self.probe / f"command-{self._count:04d}"
		temporary.write_text(json.dumps(words), encoding="utf-8")
		given = time.monotonic()
		temporary.rename(path)
		return given

	def records(self, kind: str, since: float = 0) -> list[dict]:
		text = (self.probe / "records.jsonl").read_text(encoding="utf-8")
		records = [json.loads(line) for line in text.splitlines() if line.strip()]
		return [r for r in records if r["kind"] == kind and r["t"] >= since]

	def waitFor(self, kind: str, since: float = 0, timeout: float = 10) -> dict:
		deadline = time.monotonic() + timeout
		while True:
			exited = self.launch.process.poll() is not None
			found = self.records(kind, since)
			if found:
				return found[0]
			if exited or time.monotonic() >= deadline:
				raise AssertionError(f"no {kind} record as waited for\n{self.launch.describe()}")
			time.sleep(0.05)

	def ask(self, command: str, kind: str, *args: str) -> dict:
		return self.waitFor(kind, since=self.command(command, *args))

	def spoken(self) -> list[str]:
		return self.launch.spokenTexts()

	def expect(self, text: str, since: int, timeout: float = 5) -> None:
		"""Wait until Linda has spoken text, a whole speak record, after since."""
		deadline = time.monotonic() + timeout
		while text not in self.spoken()[since:]:
			assert self.launch.process.poll() is None and time.monotonic() < deadline, (
				f"Linda did not speak {text!r}; it spoke {self.spoken()[since:]}\n{self.launch.describe()}"
			)
			time.sleep(0.05)

	def copy(self, text: str) -> tuple[dict, list[str]]:
		"""Copy as NVDA's commands do; the plugin's record and what Linda spoke for it."""
		mark = len(self.spoken())
		copied = self.ask("copy", "copied", text)
		time.sleep(SETTLE)
		return copied, self.spoken()[mark:]

	def report(self) -> tuple[dict, list[str]]:
		"""Read as NVDA+c does; the plugin's record and what Linda spoke."""
		mark = len(self.spoken())
		reported = self.ask("report", "reported")
		time.sleep(SETTLE)
		return reported, self.spoken()[mark:]

	def stop(self) -> None:
		process = self.launch.process
		if process.poll() is None:
			process.send_signal(signal.SIGTERM)
			try:
				process.wait(EXIT_BOUND)
			except subprocess.TimeoutExpired:
				process.kill()
				process.wait()


class ClipboardSession:
	def __init__(self, directory: Path) -> None:
		directory.mkdir(parents=True, exist_ok=True)
		self.other = OtherProgram(directory)
		self.linda: Linda | None = None
		self.linda = Linda(directory / "linda")
		assert self.linda.ready["displayKind"] == "wayland", self.linda.ready
		self.linda.expect("edit blank", since=0, timeout=20)
		time.sleep(SETTLE)
		# Linda's start takes the keyboard from nobody; the other program keeps it.
		assert self.other.hasKeyboard()

	def close(self) -> None:
		if self.linda is not None:
			self.linda.stop()
		self.other.stop()


@pytest.fixture
def session(tmp_path: Path) -> Iterator[ClipboardSession]:
	opened = ClipboardSession(tmp_path / "session")
	try:
		yield opened
	finally:
		opened.close()


def wlPaste() -> str:
	"""The clipboard's text as wl-paste reads it, or "" when it holds none."""
	done = subprocess.run(["wl-paste", "--no-newline"], capture_output=True, timeout=WL_BOUND, check=False)
	if done.returncode != 0:
		assert b"Nothing is copied" in done.stderr, done
		return ""
	return done.stdout.decode("utf-8")


def wlCopy(text: str) -> None:
	"""Have wl-copy own the clipboard with text, from a process of its own in the background."""
	subprocess.run(
		["wl-copy", text], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=WL_BOUND, check=True
	)
	deadline = time.monotonic() + WL_BOUND
	while wlPaste() != text:
		assert time.monotonic() < deadline, "wl-copy did not take the clipboard"
		time.sleep(0.05)


def assertMarkerNotLogged(linda: Linda) -> None:
	"""No record of Linda's log holds MARKER but those of what NVDA spoke, which it logs at debug level
	as it would on Windows."""
	log = linda.launch.text("nvda.log")
	headers = list(harness.NVDA_LOG_RECORD_HEADER.finditer(log))
	records = [
		log[header.start() : headers[index + 1].start() if index + 1 < len(headers) else len(log)]
		for index, header in enumerate(headers)
	]
	kept = "".join(record for record in records if not record.startswith("IO - speech.speech.speak "))
	harness.assertMarkerAbsent(MARKER, {"nvda.log without speech": kept})


def expectedRoute() -> tuple[str, str | None]:
	return E17_ROUTES[harness.rigSession()]


def assertKeyboardKept(session: ClipboardSession, since: float) -> None:
	"""The other program has the keyboard, and as E17 found, kept it throughout where data control is the
	route and got it back where Linda's window took it."""
	changes = session.other.keyboardChanges(since)
	if expectedRoute()[0] == "_DataControlRoute":
		assert changes == [], changes
	else:
		assert changes and changes[-1], changes
	assert session.other.hasKeyboard()


def waitForKeyboard(session: ClipboardSession) -> None:
	"""Wait until the other program has the keyboard back from a window wl-paste or wl-copy mapped, and
	the changes that brought it are over."""
	deadline = time.monotonic() + KEYBOARD_BACK_BOUND
	while not session.other.hasKeyboard():
		assert time.monotonic() < deadline, session.other.records
		time.sleep(0.05)
	time.sleep(SETTLE)


def test_whatLindaCopiesAnotherProgramPastes(session: ClipboardSession) -> None:
	linda, other = session.linda, session.other
	for text in (POLISH, "Second line\nthird line", MARKER):
		started = time.monotonic()
		copied, spoken = linda.copy(text)
		assert copied["copied"], (copied, spoken)
		assert (copied["route"]["kind"], copied["route"]["manager"]) == expectedRoute(), copied
		assert copied["seconds"] < CLIPBOARD_BOUND, copied
		# The transcript gives a line end as a space.
		assert spoken == [COPIED.format(" ".join(text.split()))], spoken
		assertKeyboardKept(session, started)
		assert other.paste() == text
		# Under Mutter wl-paste takes the keyboard for a window of its own, as Linda does.
		assert wlPaste() == text
		waitForKeyboard(session)
	linda.stop()
	linda.launch.assertClean(logNames=("nvda.log",))
	assertMarkerNotLogged(linda)


def test_lindaReadsWhatAnotherProgramCopiedNotWhatItCopiedItself(session: ClipboardSession) -> None:
	linda, other = session.linda, session.other
	copied, _spoken = linda.copy("Linda's own copy")
	assert copied["copied"], copied

	other.copy(MARKER)
	assert wlPaste() == MARKER
	waitForKeyboard(session)
	started = time.monotonic()
	reported, spoken = linda.report()
	assert spoken == [MARKER], spoken
	assert (reported["route"]["kind"], reported["route"]["manager"]) == expectedRoute(), reported
	assert reported["seconds"] < CLIPBOARD_BOUND, reported
	assertKeyboardKept(session, started)

	# A program with no window of its own, and an empty clipboard.
	wlCopy("From the background")
	assert linda.ask("read", "read")["text"] == "From the background"
	subprocess.run(["wl-copy", "--clear"], timeout=WL_BOUND, check=True)
	assert linda.ask("read", "read")["text"] == ""
	_reported, spoken = linda.report()
	assert spoken == [NO_TEXT], spoken
	linda.stop()
	linda.launch.assertClean(logNames=("nvda.log",))
	assertMarkerNotLogged(linda)


def test_anOwnerThatNeverAnswersCostsLindaAMoment(session: ClipboardSession) -> None:
	linda = session.linda
	owner = subprocess.Popen(
		["wl-copy", "--foreground", "Held by a stopped program"],
		stdout=subprocess.DEVNULL,
		stderr=subprocess.DEVNULL,
	)
	try:
		deadline = time.monotonic() + WL_BOUND
		while wlPaste() != "Held by a stopped program":
			assert time.monotonic() < deadline, "wl-copy did not take the clipboard"
			time.sleep(0.05)
		owner.send_signal(signal.SIGSTOP)
		reported, spoken = linda.report()
		assert spoken == [NO_TEXT], spoken
		assert reported["seconds"] < HUNG_OWNER_BOUND, reported
	finally:
		owner.send_signal(signal.SIGCONT)
		owner.terminate()
		owner.wait()
	# Linda's own copy is unharmed by the wait.
	copied, spoken = linda.copy(POLISH)
	assert copied["copied"] and spoken == [COPIED.format(POLISH)], (copied, spoken)
	assert session.other.paste() == POLISH
	linda.stop()
	linda.launch.assertClean(logNames=("nvda.log",))


#: A clipboard manager as Plasma's is: it takes each new text over at once, serving it itself, so that the
#: clipboard outlives the program that copied. It copies only what it has not copied itself, or it would
#: take its own copies over for ever.
CLIPBOARD_MANAGER = """\
text=$(cat; echo .)
text=${text%.}
last=$(cat "$1"; echo .)
last=${last%.}
[ -z "$text" ] || [ "$text" = "$last" ] && exit 0
printf %s "$text" >"$1"
printf %s "$text" | wl-copy
"""


@pytest.mark.rigSession("sway")
def test_aClipboardManagerTakingEachCopyOverLeavesItCopied(session: ClipboardSession, tmp_path: Path) -> None:
	linda = session.linda
	script = tmp_path / "manager.sh"
	script.write_text(CLIPBOARD_MANAGER, encoding="utf-8")
	last = tmp_path / "last"
	last.touch()
	manager = subprocess.Popen(
		["wl-paste", "--watch", "sh", str(script), str(last)],
		stdout=subprocess.DEVNULL,
		stderr=(tmp_path / "manager.stderr.txt").open("w"),
	)
	try:
		for count in range(1, 5):
			text = f"Copy number {count}"
			copied, spoken = linda.copy(text)
			assert copied["copied"] and spoken == [COPIED.format(text)], (copied, spoken)
			deadline = time.monotonic() + WL_BOUND
			while last.read_text(encoding="utf-8") != text:
				assert time.monotonic() < deadline, "the clipboard manager never took the copy over"
				time.sleep(0.05)
			assert session.other.paste() == text
	finally:
		manager.terminate()
		manager.wait()
	linda.stop()
	linda.launch.assertClean(logNames=("nvda.log",))
	assert "The compositor ended NVDA's connection" not in linda.launch.text("nvda.log")


@pytest.mark.rigSession("sway")
def test_withoutDataControlLindaUsesAWindowAndSaysWhenItCannot(session: ClipboardSession) -> None:
	linda, other = session.linda, session.other
	linda.ask("withoutDataControl", "withoutDataControl")
	started = time.monotonic()
	copied, spoken = linda.copy(POLISH)
	assert copied["copied"], (copied, spoken)
	assert copied["route"]["kind"] == "_WindowRoute", copied
	assert spoken == [COPIED.format(POLISH)], spoken
	assert other.paste() == POLISH
	changes = other.keyboardChanges(started)
	assert changes and changes[-1], changes
	other.copy("Copied by the other program")
	_reported, spoken = linda.report()
	assert spoken == ["Copied by the other program"], spoken

	# sway gives no window of Linda's the keyboard from now on, which leaves Linda no way at all.
	subprocess.run(["swaymsg", f"no_focus [pid={linda.ready['pid']}]"], capture_output=True, check=True)
	copied, spoken = linda.copy("Never copied")
	assert not copied["copied"], copied
	assert spoken == [CANNOT_COPY], spoken
	assert other.paste() == "Copied by the other program"
	assert wlPaste() == "Copied by the other program"
	read = linda.ask("read", "read")
	assert read["error"] == "ClipboardRefused" and read["text"] is None, read
	_reported, spoken = linda.report()
	assert spoken[0] == CANNOT_READ, spoken
	assert other.hasKeyboard()
	linda.stop()
	linda.launch.assertClean(logNames=("nvda.log",))


# Against a compositor of the test's own.

#: The requests FakeCompositor understands, by interface and opcode: the types of their arguments in the
#: protocol's wire format, the interface of the object one makes, and whether one destroys the object it
#: is sent to. The object a bind makes has the interface the bind names.
REQUESTS: dict[str, dict[int, tuple[str, str | None, bool]]] = {
	"wl_display": {0: ("n", "wl_callback", False), 1: ("n", "wl_registry", False)},
	"wl_registry": {0: ("usun", None, False)},
	"wl_compositor": {0: ("n", "wl_surface", False)},
	"wl_surface": {0: ("", None, True), 1: ("oii", None, False), 6: ("", None, False)},
	"wl_shm": {0: ("nhi", "wl_shm_pool", False)},
	"wl_shm_pool": {0: ("niiiiu", "wl_buffer", False), 1: ("", None, True)},
	"wl_buffer": {0: ("", None, True)},
	"xdg_wm_base": {2: ("no", "xdg_surface", False), 3: ("u", None, False)},
	"xdg_surface": {0: ("", None, True), 1: ("n", "xdg_toplevel", False), 4: ("u", None, False)},
	"xdg_toplevel": {0: ("", None, True), 2: ("s", None, False)},
	"wl_seat": {1: ("n", "wl_keyboard", False)},
	"wl_data_device_manager": {0: ("n", "wl_data_source", False), 1: ("no", "wl_data_device", False)},
	"wl_data_device": {1: ("ou", None, False)},
	"wl_data_source": {0: ("s", None, False), 1: ("", None, True)},
	"wl_data_offer": {1: ("sh", None, False), 2: ("", None, True)},
	"ext_data_control_manager_v1": {
		0: ("n", "ext_data_control_source_v1", False),
		1: ("no", "ext_data_control_device_v1", False),
	},
	"ext_data_control_device_v1": {0: ("o", None, False)},
	"ext_data_control_source_v1": {0: ("s", None, False), 1: ("", None, True)},
	"ext_data_control_offer_v1": {0: ("sh", None, False), 1: ("", None, True)},
}
#: For data control and for the data device a window uses, the interfaces and the opcodes that differ.
FAMILIES = {
	"ext_data_control": {
		"device": "ext_data_control_device_v1",
		"source": "ext_data_control_source_v1",
		"offer": "ext_data_control_offer_v1",
		"setSelection": 0,
		"receive": 0,
		"selection": 1,
		"finished": 2,
		"send": 0,
		"cancelled": 1,
	},
	"wl_data": {
		"device": "wl_data_device",
		"source": "wl_data_source",
		"offer": "wl_data_offer",
		"setSelection": 1,
		"receive": 1,
		"selection": 5,
		"finished": None,
		"send": 1,
		"cancelled": 2,
	},
}
#: What another program puts on the fake's clipboard when the fake gives Linda's copy away.
ANOTHER_PROGRAMS_TEXT = "Another program's text"
CANNOT_COPY_UNBRANDED = "NVDA cannot copy to the clipboard in this session"
_fakeNumbers = itertools.count()


def _wireUint(value: int) -> bytes:
	return struct.pack("=I", value)


def _wireString(value: str) -> bytes:
	data = value.encode("utf-8") + b"\0"
	return _wireUint(len(data)) + data + b"\0" * (-len(data) % 4)


class FakeCompositor:
	"""A compositor of the test's own, as much of one as the clipboard uses: the registry, a seat with a
	keyboard, and data control or the windows and data device a program without it needs. Linda
	connects to it by an abstract socket name.

	A request to an object that is not alive, or naming one, or making one under a number in use, is
	the protocol error it is on any compositor: the fake records it in errors, says so and ends the
	connection.
	"""

	def __init__(self, *, dataControl: bool = True, seat: bool = True, windows: bool = False) -> None:
		self.path = f"\0lindaFakeCompositor-{os.getpid()}-{next(_fakeNumbers)}"
		self._listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM | socket.SOCK_CLOEXEC)
		self._listener.bind(self.path)
		self._listener.listen(1)
		self._globals = []
		if seat:
			self._globals.append(("wl_seat", 1))
		if dataControl:
			self._globals.append(("ext_data_control_manager_v1", 1))
		if windows:
			self._globals += [
				("wl_compositor", 4),
				("wl_shm", 1),
				("xdg_wm_base", 1),
				("wl_data_device_manager", 3),
			]
		self._family = FAMILIES["ext_data_control" if dataControl else "wl_data"]
		#: What a set_selection does: "take" makes the source the clipboard, "ignore" drops it, and "other"
		#: gives the clipboard to another program instead.
		self.policy = "take"
		#: Whether a new source of Linda's at once cancels the one that is the clipboard, as a clipboard
		#: manager taking that one over would.
		self.cancelOnNewSource = False
		#: Whether the next sync while a window of Linda's has the keyboard takes the keyboard away first.
		self.leaveOnSync = False
		self.errors: list[str] = []
		#: Each request as interface, opcode and object.
		self.requests: list[tuple[str, int, int]] = []
		self.condition = threading.Condition()
		self._sendLock = threading.Lock()
		self._connection: socket.socket | None = None
		self._objects: dict[int, str] = {1: "wl_display"}
		self._serverIds = itertools.count(0xFF000000)
		self._serials = itertools.count(1)
		#: ("linda", source), ("other", text) or None.
		self._selection: tuple | None = None
		#: Each offer alive, with the selection it was made for, or None once the clipboard moved on.
		self._offers: dict[int, tuple | None] = {}
		self._sourceTypes: dict[int, list[str]] = {}
		self._devices: list[int] = []
		self._keyboards: list[int] = []
		#: Each xdg_surface alive, with its surface and how far it has come to being shown.
		self._windows: dict[int, dict] = {}
		self._attached: set[int] = set()
		self._focus: int | None = None
		self._thread = threading.Thread(target=self._serve, name="fake compositor", daemon=True)
		self._thread.start()

	def close(self) -> None:
		self._listener.close()
		if self._connection is not None:
			try:
				self._connection.shutdown(socket.SHUT_RDWR)
			except OSError:
				pass
		self._thread.join(5)

	def otherProgramCopies(self, text: str) -> None:
		with self.condition:
			self._setSelection(("other", text))

	def finishDevices(self) -> None:
		with self.condition:
			for device in self._devices:
				self._event(device, self._family["finished"])

	def waitForRequest(
		self, predicate: Callable[[tuple[str, int, int]], bool], since: int, timeout: float
	) -> bool:
		with self.condition:
			return self.condition.wait_for(lambda: any(map(predicate, self.requests[since:])), timeout)

	def _serve(self) -> None:
		try:
			connection, _address = self._listener.accept()
		except OSError:
			return
		self._connection = connection
		data = b""
		fds: list[int] = []
		try:
			while True:
				chunk, ancillary, _flags, _address = connection.recvmsg(65536, socket.CMSG_SPACE(64 * 4))
				if not chunk:
					return
				for level, kind, payload in ancillary:
					if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
						received = array.array("i")
						received.frombytes(payload[: len(payload) - len(payload) % received.itemsize])
						fds.extend(received)
				data += chunk
				while len(data) >= 8:
					objectId, word = struct.unpack_from("=II", data)
					size = word >> 16
					if len(data) < size:
						break
					payload, data = data[8:size], data[size:]
					with self.condition:
						if not self._handle(objectId, word & 0xFFFF, payload, fds):
							return
		except OSError:
			pass
		finally:
			connection.close()
			for fd in fds:
				os.close(fd)

	def _event(self, objectId: int, opcode: int, payload: bytes = b"", fds: tuple[int, ...] = ()) -> None:
		message = struct.pack("=II", objectId, (8 + len(payload)) << 16 | opcode) + payload
		with self._sendLock:
			try:
				if fds:
					ancillary = [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", fds))]
					self._connection.sendmsg([message], ancillary)
				else:
					self._connection.sendall(message)
			except OSError:
				pass

	def _fail(self, message: str) -> bool:
		self.errors.append(message)
		self.condition.notify_all()
		self._event(1, 0, _wireUint(1) + _wireUint(0) + _wireString(message))
		self._connection.shutdown(socket.SHUT_RDWR)
		return False

	def _parse(self, signature: str, payload: bytes, fds: list[int]) -> list:
		arguments: list = []
		offset = 0
		for kind in signature:
			if kind == "h":
				arguments.append(fds.pop(0))
			elif kind == "s":
				(length,) = struct.unpack_from("=I", payload, offset)
				arguments.append(payload[offset + 4 : offset + 4 + length - 1].decode("utf-8"))
				offset += 4 + length + (-length % 4)
			else:
				arguments.append(struct.unpack_from("=i" if kind == "i" else "=I", payload, offset)[0])
				offset += 4
		return arguments

	def _handle(self, objectId: int, opcode: int, payload: bytes, fds: list[int]) -> bool:
		interface = self._objects.get(objectId)
		if interface is None:
			return self._fail(f"invalid object {objectId}")
		self.requests.append((interface, opcode, objectId))
		self.condition.notify_all()
		signature, made, destructor = REQUESTS.get(interface, {}).get(opcode, ("", None, False))
		arguments = self._parse(signature, payload, fds)
		receiving = interface == self._family["offer"] and opcode == self._family["receive"]
		for kind, value in zip(signature, arguments):
			if kind == "h" and not receiving:
				os.close(value)
			elif kind == "o" and value and value not in self._objects:
				return self._fail(f"invalid object {value}")
			elif kind == "n":
				if value in self._objects:
					return self._fail(f"invalid new id {value}")
				self._objects[value] = arguments[1] if interface == "wl_registry" else made
		if destructor:
			del self._objects[objectId]
			self._event(1, 1, _wireUint(objectId))
		self._act(interface, opcode, objectId, arguments, destructor)
		return True

	def _act(self, interface: str, opcode: int, objectId: int, arguments: list, destructor: bool) -> None:
		family = self._family
		managers = ("ext_data_control_manager_v1", "wl_data_device_manager")
		if interface == "wl_display" and opcode == 0:
			(callback,) = arguments
			if self.leaveOnSync and self._focus is not None:
				self.leaveOnSync = False
				for keyboard in self._keyboards:
					self._event(keyboard, 2, _wireUint(next(self._serials)) + _wireUint(self._focus))
				self._focus = None
			self._event(callback, 0, _wireUint(next(self._serials)))
			del self._objects[callback]
			self._event(1, 1, _wireUint(callback))
		elif interface == "wl_display" and opcode == 1:
			for name, (globalInterface, version) in enumerate(self._globals, 1):
				self._event(
					arguments[0], 0, _wireUint(name) + _wireString(globalInterface) + _wireUint(version)
				)
		elif interface in managers and opcode == 0:
			self._sourceTypes[arguments[0]] = []
			if self.cancelOnNewSource and self._selection is not None and self._selection[0] == "linda":
				self._setSelection(("other", ANOTHER_PROGRAMS_TEXT))
		elif interface in managers and opcode == 1:
			self._devices.append(arguments[0])
			self._announce(arguments[0])
		elif interface == family["source"] and opcode == 0:
			self._sourceTypes[objectId].append(arguments[0])
		elif interface == family["source"] and destructor:
			del self._sourceTypes[objectId]
			if self._selection == ("linda", objectId):
				self._setSelection(None)
		elif interface == family["device"] and opcode == family["setSelection"]:
			if family["device"] == "wl_data_device" and self._focus is None:
				return
			if self.policy == "other":
				self._setSelection(("other", ANOTHER_PROGRAMS_TEXT))
			elif self.policy == "take":
				self._setSelection(("linda", arguments[0]) if arguments[0] else None)
		elif interface == family["offer"] and opcode == family["receive"]:
			mimeType, fd = arguments
			made = self._offers.get(objectId)
			if made is not None and made[0] == "linda":
				self._event(made[1], family["send"], _wireString(mimeType), fds=(fd,))
			elif made is not None:
				os.write(fd, made[1].encode("utf-8"))
			os.close(fd)
		elif interface == family["offer"] and destructor:
			del self._offers[objectId]
		elif interface == "wl_seat" and opcode == 1:
			self._keyboards.append(arguments[0])
		elif interface == "xdg_wm_base" and opcode == 2:
			self._windows[arguments[0]] = {
				"surface": arguments[1],
				"toplevel": False,
				"configured": False,
				"acked": False,
			}
		elif interface == "xdg_surface" and opcode == 1:
			self._windows[objectId]["toplevel"] = True
		elif interface == "xdg_surface" and opcode == 4:
			self._windows[objectId]["acked"] = True
		elif interface == "xdg_surface" and destructor:
			del self._windows[objectId]
		elif interface == "wl_surface" and opcode == 1:
			self._attached.add(objectId)
		elif interface == "wl_surface" and opcode == 6:
			self._commit(objectId)
		elif interface == "wl_surface" and destructor:
			self._attached.discard(objectId)
			if self._focus == objectId:
				self._focus = None

	def _commit(self, surface: int) -> None:
		for xdgSurface, window in self._windows.items():
			if window["surface"] != surface or not window["toplevel"]:
				continue
			if not window["configured"]:
				window["configured"] = True
				self._event(xdgSurface, 0, _wireUint(next(self._serials)))
			elif window["acked"] and surface in self._attached and self._focus is None:
				self._focus = surface
				serial = next(self._serials)
				for keyboard in self._keyboards:
					self._event(keyboard, 1, _wireUint(serial) + _wireUint(surface) + _wireUint(0))
				for device in self._devices:
					self._announce(device)

	def _setSelection(self, selection: tuple | None) -> None:
		old = self._selection
		if old is not None and old[0] == "linda" and old != selection and old[1] in self._objects:
			self._event(old[1], self._family["cancelled"])
		for offer in self._offers:
			self._offers[offer] = None
		self._selection = selection
		for device in self._devices:
			self._announce(device)

	def _announce(self, device: int) -> None:
		"""Tell device what the clipboard is, as a compositor tells the program with the keyboard, or any
		program through data control."""
		family = self._family
		if family["device"] == "wl_data_device" and self._focus is None:
			return
		if self._selection is None:
			self._event(device, family["selection"], _wireUint(0))
			return
		offer = next(self._serverIds)
		self._objects[offer] = family["offer"]
		self._offers[offer] = self._selection
		self._event(device, 0, _wireUint(offer))
		if self._selection[0] == "linda":
			offered = self._sourceTypes.get(self._selection[1], [])
		else:
			offered = ["text/plain;charset=utf-8"]
		for mimeType in offered:
			self._event(offer, 0, _wireString(mimeType))
		self._event(device, family["selection"], _wireUint(offer))


@pytest.fixture
def clipboardAlone(monkeypatch: pytest.MonkeyPatch) -> Iterator[types.ModuleType]:
	"""source/linuxPlatform/clipboard.py loaded alone on a Wayland display, with stand-ins for what it
	takes from NVDA: a log whose records it keeps in logged, ui whose speech it keeps in spoken, core
	naming this thread the main one, and no window of NVDA's with the keyboard."""
	logged: list[str] = []
	spoken: list[str] = []

	def recorder(level: str) -> Callable:
		def record(message: str, *_args, exc_info: bool = False, **_kwargs) -> None:
			logged.append(f"{level}: {message}\n{traceback.format_exc() if exc_info else ''}")

		return record

	log = types.SimpleNamespace(
		**{level: recorder(level) for level in ("debug", "debugWarning", "info", "warning", "error")}
	)
	log.exception = lambda message, *_args, **_kwargs: logged.append(
		f"exception: {message}\n{traceback.format_exc()}"
	)
	monkeypatch.setitem(sys.modules, "logHandler", types.SimpleNamespace(log=log))
	monkeypatch.setitem(
		sys.modules,
		"ui",
		types.SimpleNamespace(
			message=spoken.append,
			reportTextCopiedToClipboard=lambda text=None: spoken.append(
				COPIED.format(text) if text else "Unable to copy"
			),
		),
	)
	monkeypatch.setitem(sys.modules, "core", types.SimpleNamespace(mainThreadId=threading.get_ident()))
	monkeypatch.setattr(builtins, "_", lambda text: text, raising=False)
	spec = importlib.util.spec_from_file_location(
		"clipboardAlone", harness.SOURCE / "linuxPlatform" / "clipboard.py"
	)
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	module._onWaylandDisplay = lambda: True
	module._ownWindowHasKeyboard = lambda: False
	module.logged = logged
	module.spoken = spoken
	yield module
	if module._route is not None:
		module._route.wire.close()


@pytest.fixture
def fakeCompositor() -> Iterator[Callable[..., FakeCompositor]]:
	made: list[FakeCompositor] = []

	def make(**options: bool) -> FakeCompositor:
		made.append(FakeCompositor(**options))
		return made[-1]

	yield make
	for fake in made:
		fake.close()


def dataControlRoute(clipboard: types.ModuleType, fake: FakeCompositor):
	"""The data control route over fake, which clipboard then uses as its route."""
	clipboard._route = clipboard._DataControlRoute(
		clipboard._Wire(fake.path), "ext_data_control_manager_v1", "ext_data_control"
	)
	return clipboard._route


def test_theClipboardChangingAsLindaReadsItsCopyBackEndsNothing(
	clipboardAlone: types.ModuleType,
	fakeCompositor: Callable[..., FakeCompositor],
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	fake = fakeCompositor()
	route = dataControlRoute(clipboardAlone, fake)
	mainThread = threading.get_ident()
	pipe2 = os.pipe2
	changed: list[bool] = []

	def changeTheClipboardFirst(flags: int) -> tuple[int, int]:
		# Linda makes the pipe to read its copy back through just before it asks for the copy: a
		# clipboard manager takes the copy over in that moment, and the compositor's word of it is given
		# all the time it needs to reach Linda.
		if threading.get_ident() == mainThread and not changed:
			changed.append(True)
			since = len(fake.requests)
			fake.otherProgramCopies("Taken over")
			fake.waitForRequest(lambda request: request[:2] == ("ext_data_control_offer_v1", 1), since, 0.3)
		return pipe2(flags)

	monkeypatch.setattr(os, "pipe2", changeTheClipboardFirst)
	copied = route.copy("Taken over")
	assert changed
	assert fake.errors == []
	assert route.wire.alive
	assert copied
	assert route.read(1.0) == "Taken over"
