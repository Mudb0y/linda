"""The Linux side of winInputHook: where keyboard and mouse capture starts, stops and delivers events.

On Windows, winInputHook installs low-level hooks on a thread of its own, and the hooks call the
callbacks that keyboardHandler and mouseHandler register through winInputHook.setCallbacks. On Linux
a capture layer will take the hooks' place. It is started and stopped here, and it hands each event
to deliverKey or deliverMouse, which call those same callbacks with the same arguments and return
their answer, whether the event goes on to the rest of the system. No capture layer exists yet, so
initialize and terminate start and stop nothing, and only tests deliver events.
"""

import string

from linuxPlatform import NoLinuxImplementation

_refCount = 0

#: Windows' SKF_ flags for the desktop's sticky keys setting, which keyboardHandler reads on each key
#: press to decide whether the NVDA key latches. They are the capture layer's to report. Until one
#: exists no real key reaches NVDA, so there is nothing to latch, and sticky keys reads as off.
stickyKeysFlags: int = 0


def initialize() -> None:
	global _refCount
	_refCount += 1


def terminate() -> None:
	global _refCount
	if _refCount == 0:
		raise RuntimeError("winInputHook not running")
	_refCount -= 1


def isRunning() -> bool:
	return _refCount > 0


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


def vkKeyScan(char: str) -> tuple[int, int]:
	"""The modifiers and virtual-key code that type char, as winUser.VkKeyScanEx returns them.

	Windows defines the virtual-key codes of the letters A to Z as their upper case ASCII codes on
	every layout, and on Latin layouts a lower case letter takes no modifier. Any other character,
	digits included, which take shift on French layouts, depends on the layout in use, which only a
	capture layer will know.
	"""
	if len(char) == 1 and char in string.ascii_lowercase:
		return 0, ord(char.upper())
	raise NoLinuxImplementation(
		"keyboard layout lookup", f"which key types {char!r} depends on the keyboard layout"
	)
