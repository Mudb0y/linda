"""A window that opens in a Wayland session is spoken, its title and then its focus, with no key pressed.

Under headless Mutter and headless sway, for GTK 4, Qt 6 and Electron windows as their toolkits announce
them, and for a GTK 4 window whose activation Linda never hears of: experiment E3 found that GTK 4 now and
then sends neither window:activate, nor the active state, nor the focus for a window it maps, though its
states say active afterwards. A test plugin makes that happen every time, by losing those events from
Linda's queue. Under sway the window can also be kept from the keyboard as it maps and given it seconds
later, as E3 saw activation come late.

Under the X rig one test runs this file again in a rig of each compositor; tests/linux/wayland-gate runs
it in each, with and without Xwayland:

nix develop . -c tests/linux/rig python3 -m pytest tests/linux/test_waylandFocus.py
"""

import itertools
import json
import os
import subprocess
import sys
import textwrap
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import harness
import pytest

BOOT_CONFIG = harness.HERE / "fixtures" / "bootConfig" / "nvda.ini"
#: Seconds from starting NVDA until it has initialized; a start under the rig takes about three.
STARTUP_BOUND = 60.0
#: Seconds with nothing new spoken after which NVDA is taken to have finished speaking.
QUIET = 1.0
#: Seconds from a window having the keyboard to its title and focus having been spoken.
SPOKEN_BOUND = 8.0
#: Seconds a window kept from the keyboard as it maps waits for it, as E3 measured 2.3 to 4.1.
LATE_SECONDS = 2.5
#: Seconds past the last of handler.SHOWN_PROBE_SECONDS.
NEVER_ACTIVE_SECONDS = 7.5
#: handler._lookForInitialFocus's line, how many times it is written when no focus is found, and a little
#: more than the seconds between them, after which the last scheduled look has ended.
STARTUP_LOOK = "AT-SPI focus: none found yet; looking again"
STARTUP_LOOKS = 4
STARTUP_LOOK_GAP = 3.0

wayland = pytest.mark.rigSession("wayland")
swayOnly = pytest.mark.rigSession("sway")

#: Loses, while the file LINDA_MISSED_ACTIVATION names exists, every event that says a window was
#: activated or deactivated or that an object took the focus, as GTK 4 sends none of them when it
#: misses. Writes that name with ".ready" added once it is in place.
MISSING = textwrap.dedent(
	'''\
	"""Loses window activation and focus events while a file exists, for test_waylandFocus.py."""

	import collections
	import os

	import globalPluginHandler
	from linuxPlatform.atspi import handler

	FLAG = os.environ.get("LINDA_MISSED_ACTIVATION")
	LOST = frozenset(
		{
			"window:activate",
			"window:deactivate",
			"object:state-changed:active",
			"object:state-changed:focused",
		}
	)


	class _Losing(collections.deque):
		def append(self, event):
			if event[0] in LOST and os.path.exists(FLAG):
				return
			super().append(event)


	class GlobalPlugin(globalPluginHandler.GlobalPlugin):
		def __init__(self):
			super().__init__()
			if not FLAG:
				return
			with handler._queueLock:
				handler._queue = _Losing(handler._queue, maxlen=handler.MAX_QUEUED)
			with open(FLAG + ".ready", "w"):
				pass
	''',
)

#: A GTK 4 window named by its argument, with two buttons, the first focused; says "active" once it is.
GTK4_WINDOW = textwrap.dedent(
	"""\
	import sys

	import gi

	gi.require_version("Gtk", "4.0")
	from gi.repository import Gtk

	app = Gtk.Application(application_id="org.linda.WaylandFocus" + sys.argv[2])


	def activate(application):
		window = Gtk.ApplicationWindow(application=application, title=sys.argv[1])
		window.set_default_size(400, 200)
		box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
		for label in ("First", "Second"):
			box.append(Gtk.Button(label=label))
		window.set_child(box)
		window.connect("notify::is-active", lambda w, _p: w.is_active() and print("active", flush=True))
		print("backend", type(window.get_display()).__name__, flush=True)
		window.present()


	app.connect("activate", activate)
	app.run([])
	""",
)

