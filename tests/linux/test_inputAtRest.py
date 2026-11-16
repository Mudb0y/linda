"""Keyboard, mouse, touch and braille initialise, idle and terminate on Linux, touching no device.

There is no keyboard capture and no braille display yet. What is held here is that each of these
starts and stops cleanly, that keyboard gestures keep working as values, that whatever needs the
live keyboard raises NoLinuxImplementation, and that synthetic key events handed to the seam a
capture layer will use reach a script through inputCore. Run under the rig:

	nix develop . -c tests/linux/rig python3 -m pytest -p no:cacheprovider tests/linux/test_inputAtRest.py
"""

import textwrap
from pathlib import Path

import harness
import pytest

#: Enters NVDA's import cycle where core.main does, and watches for anything that reaches past the
#: process: device and system files, sockets, native libraries and child processes. The one native
#: library expected is the dev shell's ICU, which textUtils loads at import for segmentation, as
#: upstream loads Windows' ICU.
PRELUDE = """
import os
import sys

_touched = []
_icuDir = os.environ.get("BASEDREADER_ICU_LIB_DIR")


def _isIcu(args):
	return bool(_icuDir) and isinstance(args[0], str) and os.path.dirname(args[0]) == _icuDir


def _watch(event, args):
	if event == "ctypes.dlopen" and _isIcu(args):
		return
	if event in ("socket.connect", "ctypes.dlopen", "subprocess.Popen", "os.system"):
		_touched.append(f"{event} {args!r}")
	elif event == "open" and isinstance(args[0], str) and args[0].startswith(("/dev/", "/sys/", "/run/")):
		_touched.append(f"{event} {args[0]}")


sys.addaudithook(_watch)

import NVDAHelper  # noqa: F401
import languageHandler

# core.main sets the language before inputCore, which reads it to find the locale's gesture map.
languageHandler.setLanguage("en")
"""

EPILOGUE = """
result = {"value": result, "touched": _touched}
"""

#: A placeholder focus and desktop, as upstream's unit tests use, and the watchdog observer that
#: keyboardHandler.initialize receives from core.main.
OBJECTS = """
import globalVars
import watchdog
from NVDAObjects import NVDAObject


class Placeholder(NVDAObject):
	processID = None
	windowThreadID = 0

	def _isEqual(self, other):
		return False

	@staticmethod
	def objectFromPoint(x, y):
		return None


Observer = watchdog.WatchdogObserver


globalVars.focusObject = globalVars.desktopObject = Placeholder()
globalVars.focusAncestors = []

import speech

# Speech's state and the say all handler, which executing a gesture consults, without loading a
# synthesizer, which is workstream C's.
speech.speechInitialize()
speech.sayAllInitialize(speech.speak, speech.speakObject, speech.getTextInfoSpeech, speech.SpeakTextInfoState)
"""


def run(tmpPath: Path, *pieces: str, objects: bool = False) -> tuple[harness.ChildResult, object]:
	"""Run the pieces after the prelude; return the child's result and the value they left in result."""
	code = PRELUDE + (OBJECTS if objects else "") + "".join(textwrap.dedent(p) for p in pieces) + EPILOGUE
	done = harness.runChild(tmpPath, code)
	assert done.error is None, done.error
	assert not done.value["touched"], f"reached past the process: {done.value['touched']}"
	return done, done.value["value"]


def test_keyboardInitializesAndTerminates(tmp_path: Path):
	done, value = run(
		tmp_path,
		"""
		import inputCore
		import keyboardHandler
		from linuxPlatform import inputHook

		inputCore.initialize()
		keyboardHandler.initialize(Observer())
		running = inputHook.isRunning()
		state = {
			name: type(getattr(keyboardHandler, name)).__name__
			for name in (
				"keyCounter",
				"currentModifiers",
				"trappedKeys",
				"passKeyThroughCount",
				"lastNVDAModifier",
				"stickyNVDAModifier",
				"stickyNVDAModifierLocked",
				"ignoreInjected",
			)
		}
		keyboardHandler.terminate()
		inputCore.terminate()
		result = {"running": running, "after": inputHook.isRunning(), "state": state}
		""",
		objects=True,
	)
	done.assertClean()
	assert value == {
		"running": True,
		"after": False,
		"state": {
			"keyCounter": "int",
			"currentModifiers": "set",
			"trappedKeys": "set",
			"passKeyThroughCount": "int",
			"lastNVDAModifier": "NoneType",
			"stickyNVDAModifier": "NoneType",
			"stickyNVDAModifierLocked": "bool",
			"ignoreInjected": "bool",
		},
	}


