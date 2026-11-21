"""What Linda switches off in a session that says Wayland, where the X server, if there is one, is
Xwayland: the pointer, mouse tracking, the Screen Curtain and Visual Highlight. Each is unsupported, says
so when something still runs it, and opens no X connection, since on GNOME the first would start
Xwayland. An X session keeps every one of them as before.

The X rig forces the session with the variables displayServer reads, and gives Linda a display that is
a listener of the test's own, which counts every connection made to it; under the compositors the same
is run in the session as the rig gives it. tests/linux/wayland-gate runs the compositor half:

nix develop . -c tests/linux/rig env LINDA_RIG_WM=marco python3 -m pytest -q tests/linux/test_waylandFeatures.py
nix develop . -c tests/linux/wayland-gate SCRATCH -k Features
"""

import os
from collections.abc import Iterator
from pathlib import Path

import harness
import pytest
from desktopSession import DesktopSession
from test_platformFeatures import AT_START, assertExitedCleanly, readWindow, start, step, walk

LINUX = "Not available on Linux."
WAYLAND = "Not available in this Wayland session."
#: The features that platformFeatures said Linux lacks before Wayland was known.
LINUX_UNSUPPORTED = sorted(
	[
		"AUDIO_DUCKING",
		"BRAILLE_PORTS",
		"COM_REGISTRATION_FIXES",
		"DISPLAY_MODEL",
		"IACCESSIBLE",
		"INPUT_COMPOSITION",
		"INSTALLER",
		"KEEP_DISPLAY_ON",
		"MAGNIFIER",
		"NV_ACCESS_SUPPORT",
		"PORTABLE_COPY",
		"SAPI4",
		"SECURE_SCREENS",
		"SOUND_SPLIT",
		"TOUCH",
		"UI_AUTOMATION",
		"UPDATE_CHECK",
		"WINDOWS_CONSOLE",
		"WINDOWS_OCR",
	],
)
WAYLAND_UNSUPPORTED = sorted(["MOUSE_TRACKING", "POINTER", "SCREEN_CURTAIN", "VISUAL_HIGHLIGHT"])
#: A display number no rig server takes: the X rig's is 99, the curtain's tests take 71 to 73, and the
#: compositors' Xwayland 10 and 11.
FAKE_DISPLAY = ":47"
#: Child code that listens where a client of FAKE_DISPLAY connects, in both the abstract namespace
#: libxcb tries first and the file python-xlib uses, and counts each connection, closing it at once so
#: that the client fails rather than waits.
LISTEN = """
import os
import socket
import threading
import time

FAKE_X = "/tmp/.X11-unix/X47"
xConnections = []


def serve(server):
	while True:
		try:
			connection, _address = server.accept()
		except OSError:
			return
		xConnections.append(1)
		connection.close()


if os.path.exists(FAKE_X):
	os.unlink(FAKE_X)
for address in (FAKE_X, "\\0" + FAKE_X):
	server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
	server.bind(address)
	server.listen(16)
	threading.Thread(target=serve, args=(server,), daemon=True).start()


def xConnectionsSoFar():
	# A connection is queued before it is accepted.
	time.sleep(0.3)
	return len(xConnections)
"""
#: Child code that asks everything this package switches off, as NVDA would, and records whether it
#: is there and what it says, and how many X connections it took.
ASK = """
import languageHandler

languageHandler.setLanguage("en")
import NVDAHelper  # noqa: F401
import platformFeatures
import ui
from platformFeatures import Feature
from visionEnhancementProviders.NVDAHighlighter import NVDAHighlighter

from linuxPlatform import NoLinuxImplementation, displayServer, keys, pointer, visualHighlight

spoken = []
ui.message = spoken.append
# The watcher follows the pointer only while NVDA takes keys, which a child does not.
keys.isEnabled = lambda: True


def refusal(call):
	try:
		call()
	except NoLinuxImplementation as e:
		return str(e)
	return "answered"


result = {
	"kind": displayServer.kind(),
	"saysWayland": displayServer.saysWayland(),
	"unsupported": sorted(feature.name for feature in Feature if not platformFeatures.isSupported(feature)),
	"messages": {feature.name: platformFeatures.unavailableMessage(feature) for feature in Feature},
	"pointerAvailable": pointer.isAvailable(),
}
if not result["pointerAvailable"]:
	pointer.startWatching()
	result["watching"] = pointer._watcher is not None
	pointer.refreshMouseObject()
	result["refreshSaid"] = spoken[:]
	result["keyState"] = pointer.keyState(0x01)
	result["buttonsSwapped"] = pointer.buttonsSwapped()
	result["refusals"] = {
		"position": refusal(pointer.position),
		"moveTo": refusal(lambda: pointer.moveTo(1, 1)),
		"mouseEvent": refusal(lambda: pointer.mouseEvent(pointer.MOUSEEVENTF_LEFTDOWN, 0, 0)),
		"processAt": refusal(lambda: pointer.processAt(1, 1)),
	}
	from linuxPlatform.screenBitmap import ScreenBitmap

	result["refusals"]["captureImage"] = refusal(lambda: ScreenBitmap(1, 1).captureImage(0, 0, 1, 1))
result["highlightStarts"] = visualHighlight.canStart()
result["providerStarts"] = NVDAHighlighter.canStart()
from gui import settingsDialogs

result["categories"] = [panel.__name__ for panel in settingsDialogs.NVDASettingsDialog.categoryClasses]
"""
SWITCHED_OFF = {
	"watching": False,
	"refreshSaid": [WAYLAND],
	"keyState": 0,
	"buttonsSwapped": False,
	"refusals": dict.fromkeys(
		["position", "moveTo", "mouseEvent", "processAt", "captureImage"], "this is a Wayland session"
	),
	"highlightStarts": False,
	"providerStarts": False,
}


