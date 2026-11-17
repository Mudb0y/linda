"""Terminals that show their screen over AT-SPI, as VTE does for MATE Terminal and GNOME's terminals,
read as NVDA reads a Windows console: new output spoken as it appears, typing echoed once it shows,
and the screen under the review cursor.

VTE's text is the visible screen only, one line per row and nothing of the scrollback, so new output
is found as NVDA finds it in a legacy console: by comparing the whole screen before and after each
change, line by line. The comparison runs on LiveText's monitor thread as everywhere else, but AT-SPI
is read on the main thread only, so that thread asks the main thread for the screen.

A typed character is never taken from the keyboard here. typedChars echoes what the application puts
on the screen just after a typing key, so that nothing typed at a prompt that does not show it is
spoken: VTE's typedCharacter already means the character has appeared.
"""

import collections
import os
import threading
import time
from difflib import SequenceMatcher

import queueHandler
import textInfos
from diffHandler import Difflib, prefer_difflib
from linuxPlatform import inputHook
from logHandler import log
from NVDAObjects.behaviors import EnhancedTermTypedCharSupport
from speech.extensions import speechCanceled

#: Seconds the monitor thread waits for the main thread to read the screen before it compares against
#: the screen it had, and tries again.
SCREEN_READ_TIMEOUT = 1.0
#: Output spoken within this many seconds of the output before it is one stream: a key that stops
#: speech while a stream flows silences the rest of it.
STREAM_GAP_SECONDS = 0.5
#: Lines spoken at once at most, and how many more each second allows: VTE shows a cat of 50,000
#: lines as a few whole screens, each new to the diff, which would otherwise be read for minutes.
LINE_BUDGET = 30
LINES_PER_SECOND = 4.0
#: Seconds after a caret script spoke a line within which text on that line is not spoken again as
#: output, as when Up recalls a command the shell then draws, or Backspace makes it redraw the rest.
CARET_LINE_SECONDS = 1.5
#: A changed part of a line this long or longer is spoken as its whole line, as NVDA's Difflib does.
WHOLE_LINE_AFTER = 15
#: Rows changed together that are still taken as edits to the rows they replaced.
IN_PLACE_ROWS = 3
#: Seconds after a key that types within which text that appeared just before the caret is the typing
#: itself, which the typed character echo speaks.
TYPING_SECONDS = 0.5
#: Screens kept from before the screen was last replaced, to tell a screen given back from a new one.
SCREENS_KEPT = 4


def changedPart(old: str, new: str) -> str:
	"""What a line edit put into new that old did not have, or the whole of new when that is long.

	NVDA's Difflib speaks a changed line from its first difference to its end, which repeats what a
	shell only moved: Backspace in the middle of a command redraws the rest of it one place left.
	"""
	prefix = len(os.path.commonprefix([old, new]))
	limit = min(len(old), len(new)) - prefix
	suffix = 0
	while suffix < limit and old[-1 - suffix] == new[-1 - suffix]:
		suffix += 1
	changed = new[prefix : len(new) - suffix]
	return changed if len(changed) < WHOLE_LINE_AFTER else new


class ScreenDifflib(Difflib):
	"""NVDA's line diff for a screen: new rows are spoken whole, and of the row being edited at the
	caret only what is new in it.

	A row counts as edited in place when a few rows changed and as many replaced them; NVDA's Difflib
	pairs any similar rows, and so read a screen that scrolled by more than its height, as VTE shows a
	long listing, as numbers cut from the ends of unrelated lines. Away from the caret a row edited in
	place is spoken whole: a full-screen program's status line is news as a line, "1 marked", not "1".
	"""

	def diff(self, newText: str, oldText: str) -> list[str]:
		return self.diffScreens(newText, oldText, caretRow=None)

	def diffScreens(self, newText: str, oldText: str, caretRow: int | None) -> list[str]:
		"""
		:param caretRow: The row of newText the caret is on, or None to take every row edited in place
			as the caret's.
		"""
		oldRows = oldText.splitlines()
		newRows = newText.splitlines()
		outLines = []
		matcher = SequenceMatcher(None, oldRows, newRows, autojunk=False)
		for tag, oldStart, oldEnd, newStart, newEnd in matcher.get_opcodes():
			if tag not in ("insert", "replace"):
				continue
			inPlace = tag == "replace" and oldEnd - oldStart == newEnd - newStart <= IN_PLACE_ROWS
			for offset, row in enumerate(newRows[newStart:newEnd]):
				if inPlace and caretRow in (None, newStart + offset):
					row = changedPart(oldRows[oldStart + offset], row)
				if row and not row.isspace():
					outLines.append(row)
		return outLines


def isNewScreen(oldText: str, newText: str) -> bool:
	"""Whether most rows changed at once, as when a full-screen program starts or ends, or the screen
	scrolled by its height."""
	oldRows = oldText.splitlines()
	newRows = newText.splitlines()
	changed = sum(old != new for old, new in zip(oldRows, newRows)) + abs(len(oldRows) - len(newRows))
	return changed > len(newRows) // 2


_screenDifflib = ScreenDifflib()


