"""What NVDA's own files do in a session that has no pointer and no Screen Curtain, as a Wayland session:
the pointer's, mouse tracking's and the curtain's commands keep their keys and say why nothing happens,
so that numpad slash types nothing; browse mode says it cannot click where an element's action fails;
and a curtain switched on in the settings is not enabled, logs no error, opens no alert and is said
once. An X session keeps every one of them as before.

The X rig forces the session through LINDA_SESSION_TYPE, with keydStandIn for the helper; under the
compositors Linda runs natively in the session the rig gives, its keys put out on the session's
keyboard. tests/linux/wayland-gate runs the compositor half:

nix develop . -c tests/linux/rig python3 -m pytest -q tests/linux/test_waylandNvdaHooks.py
nix develop . -c tests/linux/wayland-gate SCRATCH -k NvdaHooks
"""

import shutil
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

import harness
import keydStandIn
import pytest
import test_browseKeys as browseKeys
import webSession
from desktopSession import DesktopSession
from keydStandIn import NullSink, StandIn
from test_keys import STARTUP_TIMEOUT, configText

WAYLAND = "Not available in this Wayland session."
CLICK = "Linda cannot click here"
CURTAIN = "Screen Curtain is not available in this Wayland session. The screen can be seen."
#: What NVDA says when it tried to enable a curtain and could not, as the alert's title.
COULD_NOT = "Could not enable screen curtain"
#: The global commands of the pointer and of mouse tracking.
POINTER_COMMANDS = sorted(
	[
		"leftMouseClick",
		"rightMouseClick",
		"toggleLeftMouseButton",
		"toggleRightMouseButton",
		"mouseScrollUp",
		"mouseScrollDown",
		"mouseScrollLeft",
		"mouseScrollRight",
		"moveMouseToNavigatorObject",
		"moveNavigatorObjectToMouse",
		"toggleMouseTracking",
		"toggleMouseAudioCoordinates",
		"toggleMouseTextResolution",
	],
)
CURTAIN_COMMANDS = ["reportScreenCurtainState", "toggleScreenCurtain"]
STALE = (
	"Left click",
	"Left mouse button",
	"Move navigator object to mouse",
	"Temporary Screen curtain",
	"Screen curtain",
)
#: evdev codes of the numpad's slash and asterisk, which keydStandIn has no names for.
KP_DIVIDE, KP_MULTIPLY = 98, 55
#: Seconds apart that two presses are not one repeated press to NVDA.
REPEAT_GAP = 0.7

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
SESSIONS = {"x11": {**UNSET}, "wayland": {**UNSET, "LINDA_SESSION_TYPE": "wayland"}}

TABLE = f"""
import languageHandler

languageHandler.setLanguage("en")
import NVDAHelper  # noqa: F401
import globalCommands
import platformFeatures
import ui
from linuxPlatform import displayServer

names = {[*POINTER_COMMANDS, *CURTAIN_COMMANDS]!r}
cls = globalCommands.GlobalCommands
unavailable = platformFeatures._unavailableScript("x").__code__
result = {{
	"kind": displayServer.kind(),
	"replaced": sorted(n for n in names if getattr(cls, f"script_{{n}}").__code__ is unavailable),
	"described": sorted(n for n in names if getattr(cls, f"script_{{n}}").__doc__),
	"bound": {{
		gesture: script.__name__
		for gesture, script in globalCommands.commands._gestureMap.items()
		if script.__name__[len("script_") :] in names
	}},
}}
if result["kind"] != "x11":
	spoken = []
	ui.message = spoken.append
	for name in names:
		getattr(globalCommands.commands, f"script_{{name}}")(None)
	result["spoken"] = spoken
"""


