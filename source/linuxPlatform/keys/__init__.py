"""Where key events come from on Linux, and what NVDA is told about each.

A key source watches the keyboard for the whole session and hands each press and release to
linuxPlatform.inputHook, which gives it to keyboardHandler as the Windows keyboard hook would. The
keys NVDA uses as the NVDA key and the numpad review keys are taken from the applications, so that a
command reaches NVDA alone; every other key reaches the application and NVDA both, and NVDA's
scripts for it then read what the application did instead of sending the key on.
"""

import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

#: Whether keys that type text are hidden in the debug log's "Input:" lines. NVDA logs every key at
#: debug level, and a password typed while debug logging is on would otherwise be in the log. Command
#: keys and chords with the NVDA key, Control, Alt or Super are logged by name either way.
MASK_TYPED_KEYS_IN_LOG: bool = True

#: Set to "off" in the environment, no key source is started.
SWITCH_VARIABLE = "BASEDREADER_KEYS"


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


class KeySource(Protocol):
	def start(self, deliver: Callable[[KeyEvent], None]) -> None:
		"""Start watching the keyboard; deliver is called for each event, on the source's own thread."""

	def stop(self) -> None: ...

	def setConsumed(self, nvdaModifierKeys: int, numpadReview: bool) -> None:
		"""Take these keys from the applications.

		:param nvdaModifierKeys: config.configFlags.NVDAKey flags.
		:param numpadReview: Whether to take the numpad review keys while Num Lock is off.
		"""

	def holdKeyboard(self, hold: bool) -> None:
		"""Take every key from the applications, or stop doing so."""

	def inject(self, vkCode: int, scanCode: int, extended: bool, isUp: bool) -> None:
		"""Send a key as if typed. Its own echo is not delivered."""

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