def test_inputHookTerminateWithoutInitializeRaises(tmp_path: Path):
	done, value = run(
		tmp_path,
		"""
		import winInputHook

		winInputHook.initialize()
		winInputHook.initialize()
		winInputHook.terminate()
		winInputHook.terminate()
		try:
			winInputHook.terminate()
		except RuntimeError as e:
			result = str(e)
		""",
	)
	done.assertClean()
	assert value == "winInputHook not running"


def test_deliveryFollowsTheHookState(tmp_path: Path):
	done, value = run(
		tmp_path,
		"""
		import winInputHook
		from linuxPlatform import inputHook

		KEY = (0x51, 0x10, False, False)
		MOVE = (0x0200, 10, 20, False)


		def attempt(deliver, *args):
			try:
				return deliver(*args)
			except RuntimeError as e:
				return str(e)


		result = {"stopped": [attempt(inputHook.deliverKey, *KEY, False), attempt(inputHook.deliverMouse, *MOVE)]}
		winInputHook.initialize()
		# Until a handler registers, the hooks pass every event on, as Windows' do.
		result["noCallbacks"] = [inputHook.deliverKey(*KEY, False), inputHook.deliverKey(*KEY, True), inputHook.deliverMouse(*MOVE)]
		calls = []


		def callback(name, answer):
			def record(*args):
				calls.append([name, *args])
				return answer

			return record


		winInputHook.setCallbacks(keyUp=callback("up", 1), keyDown=callback("down", 0), mouse=callback("mouse", []))
		result["answers"] = [inputHook.deliverKey(*KEY, False), inputHook.deliverKey(*KEY, True), inputHook.deliverMouse(*MOVE)]
		result["calls"] = calls
		winInputHook.terminate()
		result["terminated"] = attempt(inputHook.deliverKey, *KEY, False)
		""",
	)
	done.assertClean()
	assert value == {
		"stopped": ["winInputHook not running"] * 2,
		"noCallbacks": [True, True, True],
		"answers": [False, True, False],
		"calls": [
			["down", 0x51, 0x10, False, False],
			["up", 0x51, 0x10, False, False],
			["mouse", 0x0200, 10, 20, False],
		],
		"terminated": "winInputHook not running",
	}


#: Gesture names as gesture maps and scripts give them: (name, main key, modifier names).
GESTURE_NAMES = (
	("NVDA+t", "t", ["NVDA"]),
	("control+alt+n", "n", ["alt", "control"]),
	("shift+numpad2", "numpad2", ["shift"]),
	("NVDA+shift+upArrow", "upArrow", ["NVDA", "shift"]),
	("NVDA+control+f", "f", ["NVDA", "control"]),
	("alt+f4", "f4", ["alt"]),
	("windows+d", "d", ["windows"]),
	("leftControl+home", "home", ["control"]),
	("numpadInsert+downArrow", "downArrow", ["NVDA"]),
	("escape", "escape", []),
	("f12", "f12", []),
)


