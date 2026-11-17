"""Terminals that show their screen over AT-SPI, as VTE does for MATE Terminal and GNOME's terminals,
read as NVDA reads a Windows console: new output spoken as it appears, typing echoed once it shows,
and the screen under the review cursor.

VTE's text is the visible screen only, one line per row and nothing of the scrollback, and it says
only that something changed. So the screen is read again after each change and compared with the
screen before, row by row, as NVDA compares a legacy console's; the comparison runs on LiveText's
monitor thread, but AT-SPI is read on the main thread only, so that thread is asked for the screen.

Programs that draw a text interface rather than print lines erase and rewrite a region at the bottom
of the screen many times a second, so a row is followed through the rewrites, and what each row has
already said is kept: a row that grows is held until it ends or pauses and then only its new part is
spoken, a row that changes in place in small parts again and again is animation and never spoken, a
mark moved from one row to another speaks the row it moved to, and rows under the line being typed
in are the program's own furniture.

A typed character is never taken from the keyboard here. typedChars echoes what the application puts
on the screen just after a typing key, so that nothing typed at a prompt that does not show it is
spoken.
"""

import collections
import threading
import time
import unicodedata
from dataclasses import dataclass, field, replace
from difflib import SequenceMatcher

import api
import config
import queueHandler
import speech
import textInfos
from diffHandler import prefer_difflib
from linuxPlatform import inputHook
from logHandler import log
from scriptHandler import isScriptWaiting
from speech.extensions import speechCanceled

from NVDAObjects.AtSpi.textInfo import AtspiTextInfo
from NVDAObjects.behaviors import EnhancedTermTypedCharSupport, LiveText

#: Seconds the monitor thread waits for the main thread to read the screen before it compares against
#: the screen it had, and tries again.
SCREEN_READ_TIMEOUT = 1.0
#: Output spoken within this many seconds of the output before it is one stream: a key that stops
#: speech while a flood flows silences the rest of it.
STREAM_GAP_SECONDS = 0.5
#: Rows of new output a second that make a flood.
FLOOD_ROWS_PER_SECOND = 12
#: Lines spoken at once at most, or the screen's height when that is more, and how many more each
#: second allows: VTE shows a cat of 50,000 lines as a few whole screens, each new, which would
#: otherwise be read for minutes.
LINE_BUDGET = 30
LINES_PER_SECOND = 4.0
#: Rows changed together that are still taken as edits to the rows they replaced.
IN_PLACE_ROWS = 3
#: Rows replaced together that may still be the same rows changed.
PAIRED_ROWS = 6
#: Screens kept from before the screen was last replaced, to tell a screen given back from a new one.
SCREENS_KEPT = 4
#: Seconds a growing row may rest before what it has is spoken up to its last whole word, and before
#: all of it is.
PAUSE_SECONDS = 0.15
LONG_PAUSE_SECONDS = 0.5
#: Seconds new text waits at most before its whole words are spoken while its row still grows, when
#: nothing is being said, and while earlier output may still be being said.
FIRST_WAIT_SECONDS = 0.25
LATER_WAIT_SECONDS = 1.0
#: Seconds a voice at its default rate takes over a character, roughly.
SECONDS_PER_CHARACTER = 0.06
#: Seconds a row rewritten in place without a key waits, to see whether it is animation.
EDIT_HOLD_SECONDS = 0.6
#: Rewrites in place within this many seconds that make a row animation, and how long a row stays
#: animation after its last.
ANIMATION_WINDOW_SECONDS = 2.0
ANIMATION_DECAY_SECONDS = 2.0
#: A rewrite in place this many characters long or shorter is a small part of its row.
SMALL_EDIT = 12
#: Seconds after a key within which a change at the caret, or in the line typed in, answers it.
KEY_SECONDS = 0.5
#: Seconds a row that vanished is remembered, so that a region erased and written again is not news,
#: and how many are.
VANISHED_SECONDS = 3.0
VANISHED_KEPT = 64
#: Seconds after a key before its character, seen in the line typed in, is echoed by the terminal
#: itself, when VTE's report of it did not echo it.
ECHO_FALLBACK_SECONDS = 0.12
#: Caret positions kept, to tell where the caret was when a key went down.
CARET_HISTORY = 16
#: Prompt characters of a line typed in, which reading that line leaves out.
PROMPT_MARKS = frozenset(">\u276f\u203a\u00bb\u2192")

#: Windows virtual-key codes the tracker tells apart.
_KEY_KINDS = {
	0x08: "backspace",
	0x09: "tab",
	0x0D: "enter",
	0x1B: "escape",
	0x21: "pageUp",
	0x22: "pageDown",
	0x23: "end",
	0x24: "home",
	0x25: "left",
	0x26: "up",
	0x27: "right",
	0x28: "down",
	0x2E: "delete",
}
_MODIFIER_KEYS = frozenset({0x10, 0x11, 0x12, 0x14, 0x2D, 0x5B, 0x5C, 0x90, *range(0xA0, 0xA6)})


def isBoxCharacter(ch: str) -> bool:
	"""Box drawing and block elements, which frame and rule a text interface."""
	return "\u2500" <= ch <= "\u259f"


def isMark(ch: str) -> bool:
	"""A symbol a text interface marks a row with: a bullet, an arrow, a pointer, a spinner. Check boxes
	and ticks are not: they say whether something is done."""
	if "\u2610" <= ch <= "\u2612" or "\u2713" <= ch <= "\u2718":
		return False
	return ch in "\u2022\u00b7\u2219" or "\u2190" <= ch <= "\u2bff" or unicodedata.category(ch) == "So"