#: The same in Qt 6.
QT6_WINDOW = textwrap.dedent(
	"""\
	import sys

	from PyQt6.QtCore import QEvent
	from PyQt6.QtWidgets import QApplication, QMainWindow, QPushButton, QVBoxLayout, QWidget

	app = QApplication(sys.argv[:1])


	class Window(QMainWindow):
		def event(self, event):
			if event.type() == QEvent.Type.WindowActivate:
				print("active", flush=True)
			return super().event(event)


	window = Window()
	window.setWindowTitle(sys.argv[1])
	central = QWidget()
	window.setCentralWidget(central)
	layout = QVBoxLayout(central)
	first = QPushButton("First")
	layout.addWidget(first)
	layout.addWidget(QPushButton("Second"))
	window.resize(400, 200)
	first.setFocus()
	print("backend", app.platformName(), flush=True)
	window.show()
	sys.exit(app.exec())
	""",
)

#: The same in Electron, its page's first button focused.
ELECTRON_MAIN = textwrap.dedent(
	"""\
	const { app, BrowserWindow } = require("electron");

	const title = process.env.LINDA_WINDOW_TITLE;
	app.whenReady().then(() => {
		const window = new BrowserWindow({ width: 600, height: 400, autoHideMenuBar: true, title });
		window.setMenu(null);
		window.on("focus", () => console.log("active"));
		window.loadURL(
			"data:text/html," +
				encodeURIComponent(`<title>${title}</title><button autofocus>First</button><button>Second</button>`)
		);
		console.log("backend", app.commandLine.getSwitchValue("ozone-platform"));
	});

	app.on("window-all-closed", () => app.quit());
	""",
)


@pytest.mark.rigSession("x11")
@pytest.mark.parametrize("compositor", ["mutter", "sway"])
def test_waylandSessions(tmp_path: Path, compositor: str) -> None:
	# The X session's choices, which a Wayland rig refuses; many tests' usage lines set LINDA_RIG_WM.
	env = {
		name: value
		for name, value in os.environ.items()
		if not name.startswith(("LINDA_RIG_SESSION", "LINDA_RIG_WM"))
	}
	env.update(LINDA_RIG_COMPOSITOR=compositor)
	ran = subprocess.run(
		[
			str(harness.HERE / "rig"),
			"--bind",
			str(tmp_path),
			sys.executable,
			"-m",
			"pytest",
			"-q",
			"-p",
			"no:cacheprovider",
			f"--basetemp={tmp_path / 'pt'}",
			__file__,
		],
		env=env,
		capture_output=True,
		text=True,
		timeout=900,
		check=False,
	)
	assert ran.returncode == 0, ran.stdout[-6000:] + ran.stderr[-3000:]


class Linda:
	"""One NVDA for every test here, with the plugin that can lose its window events."""

	def __init__(self, directory: Path) -> None:
		directory.mkdir(parents=True)
		config = directory / "config"
		config.mkdir()
		text = BOOT_CONFIG.read_text(encoding="utf-8")
		(config / "nvda.ini").write_text(
			text.replace("[general]\n", "[general]\n\tshowWelcomeDialogAtStartup = False\n", 1),
			encoding="utf-8",
		)
		hooks = directory / "hooks"
		(hooks / "globalPlugins").mkdir(parents=True)
		(hooks / "globalPlugins" / "missedActivation.py").write_text(MISSING, encoding="utf-8")
		(hooks / "synthDrivers").symlink_to(harness.HERE / "synthDrivers")
		self.flag = directory / "missing"
		run = directory / "run"
		self.launch = harness.startLauncher(
			run,
			"-c",
			str(config),
			"-f",
			str(run / "nvda.log"),
			"--debug-logging",
			env={"LINDA_TEST_HOOKS_DIR": str(hooks), "LINDA_MISSED_ACTIVATION": str(self.flag)},
		)
		self.launch.waitForLog("NVDA initialized", STARTUP_BOUND)
		deadline = time.monotonic() + 10
		while not Path(str(self.flag) + ".ready").exists():
			assert time.monotonic() < deadline, f"the plugin never started\n{self.launch.describe()}"
			time.sleep(0.05)
		# NVDA looks for a focus again a few times after it starts while it has none, which would find a
		# window opened meanwhile whatever was or was not heard of it.
		deadline = time.monotonic() + STARTUP_BOUND
		while self.launch.text("nvda.log").count(STARTUP_LOOK) < STARTUP_LOOKS:
			assert time.monotonic() < deadline, (
				f"NVDA's looks for a focus never ended\n{self.launch.describe()}"
			)
			time.sleep(0.1)
		time.sleep(STARTUP_LOOK_GAP)
		self.waitForQuiet()

	def mark(self) -> int:
		return len(self.launch.spoken())

	def spokenSince(self, mark: int) -> list[str]:
		return self.launch.spokenTexts()[mark:]

	def waitForQuiet(self, limit: float = 15) -> None:
		end = time.monotonic() + limit
		count = self.mark()
		last = time.monotonic()
		while time.monotonic() < end:
			time.sleep(0.05)
			now = self.mark()
			if now != count:
				count, last = now, time.monotonic()
			elif time.monotonic() - last >= QUIET:
				return

	def expect(self, mark: int, *texts: str, timeout: float = SPOKEN_BOUND) -> None:
		"""Wait until texts have been spoken since mark, in this order, other things between them allowed."""
		deadline = time.monotonic() + timeout
		while True:
			spoken = self.spokenSince(mark)
			rest = iter(spoken)
			if all(any(said == text for said in rest) for text in texts):
				return
			assert time.monotonic() < deadline, (
				f"{texts} not spoken within {timeout} s; spoken: {spoken}\n{self.launch.text('nvda.log')[-8000:]}"
			)
			time.sleep(0.05)

	def stop(self) -> None:
		self.launch.stop()
		harness.stopRunningCopy()


