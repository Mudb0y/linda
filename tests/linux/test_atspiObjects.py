"""What NVDA says about the controls of GTK 3, GTK 4, Qt 6 and wx applications, read through AT-SPI.

Keys reach the application through xdotool. NVDA's own commands run through a plugin that takes
gesture names from a FIFO and runs them as if NVDA had captured the keys, so these tests need no
keyboard capture.

nix develop . -c tests/linux/rig python3 -m pytest tests/linux/test_atspiObjects.py
"""

import errno
import os
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

	def _connect(self) -> int:
		deadline = time.monotonic() + STARTUP_TIMEOUT
		while True:
			try:
				return os.open(self.fifo, os.O_WRONLY | os.O_NONBLOCK)
			except OSError as e:
				# Nobody reads the FIFO until NVDA has loaded the plugin.
				if e.errno != errno.ENXIO or time.monotonic() >= deadline or self.launch.process.poll() is not None:
					raise AssertionError(f"NVDA never read its commands\n{self.launch.describe()}") from None
			time.sleep(0.05)

	def command(self, line: str) -> None:
		if self._commands is None:
			self._commands = self._connect()
		os.write(self._commands, f"{line}\n".encode())

	def gesture(self, name: str) -> None:
		self.command(f"gesture {name}")

	def step(self, action, *expected: str, timeout: float = 5) -> list[str]:
		"""Run action, and require that NVDA then says exactly expected, record for record."""
		mark = self.desktop.mark()
		action()
		if expected:
			self.desktop.expect(*expected, since=mark, timeout=timeout)
		spoken = self.settle(mark)
		assert spoken == list(expected), f"expected {list(expected)}, NVDA said {spoken}"
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


@pytest.fixture
def desktop(tmp_path: Path) -> Iterator[DesktopSession]:
	with DesktopSession(tmp_path / "desktop") as session:
		yield session


def _press(desktop: DesktopSession, *keys: str):
	return lambda: desktop.press(*keys)


def test_gtkControlsAreSpokenAsOnWindows(desktop: DesktopSession, tmp_path: Path):
	desktop.startApp(desktop.fixture("gtkFixture"), title="Fixture window")
	nvda = Reader(desktop, tmp_path)
	desktop.expect("Fixture window", "Press me button Does nothing Alt+P", since=0, timeout=STARTUP_TIMEOUT)
	nvda.settle(0)
	tab = _press(desktop, "Tab")
	nvda.step(tab, "Enable sound check box not checked Alt+E")
	nvda.step(tab, "Size grouping", "Medium radio button checked 2 of 3")
	nvda.step(tab, "Colour: combo box Green collapsed Alt+C")
	nvda.step(tab, "Name: edit Alt+N selected Alice")
	nvda.step(tab, "Volume: slider 40")
	nvda.step(tab, "Count: spin button 3 Alt+O")
	nvda.step(tab, "Fruit: list", "Apples 1 of 3")
	nvda.step(tab, "Bold toggle button not pressed Alt+B")
	nvda.step(tab, "Example link link")
	nvda.step(tab, "tab control", "General tab selected 1 of 2")
	nvda.step(tab, "Press me button Does nothing Alt+P")
	nvda.step(tab, "Enable sound check box not checked Alt+E")
	space = _press(desktop, "space")
	nvda.step(space, "checked")
	nvda.step(space, "not checked")
	nvda.step(tab, "Size grouping", "Medium radio button checked 2 of 3")
	nvda.step(_press(desktop, "Down"), "Large radio button checked 3 of 3")
	nvda.step(lambda: nvda.gesture("NVDA+tab"), "Large radio button focused checked 3 of 3")
	nvda.step(lambda: nvda.gesture("NVDA+t"), "Fixture window")
	nvda.step(lambda: nvda.gesture("NVDA+end"), "Ready")
	nvda.step(lambda: nvda.gesture("NVDA+numpad5"), "Large radio button focused checked 3 of 3")
	parent = lambda: nvda.gesture("NVDA+numpad8")  # noqa: E731
	nvda.step(parent, "Size grouping")
	nvda.step(parent, "Fixture window")
	nvda.step(parent, "Desktop desktop pane")
	nvda.step(lambda: nvda.command("script navigatorObject_toFocus"), "Move to focus", "Large radio button checked 3 of 3")
	nvda.step(parent, "Size grouping")
	nvda.step(parent, "Fixture window")
	nvda.step(lambda: nvda.gesture("NVDA+numpad2"), "menu bar")
	nvda.step(lambda: nvda.gesture("NVDA+numpad6"), "Press me button Does nothing Alt+P")
	nvda.step(lambda: nvda.gesture("NVDA+numpad6"), "Enable sound check box not checked Alt+E")
	nvda.step(lambda: nvda.gesture("NVDA+numpad4"), "Press me button Does nothing Alt+P")
	nvda.step(tab, "Colour: combo box Green collapsed Alt+C")
	nvda.step(_press(desktop, "Down"), "Blue")
	nvda.step(tab, "Name: edit Alt+N selected Alice")
	nvda.step(_press(desktop, "shift+Tab"), "Colour: combo box Blue collapsed Alt+C")
	nvda.step(_press(desktop, "Tab", "Tab"), "Name: edit Alt+N selected Alice", "Volume: slider 40")
	nvda.step(_press(desktop, "Right"), "41")
	nvda.step(tab, "Count: spin button 3 Alt+O")
	nvda.step(_press(desktop, "Up"), "4")
	nvda.step(tab, "Fruit: list", "Apples 1 of 3")
	nvda.step(_press(desktop, "Down"), "Pears 2 of 3")
	nvda.step(tab, "Bold toggle button not pressed Alt+B")
	nvda.step(space, "pressed")
	nvda.step(_press(desktop, "Tab", "Tab"), "Example link link", "tab control", "General tab selected 1 of 2")
	nvda.step(_press(desktop, "Right"), "Advanced tab selected 2 of 2")
	nvda.step(_press(desktop, "F10"), "File subMenu Alt+F 1 of 2")
	nvda.step(_press(desktop, "Down"), "Open Ctrl+O O 1 of 4")
	nvda.step(_press(desktop, "Down"), "Recent subMenu R 2 of 4")
	nvda.step(_press(desktop, "Right"), "one.txt 1 of 1")
	nvda.step(_press(desktop, "Left"), "Recent subMenu R 2 of 4")
	nvda.step(_press(desktop, "Down"), "Wrap lines not checked W 3 of 4")
	nvda.step(_press(desktop, "Escape", "Escape"), "tab control", "Advanced tab selected 2 of 2")
	nvda.exitCleanly()


