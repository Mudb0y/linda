"""What NVDA says about the controls of GTK 3, GTK 4, Qt 6 and wx applications, read through AT-SPI.

Keys reach the application through xdotool. NVDA's own commands run through a plugin that takes
gesture names from a FIFO and runs them as if NVDA had captured the keys, so these tests need no
keyboard capture.

nix develop . -c tests/linux/rig python3 -m pytest tests/linux/test_atspiObjects.py
"""

import errno
import os
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import harness
import pytest
from desktopSession import DesktopSession

#: Seconds from starting NVDA to its first words about the application.
STARTUP_TIMEOUT = 60.0
#: Seconds with nothing new spoken after which a step counts as finished.
QUIET = 0.6
#: What the watchdog logs when the main thread was stuck.
FREEZE_RECORD = "Recovered from freeze"

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
[featureFlag]
	playErrorSound = 2
[addonStore]
	automaticUpdates = disabled
	baseServerURL = http://127.0.0.1:1
"""

DRIVER_PLUGIN = '''\
"""Runs NVDA commands that a test writes to the FIFO BASEDREADER_OBJECTS_FIFO names, one per line, on
NVDA's main thread: "gesture NAME" runs a key gesture through inputCore as if NVDA had captured the
keys, "script NAME" runs globalCommands' script_NAME, and "exit" asks NVDA to exit."""

import os
import threading

import core
import globalCommands
import globalPluginHandler
import inputCore
import keyboardHandler
import queueHandler
import scriptHandler
from logHandler import log


class GlobalPlugin(globalPluginHandler.GlobalPlugin):
	def __init__(self):
		super().__init__()
		self._fifo = os.environ.get("BASEDREADER_OBJECTS_FIFO")
		if self._fifo:
			threading.Thread(target=self._read, name="objectsDriver", daemon=True).start()

	def _read(self):
		with open(self._fifo, encoding="utf-8") as commands:
			for line in commands:
				command, _, argument = line.strip().partition(" ")
				queueHandler.queueFunction(queueHandler.eventQueue, self._run, command, argument)

	def _run(self, command, argument):
		log.info(f"objects driver: {command} {argument}")
		if command == "gesture":
			gesture = keyboardHandler.KeyboardInputGesture.fromName(argument)
			try:
				inputCore.manager.executeGesture(gesture)
			except inputCore.NoInputGestureAction:
				log.info(f"objects driver: no script for {argument}")
		elif command == "script":
			gesture = keyboardHandler.KeyboardInputGesture.fromName("NVDA+tab")
			scriptHandler.executeScript(getattr(globalCommands.commands, f"script_{argument}"), gesture)
		elif command == "exit":
			core.triggerNVDAExit()