def isRule(text: str) -> bool:
	"""Whether text is only framing or a rule: box drawing, or a run of dashes, equals or underscores."""
	stripped = text.strip()
	if not stripped:
		return True
	if all(isBoxCharacter(ch) or ch == " " for ch in stripped):
		return True
	return len(stripped) >= 3 and len(set(stripped)) == 1 and stripped[0] in "-=_~*"


def stripFrame(text: str) -> str:
	"""text without the box drawing and the spaces at its two ends."""
	start, end = 0, len(text)
	while start < end and (isBoxCharacter(text[start]) or text[start].isspace()):
		start += 1
	while end > start and (isBoxCharacter(text[end - 1]) or text[end - 1].isspace()):
		end -= 1
	return text[start:end]


def stripMarks(text: str) -> str:
	"""text without the marks and spaces it starts with."""
	start = 0
	while start < len(text) and (isMark(text[start]) or text[start].isspace()):
		start += 1
	return text[start:]


def cleanLine(text: str, rowStart: bool = True) -> str:
	"""What is worth hearing of text from a row: no framing, no rule, and, at the start of a row, no
	marks and no prompt character."""
	if isRule(text):
		return ""
	text = stripFrame(text)
	if rowStart:
		text = stripMarks(text)
		if text[:1] in PROMPT_MARKS and text[1:2] in ("", " "):
			text = text[2:].lstrip()
	return "" if isRule(text) else text


def collapsed(text: str) -> str:
	"""text's words, for telling whether two rows are one row changed; padding inside a box would
	otherwise make any two of its rows look alike."""
	return " ".join(stripFrame(text).split())


def changedSpans(old: str, new: str) -> tuple[str, str, int]:
	"""What old lost and new gained, each run together, and where in new the first change is."""
	removed, added, at = [], [], None
	for tag, i1, i2, j1, j2 in SequenceMatcher(None, old, new, autojunk=False).get_opcodes():
		if tag == "equal":
			continue
		if at is None:
			at = j1
		removed.append(old[i1:i2])
		added.append(new[j1:j2])
	return "".join(removed), "".join(added), at if at is not None else len(new)


def isSmallEdit(old: str, new: str) -> bool:
	"""Whether new is old with a few characters rewritten in place, as a spinner or a counter changes."""
	removed, added, _at = changedSpans(old, new)
	return len(removed.strip()) + len(added.strip()) <= SMALL_EDIT


def sameRow(old: str, new: str) -> bool:
	"""Whether new is old changed, rather than another row."""
	if not old.strip() or not new.strip():
		return False
	if new.startswith(old) or old.startswith(new):
		return True
	a, b = collapsed(old), collapsed(new)
	if not a or not b:
		return False
	if a.startswith(b) or b.startswith(a):
		return True
	return SequenceMatcher(None, a, b, autojunk=False).ratio() >= 0.6


def markZone(text: str) -> tuple[str, str, str]:
	"""text as its framing, the marks and spaces before its first letter or digit, and the rest."""
	start = 0
	while start < len(text) and isBoxCharacter(text[start]):
		start += 1
	end = start
	while end < len(text) and not text[end].isalnum():
		end += 1
	return text[:start], text[start:end], text[end:]


def isNewScreen(oldRows: list[str], newRows: list[str]) -> bool:
	"""Whether most rows with text changed at once, as when a full-screen program starts or ends, or the
	screen scrolled by its height."""
	rows = max(len(oldRows), len(newRows))
	oldRows = oldRows + [""] * (rows - len(oldRows))
	newRows = newRows + [""] * (rows - len(newRows))
	changed = sum(old != new for old, new in zip(oldRows, newRows))
	withText = sum(bool(old or new) for old, new in zip(oldRows, newRows))
	return changed > IN_PLACE_ROWS and changed > withText // 2


def inputContentStart(text: str) -> int:
	"""Where what is typed starts in a row typed in: after its framing and its prompt character."""
	start = 0
	while start < len(text) and (isBoxCharacter(text[start]) or text[start] == " "):
		start += 1
	if start < len(text) and text[start] in PROMPT_MARKS and text[start + 1 : start + 2] in ("", " "):
		start = min(start + 2, len(text))
	return start


def looksTypedIn(text: str, above: str) -> bool:
	"""Whether a row is a text interface's empty line to type in: a prompt character alone in a box, or
	under a rule. A menu's chosen option starts with the same character, followed by the option."""
	start = 0
	while start < len(text) and (isBoxCharacter(text[start]) or text[start] == " "):
		start += 1
	if text[start : start + 1] not in PROMPT_MARKS or text[start + 1 : start + 2] not in ("", " "):
		return False
	if text[start + 1 : inputContentEnd(text)].strip():
		return False
	return isBoxCharacter(text[0]) or (bool(above.strip()) and isRule(above))


def inputContentEnd(text: str) -> int:
	end = len(text)
	while end > 0 and (isBoxCharacter(text[end - 1]) or text[end - 1].isspace()):
		end -= 1
	return end


@dataclass
class Key:
	"""The last key that reached the application, as the tracker tells keys apart."""

	kind: str
	text: str
	time: float


def lastKey() -> Key | None:
	event = inputHook.lastKeyDown()
	if event is None or not event.reachedApplication:
		return None
	if event.vkCode in _MODIFIER_KEYS:
		kind = "modifier"
	elif event.typing and event.text:
		kind = "typing"
	else:
		kind = _KEY_KINDS.get(event.vkCode, "other")
	return Key(kind, event.text, event.time)


