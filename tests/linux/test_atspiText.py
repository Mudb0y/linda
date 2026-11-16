"""Text in GTK and Qt fields as NVDA reads it: review, caret and selection, say all and typing echo,
with what a password field holds kept out of speech and the log.

NVDA's caret, review and say all scripts run from keys pressed on the rig's display when Basedreader
has a key source. Without one, a test plugin runs each script as that key would. A caret script's key
is pressed first and its gesture's send does nothing, as with the key source, which leaves every key
to the application too; so the caret has often moved before the script reads it.

nix develop . -c tests/linux/rig python3 -m pytest tests/linux/test_atspiText.py
"""

import re
import time
from collections.abc import Iterator
from pathlib import Path

import desktopSession
import harness
import pytest
from desktopSession import DesktopSession

BOOT_CONFIG = harness.HERE / "fixtures" / "bootConfig" / "nvda.ini"
#: Whether NVDA reads keys from the X server, so that a key pressed on the display runs its script.
HAS_KEY_SOURCE = (harness.SOURCE / "linuxPlatform" / "keys" / "x11.py").is_file()
#: Seconds from starting NVDA to its first words; a start under the rig takes about three.
STARTUP_BOUND = 60.0
WINDOW_TITLE = "TextFixture"
#: The key that runs each script in NVDA's desktop layout, pressed when there is a key source; a caret
#: script's key is given where it runs.
SCRIPT_KEYS = {
	"caret_moveByLine": None,
	"caret_moveByCharacter": None,
	"caret_moveByWord": None,
	"sayAll": "Insert+Down",
	"review_top": "shift+KP_Home",
	"review_nextLine": "KP_Prior",
	"review_startOfLine": "shift+KP_End",
	"review_nextWord": "KP_Right",
}

DRIVER_PLUGIN = '''
"""Runs NVDA scripts a test asks for, one per line of the file BASEDREADER_TEXT_DRIVER names, and
counts the lines it has run in that file with ".done" appended.

A line is "focus SCRIPT", a script of the focus object, or "global SCRIPT", one of globalCommands.
Each gets a gesture whose send does nothing: the test has pressed the key already.

If the AT-SPI handler does not hand text changes to linuxPlatform.atspi.typedChars, this does.
"""

import os

import api
import core
import globalCommands
import globalPluginHandler

COMMANDS = os.environ.get("BASEDREADER_TEXT_DRIVER")


class Gesture:
	wasInSayAll = False
	script = None
	isModifier = False
	speechEffectWhenExecuted = None

	def send(self):
		pass


def _onTextChange(event):
	from linuxPlatform.atspi import handler, typedChars

	try:
		node = handler.client().node(event.source)
		isPassword = event.source.get_role_name() == "password text"
		obj = handler.eventObject(node)
	except Exception:
		return
	kind = "insert" if event.type.startswith("object:text-changed:insert") else "delete"
	text = None if isPassword or not isinstance(event.any_data, str) else event.any_data
	typedChars.handleTextChange(obj, kind, event.detail1, event.detail2, text)


class GlobalPlugin(globalPluginHandler.GlobalPlugin):
	def __init__(self):
		super().__init__()
		self.running = bool(COMMANDS)
		if not self.running:
			return
		self.done = 0
		self.listener = None
		from linuxPlatform.atspi import handler

		with open(handler.__file__, encoding="utf-8") as f:
			if "typedChars" not in f.read():
				import gi

				gi.require_version("Atspi", "2.0")
				from gi.repository import Atspi

				self.listener = Atspi.EventListener.new(_onTextChange)
				self.listener.register("object:text-changed")
		core.callLater(100, self.poll)

	def poll(self):
		if not self.running:
			return
		with open(COMMANDS, encoding="utf-8") as f:
			lines = f.read().splitlines()
		for line in lines[self.done:]:
			kind, script = line.split()
			if kind == "focus":
				getattr(api.getFocusObject(), f"script_{script}")(Gesture())
			else:
				getattr(globalCommands.commands, f"script_{script}")(Gesture())
			self.done += 1
		# Written on the first poll too, which is NVDA's main loop running.
		with open(COMMANDS + ".done", "w", encoding="utf-8") as f:
			f.write(str(self.done))
		core.callLater(50, self.poll)

	def terminate(self):
		self.running = False
		if self.listener is not None:
			self.listener.deregister("object:text-changed")
		super().terminate()
'''


