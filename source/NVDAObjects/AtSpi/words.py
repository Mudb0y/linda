"""Word movement in toolkits whose caret stops at the end of a word on Linux."""

import eventHandler
import speech
import textInfos
from controlTypes import OutputReason
from scriptHandler import isScriptWaiting, willSayAllResume


class EndOfWordMovement:
	"""A field whose Control+Right stops at the end of the word it moves over on Linux (Gecko,
	Chromium/Electron, and GTK), where on Windows it stops at the start of the next word; Control+Left
	stops at the start of a word on both. NVDA reads the word the caret comes to, which on Windows is
	the one Control+Right moved onto; here it is the word the caret passed, after Control+Right as
	before it. Without this, moving over the last word of a line lands past its end and speaks "blank"
	instead of the word (issue #66), and at the end of "isn" of "isn't" in Firefox the word at the
	caret is already "t".
	"""

	def event_gainFocus(self) -> None:
		self._lastWordMovementBookmark = None
		super().event_gainFocus()

	def event_typedCharacter(self, ch: str) -> None:
		self._lastWordMovementBookmark = None
		super().event_typedCharacter(ch=ch)

	def event_textChange(self) -> None:
		self._lastWordMovementBookmark = None
		super().event_textChange()

	def _caretScriptPostMovedHelper(self, speakUnit, gesture, info=None) -> None:
		super()._caretScriptPostMovedHelper(speakUnit, gesture, info)
		if info is not None:
			try:
				self._lastWordMovementBookmark = info.bookmark
			except (RuntimeError, NotImplementedError):
				self._lastWordMovementBookmark = None

	def _caretMovementScriptHelper(self, gesture, unit) -> None:
		if unit != textInfos.UNIT_WORD:
			super()._caretMovementScriptHelper(gesture, unit)
			return
		try:
			before = self.makeTextInfo(textInfos.POSITION_CARET)
		except (RuntimeError, NotImplementedError, LookupError):
			gesture.send()
			return
		bookmark = before.bookmark
		gesture.send()
		moved, info = self._hasCaretMoved(bookmark)
		if not moved and self.shouldFireCaretMovementFailedEvents:
			eventHandler.executeEvent("caretMovementFailed", self, gesture=gesture)
		# On Linux the arrow key reaches the application in parallel with NVDA. In editors whose caret
		# move is detected by bookmark alone (such as MonacoEditor), the caret may already have moved
		# before `before` was read; compare against the last caret position seen by a caret script.
		alreadyMoved = False
		if (
			not moved
			and info is not None
			and not self.caretMovementDetectionUsesEvents
			and getattr(self, "_lastWordMovementBookmark", None) is not None
		):
			try:
				alreadyMoved = info.bookmark != self._lastWordMovementBookmark
			except (RuntimeError, NotImplementedError):
				alreadyMoved = False
		passed = (
			_wordPassed(info)
			if (moved or alreadyMoved) and info is not None and _forward(gesture, before, info)
			else None
		)
		if passed is None:
			self._caretScriptPostMovedHelper(unit, gesture, info)
			return
		# The review cursor and braille follow the caret; the word passed is spoken.
		self._caretScriptPostMovedHelper(None, gesture, info)
		if not isScriptWaiting() and not willSayAllResume(gesture):
			speech.speakTextInfo(passed, unit=unit, reason=OutputReason.CARET)


#: Backwards-compatible alias for earlier callers.
GeckoWordMovement = EndOfWordMovement


def _wordPassed(caret: textInfos.TextInfo) -> textInfos.TextInfo | None:
	"""The word holding the character before caret, which a forward word move has passed; None when that
	character is a space or ends a line, as when the caret has moved onto a line or past spaces to the
	start of a word, where the word at the caret is the one to read."""
	before = caret.copy()
	before.collapse()
	if before.move(textInfos.UNIT_CHARACTER, -1) == 0:
		return None
	before.expand(textInfos.UNIT_CHARACTER)
	if not before.text or before.text.isspace():
		return None
	before.expand(textInfos.UNIT_WORD)
	return before


def _forward(gesture, before: textInfos.TextInfo, after: textInfos.TextInfo) -> bool:
	"""Whether the word move went forward. The key reaches Firefox as it reaches NVDA, so the caret has
	often moved already when the script reads where it was: where it was and where it is are the same
	then, and the key tells, Control+Right being forward."""
	moved = after.compareEndPoints(before, "startToStart")
	if moved:
		return moved > 0
	return getattr(gesture, "mainKeyName", None) == "rightArrow"
