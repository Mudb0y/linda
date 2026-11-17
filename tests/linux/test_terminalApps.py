"""A coding assistant's text interface in MATE Terminal, as fixtures/apps/terminalAssistant.py
imitates one: a reply streaming into a region the program erases and writes again many times a
second, a status row that animates, an input box drawn with box-drawing characters, a tool call, and
a menu whose chosen option is marked by a character; with the program's own drawn cursor, and with
the terminal's.

nix develop . -c tests/linux/rig python3 -m pytest -q tests/linux/test_terminalApps.py
"""

import json
import re
import time
from collections import Counter
from collections.abc import Iterator
from pathlib import Path

import desktopSession
import harness
import pytest
from desktopSession import DesktopSession

SESSION_CONFIG = harness.HERE / "fixtures" / "sessionConfig" / "nvda.ini"
TITLE = "Terminal fixture"
TIMED_SPEECH = {"BASEDREADER_TRANSCRIPT_MODE": "timed", "BASEDREADER_TRANSCRIPT_MS_PER_CHAR": "10"}
STARTUP_BOUND = 60.0
#: Seconds to wait for anything more after what a step expects.
SETTLE = 0.6
#: Seconds the whole reply, up to the question, may take to be heard.
REPLY_BOUND = 40.0
#: Seconds from a paragraph appearing to its first words being spoken, with nothing else being said.
FIRST_WORDS_BOUND = 0.4
#: Seconds from a key to NVDA's answer while a reply streams.
KEY_BOUND = 0.1
#: Words NVDA's symbol processing adds for the fixture's punctuation: "notes.txt", "settings = load()".
SYMBOL_WORDS = frozenset({"dot", "equals"})
#: Characters that frame and mark the fixture's rows, none of which may reach the synth.
DECORATION = re.compile("[─-▟←-⯿•·]")

ASSISTANT = harness._loadFile(
	"terminalAssistant", harness.HERE / "fixtures" / "apps" / "terminalAssistant.py"
)
#: The last row of the question, whose middle dot NVDA does not say.
QUESTION_END = ASSISTANT.MENU_HINT.split("\u00b7")[0].strip() + "\N{HORIZONTAL ELLIPSIS}"


@pytest.fixture
def desktop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[DesktopSession]:
	monkeypatch.setenv("BASEDREADER_ASSISTANT_LOG", str(tmp_path / "assistant.jsonl"))
	with DesktopSession(tmp_path / "desktop") as session:
		yield session


def words(texts: list[str]) -> list[str]:
	return [w for text in texts for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in SYMBOL_WORDS]


def spoken(desktop: DesktopSession, mark: int) -> list[str]:
	"""What was said since mark, without the empty records that end each spelled character."""
	return [text for text in desktop.spokenSince(mark) if text]


def records(nvda: harness.Launch) -> list[dict]:
	return harness.transcriptRecords().readRecords(nvda.directory / "transcript.jsonl")


def step(desktop: DesktopSession, chord: str, *expected: str) -> None:
	"""Press chord; expected must be said, in order, and nothing else."""
	mark = desktop.mark()
	desktop.press(chord)
	desktop.expect(*expected, since=mark)
	time.sleep(SETTLE)
	assert spoken(desktop, mark) == list(expected), spoken(desktop, mark)


def startAssistant(desktop: DesktopSession, *options: str) -> harness.Launch:
	desktop.startApp(desktop.fixture("terminal"), title=TITLE)
	desktopSession.activate(TITLE)
	nvda = desktop.startNVDA(SESSION_CONFIG, env=TIMED_SPEECH)
	desktop.expect(TITLE, "terminal $", since=0, timeout=STARTUP_BOUND)
	desktop.typeText(" ".join(["ai", *options]))
	mark = desktop.mark()
	desktop.press("Return")
	desktop.expect("Assistant fixture", "an imitation, for tests", since=mark)
	time.sleep(SETTLE)
	# The empty input box and the hint row under it are not news.
	assert spoken(desktop, mark) == ["Assistant fixture", "an imitation, for tests"], spoken(desktop, mark)
	return nvda


def typePrompt(desktop: DesktopSession, text: str) -> None:
	mark = desktop.mark()
	desktop.typeText(text)
	desktop.expect(*text, since=mark)
	time.sleep(SETTLE)


def submit(desktop: DesktopSession, until: str) -> tuple[int, float]:
	"""Press Return and wait until until is said; the mark and the time of the press."""
	mark = desktop.mark()
	pressed = time.monotonic()
	desktop.press("Return", gap=0)
	desktop.expect(until, since=mark, timeout=REPLY_BOUND)
	time.sleep(SETTLE)
	return mark, pressed


def waitForWord(desktop: DesktopSession, mark: int, word: str) -> None:
	deadline = time.monotonic() + REPLY_BOUND
	while word not in words(spoken(desktop, mark)):
		assert time.monotonic() < deadline, spoken(desktop, mark)
		time.sleep(0.02)


def assertHeardOnce(said: list[str], expected: list[str]) -> None:
	"""said is expected, word for word: nothing repeated by a redraw, nothing lost, nothing that only
	animates, in order."""
	assert words(said) == words(expected), f"said {said}\nexpected {expected}"
	decorated = [text for text in said if DECORATION.search(text)]
	assert not decorated, decorated


def paragraphTimes(tmp_path: Path) -> dict[str, float]:
	"""When each paragraph of the reply started to appear."""
	times = {}
	for line in (tmp_path / "assistant.jsonl").read_text().splitlines():
		entry = json.loads(line)
		times.setdefault(entry["text"], entry["t"])
	return times


