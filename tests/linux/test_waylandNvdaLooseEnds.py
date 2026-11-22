"""NVDA's own commands where Linda cannot reach the clipboard or draw a highlight, as on a Wayland
desktop: a refused read of the clipboard speaks the refusal alone; a refused copy from the log fragment
command, the About dialog, a browseable message or Remote Access speaks the refusal and never a claim
that it copied; and Visual Highlight switched on in the settings logs no error at a start and is still
on afterwards. An X display keeps every one of them as before, a failed copy's old lines included.

The X rig runs NVDA's code in a child with GDK's display taken as Wayland and no compositor to reach,
which is what the clipboard refuses for, and as X, and starts Linda with the session forced to Wayland;
under the compositors Linda runs natively, and under sway the clipboard is refused for real, with data
control taken from Linda's reach and sway told never to give Linda's windows the keyboard.
tests/linux/wayland-gate runs the compositor half:

nix develop . -c tests/linux/rig python3 -m pytest -q tests/linux/test_waylandNvdaLooseEnds.py
nix develop . -c tests/linux/wayland-gate SCRATCH -k LooseEnds
"""

import json
import shutil
import subprocess
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

import desktopSession
import harness
import pytest
from desktopSession import DesktopSession

SESSION_CONFIG = harness.HERE / "fixtures" / "sessionConfig" / "nvda.ini"
HIGHLIGHT_ON = """[vision]
	[[NVDAHighlighter]]
		enabled = True
		highlightFocus = True
		highlightNavigator = True
		highlightBrowseMode = True
"""
#: Seconds from starting NVDA to its first words; a start under the rig takes about three.
STARTUP_BOUND = 60.0
#: Seconds NVDA runs on after its first words, past the vision handler's first profile switch.
RUN_ON = 3.0
#: Seconds for the plugin to answer a command, a copy refused after the clipboard's second for a
#: window of Linda's own to be given the keyboard included.
ANSWER_BOUND = 10.0
#: Seconds for anything said after a command, ui.delayedMessage's included, to have been said.
SETTLE = 1.0
GTK = ("gtkFixture", "Fixture window")
AT_START = ("Fixture window", "Press me button Does nothing Alt plus P")

CANNOT_COPY = "Linda cannot copy to the clipboard in this session"
CANNOT_READ = "Linda cannot read the clipboard here"
NO_TEXT = "There is no text on the clipboard"
UNABLE = "Unable to copy"
COPIED = "Copied to clipboard"
COPIED_LINK = "Copied link"
FRAGMENT_COPIED = "Log fragment copied to clipboard"
FRAGMENT_MARKED = "Log fragment start position marked, press again to copy to clipboard"
#: What each place copies where the test chooses it.
BROWSEABLE = "A browseable message to copy"
LINK = "nvdaremote://example.invalid/?key=looseEnds&port=6837&mode=master"
#: The places that copy, in the order they are tried; reportClipboardText reads.
COPIERS = ("logFragment", "version", "browseable", "link")
COULD_NOT_HIGHLIGHT = "Could not initialize the NVDAHighlighter vision enhancement provider"
NOT_STARTED = "The NVDAHighlighter vision enhancement provider is not started: this session lacks it"

#: Variables a child gets so that only those a case names decide its session.
UNSET = dict.fromkeys(
	[
		"LINDA_SESSION_TYPE",
		"XDG_SESSION_TYPE",
		"XDG_CURRENT_DESKTOP",
		"WAYLAND_DISPLAY",
		"GAMESCOPE_WAYLAND_DISPLAY",
	]
)
FORCED_WAYLAND = {"LINDA_SESSION_TYPE": "wayland", "XDG_CURRENT_DESKTOP": None, "XKB_DEFAULT_LAYOUT": "us"}