@pytest.mark.rigSession("x11")
def test_aWaylandSessionKeepsThePointersKeysAndSaysWhy(tmp_path: Path):
	inX = harness.runChild(tmp_path, TABLE, env=SESSIONS["x11"])
	inX.assertClean()
	done = harness.runChild(tmp_path, TABLE, env=SESSIONS["wayland"])
	done.assertClean()
	assert (inX.value["kind"], done.value["kind"]) == ("x11", "wayland")
	# An X session replaces none of them.
	assert inX.value["replaced"] == []
	assert inX.value["described"] == sorted([*POINTER_COMMANDS, *CURTAIN_COMMANDS])
	assert inX.value["bound"]["kb:numpaddivide"] == "script_leftMouseClick"
	assert inX.value["bound"]["kb:numpadmultiply+nvda"] == "script_moveNavigatorObjectToMouse"
	assert inX.value["bound"]["kb:control+escape+nvda"] == "script_toggleScreenCurtain"
	# A Wayland session replaces each, keeps every key an X session has for them, and lists none.
	assert done.value["replaced"] == sorted([*POINTER_COMMANDS, *CURTAIN_COMMANDS])
	assert done.value["described"] == []
	assert done.value["bound"] == inX.value["bound"]
	# The navigator to the mouse included, which before said its own name and then the stale object.
	assert done.value["spoken"] == [WAYLAND] * len(POINTER_COMMANDS + CURTAIN_COMMANDS)


CLICKING = """
import os

import languageHandler

languageHandler.setLanguage("en")
import NVDAHelper  # noqa: F401
import browseMode
import mouseHandler
import ui
import winUser
from locationHelper import RectLTWH

spoken = []
ui.message = spoken.append
clicks = []
actions = []
if os.environ.get("LINDA_SESSION_TYPE") != "wayland":
	# The rig's own pointer is left where it is.
	winUser.getCursorPos = lambda: (1, 2)
	winUser.setCursorPos = lambda x, y: clicks.append(("move", x, y))
	mouseHandler.doPrimaryClick = lambda *a, **k: clicks.append(("click",))


class Element:
	def __init__(self, name, parent, location, acts=False):
		self.name, self.parent, self.location, self.acts = name, parent, location, acts
		self.hasIrrelevantLocation = location is None

	def doAction(self):
		actions.append(self.name)
		if not self.acts:
			raise NotImplementedError


root = Element("document", None, RectLTWH(0, 0, 400, 300))
document = type("Document", (), {"rootNVDAObject": root})()
paragraph = Element("paragraph", root, RectLTWH(10, 20, 40, 10))
cases = {
	"fails": paragraph,
	"inAnElementWithNoSize": Element("span", paragraph, RectLTWH(10, 20, 0, 0)),
	"acts": Element("button", root, RectLTWH(10, 40, 40, 10), acts=True),
	# As every element is under a compositor, whose programs give no place on the screen.
	"withNoLocationUpToTheRoot": Element("heading", Element("section", root, None), None),
	"inALinkWithNoLocation": Element("span", Element("link", root, None, acts=True), None),
	"theRootItself": root,
}
result = {}
for case, element in cases.items():
	del spoken[:], clicks[:], actions[:]
	browseMode.BrowseModeTreeInterceptor._activateNVDAObject(document, element)
	result[case] = {"spoken": spoken[:], "clicks": clicks[:], "actions": actions[:]}
"""


