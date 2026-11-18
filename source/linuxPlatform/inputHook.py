"""The Linux side of winInputHook: where keyboard and mouse capture starts, stops and delivers events.

On Windows, winInputHook installs low-level hooks on a thread of its own, and the hooks call the
callbacks that keyboardHandler and mouseHandler register through winInputHook.setCallbacks. Here
deliverKey and deliverMouse take the hooks' place: the key source of linuxPlatform.keys hands each
key event to them on its own thread, and they call those same callbacks with the same arguments.
What keyboardHandler asks of the live keyboard while it turns those events into gestures, the layout
and the key states, is asked of that source too. With no source, as when there is no X display or
LINDA_KEYS is off, nothing reads the keyboard, and whatever needs the live layout or key
states raises NoLinuxImplementation.
"""

import logging
import os
import re
import string
import threading
import time
from collections.abc import Callable

from linuxPlatform import NoLinuxImplementation, keys
from linuxPlatform.keys import CaptureMode, GestureKey, KeyEvent, KeySource

_refCount = 0

#: Windows' SKF_ flags for the desktop's sticky keys setting, which keyboardHandler reads on each key
#: press to decide whether the NVDA key latches. Nothing here reads the desktop's setting, so sticky
#: keys reads as off and the NVDA key never latches.
stickyKeysFlags: int = 0

VK_CAPITAL = 0x14
VK_NONE = 0xFF
VK_LCONTROL = 0xA2
KEYEVENTF_EXTENDEDKEY = 0x1
KEYEVENTF_KEYUP = 0x2
#: Seconds between looks at the configuration's NVDA keys, at whether input help is on, and at
#: whether the focus is in a document in browse mode.
WATCH_INTERVAL = 0.05
#: Set to "off" in the environment, browse mode's keys are never captured, and reach the application
#: and NVDA both, as every other key does.
BROWSE_KEYS_VARIABLE = "LINDA_BROWSE_KEYS"

_source: KeySource | None = None
_delivering = threading.local()
_lastKeyDown: KeyEvent | None = None
#: Whether Caps Lock was on before the press NVDA blocked that is still held; None when none is.
_capsLockBlockedWasOn: bool | None = None
#: Callables that say whether another computer has the keyboard, as Remote Access does while it sends
#: the keys typed here to the computer it controls: the keyboard is held while any says so, as for
#: input help, since only a held key reaches NVDA alone, as one the Windows hook blocks does.
keyboardTakers: list[Callable[[], bool]] = []
_watcher: threading.Thread | None = None
_stopWatching = threading.Event()
#: Set when NVDA's focus enters or leaves a document or changes its mode, to have the watcher look at
#: once rather than at its next turn.
_changed = threading.Event()
#: Seconds after such a change before the watcher looks again: the focus it announces is made NVDA's
#: only once the announcement is over.
CHANGE_SETTLE = 0.01
#: Virtual-key code of each modifier NVDA's gestures name, generalised as keyboardHandler has them: its
#: name in a gesture.
_GESTURE_MODIFIERS = {0x10: "shift", 0x11: "control", 0x12: "alt", "windows": "windows"}
_KEYBOARD_GESTURE = re.compile(r"^kb(?:\((\w+)\))?:(.+)$")


def initialize() -> None:
	global _refCount
	_refCount += 1
	if _refCount == 1:
		_startSource()


def terminate() -> None:
	global _refCount
	if _refCount == 0:
		raise RuntimeError("winInputHook not running")
	if _refCount == 1:
		_stopSource()
	_refCount -= 1


def isRunning() -> bool:
	return _refCount > 0


def _startSource() -> None:
	global _source, _watcher
	_installLogMask()
	source = keys.createSource()
	if source is None:
		return
	from logHandler import log

	try:
		source.start(_onKey, _modeNow)
	except Exception:
		log.warning("NVDA cannot read the keyboard from the X display", exc_info=True)
		return
	_source = source
	_stopWatching.clear()
	_watcher = threading.Thread(target=_watch, args=(source,), name="keyboardWatch", daemon=True)
	_watcher.start()


