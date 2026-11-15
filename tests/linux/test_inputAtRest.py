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
#: process: device and system files, sockets, native libraries and child processes.
PRELUDE = """
import sys

_touched = []


def _watch(event, args):
	if event in ("socket.connect", "ctypes.dlopen", "subprocess.Popen", "os.system"):
		_touched.append(f"{event} {args!r}")
	elif event == "open" and isinstance(args[0], str) and args[0].startswith(("/dev/", "/sys/", "/run/")):
		_touched.append(f"{event} {args[0]}")


sys.addaudithook(_watch)

import NVDAHelper  # noqa: F401
import languageHandler

# STUB, owned by workstream A2: languageHandler.setLanguage needs Windows locale identifiers, which
# have no Linux body yet. inputCore reads the language to find the locale's gesture map.
languageHandler._language = "en"
"""

EPILOGUE = """
result = {"value": result, "touched": _touched}
"""

#: A placeholder focus and desktop, as upstream's unit tests use, and a watchdog observer that is
#: not recovering, which is what keyboardHandler.initialize receives from core.main.
OBJECTS = """
import globalVars
from NVDAObjects import NVDAObject


class Placeholder(NVDAObject):
	processID = None
	windowThreadID = 0
	# Finding an object's app module reads /proc on Linux, which is workstream B3's and not built yet.
	appModule = None

	def _isEqual(self, other):
		return False

	@staticmethod
	def objectFromPoint(x, y):
		return None


class Observer:
	isAttemptingRecovery = False


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
		space = KeyboardInputGesture.fromName("space")
		capsLock = KeyboardInputGesture.fromName("capsLock")
		result = {
			"layouts": sorted(KeyboardInputGesture.LAYOUTS),
			"nvda": [nvda.isModifier, nvda.isNVDAModifierKey, nvda.mainKeyName],
			"shift": [shift.isModifier, shift.isNVDAModifierKey, shift.isCharacter],
			"generalized": sorted(leftControlA.generalizedModifiers),
			"leftControlA": [leftControlA.isCharacter, leftControlA.shouldReportAsCommand, leftControlA.identifiers[1]],
			"shiftA": [shiftA.isCharacter, shiftA.shouldReportAsCommand],
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
		# Which key types a character other than a lower case letter depends on the layout.
		("KeyboardInputGesture.fromName('NVDA+.')", "keyboard layout lookup"),
		("KeyboardInputGesture.fromName('NVDA+1')", "keyboard layout lookup"),
		("KeyboardInputGesture.fromName('T')", "keyboard layout lookup"),
		# The numpad operators read whether num lock is on.
		("KeyboardInputGesture.fromName('numpadPlus')", "user32.GetKeyState"),
		# A key with no name of its own is named after what it types.
		("KeyboardInputGesture(set(), 0xBA, 0x27, False).mainKeyName", "user32.GetKeyboardLayout"),
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


def test_mouseInitializesAndTerminates(tmp_path: Path):
	done, value = run(
		tmp_path,
		"""
		import wx

		# The rig's private display; core.main has made the application by this point.
		app = wx.App()
		import api
		import mouseHandler
		from linuxPlatform import inputHook

		mouseHandler.initialize()
		mouseObjectIsDesktop = api.getMouseObject() is api.getDesktopObject()
		passed = inputHook.deliverMouse(mouseHandler.WM_MOUSEMOVE, 10, 20, False)
		moved = [mouseHandler.mouseMoved, list(mouseHandler.curMousePos)]
		mouseHandler.terminate()
		result = {
			"mouseObjectIsDesktop": mouseObjectIsDesktop,
			"passed": passed,
			"moved": moved,
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
		"running": False,
		"timer": None,
	}


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
	timeout = 0.05

	def __init__(self):
		super().__init__()
		self.numCells = 4
		self.written = []

	def display(self, cells):
		self.written.append(list(cells))


display = AckDisplay()
handler.display = display
handler.queuedWrite = [1, 2, 3, 4]
handler._writeCellsInBackground()
deadline = time.monotonic() + 2
while not display._awaitingAck and time.monotonic() < deadline:
	time.sleep(0.01)
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
		# Twice the display's timeout is when the handler stops waiting for an acknowledgement.
		time.sleep(0.4)
		up = {{
			"written": [list(cells) for cells in display.written],
			"awaitingAfterWrite": awaiting,
			"awaitingNow": display._awaitingAck,
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
		"awaitingAfterWrite": True,
		"awaitingNow": False,
		"timedOut": 0 if acknowledged else 1,
	}