#: Each place NVDA copies or reads the clipboard from, as its command or button runs it. With
#: LOOSE_ENDS_DISPLAY wayland, GDK's display is taken to be Wayland's, and with no compositor to reach the
#: clipboard refuses as it does on a desktop that lets Linda neither copy nor read. With x11, the X
#: clipboard is used, through GTK; LOOSE_ENDS_X failed has it fail as an X owner that never answers does.
PLACES = f"""
import os
import types

import languageHandler

languageHandler.setLanguage("en")
import NVDAHelper  # noqa: F401
import wx

app = wx.App()
import globalCommands
import gui
import ui
import versionInfo
from _remoteClient.client import RemoteClient
from linuxPlatform import clipboard, ownGui
from logHandler import log

spoken = []
ui.message = lambda text, *args, **kwargs: spoken.append(text)
ui.delayedMessage = lambda text, *args, **kwargs: spoken.append(text)
if os.environ["LOOSE_ENDS_DISPLAY"] == "wayland":
	ownGui._displayKind = "wayland"
elif os.environ["LOOSE_ENDS_X"] == "failed":

	def neverAnswered(*args, **kwargs):
		raise OSError("the clipboard's owner did not answer")

	clipboard.setText = clipboard.getText = neverAnswered
else:
	# What NVDA+c reads.
	clipboard.setText("On the clipboard before")
log.fragmentStart = 0
log.getFragment = lambda: "A fragment of the log"
link = types.SimpleNamespace(getURLToConnect=lambda: {LINK!r})
remote = types.SimpleNamespace(
	leaderSession=types.SimpleNamespace(getConnectionInfo=lambda: link), followerSession=None
)
places = {{
	"report": lambda: globalCommands.commands.script_reportClipboardText(None),
	"logFragment": lambda: globalCommands.commands.script_log_markStartThenCopy(None),
	"version": lambda: gui.MainFrame._copyVersionToClipboard(None),
	"browseable": lambda: ui._copyBrowseableMessageToClipboard({BROWSEABLE!r}),
	"link": lambda: RemoteClient.copyLink(remote),
}}
result = {{"displayKind": ownGui.displayKind(), "spoken": {{}}, "clipboard": {{}}}}
for name, place in places.items():
	del spoken[:]
	place()
	result["spoken"][name] = spoken[:]
	if name != "report" and result["displayKind"] == "x11" and os.environ["LOOSE_ENDS_X"] == "made":
		result["clipboard"][name] = clipboard.getText()
result["version"] = f"{{versionInfo.version}} ({{versionInfo.version_detailed}})"
"""


def runPlaces(tmp_path: Path, display: str, x: str = "made") -> harness.ChildResult:
	return harness.runChild(
		tmp_path,
		PLACES,
		env={
			**UNSET,
			# The owner's X session, or a Wayland one, whose display GDK is then taken to have opened.
			**(FORCED_WAYLAND if display == "wayland" else {}),
			"LOOSE_ENDS_DISPLAY": display,
			"LOOSE_ENDS_X": x,
		},
	)


@pytest.mark.rigSession("x11")
def test_aRefusedClipboardSaysTheRefusalAloneFromEachPlace(tmp_path: Path):
	done = runPlaces(tmp_path, "wayland")
	done.assertClean()
	assert done.value["displayKind"] == "wayland"
	# Before, NVDA+c went on to say there was no text, the log fragment's copy said "Unable to copy",
	# and the other three said they had copied.
	assert done.value["spoken"] == {
		"report": [CANNOT_READ],
		**{name: [CANNOT_COPY] for name in COPIERS},
	}


@pytest.mark.rigSession("x11")
def test_anXDisplaySaysWhatItAlwaysSaid(tmp_path: Path):
	made = runPlaces(tmp_path, "x11", "made")
	made.assertClean()
	assert made.value["displayKind"] == "x11"
	assert made.value["spoken"] == {
		"report": ["On the clipboard before"],
		"logFragment": [FRAGMENT_COPIED],
		"version": [COPIED],
		"browseable": [COPIED],
		"link": [COPIED_LINK],
	}
	assert made.value["clipboard"] == {
		"logFragment": "A fragment of the log",
		"version": made.value["version"],
		"browseable": BROWSEABLE,
		"link": LINK,
	}
	# A copy that fails on X is answered as it always was, the three that never looked at the answer
	# claiming the copy as before: the Wayland branches are not taken.
	failed = runPlaces(tmp_path, "x11", "failed")
	failed.assertClean()
	assert failed.value["displayKind"] == "x11"
	assert failed.value["spoken"] == {
		"report": [NO_TEXT],
		"logFragment": [UNABLE],
		"version": [COPIED],
		"browseable": [COPIED],
		"link": [COPIED_LINK],
	}