def _stopSource() -> None:
	global _source, _watcher
	if _source is None:
		return
	_stopWatching.set()
	_changed.set()
	if _watcher is not None:
		_watcher.join(1)
		_watcher = None
	source, _source = _source, None
	source.stop()


def _watch(source: KeySource) -> None:
	"""Keep the keys the source takes in step with the configuration, which the settings dialog, the
	welcome dialog and profile switches all change, hold the keyboard while input help is on, so
	that the keys it describes do nothing, and capture a document's keys while the focus is in one.

	A thread of its own, so that the keyboard is let go during watchdog recovery even when the main
	thread is frozen."""
	import api
	import config
	import inputCore
	import treeInterceptorHandler
	import watchdog
	from logHandler import log

	treeInterceptorHandler.post_browseModeStateChange.register(_onBrowseModeStateChange)
	nvdaKeys = None
	held = False
	captured: CaptureMode = "off"
	gestures = None
	interval = WATCH_INTERVAL
	while True:
		try:
			wanted = config.conf["keyboard"]["NVDAModifierKeys"]
			if wanted != nvdaKeys:
				source.setConsumed(wanted, numpadReview=True)
				nvdaKeys = wanted
			manager = inputCore.manager
			hold = (
				bool(manager and manager.isInputHelpActive) or any(taker() for taker in keyboardTakers)
			) and not watchdog.isAttemptingRecovery
			if hold != held:
				source.holdKeyboard(hold)
				held = hold
			focus = api.getFocusObject()
			capture = "off" if watchdog.isAttemptingRecovery else captureModeFor(focus)
			if capture != "off":
				try:
					interceptor = focus.treeInterceptor
					signature = _gestureSignature(interceptor)
					if gestures is None or gestures[0] != signature:
						gestures = (signature, *_gesturesFor(interceptor))
						source.setCaptureGestures(gestures[1], gestures[2])
				# This thread must go on whatever a focus going away or an add-on's gesture map raises: it
				# lets the keyboard go during recovery. The keys captured anyway are browse mode's own.
				except Exception:  # noqa: BLE001
					log.debugWarning("Finding the keys bound in browse mode", exc_info=True)
			# Asked again while capturing, as the source lets its grabs go when the focus leaves the window
			# they are on, and takes them again only once asked.
			if capture != captured or capture != "off":
				source.setCapture(capture, _processOf(focus) if capture != "off" else None)
				captured = capture
		# Nothing else holds the keyboard for input help or lets it go during recovery: a look that
		# failed, as every look does while a reset leaves NVDA without a configuration, is taken again
		# at the next. Logged below ERROR, which would read that configuration for the error sound.
		except Exception:  # noqa: BLE001
			log.debugWarning("Looking at what to do with the keyboard", exc_info=True)
		changed = _changed.wait(interval)
		_changed.clear()
		if _stopWatching.is_set():
			break
		interval = CHANGE_SETTLE if changed else WATCH_INTERVAL
	treeInterceptorHandler.post_browseModeStateChange.unregister(_onBrowseModeStateChange)
	if held:
		source.holdKeyboard(False)
	if captured != "off":
		source.setCapture("off")


def captureModeFor(focus) -> CaptureMode:
	"""Which keys to capture with this focus: browse mode's while it is in a document in browse mode,
	Escape alone while that document is in focus mode, and otherwise none.

	Asked off the main thread: the focus's tree interceptor is the one api.setFocusObject set on it,
	so asking for it calls nothing in the application.
	"""
	if os.environ.get(BROWSE_KEYS_VARIABLE, "") == "off" or focus is None:
		return "off"
	import browseMode

	try:
		interceptor = focus.treeInterceptor
	# A focus that has just gone away raises whatever its kind of object raises; it has no document.
	except Exception:  # noqa: BLE001
		return "off"
	if not isinstance(interceptor, browseMode.BrowseModeTreeInterceptor) or not interceptor.isReady:
		return "off"
	return "focus" if interceptor.passThrough else "browse"