@pytest.mark.parametrize("layout", ("desktop", "laptop"))
def test_gestureNamesRoundTrip(tmp_path: Path, layout: str):
	done, value = run(
		tmp_path,
		f"""
		import config
		import inputCore
		from keyboardHandler import KeyboardInputGesture
		from keyLabels import localizedKeyLabels

		config.conf["keyboard"]["keyboardLayout"] = {layout!r}
		result = {{"labels": localizedKeyLabels}}
		for name in {[name for name, _main, _mods in GESTURE_NAMES]!r}:
			gesture = KeyboardInputGesture.fromName(name)
			again = KeyboardInputGesture.fromName("+".join(gesture._keyNamesInDisplayOrder))
			result[name] = {{
				"identifiers": list(gesture.identifiers),
				"normalized": [inputCore.normalizeGestureIdentifier(i) for i in gesture.identifiers],
				"again": [inputCore.normalizeGestureIdentifier(i) for i in again.identifiers],
				"main": gesture.mainKeyName,
				"modifiers": sorted(gesture.modifierNames),
				"display": gesture.displayName,
				"layout": gesture.layout,
				"described": [
					list(KeyboardInputGesture.getDisplayTextForIdentifier(inputCore.normalizeGestureIdentifier(i)))
					for i in gesture.identifiers
				],
			}}
		""",
	)
	done.assertClean()
	labels = value["labels"]

	def label(key: str) -> str:
		return "NVDA" if key == "NVDA" else labels.get(key.lower(), key)

	for name, main, modifiers in GESTURE_NAMES:
		got = value[name]
		layoutId, allLayoutsId = got["identifiers"]
		assert layoutId.startswith(f"kb({layout}):") and allLayoutsId.startswith("kb:"), got
		keyNames = "+".join(modifiers + [main])
		assert got["normalized"] == [
			inputCore_normalize(f"kb({layout}):{keyNames}"),
			inputCore_normalize(f"kb:{keyNames}"),
		]
		assert got["again"] == got["normalized"]
		assert got["main"] == main
		assert got["modifiers"] == sorted(modifiers)
		assert got["layout"] == layout
		displayParts = got["display"].split("+")
		assert displayParts[-1] == label(main)
		assert sorted(displayParts[:-1]) == sorted(label(m) for m in modifiers)
		(layoutSource, layoutText), (allSource, allText) = got["described"]
		assert layoutSource == f"{layout} keyboard"
		assert allSource == "keyboard, all layouts"
		assert layoutText == allText
		described = layoutText.split("+")
		if "NVDA" in modifiers:
			assert described[0] == "NVDA"
		assert described[-1] == label(main)
		assert sorted(described) == sorted(label(k) for k in modifiers + [main])


def inputCore_normalize(identifier: str) -> str:
	"""inputCore.normalizeGestureIdentifier, restated so the expectation does not come from the code under test."""
	prefix, keys = identifier.lower().split(":", 1)
	return f"{prefix}:{'+'.join(sorted(keys.split('+')))}"


def test_gestureModifierLogic(tmp_path: Path):
	done, value = run(
		tmp_path,
		"""
		import inputCore
		import keyboardHandler
		import winUser
		from keyboardHandler import KeyboardInputGesture

		inputCore.initialize()
		nvda = KeyboardInputGesture.fromName("NVDA")
		shift = KeyboardInputGesture.fromName("shift")
		leftControlA = KeyboardInputGesture({(winUser.VK_LCONTROL, False)}, ord("A"), 0x1E, False)
		shiftA = KeyboardInputGesture.fromName("shift+a")
		upperT = KeyboardInputGesture.fromName("NVDA+T")
		space = KeyboardInputGesture.fromName("space")
		capsLock = KeyboardInputGesture.fromName("capsLock")
		result = {
			"layouts": sorted(KeyboardInputGesture.LAYOUTS),
			"nvda": [nvda.isModifier, nvda.isNVDAModifierKey, nvda.mainKeyName],
			"shift": [shift.isModifier, shift.isNVDAModifierKey, shift.isCharacter],
			"generalized": sorted(leftControlA.generalizedModifiers),
			"leftControlA": [leftControlA.isCharacter, leftControlA.shouldReportAsCommand, leftControlA.identifiers[1]],
			"shiftA": [shiftA.isCharacter, shiftA.shouldReportAsCommand],
			"upperT": [inputCore.normalizeGestureIdentifier(i) for i in upperT.identifiers],
			"space": [space.isCharacter, space.shouldReportAsCommand],
			"capsLock": [capsLock.isNVDAModifierKey, capsLock.shouldReportAsCommand],
			"speechEffect": [shiftA.speechEffectWhenExecuted, shift.speechEffectWhenExecuted],
			"altAlone": keyboardHandler.canModifiersPerformAction({(winUser.VK_MENU, False)}),
			"controlShift": keyboardHandler.canModifiersPerformAction(
				{(winUser.VK_CONTROL, False), (winUser.VK_SHIFT, False)}
			),
			"controlAlone": keyboardHandler.canModifiersPerformAction({(winUser.VK_CONTROL, False)}),
		}
		inputCore.terminate()
		""",
		objects=True,
	)
	done.assertClean()
	assert value == {
		"layouts": ["desktop", "laptop"],
		"nvda": [True, True, "NVDA"],
		"shift": [True, False, False],
		"generalized": [[17, False]],
		"leftControlA": [False, True, "kb:control+a"],
		"shiftA": [True, False],
		# An upper case letter in a key name is typed with shift, as VkKeyScanEx answers on Windows.
		"upperT": ["kb(desktop):nvda+shift+t", "kb:nvda+shift+t"],
		"space": [True, False],
		# Caps lock is an NVDA key only when the configuration says so, which by default it does not.
		"capsLock": [False, False],
		"speechEffect": ["cancel", "pause"],
		"altAlone": True,
		"controlShift": True,
		"controlAlone": False,
	}


