"""The Linux side of winInputHook: where keyboard and mouse capture starts, stops and delivers events.

On Windows, winInputHook installs low-level hooks on a thread of its own, and the hooks call the
callbacks that keyboardHandler and mouseHandler register through winInputHook.setCallbacks. Here
deliverKey and deliverMouse take the hooks' place: the key source of linuxPlatform.keys hands each
key event to them on its own thread, and they call those same callbacks with the same arguments.
What keyboardHandler asks of the live keyboard while it turns those events into gestures, the layout
and the key states, is asked of that source too. With no source, as when there is no X display or
BASEDREADER_KEYS is off, nothing reads the keyboard, and whatever needs the live layout or key
states raises NoLinuxImplementation.
"""

import logging
import os
import re
import string
import threading
import time

from linuxPlatform import NoLinuxImplementation, keys
from linuxPlatform.keys import CaptureMode, KeyEvent, KeySource

_refCount = 0

#: Windows' SKF_ flags for the desktop's sticky keys setting, which keyboardHandler reads on each key
#: press to decide whether the NVDA key latches. Nothing here reads the desktop's setting, so sticky
#: keys reads as off and the NVDA key never latches.
stickyKeysFlags: int = 0

VK_CAPITAL = 0x14
VK_NONE = 0xFF
KEYEVENTF_EXTENDEDKEY = 0x1
KEYEVENTF_KEYUP = 0x2
#: Seconds between looks at the configuration's NVDA keys, at whether input help is on, and at
#: whether the focus is in a document in browse mode.
WATCH_INTERVAL = 0.05
#: Set to "off" in the environment, browse mode's keys are never captured, and reach the application
#: and NVDA both, as every other key does.
BROWSE_KEYS_VARIABLE = "BASEDREADER_BROWSE_KEYS"

_source: KeySource | None = None
_delivering = threading.local()
_lastKeyDown: KeyEvent | None = None
#: Whether Caps Lock was on before the press NVDA blocked that is still held; None when none is.
_capsLockBlockedWasOn: bool | None = None
_watcher: threading.Thread | None = None
_stopWatching = threading.Event()


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
		source.start(_onKey)
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
	if _watcher is not None:
		_watcher.join(1)
		_watcher = None
	source, _source = _source, None
	source.stop()


def _watch(source: KeySource) -> None:
	"""Keep the keys the source takes in step with the configuration, which the settings dialog, the
	welcome dialog and profile switches all change, hold the keyboard while input help is on, so
	that the keys it describes do nothing, and capture browse mode's keys while the focus is in a
	document in browse mode.

	A thread of its own, so that the keyboard is let go during watchdog recovery even when the main
	thread is frozen."""
	import api
	import config
	import inputCore
	import watchdog

	nvdaKeys = None
	held = False
	captured: CaptureMode = "off"
	while True:
		wanted = config.conf["keyboard"]["NVDAModifierKeys"]
		if wanted != nvdaKeys:
			source.setConsumed(wanted, numpadReview=True)
			nvdaKeys = wanted
		manager = inputCore.manager
		hold = bool(manager and manager.isInputHelpActive) and not watchdog.isAttemptingRecovery
		if hold != held:
			source.holdKeyboard(hold)
			held = hold
		capture = "off" if watchdog.isAttemptingRecovery else captureModeFor(api.getFocusObject())
		# Asked again while capturing, as the source lets its grabs go when the focus leaves the window
		# they are on, and takes them again only once asked.
		if capture != captured or capture != "off":
			source.setCapture(capture)
			captured = capture
		if _stopWatching.wait(WATCH_INTERVAL):
			break
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
	if event.captured and _isCoreStuck():
		# Its command would run only once NVDA comes back, if ever; the application is better served.
		return True
	if not event.isUp:
		_lastKeyDown = event
	_delivering.event = event
	try:
		passed = deliverKey(event.vkCode, event.scanCode, event.extended, event.injected, event.isUp)
	finally:
		_delivering.event = None
	if event.vkCode == VK_CAPITAL and _source is not None and not event.repeat:
		if not event.isUp and not event.reachedApplication and not passed:
			# On Windows a key the hook blocks toggles nothing. The X server locks Caps Lock as the key goes
			# down, whatever the grab does, and unlocks it as the key comes up when it was locked before;
			# it is put back both times.
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