def _processOf(focus) -> int | None:
	"""The process of the focus's application, as its app module already holds it: asked off the main
	thread, so nothing is asked of the bus."""
	try:
		ref = focus.__dict__.get("_appModuleRef")
		appModule = ref() if ref is not None else None
	# A focus that has just gone away raises whatever its kind of object raises.
	except Exception:  # noqa: BLE001
		return None
	return getattr(appModule, "processID", None) or None


def _onBrowseModeStateChange() -> None:
	_changed.set()


def _modeNow() -> CaptureMode | None:
	"""Which keys NVDA captures at this moment, for the key source to ask of each captured press."""
	import api
	import watchdog

	if watchdog.isAttemptingRecovery:
		return "off"
	return captureModeFor(api.getFocusObject())


def _gestureSignature(interceptor) -> tuple:
	"""What the keys bound in interceptor follow: its class, its own bindings, the keyboard layout
	and the gesture maps, which the Input Gestures dialog changes."""
	import config
	import inputCore

	manager = inputCore.manager
	return (
		type(interceptor),
		len(interceptor._gestureMap),
		config.conf["keyboard"]["keyboardLayout"],
		repr(manager.userGestureMap._map),
		id(manager.localeGestureMap),
	)


def _gesturesFor(interceptor) -> tuple[frozenset[GestureKey], frozenset[GestureKey]]:
	"""The keys bound to scripts of interceptor without the NVDA key, by its classes and by the gesture
	maps of the locale and the user, add-ons' included: (all of them, those that work in focus mode)."""
	import config
	import inputCore
	import keyboardHandler

	layout = config.conf["keyboard"]["keyboardLayout"]
	scripts = dict(interceptor._gestureMap)
	classes = {(cls.__module__, cls.__name__) for cls in type(interceptor).__mro__}
	manager = inputCore.manager
	for gestureMap in (manager.localeGestureMap, manager.userGestureMap):
		for identifier, entries in gestureMap._map.items():
			for module, className, scriptName in entries:
				if (module, className) in classes:
					scripts[identifier] = (
						getattr(type(interceptor), f"script_{scriptName}", None) if scriptName else None
					)
	browse: set[GestureKey] = set()
	focus: set[GestureKey] = set()
	for identifier, script in scripts.items():
		match = _KEYBOARD_GESTURE.match(identifier)
		if script is None or match is None or match[1] not in (None, layout) or "nvda" in match[2].split("+"):
			continue
		try:
			gesture = keyboardHandler.KeyboardInputGesture.fromName(match[2])
		# A name for a key this keyboard does not have.
		except (LookupError, ValueError, NoLinuxImplementation):
			continue
		names = frozenset(_GESTURE_MODIFIERS.get(vk) for vk, _extended in gesture.generalizedModifiers)
		if None in names:
			continue
		key = (names, gesture.vkCode, bool(gesture.isExtended))
		browse.add(key)
		if getattr(script, "ignoreTreeInterceptorPassThrough", False):
			focus.add(key)
	return frozenset(browse), frozenset(focus)


def _isCoreStuck() -> bool:
	"""Whether NVDA's main thread has been in one cycle of its core longer than the watchdog takes for
	a possible freeze, so that a command queued now would wait for as long as that lasts."""
	import watchdog

	if watchdog.isAttemptingRecovery:
		return True
	if not watchdog.isRunning or watchdog.isCoreAsleep():
		return False
	lastAlive = getattr(watchdog, "_lastAlive", None)
	return lastAlive is not None and time.perf_counter() - lastAlive > watchdog.MIN_CORE_ALIVE_TIMEOUT