def messages(waylandFeatures: list[str]) -> dict[str, str]:
	return {
		name: WAYLAND if name in waylandFeatures else LINUX for name in [*LINUX_UNSUPPORTED, *waylandFeatures]
	}


def ask(tmp_path: Path, env: dict[str, str | None], *, listen: bool) -> harness.ChildResult:
	code = (LISTEN if listen else "") + ASK
	if listen:
		code += '\nresult["xConnections"] = xConnectionsSoFar()\nos.unlink(FAKE_X)\n'
	return harness.runChild(tmp_path, code, env=env)


def assertWaylandAnswers(done: harness.ChildResult, kind: str) -> None:
	# The refusals are the pointer's own, which the code asked for and caught.
	done.assertClean(ignoring={"noLinuxImplementation"})
	assert set(done.noLinuxImplementation) == {"the mouse pointer"}, done.noLinuxImplementation
	value = done.value
	assert value["kind"] == kind
	assert value["saysWayland"]
	assert value["unsupported"] == sorted([*LINUX_UNSUPPORTED, *WAYLAND_UNSUPPORTED])
	assert {name: value["messages"][name] for name in value["unsupported"]} == messages(WAYLAND_UNSUPPORTED)
	assert not value["pointerAvailable"]
	assert {name: value[name] for name in SWITCHED_OFF} == SWITCHED_OFF
	assert "VisionSettingsPanel" not in value["categories"], value["categories"]
	assert "PrivacyAndSecuritySettingsPanel" in value["categories"], value["categories"]


#: Variables a child gets so that only those a case names decide its session.
UNSET = dict.fromkeys(
	[
		"LINDA_SESSION_TYPE",
		"XDG_SESSION_TYPE",
		"XDG_CURRENT_DESKTOP",
		"WAYLAND_DISPLAY",
		"GAMESCOPE_WAYLAND_DISPLAY",
	],
)


@pytest.mark.rigSession("x11")
def test_anXSessionKeepsEveryFeatureAndTheListenerHearsEachConnection(tmp_path: Path):
	# The owner's session as they printed it, on the listener.
	done = ask(
		tmp_path,
		{**UNSET, "XDG_SESSION_TYPE": "x11", "XDG_CURRENT_DESKTOP": "MATE", "DISPLAY": FAKE_DISPLAY},
		listen=True,
	)
	# The listener is no X server: the highlight's probe of it fails, at debug level.
	done.assertClean()
	value = done.value
	assert value["kind"] == "x11"
	assert not value["saysWayland"]
	assert value["unsupported"] == LINUX_UNSUPPORTED
	assert {name: value["messages"][name] for name in value["unsupported"]} == messages([])
	assert value["pointerAvailable"]
	assert not value["highlightStarts"]
	assert "VisionSettingsPanel" in value["categories"]
	# The highlight's probe, asked once and cached, and nothing else of what was asked.
	assert value["xConnections"] == 1


