"""NVDA's menu on a Wayland display, opened as a window of its own, and on an X display as before.

Under each of the rig's compositors Linda runs natively beside the GTK 4 key probe, with a global plugin
of the test's own that opens the menu as NVDA+N does, since the key route on Wayland is not this file's,
and records what NVDA's handlers see. Keys go to the focused window through the rig's virtual keyboard,
and an AT-SPI listener of the test's own records which process each activated window belongs to.

tests/linux/wayland-gate SCRATCH runs this file under Mutter and sway, with and without Xwayland; in the
X rig only the X test runs:

nix develop . -c tests/linux/rig python3 -m pytest -q tests/linux/test_waylandMenu.py
"""

import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import desktopSession
import harness
import pytest

SESSION_CONFIG = harness.HERE / "fixtures" / "sessionConfig" / "nvda.ini"
KEY_PROBE = harness.HERE / "fixtures" / "apps" / "waylandKeyProbe.py"
#: Seconds from starting Linda to its plugin's first record; a start under the rig takes about three.
STARTUP_BOUND = 60.0
#: Seconds Linda has to exit on SIGTERM before it is killed.
EXIT_BOUND = 10.0
#: Seconds the menu window has from PopupMenu to being activated, as the package asks.
ACTIVATION_BOUND = 1.0
#: The title of NVDA's menu window and its first two items, as the tree speaks them.
MENU_TITLE = "Linda"
#: Seconds to wait for anything more after what a step expects.
SETTLE = 0.4
#: The start of the warning logged when GDK's display is not of the session's kind.
DISAGREEMENT = "GDK opened a display of kind"
#: Typed into the probe, and never to be found in Linda's log.
MARKER = "kqzmxjwv"
GTK = ("gtkFixture", "Fixture window")

