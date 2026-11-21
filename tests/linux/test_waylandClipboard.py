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

tests/linux/wayland-gate SCRATCH runs this file under Mutter and sway, with and without Xwayland; the X
rig runs none of it, and test_clipboard.py holds the X display to what it always did.
"""

import json
import os
import signal
import subprocess
import sys
import threading
import time
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