class VteTerminal(EnhancedTermTypedCharSupport):
	"""A terminal's screen through the AT-SPI Text interface, an overlay on AtSpi."""

	#: VTE announces one write of a program as several changes; read the screen once after them.
	STABILIZE_DELAY = 0.03
	#: Typed characters arrive only once the screen shows them, so they are never held back for it.
	_supportsTextChange = False
	#: VTE names every terminal "Terminal", which the role already says; a Windows console is unnamed.
	name = ""

	def initOverlayClass(self) -> None:
		self._lastScreen = ""
		self._caretRow: int | None = None
		self._screensBefore: collections.deque[str] = collections.deque(maxlen=SCREENS_KEPT)
		self._lastOutputAt = 0.0
		self._silenced = False
		self._budget = float(LINE_BUDGET)
		self._budgetAt = time.monotonic()
		self._caretLine: tuple[float, str] | None = None

	def _get_diffAlgo(self):
		# Screen-only text, as in a legacy console, which NVDA diffs by line (#12974); the user's choice
		# of the character diff in NVDA's settings still wins.
		algo = prefer_difflib()
		return _screenDifflib if isinstance(algo, Difflib) else algo

	def startMonitoring(self) -> None:
		speechCanceled.register(self._onSpeechCanceled)
		super().startMonitoring()

	def stopMonitoring(self) -> None:
		super().stopMonitoring()
		speechCanceled.unregister(self._onSpeechCanceled)

	def _readScreen(self) -> None:
		"""Read the screen, each row without the spaces VTE leaves where text was erased, and the row of
		the caret; main thread only."""
		text = self.makeTextInfo(textInfos.POSITION_ALL).text
		if not text:
			# A gone or unanswering terminal reads as empty, which would make its whole screen new later.
			return
		try:
			caretOffset = self.makeTextInfo(textInfos.POSITION_CARET)._startOffset
		except RuntimeError:
			caretRow = None
		else:
			# VTE counts code points, as str does.
			caretRow = text.count("\n", 0, caretOffset)
		self._lastScreen = "\n".join(row.rstrip() for row in text.split("\n"))
		self._caretRow = caretRow

	def _getText(self) -> str:
		if threading.current_thread() is threading.main_thread():
			self._readScreen()
			return self._lastScreen
		done = threading.Event()

		def readOnMainThread() -> None:
			try:
				self._readScreen()
			except Exception:
				log.debugWarning("Could not read the terminal's screen", exc_info=True)
			finally:
				done.set()

		queueHandler.queueFunction(queueHandler.eventQueue, readOnMainThread)
		if not done.wait(SCREEN_READ_TIMEOUT):
			# The main thread is busy; this change is compared again once it is free.
			self._event.set()
		return self._lastScreen

	def _calculateNewText(self, newText: str, oldText: str) -> list[str]:
		algo = self.diffAlgo
		caretRow = self._caretRow

		def diff(old: str) -> list[str]:
			if algo is _screenDifflib:
				return algo.diffScreens(newText, old, caretRow)
			return algo.diff(newText, old)

		lines = diff(oldText)
		if isNewScreen(oldText, newText):
			# A full-screen program that ends gives back the screen from before it started, which is not
			# news; only what differs from that screen is. It may be read half drawn on the way, so
			# several screens are kept.
			if len(lines) > 1:
				for before in self._screensBefore:
					fromBefore = diff(before)
					if len(fromBefore) < len(lines):
						lines = fromBefore
			if oldText.strip():
				self._screensBefore.append(oldText)
		return lines

	def _onSpeechCanceled(self) -> None:
		if time.monotonic() - self._lastOutputAt < STREAM_GAP_SECONDS:
			self._silenced = True

	def _reportNewLines(self, lines: list[str]) -> None:
		lines = self._withoutCaretLine(lines)
		# Given nothing, the typed character filter would still forget the word being typed.
		if lines and not self._isTypedText(lines):
			super()._reportNewLines(lines)

	def _isTypedText(self, lines: list[str]) -> bool:
		"""Whether lines are only what was just typed, just before the caret. The screen can show typing
		before its typed character events are handled, which NVDA's own filter relies on."""
		key = inputHook.lastKeyDown()
		if len(lines) != 1 or key is None or not key.typing or time.monotonic() - key.time > TYPING_SECONDS:
			return False
		try:
			caret = self.makeTextInfo(textInfos.POSITION_CARET)
			beforeCaret = caret.copy()
			beforeCaret.expand(textInfos.UNIT_LINE)
			beforeCaret.setEndPoint(caret, "endToStart")
		except (RuntimeError, NotImplementedError):
			return False
		return beforeCaret.text.rstrip().endswith(lines[0].strip())

	def _reportNewText(self, line: str) -> None:
		now = time.monotonic()
		continuing = now - self._lastOutputAt < STREAM_GAP_SECONDS
		self._lastOutputAt = now
		if self._silenced and continuing:
			return
		self._silenced = False
		self._budget = min(LINE_BUDGET, self._budget + (now - self._budgetAt) * LINES_PER_SECOND)
		self._budgetAt = now
		if self._budget < 1:
			log.debug("Terminal output is coming faster than it can be spoken; a line was not spoken")
			return
		self._budget -= 1
		super()._reportNewText(line)

	def _withoutCaretLine(self, lines: list[str]) -> list[str]:
		if self._caretLine is None:
			return lines
		spokenAt, spoken = self._caretLine
		if time.monotonic() - spokenAt > CARET_LINE_SECONDS:
			self._caretLine = None
			return lines
		return [line for line in lines if not (line.strip() and line.strip() in spoken)]

	def _caretScriptPostMovedHelper(self, speakUnit, gesture, info=None) -> None:
		super()._caretScriptPostMovedHelper(speakUnit, gesture, info)
		try:
			line = self.makeTextInfo(textInfos.POSITION_CARET)
			line.expand(textInfos.UNIT_LINE)
		except (RuntimeError, NotImplementedError):
			return
		self._caretLine = (time.monotonic(), line.text.strip())

	def script_flush_queuedChars(self, gesture) -> None:
		super().script_flush_queuedChars(gesture)
		# What Enter brings is output, even where it repeats the command line.
		self._caretLine = None
		# The shell's completion can be a letter or two, which is still worth hearing.
		self._hasTab = gesture.mainKeyName == "tab"