def _onKey(event: KeyEvent) -> bool:
	"""Called by the key source for each key, on its thread.

	:return: True to pass the key on to the application, which matters only for a captured press.
	"""
	global _lastKeyDown, _capsLockBlockedWasOn
	if event.captured and not event.nvdaKeyGrab and _isCoreStuck():
		# Its command would run only once NVDA comes back, if ever; the application is better served.
		# A chord with the NVDA key is NVDA's whatever, as on Windows, where it waits for NVDA too.
		return True
	if not event.isUp:
		_lastKeyDown = event
	_delivering.event = event
	try:
		passed = deliverKey(event.vkCode, event.scanCode, event.extended, event.injected, event.isUp)
	finally:
		_delivering.event = None
	if not event.isUp and not passed:
		import queueHandler

		# Its command, queued there before this, may change what the keyboard is held for, as input
		# help's and Remote Access's toggles do; looked at only at the watcher's next turn, the keys
		# typed until then would reach the application, or no one.
		queueHandler.queueFunction(queueHandler.eventQueue, _changed.set)
	if event.vkCode == VK_CAPITAL and _source is not None and not event.repeat:
		if not event.isUp and not passed:
			# On Windows a key the hook blocks toggles nothing. The X server locks Caps Lock as the key goes
			# down, whatever the grab does, and unlocks it as the key comes up when it was locked before;
			# it is put back both times. So it is when the application had the key too, as every key
			# reaches the program that holds the keyboard, an open menu or a password prompt.
			_capsLockBlockedWasOn = event.capsLockWasOn
			_source.setCapsLock(event.capsLockWasOn)
		elif event.isUp and _capsLockBlockedWasOn is not None:
			_source.setCapsLock(_capsLockBlockedWasOn)
			_capsLockBlockedWasOn = None
	return passed


def deliverKey(vkCode: int, scanCode: int, extended: bool, injected: bool, isUp: bool) -> bool:
	"""Hand a key event to the callback registered for it, as the Windows keyboard hook does.

	:param vkCode: The Windows virtual-key code, which NVDA's gesture names are built on.
	:param scanCode: The PC set 1 scan code.
	:param extended: Whether the scan code has the E0 prefix.
	:param injected: Whether software rather than a keyboard produced the event.
	:param isUp: Whether the key was released rather than pressed.
	:return: True to pass the key on to the system, False to swallow it.
	"""
	import winInputHook

	if not isRunning():
		raise RuntimeError("winInputHook not running")
	callback = winInputHook.keyUpCallback if isUp else winInputHook.keyDownCallback
	if callback is None:
		return True
	return bool(callback(vkCode, scanCode, extended, injected))


def deliverMouse(msg: int, x: int, y: int, injected: bool) -> bool:
	"""Hand a mouse event to the registered callback, as the Windows mouse hook does.

	:param msg: The Windows mouse message, such as mouseHandler.WM_MOUSEMOVE.
	:return: True to pass the event on to the system, False to swallow it.
	"""
	import watchdog
	import winInputHook

	if not isRunning():
		raise RuntimeError("winInputHook not running")
	if watchdog.isAttemptingRecovery or winInputHook.mouseCallback is None:
		return True
	return bool(winInputHook.mouseCallback(msg, x, y, injected))


def deliveringKey() -> KeyEvent | None:
	"""The key event being delivered on this thread, if any."""
	return getattr(_delivering, "event", None)


def isDeliveringToApplication() -> bool:
	"""Whether the key being delivered on this thread also reached the application.

	A gesture made from it then has nothing to send on: the application already has the key."""
	event = deliveringKey()
	return event is not None and event.reachedApplication


def lastKeyDown() -> KeyEvent | None:
	"""The last key pressed, as the key source delivered it."""
	return _lastKeyDown


def keybdEvent(vk: int, scan: int, flags: int, extraInfo: int) -> None:
	"""Send a key as winUser.keybd_event does."""
	if vk == VK_NONE:
		# Windows sends it so that an application sees Alt or Windows was not pressed alone; no X key
		# has this code, and X applications open no menu on Alt alone.
		return
	if _source is None:
		raise NoLinuxImplementation("key injection", "sending a key needs the X display")
	_source.inject(vk, scan, bool(flags & KEYEVENTF_EXTENDEDKEY), bool(flags & KEYEVENTF_KEYUP))