#: The vision handler's first profile switch, as at a start, with Visual Highlight on in the settings.
#: With LOOSE_ENDS_HIGHLIGHT cannotStart, the highlighter refuses to start as it does on an X display
#: it cannot draw on.
VISION = """
import os

import languageHandler

languageHandler.setLanguage("en")
import NVDAHelper  # noqa: F401
import config
import platformFeatures
from vision import visionHandler
from visionEnhancementProviders import NVDAHighlighter

config.conf["vision"]["NVDAHighlighter"]["enabled"] = True
started = []
if os.environ.get("LOOSE_ENDS_HIGHLIGHT") == "cannotStart":
	NVDAHighlighter.NVDAHighlighter.canStart = classmethod(lambda cls: False)
else:
	NVDAHighlighter.NVDAHighlighter.__init__ = lambda self: started.append(1)
	NVDAHighlighter.NVDAHighlighter.registerEventExtensionPoints = lambda self, points: None
handler = visionHandler.VisionHandler()
handler._updateAllProvidersList()
handler.handleConfigProfileSwitch()
result = {
	"supported": platformFeatures.isSupported(platformFeatures.Feature.VISUAL_HIGHLIGHT),
	"providers": sorted(handler._providers),
	"started": len(started),
	"enabled": config.conf["vision"]["NVDAHighlighter"]["enabled"],
}
"""


@pytest.mark.rigSession("x11")
def test_visualHighlightOnInTheSettingsLogsNoErrorInAWaylandSession(tmp_path: Path):
	done = harness.runChild(tmp_path, VISION, env={**UNSET, "LINDA_SESSION_TYPE": "wayland"})
	done.assertClean()
	assert done.value == {"supported": False, "providers": [], "started": 0, "enabled": True}


@pytest.mark.rigSession("x11")
def test_anXSessionStartsVisualHighlightOrLogsItsFailureAsBefore(tmp_path: Path):
	started = harness.runChild(tmp_path, VISION, env=UNSET)
	started.assertClean()
	assert started.value == {
		"supported": True,
		"providers": ["NVDAHighlighter"],
		"started": 1,
		"enabled": True,
	}
	# A highlighter that cannot start on X is still an error there, as upstream logs it.
	failed = harness.runChild(tmp_path, VISION, env={**UNSET, "LOOSE_ENDS_HIGHLIGHT": "cannotStart"})
	failed.assertClean(ignoring=("logErrors",))
	assert failed.value == {"supported": True, "providers": [], "started": 0, "enabled": True}
	assert [record for record in failed.logErrors if COULD_NOT_HIGHLIGHT in record], failed.logErrors


# Linda itself.

#: A global plugin that runs the places of PLACES on commands from files a test writes, and records
#: what came of each, as JSON lines.
PLUGIN = f"""\
import json
import os
import time
import types

import config
import globalCommands
import globalPluginHandler
import globalVars
import gui
import ui
import versionInfo
import vision
import wx
from _remoteClient.client import RemoteClient
from linuxPlatform import clipboard, ownGui

DIRECTORY = os.environ.get("LINDA_LOOSE_ENDS_PROBE")
LINK = {LINK!r}
BROWSEABLE = {BROWSEABLE!r}


def record(kind, **fields):
	with open(os.path.join(DIRECTORY, "records.jsonl"), "a", encoding="utf-8") as out:
		out.write(json.dumps({{"kind": kind, "t": time.monotonic(), **fields}}) + "\\n")


link = types.SimpleNamespace(getURLToConnect=lambda: LINK)
remote = types.SimpleNamespace(
	leaderSession=types.SimpleNamespace(getConnectionInfo=lambda: link), followerSession=None
)


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

	def run(self, command):
		getattr(self, "do_" + command)()
		record(command)

	def do_vision(self):
		record(
			"visionState",
			enabled=config.conf["vision"]["NVDAHighlighter"]["enabled"],
			providers=sorted(vision.handler._providers),
		)

	def do_withoutDataControl(self):
		clipboard.DATA_CONTROL_MANAGERS = ()
		if clipboard._route is not None:
			clipboard._route.wire.close()
			clipboard._route = None

	def do_report(self):
		globalCommands.commands.script_reportClipboardText(None)

	def do_logFragment(self):
		globalCommands.commands.script_log_markStartThenCopy(None)

	def do_version(self):
		gui.MainFrame._copyVersionToClipboard(None)
		record("versionText", text=f"{{versionInfo.version}} ({{versionInfo.version_detailed}})")

	def do_browseable(self):
		ui._copyBrowseableMessageToClipboard(BROWSEABLE)

	def do_link(self):
		RemoteClient.copyLink(remote)
"""


