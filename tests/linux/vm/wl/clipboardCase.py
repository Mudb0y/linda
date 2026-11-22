"""The clipboard between Linda, which has no window with the keyboard, and a native GTK 3 program
that has it, with wl-paste and wl-copy beside them, held on each desktop to the outcome experiment
E17 found there (results/E17.md): through a window of Linda's own on GNOME, which has no data
control, and through ext_data_control_manager_v1 on sway and Plasma. Linda runs its own clipboard
code; a global plugin of the case's own copies as NVDA's commands do and reads as NVDA+c does. Each
step's outcome is logged as an E17 line before anything is asserted. The cases wlGnomeClipboard,
wlSwayClipboard and wlPlasmaClipboard run it.

The file has two halves. On the host, run(ground). In the guest, as clipboard-other, which a case's
module installs (cases/wlGnomeClipboard.nix), the other program:

	clipboardCase.py other DIRECTORY TITLE

a window titled TITLE with a text field, which takes commands as files in DIRECTORY/commands, each a
JSON list: ["paste"], ["copy", TEXT] and ["state"]; and writes one JSON object a line to
DIRECTORY/records.jsonl, of kind "active" whenever its window gains or loses the keyboard, "pasted"
with the clipboard's "text", "copied" and "state" with "active", each with "t" on the guest's
CLOCK_MONOTONIC.
"""

import base64
import json
import os
import sys
import time
from typing import TYPE_CHECKING

if __name__ != "__main__":
	from ground import BASE_SETTINGS, LINDA_DIRECTORY, TRANSCRIPT, USER, Mark

if TYPE_CHECKING:
	from ground import Ground

PROBE = "/tmp/ground/cbprobe"
HOOKS = "/tmp/ground/cbhooks"
OTHER = "/tmp/ground/cbother"
POLISH = "Zażółć gęślą jaźń"
OTHER_TITLE = "Clipboard window"
#: Copied one after another, so that a clipboard manager taking each over, as Plasma's does, is met.
TEXTS = (POLISH, "Second line\nthird line", "A third copy", "A fourth copy")
#: The route Linda's clipboard must take on each desktop, by the guest's kind, as the class name in
#: linuxPlatform.clipboard and the data control manager it binds.
EXPECTED_ROUTE = {
	"gnome": ("_WindowRoute", None),
	"sway": ("_DataControlRoute", "ext_data_control_manager_v1"),
	"plasma": ("_DataControlRoute", "ext_data_control_manager_v1"),
}

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
"""


class Probe:
	"""Commands to a directory a program polls, and the JSON lines it records."""

	def __init__(self, ground: "Ground", directory: str) -> None:
		self.ground = ground
		self.directory = directory
		self.count = 0

	def command(self, *words: str) -> float:
		self.count += 1
		data = base64.b64encode(json.dumps(words).encode()).decode()
		name = f"{self.count:04d}"
		given = self.ground.now()
		# Written aside and moved in, so that the program never reads half a command.
		self.ground.shell(
			f"echo {data} | base64 -d > {self.directory}/c-{name} && chown {USER} {self.directory}/c-{name} "
			f"&& mv {self.directory}/c-{name} {self.directory}/commands/{name}"
		)
		return given

	def records(self, kind: str, since: float = 0) -> list[dict]:
		return [
			r
			for r in self.ground.readJsonLines(f"{self.directory}/records.jsonl")
			if r["kind"] == kind and r["t"] >= since
		]

	def ask(self, command: str, kind: str, *args: str, timeout: float = 15) -> dict:
		since = self.command(command, *args)
		return self.ground.waitFor(
			lambda: (found := self.records(kind, since)) and found[0], f"{kind} after {command}", timeout
		)


def writeFile(ground: "Ground", path: str, text: str) -> None:
	data = base64.b64encode(text.encode()).decode()
	ground.shell(f"echo {data} | base64 -d > {path} && chown {USER} {path}")


def startLinda(ground: "Ground") -> Probe:
	"""Linda as Ground.startLinda starts it, with the case's plugin among the test hooks and the
	directory it polls in its environment; answers that directory's probe once the plugin is up."""
	hooks = ground.linda + "/share/linda/tests/linux/testHooks"
	ground.machine.succeed(
		f"install -d -o {USER} {PROBE} {PROBE}/commands {HOOKS} {HOOKS}/globalPlugins {LINDA_DIRECTORY} "
		f"{LINDA_DIRECTORY}/config",
		f"touch {PROBE}/records.jsonl && chown {USER} {PROBE}/records.jsonl",
		f"ln -sfn {hooks}/synthDrivers {HOOKS}/synthDrivers",
	)
	writeFile(ground, f"{HOOKS}/globalPlugins/clipboardProbe.py", PLUGIN)
	lines = ["schemaVersion = 25"]
	for section, values in BASE_SETTINGS.items():
		lines.append(f"[{section}]")
		lines += [f"\t{key} = {value}" for key, value in values.items()]
	writeFile(ground, f"{LINDA_DIRECTORY}/config/nvda.ini", "\n".join(lines) + "\n")
	ground.start(
		"linda",
		[
			f"{ground.linda}/bin/linda",
			"-c",
			f"{LINDA_DIRECTORY}/config",
			"-f",
			f"{LINDA_DIRECTORY}/nvda.log",
			"--debug-logging",
		],
		env={
			"LINDA_TEST_HOOKS_DIR": HOOKS,
			"LINDA_TRANSCRIPT_FILE": TRANSCRIPT,
			"LINDA_TRANSCRIPT_MODE": "immediate",
			"LINDA_CLIPBOARD_PROBE": PROBE,
		},
	)
	ground.lindaStarted = True
	probe = Probe(ground, PROBE)
	ready = ground.waitFor(lambda: probe.records("ready"), "Linda's clipboard probe", 120)[0]
	ground.machine.log(f"E17 Linda ready: {json.dumps(ready)}")
	return probe