@dataclass
class Row:
	"""One row of the screen, followed as it moves and changes."""

	text: str
	changedAt: float
	#: How much of text, from its start, has been spoken or needs no speaking.
	heard: str = ""
	#: When text that is not heard began to wait.
	pendingSince: float | None = None
	#: Not spoken before this time, while it shows whether it is animation.
	holdUntil: float = 0.0
	edits: list[float] = field(default_factory=list)
	animatedUntil: float = 0.0
	#: The line typed in: typing appeared in it, or it is a prompt character alone in a box.
	isInput: bool = False
	#: Whether the terminal's own cursor was on it as it was typed in, as at a shell's prompt.
	inputAtCaret: bool = False
	#: Whether text was added to its end since it appeared, so that its last word may be unfinished.
	grew: bool = False

	def pending(self) -> str:
		if self.text.startswith(self.heard):
			return self.text[len(self.heard) :]
		return self.text

	def hear(self) -> None:
		self.heard = self.text
		self.pendingSince = None


class ScreenTracker:
	"""What each row of a terminal's screen has said, through the screen's changes; it decides what is
	new output and when to speak it.

	Each method is called with the time, so that the rules can be followed without a clock.
	"""

	def __init__(self) -> None:
		self.rows: list[Row] = []
		self.caret: tuple[int, int] | None = None
		#: Each row's text and what of it was heard, for screens from before the screen was replaced.
		self.screensBefore: collections.deque[dict[str, str]] = collections.deque(maxlen=SCREENS_KEPT)
		self.vanished: list[tuple[float, Row]] = []
		self.outputTimes: collections.deque[float] = collections.deque(maxlen=200)
		self.lastOutputAt = 0.0
		#: When what was last spoken may have been said, at about the rate of a voice at its default.
		self.speakingUntil = 0.0
		self.silenced = False
		#: Lines to speak at once, ahead of held output.
		self.immediate: list[str] = []
		#: The column the program's own cursor is in, in the line typed in.
		self.inputColumn: int | None = None
		#: The last typing key whose character showed in the line typed in.
		self.typedKey: Key | None = None

	def reset(self, texts: list[str], caret: tuple[int, int] | None, now: float) -> None:
		"""Start from a screen, all of it heard, as when the terminal gains the focus."""
		self.rows = [Row(text.rstrip(), now, heard=text.rstrip()) for text in texts]
		self.caret = caret
		typedIn = [
			index
			for index in range(len(self.rows))
			if looksTypedIn(self.rows[index].text, self.rows[index - 1].text if index else "")
		]
		if typedIn:
			self.rows[typedIn[-1]].isInput = True
			self.inputColumn = inputContentEnd(self.rows[typedIn[-1]].text)

	def inputRow(self) -> int | None:
		for index in range(len(self.rows) - 1, -1, -1):
			if self.rows[index].isInput:
				return index
		return None

	def isCaretParked(self) -> bool:
		"""Whether the terminal's cursor waits at the start of an empty row under everything, where a
		program that hides it and draws its own leaves it."""
		if self.caret is None:
			return False
		row, column = self.caret
		if column != 0 or row >= len(self.rows) or row == 0:
			return False
		return all(not r.text.strip() for r in self.rows[row:]) and any(
			r.text.strip() for r in self.rows[:row]
		)

	# Changes

	def update(self, texts: list[str], caret: tuple[int, int] | None, now: float, key: Key | None) -> None:
		texts = [text.rstrip() for text in texts]
		old = self.rows
		oldTexts = [row.text for row in old]
		self.caret = caret
		if texts == oldTexts:
			return
		restored: dict[str, str] = {}
		if isNewScreen(oldTexts, texts):
			restored = self._restored(texts)
			if any(text.strip() for text in oldTexts):
				self.screensBefore.append({row.text: row.heard for row in old if row.text.strip()})
		rows: list[Row | None] = [None] * len(texts)
		edited: list[tuple[int, str]] = []
		created: list[int] = []
		matcher = SequenceMatcher(None, oldTexts, texts, autojunk=False)
		for tag, i1, i2, j1, j2 in matcher.get_opcodes():
			if tag == "equal":
				rows[j1:j2] = old[i1:i2]
				continue
			used: set[int] = set()
			start = i1
			# Many rows replaced at once scrolled or were redrawn, as a listing or a new screen does;
			# rows changed in place come a few at a time.
			pairing = i2 - i1 <= PAIRED_ROWS and j2 - j1 <= PAIRED_ROWS
			for j in range(j1, j2):
				match = next(
					(i for i in range(start, i2) if pairing and sameRow(old[i].text, texts[j])), None
				)
				if match is None:
					rows[j] = self._newRow(texts[j], now, restored, texts[j - 1] if j else "")
					if texts[j].strip():
						created.append(j)
					continue
				used.add(match)
				start = match + 1
				row = old[match]
				rows[j] = row
				edited.append((j, row.text))
				self._edit(row, texts[j], j, now, key, restored)
			for i in range(i1, i2):
				if i not in used:
					self._vanish(old[i], now)
		self.rows = rows
		self._afterEdits(edited, created, now, key)

	def _restored(self, texts: list[str]) -> dict[str, str]:
		"""What was heard of the rows of an earlier screen, when it is that screen given back, as when a
		full-screen program ends, or a region erased and written again is read in between: most of that
		screen's rows with text are back."""
		shown = {text for text in texts if text.strip()}
		for before in reversed(self.screensBefore):
			back = len(before.keys() & shown)
			if back and back * 10 >= len(before) * 6:
				return before
		return {}

	def _vanish(self, row: Row, now: float) -> None:
		if row.heard.strip() or row.animatedUntil > now or row.isInput:
			self.vanished.append((now, replace(row, edits=list(row.edits))))
			# A listing scrolls a screenful away many times a second.
			del self.vanished[:-VANISHED_KEPT]

	def _fromVanished(self, text: str, now: float) -> Row | None:
		"""The row that vanished lately which text is, written again: the same, grown, the same animation
		a step on, or one whose words so far it starts with. A program that erases its region and
		writes it again can be read between the two."""
		self.vanished = [(at, row) for at, row in self.vanished if now - at <= VANISHED_SECONDS]

		def rank(row: Row) -> int:
			if row.text == text:
				return 4
			if row.text.strip() and text.startswith(row.text) and _endsWord(text, len(row.text)):
				return 3
			if row.animatedUntil > now and isSmallEdit(row.text, text):
				return 2
			heard = row.heard
			# Whole words only: "line 1" said is not the start of "line 18664".
			if heard.strip() and text.startswith(heard) and _endsWord(text, len(heard)):
				return 1
			return 0

		ranked = [(rank(row), index) for index, (_at, row) in enumerate(self.vanished)]
		best = max(ranked, default=(0, -1))
		if not best[0]:
			return None
		return self.vanished.pop(best[1])[1]

	def _newRow(self, text: str, now: float, restored: dict[str, str], above: str = "") -> Row:
		row = Row(text, now)
		if not text.strip() or isRule(text):
			row.hear()
			return row
		before = self._fromVanished(text, now)
		if before is not None:
			grown = before.text != text
			row = replace(before, text=text, edits=before.edits)
			if row.animatedUntil > now:
				row.animatedUntil = now + ANIMATION_DECAY_SECONDS
				row.hear()
				return row
			if not text.startswith(row.heard):
				row.heard = ""
			if grown:
				row.changedAt, row.grew = now, True
			if row.pending() and row.pendingSince is None:
				row.pendingSince = now
			return row
		if text in restored:
			row.heard = restored[text]
		if looksTypedIn(text, above):
			row.isInput = True
			row.hear()
			self.inputColumn = inputContentEnd(text)
			return row
		if row.pending():
			row.pendingSince = now
		return row

	def _answersKey(self, index: int, row: Row, old: str, new: str, key: Key | None, now: float) -> bool:
		"""Whether a change to a row is the program showing an edit the user made at the caret, which
		typing echo and the caret commands speak."""
		if key is None or now - key.time > KEY_SECONDS or key.kind in ("enter", "tab", "modifier"):
			return False
		removed, added, _at = changedSpans(old, new)
		typed = key.text.strip()
		typedNow = added.strip()
		isTyping = (
			key.kind == "typing"
			and typed
			and not removed.strip()
			and typedNow.endswith(typed)
			and len(typedNow) <= 4
		)
		if isTyping:
			self.typedKey = key
		if self.caret is not None and index == self.caret[0] and not self.isCaretParked():
			if isTyping:
				row.isInput = row.inputAtCaret = True
			return True
		if row.isInput:
			if key.kind == "typing":
				self.inputColumn = self._typedColumn(old, new, added)
			elif key.kind in ("up", "down"):
				self.inputColumn = inputContentEnd(new)
				content = new[inputContentStart(new) : inputContentEnd(new)]
				# Nothing else says what history put in the line typed in while the cursor is drawn.
				if content.strip():
					self.immediate.append(content)
			return True
		if isTyping:
			for other in self.rows:
				other.isInput = False
			row.isInput = True
			self.inputColumn = None
			self.inputColumn = self._typedColumn(old, new, added)
			return True
		return False

	def _typedColumn(self, old: str, new: str, added: str) -> int:
		"""Where the cursor is after typing changed old into new: after what was typed at the cursor, or,
		where that does not fit, after the first difference. A letter typed beside the same letter
		could otherwise be either."""
		typed = added.strip()
		column = self.inputColumn
		if column is not None and new.startswith(old[:column] + typed):
			return column + len(typed)
		return len(_commonPrefix(old, new)) + len(typed)

	def _edit(
		self, row: Row, text: str, index: int, now: float, key: Key | None, restored: dict[str, str]
	) -> None:
		old = row.text
		row.text = text
		row.changedAt = now
		if not text.strip():
			self._vanish(replace(row, text=old, edits=list(row.edits)), now)
			row.heard, row.pendingSince, row.isInput, row.inputAtCaret = "", None, False, False
			row.animatedUntil, row.edits, row.grew = 0.0, [], False
			return
		if text in restored:
			row.heard = restored[text]
			row.pendingSince = now if row.pending() else None
			return
		if self._answersKey(index, row, old, text, key, now):
			row.hear()
			return
		if row.animatedUntil > now:
			row.animatedUntil = now + ANIMATION_DECAY_SECONDS
			row.hear()
			return
		keyed = key is not None and now - key.time <= KEY_SECONDS and key.kind not in ("enter", "modifier")
		if text.startswith(old):
			# A completion that Tab added is whole at once, however short.
			row.grew = row.grew or not (keyed and key.kind == "tab")
			if not text.startswith(row.heard):
				row.heard = ""
			if row.pendingSince is None:
				row.pendingSince = now
			return
		row.edits = [at for at in row.edits if now - at <= ANIMATION_WINDOW_SECONDS]
		if old.startswith(text):
			# Shorter: wrapped again, or something deleted.
			if not text.startswith(row.heard):
				row.heard = text[: len(_commonPrefix(row.heard, text))]
			if not keyed:
				row.edits.append(now)
			if not row.pending():
				row.pendingSince = None
			return
		small = isSmallEdit(old, text)
		if not keyed:
			row.edits.append(now)
		if small and (row.pendingSince is not None or len(row.edits) >= 2):
			# A row that changes in small parts before it could be read, or again and again.
			row.animatedUntil = now + ANIMATION_DECAY_SECONDS
			row.hear()
			return
		# Rewritten: as a status line, the whole row is the news.
		row.heard = ""
		row.pendingSince = now
		row.holdUntil = now + (PAUSE_SECONDS if keyed else EDIT_HOLD_SECONDS)

	def _afterEdits(
		self, edited: list[tuple[int, str]], created: list[int], now: float, key: Key | None
	) -> None:
		# A mark moved from one row to another, as in a menu: the row it moved to is spoken, alone.
		gained, lost = [], []
		for index, old in edited:
			new = self.rows[index].text
			oldFrame, oldMarks, oldRest = markZone(old)
			newFrame, newMarks, newRest = markZone(new)
			if oldFrame != newFrame or oldRest != newRest or len(oldMarks) != len(newMarks):
				continue
			if newMarks.strip() and not oldMarks.strip():
				gained.append(index)
			elif oldMarks.strip() and not newMarks.strip():
				lost.append(index)
		if gained and lost:
			for index in lost + gained:
				row = self.rows[index]
				row.hear()
				row.animatedUntil = 0.0
			caretRow = self.caret[0] if self.caret is not None and not self.isCaretParked() else None
			for index in gained:
				# The caret commands read a mark the program also puts the cursor on.
				if index != caretRow:
					self.immediate.append(cleanLine(self.rows[index].text))
		inputRow = self.inputRow()
		if (
			inputRow is not None
			and self.rows[inputRow].inputAtCaret
			and key is not None
			and key.kind == "enter"
			and now - key.time <= KEY_SECONDS
			and self.caret is not None
			and self.caret[0] > inputRow
		):
			# Enter took the cursor on under the line typed in: it was a command line, run.
			self.rows[inputRow].isInput = self.rows[inputRow].inputAtCaret = False
			self.inputColumn = None
		newInput = [index for index in created if self.rows[index].isInput]
		if newInput:
			for index, row in enumerate(self.rows):
				row.isInput = index == newInput[-1]
		elif self.inputRow() is None:
			# A line typed in that was drawn while a redraw was being read.
			for index in range(len(self.rows) - 1, -1, -1):
				if looksTypedIn(self.rows[index].text, self.rows[index - 1].text if index else ""):
					self.rows[index].isInput = True
					self.inputColumn = inputContentEnd(self.rows[index].text)
					break

	# Speaking

	def isFlooding(self, now: float) -> bool:
		return sum(1 for at in self.outputTimes if now - at <= 1.0) >= FLOOD_ROWS_PER_SECOND

	def cancel(self, at: float, now: float) -> None:
		"""Speech was stopped by a key that went down at at: what waited since before it is dropped, and a
		flood stays silent until it pauses."""
		self.immediate.clear()
		for row in self.rows:
			if row.pending() and (row.pendingSince is None or row.pendingSince < at):
				row.hear()
		if self.isFlooding(now):
			self.silenced = True

	def due(self, now: float) -> list[str]:
		"""The lines to speak now, each marked heard."""
		if self.silenced and now - self.lastOutputAt >= STREAM_GAP_SECONDS:
			self.silenced = False
		answers, self.immediate = self.immediate, []
		lines = []
		inputRow = self.inputRow()
		# While earlier output is still being said there is time to let a phrase grow.
		waitLimit = LATER_WAIT_SECONDS if now < self.speakingUntil else FIRST_WAIT_SECONDS
		for index, row in enumerate(self.rows):
			pending = row.pending()
			if not pending:
				continue
			if row.animatedUntil > now or (inputRow is not None and index > inputRow):
				row.hear()
				continue
			if not cleanLine(pending, rowStart=not row.heard):
				row.hear()
				continue
			if now < row.holdUntil:
				break
			quiet = now - row.changedAt
			if (
				quiet >= LONG_PAUSE_SECONDS
				or self._movedOn(index, now)
				or quiet >= PAUSE_SECONDS
				and (not row.grew or pending.rstrip()[-1:] in ".!?:;)")
			):
				spoken = pending
			elif quiet >= PAUSE_SECONDS or now - (row.pendingSince or now) >= waitLimit:
				cut = pending.rfind(" ")
				if cut <= 0:
					break
				spoken = pending[: cut + 1]
			else:
				break
			text = cleanLine(spoken, rowStart=not row.heard)
			row.heard += spoken
			row.pendingSince = now if row.pending() else None
			if text:
				lines.append(text)
			if row.pending():
				break
		if lines:
			# Output that would be said counts towards a flood, said or not.
			self.outputTimes.extend([now] * len(lines))
			self.lastOutputAt = now
		if self.silenced:
			lines = []
		lines = answers + lines
		if lines:
			self.speakingUntil = (
				max(self.speakingUntil, now) + sum(len(line) for line in lines) * SECONDS_PER_CHARACTER
			)
		return lines

	def _movedOn(self, index: int, now: float) -> bool:
		"""Whether text was written under a row since it last changed: it has ended."""
		changedAt = self.rows[index].changedAt
		inputRow = self.inputRow()
		for below, row in enumerate(self.rows[index + 1 :], index + 1):
			if inputRow is not None and below >= inputRow:
				break
			if row.changedAt >= changedAt and row.animatedUntil <= now and not isRule(row.text):
				return True
		return False

	def hasPending(self) -> bool:
		return bool(self.immediate) or any(row.pending() for row in self.rows)


