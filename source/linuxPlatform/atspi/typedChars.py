"""Typed characters, learnt from the text an application inserts into the focus.

On Windows NVDA's in-process hook tells it each character a key typed. Here the application's own
text-changed signal carries it: one character inserted into the focus just after a key that types
text is the typed character, and NVDA's typedCharacter event speaks it, as it speaks one on Windows.

The text of a password field never passes through here: the caller gives None for it, and Qt sends
the real characters even so. A character inserted into a protected field is NVDA's protected
character, whatever the field holds.
"""

import ctypes
import time
from typing import Literal

import textUtils

from .textSource import toolkitOf, unitFor

#: Seconds after a key goes down within which an insert counts as what it typed.
TYPING_WINDOW = 0.5
#: Windows' virtual-key code of Backspace.
VK_BACK = 0x08
#: Line and paragraph breaks, which NVDA on Windows does not speak as typed: Qt inserts U+2029 for
#: Enter.
NOT_ECHOED = frozenset({"\n", "\r", "\r\n", " ", " "})
#: What a character typed into a protected field is spoken as, as speech.PROTECTED_CHAR.
PROTECTED_CHARACTER = "*"


def _lastKeyDown():
	from linuxPlatform import inputHook

	lastKeyDown = getattr(inputHook, "lastKeyDown", None)
	return lastKeyDown() if lastKeyDown is not None else None


def isOneCharacter(text: str) -> bool:
	"""Whether text is one character as NVDA counts them, a grapheme cluster: an emoji, or a letter
	with its combining marks."""
	if len(text) == 1:
		return True
	if not text:
		return False
	import NVDAHelper

	length = textUtils.WideStringOffsetConverter(text).encodedStringLength
	start, end = ctypes.c_int(), ctypes.c_int()
	if not NVDAHelper.localLib.calculateCharacterOffsets(
		text,
		length,
		0,
		ctypes.byref(start),
		ctypes.byref(end),
	):
		return False
	return start.value == 0 and end.value == length


def _typedPart(text: str, key) -> str:
	"""What a key typed, out of an insert that also carries blanks before it. VTE inserts a space it had
	not yet counted as text together with the character typed after it, as " x", when a program has
	drawn a prompt ending in a space and moved the cursor away and back."""
	typed = key.text if key is not None else ""
	if not typed or typed.isspace() or len(text) <= len(typed) or not text.endswith(typed):
		return text
	return typed if text[: -len(typed)].isspace() else text


def handleTextChange(
	obj,
	kind: Literal["insert", "delete"],
	offset: int,
	length: int,
	text: str | None,
) -> None:
	"""Queue typedCharacter for a change to obj's text, when it is a character typed into the focus.

	:param obj: The NVDAObject whose text changed.
	:param kind: Whether text was inserted or deleted.
	:param offset: Where, in the toolkit's unit.
	:param length: How much, in the toolkit's unit.
	:param text: What was inserted, or None when it must not be read: a password field's.
	"""
	import eventHandler

	# The focus as queued, which a focus event earlier in the same pump has already changed.
	focus = eventHandler.lastQueuedFocusObject
	node = getattr(focus, "node", None)
	if node is None or getattr(obj, "node", None) != node:
		return
	key = _lastKeyDown()
	isRecent = key is not None and time.monotonic() - key.time <= TYPING_WINDOW
	if kind == "delete":
		# Clears the character from the word NVDA builds for typed word echo; nothing is spoken.
		if isRecent and key.vkCode == VK_BACK:
			eventHandler.queueEvent("typedCharacter", focus, ch="\b")
		return
	if kind != "insert":
		return
	# Without a key source there is nothing to tell typing from an application's own inserts by.
	if key is not None and not (isRecent and key.typing and key.text >= " "):
		return
	if text is None or focus.isProtected:
		toolkit = getattr(focus, "toolkit", None) or toolkitOf(node)
		# An emoji is two UTF-16 units.
		if length > (2 if unitFor(toolkit) == "utf16" else 1):
			return
		character = PROTECTED_CHARACTER
	else:
		text = _typedPart(text, key)
		if text in NOT_ECHOED or not isOneCharacter(text):
			return
		character = text
	eventHandler.queueEvent("typedCharacter", focus, ch=character)