def wlPaste(ground: "Ground") -> dict:
	answer = ground.run(["timeout", "5", "wl-paste", "--no-newline"])
	return {"returncode": answer.returncode, "stdout": answer.stdout, "stderr": answer.stderr.strip()}


def otherHasKeyboard(other: Probe) -> bool:
	return other.ask("state", "state")["active"]


def spokenSince(ground: "Ground", mark: "Mark") -> list[str]:
	ground.settle(0.8)
	return [text for _t, text in ground.speech(mark)]


def run(ground: "Ground") -> None:
	kind = ground.guest["kind"]
	with ground.subtest("the compositor's globals"):
		info = ground.run(["wayland-info"])
		interfaces = sorted(
			{line.split("'")[1] for line in info.stdout.splitlines() if "interface: '" in line}
		)
		ground.machine.log(f"E17 {kind} globals: {interfaces}")
		dataControl = [name for name in interfaces if "data_control" in name]
		ground.machine.log(f"E17 {kind} data control managers: {dataControl}")

	with ground.subtest("the other program has the keyboard, and Linda starts beside it"):
		ground.machine.succeed(
			f"install -d -o {USER} {OTHER} {OTHER}/commands",
			f"touch {OTHER}/records.jsonl && chown {USER} {OTHER}/records.jsonl",
		)
		ground.start("other", ["clipboard-other", OTHER, OTHER_TITLE], env={"GDK_BACKEND": "wayland"})
		other = Probe(ground, OTHER)
		ground.waitFor(lambda: other.records("active"), "the other program's window", 60)
		if not otherHasKeyboard(other):
			ground.activate(OTHER_TITLE)
		ground.waitFor(lambda: otherHasKeyboard(other), "the other program to have the keyboard", 30)
		linda = startLinda(ground)
		ground.expect("edit…", since=Mark(0, 0.0), timeout=120)
		ground.settle(1)
		ground.machine.log(f"E17 {kind} Linda's start: {ground.spoken()}")
		ground.waitFor(lambda: otherHasKeyboard(other), "the other program to keep the keyboard", 10)

	outcomes: dict[str, object] = {"copy": []}
	with ground.subtest("Linda copies, and the other program and wl-paste paste"):
		for text in TEXTS:
			mark = ground.mark()
			copied = linda.ask("copy", "copied", text)
			spoken = spokenSince(ground, mark)
			changes = [r["active"] for r in other.records("active", mark.t)]
			pasted = other.ask("paste", "pasted")["text"]
			pastedByWl = wlPaste(ground)
			ground.settle(1)
			outcome = {
				"copied": copied,
				"spoken": spoken,
				"otherKeyboardChanges": changes,
				"otherPasted": pasted,
				"wlPaste": pastedByWl,
				"otherHasKeyboardAfter": otherHasKeyboard(other),
			}
			ground.machine.log(f"E17 {kind} copy {text!r}: {json.dumps(outcome, ensure_ascii=False)}")
			outcomes["copy"].append(outcome)

	with ground.subtest("the other program copies, and Linda reads it"):
		# A data control client's copy, Linda's, leaves wlroots and mutter ignoring a focused program's
		# own copy until it has had a new key (E17), which a real Ctrl+C would give it.
		ground.press("shift")
		ground.settle(0.3)
		other.ask("copy", "copied", "Copied by the other program")
		byWl = wlPaste(ground)
		ground.settle(1)
		ground.waitFor(lambda: otherHasKeyboard(other), "the other program to have the keyboard", 10)
		mark = ground.mark()
		reported = linda.ask("report", "reported")
		spoken = spokenSince(ground, mark)
		changes = [r["active"] for r in other.records("active", mark.t)]
		outcome = {"wlPaste": byWl, "reported": reported, "spoken": spoken, "otherKeyboardChanges": changes}
		ground.machine.log(
			f"E17 {kind} the other program's copy read: {json.dumps(outcome, ensure_ascii=False)}"
		)
		outcomes["read"] = outcome
		ground.run(["sh", "-c", "wl-copy 'From the background' >/dev/null 2>&1"])
		ground.settle(1)
		ground.waitFor(lambda: otherHasKeyboard(other), "the other program to have the keyboard", 10)
		background = linda.ask("read", "read")
		ground.machine.log(f"E17 {kind} wl-copy's copy read: {json.dumps(background, ensure_ascii=False)}")
		outcomes["background"] = background

	ground.machine.log(
		f"E17 {kind} Linda's log, clipboard lines: "
		+ json.dumps([record for _t, record in ground.lindaLogRecords("clipboard")][-10:])
	)

	with ground.subtest("the outcome E17 expects of this desktop"):
		expected = EXPECTED_ROUTE[kind]
		for outcome, text in zip(outcomes["copy"], TEXTS, strict=True):
			copied = outcome["copied"]
			assert copied["copied"], outcome
			assert (copied["route"]["kind"], copied["route"]["manager"]) == expected, outcome
			assert outcome["otherPasted"] == text, outcome
			assert outcome["wlPaste"]["stdout"] == text, outcome
			assert outcome["otherHasKeyboardAfter"], outcome
			# Nothing but the copy, such as Linda's window coming and going; the transcript gives a line
			# end as a space.
			assert outcome["spoken"] == ["Copied to clipboard: " + " ".join(text.split())], outcome
		assert outcomes["read"]["spoken"] == ["Copied by the other program"], outcomes["read"]
		assert outcomes["background"]["text"] == "From the background", outcomes["background"]
	ground.stopLinda()