@pytest.mark.parametrize(
	("call", "missing"),
	(
		# Which key types a character other than an ASCII letter depends on the layout.
		("KeyboardInputGesture.fromName('NVDA+.')", "keyboard layout lookup"),
		("KeyboardInputGesture.fromName('NVDA+1')", "keyboard layout lookup"),
		("KeyboardInputGesture.fromName('NVDA+\u00e9')", "keyboard layout lookup"),
		# The numpad operators read whether num lock is on.
		("KeyboardInputGesture.fromName('numpadPlus')", "keyboard state"),
		# A key with no name of its own is named after what it types.
		("KeyboardInputGesture(set(), 0xBA, 0x27, False).mainKeyName", "keyboard layout lookup"),
		("KeyboardInputGesture.fromName('a').character", "user32.GetKeyboardLayout"),
		("KeyboardInputGesture.fromName('control+a').send()", "user32.GetKeyState"),
		("keyboardHandler.injectRawKeyboardInput(True, 0x1E, False)", "user32.GetKeyboardLayout"),
	),
)
def test_whatNeedsTheLiveKeyboardRaises(tmp_path: Path, call: str, missing: str):
	done, value = run(
		tmp_path,
		f"""
		import keyboardHandler
		from keyboardHandler import KeyboardInputGesture
		from linuxPlatform import NoLinuxImplementation

		try:
			{call}
		except NoLinuxImplementation as e:
			result = e.what
		""",
		objects=True,
	)
	assert value == missing
	assert done.noLinuxImplementation == [missing]
	assert not done.logErrors


#: Windows virtual-key and scan codes of the keys the seam test presses.
INSERT = (0x2D, 0x52, True)
T = (0x54, 0x14, False)
Q = (0x51, 0x10, False)

SEAM = f"""
import globalPluginHandler
import inputCore
import keyboardHandler
import queueHandler
import scriptHandler
from linuxPlatform import inputHook

INSERT, T, Q = {INSERT!r}, {T!r}, {Q!r}
ran = []


class Plugin(globalPluginHandler.GlobalPlugin):
	def script_test(self, gesture):
		ran.append(
			{{
				"identifiers": [inputCore.normalizeGestureIdentifier(i) for i in gesture.identifiers],
				"modifiers": sorted(gesture.modifierNames),
			}}
		)

	__gestures = {{"kb:NVDA+t": "test"}}


def press(key):
	return inputHook.deliverKey(*key, False, False)


def release(key):
	return inputHook.deliverKey(*key, False, True)


def drain():
	\"\"\"Run the queued scripts as the core pump would, and name everything queued.\"\"\"
	queued = []
	while not queueHandler.eventQueue.empty():
		func, args, kwargs = queueHandler.eventQueue.get_nowait()
		queued.append(func.__qualname__)
		if func is scriptHandler._queueScriptCallback:
			func(*args, **kwargs)
	return queued


inputCore.initialize()
keyboardHandler.initialize(Observer())
globalPluginHandler.runningPlugins.add(Plugin())
"""


