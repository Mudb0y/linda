"""Moving by word in MATE Terminal and across AT-SPI text controls reads the word the caret
lands on, each part of a path or hyphenated identifier included (GitHub issue 8).

nix develop . -c tests/linux/rig python3 -m pytest -q tests/linux/test_terminalWords.py
"""

import time
from collections.abc import Iterator
from pathlib import Path

import desktopSession
import harness
import pytest
from desktopSession import DesktopSession

SESSION_CONFIG = harness.HERE / "fixtures" / "sessionConfig" / "nvda.ini"
STARTUP_BOUND = 60.0
#: Seconds to wait for anything more after what a step expects.
SETTLE = 0.6
#: Each line typed, then what Control+Right says three times from its start, then Control+Left three
#: times from its end. Bash stops at the end of a word, on the separator after it. The session's
#: punctuation level names a slash and a dot but leaves a hyphen to the synthesizer.
TERMINAL_WORDS = (
	(
		"/usr/bin/bash",
		["usr slash", "bin slash", "bash"],
		["bash", "bin slash", "usr slash"],
	),
	(
		"foo.bar.baz",
		["foo dot bar dot baz"] * 3,
		["foo dot bar dot baz"] * 3,
	),
	(
		"foo-bar-baz",
		["foo-", "bar-", "baz"],
		["baz", "bar-", "foo-"],
	),
	("one two three", ["one", "two", "three"], ["three", "two", "one"]),
)
#: The same in a GTK entry: GTK also stops at the end of a word, and EndOfWordMovement speaks the
#: word passed including the last word of the line (issue 66).
ENTRY_WORDS = (
	("/usr/bin/bash", ["usr slash", "bin slash", "bash"], ["bash", "bin slash", "usr slash"]),
	(
		"foo.bar.baz",
		["foo dot bar dot baz"] * 3,
		["foo dot bar dot baz"] * 3,
	),
	("foo-bar-baz", ["foo-", "bar-", "baz"], ["baz", "bar-", "foo-"]),
	("one two three", ["one", "two", "three"], ["three", "two", "one"]),
	("a-b-c", ["a-", "b-", "c"], ["c", "b-", "a-"]),
)


#: Terminal lines whose words the review cursor walks: the path and the separators above, a variation
#: selector, a keycap, a combining accent, a joiner, a NUL, which NVDA's words take for a space, and a
#: zero-width space after punctuation, punctuation after an accented letter and after a variation
#: selector, and code.
PARTITIONED_LINES = (
	"/usr/bin/bash\n",
	"foo-bar-baz\n",
	"wow\u203c\ufe0f great news\n",
	"x#\ufe0f\u20e3y z\n",
	"a/\u0301b c\n",
	"a/\u200db c\n",
	"a/\x00b c\n",
	"a/\u200bb c\n",
	"e\u0301/x y\n",
	"wow\u203c\ufe0f\u203c\ufe0f x\n",
	"f(x, y[0]);\n",
)
#: The words of a path, which the shell's word movement stops between.
PATH_WORDS = ["/", "usr/", "bin/", "bash\n"]

#: Run under harness.runChild with LINES set: for each line, the word AtspiTextInfo gives each offset,
#: and where moving by word from the start stops.
WORDS_CHILD = r"""
# Enters NVDA's import cycle where core.main does.
import NVDAHelper  # noqa: F401
import textInfos
from textInfos.offsets import Offsets
from NVDAObjects.AtSpi.textInfo import AtspiTextInfo


class LineSource:
	unit = "codepoints"

	def __init__(self, text):
		self.line = text

	def characterCount(self):
		return len(self.line)

	def text(self, start, end):
		return self.line[start : len(self.line) if end < 0 else end]

	def stringAtOffset(self, offset, granularity):
		assert granularity == "line", granularity
		return (0, len(self.line), self.line)


class TextObject:
	isProtected = False


result = []
for line in LINES:

	class LineInfo(AtspiTextInfo):
		def _get_textSource(self):
			return LineSource(line)

	target = TextObject()
	words = [list(LineInfo(target, Offsets(offset, offset))._getWordOffsets(offset)) for offset in range(len(line))]
	info = LineInfo(target, Offsets(0, 0))
	stops = []
	while info.move(textInfos.UNIT_WORD, 1) and len(stops) < len(line):
		stops.append(info._startOffset)
	result.append({"words": words, "stops": stops})
"""