'''


class Reader:
	"""NVDA beside the applications of a desktop, given commands and listened to."""

	def __init__(self, desktop: DesktopSession, directory: Path) -> None:
		self.desktop = desktop
		hooks = directory / "hooks"
		(hooks / "globalPlugins").mkdir(parents=True)
		(hooks / "synthDrivers").symlink_to(harness.HERE / "synthDrivers")
		(hooks / "globalPlugins" / "objectsDriver.py").write_text(DRIVER_PLUGIN)
		self.fifo = directory / "commands.fifo"
		os.mkfifo(self.fifo)
		config = directory / "nvda.ini"
		config.write_text(CONFIG)
		self.launch = desktop.startNVDA(
			config,
			env={
				"BASEDREADER_TEST_HOOKS_DIR": str(hooks),
				"BASEDREADER_OBJECTS_FIFO": str(self.fifo),
			},
		)
		self._commands: int | None = None
		self.differences: list[str] = []

	def _connect(self) -> int:
		deadline = time.monotonic() + STARTUP_TIMEOUT
		while True:
			try:
				return os.open(self.fifo, os.O_WRONLY | os.O_NONBLOCK)
			except OSError as e:
				# Nobody reads the FIFO until NVDA has loaded the plugin.
				if (
					e.errno != errno.ENXIO
					or time.monotonic() >= deadline
					or self.launch.process.poll() is not None
				):
					raise AssertionError(f"NVDA never read its commands\n{self.launch.describe()}") from None
			time.sleep(0.05)

	def command(self, line: str) -> None:
		if self._commands is None:
			self._commands = self._connect()
		os.write(self._commands, f"{line}\n".encode())

	def gesture(self, name: str) -> None:
		self.command(f"gesture {name}")

	def step(self, action, *expected: str, timeout: float = 5) -> list[str]:
		"""Run action, and require that NVDA then says exactly expected, record for record.

		A step that differs is noted and the run goes on, so that one run shows every difference;
		exitCleanly fails the test on any.
		"""
		mark = self.desktop.mark()
		action()
		if expected:
			try:
				self.desktop.expect(*expected, since=mark, timeout=timeout)
			except AssertionError:
				pass
		spoken = self.settle(mark)
		if spoken != list(expected):
			self.differences.append(f"expected {list(expected)}, NVDA said {spoken}")
		return spoken

	def settle(self, mark: int) -> list[str]:
		"""What NVDA said after mark, once it has said nothing new for QUIET seconds."""
		spoken = self.desktop.spokenSince(mark)
		quietSince = time.monotonic()
		while time.monotonic() - quietSince < QUIET:
			time.sleep(0.05)
			now = self.desktop.spokenSince(mark)
			if now != spoken:
				spoken = now
				quietSince = time.monotonic()
		return spoken

	def exitCleanly(self) -> None:
		self.command("exit")
		assert self.launch.waitForExit(20) == 0, self.launch.describe()
		os.close(self._commands)
		self._commands = None
		log = self.launch.text("nvda.log")
		assert FREEZE_RECORD not in log, log[-8000:]
		self.launch.assertClean(logNames=["nvda.log"])
		assert not self.differences, "\n".join(self.differences)


@pytest.fixture
def desktop(tmp_path: Path) -> Iterator[DesktopSession]:
	with DesktopSession(tmp_path / "desktop") as session:
		yield session


def _press(desktop: DesktopSession, *keys: str):
	return lambda: desktop.press(*keys)


def _focusedWindowName() -> str:
	return subprocess.run(
		["xdotool", "getwindowfocus", "getwindowname"],
		capture_output=True,
		text=True,
		check=False,
	).stdout.strip()


def _giveKeyboard(title: str) -> None:
	"""Make sure the window named title has the keyboard: Qt and wx windows that marco mapped were
	sometimes active without the input focus, and every key went nowhere. GTK takes the focus
	itself."""
	deadline = time.monotonic() + 5
	while _focusedWindowName() != title:
		assert time.monotonic() < deadline, (
			f"{title!r} never got the keyboard; {_focusedWindowName()!r} has it"
		)
		subprocess.run(["wmctrl", "-a", title], check=False)
		time.sleep(0.2)
		if _focusedWindowName() != title:
			# A marco started after another test's had quit left the focus on the root window.
			subprocess.run(["xdotool", "search", "--name", f"^{title}$", "windowfocus"], check=False)
			time.sleep(0.2)


def _startApp(desktop: DesktopSession, fixture: str, title: str) -> subprocess.Popen:
	app = desktop.startApp(desktop.fixture(fixture), title=title)
	_giveKeyboard(title)
	return app


def test_gtkControlsAreSpokenAsOnWindows(desktop: DesktopSession, tmp_path: Path):
	_startApp(desktop, "gtkFixture", "Fixture window")
	nvda = Reader(desktop, tmp_path)
	desktop.expect(
		"Fixture window", "Press me button Does nothing Alt plus P", since=0, timeout=STARTUP_TIMEOUT
	)
	nvda.settle(0)
	tab = _press(desktop, "Tab")
	nvda.step(tab, "Enable sound check box not checked Alt plus E")
	nvda.step(tab, "Size grouping", "Medium radio button checked 2 of 3")
	nvda.step(tab, "Colour: combo box Green collapsed Alt plus C")
	nvda.step(tab, "Name: edit Alt plus N selected Alice")
	nvda.step(tab, "Volume: slider 40")
	nvda.step(tab, "Count: spin button 3 Alt plus O")
	nvda.step(tab, "Fruit: list", "Apples 1 of 3")
	nvda.step(tab, "Bold toggle button not pressed Alt plus B")
	nvda.step(tab, "Example link link")
	nvda.step(tab, "tab control", "General tab selected 1 of 2")
	nvda.step(tab, "Press me button Does nothing Alt plus P")
	nvda.step(tab, "Enable sound check box not checked Alt plus E")
	space = _press(desktop, "space")
	nvda.step(space, "checked")
	nvda.step(space, "not checked")
	nvda.step(tab, "Size grouping", "Medium radio button checked 2 of 3")
	nvda.step(_press(desktop, "Down"), "Large radio button checked 3 of 3")
	nvda.step(lambda: nvda.gesture("NVDA+tab"), "Large radio button focused checked 3 of 3")
	nvda.step(lambda: nvda.gesture("NVDA+t"), "Fixture window")
	nvda.step(lambda: nvda.gesture("NVDA+end"), "Ready")
	nvda.step(lambda: nvda.gesture("NVDA+numpad5"), "Large radio button focused checked 3 of 3")
	parent = lambda: nvda.gesture("NVDA+numpad8")
	nvda.step(parent, "Size grouping")
	nvda.step(parent, "Fixture window")
	nvda.step(parent, "Desktop desktop pane")
	nvda.step(
		lambda: nvda.command("script navigatorObject_toFocus"),
		"Move to focus",
		"Large radio button checked 3 of 3",
	)
	nvda.step(parent, "Size grouping")
	nvda.step(parent, "Fixture window")
	nvda.step(lambda: nvda.gesture("NVDA+numpad2"), "menu bar")
	nvda.step(lambda: nvda.gesture("NVDA+numpad6"), "Press me button Does nothing Alt plus P")
	nvda.step(lambda: nvda.gesture("NVDA+numpad6"), "Enable sound check box not checked Alt plus E")
	nvda.step(lambda: nvda.gesture("NVDA+numpad4"), "Press me button Does nothing Alt plus P")
	nvda.step(tab, "Colour: combo box Green collapsed Alt plus C")
	nvda.step(_press(desktop, "Down"), "Blue")
	nvda.step(tab, "Name: edit Alt plus N selected Alice")
	nvda.step(_press(desktop, "shift+Tab"), "Colour: combo box Blue collapsed Alt plus C")
	nvda.step(_press(desktop, "Tab", "Tab"), "Name: edit Alt plus N selected Alice", "Volume: slider 40")
	nvda.step(_press(desktop, "Right"), "41")
	nvda.step(tab, "Count: spin button 3 Alt plus O")
	nvda.step(_press(desktop, "Up"), "4")
	nvda.step(tab, "Fruit: list", "Apples 1 of 3")
	nvda.step(_press(desktop, "Down"), "Pears 2 of 3")
	nvda.step(tab, "Bold toggle button not pressed Alt plus B")
	nvda.step(space, "pressed")
	nvda.step(
		_press(desktop, "Tab", "Tab"), "Example link link", "tab control", "General tab selected 1 of 2"
	)
	nvda.step(_press(desktop, "Right"), "Advanced tab selected 2 of 2")
	nvda.step(_press(desktop, "F10"), "File sub Menu Alt plus F 1 of 2")
	nvda.step(_press(desktop, "Down"), "Open Ctrl plus O O 1 of 4")
	nvda.step(_press(desktop, "Down"), "Recent sub Menu R 2 of 4")
	nvda.step(_press(desktop, "Right"), "one dot txt 1 of 1")
	nvda.step(_press(desktop, "Left"), "Recent sub Menu R 2 of 4")
	nvda.step(_press(desktop, "Down"), "Wrap lines not checked W 3 of 4")
	nvda.step(_press(desktop, "Escape", "Escape"), "tab control", "Advanced tab selected 2 of 2")
	nvda.exitCleanly()


def _startQt(desktop: DesktopSession, tmp_path: Path) -> "Reader":
	"""NVDA, listening, and then the Qt fixture: a Qt program started before NVDA registered for
	events sent it none in two runs of four."""
	nvda = Reader(desktop, tmp_path)
	nvda.command("ready")
	_startApp(desktop, "qtFixture", "Qt fixture window")
	return nvda


def test_qtControlsAreSpokenAsOnWindows(desktop: DesktopSession, tmp_path: Path):
	nvda = _startQt(desktop, tmp_path)
	desktop.expect(
		"Qt fixture window",
		"Press me button Does nothing Alt plus P",
		since=0,
		timeout=STARTUP_TIMEOUT,
	)
	nvda.settle(0)
	tab = _press(desktop, "Tab")
	nvda.step(tab, "Enable sound check box not checked Alt plus E")
	# Qt puts the focus on a group's first radio button, not on its checked one.
	nvda.step(tab, "Size grouping", "Small radio button not checked 1 of 3")
	# Qt binds Down to a combo box, which opens it and is no way to reach it.
	nvda.step(tab, "Colour: combo box Green collapsed")
	nvda.step(tab, "Name: edit Alt plus N selected Alice")
	nvda.step(tab, "Volume: slider 40 Alt plus V")
	nvda.step(tab, "Count: spin button 3 Alt plus O")
	nvda.step(tab, "Fruit: list Alt plus U", "Apples 1 of 3")
	nvda.step(tab, "tab control", "General tab 1 of 2")
	nvda.step(_press(desktop, "Right"), "Advanced tab 2 of 2")
	nvda.step(_press(desktop, "shift+Tab"), "Fruit: list Alt plus U", "Apples 1 of 3")
	nvda.step(_press(desktop, "Down"), "Pears 2 of 3")
	nvda.step(lambda: nvda.gesture("NVDA+tab"), "Pears list item focused selected 2 of 3")
	nvda.step(lambda: nvda.gesture("NVDA+t"), "Qt fixture window")
	nvda.step(lambda: nvda.gesture("NVDA+end"), "Ready")
	nvda.exitCleanly()


@pytest.mark.parametrize(
	("fixture", "title", "tabs"),
	[("gtkFixture", "Fixture window", 7), ("qtFixture", "Qt fixture window", 7)],
)
def test_longListsDoNotFreeze(
	desktop: DesktopSession,
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
	fixture: str,
	title: str,
	tabs: int,
):
	monkeypatch.setenv("BASEDREADER_LISTSIZE", "2000")
	if fixture == "qtFixture":
		nvda = _startQt(desktop, tmp_path)
	else:
		_startApp(desktop, fixture, title)
		nvda = Reader(desktop, tmp_path)
	desktop.expect(title, since=0, timeout=STARTUP_TIMEOUT)
	nvda.settle(0)
	mark = desktop.mark()
	desktop.press(*["Tab"] * tabs)
	desktop.expect("Apples 1 of 2003", since=mark, timeout=10)
	nvda.settle(mark)
	nvda.step(_press(desktop, "End"), "Item 1999 2003 of 2003")
	nvda.step(_press(desktop, "Home"), "Apples 1 of 2003")
	nvda.exitCleanly()


def test_gtkMessageDialogIsSpokenAsADialog(desktop: DesktopSession, tmp_path: Path):
	app = _startApp(desktop, "twoWindows", "First window")
	nvda = Reader(desktop, tmp_path)
	desktop.expect("First window", since=0, timeout=STARTUP_TIMEOUT)
	nvda.settle(0)
	mark = desktop.mark()
	app.stdin.write("dialog\n")
	app.stdin.flush()
	desktop.expect("Question dialog Save changes? Your edits will be lost otherwise.", since=mark)
	spoken = nvda.settle(mark)
	assert spoken.count("Question dialog Save changes? Your edits will be lost otherwise.") == 1, spoken
	nvda.step(
		lambda: nvda.gesture("NVDA+b"),
		"Question dialog Save changes? Your edits will be lost otherwise.",
		"Save changes?",
		"Your edits will be lost otherwise.",
		"Cancel button Alt plus C",
		"OK button Alt plus O",
	)
	app.stdin.write("close\n")
	app.stdin.flush()
	nvda.exitCleanly()


def test_wxDialogAndStatusBar(desktop: DesktopSession, tmp_path: Path):
	app = _startApp(desktop, "wxfixture", "wx fixture")
	nvda = Reader(desktop, tmp_path)
	desktop.expect("wx fixture", since=0, timeout=STARTUP_TIMEOUT)
	nvda.settle(0)
	nvda.step(lambda: nvda.gesture("NVDA+end"), "Ready")
	mark = desktop.mark()
	app.stdin.write("focus dialogButton\n")
	app.stdin.flush()
	desktop.expect("Open dialog... button", since=mark)
	nvda.settle(mark)
	mark = desktop.mark()
	desktop.press("space")
	_giveKeyboard("Settings")
	desktop.expect("Settings dialog", since=mark)
	nvda.settle(mark)
	nvda.step(lambda: nvda.gesture("NVDA+t"), "Settings")
	nvda.exitCleanly()


def test_gtk4StatusBarAndListItems(desktop: DesktopSession, tmp_path: Path):
	app = _startApp(desktop, "gtk4", "GTK4 fixture")
	nvda = Reader(desktop, tmp_path)
	# Once NVDA reads its commands it is listening; GTK 4 has no Collection interface, through which
	# NVDA finds the focus it starts with, so the test moves the focus to be heard.
	nvda.command("ready")
	app.stdin.write("focus list\n")
	app.stdin.flush()
	desktop.expect("GTK 4 fixture", "Items: list Alt plus I", since=0, timeout=STARTUP_TIMEOUT)
	nvda.settle(0)
	# GTK 4 leaves its list items unnamed, with the text in a label inside.
	nvda.step(_press(desktop, "Down"), "Item 1 1 of 5")
	nvda.step(_press(desktop, "Down"), "Item 2 2 of 5")
	nvda.step(lambda: nvda.gesture("NVDA+end"), "Ready")
	nvda.exitCleanly()