@pytest.mark.rigSession("x11")
def test_theRigsXSessionKeepsThePointerAndTheHighlight(tmp_path: Path):
	done = ask(tmp_path, UNSET, listen=False)
	done.assertClean()
	value = done.value
	assert value["kind"] == "x11"
	assert value["unsupported"] == LINUX_UNSUPPORTED
	assert value["pointerAvailable"]
	assert value["highlightStarts"]
	assert value["providerStarts"]
	assert "VisionSettingsPanel" in value["categories"]


@pytest.mark.rigSession("x11")
def test_anXSessionWithAStrayWaylandSocketStaysX(tmp_path: Path):
	socketPath = tmp_path / "wayland-stray"
	done = harness.runChild(
		tmp_path,
		f"""
		import socket

		stray = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
		stray.bind({str(socketPath)!r})
		import platformFeatures

		from linuxPlatform import displayServer, pointer

		result = [displayServer.reason(), platformFeatures.isSupported(platformFeatures.Feature.POINTER), pointer.isAvailable()]
		""",
		env={
			**UNSET,
			"XDG_SESSION_TYPE": "x11",
			"XDG_CURRENT_DESKTOP": "MATE",
			"WAYLAND_DISPLAY": str(socketPath),
		},
	)
	done.assertClean()
	assert done.value == ["x11 by rule 2: XDG_SESSION_TYPE is x11 and DISPLAY is set", True, True]


@pytest.mark.rigSession("x11")
def test_aForcedWaylandSessionSwitchesOffAndConnectsToNothing(tmp_path: Path):
	done = ask(tmp_path, {**UNSET, "LINDA_SESSION_TYPE": "wayland", "DISPLAY": FAKE_DISPLAY}, listen=True)
	assertWaylandAnswers(done, "wayland")
	assert done.value["xConnections"] == 0


@pytest.mark.rigSession("x11")
@pytest.mark.parametrize(
	("override", "kind"),
	[(None, "wayland"), ("x11", "x11")],
	ids=["sessionTypeAlone", "kindForcedToX"],
)
def test_aSessionTypeOfWaylandWithADisplayStillRefusesTheHighlight(
	tmp_path: Path, override: str | None, kind: str
):
	done = harness.runChild(
		tmp_path,
		LISTEN
		+ """
import languageHandler

languageHandler.setLanguage("en")
import NVDAHelper  # noqa: F401

from linuxPlatform import displayServer, visualHighlight

result = {
	"kind": displayServer.kind(),
	"highlightStarts": visualHighlight.canStart(),
	"xConnections": xConnectionsSoFar(),
}
os.unlink(FAKE_X)
""",
		env={**UNSET, "XDG_SESSION_TYPE": "wayland", "LINDA_SESSION_TYPE": override, "DISPLAY": FAKE_DISPLAY},
	)
	done.assertClean()
	assert done.value == {"kind": kind, "highlightStarts": False, "xConnections": 0}


KEPT = """
import languageHandler

languageHandler.setLanguage("en")
import NVDAHelper  # noqa: F401
import platformFeatures
import ui
from baseObject import ScriptableObject
from platformFeatures import Feature
from scriptHandler import script


class Commands(ScriptableObject):
	@script(description="Clicks", gesture="kb:NVDA+f1")
	def script_click(self, gesture):
		ui.message("clicked")

	@script(description="Follows the pointer", gesture="kb:NVDA+f2")
	def script_track(self, gesture):
		ui.message("following")

	@script(description="Magnifies", gesture="kb:NVDA+f3")
	def script_zoom(self, gesture):
		ui.message("magnified")

	@script(description="Says the time", gesture="kb:NVDA+f4")
	def script_time(self, gesture):
		ui.message("noon")


platformFeatures.hideUnsupportedScripts(
	Commands,
	{"click": Feature.POINTER, "track": Feature.MOUSE_TRACKING, "zoom": Feature.MAGNIFIER},
	keepGestures={Feature.POINTER, Feature.MAGNIFIER},
)
commands = Commands()
spoken = []
ui.message = spoken.append
for name in ("click", "track", "zoom", "time"):
	getattr(commands, f"script_{name}")(None)
result = {
	"bound": {gesture: script.__name__ for gesture, script in commands._gestureMap.items()},
	"described": [name for name in ("click", "track", "zoom", "time") if getattr(Commands, f"script_{name}").__doc__],
	"spoken": spoken,
}
"""


