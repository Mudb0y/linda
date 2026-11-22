"""Positions compared inside one window, read in the space Linda asks for them in: the screen's on X, and
each top-level window's own in a Wayland session, where no program knows where its windows are.

Measured on 22 November 2026 under rig Mutter and sway and the X rig, with GTK 4.24, GTK 3.24, Qt 6.12,
Firefox 160 and Chromium 157 (the tests print what Linda read, with -s):
- GTK 4 answers 0, 0 for every SCREEN position, on X too, and refuses SCREEN for a character; its
  WINDOW positions are right.
- GTK 3, Qt and Firefox answer SCREEN as WINDOW in a Wayland session.
- Chromium's SCREEN is measured from its window's corner in a Wayland session, as its components'
  WINDOW is. Its text's WINDOW is the page's own (a paragraph at 24, 109 has its first character at
  8, 8, under Mutter) and its offset at any WINDOW point is -1, on X as well, while its text's SCREEN
  agrees with its components'. So Chromium is still asked for SCREEN, which is all package WC's
  Chromium correction comes to.

Through the client in a child process: what extents, accessibleAtPoint, characterExtents and
offsetAtPoint answer for three radio buttons and a text, beside what the toolkit answers for each space
when asked itself. Through Linda: the place of each radio button in its group, which _inTreeOrder finds
from their positions since their parents differ, and vertical paragraph navigation in Firefox and
Chromium.

Under the X rig and each compositor, with and without Xwayland, through tests/linux/wayland-gate, or:

	nix develop . -c tests/linux/rig python3 -m pytest -q tests/linux/test_waylandCoordinates.py
	LINDA_RIG_COMPOSITOR=mutter nix develop . -c tests/linux/rig python3 -m pytest -q -s tests/linux/test_waylandCoordinates.py
"""

import errno
import json
import os
import shutil
import sys
import tempfile
import textwrap
import time
from collections.abc import Iterator
from pathlib import Path

import chromiumSession
import desktopSession
import harness
import pytest
import webSession
from desktopSession import DesktopSession

#: Seconds from starting Linda to its reading the driver's commands.
STARTUP_BOUND = 60.0
#: Seconds from starting a browser to its page in a buffer.
BUFFER_BOUND = 60.0
#: Seconds for one command of the driver to be answered.
ANSWER_BOUND = 10.0
#: Seconds a program's tree is given to settle after it is shown.
SETTLE = 2.0
#: Seconds Firefox takes to have every position once the first is asked of it, measured at 6 to 11 ms.
FIRST_POSITION_SECONDS = 0.2