@pytest.fixture(scope="module")
def linda(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Linda]:
	running = Linda(tmp_path_factory.mktemp("linda") / "nvda")
	try:
		yield running
	finally:
		running.stop()


#: Numbers for GTK 4 application ids: a second window under one id would be handed to the first.
_windowNumbers = itertools.count()


class Window:
	"""A program of one window, started with its title, and what it printed."""

	def __init__(self, toolkit: str, title: str, directory: Path) -> None:
		self.title = title
		directory.mkdir(parents=True, exist_ok=True)
		env = dict(os.environ)
		if toolkit == "gtk4":
			script = directory / "gtk4Window.py"
			script.write_text(GTK4_WINDOW, encoding="utf-8")
			command = [sys.executable, str(script), title, str(next(_windowNumbers))]
		elif toolkit == "qt6":
			script = directory / "qt6Window.py"
			script.write_text(QT6_WINDOW, encoding="utf-8")
			command = [sys.executable, str(script), title]
			env.update(QT_QPA_PLATFORM="wayland", QT_LINUX_ACCESSIBILITY_ALWAYS_ON="1")
			if os.environ.get("LINDA_QT6_PLUGIN_PATH"):
				env.setdefault("QT_PLUGIN_PATH", os.environ["LINDA_QT6_PLUGIN_PATH"])
		else:
			program = directory / "electronWindow"
			program.mkdir(exist_ok=True)
			(program / "main.js").write_text(ELECTRON_MAIN, encoding="utf-8")
			(program / "package.json").write_text(
				json.dumps({"name": "linda-wayland-focus", "version": "1.0.0", "main": "main.js"}),
				encoding="utf-8",
			)
			command = [
				"electron",
				"--ozone-platform=wayland",
				"--disable-gpu",
				f"--user-data-dir={directory / 'electronProfile'}",
				str(program),
			]
			env["LINDA_WINDOW_TITLE"] = title
		self.stderr = directory / f"{toolkit}.stderr.txt"
		with open(self.stderr, "w") as stderr:
			self.process = subprocess.Popen(
				command, stdout=subprocess.PIPE, stderr=stderr, stdin=subprocess.DEVNULL, text=True, env=env
			)
		self.lines: list[str] = []
		threading.Thread(target=self._read, daemon=True).start()
		self.backend = self.waitFor(lambda line: line.startswith("backend ")).split()[1]

	def _read(self) -> None:
		for line in self.process.stdout:
			self.lines.append(line.rstrip("\n"))

	def waitFor(self, predicate, timeout: float = 30) -> str:
		deadline = time.monotonic() + timeout
		while True:
			for line in list(self.lines):
				if predicate(line):
					return line
			assert self.process.poll() is None, f"{self.title} ended: {self.stderr.read_text()[-3000:]}"
			assert time.monotonic() < deadline, f"{self.title} printed {self.lines}"
			time.sleep(0.02)

	def isActive(self) -> bool:
		return "active" in self.lines

	def waitUntilActive(self, timeout: float = 30) -> None:
		self.waitFor(lambda line: line == "active", timeout)

	def close(self) -> None:
		self.process.terminate()
		try:
			self.process.wait(10)
		except subprocess.TimeoutExpired:
			self.process.kill()
			self.process.wait()


@pytest.fixture
def windows(tmp_path: Path) -> Iterator[list[Window]]:
	opened: list[Window] = []
	yield opened
	for window in opened:
		window.close()


def _sway(*command: str) -> None:
	subprocess.run(["swaymsg", *command], check=True, capture_output=True, text=True)