def test_terminalWordsPartitionEachLine(tmp_path: Path):
	"""Every offset of a line lies in the word it is given, every offset of that word is given the same
	word, and moving by word from the start stops at each word, as the review cursor needs."""
	done = harness.runChild(tmp_path, f"LINES = {PARTITIONED_LINES!r}\n" + WORDS_CHILD, timeout=120)
	assert done.error is None, f"{done.error}\n{done.stderr[-6000:]}"
	faults = []
	for line, found in zip(PARTITIONED_LINES, done.value, strict=True):
		words = [tuple(word) for word in found["words"]]
		for offset, (start, end) in enumerate(words):
			if not start <= offset < end:
				faults.append(f"{line!r}: offset {offset} is given ({start}, {end})")
			elif any(words[inside] != (start, end) for inside in range(start, end)):
				faults.append(f"{line!r}: the offsets of ({start}, {end}) are given other words")
		# NVDA lets a move by word go one past the end of the text.
		starts = sorted({start for start, _end in words} - {0}) + [len(line)]
		if found["stops"] != starts:
			faults.append(f"{line!r}: moving by word stops at {found['stops']}, not {starts}")
	assert not faults, "\n".join(faults)
	pathWords = done.value[0]["words"]
	starts = sorted({start for start, _end in pathWords})
	assert [PARTITIONED_LINES[0][start : pathWords[start][1]] for start in starts] == PATH_WORDS


@pytest.fixture
def desktop(tmp_path: Path) -> Iterator[DesktopSession]:
	with DesktopSession(tmp_path / "desktop") as session:
		yield session


def step(desktop: DesktopSession, chord: str, expected: str) -> None:
	"""Press chord and wait for expected, then for anything more; nothing else may have been said."""
	mark = desktop.mark()
	desktop.press(chord)
	desktop.expect(expected, since=mark)
	time.sleep(SETTLE)
	said = [text for text in desktop.spokenSince(mark) if text]
	assert said == [expected], f"{chord}: {said}"


def moveByWord(desktop: DesktopSession, forward: list[str], back: list[str]) -> None:
	for expected in forward:
		step(desktop, "ctrl+Right", expected)
	desktop.press("End")
	time.sleep(SETTLE)
	for expected in back:
		step(desktop, "ctrl+Left", expected)


def test_terminalWordsAtTheShellsCaret(desktop: DesktopSession):
	title = "Terminal fixture"
	desktop.startApp(desktop.fixture("terminal"), title=title)
	desktopSession.activate(title)
	nvda = desktop.startNVDA(SESSION_CONFIG)
	desktop.expect(title, "terminal $", since=0, timeout=STARTUP_BOUND)
	for line, forward, back in TERMINAL_WORDS:
		mark = desktop.mark()
		desktop.typeText(line)
		desktop.expect(line[-1], since=mark)
		desktop.press("Home")
		time.sleep(SETTLE)
		moveByWord(desktop, forward, back)
		desktop.press("End", "ctrl+u")
		time.sleep(SETTLE)
	desktop.close()
	assert nvda.process.returncode == 0, nvda.describe()


def test_entryWordsAtTheCaret(desktop: DesktopSession):
	title = "TextFixture"
	desktop.startApp(desktop.fixture("textapp", "gtk3"), title=title)
	desktopSession.activate(title)
	nvda = desktop.startNVDA(SESSION_CONFIG)
	desktop.expect("entry edit…", since=0, timeout=STARTUP_BOUND)
	for line, forward, back in ENTRY_WORDS:
		desktop.press("ctrl+a")
		mark = desktop.mark()
		desktop.typeText(line)
		desktop.expect(line[-1], since=mark)
		desktop.press("Home")
		time.sleep(SETTLE)
		moveByWord(desktop, forward, back)
	desktop.close()
	assert nvda.process.returncode == 0, nvda.describe()