@pytest.mark.rigSession("x11")
def test_anElementWhoseActionFailsSaysTheClickLineInAWaylandSession(tmp_path: Path):
	inX = harness.runChild(tmp_path, CLICKING, env=SESSIONS["x11"])
	inX.assertClean()
	done = harness.runChild(tmp_path, CLICKING, env=SESSIONS["wayland"])
	# Nothing asked the pointer, which in a Wayland session would raise NoLinuxImplementation.
	done.assertClean()
	# JSON gives each a list.
	clicked = [["move", 30, 25], ["click"], ["move", 1, 2]]
	assert inX.value == {
		"fails": {"spoken": [], "clicks": clicked, "actions": ["paragraph"]},
		"inAnElementWithNoSize": {"spoken": [], "clicks": clicked, "actions": ["span", "paragraph"]},
		"acts": {"spoken": [], "clicks": [], "actions": ["button"]},
		"withNoLocationUpToTheRoot": {"spoken": [], "clicks": [], "actions": ["heading", "section"]},
		"inALinkWithNoLocation": {"spoken": [], "clicks": [], "actions": ["span", "link"]},
		"theRootItself": {"spoken": [], "clicks": [], "actions": []},
	}
	assert done.value == {
		"fails": {"spoken": [CLICK], "clicks": [], "actions": ["paragraph"]},
		"inAnElementWithNoSize": {"spoken": [CLICK], "clicks": [], "actions": ["span", "paragraph"]},
		"acts": {"spoken": [], "clicks": [], "actions": ["button"]},
		"withNoLocationUpToTheRoot": {"spoken": [CLICK], "clicks": [], "actions": ["heading", "section"]},
		"inALinkWithNoLocation": {"spoken": [], "clicks": [], "actions": ["span", "link"]},
		"theRootItself": {"spoken": [], "clicks": [], "actions": []},
	}


CURTAIN_AT_START = """
import languageHandler

languageHandler.setLanguage("en")
import NVDAHelper  # noqa: F401
import config
import core
import ui
from NVDAState import _TrackNVDAInitialization
from screenCurtain import _screenCurtain

from linuxPlatform import gammaCurtain

spoken = []
ui.message = lambda text, **kwargs: spoken.append(text)
enables = []
gammaCurtain.enable = lambda: enables.append(1)
gammaCurtain.isActive = lambda: bool(enables)
gammaCurtain.disable = lambda: enables.clear()
alerts = []
_screenCurtain.ScreenCurtain._postInitialisationActivationFailureMessage = lambda self: alerts.append(1)
config.conf["screenCurtain"]["enabled"] = True
config.conf["screenCurtain"]["playToggleSounds"] = False
curtain = _screenCurtain.ScreenCurtain()
result = {"duringStart": spoken[:]}
core.postNvdaStartup.notify()
result["atStart"] = spoken[:]
core.postNvdaStartup.notify()
result["afterAnotherNotify"] = spoken[:]
result.update(enables=len(enables), enabled=curtain.enabled, setting=config.conf["screenCurtain"]["enabled"])
curtain.disable(persist=False)
del spoken[:]
# Made again once NVDA has started, as screenCurtain.initialize may be.
_TrackNVDAInitialization.markInitializationComplete()
_screenCurtain.ScreenCurtain()
result.update(madeAfterStart=spoken[:], alerts=len(alerts))
"""


@pytest.mark.rigSession("x11")
def test_aCurtainOnInTheSettingsIsSaidOnceAndNotEnabledInAWaylandSession(tmp_path: Path):
	inX = harness.runChild(tmp_path, CURTAIN_AT_START, env=SESSIONS["x11"])
	inX.assertClean()
	assert inX.value == {
		"duringStart": [],
		"atStart": [],
		"afterAnotherNotify": [],
		"enables": 1,
		"enabled": True,
		"setting": True,
		"madeAfterStart": [],
		"alerts": 0,
	}
	done = harness.runChild(tmp_path, CURTAIN_AT_START, env=SESSIONS["wayland"])
	done.assertClean()
	assert done.value == {
		"duringStart": [],
		"atStart": [CURTAIN],
		"afterAnotherNotify": [CURTAIN],
		"enables": 0,
		"enabled": False,
		# Kept, for a session where the curtain works.
		"setting": True,
		"madeAfterStart": [CURTAIN],
		"alerts": 0,
	}


# Linda itself.