#: Titles with no digit beside a letter, which speech would part.
TITLES = {"gtk4": "Gtk window", "qt6": "Qt window", "electron": "Electron window"}
#: What NVDA says for each toolkit's window after its title, with no key pressed.
FOCUS = {"gtk4": "First button", "qt6": "First button", "electron": "First button"}


@wayland
@pytest.mark.parametrize("toolkit", ["gtk4", "qt6", "electron"])
def test_newWindowSpeaksTitleAndFocus(
	linda: Linda, windows: list[Window], tmp_path: Path, toolkit: str
) -> None:
	title = TITLES[toolkit]
	mark = linda.mark()
	window = Window(toolkit, title, tmp_path)
	windows.append(window)
	window.waitUntilActive()
	linda.expect(mark, title, FOCUS[toolkit])
	linda.waitForQuiet()
	spoken = linda.spokenSince(mark)
	assert spoken.count(title) == (2 if toolkit == "electron" else 1), spoken
	window.close()
	windows.remove(window)
	linda.waitForQuiet()


@wayland
def test_missedActivationIsFound(linda: Linda, windows: list[Window], tmp_path: Path) -> None:
	title = "Missed window"
	linda.flag.touch()
	try:
		mark = linda.mark()
		window = Window("gtk4", title, tmp_path)
		windows.append(window)
		window.waitUntilActive()
		linda.expect(mark, title, FOCUS["gtk4"])
		linda.waitForQuiet()
		assert linda.spokenSince(mark).count(title) == 1, linda.spokenSince(mark)
	finally:
		linda.flag.unlink()
	window.close()
	windows.remove(window)
	linda.waitForQuiet()


@swayOnly
@pytest.mark.parametrize("missing", [False, True], ids=["heard", "missed"])
def test_lateActivationIsFound(linda: Linda, windows: list[Window], tmp_path: Path, missing: bool) -> None:
	"""A window that maps without the keyboard and is given it LATE_SECONDS later, as sway does for one its
	no_focus rule names: spoken once it has the keyboard, whether or not its activation is heard."""
	holder = Window("gtk4", "Holder window", tmp_path / "holder")
	windows.append(holder)
	holder.waitUntilActive()
	linda.waitForQuiet()
	title = "Late window missed" if missing else "Late window heard"
	_sway(f'no_focus [title="^{title}$"]')
	if missing:
		linda.flag.touch()
	try:
		mark = linda.mark()
		window = Window("gtk4", title, tmp_path / "late")
		windows.append(window)
		time.sleep(LATE_SECONDS)
		assert not window.isActive(), "sway gave the window the keyboard as it mapped"
		assert title not in linda.spokenSince(mark), linda.spokenSince(mark)
		_sway(f'[title="^{title}$"]', "focus")
		window.waitUntilActive()
		linda.expect(mark, title, FOCUS["gtk4"])
		linda.waitForQuiet()
		assert linda.spokenSince(mark).count(title) == 1, linda.spokenSince(mark)
	finally:
		if missing:
			linda.flag.unlink()
	linda.waitForQuiet()


@swayOnly
def test_windowThatNeverHasTheKeyboardIsNotSpoken(
	linda: Linda, windows: list[Window], tmp_path: Path
) -> None:
	"""A window that maps without the keyboard and never gets it is read for an activation for some seconds
	and found to have none: nothing is said, and the window that has the keyboard keeps NVDA's focus."""
	holder = Window("gtk4", "Holder window", tmp_path / "holder")
	windows.append(holder)
	holder.waitUntilActive()
	linda.waitForQuiet()
	title = "Background window"
	_sway(f'no_focus [title="^{title}$"]')
	mark = linda.mark()
	logBefore = len(linda.launch.text("nvda.log"))
	window = Window("gtk4", title, tmp_path / "background")
	windows.append(window)
	time.sleep(NEVER_ACTIVE_SECONDS)
	assert not window.isActive(), "sway gave the window the keyboard"
	assert linda.spokenSince(mark) == [], linda.spokenSince(mark)
	assert "says it is active" not in linda.launch.text("nvda.log")[logBefore:]