class Probe:
	"""The plugin's command files and records, for the Linda the desktop session started with it."""

	def __init__(self, directory: Path) -> None:
		self.directory = directory / "probe"
		(self.directory / "commands").mkdir(parents=True)
		(self.directory / "records.jsonl").touch()
		hooks = directory / "hooks"
		(hooks / "globalPlugins").mkdir(parents=True)
		(hooks / "globalPlugins" / "looseEndsProbe.py").write_text(PLUGIN, encoding="utf-8")
		(hooks / "synthDrivers").symlink_to(harness.HERE / "synthDrivers")
		self.env = {"LINDA_TEST_HOOKS_DIR": str(hooks), "LINDA_LOOSE_ENDS_PROBE": str(self.directory)}
		self._count = 0

	def records(self, kind: str, since: float = 0) -> list[dict]:
		text = (self.directory / "records.jsonl").read_text(encoding="utf-8")
		records = [json.loads(line) for line in text.splitlines() if line.strip()]
		return [r for r in records if r["kind"] == kind and r["t"] >= since]

	def waitFor(
		self, nvda: harness.Launch, kind: str, since: float = 0, timeout: float = ANSWER_BOUND
	) -> dict:
		deadline = time.monotonic() + timeout
		while True:
			exited = nvda.process.poll() is not None
			if found := self.records(kind, since):
				return found[0]
			if exited or time.monotonic() >= deadline:
				raise AssertionError(f"no {kind} record as waited for\n{nvda.describe()}")
			time.sleep(0.05)

	def ask(self, nvda: harness.Launch, command: str) -> dict:
		self._count += 1
		temporary = self.directory / f"command-{self._count:04d}"
		temporary.write_text(json.dumps([command]), encoding="utf-8")
		given = time.monotonic()
		temporary.rename(self.directory / "commands" / f"{self._count:04d}")
		return self.waitFor(nvda, command, since=given)


@pytest.fixture
def desktop(tmp_path: Path) -> Iterator[DesktopSession]:
	with DesktopSession(tmp_path / "desktop") as session:
		yield session


@pytest.fixture
def socketDir() -> Iterator[Path]:
	# The pytest directory's path is too long for a socket's.
	directory = Path(tempfile.mkdtemp(prefix="keyd", dir="/tmp"))
	try:
		yield directory
	finally:
		shutil.rmtree(directory, ignore_errors=True)


def startLinda(
	desktop: DesktopSession, tmp_path: Path, probe: Probe, socketDir: Path, env: dict[str, str | None]
) -> harness.Launch:
	"""Linda with Visual Highlight on in its settings, beside the GTK fixture, with no linda-keyd: the
	plugin's commands stand for the keys."""
	app = desktop.startApp(desktop.fixture(GTK[0]), title=GTK[1])
	assert app.stdout.readline() == "ready\n"
	desktopSession.activate(GTK[1])
	config = tmp_path / "nvda.ini"
	config.write_text(SESSION_CONFIG.read_text(encoding="utf-8") + HIGHLIGHT_ON, encoding="utf-8")
	nvda = desktop.startNVDA(
		config, env={**probe.env, "LINDA_TEST_KEYD_SOCKET": str(socketDir / "socket"), **env}
	)
	desktop.expect(*AT_START, since=0, timeout=STARTUP_BOUND)
	probe.waitFor(nvda, "ready")
	return nvda


def say(desktop: DesktopSession, probe: Probe, command: str) -> list[str]:
	"""What Linda says for one of the plugin's commands."""
	nvda = desktop.nvda
	mark = desktop.mark()
	probe.ask(nvda, command)
	time.sleep(SETTLE)
	return desktop.spokenSince(mark)