#: A global plugin that takes commands from files a test writes, and records what NVDA's own code sees,
#: as JSON lines with the monotonic clock every process shares.
PLUGIN = """\
import json
import os
import time

import api
import appModuleHandler
import config
import globalPluginHandler
import globalVars
import gui
import wx
from gui import message
from linuxPlatform import ownGui

DIRECTORY = os.environ.get("LINDA_WINDOWS_PROBE")
PROFILE = "windowsProbe"


def describe(obj):
	if obj is None:
		return None
	return {"name": obj.name, "role": obj.role.name, "processID": obj.processID}


def record(kind, **fields):
	with open(os.path.join(DIRECTORY, "records.jsonl"), "a", encoding="utf-8") as out:
		out.write(json.dumps({"kind": kind, "t": time.monotonic(), **fields}) + "\\n")


def profiles():
	return [profile.name for profile in config.conf.profiles]


def shownWindow(title):
	for window in wx.GetTopLevelWindows():
		if window.IsShown() and window.GetTitle().startswith(title):
			return window
	return None


class GlobalPlugin(globalPluginHandler.GlobalPlugin):
	def __init__(self):
		super().__init__()
		if not DIRECTORY:
			return
		self.focusAtMenu = None
		appID = os.environ.get("LINDA_WINDOWS_PROBE_APP")
		if appID:
			# A profile the probe's application triggers, which must not switch while NVDA's menu is open.
			appName = appModuleHandler.getAppNameFromProcessID(int(appID))
			if PROFILE not in config.conf.listProfiles():
				config.conf.createProfile(PROFILE)
			config.conf.triggersToProfiles[f"app:{appName}"] = PROFILE
		config.post_configProfileSwitch.register(self.onProfileSwitch)
		tray = gui.mainFrame.sysTrayIcon
		popUp = tray.PopupMenu

		def popupMenu(menu):
			self.focusAtMenu = api.getFocusObject()
			record("popupMenu", focus=describe(self.focusAtMenu), displayKind=ownGui.displayKind())
			try:
				return popUp(menu)
			finally:
				record("popupReturned")

		tray.PopupMenu = popupMenu
		popupSettings = gui.mainFrame.popupSettingsDialog

		def popupSettingsDialog(dialog, *args, **kwargs):
			prevFocus = gui.mainFrame.prevFocus
			record(
				"settingsHandler",
				menuOpen=gui._isNvdaMenuOpen,
				prevFocus=describe(prevFocus),
				prevFocusIsFocusAtMenu=prevFocus is not None and prevFocus is self.focusAtMenu,
				profiles=profiles(),
			)
			return popupSettings(dialog, *args, **kwargs)

		gui.mainFrame.popupSettingsDialog = popupSettingsDialog
		presentAgain = ownGui.presentAgain

		def presentAgainRecorded(window):
			record("presentAgain", title=window.GetTitle())
			presentAgain(window)

		ownGui.presentAgain = presentAgainRecorded
		raiseWindow = ownGui._upstreamRaise

		def raiseRecorded(window):
			record("upstreamRaise", title=window.GetTitle() if isinstance(window, wx.TopLevelWindow) else "")
			raiseWindow(window)

		ownGui._upstreamRaise = raiseRecorded
		self.timer = wx.Timer()
		self.timer.Bind(wx.EVT_TIMER, self.poll)
		self.timer.Start(50)
		record(
			"ready",
			pid=globalVars.appPid,
			displayKind=ownGui.displayKind(),
			keptGtk=ownGui._keptGtk is not None,
		)

	def onProfileSwitch(self, **kwargs):
		record("profileSwitch", profiles=profiles())

	def poll(self, evt):
		commands = os.path.join(DIRECTORY, "commands")
		for name in sorted(os.listdir(commands)):
			path = os.path.join(commands, name)
			with open(path, encoding="utf-8") as source:
				words = json.load(source)
			os.unlink(path)
			# After this handler, so that a command that blocks does not hold the timer.
			wx.CallAfter(self.run, *words)

	def run(self, command, *args):
		getattr(self, "do_" + command)(*args)

	def do_state(self, title=""):
		window = shownWindow(title) if title else None
		record(
			"state",
			focus=describe(api.getFocusObject()),
			foreground=describe(api.getForegroundObject()),
			menuOpen=gui._isNvdaMenuOpen,
			profiles=profiles(),
			shown=window is not None,
			active=window is not None and window.IsActive(),
			topLevels=[w.GetTitle() for w in wx.GetTopLevelWindows() if w.IsShown()],
		)

	def do_menu(self):
		gui.mainFrame.showGui()

	def do_closeMenu(self, title):
		window = shownWindow(title)
		if isinstance(window, wx.Dialog) and window.IsModal():
			window.EndModal(wx.ID_CANCEL)

	def do_settings(self):
		gui.mainFrame.onNVDASettingsCommand(None)

	def do_raise(self, title):
		window = shownWindow(title)
		record("raising", title=title, found=window is not None, active=bool(window and window.IsActive()))
		window.Raise()

	def do_setFocus(self, title):
		window = shownWindow(title)
		record("focusing", title=title, found=window is not None, active=bool(window and window.IsActive()))
		window.SetFocus()

	def do_focusBlocking(self):
		message.MessageDialog.focusBlockingInstances()

	def do_messageBox(self, caption):
		record("messageShown", caption=caption)
		answer = gui.messageBox("The windows probe asks.", caption, wx.OK | wx.CANCEL)
		record("messageAnswered", caption=caption, answer=answer, ok=answer == wx.OK)
"""

#: Prints, as JSON lines, every window activation and deactivation the accessibility bus carries, with
#: the process of the window's application.
LISTENER = """\
import json
import time

import gi

gi.require_version("Atspi", "2.0")
from gi.repository import Atspi, GLib


def onEvent(event):
	try:
		processID = event.source.get_process_id()
		name = event.source.get_name()
	except Exception:
		processID, name = 0, ""
	print(json.dumps({"t": time.monotonic(), "type": event.type, "pid": processID, "name": name}), flush=True)


listener = Atspi.EventListener.new(onEvent)
listener.register("window:activate")
listener.register("window:deactivate")
print("ready", flush=True)
GLib.MainLoop().run()
"""


def _holdInOrder(texts: tuple[str, ...], spoken: list[str]) -> bool:
	"""Whether each of texts is in a speak record after the one the text before it was in; a tree item
	says its level beside its name, before or after it."""
	records = iter(spoken)
	return all(any(text in record for record in records) for text in texts)