#: Reads shown top-levels that all say active through handler._probeShown, with a client that answers for
#: them, as no compositor in the rig makes two windows say active at once: Mutter does for a parent and
#: the modal dialog GNOME Shell attaches to it, and headless Mutter attaches none.
GUARDS = """
	import types

	import core
	import eventHandler
	from linuxPlatform.atspi import client as atspiClient
	from linuxPlatform.atspi import handler
	from linuxPlatform.atspi.client import ROOT_PATH, Node

	core.callLater = lambda *args, **kwargs: None
	core.requestPump = lambda: None
	BUS = ":1.42"
	application = Node(BUS, ROOT_PATH, None)
	parents = {}


	class Client:
		def parent(self, node):
			return parents[node.key]

		def states(self, node):
			return frozenset({"active", "showing", "visible"})


	atspiClient._client = Client()


	def node(path, parent):
		made = Node(BUS, path, None)
		parents[made.key] = parent
		return made


	parent = node("/parent", application)
	dialog = node("/dialog", application)
	inDialog = node("/dialog/button", dialog)
	inParent = node("/parent/button", parent)
	other = node("/other", application)
	T = 1000.0


	def begin(*shown, foreground=None, activatedAt=0.0, focus=None):
		handler._shown.clear()
		handler._shownProbes.clear()
		handler._topLevels.clear()
		handler._probePumpAt = None
		handler._activatedAt = activatedAt
		handler._setForeground(foreground)
		eventHandler.lastQueuedFocusObject = focus and types.SimpleNamespace(node=focus)
		handler._shown.extend(shown)


	def found(at, events=()):
		return [event[1].path for event in handler._probeShown(list(events), at)]


	def readUntilDone(start):
		taken = []
		at = start
		while handler._shownProbes:
			taken += found(at)
			at += 0.25
		return taken


	result = {}
	# The parent shows, its attached dialog shows after it, and only the dialog's activation is heard: the
	# dialog keeps the foreground through every read of the parent.
	begin((parent, T), (dialog, T + 0.05))
	taken = found(T + 0.06)
	taken += found(T + 0.08, [("window:activate", dialog, 0, 0, None)])
	handler._setForeground(dialog)
	eventHandler.lastQueuedFocusObject = types.SimpleNamespace(node=inDialog)
	taken += readUntilDone(T + 0.2)
	result["dialogAnnounced"] = {"taken": taken, "foreground": handler._foreground.path}
	# A window activated before the parent showed is no reason to leave the parent alone.
	begin((parent, T), foreground=other, activatedAt=T - 5, focus=other)
	taken = found(T + 0.1)
	result["announcedBefore"] = {"taken": taken, "foreground": handler._foreground.path}
	# Already the foreground, as a rescan found it with no window:activate.
	begin((parent, T), foreground=parent)
	result["foreground"] = {"taken": found(T + 0.1), "left": len(handler._shownProbes)}
	# The focus is already inside it, with no foreground known.
	begin((parent, T), focus=inParent)
	result["focusInside"] = {"taken": found(T + 0.1), "left": len(handler._shownProbes)}
"""


def test_shownWindowsLeaveTheFocusWhereItBelongs(tmp_path: Path) -> None:
	done = harness.runChild(tmp_path, GUARDS, prepare="config", timeout=120)
	done.assertClean()
	assert done.value == {
		"dialogAnnounced": {"taken": [], "foreground": "/dialog"},
		"announcedBefore": {"taken": ["/parent"], "foreground": "/parent"},
		"foreground": {"taken": [], "left": 0},
		"focusInside": {"taken": [], "left": 0},
	}, done.value


#: Package SH's rules for the shells' own surfaces, each driven with a client that answers for the
#: programs, as no compositor in the rig has GNOME Shell's or KWin's surfaces. Each sets handler's
#: _waylandRules itself, so the X and the Wayland rule are both held in every rig session.
SHELL_COMMON = """
	import types

	import core
	import eventHandler
	from linuxPlatform.atspi import client as atspiClient
	from linuxPlatform.atspi import handler
	from linuxPlatform.atspi.client import ROOT_PATH, AppHung, Node

	core.callLater = lambda *args, **kwargs: None
	core.requestPump = lambda *args, **kwargs: None
	clock = [1000.0]
	handler.time = types.SimpleNamespace(monotonic=lambda: clock[0])
	DESKTOP = Node("org.a11y.atspi.Registry", ROOT_PATH, None)
	names = {}
	parents = {}
	states = {}
	children = {}
	queued = []
	eventHandler.queueEvent = lambda name, obj: queued.append((name, obj.node.path))
	handler.eventObject = lambda n: types.SimpleNamespace(
		node=n, role=None, name="", states=set(), value=None, description=None, keyboardShortcut=None,
		positionInfo={}, atspiRole="frame",
	)


	def node(bus, path, parent, *nodeStates):
		made = Node(bus, path, None)
		parents[made.key] = parent
		states[made.key] = set(nodeStates)
		if parent is not None:
			children.setdefault(parent.key, []).append(made)
		return made


	def app(bus, name):
		made = Node(bus, ROOT_PATH, None)
		names[made.key] = name
		parents[made.key] = DESKTOP
		return made
"""