class TextSession:
	"""The text fixture in one toolkit, NVDA reading it, and the scripts a test runs in NVDA."""

	def __init__(self, desktop: DesktopSession, directory: Path, toolkit: str) -> None:
		self.desktop = desktop
		self.toolkit = toolkit
		self.app = desktop.startApp(desktop.fixture("textapp", toolkit), title=WINDOW_TITLE)
		hooks = directory / "hooks"
		(hooks / "globalPlugins").mkdir(parents=True)
		(hooks / "synthDrivers").symlink_to(harness.HERE / "synthDrivers")
		(hooks / "globalPlugins" / "textDriver.py").write_text(DRIVER_PLUGIN, encoding="utf-8")
		self.commands = directory / "commands.txt"
		self.commands.write_text("", encoding="utf-8")
		self.sent = 0
		config = directory / "nvda.ini"
		text = BOOT_CONFIG.read_text(encoding="utf-8")
		text = text.replace("[general]\n", "[general]\n\tshowWelcomeDialogAtStartup = False\n", 1)
		# Without a key source NVDA cannot read the Caps Lock state this beep needs.
		text += "[keyboard]\n\tbeepForLowercaseWithCapslock = False\n"
		config.write_text(text, encoding="utf-8")
		self.nvda = desktop.startNVDA(
			config,
			env={
				"BASEDREADER_TEST_HOOKS_DIR": str(hooks),
				"BASEDREADER_TEXT_DRIVER": str(self.commands),
			},
		)
		self._waitForDriver(0, "start", STARTUP_BOUND)
		if toolkit != "gtk4":
			# The entry has the focus when the fixture starts. NVDA finds no focus in GTK 4 at start.
			self.expect("entry edit…", since=0)

	def focus(self, name: str, *expected: str) -> None:
		"""Move the focus to the field named name, and wait for NVDA to speak expected."""
		mark = self.desktop.mark()
		if self.toolkit == "gtk4":
			# GTK 4 has no GrabFocus; the multi-line field follows the entry.
			assert name == "multi", name
			self.desktop.press("Tab")
		else:
			Atspi = desktopSession._atspi()
			assert Atspi.Component.grab_focus(_named(WINDOW_TITLE, name)), name
		self.expect(*expected, since=mark)

	def run(self, script: str, keys: str | None = None) -> int:
		"""Run an NVDA script as its key would, keys naming that key for a caret script, and return the
		mark from before it."""
		mark = self.desktop.mark()
		if HAS_KEY_SOURCE:
			self.desktop.press(keys or SCRIPT_KEYS[script])
			return mark
		kind = "focus" if script.startswith("caret_") else "global"
		if keys:
			self.desktop.press(keys, gap=0)
		with open(self.commands, "a", encoding="utf-8") as f:
			f.write(f"{kind} {script}\n")
		self.sent += 1
		self._waitForDriver(self.sent, script, 5)
		return mark

	def _waitForDriver(self, count: int, what: str, timeout: float) -> None:
		done = self.commands.with_name(self.commands.name + ".done")
		deadline = time.monotonic() + timeout
		while not (done.exists() and int(done.read_text() or 0) >= count):
			assert time.monotonic() < deadline, f"NVDA did not run {what}\n{self.nvda.describe()}"
			assert self.nvda.process.poll() is None, self.nvda.describe()
			time.sleep(0.02)

	def waitFor(self, isDone, what: str, *, since: int, timeout: float = 5) -> None:
		"""Wait until isDone holds of the texts NVDA spoke since the mark since."""
		deadline = time.monotonic() + timeout
		while not isDone(spoken := self.desktop.spokenSince(since)):
			assert time.monotonic() < deadline, f"NVDA did not speak {what}; it spoke {spoken}"
			time.sleep(0.05)

	def expect(self, *texts: str, since: int, timeout: float = 5) -> None:
		"""Wait until NVDA has spoken texts in this order since the mark since, with runs of spaces read
		as one; a text starting or ending with "…" matches the end or the start of what was spoken."""

		def matches(expected: str, spoken: str) -> bool:
			spoken = " ".join(spoken.split())
			if expected.endswith("…"):
				return spoken.startswith(expected[:-1])
			if expected.startswith("…"):
				return spoken.endswith(expected[1:])
			return spoken == expected

		def inOrder(spoken: list[str]) -> bool:
			records = iter(spoken)
			return all(any(matches(text, record) for record in records) for text in texts)

		self.waitFor(inOrder, str(list(texts)), since=since, timeout=timeout)

	def finish(self) -> None:
		self.desktop.close()
		assert self.nvda.process.returncode == 0, self.nvda.describe()
		self.nvda.assertClean(logNames=("nvda.log",))