#: Three radio buttons of one group, each in a row of its own, so that their parents differ and Linda
#: orders them by position, the middle one further right; then a text of two lines.
GTK4_APP = textwrap.dedent(
	"""\
	import gi

	gi.require_version("Gtk", "4.0")
	from gi.repository import Gtk

	app = Gtk.Application(application_id="org.linda.Coordinates4")


	def activate(application):
		window = Gtk.ApplicationWindow(application=application, title="Coordinates GTK 4")
		window.set_default_size(500, 400)
		outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
		first = None
		for name, indent in (("North", 0), ("Middle", 120), ("South", 0)):
			row = Gtk.Box(margin_start=indent)
			radio = Gtk.CheckButton(label=name)
			if first is None:
				first = radio
			else:
				radio.set_group(first)
			row.append(radio)
			outer.append(row)
		view = Gtk.TextView()
		view.get_buffer().set_text("One line\\nTwo line")
		outer.append(view)
		window.set_child(outer)
		window.present()


	app.connect("activate", activate)
	app.run([])
	""",
)
GTK3_APP = textwrap.dedent(
	"""\
	import gi

	gi.require_version("Gtk", "3.0")
	from gi.repository import Gtk

	window = Gtk.Window(title="Coordinates GTK 3")
	window.set_default_size(500, 400)
	outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
	first = None
	for name, indent in (("North", 0), ("Middle", 120), ("South", 0)):
		row = Gtk.Box(margin_start=indent)
		radio = Gtk.RadioButton.new_with_label_from_widget(first, name)
		first = first or radio
		row.pack_start(radio, False, False, 0)
		outer.pack_start(row, False, False, 0)
	view = Gtk.TextView()
	view.get_buffer().set_text("One line\\nTwo line")
	outer.pack_start(view, True, True, 0)
	window.add(outer)
	window.connect("destroy", Gtk.main_quit)
	window.show_all()
	Gtk.main()
	""",
)
QT_APP = textwrap.dedent(
	"""\
	import sys

	from PyQt6.QtWidgets import QApplication, QHBoxLayout, QRadioButton, QTextEdit, QVBoxLayout, QWidget

	app = QApplication(sys.argv[:1])
	window = QWidget()
	window.setWindowTitle("Coordinates Qt")
	outer = QVBoxLayout(window)
	for name, indent in (("North", 0), ("Middle", 120), ("South", 0)):
		row = QWidget()
		layout = QHBoxLayout(row)
		layout.setContentsMargins(indent, 0, 0, 0)
		layout.addWidget(QRadioButton(name))
		layout.addStretch()
		outer.addWidget(row)
	edit = QTextEdit()
	edit.setPlainText("One line\\nTwo line")
	outer.addWidget(edit)
	window.resize(500, 400)
	window.show()
	sys.exit(app.exec())
	""",
)
#: Paragraphs alternately at the left and 200 pixels in, then the radio buttons further in still, so
#: that none of them lines up with a paragraph.
PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>Coordinates page</title>
<style>p, div { margin: 4px 0; } .in { margin-left: 200px; } .far { margin-left: 400px; }
.farther { margin-left: 520px; }</style></head>
<body>
<p>Alpha left paragraph.</p>
<p class="in">Bravo indented paragraph.</p>
<p>Charlie left paragraph.</p>
<p class="in">Delta indented paragraph.</p>
<p>Echo left paragraph.</p>
<div class="far"><label><input type="radio" name="r"> North</label></div>
<div class="farther"><label><input type="radio" name="r"> Middle</label></div>
<div class="far"><label><input type="radio" name="r"> South</label></div>
</body></html>
"""
PAGE_TITLE = "Coordinates page"
#: Each program: its window's title as it starts, and the start of the text read.
PROGRAMS = {
	"gtk4": ("Coordinates GTK 4", "One line"),
	"gtk3": ("Coordinates GTK 3", "One line"),
	"qt": ("Coordinates Qt", "One line"),
	"firefox": (PAGE_TITLE, "Alpha left"),
	"chromium": (PAGE_TITLE, "Alpha left"),
}
RADIOS = ("North", "Middle", "South")

#: Run under harness.runChild with PROGRAMS, RADIOS, PROGRAM and TITLE set: what Linda's client answers
#: for the radio buttons and the text, beside what the toolkit answers when asked for each space itself.
CLIENT_CHILD = r"""
import time

import gi

gi.require_version("Atspi", "2.0")
from gi.repository import Atspi, GLib

from linuxPlatform.atspi import client as atspiClient
from linuxPlatform.atspi import textSource

c = atspiClient.client
TEXT_START = PROGRAMS[PROGRAM][1]


def box(rect):
	return [rect.x, rect.y, rect.width, rect.height]


def raw(node):
	found = {}
	for kind in ("SCREEN", "WINDOW"):
		try:
			found[kind] = box(Atspi.Component.get_extents(node.ref, getattr(Atspi.CoordType, kind)))
		except GLib.Error as e:
			found[kind] = str(e)
	return found


def search(top):
	radios = {}
	text = None
	stack = [top]
	seen = 0
	while stack and seen < 3000:
		node = stack.pop()
		seen += 1
		role = c.role(node)
		if role == "radio button" and c.name(node).strip() in RADIOS:
			radios[c.name(node).strip()] = node
		elif text is None and role != "radio button" and "Text" in c.interfaces(node):
			if c.text(node, 0, len(TEXT_START)) == TEXT_START:
				text = node
		stack.extend(reversed(c.children(node)))
	return radios, text


# Firefox has a second frame of the same name with nothing in it, GTK's own.
windows = [top for app in c.applications() for top in c.children(app) if c.name(top).startswith(TITLE)]
for top in windows:
	radios, text = search(top)
	if radios:
		break
assert sorted(radios) == sorted(RADIOS), (len(windows), sorted(radios))
assert text is not None, len(windows)
# Firefox answers nothing for the first position any client asks of it, and every one a moment later.
for node in [*radios.values(), text]:
	Atspi.Component.get_extents(node.ref, Atspi.CoordType.WINDOW)