class Lines:
	"""A child process whose standard output is read line by line on a thread of its own."""

	def __init__(self, command: list[str], stderr: Path, env: dict[str, str] | None = None) -> None:
		with open(stderr, "w") as errors:
			self.process = subprocess.Popen(
				command,
				stdout=subprocess.PIPE,
				stderr=errors,
				text=True,
				env={**os.environ, **(env or {})},
			)
		self.lines: list[str] = []
		self._queue: queue.Queue[str] = queue.Queue()
		threading.Thread(target=self._read, daemon=True).start()

	def _read(self) -> None:
		for line in self.process.stdout:
			self.lines.append(line.rstrip("\n"))

	def waitFor(self, predicate: Callable[[str], bool], timeout: float, since: int = 0) -> str:
		deadline = time.monotonic() + timeout
		while time.monotonic() < deadline:
			for line in self.lines[since:]:
				if predicate(line):
					return line
			if self.process.poll() is not None:
				break
			time.sleep(0.05)
		raise AssertionError(
			f"{self.process.args} never printed what was waited for; it printed {self.lines}"
		)

	def stop(self) -> None:
		if self.process.poll() is None:
			self.process.terminate()
			try:
				self.process.wait(5)
			except subprocess.TimeoutExpired:
				self.process.kill()
				self.process.wait()


class Listener(Lines):
	def __init__(self, directory: Path) -> None:
		super().__init__([sys.executable, "-c", LISTENER], directory / "listener.stderr.txt")
		self.waitFor(lambda line: line == "ready", 10)

	def events(self) -> list[dict]:
		return [json.loads(line) for line in self.lines if line.startswith("{")]

	def activation(self, predicate: Callable[[dict], bool], since: float, timeout: float = 5) -> dict:
		"""The first window:activate after since that predicate accepts."""
		deadline = time.monotonic() + timeout
		while True:
			for event in self.events():
				if event["type"] == "window:activate" and event["t"] >= since and predicate(event):
					return event
			assert time.monotonic() < deadline, f"no such activation; the bus carried {self.events()}"
			time.sleep(0.05)


class KeyProbe(Lines):
	"""The GTK 4 key probe, a native Wayland window, once it is active."""

	def __init__(self, directory: Path, name: str = "probe") -> None:
		super().__init__([sys.executable, str(KEY_PROBE)], directory / f"{name}.stderr.txt")
		self.waitFor(lambda line: line == "active", 15)