def typeKey(vk: int, extended: bool, isUp: bool, scan: int | None = None) -> None:
	"""Press or release a key as SendInput does for another program on Windows: NVDA has it as it has
	any key, and the application then has it unless NVDA takes it.

	:param scan: The key's scan code, when the sender gave one.
	"""
	if vk is None or vk == VK_NONE:
		# Windows sends VK_NONE so that an application sees a modifier was not pressed alone; no X key
		# has it.
		return
	if vk == VK_LCONTROL and scan == keys.ALTGR_CONTROL_SCAN:
		# Windows reports AltGr as this Control and right Alt. Right Alt alone is AltGr on an X layout
		# that has one, and with Control the letter after it would be a shortcut instead of typing.
		return
	if _source is None:
		from logHandler import log

		log.debugWarning(f"No keyboard to type virtual key {vk:#x} on")
		return
	_source.typeKey(vk, extended, isUp)


def releaseTypedKeys() -> None:
	"""Release every key typeKey holds down."""
	if _source is not None:
		_source.releaseTypedKeys()


def vkKeyScan(char: str) -> tuple[int, int]:
	"""The modifiers and virtual-key code that type char, as winUser.VkKeyScanEx returns them.

	Windows defines the virtual-key codes of the letters A to Z as their upper case ASCII codes on
	every layout, and on Latin layouts a lower case letter takes no modifier and an upper case one
	takes shift; with no key source, that is the answer for them. Any other character depends on the
	layout in use.

	:return: The modifiers as VkKeyScanEx's high byte gives them, 1 being shift, and the virtual-key code.
	"""
	if _source is not None:
		try:
			return _source.vkForChar(char)
		except LookupError:
			pass
	if len(char) == 1 and char in string.ascii_letters:
		return int(char.isupper()), ord(char.upper())
	raise NoLinuxImplementation(
		"keyboard layout lookup",
		f"which key types {char!r} depends on the keyboard layout",
	)


def mapVirtualKeyToChar(vkCode: int) -> int:
	"""What the key with vkCode types unshifted, as user32.MapVirtualKeyEx with MAPVK_VK_TO_CHAR returns it.

	keyboardHandler names a key that has no name of its own, such as the punctuation keys, after
	this character, so every press of one asks.

	:return: The character's code, with the top bit of 32 set for a dead key, or 0 if it types none.
	"""
	if _source is not None:
		return _source.charForVk(vkCode)
	raise NoLinuxImplementation(
		"keyboard layout lookup",
		f"what virtual key {vkCode:#x} types depends on the keyboard layout",
	)


def getKeyState(vkCode: int) -> int:
	"""The state of a key, as winUser.getKeyState returns it.

	:return: Bit 0 set while a toggle key is on, and bit 15 set while the key is held down.
	"""
	if _source is not None:
		return _source.keyState(vkCode)
	raise NoLinuxImplementation(
		"keyboard state",
		f"the state of virtual key {vkCode:#x} needs the live keyboard",
	)


def getKeyNameText(scanCode: int, extended: bool) -> str:
	"""A name for the key at this position, as winUser.getKeyNameText gives it."""
	if _source is not None:
		return _source.keyName(scanCode, extended)
	raise NoLinuxImplementation("key names", "naming a key by its position needs the live keyboard")


#: An "Input:" record of inputCore, split into what stays and the key names.
_INPUT_RECORD = re.compile(r"^(Input: kb(?:\([^)]*\))?:)(.*)$", re.DOTALL)
#: Modifiers that make a chord a command, which is logged by name.
_COMMAND_MODIFIERS = frozenset(("nvda", "control", "alt", "windows"))


class _TypedKeyMask(logging.Filter):
	"""Hides, in inputCore's "Input:" records, keys that typed text."""

	def filter(self, record: logging.LogRecord) -> bool:
		if not keys.MASK_TYPED_KEYS_IN_LOG or not isinstance(record.msg, str):
			return True
		match = _INPUT_RECORD.match(record.msg)
		event = deliveringKey()
		if match is None or event is None or not event.typing:
			return True
		names = match.group(2).split("+")
		if any(name.lower() in _COMMAND_MODIFIERS for name in names[:-1]):
			return True
		record.msg = f"{match.group(1)}(typing key)"
		record.args = ()
		return True


_typedKeyMask = _TypedKeyMask()


def _installLogMask() -> None:
	from logHandler import log

	if _typedKeyMask not in log.filters:
		log.addFilter(_typedKeyMask)