def _endsWord(text: str, length: int) -> bool:
	"""Whether text's first length characters end at a word's end."""
	return text[length - 1 : length].isspace() or text[length : length + 1] in ("", " ")


def _commonPrefix(a: str, b: str) -> str:
	length = 0
	while length < min(len(a), len(b)) and a[length] == b[length]:
		length += 1
	return a[:length]


class VteTextInfo(AtspiTextInfo):
	"""A terminal's text, with the caret the program draws when it hides the terminal's own, and rows
	read without their framing."""

	def _getCaretOffset(self) -> int:
		offset = self.obj._drawnCaretOffset()
		return offset if offset is not None else super()._getCaretOffset()

	def getTextWithFields(self, formatConfig: dict | None = None) -> textInfos.TextInfo.TextWithFieldsT:
		fields = super().getTextWithFields(formatConfig)
		if self._endOffset - self._startOffset <= 1:
			return fields
		strings = [index for index, item in enumerate(fields) if isinstance(item, str)]
		if not strings:
			return fields
		first, last = strings[0], strings[-1]
		rowStart = self._startOffset in self.obj._rowStarts
		promptEnd = self.obj._inputPromptEnd(self._startOffset)
		if promptEnd is not None and len(fields[first]) >= promptEnd:
			fields[first] = fields[first][promptEnd:]
		head = fields[first]
		trimmed = head.lstrip()
		while trimmed and (isBoxCharacter(trimmed[0]) or (rowStart and isMark(trimmed[0]))):
			trimmed = trimmed[1:].lstrip()
		fields[first] = trimmed if trimmed != head.lstrip() else head
		tail = fields[last]
		end = len(tail.rstrip("\n").rstrip())
		cut = end
		while cut > 0 and (isBoxCharacter(tail[cut - 1]) or tail[cut - 1].isspace()):
			cut -= 1
		if cut < end:
			fields[last] = tail[:cut] + tail[len(tail.rstrip("\n")) :]
		return fields