# The guest half.


def other(directory: str, title: str) -> None:
	import gi

	gi.require_version("Gtk", "3.0")
	gi.require_version("Gdk", "3.0")
	from gi.repository import Gdk, GLib, Gtk

	def out(kind: str, **fields: object) -> None:
		with open(os.path.join(directory, "records.jsonl"), "a", encoding="utf-8") as records:
			records.write(json.dumps({"kind": kind, "t": time.monotonic(), **fields}) + "\n")

	clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
	window = Gtk.Window(title=title)
	window.add(Gtk.Entry())
	window.set_default_size(300, 200)
	window.connect("notify::is-active", lambda w, _p: out("active", active=w.is_active()))
	window.connect("destroy", lambda _w: Gtk.main_quit())

	def poll() -> bool:
		commands = os.path.join(directory, "commands")
		for name in sorted(os.listdir(commands)):
			path = os.path.join(commands, name)
			with open(path, encoding="utf-8") as source:
				words = json.load(source)
			os.unlink(path)
			command, argument = words[0], (words[1] if len(words) > 1 else "")
			if command == "paste":
				clipboard.request_text(lambda _c, text, *_d: out("pasted", text=text))
			elif command == "copy":
				clipboard.set_text(argument, -1)
				Gdk.Display.get_default().sync()
				out("copied", text=argument)
			elif command == "state":
				out("state", active=window.is_active())
		return True

	GLib.timeout_add(50, poll)
	out("backend", display=type(Gdk.Display.get_default()).__name__)
	window.show_all()
	Gtk.main()


if __name__ == "__main__":
	if len(sys.argv) == 4 and sys.argv[1] == "other":
		other(sys.argv[2], sys.argv[3])
	else:
		sys.exit("usage: clipboardCase.py other DIRECTORY TITLE")