def assertExitedCleanly(desktop: DesktopSession, nvda: harness.Launch) -> None:
	desktop.close()
	assert nvda.process.returncode == 0, nvda.describe()
	nvda.assertClean(logNames=("nvda.log",))


def test_replyAndMenuWithDrawnCursor(desktop: DesktopSession, tmp_path: Path):
	nvda = startAssistant(desktop)

	# The input box, with the cursor the program draws and the terminal's own hidden under it.
	step(desktop, "Insert+Up", "blank")
	typePrompt(desktop, "hello")
	step(desktop, "Left", "o")
	step(desktop, "Left", "l")
	step(desktop, "Insert+Up", "hello")
	step(desktop, "End", "blank")
	step(desktop, "BackSpace", "o")
	step(desktop, "Insert+Up", "hell")
	step(desktop, "Home", "h")
	desktop.press("End")

	before, after = ASSISTANT.heard("hell", chosen=1)
	mark, _pressed = submit(desktop, QUESTION_END)
	said = spoken(desktop, mark)
	assertHeardOnce(said, before)

	# The reply's first words come soon after they appear.
	first = ASSISTANT.REPLY[1][1]
	appeared = paragraphTimes(tmp_path)[first]
	firstWord = first.split(" ")[0]
	spokenAt = next(
		r["t"]
		for r in records(nvda)
		if r["kind"] == "speak"
		and r["t"] > appeared
		and firstWord in harness.transcriptRecords().spokenText(r)
	)
	assert spokenAt - appeared <= FIRST_WORDS_BOUND, spokenAt - appeared

	# Moving the mark speaks the option it moves to, alone.
	step(desktop, "Down", "2. Yes, and do not ask again for this command")
	step(desktop, "Down", "3. No, and tell me what to do differently")
	step(desktop, "Up", "2. Yes, and do not ask again for this command")

	mark, _pressed = submit(desktop, after[-1])
	time.sleep(1)
	assertHeardOnce(spoken(desktop, mark), after)

	assertExitedCleanly(desktop, nvda)


def test_silencingAndRealCursor(desktop: DesktopSession, tmp_path: Path):
	nvda = startAssistant(desktop, "--cursor=real", "--render=lines")

	# The input box, with the terminal's cursor in it.
	typePrompt(desktop, "hey")
	step(desktop, "Left", "y")
	step(desktop, "BackSpace", "e")
	step(desktop, "Insert+Up", "hy")
	step(desktop, "Delete", "blank")
	desktop.press("End", "BackSpace", "BackSpace")
	typePrompt(desktop, "wait")

	before, after = ASSISTANT.heard("wait", chosen=0)
	expected = words(before)
	mark = desktop.mark()
	desktop.press("Return")
	# Control while only the status row animates: it stops nothing, and leaves nothing silent.
	time.sleep(1.5)
	desktop.press("Control_L")
	waitForWord(desktop, mark, "look")

	# Control while the reply is said stops it at once, drops what waited, and what comes later is
	# said.
	pressed = time.monotonic()
	desktop.press("Control_L", gap=0)
	cancels = []
	deadline = time.monotonic() + 1
	while not cancels and time.monotonic() < deadline:
		cancels = [r for r in records(nvda) if r["kind"] == "cancel" and r["t"] >= pressed]
		time.sleep(0.01)
	assert cancels and cancels[0]["t"] - pressed <= KEY_BOUND, records(nvda)[-10:]
	afterCancel = desktop.mark()

	# A key gets its answer at once while the reply streams: NVDA+Up reads the empty input line.
	desktop.expect("Here is what each file does:", since=afterCancel, timeout=REPLY_BOUND)
	pressed = time.monotonic()
	desktop.press("Insert+Up", gap=0)
	time.sleep(1)
	answer = [r for r in records(nvda) if r["kind"] == "speak" and r["t"] >= pressed]
	texts = harness.transcriptRecords().spokenTexts(answer)
	assert texts and texts[0] == "blank", texts
	assert answer[0]["t"] - pressed <= KEY_BOUND, answer[0]["t"] - pressed

	desktop.expect(QUESTION_END, since=afterCancel, timeout=REPLY_BOUND)
	time.sleep(SETTLE)
	said = words([text for text in spoken(desktop, mark) if text != "blank"])
	# In order, nothing twice, and something after each key.
	position = 0
	for word in said:
		assert word in expected[position:], f"{word!r} out of order or repeated in {said}"
		position = expected.index(word, position) + 1
	counts = Counter(expected)
	assert all(count <= counts[word] for word, count in Counter(said).items()), said
	assert words(spoken(desktop, afterCancel))[-3:] == expected[-3:]

	# The terminal's cursor goes to the chosen option, which the caret commands then read.
	step(desktop, "Down", "2. Yes, and do not ask again for this command")
	step(desktop, "Up", "1. Yes")

	mark, _pressed = submit(desktop, after[-1])
	time.sleep(1)
	assertHeardOnce(spoken(desktop, mark), after)

	# The review cursor reads the rows of the screen, without their framing.
	mark = desktop.mark()
	desktop.press("shift+KP_Home")
	desktop.press("KP_Prior")
	time.sleep(SETTLE)
	reviewed = spoken(desktop, mark)
	assert len(reviewed) == 2 and all(reviewed) and not any(DECORATION.search(text) for text in reviewed), (
		reviewed
	)

	assertExitedCleanly(desktop, nvda)