@pytest.mark.rigSession("x11")
@pytest.mark.parametrize("session", ["x11", "wayland"])
def test_keptGesturesStayBoundAndSayWhy(tmp_path: Path, session: str):
	done = harness.runChild(tmp_path, KEPT, env={"LINDA_SESSION_TYPE": session})
	done.assertClean()
	if session == "wayland":
		assert done.value == {
			"bound": {"kb:f1+nvda": "script_click", "kb:f3+nvda": "script_zoom", "kb:f4+nvda": "script_time"},
			"described": ["time"],
			"spoken": [WAYLAND, WAYLAND, LINUX, "noon"],
		}
	else:
		assert done.value == {
			"bound": {
				"kb:f1+nvda": "script_click",
				"kb:f2+nvda": "script_track",
				"kb:f3+nvda": "script_zoom",
				"kb:f4+nvda": "script_time",
			},
			"described": ["click", "track", "time"],
			"spoken": ["clicked", "following", LINUX, "noon"],
		}


@pytest.fixture
def desktop(tmp_path: Path) -> Iterator[DesktopSession]:
	with DesktopSession(tmp_path / "desktop") as session:
		yield session


#: The settings' categories with Visual Highlight's gone.
CATEGORIES = [
	"General 1 of 15",
	"Speech 2 of 15",
	"Braille 3 of 15",
	"Audio 4 of 15",
	"Privacy and Security 5 of 15",
	"Keyboard 6 of 15",
	"Mouse 7 of 15",
	"Review Cursor 8 of 15",
	"Object Presentation 9 of 15",
	"Browse Mode 10 of 15",
	"Document Formatting 11 of 15",
	"Document Navigation 12 of 15",
	"Remote Access 13 of 15",
	"NVDA Add-on Store 14 of 15",
	"Advanced 15 of 15",
]


@pytest.mark.rigSession("x11")
def test_theCurtainAndTheHighlightAreLeftOutOfTheSettings(desktop: DesktopSession):
	# The session says Wayland, while its keys and windows stay on the rig's X display.
	nvda = start(desktop, env={"LINDA_SESSION_TYPE": "x11", "XDG_SESSION_TYPE": "wayland"})
	step(desktop, "Insert+n", "Preferences sub Menu P 1 of 9")
	step(desktop, "Return", "Settings... S 1 of 4")
	step(
		desktop,
		"Return",
		"Linda Settings: General (normal configuration) dialog",
		"Categories: list",
		CATEGORIES[0],
	)
	walk(desktop, "Down", CATEGORIES, wraps=False)
	step(desktop, "Home", CATEGORIES[0])
	for category in CATEGORIES[1:5]:
		step(desktop, "Down", category)
	privacy = readWindow(desktop, "separator")
	assert "General grouping" in privacy, privacy
	assert not [text for text in privacy if "Curtain" in text], privacy
	step(desktop, "Escape", *AT_START)
	desktop.close()
	assertExitedCleanly(nvda)


def xwaylandRunning() -> bool:
	for entry in Path("/proc").iterdir():
		try:
			if entry.name.isdigit() and (entry / "comm").read_text().strip() == "Xwayland":
				return True
		except OSError:
			continue
	return False


@pytest.mark.rigSession("wayland")
def test_theCompositorsSessionSwitchesOffAndStartsNoXwayland(tmp_path: Path):
	assert harness.rigHasXwayland() == ("DISPLAY" in os.environ)
	before = xwaylandRunning()
	done = ask(tmp_path, {}, listen=False)
	assertWaylandAnswers(done, "wayland")
	if harness.rigHasXwayland() and not before:
		assert not xwaylandRunning()


@pytest.mark.rigSession("wayland")
def test_theCompositorsSessionConnectsToNoDisplayItIsGiven(tmp_path: Path):
	done = ask(tmp_path, {"DISPLAY": FAKE_DISPLAY}, listen=True)
	assertWaylandAnswers(done, "wayland")
	assert done.value["xConnections"] == 0