#: A rescan while GNOME Shell's stage is active and a program's window is the foreground, not active.
SWITCHER_RESCAN = (
	SHELL_COMMON
	+ """
	shell = app(":1.10", "gnome-shell")
	stage = node(":1.10", "/stage", shell, "active")
	editor = app(":1.20", "pluma")
	left = node(":1.20", "/left", editor)


	class Client:
		def applications(self):
			return [shell, editor]

		def desktop(self):
			return DESKTOP

		def name(self, n):
			return names.get(n.key, "")

		def children(self, n):
			return list(children.get(n.key, []))

		def states(self, n):
			return frozenset(states.get(n.key, ()))

		def parent(self, n):
			return parents.get(n.key)

		def isHung(self, busName):
			return False


	atspiClient._client = Client()
	processed = []
	handler._processFocus = lambda event, mayRetry=True: processed.append((event[0], event[1].path)) or True
	handler._waylandRules = WAYLAND
	handler._setForeground(left, active=False)
	handler._rescans[:] = [clock[0] - 0.01]
	handler.pumpAll()
	result = {
		"processed": processed,
		"rescansLeft": len(handler._rescans),
		"foreground": handler._foreground.path if handler._foreground is not None else None,
	}
"""
)


@pytest.mark.parametrize("session", ["x11", "wayland"])
def test_switcherRescanTakesTheShellOnlyInX(tmp_path: Path, session: str) -> None:
	"""In an X session GNOME Shell's active stage is the foreground a rescan finds, as on main; in a
	Wayland session the stage is a switcher, looked past until a window takes over."""
	code = SWITCHER_RESCAN.replace("WAYLAND", str(session == "wayland"))
	done = harness.runChild(tmp_path, code, prepare="config", timeout=120)
	done.assertClean()
	expected = {
		"x11": {"processed": [["window:activate", "/stage"]], "rescansLeft": 0, "foreground": "/stage"},
		"wayland": {"processed": [], "rescansLeft": 2, "foreground": "/left"},
	}
	assert done.value == expected[session], done.value


#: GNOME Shell's switcher choice, queued while the shell answers nothing; from ANSWERS_FROM on it
#: answers. Each pump is half a second on the stand-in clock.
HELD_SWITCHER = (
	SHELL_COMMON
	+ """
	shell = app(":1.10", "gnome-shell")
	stage = node(":1.10", "/stage", shell, "active")
	item = node(":1.10", "/item", stage, "selected")
	hungSince = [None]


	class Client:
		def applications(self):
			return [shell]

		def desktop(self):
			return DESKTOP

		def name(self, n):
			return names.get(n.key, "")

		def children(self, n):
			return list(children.get(n.key, []))

		def states(self, n):
			return frozenset(states.get(n.key, ()))

		def parent(self, n):
			return parents.get(n.key)

		def role(self, n):
			if clock[0] >= ANSWERS_FROM:
				return "list item"
			# As client.call does for an application that keeps timing out: refused at once while marked,
			# and the probe after PROBE_AFTER_SECONDS times out again and marks it anew.
			if hungSince[0] is None or clock[0] - hungSince[0] >= 2.0:
				hungSince[0] = clock[0]
			raise AppHung(n.busName)

		def isHung(self, busName):
			if clock[0] >= ANSWERS_FROM:
				return False
			return hungSince[0] is not None and clock[0] - hungSince[0] < 2.0


	atspiClient._client = Client()
	accepted = []
	handler._shouldAccept = lambda nvdaEvent, n: accepted.append((nvdaEvent, n.path)) and False
	handler._waylandRules = True
	handler._setForeground(stage)
	with handler._queueLock:
		handler._queue.append(("object:state-changed:selected", item, 1, 0, None))
	held = []
	for _step in range(60):
		handler.pumpAll()
		held.append(sum(len(events) for _since, events in handler._switcherEventsWhileHung.values()))
		clock[0] += 0.5
	result = {"heldAtFirst": held[0], "heldAfter11s": held[22], "heldAtEnd": held[-1], "accepted": accepted}
"""
)


@pytest.mark.parametrize("answers", ["never", "within"])
def test_heldSwitcherChoice(tmp_path: Path, answers: str) -> None:
	"""A switcher choice that comes while GNOME Shell is marked hung is held, read once the shell
	answers within handler.HUNG_FOCUS_SECONDS, and given up after it."""
	code = HELD_SWITCHER.replace("ANSWERS_FROM", "1005.0" if answers == "within" else "float('inf')")
	done = harness.runChild(tmp_path, code, prepare="config", timeout=120)
	done.assertClean()
	assert done.value["heldAtFirst"] == 1, done.value
	assert done.value["heldAtEnd"] == 0, done.value
	if answers == "within":
		assert done.value["accepted"] == [["stateChange", "/item"]], done.value
	else:
		assert done.value["heldAfter11s"] == 0, done.value
		assert done.value["accepted"] == [], done.value