class VteTerminal(EnhancedTermTypedCharSupport):
	"""A terminal's screen through the AT-SPI Text interface, an overlay on AtSpi."""

	#: VTE announces one write of a program as several changes; read the screen once after them.
	STABILIZE_DELAY = 0.03
	#: Typed characters arrive only once the screen shows them, so they are never held back for it.
	_supportsTextChange = False
	#: VTE names every terminal "Terminal", which the role already says; a Windows console is unnamed.
	name = ""
	TextInfo = VteTextInfo

	def initOverlayClass(self) -> None:
		self._tracker = ScreenTracker()
		self._lock = threading.Lock()
		self._rawRows: list[str] = []
		self._rowStarts: frozenset[int] = frozenset()
		self._lastText = ""
		self._caretOffset: int | None = None
		self._cancelledAt = 0.0
		self._budget = float(LINE_BUDGET)
		self._budgetAt = time.monotonic()
		self._caretHistory: collections.deque[tuple[float, int]] = collections.deque(maxlen=CARET_HISTORY)
		self._screenHistory: collections.deque[tuple[float, str, int | None]] = collections.deque(
			maxlen=CARET_HISTORY
		)

	def event_caret(self) -> None:
		try:
			self._caretHistory.append(
				(time.monotonic(), AtspiTextInfo(self, textInfos.POSITION_CARET)._startOffset)
			)
		except RuntimeError:
			pass
		super().event_caret()

	def _get_caretMovementDetectionUsesEvents(self) -> bool:
		# VTE says where its cursor went within a few milliseconds of a program moving it.
		return True

	def _beforeKey(self) -> tuple[str, int] | None:
		"""The screen and the caret offset as they were when the last key went down.

		The key reaches the terminal as it reaches NVDA, so by the time a script runs the program may
		have moved the caret and changed the line already, where on Windows NVDA's hook holds the key
		until the script sends it on.
		"""
		key = inputHook.lastKeyDown()
		if key is None:
			return None
		screens = [screen for screen in self._screenHistory if screen[0] < key.time]
		if not screens:
			return None
		readAt, text, caret = screens[-1]
		carets = [offset for at, offset in self._caretHistory if readAt <= at < key.time]
		if carets:
			caret = carets[-1]
		return None if caret is None else (text, caret)

	def _hasCaretMoved(self, bookmark, retryInterval=0.01, timeout=None, origWord=None):
		before = self._beforeKey()
		if before is None or self._cursorIsDrawn():
			return super()._hasCaretMoved(bookmark, retryInterval, timeout, origWord)
		if timeout is None:
			timeout = config.conf["editableText"]["caretMoveTimeoutMs"] / 1000
		deadline = time.monotonic() + timeout
		while True:
			if isScriptWaiting():
				return (False, None)
			try:
				info = self.makeTextInfo(textInfos.POSITION_CARET)
			except (RuntimeError, NotImplementedError):
				return (False, None)
			if info._startOffset != before[1]:
				return (True, info)
			if time.monotonic() >= deadline:
				return (False, info)
			api.processPendingEvents(processEventQueue=False)
			time.sleep(retryInterval)

	def _caretScriptPostMovedHelper(self, speakUnit, gesture, info=None) -> None:
		if speakUnit == textInfos.UNIT_CHARACTER and info is not None and self._isPastTyping(info):
			# Past what is typed there is only the row's end or a box's padding, which in an edit field is
			# the end of the line.
			super()._caretScriptPostMovedHelper(None, gesture, info)
			speech.speakMessage(_("blank"))
			return
		super()._caretScriptPostMovedHelper(speakUnit, gesture, info)

	def _isPastTyping(self, info) -> bool:
		"""Whether nothing but the row's end, or the padding of a box, follows the caret on its row."""
		rest = info.copy()
		try:
			rest.expand(textInfos.UNIT_LINE)
			rest.setEndPoint(info, "startToStart")
			text = rest.text
		except (RuntimeError, NotImplementedError, LookupError):
			return False
		if not text.strip("\n"):
			return True
		return any(isBoxCharacter(ch) for ch in text) and not stripFrame(text)

	def _get_diffAlgo(self):
		# Screen-only text, as in a legacy console, which NVDA diffs by line (#12974).
		return prefer_difflib()

	def startMonitoring(self) -> None:
		speechCanceled.register(self._onSpeechCanceled)
		super().startMonitoring()

	def stopMonitoring(self) -> None:
		super().stopMonitoring()
		speechCanceled.unregister(self._onSpeechCanceled)

	# The screen

	def _readScreen(self) -> None:
		"""Read the screen and the caret; main thread only."""
		info = AtspiTextInfo(self, textInfos.POSITION_ALL)
		text = info.text
		if not text:
			# A gone or unanswering terminal reads as empty, which would make its whole screen new later.
			return
		try:
			caretOffset = AtspiTextInfo(self, textInfos.POSITION_CARET)._startOffset
		except RuntimeError:
			caretOffset = None
		self._lastText = text
		self._caretOffset = caretOffset
		self._screenHistory.append((time.monotonic(), text, caretOffset))

	def _getScreen(self) -> tuple[str, int | None]:
		if threading.current_thread() is threading.main_thread():
			self._readScreen()
			return self._lastText, self._caretOffset
		done = threading.Event()

		def readOnMainThread() -> None:
			try:
				self._readScreen()
			except (LookupError, RuntimeError, NotImplementedError):
				log.debugWarning("Could not read the terminal's screen", exc_info=True)
			finally:
				done.set()

		queueHandler.queueFunction(queueHandler.eventQueue, readOnMainThread)
		if not done.wait(SCREEN_READ_TIMEOUT):
			# The main thread is busy; this change is compared again once it is free.
			self._event.set()
		return self._lastText, self._caretOffset

	def _getText(self) -> str:
		return self._getScreen()[0]

	def _rows(self, text: str, caretOffset: int | None) -> tuple[list[str], tuple[int, int] | None]:
		rows = text.split("\n")
		self._rawRows = rows
		starts, offset = [], 0
		for row in rows:
			starts.append(offset)
			offset += len(row) + 1
		self._rowStarts = frozenset(starts)
		caret = None
		if caretOffset is not None:
			# VTE counts code points, as str does.
			row = text.count("\n", 0, caretOffset)
			caret = (row, caretOffset - starts[row] if row < len(starts) else 0)
		return rows, caret

	def _monitor(self) -> None:
		tracker = self._tracker
		try:
			text, caretOffset = self._getScreen()
			rows, caret = self._rows(text, caretOffset)
			with self._lock:
				tracker.reset(rows, caret, time.monotonic())
		except Exception:
			log.exception("Error getting initial text")
		echo: Key | None = None
		while self._keepMonitoring:
			with self._lock:
				waiting = tracker.hasPending() or tracker.silenced or echo is not None
			changed = self._event.wait(0.05 if waiting else None)
			if not self._keepMonitoring:
				break
			try:
				if changed:
					if self.STABILIZE_DELAY > 0:
						time.sleep(self.STABILIZE_DELAY)
						if not self._keepMonitoring:
							break
					self._event.clear()
					text, caretOffset = self._getScreen()
					rows, caret = self._rows(text, caretOffset)
					key = lastKey()
					with self._lock:
						tracker.update(rows, caret, time.monotonic(), key)
						if tracker.typedKey is not None:
							echo, tracker.typedKey = tracker.typedKey, None
				if echo is not None and time.monotonic() - echo.time >= ECHO_FALLBACK_SECONDS:
					self._echoIfMissed(echo)
					echo = None
				with self._lock:
					now = time.monotonic()
					lines = tracker.due(now)
				if lines and config.conf["presentation"]["reportDynamicContentChanges"]:
					queueHandler.queueFunction(
						queueHandler.eventQueue,
						self._reportNewLines,
						lines,
						now,
						_immediate=True,
					)
			except Exception:
				log.exception("Error getting or calculating new text")

	# Speaking

	def _echoIfMissed(self, key: Key) -> None:
		"""Echo what key typed, which showed in the line typed in, if VTE's report of it did not: VTE
		reports a letter typed in the same write as a redrawn spinner as one wide change."""
		from linuxPlatform.atspi import typedChars

		if not typedChars.claimEcho(key.time):
			return
		import eventHandler

		queueHandler.queueFunction(
			queueHandler.eventQueue,
			eventHandler.queueEvent,
			"typedCharacter",
			self,
			ch=key.text,
		)

	def _onSpeechCanceled(self) -> None:
		now = time.monotonic()
		# A key stops speech once NVDA's main thread gets to it, which can be after the program has
		# answered the key: what it answered is not what the key stopped.
		key = inputHook.lastKeyDown()
		with self._lock:
			at = key.time if key is not None and now - key.time <= KEY_SECONDS else now
			if self._tracker.isFlooding(now):
				# A flood is silenced whole, whenever it was found.
				at = now
			self._cancelledAt = at
			self._tracker.cancel(at, now)

	def _reportNewLines(self, lines: list[str], foundAt: float | None = None) -> None:
		if foundAt is not None and foundAt <= self._cancelledAt:
			# Found before speech was stopped, and so dropped with what was waiting.
			return
		speech.clearTypedWordBuffer()
		self._queuedChars = []
		LiveText._reportNewLines(self, lines)

	def _reportNewText(self, line: str) -> None:
		now = time.monotonic()
		# A whole screen of output is always heard whole, however tall the screen.
		capacity = max(LINE_BUDGET, len(self._rawRows))
		self._budget = min(capacity, self._budget + (now - self._budgetAt) * LINES_PER_SECOND)
		self._budgetAt = now
		if self._budget < 1:
			log.debug("Terminal output is coming faster than it can be spoken; a line was not spoken")
			return
		self._budget -= 1
		super()._reportNewText(line)

	# The caret a program draws itself

	def _inputRowNow(self) -> int | None:
		with self._lock:
			return self._tracker.inputRow()

	def _cursorIsDrawn(self) -> bool:
		"""Whether the program hides the terminal's cursor and shows its own in the line typed in."""
		with self._lock:
			tracker = self._tracker
			inputRow = tracker.inputRow()
			return (
				inputRow is not None
				and tracker.inputColumn is not None
				and (tracker.caret is None or tracker.caret[0] != inputRow)
			)

	def _caretIsParked(self) -> bool:
		with self._lock:
			return self._tracker.isCaretParked()

	def _drawnCaretOffset(self) -> int | None:
		"""The offset of the cursor the program draws, while it draws one."""
		if not self._cursorIsDrawn():
			return None
		inputRow = self._inputRowNow()
		rows = self._rawRows
		if inputRow is None or inputRow >= len(rows):
			return None
		start = sum(len(row) + 1 for row in rows[:inputRow])
		row = rows[inputRow]
		column = min(max(self._tracker.inputColumn, inputContentStart(row)), inputContentEnd(row))
		return start + column

	def _inputPromptEnd(self, offset: int) -> int | None:
		"""How far into the row starting at offset its prompt reaches, when it is the line typed in."""
		inputRow = self._inputRowNow()
		rows = self._rawRows
		if inputRow is None or inputRow >= len(rows):
			return None
		if offset != sum(len(row) + 1 for row in rows[:inputRow]):
			return None
		start = inputContentStart(rows[inputRow])
		return start if start and rows[inputRow][start - 2 : start - 1] in PROMPT_MARKS else None

	def _setDrawnColumn(self, column: int) -> None:
		inputRow = self._inputRowNow()
		if inputRow is None or inputRow >= len(self._rawRows):
			return
		row = self._rawRows[inputRow]
		self._tracker.inputColumn = min(max(column, inputContentStart(row)), inputContentEnd(row))

	def _drawnMove(self, gesture) -> bool:
		"""Move the drawn cursor as the program will for gesture; whether gesture is a move it knows."""
		inputRow = self._inputRowNow()
		row = self._rawRows[inputRow]
		start, end = inputContentStart(row), inputContentEnd(row)
		column = self._tracker.inputColumn
		key = gesture.mainKeyName
		byWord = "control" in gesture.modifierNames
		if key == "home":
			column = start
		elif key == "end":
			column = end
		elif key == "leftArrow" and byWord:
			while column > start and row[column - 1] == " ":
				column -= 1
			while column > start and row[column - 1] != " ":
				column -= 1
		elif key == "rightArrow" and byWord:
			while column < end and row[column] == " ":
				column += 1
			while column < end and row[column] != " ":
				column += 1
		elif key == "leftArrow":
			column -= 1
		elif key == "rightArrow":
			column += 1
		else:
			return False
		self._setDrawnColumn(column)
		return True

	def _caretMovementScriptHelper(self, gesture, unit) -> None:
		if self._cursorIsDrawn():
			if self._drawnMove(gesture):
				gesture.send()
				self._caretScriptPostMovedHelper(unit, gesture, self.makeTextInfo(textInfos.POSITION_CARET))
				return
			# Up and Down: the program changes the line, or moves a mark, which the screen then says.
			gesture.send()
			return
		if self._caretIsParked():
			# The cursor waits under a program that draws its own; reading where it waits says nothing.
			gesture.send()
			return
		super()._caretMovementScriptHelper(gesture, unit)

	def _backspaceScriptHelper(self, unit, gesture) -> None:
		if not self._cursorIsDrawn():
			self._backspaceFromBeforeKey(unit, gesture)
			return
		row = self._rawRows[self._inputRowNow()]
		column = self._tracker.inputColumn
		start = inputContentStart(row)
		if column <= start:
			gesture.send()
			return
		deletedFrom = column - 1
		if unit == textInfos.UNIT_WORD:
			while deletedFrom > start and row[deletedFrom - 1] == " ":
				deletedFrom -= 1
			while deletedFrom > start and row[deletedFrom - 1] != " ":
				deletedFrom -= 1
		chunk = row[deletedFrom:column]
		gesture.send()
		self._setDrawnColumn(deletedFrom)
		if len(chunk) > 1:
			speech.speakMessage(chunk)
		else:
			speech.speakSpelling(chunk)

	def _backspaceFromBeforeKey(self, unit, gesture) -> None:
		"""Backspace with the terminal's own cursor: what it deleted is read from the line as it was when
		the key went down."""
		before = self._beforeKey()
		if before is None:
			super()._backspaceScriptHelper(unit, gesture)
			return
		text, caret = before
		lineStart = text.rfind("\n", 0, caret) + 1
		start = caret - 1
		if unit == textInfos.UNIT_WORD:
			while start > lineStart and text[start - 1] == " ":
				start -= 1
			while start > lineStart and text[start - 1] != " ":
				start -= 1
		gesture.send()
		if start < lineStart:
			return
		caretMoved, newInfo = self._hasCaretMoved(None)
		if not caretMoved:
			return
		chunk = text[start:caret]
		if len(chunk) > 1:
			speech.speakMessage(chunk)
		else:
			speech.speakSpelling(chunk)
		self._caretScriptPostMovedHelper(None, gesture, newInfo)

	def _deleteScriptHelper(self, unit, gesture) -> None:
		if not self._cursorIsDrawn():
			super()._deleteScriptHelper(unit, gesture)
			return
		gesture.send()