def test_syntheticKeysReachABoundScript(tmp_path: Path):
	done, value = run(
		tmp_path,
		SEAM,
		"""
		before = keyboardHandler.keyCounter
		passed = [press(INSERT), press(T), release(T), release(INSERT)]
		queued = drain()
		result = {
			"passed": passed,
			"ran": ran,
			"queued": queued,
			"counted": keyboardHandler.keyCounter - before,
			"trapped": sorted(keyboardHandler.trappedKeys),
			"modifiers": sorted(keyboardHandler.currentModifiers),
		}
		keyboardHandler.terminate()
		""",
		objects=True,
	)
	done.assertClean()
	assert value["passed"] == [False, False, False, False]
	assert value["ran"] == [{"identifiers": ["kb(desktop):nvda+t", "kb:nvda+t"], "modifiers": ["NVDA"]}]
	assert "_queueScriptCallback" in value["queued"]
	assert value["counted"] == 2
	assert value["trapped"] == []
	assert value["modifiers"] == []


def test_unboundKeyPassesToTheSystem(tmp_path: Path):
	done, value = run(
		tmp_path,
		SEAM,
		"""
		passed = [press(Q), release(Q)]
		queued = drain()
		result = {"passed": passed, "ran": ran, "scripts": queued.count("_queueScriptCallback")}
		keyboardHandler.terminate()
		""",
		objects=True,
	)
	done.assertClean()
	assert value == {"passed": [True, True], "ran": [], "scripts": 0}


def test_passNextKeyThroughLetsABoundGestureThrough(tmp_path: Path):
	done, value = run(
		tmp_path,
		SEAM,
		"""
		keyboardHandler.passNextKeyThrough()
		passed = [press(INSERT), press(T), release(T), release(INSERT)]
		drain()
		throughCount = keyboardHandler.passKeyThroughCount
		again = [press(INSERT), press(T), release(T), release(INSERT)]
		drain()
		result = {"passed": passed, "count": throughCount, "again": again, "ran": len(ran)}
		keyboardHandler.terminate()
		""",
		objects=True,
	)
	done.assertClean()
	assert value == {"passed": [True, True, True, True], "count": -1, "again": [False] * 4, "ran": 1}


#: Keys whose gestures need the live keyboard while they are pressed: (keys, what is missing).
SEAM_GAPS = {
	# The laptop layout's NVDA+[ and its other punctuation bindings: [ has no name of its own.
	"NVDA+[": ((INSERT, (0xDB, 0x1A, False)), "keyboard layout lookup"),
	# Say all on the desktop layout: the numpad operators read whether num lock is on.
	"numpadPlus": (((0x6B, 0x4E, False),), "keyboard state"),
}


@pytest.mark.parametrize("gesture", SEAM_GAPS)
def test_seamGapsWaitForTheCaptureLayer(tmp_path: Path, gesture: str):
	keys, missing = SEAM_GAPS[gesture]
	done, value = run(
		tmp_path,
		SEAM,
		f"""
		keys = {keys!r}
		passed = [press(key) for key in keys] + [release(key) for key in reversed(keys)]
		result = {{"passed": passed, "scripts": drain().count("_queueScriptCallback")}}
		keyboardHandler.terminate()
		""",
		objects=True,
	)
	# keyboardHandler logs the failure and lets the key through, as it does for any error on a press.
	assert done.noLinuxImplementation == [missing]
	assert done.logErrors == ["ERROR: internal_keyDownEvent"]
	assert value["scripts"] == 0
	assert value["passed"][len(keys) - 1] is True