#: A renaming from GNOME Shell and one from another program, each while its program is hung, with
#: the label's role already known.
NAME_CHANGE_HUNG = (
	SHELL_COMMON
	+ """
	shell = app(":1.10", "gnome-shell")
	shellLabel = node(":1.10", "/label", shell)
	editor = app(":1.20", "kate")
	editorLabel = node(":1.20", "/label", editor)


	class Client:
		def applications(self):
			return [shell, editor]

		def name(self, n):
			return names.get(n.key, "")

		def role(self, n):
			raise AppHung(n.busName)


	atspiClient._client = Client()
	handler._waylandRules = True
	handler._roles[shellLabel.key] = "label"
	handler._roles[editorLabel.key] = "label"
	result = {}
	for which, n in (("shell", shellLabel), ("editor", editorLabel)):
		try:
			result[which] = handler._nvdaEvents(("object:property-change:accessible-name", n, 0, 0, None))
		except AppHung:
			result[which] = "AppHung"
"""
)


def test_hungProgramsRenamingWaitsOnlyForTheShell(tmp_path: Path) -> None:
	"""In a Wayland session GNOME Shell's renaming waits for the shell to answer, so that it is held
	with its switcher choice; another hung program's goes by the role already known."""
	done = harness.runChild(tmp_path, NAME_CHANGE_HUNG, prepare="config", timeout=120)
	done.assertClean()
	assert done.value == {"shell": "AppHung", "editor": ["nameChange"]}, done.value


#: A Qt program's frame, which finds no focus in itself, and its field, with a client that answers for
#: them.
FRAME_AND_FIELD = """
	import types

	import core
	import eventHandler
	from linuxPlatform.atspi import client as atspiClient
	from linuxPlatform.atspi import handler
	from linuxPlatform.atspi.client import ROOT_PATH, Node

	# After eventHandler, which settles the import cycle api's own imports enter.
	import api

	core.callLater = lambda *args, **kwargs: None
	core.requestPump = lambda *args, **kwargs: None
	BUS = ":1.42"
	application = Node(BUS, ROOT_PATH, None)
	parents = {}
	states = {}


	class Client:
		def parent(self, node):
			return parents[node.key]

		def states(self, node):
			return frozenset(states.get(node.key, ()))

		def toolkit(self, node):
			return "Qt"

		def matchesWithState(self, node, state, count):
			return []

		def role(self, node):
			return "frame"

		def isHung(self, busName):
			return False


	atspiClient._client = Client()


	def node(path, parent, *nodeStates):
		made = Node(BUS, path, None)
		parents[made.key] = parent
		states[made.key] = set(nodeStates)
		return made


	frame = node("/frame", application, "active")
	field = node("/frame/field", frame, "focused")
	queued = []
	eventHandler.queueEvent = lambda name, obj: queued.append((name, obj.node.path))
	handler.eventObject = lambda n: types.SimpleNamespace(
		node=n, role=None, name="", states=set(), value=None, description=None, keyboardShortcut=None,
		positionInfo={}, atspiRole="frame",
	)
	other = Node(":1.7", "/other", None)
"""

#: A window:activate of the frame that holds NVDA's focus, with no focus found in it, while the field
#: is or is not still focused; then a focus and the frame's activation deferred while the program is
#: hung, and whether a later event of it is accepted.
KEEPS_FOCUS = (
	FRAME_AND_FIELD
	+ """
	result = {}
	for session, wayland in (("x11", False), ("wayland", True)):
		handler._waylandRules = wayland
		handler._setForeground(frame)
		for fieldFocused in (True, False):
			states[field.key] = {"focused"} if fieldFocused else set()
			queued.clear()
			eventHandler.lastQueuedFocusObject = types.SimpleNamespace(node=field)
			handler._processFocus(("window:activate", frame, 0, 0, None))
			result[f"{session} field focused={fieldFocused}"] = list(queued)
		handler._focusWhileHung.clear()
		handler._activationWhileHung.clear()
		handler._deferWhileHung(("object:state-changed:focused", field, 1, 0, None))
		handler._deferWhileHung(("window:activate", frame, 0, 0, None))
		api.getFocusObject = lambda: types.SimpleNamespace(node=other)
		api.getForegroundObject = lambda: types.SimpleNamespace(node=other)
		handler._backgroundEventsOf = lambda n: frozenset()
		handler._setForeground(frame)
		result[f"{session} accepted"] = handler._shouldAccept("stateChange", field)
		activation = handler._activationWhileHung.get(BUS)
		result[f"{session} waiting"] = [
			handler._focusWhileHung[BUS][0][1].path,
			activation[1].path if activation else None,
		]
"""
)