Atspi.Text.get_character_extents(text.ref, 0, Atspi.CoordType.WINDOW)
time.sleep(FIRST_POSITION_SECONDS)


def described(extents):
	if extents is None:
		return None
	window = getattr(extents, "window", None)
	return {
		"box": list(extents),
		"type": type(extents).__name__,
		"window": c.name(window) if window is not None else None,
	}


result = {"toolkit": c.toolkit(top), "radios": {}, "windowCoordinates": atspiClient.windowCoordinates()}
for name, node in radios.items():
	extents = c.extents(node)
	entry = {"client": described(extents), "raw": raw(node)}
	if extents is not None:
		x, y, width, height = extents
		hit = top
		for _ in range(40):
			deeper = c.accessibleAtPoint(hit, x + width // 2, y + height // 2)
			if deeper is None or deeper == hit:
				break
			hit = deeper
		entry["hit"] = [c.role(hit), c.name(hit)]
	result["radios"][name] = entry
source = textSource.TextSource(text, textSource.unitFor(c.toolkit(text)))
entry = {"client": described(c.extents(text)), "raw": raw(text)}
try:
	character = source.characterExtents(0)
except atspiClient.NodeGone as e:
	entry["character"] = str(e)
else:
	entry["character"] = described(character)
	x, y, width, height = character
	entry["offsetAtCharacter"] = source.offsetAtPoint(x + max(width, 1) // 2, y + max(height, 1) // 2)
for kind in ("SCREEN", "WINDOW"):
	try:
		entry["rawCharacter" + kind] = box(
			Atspi.Text.get_character_extents(text.ref, 0, getattr(Atspi.CoordType, kind))
		)
	except GLib.Error as e:
		entry["rawCharacter" + kind] = str(e)
result["text"] = entry
"""

#: Runs, on Linda's main thread, the commands a test writes to the FIFO LINDA_COORDINATES_FIFO names,
#: one per line, and answers each with a line of JSON in the file LINDA_COORDINATES_ANSWERS names:
#: "ready" answers only; "gesture NAME" runs a key gesture as if Linda had captured the keys; "browse
#: NAME" runs browse mode's script_NAME in the focus's document; "radios TITLE" gives the place in its
#: group, the location and the location's window of each radio button in the window TITLE starts;
#: "position" gives the focus's location.
DRIVER_PLUGIN = '''\
"""The driver of test_waylandCoordinates.py."""

import json
import os
import threading

import api
import controlTypes
import globalPluginHandler
import inputCore
import keyboardHandler
import queueHandler
import scriptHandler
from linuxPlatform.atspi import client as atspiClient
from logHandler import log

FIFO = os.environ.get("LINDA_COORDINATES_FIFO")
ANSWERS = os.environ.get("LINDA_COORDINATES_ANSWERS")


def _radios(title):
	window = next(
		(w for w in api.getDesktopObject().children if (w.name or "").startswith(title)),
		None,
	)
	if window is None:
		return {}
	found = {}
	stack = [window]
	seen = 0
	while stack and seen < 3000:
		obj = stack.pop()
		seen += 1
		if obj.role == controlTypes.Role.RADIOBUTTON:
			location = obj.location
			windowNode = getattr(location, "window", None)
			info = obj.positionInfo
			found[(obj.name or "").strip()] = {
				"place": [info.get("indexInGroup"), info.get("similarItemsInGroup")],
				"location": list(location) if location else None,
				"window": atspiClient.client.name(windowNode) if windowNode is not None else None,
			}
		stack.extend(reversed(obj.children))
	return found


class GlobalPlugin(globalPluginHandler.GlobalPlugin):
	def __init__(self):
		super().__init__()
		if FIFO:
			threading.Thread(target=self._read, name="coordinatesDriver", daemon=True).start()

	def _read(self):
		with open(FIFO, encoding="utf-8") as commands:
			for line in commands:
				command, _, argument = line.strip().partition(" ")
				queueHandler.queueFunction(queueHandler.eventQueue, self._run, command, argument)

	def _run(self, command, argument):
		log.info(f"coordinates driver: {command}")
		answer = {"command": command}
		try:
			if command == "gesture":
				inputCore.manager.executeGesture(keyboardHandler.KeyboardInputGesture.fromName(argument))
			elif command == "browse":
				document = api.getFocusObject().treeInterceptor
				gesture = keyboardHandler.KeyboardInputGesture.fromName("NVDA+tab")
				scriptHandler.executeScript(getattr(document, f"script_{argument}"), gesture)
			elif command == "radios":
				answer["radios"] = _radios(argument)
			elif command == "position":
				answer["location"] = list(api.getFocusObject().location or ())
		except Exception as e:
			log.debugWarning("coordinates driver", exc_info=True)
			answer["error"] = repr(e)
		with open(ANSWERS, "a", encoding="utf-8") as answers:
			answers.write(json.dumps(answer) + "\\n")
'''

CONFIG = """\
schemaVersion = 25
[general]
	language = en
	playStartAndExitSounds = False
	showWelcomeDialogAtStartup = False
[speech]
	synth = transcript
[braille]
	display = noBraille
[virtualBuffers]
	autoSayAllOnPageLoad = False
[addonStore]
	automaticUpdates = disabled
	baseServerURL = http://127.0.0.1:1
"""


def sessionName() -> str:
	return harness.rigSession() + ("-xwayland" if harness.rigHasXwayland() else "")


def isWayland() -> bool:
	return harness.rigSession() != "x11"


@pytest.fixture
def desktop(tmp_path: Path) -> Iterator[DesktopSession]:
	with DesktopSession(tmp_path / "desktop") as session:
		yield session


@pytest.fixture
def socketDir() -> Iterator[Path]:
	"""Where Linda looks for linda-keyd and finds none: a socket's path may be no longer than 107 bytes,
	and pytest's folders can be longer."""
	directory = Path(tempfile.mkdtemp(prefix="keyd", dir="/tmp"))
	try:
		yield directory
	finally:
		shutil.rmtree(directory, ignore_errors=True)


def startProgram(desktop: DesktopSession, program: str, tmp_path: Path, *switches: str) -> None:
	"""Start program on its fixture, wait until its window is on the desktop with the keyboard, and give
	it SETTLE seconds more; switches are Chromium's."""
	title = PROGRAMS[program][0]
	if program in ("gtk4", "gtk3", "qt"):
		script = tmp_path / f"{program}.py"
		script.write_text({"gtk4": GTK4_APP, "gtk3": GTK3_APP, "qt": QT_APP}[program])
		desktop.startApp([sys.executable, str(script)], title=title, timeout=30)
		desktopSession.activate(title)
	else:
		page = tmp_path / "coordinates.html"
		page.write_text(PAGE)
		if program == "firefox":
			webSession.startFirefox(desktop, page.as_uri())
		else:
			chromiumSession._start(
				desktop,
				[
					"chromium",
					f"--user-data-dir={tmp_path / 'chromium'}",
					*chromiumSession.CHROMIUM_SWITCHES,
					*chromiumSession._platformSwitches(),
					*switches,
					page.as_uri(),
				],
				window=f"{title}{chromiumSession.CHROMIUM_TITLE_SUFFIX}",
				logName="chromium.log",
			)
	time.sleep(SETTLE)


def readThroughTheClient(tmp_path: Path, program: str) -> dict:
	done = harness.runChild(
		tmp_path,
		f"PROGRAMS = {PROGRAMS!r}\nRADIOS = {RADIOS!r}\nPROGRAM = {program!r}\n"
		f"FIRST_POSITION_SECONDS = {FIRST_POSITION_SECONDS!r}\n"
		f"TITLE = {PROGRAMS[program][0]!r}\n" + CLIENT_CHILD,
		timeout=60,
	)
	assert done.error is None, f"{done.error}\n{done.stderr[-6000:]}"
	print(f"{sessionName()}: {program} through the client: {json.dumps(done.value)}")
	return done.value


def assertInOrder(boxes: dict[str, list[int]]) -> None:
	"""North above Middle above South, Middle further right, North and South at one left edge."""
	north, middle, south = (boxes[name] for name in RADIOS)
	assert north[1] < middle[1] < south[1], boxes
	assert north[0] == south[0] < middle[0], boxes


def inside(inner: list[int], outer: list[int]) -> bool:
	return (
		outer[0] <= inner[0]
		and outer[1] <= inner[1]
		and inner[0] + inner[2] <= outer[0] + outer[2]
		and inner[1] + inner[3] <= outer[1] + outer[3]
	)


@pytest.mark.parametrize("program", list(PROGRAMS))
def test_theClientAsksInTheSessionsSpace(desktop: DesktopSession, tmp_path: Path, program: str) -> None:
	if program == "chromium":
		# Chromium builds a page's tree only for a client that shows a screen reader's interest, which
		# the client's questions alone do not.
		chromiumSession._status("Set", "string:IsEnabled", "variant:boolean:true")
	startProgram(desktop, program, tmp_path, "--force-renderer-accessibility")
	found = readThroughTheClient(tmp_path, program)
	radios = found["radios"]
	text = found["text"]
	assert found["windowCoordinates"] is isWayland()
	if not isWayland():
		# The X route as it was: SCREEN, as plain tuples with no window, whatever the toolkit answers.
		for entry in [*radios.values(), text]:
			assert entry["client"]["type"] == "tuple", entry
			assert entry["client"]["box"] == entry["raw"]["SCREEN"], entry
		if program == "gtk4":
			# GTK 4 answers 0, 0 for every SCREEN position and refuses SCREEN for a character, on X as
			# much as anywhere; mending that on X is the owner's decision D8.
			assert {tuple(entry["client"]["box"][:2]) for entry in radios.values()} == {(0, 0)}, radios
			assert "Unsupported coordinate space" in text["character"], text
			return
		assert text["character"]["type"] == "tuple", text
		assert text["character"]["box"] == text["rawCharacterSCREEN"], text
	else:
		space = "SCREEN" if program == "chromium" else "WINDOW"
		assert (found["toolkit"] == "Chromium") is (program == "chromium"), found["toolkit"]
		for entry in [*radios.values(), text]:
			assert entry["client"]["type"] == "WindowExtents", entry
			assert entry["client"]["box"] == entry["raw"][space], entry
			assert entry["client"]["window"].startswith(PROGRAMS[program][0]), entry
		assert text["character"]["type"] == "WindowExtents", text
		assert text["character"]["box"] == text["rawCharacter" + space], text
		assert text["character"]["window"].startswith(PROGRAMS[program][0]), text
	assertInOrder({name: entry["client"]["box"] for name, entry in radios.items()})
	for name, entry in radios.items():
		assert entry["hit"][0] == "radio button" or name in entry["hit"][1], (name, entry)
	assert inside(text["character"]["box"], text["client"]["box"]), text
	assert text["offsetAtCharacter"] == 0, text


class Reader:
	"""Linda with the driver plugin, beside a desktop's programs, given commands and listened to."""

	def __init__(self, desktop: DesktopSession, directory: Path, socketPath: Path) -> None:
		self.desktop = desktop
		hooks = directory / "hooks"
		(hooks / "globalPlugins").mkdir(parents=True)
		(hooks / "synthDrivers").symlink_to(harness.HERE / "synthDrivers")
		(hooks / "globalPlugins" / "coordinatesDriver.py").write_text(DRIVER_PLUGIN)
		self.fifo = directory / "commands.fifo"
		os.mkfifo(self.fifo)
		self.answers = directory / "answers.jsonl"
		self.answers.touch()
		config = directory / "nvda.ini"
		config.write_text(CONFIG)
		self.launch = desktop.startNVDA(
			config,
			env={
				"LINDA_TEST_HOOKS_DIR": str(hooks),
				"LINDA_TEST_KEYD_SOCKET": str(socketPath),
				"LINDA_COORDINATES_FIFO": str(self.fifo),
				"LINDA_COORDINATES_ANSWERS": str(self.answers),
			},
		)
		self._commands: int | None = None
		self._answered = 0

	def _connect(self) -> int:
		deadline = time.monotonic() + STARTUP_BOUND
		while True:
			try:
				return os.open(self.fifo, os.O_WRONLY | os.O_NONBLOCK)
			except OSError as e:
				# Nobody reads the FIFO until Linda has loaded the plugin.
				if (
					e.errno != errno.ENXIO
					or time.monotonic() >= deadline
					or self.launch.process.poll() is not None
				):
					raise AssertionError(f"Linda never read its commands\n{self.launch.describe()}") from None
			time.sleep(0.05)

	def command(self, line: str) -> dict:
		"""Send line and wait for its answer."""
		if self._commands is None:
			self._commands = self._connect()
		os.write(self._commands, f"{line}\n".encode())
		deadline = time.monotonic() + ANSWER_BOUND
		while True:
			lines = self.answers.read_text().splitlines()
			if len(lines) > self._answered:
				self._answered += 1
				answer = json.loads(lines[self._answered - 1])
				assert "error" not in answer, (line, answer, self.launch.text("nvda.log")[-6000:])
				return answer
			assert time.monotonic() < deadline, f"no answer to {line!r}\n{self.launch.describe()}"
			time.sleep(0.05)

	def said(self, line: str, expected: str, timeout: float = 5) -> None:
		"""Send line, and require expected among what Linda then says."""
		mark = self.desktop.mark()
		self.command(line)
		self.desktop.expect(expected, since=mark, timeout=timeout)

	def quit(self) -> None:
		if self._commands is not None:
			os.close(self._commands)
			self._commands = None
		self.desktop.close()
		assert self.launch.process.returncode == 0, self.launch.describe()
		self.launch.assertClean(logNames=["nvda.log"])


def startReader(desktop: DesktopSession, tmp_path: Path, socketDir: Path, program: str) -> Reader:
	"""Linda first, then the program; for Chromium the accessibility status is off before Linda starts,
	as a desktop has it before a screen reader runs, so that Chromium joins the desktop for Linda."""
	if program in ("firefox", "chromium"):
		webSession.requireBrowseLibraries()
	if program == "chromium":
		chromiumSession.clearAccessibilityStatus()
	reader = Reader(desktop, tmp_path / "linda", socketDir / "socket")
	reader.command("ready")
	startProgram(desktop, program, tmp_path)
	if program in ("firefox", "chromium"):
		reader.launch.waitForLog("Buffer rendered", BUFFER_BOUND)
		time.sleep(SETTLE)
	if not isWayland():
		# On X, Firefox's answer to the first position Linda asks of it is lost, and with it the first
		# vertical paragraph navigation; the X route is left as it was. In a Wayland session Linda waits
		# the moment Firefox takes, which these tests show by asking nothing first.
		reader.command("position")
		time.sleep(FIRST_POSITION_SECONDS)
	return reader


@pytest.mark.parametrize("program", ["gtk4", "gtk3", "firefox", "chromium"])
def test_radioButtonsAreCountedInTheOrderTheyAreSeen(
	desktop: DesktopSession, tmp_path: Path, socketDir: Path, program: str
) -> None:
	reader = startReader(desktop, tmp_path, socketDir, program)
	radios = reader.command(f"radios {PROGRAMS[program][0]}")["radios"]
	print(f"{sessionName()}: {program} through Linda: {json.dumps(radios)}")
	assert sorted(radios) == sorted(RADIOS), radios
	if program == "gtk4":
		# GTK 4 gives its radio buttons no member-of relation, so each is counted among its own
		# siblings, in one session as in the other, and _inTreeOrder is not reached.
		assert [radios[name]["place"] for name in RADIOS] == [[1, 1], [1, 1], [1, 1]], radios
	else:
		assert [radios[name]["place"] for name in RADIOS] == [[1, 3], [2, 3], [3, 3]], radios
	if isWayland() or program != "gtk4":
		assertInOrder({name: entry["location"] for name, entry in radios.items()})
	for entry in radios.values():
		if isWayland():
			assert entry["window"].startswith(PROGRAMS[program][0]), radios
		else:
			assert entry["window"] is None, radios
	reader.quit()


@pytest.mark.parametrize("program", ["firefox", "chromium"])
def test_verticalParagraphNavigation(
	desktop: DesktopSession, tmp_path: Path, socketDir: Path, program: str
) -> None:
	reader = startReader(desktop, tmp_path, socketDir, program)
	reader.said("gesture control+home", "Alpha left paragraph.")
	reader.said("browse nextVerticalParagraph", "Charlie left paragraph.")
	reader.said("browse nextVerticalParagraph", "Echo left paragraph.")
	reader.said("browse nextVerticalParagraph", "no next vertically aligned paragraph")
	reader.said("browse previousVerticalParagraph", "Charlie left paragraph.")
	reader.said("gesture downArrow", "Delta indented paragraph.")
	reader.said("browse previousVerticalParagraph", "Bravo indented paragraph.")
	reader.said("browse previousVerticalParagraph", "no previous vertically aligned paragraph")
	reader.quit()