def test_captureLayerAnswersCompleteTheSeam(tmp_path: Path):
	done, value = run(
		tmp_path,
		SEAM,
		"""
		import winUser

		NUMPAD_PLUS = (0x6B, 0x4E, False)
		BRACKET = (0xDB, 0x1A, False)


		# What a capture layer will answer from the live layout and key states.
		def mapVirtualKeyToChar(vkCode):
			return {0xDB: ord("["), 0xBE: ord(".")}.get(vkCode, 0)


		numLock = [0]


		def getKeyState(vkCode):
			return numLock[0] if vkCode == winUser.VK_NUMLOCK else 0


		inputHook.mapVirtualKeyToChar = mapVirtualKeyToChar
		inputHook.getKeyState = getKeyState


		class Plugin2(globalPluginHandler.GlobalPlugin):
			def script_bracket(self, gesture):
				ran.append([inputCore.normalizeGestureIdentifier(i) for i in gesture.identifiers])

			script_plus = script_bracket

			__gestures = {"kb:NVDA+[": "bracket", "kb:numpadPlus": "plus"}


		globalPluginHandler.runningPlugins.add(Plugin2())
		passed = [press(INSERT), press(BRACKET), release(BRACKET), release(INSERT)]
		passed += [press(NUMPAD_PLUS), release(NUMPAD_PLUS)]
		drain()
		numLock[0] = 1
		passed += [press(NUMPAD_PLUS), release(NUMPAD_PLUS)]
		drain()
		result = {"passed": passed, "ran": ran}
		keyboardHandler.terminate()
		""",
		objects=True,
	)
	done.assertClean()
	assert value == {
		# With num lock on, the numpad plus key types and is not say all.
		"passed": [False, False, False, False, False, False, True, True],
		"ran": [["kb(desktop):[+nvda", "kb:[+nvda"], ["kb(desktop):numpadplus", "kb:numpadplus"]],
	}


def test_scriptWithAltNeedsKeyInjection(tmp_path: Path):
	done, value = run(
		tmp_path,
		SEAM,
		"""
		from linuxPlatform import NoLinuxImplementation

		CONTROL = (0xA2, 0x1D, False)
		ALT = (0xA4, 0x38, False)
		N = (0x4E, 0x31, False)


		class Plugin2(globalPluginHandler.GlobalPlugin):
			def script_alt(self, gesture):
				ran.append("alt")

			__gestures = {"kb:control+alt+n": "alt"}


		globalPluginHandler.runningPlugins.add(Plugin2())
		passed = [press(CONTROL), press(ALT), press(N), release(N), release(ALT), release(CONTROL)]
		# Alt pressed and released alone would open a menu, so before the script runs keyboardHandler
		# injects a key the application ignores, which needs key injection.
		try:
			drain()
		except NoLinuxImplementation as e:
			missing = e.what
		result = {"passed": passed, "ran": ran, "missing": missing}
		keyboardHandler.terminate()
		""",
		objects=True,
	)
	assert done.noLinuxImplementation == ["user32.keybd_event"]
	assert not done.logErrors
	assert value == {
		"passed": [True, True, False, False, True, True],
		"ran": [],
		"missing": "user32.keybd_event",
	}


def test_mouseInitializesAndTerminates(tmp_path: Path):
	done, value = run(
		tmp_path,
		"""
		import wx

		# The rig's private display; core.main has made the application by this point.
		app = wx.App()
		import api
		import mouseHandler
		import watchdog
		from linuxPlatform import inputHook

		mouseHandler.initialize()
		mouseObjectIsDesktop = api.getMouseObject() is api.getDesktopObject()
		passed = inputHook.deliverMouse(mouseHandler.WM_MOUSEMOVE, 10, 20, False)
		moved = [mouseHandler.mouseMoved, list(mouseHandler.curMousePos)]
		# While the watchdog recovers from a freeze the hook passes mouse events on without NVDA seeing them.
		watchdog.isAttemptingRecovery = True
		try:
			passedInRecovery = inputHook.deliverMouse(mouseHandler.WM_MOUSEMOVE, 30, 40, False)
		finally:
			watchdog.isAttemptingRecovery = False
		movedInRecovery = list(mouseHandler.curMousePos)
		mouseHandler.terminate()
		result = {
			"mouseObjectIsDesktop": mouseObjectIsDesktop,
			"passed": passed,
			"moved": moved,
			"passedInRecovery": passedInRecovery,
			"movedInRecovery": movedInRecovery,
			"running": inputHook.isRunning(),
			"timer": mouseHandler._shapeTimer,
		}
		app.Destroy()
		""",
		objects=True,
	)
	done.assertClean()
	assert value == {
		"mouseObjectIsDesktop": True,
		"passed": True,
		"moved": [True, [10, 20]],
		"passedInRecovery": True,
		"movedInRecovery": [10, 20],
		"running": False,
		"timer": None,
	}