def visualHighlightStartsClean(
	desktop: DesktopSession, tmp_path: Path, socketDir: Path, env: dict[str, str | None]
) -> None:
	probe = Probe(tmp_path)
	nvda = startLinda(desktop, tmp_path, probe, socketDir, env)
	time.sleep(RUN_ON)
	probe.ask(nvda, "vision")
	state = probe.records("visionState")[0]
	assert (state["enabled"], state["providers"]) == (True, []), state
	desktop.close()
	assert nvda.process.returncode == 0, nvda.describe()
	nvda.assertClean(logNames=("nvda.log",))
	log = nvda.text("nvda.log")
	assert COULD_NOT_HIGHLIGHT not in log
	assert log.count(NOT_STARTED) == 1, log[-6000:]


@pytest.mark.rigSession("x11")
def test_visualHighlightStartsCleanInAForcedWaylandSession(
	desktop: DesktopSession, tmp_path: Path, socketDir: Path
):
	visualHighlightStartsClean(desktop, tmp_path, socketDir, FORCED_WAYLAND)


@pytest.mark.rigSession("wayland")
def test_visualHighlightStartsCleanUnderTheCompositor(
	desktop: DesktopSession, tmp_path: Path, socketDir: Path
):
	visualHighlightStartsClean(desktop, tmp_path, socketDir, {"XKB_DEFAULT_LAYOUT": "pl"})


def wlPaste() -> str:
	done = subprocess.run(["wl-paste", "--no-newline"], capture_output=True, timeout=5, check=False)
	assert done.returncode == 0, done
	return done.stdout.decode("utf-8")


def waitForTheFixture() -> None:
	"""Wait until the fixture has the keyboard back from a window wl-paste or Linda mapped for a moment,
	as each does under Mutter."""
	deadline = time.monotonic() + 5
	while GTK[1] not in desktopSession.activeTopLevelNames():
		assert time.monotonic() < deadline, desktopSession.activeTopLevelNames()
		time.sleep(0.05)
	time.sleep(0.3)


@pytest.mark.rigSession("wayland")
def test_eachPlaceSaysItCopiedWhenItDidUnderTheCompositor(
	desktop: DesktopSession, tmp_path: Path, socketDir: Path
):
	probe = Probe(tmp_path)
	nvda = startLinda(desktop, tmp_path, probe, socketDir, {"XKB_DEFAULT_LAYOUT": "pl"})
	assert probe.records("ready")[0]["displayKind"] == "wayland"
	assert say(desktop, probe, "logFragment") == [FRAGMENT_MARKED]
	expected = {
		"logFragment": [FRAGMENT_COPIED],
		"version": [COPIED],
		"browseable": [COPIED],
		"link": [COPIED_LINK],
	}
	for name in COPIERS:
		assert say(desktop, probe, name) == expected[name], name
		pasted = wlPaste()
		waitForTheFixture()
		if name == "logFragment":
			assert pasted and FRAGMENT_MARKED in pasted, pasted
		elif name == "version":
			assert pasted == probe.records("versionText")[-1]["text"]
		else:
			assert pasted == {"browseable": BROWSEABLE, "link": LINK}[name]
	# What NVDA+c reads back is in words, which a link's punctuation is not.
	assert say(desktop, probe, "browseable") == [COPIED]
	waitForTheFixture()
	assert say(desktop, probe, "report") == [BROWSEABLE]
	desktop.close()
	assert nvda.process.returncode == 0, nvda.describe()
	nvda.assertClean(logNames=("nvda.log",))


@pytest.mark.rigSession("sway")
def test_eachPlaceSaysTheRefusalAloneUnderSway(desktop: DesktopSession, tmp_path: Path, socketDir: Path):
	probe = Probe(tmp_path)
	nvda = startLinda(desktop, tmp_path, probe, socketDir, {"XKB_DEFAULT_LAYOUT": "pl"})
	pid = probe.records("ready")[0]["pid"]
	probe.ask(nvda, "withoutDataControl")
	subprocess.run(["swaymsg", f"no_focus [pid={pid}]"], capture_output=True, check=True, timeout=5)
	assert say(desktop, probe, "logFragment") == [FRAGMENT_MARKED]
	for name in COPIERS:
		assert say(desktop, probe, name) == [CANNOT_COPY], name
	assert say(desktop, probe, "report") == [CANNOT_READ]
	assert GTK[1] in desktopSession.activeTopLevelNames()
	desktop.close()
	assert nvda.process.returncode == 0, nvda.describe()
	nvda.assertClean(logNames=("nvda.log",))