def test_qtControlsAreSpokenAsOnWindows(desktop: DesktopSession, tmp_path: Path):
	desktop.startApp(desktop.fixture("qtFixture"), title="Qt fixture window")
	nvda = Reader(desktop, tmp_path)
	desktop.expect(
		"Qt fixture window",
		"Press me button Does nothing Alt+P",
		since=0,
		timeout=STARTUP_TIMEOUT,
	)
	nvda.settle(0)
	tab = _press(desktop, "Tab")
	nvda.step(tab, "Enable sound check box not checked Alt+E")
	# Qt puts the focus on a group's first radio button, not on its checked one.
	nvda.step(tab, "Size grouping", "Small radio button not checked 1 of 3")
	# Qt binds Down to a combo box, which opens it and is no way to reach it.
	nvda.step(tab, "Colour: combo box Green collapsed")
	nvda.step(tab, "Name: edit Alt+N selected Alice")
	nvda.step(tab, "Volume: slider 40 Alt+V")
	nvda.step(tab, "Count: spin button 3 Alt+O")
	nvda.step(tab, "Fruit: list Alt+U", "Apples 1 of 3")
	nvda.step(tab, "tab control", "General tab selected 1 of 2")
	nvda.step(_press(desktop, "Right"), "Advanced tab selected 2 of 2")
	nvda.step(_press(desktop, "shift+Tab"), "Fruit: list Alt+U", "Apples 1 of 3")
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
	desktop.startApp(desktop.fixture(fixture), title=title)
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
	app = desktop.startApp(desktop.fixture("twoWindows"), title="First window")
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
		"Cancel button Alt+C",
		"OK button Alt+O",
	)
	app.stdin.write("close\n")
	app.stdin.flush()
	nvda.exitCleanly()


def test_wxDialogAndStatusBar(desktop: DesktopSession, tmp_path: Path):
	app = desktop.startApp(desktop.fixture("wxfixture"), title="wx fixture")
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
	desktop.expect("Settings dialog", since=mark)
	nvda.settle(mark)
	nvda.step(lambda: nvda.gesture("NVDA+t"), "Settings")
	nvda.step(_press(desktop, "Escape"), "wx fixture", "Open dialog... button")
	nvda.exitCleanly()


def test_gtk4StatusBarAndListItems(desktop: DesktopSession, tmp_path: Path):
	app = desktop.startApp(desktop.fixture("gtk4"), title="GTK4 fixture")
	nvda = Reader(desktop, tmp_path)
	desktop.expect("GTK4 fixture", since=0, timeout=STARTUP_TIMEOUT)
	nvda.settle(0)
	nvda.step(lambda: nvda.gesture("NVDA+end"), "Ready")
	mark = desktop.mark()
	app.stdin.write("focus list\n")
	app.stdin.flush()
	desktop.expect("Items: list Alt+I", "Item 1 1 of 5", since=mark)
	nvda.settle(mark)
	nvda.step(_press(desktop, "Down"), "Item 2 2 of 5")
	nvda.exitCleanly()