def test_audioCoordinatesBrightnessNamesScreenCapture(tmp_path: Path):
	done, value = run(
		tmp_path,
		"""
		import types

		import wx

		app = wx.App()
		import mouseHandler
		from linuxPlatform import NoLinuxImplementation

		mouseHandler.initialize()
		try:
			mouseHandler.playAudioCoordinates(10, 20, 100, 100, types.SimpleNamespace(x=0, y=0), detectBrightness=True)
		except NoLinuxImplementation as e:
			missing = e.what
		mouseHandler.terminate()
		result = missing
		app.Destroy()
		""",
		objects=True,
	)
	assert value == "screen capture"
	assert done.noLinuxImplementation == ["screen capture"]
	assert not done.logErrors


def test_touchIsNotSupported(tmp_path: Path):
	done, value = run(
		tmp_path,
		"""
		import touchHandler

		supported = touchHandler.touchSupported(debugLog=True)
		# core.main reads NotImplementedError from initialize as "no touch screen".
		try:
			touchHandler.initialize()
		except NotImplementedError as e:
			raised = type(e).__name__
		touchHandler.terminate()
		result = {"supported": supported, "raised": raised, "handler": touchHandler.handler}
		""",
	)
	done.assertClean()
	assert value == {"supported": False, "raised": "NotImplementedError", "handler": None}


BRAILLE_UP = """
import bdDetect
import braille
import braille.input
import brailleTables
import config
import hwIo
import inputCore
from linuxPlatform.ioThread import WaitableTimer

inputCore.initialize()
hwIo.initialize()
bdDetect.initialize()
brailleTables.initialize()
braille.input.initialize()
braille.initialize()
handler = braille.handler
"""

#: In core.main's order.
BRAILLE_DOWN = """
braille.input.terminate()
braille.terminate()
brailleTables.terminate()
bdDetect.terminate()
hwIo.terminate()
inputCore.terminate()
"""


def test_bdDetectReportsNoDevices(tmp_path: Path):
	done, value = run(
		tmp_path,
		"""
		import bdDetect
		import hwPortUtils

		bdDetect.initialize()
		fetcher = bdDetect.deviceInfoFetcher
		result = {
			"comPorts": fetcher.comPorts,
			"usbDevices": fetcher.usbDevices,
			"hidDevices": fetcher.hidDevices,
			"usbComPorts": fetcher.usbComPorts,
			"listed": [
				list(hwPortUtils.listComPorts(onlyAvailable=False)),
				list(hwPortUtils.listUsbDevices(onlyAvailable=False)),
				list(hwPortUtils.listHidDevices(onlyAvailable=False)),
			],
			"usbDrivers": list(bdDetect.getDriversForConnectedUsbDevices()),
			"bluetoothDrivers": list(bdDetect.getDriversForPossibleBluetoothDevices()),
			"registered": len(bdDetect._driverDevices) > 0,
		}
		bdDetect.terminate()
		result["afterTerminate"] = [bdDetect.deviceInfoFetcher, len(bdDetect._driverDevices)]
		""",
	)
	done.assertClean()
	assert value == {
		"comPorts": [],
		"usbDevices": [],
		"hidDevices": [],
		"usbComPorts": [],
		"listed": [[], [], []],
		"usbDrivers": [],
		"bluetoothDrivers": [],
		"registered": True,
		"afterTerminate": [None, 0],
	}


def test_brailleWithNoBrailleDisplay(tmp_path: Path):
	done, value = run(
		tmp_path,
		"""
		import config

		config.conf["braille"]["display"] = "noBraille"
		""",
		BRAILLE_UP,
		"""
		up = {
			"display": handler.display.name,
			"detector": handler._detector,
			"ackTimer": isinstance(handler.ackTimerHandle, WaitableTimer),
		}
		braille.pumpAll()
		""",
		BRAILLE_DOWN,
		"""
		import threading

		result = {
			**up,
			"handler": braille.handler,
			"ackTimerAfter": handler.ackTimerHandle,
			"threads": [t.name for t in threading.enumerate()],
		}
		""",
	)
	done.assertClean()
	assert value == {
		"display": "noBraille",
		"detector": None,
		"ackTimer": True,
		"handler": None,
		"ackTimerAfter": None,
		"threads": ["MainThread"],
	}


