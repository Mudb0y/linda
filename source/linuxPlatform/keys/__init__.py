"""Where key events come from on Linux, and what NVDA is told about each.

A key source watches the keyboard for the whole session and hands each press and release to
linuxPlatform.inputHook, which gives it to keyboardHandler as the Windows keyboard hook would. The
keys NVDA uses as the NVDA key and the numpad review keys are taken from the applications, and every
key pressed while one of them is held, so that a command reaches NVDA alone; a key taken so that NVDA
passes reaches the application as it was typed. Every other key reaches the application and NVDA
both, and NVDA's scripts for it then read what the application did instead of sending the key on.

On a web page the keys browse mode uses, the arrows and the letters among them, and those focus mode
uses, as Escape, are captured: the application gets such a key only when NVDA's answer for it says
to pass it on, as the Windows hook's return value does, or when the mode NVDA is in as it comes does
not use it.
"""

import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, Protocol

#: Whether keys that type text are hidden in the debug log's "Input:" lines. NVDA logs every key at
#: debug level, and a password typed while debug logging is on would otherwise be in the log. Command
#: keys and chords with the NVDA key, Control, Alt or Super are logged by name either way.
MASK_TYPED_KEYS_IN_LOG: bool = True

#: Set to "off" in the environment, no key source is started.
SWITCH_VARIABLE = "LINDA_KEYS"

#: Which keys are captured: none beyond the NVDA keys, Escape alone for focus mode, or browse mode's.
CaptureMode = Literal["off", "focus", "browse"]


@dataclass(frozen=True)
class KeyEvent:
	"""A key press or release, in the terms of the Windows keyboard hook, with what Linux adds."""

	vkCode: int
	scanCode: int
	extended: bool
	injected: bool
	isUp: bool
	repeat: bool
	#: False when NVDA took the key and the application never received it.
	reachedApplication: bool
	#: What the key types in the keyboard's state as it was pressed; "" for modifiers and dead keys.
	text: str
	#: Whether the key typed text with no Control, Alt or Super held.
	typing: bool
	#: time.monotonic() when the event arrived.
	time: float
	#: Whether the application gets the key only if deliver returns True for it: a captured press.
	captured: bool = False
	#: Whether Caps Lock was on before the key went down.
	capsLockWasOn: bool = False
	#: Whether the press is an NVDA key or numpad review key, or came while one was held: NVDA's
	#: unless deliver passes it, however long NVDA's core takes to run its command.
	nvdaKeyGrab: bool = False


#: (modifier names as NVDA's gestures have them, virtual-key code, extended) of a gesture to capture.
GestureKey = tuple[frozenset[str], int, bool]


class KeySource(Protocol):
	def start(
		self,
		deliver: Callable[[KeyEvent], bool],
		modeNow: Callable[[], CaptureMode | None] | None = None,
	) -> None:
		"""Start watching the keyboard; deliver is called for each event, in order, on a thread of the
		source's own, and returns True to pass the key on to the application, as the Windows hook's
		callback does. Only a captured press waits for that answer, and not for long: with no answer in
		time, a key of browse mode reaches the application, and one with the NVDA key stays NVDA's.

		:param modeNow: Which keys NVDA captures at this moment, asked on that thread for each
			captured press: one the mode does not capture goes to the application at once, and to
			deliver as one that reached it. None from it, or no modeNow, keeps the press captured.
		"""

	def stop(self) -> None: ...

	def setCapture(self, mode: CaptureMode, processID: int | None = None) -> None:
		"""Capture the keys of a document from now on, browse mode's and focus mode's both, or none
		when mode is off; mode is the one in force, for the record.

		:param processID: The process of the application whose document mode is that of, when known.
		"""

	def setCaptureGestures(self, browse: frozenset[GestureKey], focus: frozenset[GestureKey]) -> None:
		"""Capture these too, beyond the keys each mode always captures: the gestures the gesture maps
		bind in each mode."""

	def setConsumed(self, nvdaModifierKeys: int, numpadReview: bool) -> None:
		"""Take these keys from the applications.

		:param nvdaModifierKeys: config.configFlags.NVDAKey flags.
		:param numpadReview: Whether to take the numpad review keys while Num Lock is off.
		"""

	def holdKeyboard(self, hold: bool) -> None:
		"""Take every key from the applications, or stop doing so."""

	def setCapsLock(self, on: bool) -> None:
		"""Turn Caps Lock on or off without a key press anything sees."""

	def inject(self, vkCode: int, scanCode: int, extended: bool, isUp: bool) -> None:
		"""Send a key as if typed. Its own echo is not delivered."""

	def typeKey(self, vkCode: int, extended: bool, isUp: bool) -> None:
		"""Press or release a key as a keyboard does, as Remote Access does with the keys of the
		computer controlling this one: unlike inject, the key comes to NVDA as any key does, marked
		injected, and NVDA's grabs take it, so that a chord with the NVDA key runs NVDA's command."""

	def releaseTypedKeys(self) -> None:
		"""Release every key typeKey holds down."""

	def keyState(self, vkCode: int) -> int:
		"""As user32.GetKeyState: bit 0 while a lock is on, bit 15 while the key is down."""

	def charForVk(self, vkCode: int) -> int:
		"""As MapVirtualKeyEx with MAPVK_VK_TO_CHAR in the active layout."""

	def vkForChar(self, ch: str) -> tuple[int, int]:
		"""As VkKeyScanEx: the modifiers and the virtual-key code that type ch."""

	def keyName(self, scanCode: int, extended: bool) -> str:
		"""As GetKeyNameText: a name for the key at this position."""


def isEnabled() -> bool:
	return os.environ.get(SWITCH_VARIABLE, "") != "off"


def createSource() -> KeySource | None:
	"""The key source for this session, or None when there is none to watch."""
	if not isEnabled() or not os.environ.get("DISPLAY"):
		return None
	from . import x11

	return x11.X11KeySource()