#: One batch through handler.pumpAll: the field's focus event, then the activation of its frame, while
#: NVDA's focus is in another program.
FOCUS_FIRST = (
	FRAME_AND_FIELD
	+ """
	handler._waylandRules = WAYLAND
	handler._setForeground(other)
	eventHandler.lastQueuedFocusObject = types.SimpleNamespace(node=other)
	with handler._queueLock:
		handler._queue.append(("object:state-changed:focused", field, 1, 0, None))
		handler._queue.append(("window:activate", frame, 0, 0, None))
	handler.pumpAll()
	result = queued
"""
)


def test_activationKeepsAFocusStillInsideIt(tmp_path: Path) -> None:
	"""In a Wayland session a window:activate whose window gives no focus leaves NVDA's focus inside
	it while that is still focused, a deferred focus is not replaced by the program's activation, and
	events of the window activated last are accepted; in an X session each is as on main."""
	done = harness.runChild(tmp_path, KEEPS_FOCUS, prepare="config", timeout=120)
	done.assertClean()
	assert done.value == {
		"x11 field focused=True": [["gainFocus", "/frame"]],
		"x11 field focused=False": [["gainFocus", "/frame"]],
		"wayland field focused=True": [],
		"wayland field focused=False": [["gainFocus", "/frame"]],
		"x11 waiting": ["/frame", None],
		"wayland waiting": ["/frame/field", "/frame"],
		"x11 accepted": False,
		"wayland accepted": True,
	}, done.value


@pytest.mark.parametrize("session", ["x11", "wayland"])
def test_focusTriedBeforeItsWindowsActivation(tmp_path: Path, session: str) -> None:
	"""In a Wayland session a program's focus event is tried before its window's activation later in
	the same batch, as a Qt Quick window cannot say what in it has the focus; in an X session the
	activation, newest, is tried first, as on main."""
	code = FOCUS_FIRST.replace("WAYLAND", str(session == "wayland"))
	done = harness.runChild(tmp_path, code, prepare="config", timeout=120)
	done.assertClean()
	expected = {"x11": [["gainFocus", "/frame"]], "wayland": [["gainFocus", "/frame/field"]]}
	assert done.value == expected[session], done.value


#: A notification of GNOME Shell's showing, as package SH saw one come from notify-send: its showing event
#: first, then those of some twenty panels and labels in it and around it, in one batch.
SHELL_NOTIFICATION = (
	SHELL_COMMON
	+ """
	shell = app(":1.10", "gnome-shell")
	stage = node(":1.10", "/stage", shell, "active")
	notification = node(":1.10", "/notification", stage)
	roles = {notification.key: "notification"}
	parts = [node(":1.10", f"/part{index}", notification) for index in range(20)]
	for index, part in enumerate(parts):
		roles[part.key] = "label" if index % 2 else "panel"


	class Client:
		def applications(self):
			return [shell]

		def desktop(self):
			return DESKTOP

		def name(self, n):
			return names.get(n.key, "")

		def role(self, n):
			return roles[n.key]

		def toolkit(self, n):
			return "GAIL"

		def isHung(self, busName):
			return False


	atspiClient._client = Client()
	handler._waylandRules = WAYLAND
	with handler._queueLock:
		for shown in (notification, *parts):
			handler._queue.append(("object:state-changed:showing", shown, 1, 0, None))
	handler.pumpAll()
	result = queued
"""
)


@pytest.mark.parametrize("session", ["x11", "wayland"])
def test_shellNotificationIsAlerted(tmp_path: Path, session: str) -> None:
	"""In a Wayland session a notification GNOME Shell shows is alerted, though more than
	handler.MAX_EVENTS_PER_APPLICATION of the shell's events come after it; in an X session it is left
	out with the rest past the limit, as on main. The GNOME guest case wlGnomeShell is where it is heard."""
	code = SHELL_NOTIFICATION.replace("WAYLAND", str(session == "wayland"))
	done = harness.runChild(tmp_path, code, prepare="config", timeout=120)
	done.assertClean()
	expected = {"x11": [], "wayland": [["alert", "/notification"]]}
	assert done.value == expected[session], done.value