def _named(windowTitle: str, name: str):
	Atspi = desktopSession._atspi()
	desktop = Atspi.get_desktop(0)
	for appIndex in range(desktop.get_child_count()):
		app = desktop.get_child_at_index(appIndex)
		for index in range(app.get_child_count() if app else 0):
			window = app.get_child_at_index(index)
			if window is not None and window.get_name() == windowTitle:
				found = _find(window, name)
				assert found is not None, f"{windowTitle!r} has nothing named {name!r}"
				return found
	raise AssertionError(f"no window {windowTitle!r}")


def _find(node, name: str):
	if node.get_name() == name:
		return node
	for index in range(node.get_child_count()):
		child = node.get_child_at_index(index)
		if child is not None and (found := _find(child, name)) is not None:
			return found
	return None


@pytest.fixture(scope="module")
def windowManager(tmp_path_factory: pytest.TempPathFactory) -> Iterator[DesktopSession]:
	"""One marco for every test here: the properties it sets on the root window outlive it, and a
	session started after one that stopped it would take them for a window manager still running."""
	with DesktopSession(tmp_path_factory.mktemp("windowManager")) as session:
		yield session


@pytest.fixture
def desktop(windowManager: DesktopSession, tmp_path: Path) -> Iterator[DesktopSession]:
	with DesktopSession(tmp_path / "desktop") as session:
		yield session


@pytest.fixture(params=["gtk3", "qt6"])
def text(request: pytest.FixtureRequest, desktop: DesktopSession, tmp_path: Path) -> TextSession:
	return TextSession(desktop, tmp_path / "text", request.param)


def test_reviewByLineAndWord(text: TextSession):
	text.focus("multi", "multi edit multi line…")
	text.expect("Zażółć gęślą jaźń", since=text.run("review_top"))
	text.expect("blank", since=text.run("review_nextLine"))
	text.expect("é café grinning face emoji Tab", since=text.run("review_nextLine"))
	# NVDA says it is a letter and a combining mark that it reads as one.
	text.expect("é normalized", since=text.run("review_startOfLine"))
	for word in ("café", "grinning face", "emoji", "Tab"):
		text.expect(word, since=text.run("review_nextWord"))
	text.finish()


@pytest.mark.parametrize("toolkit", ["gtk3", "qt6", "gtk4"])
def test_caretAndSelection(desktop: DesktopSession, tmp_path: Path, toolkit: str):
	text = TextSession(desktop, tmp_path / "text", toolkit)
	text.focus("multi", "multi edit multi line…")
	text.expect("Zażółć gęślą jaźń", since=text.run("caret_moveByLine", "ctrl+Home"))
	text.expect("blank", since=text.run("caret_moveByLine", "Down"))
	text.expect("é café grinning face emoji Tab", since=text.run("caret_moveByLine", "Down"))
	text.expect("space", since=text.run("caret_moveByCharacter", "Right"))
	text.expect("c", since=text.run("caret_moveByCharacter", "Right"))
	# Qt's own word movement stops at the emoji.
	word = "grinning face" if toolkit == "qt6" else "café"
	text.expect(word, since=text.run("caret_moveByWord", "ctrl+Right"))
	mark = text.desktop.mark()
	text.desktop.press("shift+Right")
	text.expect("… selected", since=mark)
	text.finish()


def test_sayAllReadsToTheEnd(text: TextSession):
	text.focus("multi", "multi edit multi line…")
	text.run("caret_moveByLine", "ctrl+Home")
	mark = text.run("sayAll")
	text.expect("Zażółć gęślą jaźń…", "…ostatnia bez końca", since=mark, timeout=15)
	text.finish()


def test_typingEcho(text: TextSession):
	mark = text.desktop.mark()
	text.desktop.typeText("ab")
	text.expect("a", "b", since=mark)
	text.finish()


def test_passwordIsNeverHeardOrLogged(text: TextSession):
	# Its masking characters are spoken as NVDA's protected character.
	text.focus("password", "…star")
	mark = text.desktop.mark()
	text.desktop.typeText("sec")
	text.expect("star", "star", "star", since=mark)
	spoken = text.desktop.spokenSince(0)
	text.finish()
	assert not [s for s in spoken if "sec" in s], spoken
	log = (text.nvda.directory / "nvda.log").read_text(encoding="utf-8")
	assert not re.search(r"\bsec\b|'[sec]'", log), [
		line for line in log.splitlines() if re.search(r"\bsec\b|'[sec]'", line)
	]