def curtainConfig(directory: Path, text: str) -> Path:
	"""An nvda.ini of text with the Screen Curtain switched on, as a person who relies on it has it."""
	path = directory / "nvda.ini"
	path.write_text(
		text + "\n[screenCurtain]\n\tenabled = True\n\twarnOnLoad = False\n\tplayToggleSounds = False\n",
		encoding="utf-8",
	)
	return path


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


def hooks(tmp_path: Path) -> dict[str, str]:
	directory = tmp_path / "hooks"
	(directory / "globalPlugins").mkdir(parents=True)
	(directory / "synthDrivers").symlink_to(harness.HERE / "synthDrivers")
	return {"LINDA_TEST_HOOKS_DIR": str(directory)}


def pressCodes(keyboard: StandIn, *codes: str | int) -> None:
	for key in codes:
		keyboard.down(key)
		time.sleep(0.03)
	time.sleep(0.1)
	for key in reversed(codes):
		keyboard.up(key)
		time.sleep(0.03)


def sayWhatTheSessionLacks(
	desktop: DesktopSession, socketDir: Path, tmp_path: Path, sink, env: dict[str, str | None]
) -> harness.Launch:
	"""Linda with the curtain on in its settings beside the GTK fixture, the stand-in holding the
	keyboard; the curtain's line once, and the pointer's and curtain's keys each saying why."""
	desktop.startApp(desktop.fixture("gtkFixture"), title="Fixture window")
	config = curtainConfig(tmp_path, configText("desktop", 7))
	with StandIn(socketDir / "socket", sink=sink) as keyboard:
		nvda = desktop.startNVDA(
			config, env={**hooks(tmp_path), "LINDA_TEST_KEYD_SOCKET": keyboard.path, **env}
		)
		desktop.expect(CURTAIN, since=0, timeout=STARTUP_TIMEOUT)
		keyboard.waitFor(lambda: keyboard.capturing, 10)
		time.sleep(REPEAT_GAP)
		for chord in (
			(KP_DIVIDE,),
			("Insert", KP_MULTIPLY),
			("Insert", "Control_L", "Escape"),
			("shift", KP_DIVIDE),
		):
			mark = desktop.mark()
			pressCodes(keyboard, *chord)
			desktop.expect(WAYLAND, since=mark)
			time.sleep(REPEAT_GAP)
			said = desktop.spokenSince(mark)
			assert said.count(WAYLAND) == 1, (chord, said)
			# What each command said of itself before it was replaced.
			assert not [text for text in said if text.startswith(STALE)], (chord, said)
		# None of them reached the program but the modifiers, which NVDA passes on as it does on X, and a
		# letter Linda does not keep does.
		modifiers = [keydStandIn.code("Control_L"), keydStandIn.code("shift")]
		assert [key for key in sink.passed() if key[0] not in modifiers] == []
		pressCodes(keyboard, "b")
		time.sleep(0.3)
		assert [key for key in sink.passed() if key[0] not in modifiers] == [
			(keydStandIn.code("b"), True),
			(keydStandIn.code("b"), False),
		]
		nvda.process.terminate()
		nvda.waitForExit(20)
	return nvda


def assertCurtainSaidOnceAndNothingWrong(desktop: DesktopSession, nvda: harness.Launch) -> None:
	nvda.assertClean(logNames=("nvda.log",))
	spoken = desktop.spokenSince(0)
	assert spoken.count(CURTAIN) == 1, spoken
	assert not [text for text in spoken if COULD_NOT in text], spoken
	log = nvda.text("nvda.log")
	assert "Failed to enable Screen Curtain" not in log
	assert "Enabling ScreenCurtain" not in log


@pytest.mark.rigSession("x11")
def test_lindaInAForcedWaylandSession(desktop: DesktopSession, socketDir: Path, tmp_path: Path):
	env = {"LINDA_SESSION_TYPE": "wayland", "XDG_CURRENT_DESKTOP": None, "XKB_DEFAULT_LAYOUT": "us"}
	nvda = sayWhatTheSessionLacks(desktop, socketDir, tmp_path, NullSink(), env)
	assertCurtainSaidOnceAndNothingWrong(desktop, nvda)