class Linda:
	"""Linda with the windows probe plugin: commands to it, what it recorded and what it spoke."""

	def __init__(
		self,
		directory: Path,
		*,
		appProcess: int | None = None,
		config: Path = SESSION_CONFIG,
		env: dict[str, str] | None = None,
	) -> None:
		self.directory = directory
		self.probe = directory / "probe"
		(self.probe / "commands").mkdir(parents=True)
		(self.probe / "records.jsonl").touch()
		hooks = directory / "hooks"
		(hooks / "globalPlugins").mkdir(parents=True, exist_ok=True)
		(hooks / "globalPlugins" / "windowsProbe.py").write_text(PLUGIN, encoding="utf-8")
		(hooks / "synthDrivers").symlink_to(harness.HERE / "synthDrivers")
		configDir = directory / "config"
		configDir.mkdir()
		(configDir / "nvda.ini").write_text(config.read_text(encoding="utf-8"), encoding="utf-8")
		env = {"LINDA_TEST_HOOKS_DIR": str(hooks), "LINDA_WINDOWS_PROBE": str(self.probe), **(env or {})}
		if appProcess is not None:
			env["LINDA_WINDOWS_PROBE_APP"] = str(appProcess)
		self.launch = harness.startLauncher(
			directory / "run",
			"-c",
			str(configDir),
			"-f",
			str(directory / "run" / "nvda.log"),
			"--debug-logging",
			env=env,
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

	def records(self, kind: str | None = None, since: float = 0) -> list[dict]:
		text = (self.probe / "records.jsonl").read_text(encoding="utf-8")
		records = [json.loads(line) for line in text.splitlines() if line.strip()]
		return [r for r in records if (kind is None or r["kind"] == kind) and r["t"] >= since]

	def waitFor(
		self,
		kind: str,
		predicate: Callable[[dict], bool] = lambda record: True,
		since: float = 0,
		timeout: float = 10,
	) -> dict:
		deadline = time.monotonic() + timeout
		while True:
			exited = self.launch.process.poll() is not None
			for record in self.records(kind, since):
				if predicate(record):
					return record
			if exited or time.monotonic() >= deadline:
				raise AssertionError(
					f"no {kind} record as waited for; records: {self.records(since=since)}\n{self.launch.describe()}"
				)
			time.sleep(0.05)

	def state(self, title: str = "") -> dict:
		since = self.command("state", title)
		return self.waitFor("state", since=since)

	def waitForState(self, predicate: Callable[[dict], bool], title: str = "", timeout: float = 10) -> dict:
		deadline = time.monotonic() + timeout
		while True:
			state = self.state(title)
			if predicate(state):
				return state
			assert time.monotonic() < deadline, f"the state never held: {state}\n{self.launch.describe()}"
			time.sleep(0.2)

	def spoken(self) -> list[str]:
		return self.launch.spokenTexts()

	def expect(self, *texts: str, since: int, timeout: float = 5) -> None:
		"""Wait until Linda has spoken a record holding each of texts, in this order, after since."""
		deadline = time.monotonic() + timeout
		while not _holdInOrder(texts, self.spoken()[since:]):
			assert self.launch.process.poll() is None and time.monotonic() < deadline, (
				f"Linda did not speak {list(texts)} in order; it spoke {self.spoken()[since:]}\n"
				f"{self.launch.describe()}"
			)
			time.sleep(0.05)

	def stop(self) -> None:
		process = self.launch.process
		if process.poll() is None:
			process.send_signal(signal.SIGTERM)
			try:
				process.wait(EXIT_BOUND)
			except subprocess.TimeoutExpired:
				process.kill()
				process.wait()


class WaylandSession:
	"""The listener, the GTK 4 probe holding the keyboard, and Linda beside it with its focus there."""

	def __init__(self, directory: Path) -> None:
		directory.mkdir(parents=True, exist_ok=True)
		self.directory = directory
		self._children: list[Lines] = []
		self.listener = self.start(Listener(directory))
		self.probe = self.start(KeyProbe(directory))
		self.linda = Linda(directory / "linda", appProcess=self.probe.process.pid)
		self.lindaID = self.linda.ready["pid"]
		assert self.linda.ready["displayKind"] == "wayland", self.linda.ready
		# On a Wayland display the map hook no longer asks gi for Gtk at each map.
		assert self.linda.ready["keptGtk"], self.linda.ready

	def start(self, child: Lines) -> Lines:
		self._children.append(child)
		return child

	def probeHasFocus(self) -> dict:
		"""Wait until Linda's focus is in the probe and the probe's profile is on; Linda's state then."""
		probeID = self.probe.process.pid
		return self.linda.waitForState(
			lambda state: (
				(state["focus"] or {}).get("processID") == probeID and state["profiles"][-1] == "windowsProbe"
			),
			timeout=20,
		)

	def press(self, *chords: str) -> float:
		import waylandKeyboard

		pressed = time.monotonic()
		waylandKeyboard.press(*chords)
		return pressed

	def close(self) -> None:
		self.linda.stop()
		for child in reversed(self._children):
			child.stop()


@pytest.fixture
def session(tmp_path: Path) -> Iterator[WaylandSession]:
	opened = WaylandSession(tmp_path / "session")
	try:
		yield opened
	finally:
		opened.close()


def openMenu(session: WaylandSession) -> tuple[dict, dict]:
	"""Open NVDA's menu as NVDA+N does; its PopupMenu record and the activation of its window."""
	given = session.linda.command("menu")
	popup = session.linda.waitFor("popupMenu", since=given)
	activation = session.listener.activation(
		lambda event: event["pid"] == session.lindaID and event["name"] == MENU_TITLE,
		since=popup["t"],
	)
	return popup, activation


@pytest.mark.rigSession("wayland")
def test_menuWindowOpensActiveAndIsUsedFromTheKeyboard(session: WaylandSession) -> None:
	linda = session.linda
	before = session.probeHasFocus()
	assert not before["menuOpen"]

	mark = len(linda.spoken())
	popup, activation = openMenu(session)
	assert popup["displayKind"] == "wayland"
	assert popup["focus"]["processID"] == session.probe.process.pid, popup
	assert activation["t"] - popup["t"] < ACTIVATION_BOUND, (popup, activation)
	linda.expect(f"{MENU_TITLE} dialog", "tree view", "Preferences collapsed 1 of 9", since=mark)
	# PopupMenu has not returned, the flag holds, and the probe's profile stays on.
	opened = linda.waitForState(lambda state: state["foreground"]["processID"] == session.lindaID)
	assert opened["menuOpen"] and opened["profiles"][-1] == "windowsProbe", opened
	assert not linda.records("popupReturned", since=popup["t"])

	for key, said in (
		("Down", "Tools collapsed 2 of 9"),
		("Right", "View log 1 of 6"),
		("Left", "Tools collapsed 2 of 9"),
		("Up", "Preferences collapsed 1 of 9"),
	):
		mark = len(linda.spoken())
		session.press(key)
		linda.expect(said, since=mark)
		time.sleep(SETTLE)
		assert len(linda.spoken()[mark:]) == 1, (key, linda.spoken()[mark:])
	assert not linda.records("popupReturned", since=popup["t"])
	assert not linda.records("profileSwitch", since=popup["t"])

	escaped = session.press("Escape")
	returned = linda.waitFor("popupReturned", since=popup["t"])
	assert returned["t"] >= escaped
	session.listener.activation(lambda event: event["pid"] == session.probe.process.pid, since=escaped)
	closed = linda.waitForState(
		lambda state: (state["focus"] or {}).get("processID") == session.probe.process.pid
	)
	assert not closed["menuOpen"] and closed["profiles"][-1] == "windowsProbe", closed
	# Triggers resume once the probe has the focus again, and its profile is still the one on.
	switches = linda.records("profileSwitch", since=popup["t"])
	assert all(
		switch["t"] >= returned["t"] and switch["profiles"][-1] == "windowsProbe" for switch in switches
	)

	# Keys reach the probe again, and nothing typed there is in Linda's log.
	given = len(session.probe.lines)
	session.press(*MARKER)
	session.probe.waitFor(lambda line: line == f"text {MARKER}", 5, since=given)
	linda.stop()
	harness.assertMarkerAbsent(MARKER, {"nvda.log": linda.launch.text("nvda.log")})


@pytest.mark.rigSession("wayland")
def test_settingsFromTheMenuWindowSeeWhatTheyDoOnX(session: WaylandSession) -> None:
	linda = session.linda
	session.probeHasFocus()
	mark = len(linda.spoken())
	popup, _activation = openMenu(session)
	linda.expect("Preferences collapsed 1 of 9", since=mark)
	mark = len(linda.spoken())
	session.press("Return")
	linda.expect("Settings... 1 of 4", since=mark)
	chosen = session.press("Return")
	handler = linda.waitFor("settingsHandler", since=chosen)
	assert handler["menuOpen"] and handler["prevFocusIsFocusAtMenu"], handler
	assert handler["prevFocus"]["processID"] == session.probe.process.pid, handler
	assert handler["profiles"][-1] == "windowsProbe", handler
	# As on X, the handler runs inside PopupMenu, after the menu has gone.
	returned = linda.waitFor("popupReturned", since=popup["t"])
	assert returned["t"] >= handler["t"]
	session.listener.activation(
		lambda event: event["pid"] == session.lindaID and event["name"].startswith("Linda Settings"),
		since=chosen,
	)
	linda.expect("Linda Settings: General (normal configuration) dialog", "Categories: list", since=mark)
	escaped = session.press("Escape")
	session.listener.activation(lambda event: event["pid"] == session.probe.process.pid, since=escaped)


@pytest.mark.rigSession("wayland")
def test_lettersChooseByMnemonic(session: WaylandSession) -> None:
	linda = session.linda
	session.probeHasFocus()
	popup, _activation = openMenu(session)
	mark = len(linda.spoken())
	# Tools, then its View log: the letters are the menu's own.
	session.press("t")
	linda.expect("View log 1 of 6", since=mark)
	mark = len(linda.spoken())
	session.press("Escape")
	linda.expect("Tools collapsed 2 of 9", since=mark)
	escaped = session.press("Escape")
	returned = linda.waitFor("popupReturned", since=popup["t"])
	assert returned["t"] >= escaped
	assert not linda.records("settingsHandler", since=popup["t"])


@pytest.mark.rigSession("wayland")
def test_menuWindowClosedWithinASecondLeavesNoWarning(session: WaylandSession) -> None:
	"""The check a second after the map finds the window destroyed, and GTK has dropped its handlers."""
	linda = session.linda
	session.probeHasFocus()
	popup, _activation = openMenu(session)
	escaped = session.press("Escape")
	returned = linda.waitFor("popupReturned", since=popup["t"])
	assert returned["t"] - popup["t"] < ACTIVATION_BOUND, (popup, returned)
	session.listener.activation(lambda event: event["pid"] == session.probe.process.pid, since=escaped)
	time.sleep(ACTIVATION_BOUND + 1)
	linda.stop()
	lines = [line for line in linda.launch.text("nvda.log").splitlines() if "has no handler with id" in line]
	assert not lines, lines


@pytest.mark.rigSession("x11")
def test_onXTheTrayMenuIsTheGtkPopupAndHandlersSeeTheSame(tmp_path: Path) -> None:
	"""Experiment E23 on X: the menu is wx's popup, and the settings handler runs inside PopupMenu."""
	with desktopSession.DesktopSession(tmp_path / "desktop") as desktop:
		app = desktop.startApp(desktop.fixture(GTK[0]), title=GTK[1])
		assert app.stdout.readline() == "ready\n"
		desktopSession.activate(GTK[1])
		linda = Linda(tmp_path / "linda")
		try:
			assert linda.ready["displayKind"] == "x11"
			assert not linda.ready["keptGtk"], linda.ready
			linda.waitForState(lambda state: (state["focus"] or {}).get("processID") == app.pid)
			mark = len(linda.spoken())
			desktop.press("Insert+n")
			popup = linda.waitFor("popupMenu")
			assert popup["displayKind"] == "x11"
			linda.expect("Preferences sub Menu P 1 of 9", since=mark)
			mark = len(linda.spoken())
			desktop.press("Return")
			linda.expect("Settings... S 1 of 4", since=mark)
			desktop.press("Return")
			handler = linda.waitFor("settingsHandler", since=popup["t"])
			assert handler["menuOpen"] and handler["prevFocusIsFocusAtMenu"], handler
			assert handler["prevFocus"]["processID"] == app.pid, handler
			returned = linda.waitFor("popupReturned", since=popup["t"])
			assert returned["t"] >= handler["t"]
			linda.expect("Linda Settings: General (normal configuration) dialog", since=mark)
			assert not linda.records("presentAgain")
			assert DISAGREEMENT not in linda.launch.text("nvda.log")
		finally:
			linda.stop()


@pytest.mark.rigSession("x11")
def test_displayOfAnotherKindThanTheSessionIsLoggedOnceAndFollowed(tmp_path: Path) -> None:
	"""A session forced to Wayland whose GDK opened the rig's X display: NVDA's windows follow GDK."""
	with desktopSession.DesktopSession(tmp_path / "desktop") as desktop:
		app = desktop.startApp(desktop.fixture(GTK[0]), title=GTK[1])
		assert app.stdout.readline() == "ready\n"
		desktopSession.activate(GTK[1])
		linda = Linda(tmp_path / "linda", env={"LINDA_SESSION_TYPE": "wayland"})
		try:
			assert linda.ready["displayKind"] == "x11"
			# What is done for a Wayland display follows the display, not the session.
			assert not linda.ready["keptGtk"], linda.ready
			lines = [line for line in linda.launch.text("nvda.log").splitlines() if DISAGREEMENT in line]
			assert lines == [
				(
					"GDK opened a display of kind x11 in a session of kind wayland; NVDA's own windows are shown "
					"for the display. The session is wayland by rule 1: LINDA_SESSION_TYPE is wayland"
				),
			], lines
			linda.waitForState(lambda state: (state["focus"] or {}).get("processID") == app.pid)
			mark = len(linda.spoken())
			popup = linda.command("menu")
			linda.expect("Preferences sub Menu P 1 of 9", since=mark)
			assert linda.waitFor("popupMenu", since=popup)["displayKind"] == "x11"
			desktop.press("Escape")
			linda.waitFor("popupReturned", since=popup)
		finally:
			linda.stop()