def test_brailleAutomaticDetectionFindsNothing(tmp_path: Path):
	done, value = run(
		tmp_path,
		"""
		import config

		config.conf["braille"]["display"] = "auto"
		""",
		BRAILLE_UP,
		"""
		detector = handler._detector
		scanned = detector is not None and detector._queuedFuture.result(10) is None
		up = {"display": handler.display.name, "scanned": scanned}
		""",
		BRAILLE_DOWN,
		"""
		import threading

		result = {**up, "threads": [t.name for t in threading.enumerate()]}
		""",
	)
	done.assertClean()
	assert value == {"display": "noBraille", "scanned": True, "threads": ["MainThread"]}


ACK_DISPLAY = """
import logging
import threading
import time

from braille.display.driver import BrailleDisplayDriver
from logHandler import log

timedOut = []


class Collector(logging.Handler):
	def emit(self, record):
		if "ACK packet timed out" in record.getMessage():
			timedOut.append(record.getMessage())


log.setLevel(log.DEBUG)
log.addHandler(Collector())


class AckDisplay(BrailleDisplayDriver):
	name = "testAck"
	description = "acknowledges writes"
	isThreadSafe = True
	receivesAckPackets = True
	timeout = 0.25

	def __init__(self):
		super().__init__()
		self.numCells = 4
		self.written = []

	def display(self, cells):
		self.written.append(list(cells))


resetterCalls = []
_resetter = handler._ackTimeoutResetter


def recordingResetter(param):
	resetterCalls.append(param)
	_resetter(param)


# The handler arms its timer with whatever this holds when it writes. Counting the calls, rather than
# the timeouts logged, sees a timer that was not cancelled even after the acknowledgement.
handler._ackTimeoutResetter = recordingResetter
display = AckDisplay()
handler.display = display
handler.queuedWrite = [1, 2, 3, 4]
handler._writeCellsInBackground()
deadline = time.monotonic() + 30
# The handler waits for an acknowledgement before it arms the timer; acknowledging in between would
# cancel nothing.
while handler.ackTimerHandle._thread is None and not resetterCalls and time.monotonic() < deadline:
	time.sleep(0.005)
armed = handler.ackTimerHandle._thread is not None
awaiting = display._awaitingAck
"""


@pytest.mark.parametrize("acknowledged", (False, True))
def test_brailleAckTimer(tmp_path: Path, acknowledged: bool):
	done, value = run(
		tmp_path,
		"""
		import config

		config.conf["braille"]["display"] = "noBraille"
		""",
		BRAILLE_UP,
		ACK_DISPLAY,
		f"""
		if {acknowledged!r}:
			display._handleAck()
			# Twice the display's timeout, when a timer that was not cancelled would fire, and then as long
			# again.
			time.sleep(1)
		else:
			# The timer is due at twice the display's timeout, and a loaded machine runs it later.
			deadline = time.monotonic() + 30
			while display._awaitingAck and time.monotonic() < deadline:
				time.sleep(0.005)
		up = {{
			"written": [list(cells) for cells in display.written],
			"armed": armed,
			"awaitingAfterWrite": awaiting,
			"awaitingNow": display._awaitingAck,
			"timerRan": len(resetterCalls),
			"timedOut": len(timedOut),
		}}
		# The test's display has no configuration section for terminate to save its settings to.
		handler.display = None
		""",
		BRAILLE_DOWN,
		"""
		result = up
		""",
	)
	done.assertClean()
	assert value == {
		"written": [[1, 2, 3, 4]],
		"armed": True,
		"awaitingAfterWrite": True,
		"awaitingNow": False,
		"timerRan": 0 if acknowledged else 1,
		"timedOut": 0 if acknowledged else 1,
	}
