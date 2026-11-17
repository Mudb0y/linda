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
from difflib import SequenceMatcher
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
#: Characters a terminal program shows in place of what was typed, as at a password prompt.
MASKS = frozenset("*\u2022")
#: Seconds within which VTE's report of the text a change removed is paired with what replaced it.
REPLACED_SECONDS = 0.05
#: How far inside an editable focus a change to the text typed into it may come from: a web page's
#: rich text editor keeps its text in the paragraphs inside it, which report the change themselves.
INSIDE_FOCUS_DEPTH = 4

#: The last text removed from an object, as (its node, offset, text, time.monotonic()).
_lastRemoved: tuple[object, int, str, float] | None = None
#: When the key went down whose character a terminal last echoed, so that it is echoed once whether
#: VTE's report or the terminal's own reading of its screen finds it first.
echoedKeyTime: float | None = None


def claimEcho(keyTime: float | None) -> bool:
	"""Whether the character typed into a terminal by the key that went down at keyTime is still to be
	echoed, neither it nor a later one having been; it is not from now on."""
	global echoedKeyTime
	if keyTime is None or (echoedKeyTime is not None and echoedKeyTime >= keyTime):
		return False
	echoedKeyTime = keyTime
	return True


def _terminalRole():
	from controlTypes import Role

	return Role.TERMINAL


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


def _typedInTerminal(removed: str | None, inserted: str, key) -> str | None:
	"""What a key typed in a terminal, out of a change, or None. VTE reports everything from the first
	to the last difference on its screen as one change, so a typed letter comes with a spinner or a
	status line that changed with it; and a program's spinner changes one character at a time, which
	is not typing however soon after a key it comes."""
	typed = key.text if key is not None else ""
	if not typed.strip():
		# A space shows as nothing but itself.
		return typed if removed is None and inserted == typed else None
	if removed is None:
		inserted = _typedPart(inserted, key)
		return inserted if inserted == typed or inserted in MASKS else None
	found = []
	for tag, i1, i2, j1, j2 in SequenceMatcher(None, removed, inserted, autojunk=False).get_opcodes():
		added = inserted[j1:j2].strip()
		if tag != "equal" and not removed[i1:i2].strip() and (added == typed or added in MASKS):
			found.append(added)
	return found[0] if len(found) == 1 else None


def _isInsideFocus(node, focus) -> bool:
	"""Whether node is inside focus, an editable object, within INSIDE_FOCUS_DEPTH parents."""
	if "editable" not in getattr(focus, "atspiStates", ()):
		return False
	from .client import NodeGone
	from .handler import client

	try:
		for _ in range(INSIDE_FOCUS_DEPTH):
			node = client().parent(node)
			if node is None:
				return False
			if node == focus.node:
				return True
	except NodeGone:
		pass
	return False


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
	objNode = getattr(obj, "node", None)
	if node is None or objNode is None:
		return
	global _lastRemoved
	key = _lastKeyDown()
	isRecent = key is not None and time.monotonic() - key.time <= TYPING_WINDOW
	inside = objNode != node
	if inside and not (isRecent and _isInsideFocus(objNode, focus)):
		return
	removed = None
	if _lastRemoved is not None:
		removedNode, removedOffset, removedText, removedAt = _lastRemoved
		if (
			removedNode == node
			and removedOffset == offset
			and time.monotonic() - removedAt <= REPLACED_SECONDS
		):
			removed = removedText
	_lastRemoved = None
	if kind == "delete":
		if text is not None:
			_lastRemoved = (node, offset, text, time.monotonic())
		# Clears the character from the word NVDA builds for typed word echo; nothing is spoken.
		if isRecent and key.vkCode == VK_BACK and (not inside or claimEcho(key.time)):
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
	elif getattr(focus, "role", None) == _terminalRole():
		character = _typedInTerminal(removed, text, key)
		if character is None or not isOneCharacter(character) or not claimEcho(key.time):
			return
	else:
		text = _typedPart(text, key)
		if text in NOT_ECHOED or not isOneCharacter(text):
			return
		character = text
	# An editor whose paragraph and the run of text in it both report one change echoes it once.
	if inside and not claimEcho(key.time):
		return
	eventHandler.queueEvent("typedCharacter", focus, ch=character)
