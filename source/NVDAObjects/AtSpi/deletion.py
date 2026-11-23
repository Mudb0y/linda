"""Backspace and Delete in an editable AT-SPI object, spoken from the text the application removed.

On Windows NVDA's hook holds Backspace until the script has read the character before the caret, and
only then sends the key on. Here every such key reaches the application as it reaches NVDA, which
has usually deleted the character before the script can read it, so the script would read the one
before that. The application's text-changed event carries what it removed instead, and
linuxPlatform.atspi.typedChars hands each deletion that Backspace or Delete made to echoDeletion.
"""

import braille
import speech
import textInfos
from controlTypes import OutputReason
from linuxPlatform.atspi import typedChars


class DeletionEcho:
	"""Speaks what Backspace and Delete removed once the application says so, rather than reading the
	text around the caret while the key is still on its way."""

	echoesDeletion = True

	def event_gainFocus(self) -> None:
		typedChars.keepText(self)
		super().event_gainFocus()

	def _backspaceScriptHelper(self, unit, gesture) -> None:
		gesture.send()

	def _deleteScriptHelper(self, unit, gesture) -> None:
		gesture.send()

	def echoDeletion(self, backspace: bool, text: str | None) -> None:
		"""Speak what a deletion leaves to be said, as NVDA's scripts do on Windows: the text Backspace
		removed, or the character or word Delete brought to the caret.

		:param backspace: Whether Backspace made the deletion, rather than Delete.
		:param text: What was removed, or None when it must not be read: a password field's.
		"""
		if not backspace:
			oneCharacter = text is None or typedChars.isOneCharacter(text)
			self._speakAtCaret(textInfos.UNIT_CHARACTER if oneCharacter else textInfos.UNIT_WORD)
			braille.handler.handleCaretMove(self)
			return
		if text is None or self.isProtected:
			speech.speakSpelling(typedChars.PROTECTED_CHARACTER)
			return
		chunk = text.replace("\r\n", "\n").replace(typedChars.PARAGRAPH_SEPARATOR, "\n")
		if len(chunk) > 1 and not typedChars.isOneCharacter(chunk):
			speech.speakMessage(chunk)
		else:
			speech.speakSpelling(chunk)

	def _speakAtCaret(self, unit: str) -> None:
		try:
			info = self.makeTextInfo(textInfos.POSITION_CARET)
		except (RuntimeError, NotImplementedError):
			return
		info.expand(unit)
		speech.speakTextInfo(info, unit=unit, reason=OutputReason.CARET)


class JavaTypedOver:
	"""A Java program's text, whose copy typedChars keeps to know what Backspace removed, read again as a
	selection ends.

	The Java ATK wrapper reports a change only as how far the character count has moved since its last
	report, counted when its main loop gets to it rather than when the change is made. Typing over a
	selection removes and inserts at once, and reaches NVDA as nothing when what is typed is as long as
	what it replaced, or as an insert or deletion of the difference; the copy would keep what was
	replaced, and Backspace would say it. Typing over a selection always ends the selection, and the
	wrapper reports the caret's move after the change."""

	def event_caret(self) -> None:
		before = getattr(self, "_lastSelectionPos", None)
		super().event_caret()
		# NVDA's selection detection, run for the focus alone, leaves the selection it found.
		after = getattr(self, "_lastSelectionPos", None)
		ended = after is not None and after is not before and after.isCollapsed
		if ended and before is not None and not before.isCollapsed:
			typedChars.keepText(self)