@pytest.mark.rigSession("wayland")
def test_lindaUnderTheCompositor(desktop: DesktopSession, socketDir: Path, tmp_path: Path):
	sink = keydStandIn.compositorSink()
	try:
		# The rig's compositors have the pl keymap, and no settings of a desktop's for Linda to read it from.
		nvda = sayWhatTheSessionLacks(desktop, socketDir, tmp_path, sink, {"XKB_DEFAULT_LAYOUT": "pl"})
	finally:
		sink.close()
	assertCurtainSaidOnceAndNothingWrong(desktop, nvda)


#: A plugin that has every heading's action fail, as a browser's may: Firefox answers DoAction on an
#: element with no action as done.
FAILING_ACTION = """
import controlTypes
import globalPluginHandler
from NVDAObjects.AtSpi import AtSpi

doAction = AtSpi.doAction


def failingOnHeadings(self, index=None):
	if self.role == controlTypes.Role.HEADING:
		raise NotImplementedError
	return doAction(self, index)


AtSpi.doAction = failingOnHeadings


class GlobalPlugin(globalPluginHandler.GlobalPlugin):
	pass
"""


def enterOnAHeading(
	desktop: DesktopSession, socketDir: Path, tmp_path: Path, sink, env: dict[str, str | None]
) -> harness.Launch:
	"""On Firefox's keys page, with a heading whose action fails, which NVDA would click at: Linda says
	it cannot, and Enter does not reach the page."""
	webSession.requireBrowseLibraries()
	webSession.startFirefox(desktop, "keys.html")
	with StandIn(socketDir / "socket", sink=sink) as keyboard:
		env = {**browseKeys.probeHooks(tmp_path), "LINDA_TEST_KEYD_SOCKET": keyboard.path, **env}
		(tmp_path / "hooks" / "globalPlugins" / "failingAction.py").write_text(FAILING_ACTION)
		nvda = desktop.startNVDA(browseKeys.browseConfig(tmp_path), env=env)
		nvda.waitForLog("Buffer rendered", browseKeys.BUFFER_BOUND)
		browseKeys.waitForCapture(nvda, "browse", timeout=browseKeys.BUFFER_BOUND)
		keyboard.waitFor(lambda: keyboard.capturing)
		time.sleep(browseKeys.SETTLE)
		page = browseKeys.KeysPage()
		for chord, expected in (("control+Home", "heading level 1 Keys page"), ("Return", CLICK)):
			mark = desktop.mark()
			keyboard.press(chord)
			browseKeys.expect(desktop, expected, since=mark)
			time.sleep(browseKeys.SETTLE)
		assert desktop.spokenSince(mark) == [CLICK]
		assert page.log() == []
		nvda.process.terminate()
		nvda.waitForExit(20)
	return nvda


@pytest.mark.rigSession("x11")
def test_enterOnAHeadingInAForcedWaylandSession(desktop: DesktopSession, socketDir: Path, tmp_path: Path):
	# The stand-in's own sink, the X display, where Firefox is.
	env = {"LINDA_SESSION_TYPE": "wayland", "XDG_CURRENT_DESKTOP": None, "XKB_DEFAULT_LAYOUT": "us"}
	nvda = enterOnAHeading(desktop, socketDir, tmp_path, None, env)
	nvda.assertClean(logNames=("nvda.log",))


@pytest.mark.rigSession("wayland")
def test_enterOnAHeadingUnderTheCompositor(desktop: DesktopSession, socketDir: Path, tmp_path: Path):
	sink = keydStandIn.compositorSink()
	try:
		nvda = enterOnAHeading(desktop, socketDir, tmp_path, sink, {"XKB_DEFAULT_LAYOUT": "pl"})
	finally:
		sink.close()
	nvda.assertClean(logNames=("nvda.log",))
