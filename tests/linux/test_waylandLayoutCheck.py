"""The layout cross-check of a Wayland session: each key NVDA passes on that types text is compared, in
memory, with the character the application inserted, and five that differ in the last eight have NVDA
say once that its idea of the layout differs from the desktop's, logging counts and nothing typed.
Nothing is compared in a protected field, in a terminal, for an insert of more than one character, or
while an input method makes the characters; an X session has no check at all.

The check alone is held to its rules in a child; Linda itself, told the session is Wayland while the
rig gives it the X display, is given a layout that differs from the display's on purpose: the
stand-in for linda-keyd puts the keys out on the X display, which types in US English, and Linda reads
them as French, whose digit row types symbols. Run under the rig:

	nix develop . -c tests/linux/rig python3 -m pytest -q tests/linux/test_waylandLayoutCheck.py
"""

import re
import shutil
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

import desktopSession
import harness
import pytest
from desktopSession import DesktopSession
from keydStandIn import StandIn
from test_deletedCharacter import findField
from test_keys import STARTUP_TIMEOUT, configText
from test_waylandRoute import OWNER_SESSION

#: The line as the source has it, and as Linda says it.
LINE = "NVDA's idea of the keyboard layout differs from the desktop's"
SAID = LINE.replace("NVDA", "Linda")
#: The count-only record said with it, which holds nothing but numbers besides its words.
RECORD = re.compile(
	r"Keyboard layout: (\d+) of the last (\d+) characters typed differed from NVDA's layout; "
	r"(\d+) matched it and (\d+) differed since NVDA started"
)
#: What the test types: digits, which the French layout types as symbols on the same keys.
MARKER = "7340918265"
#: What Linda's French layout says MARKER's keys type.
MARKER_IN_FRENCH = "è'\"àç&_(-é"
WINDOW_TITLE = "TextFixture"
#: Seconds for the application to insert what was typed and NVDA to compare it.
SETTLE = 1.5

#: A layout check over keys and inserts the test makes itself, with an input method it can turn on.
CHECK_CHILD = r"""
import NVDAHelper  # noqa: F401
import languageHandler

languageHandler.setLanguage("en")
import logging
import time
import types

import queueHandler
from controlTypes import Role
from linuxPlatform import inputHook
from linuxPlatform.atspi import typedChars
from linuxPlatform.keys import KeyEvent
from logHandler import log

said = []
queueHandler.queueFunction = lambda queue, function, *args, **kwargs: said.append(args[0])


class Lines(logging.Handler):
	def __init__(self):
		super().__init__(logging.DEBUG)
		self.lines = []

	def emit(self, record):
		self.lines.append(record.getMessage())


logged = Lines()
log.addHandler(logged)
log.setLevel(logging.DEBUG)
inputMethod = [False]
check = typedChars.LayoutCheck(inputMethod=lambda: inputMethod[0])
FIELD = types.SimpleNamespace(atspiStates={"editable", "focused"}, role=Role.EDITABLETEXT, isProtected=False)
PASSWORD = types.SimpleNamespace(atspiStates={"editable", "focused"}, role=Role.PASSWORDEDIT, isProtected=True)
TERMINAL = types.SimpleNamespace(atspiStates={"editable", "focused"}, role=Role.TERMINAL, isProtected=False)
DOCUMENT = types.SimpleNamespace(atspiStates={"focused"}, role=Role.DOCUMENT, isProtected=False)


def key(text, vk=0x41, reached=True, at=None):
	typing = bool(text) and text.isprintable()
	event = KeyEvent(
		vkCode=vk,
		scanCode=0,
		extended=False,
		injected=False,
		isUp=False,
		repeat=False,
		reachedApplication=reached,
		text=text,
		typing=typing,
		time=time.monotonic() if at is None else at,
	)
	inputHook._delivering.event = event
	try:
		assert check.noteKey(True) is True
	finally:
		inputHook._delivering.event = None


def counts():
	return [check.matches, check.mismatches]


def insert(text, focus=FIELD):
	check.compare(focus, text)


result = {}
"""


def runCheck(tmp_path: Path, code: str) -> dict:
	done = harness.runChild(tmp_path, CHECK_CHILD + code, env={"LINDA_SESSION_TYPE": "wayland"}, timeout=60)
	done.assertClean()
	return done.value


def test_matchingCharactersAreCounted(tmp_path: Path):
	"""Each key paired with its own insert, typed slowly, and typed fast, all down before the first
	insert; a key NVDA kept, which reaches no application, is never paired."""
	value = runCheck(
		tmp_path,
		r"""
for character in "ab":
	key(character)
	insert(character)
result["slow"] = counts()
for character in "cde":
	key(character)
for character in "cde":
	insert(character)
result["fast"] = counts()
key("f", reached=False)
insert("g")
result["kept"] = counts()
result["said"] = said
""",
	)
	assert value == {"slow": [2, 0], "fast": [5, 0], "kept": [5, 0], "said": []}


def test_fiveOfEightDifferingIsSaidOnceWithCountsOnly(tmp_path: Path):
	"""Four of eight differing says nothing; the fifth does, once, with a record of counts alone; more
	say nothing again."""
	value = runCheck(
		tmp_path,
		r"""
for typed, inserted in zip("aqbqcqdq", "awbwcwdw"):
	key(typed)
	insert(inserted)
result["four"] = [counts(), list(said)]
key("z")
insert("y")
result["five"] = [counts(), list(said)]
for _ in range(10):
	key("z")
	insert("y")
result["more"] = [counts(), list(said)]
result["records"] = [line for line in logged.lines if line.startswith("Keyboard layout")]
""",
	)
	assert value["four"] == [[4, 4], []]
	assert value["five"] == [[4, 5], [SAID]]
	assert value["more"] == [[4, 15], [SAID]]
	(record,) = value["records"]
	assert RECORD.fullmatch(record), record
	assert RECORD.fullmatch(record).groups() == ("5", "8", "4", "5")


def test_whatIsNeverCompared(tmp_path: Path):
	"""A protected field, a terminal, a focus that cannot be edited, an insert of more than one
	character, one the caller may not read, a key typed too long before, and anything while an input
	method makes the characters: nothing is compared, and the next key's character is compared with
	that key's own."""
	value = runCheck(
		tmp_path,
		r"""
cases = {}
for name, focus, inserted in (
	("password", PASSWORD, "•"),
	("unread", FIELD, None),
	("terminal", TERMINAL, "y"),
	("document", DOCUMENT, "y"),
	("several", FIELD, "yy"),
):
	key("x")
	insert(inserted, focus)
	cases[name] = counts()
	# The next key's own character, compared with that key alone.
	time.sleep(0.6)
	key("m")
	insert("m")
	cases[name + " then"] = counts()
key("x", at=time.monotonic() - 1)
insert("y")
cases["late"] = counts()
inputMethod[0] = True
key("x")
insert("y")
cases["input method"] = counts()
inputMethod[0] = False
key("m")
insert("m")
cases["after input method"] = counts()
result["cases"] = cases
result["said"] = said
""",
	)
	cases = value["cases"]
	assert cases["password"] == [0, 0]
	assert cases["password then"] == [1, 0]
	assert cases["unread"] == [1, 0]
	assert cases["unread then"] == [2, 0]
	assert cases["terminal"] == [2, 0]
	assert cases["terminal then"] == [3, 0]
	assert cases["document"] == [3, 0]
	assert cases["document then"] == [4, 0]
	assert cases["several"] == [4, 0]
	assert cases["several then"] == [5, 0]
	assert cases["late"] == [5, 0]
	assert cases["input method"] == [5, 0]
	assert cases["after input method"] == [6, 0]
	assert value["said"] == []


def test_lateInsertsArePairedWithTheirOwnKeys(tmp_path: Path):
	"""With the same layout on both sides, inserts NVDA gets to well after their keys went down, as when
	it or the application is busy, still match: each insert handled 0.6 s after its key while keys go
	down 0.15 s apart, and a stall of about a second while twelve keys a second are typed."""
	value = runCheck(
		tmp_path,
		r"""
text = "the quick brown"
# Ahead of the clock the key thread forgets keys by, so that a slow machine forgets none.
start = time.monotonic()
events = []
for index, character in enumerate(text):
	events.append((start + index * 0.15, "key", character))
	events.append((start + index * 0.15 + 0.6, "insert", character))
events.sort(key=lambda event: (event[0], event[1] == "insert"))
real = time.monotonic
for at, kind, character in events:
	if kind == "key":
		key(character, at=at)
	else:
		time.monotonic = lambda at=at: at
		try:
			insert(character)
		finally:
			time.monotonic = real
result["steady"] = counts()
for character in "the ":
	key(character)
	insert(character)
word = "dermatoglyph"
base = time.monotonic() - 0.08 * len(word)
for index, character in enumerate(word):
	key(character, at=base + index * 0.08)
for character in word:
	insert(character)
result["stall"] = counts()
result["said"] = said
""",
	)
	assert value == {"steady": [15, 0], "stall": [31, 0], "said": []}


def test_keysAreForgottenWhenTheirTimeIsUp(tmp_path: Path):
	"""What was typed is held only while a key may still be paired: keys older than the hold are
	forgotten when a terminal's insert compares nothing, and when the next key goes down."""
	value = runCheck(
		tmp_path,
		r"""
for character in "hunter2":
	key(character, at=time.monotonic() - typedChars.LAYOUT_HOLD - 1)
insert("x", TERMINAL)
result["after terminal"] = len(check._pending)
for character in "hunter2":
	key(character, at=time.monotonic() - typedChars.LAYOUT_HOLD - 1)
key("m")
result["after key"] = [entry.text for entry in check._pending]
""",
	)
	assert value == {"after terminal": 0, "after key": ["m"]}


def test_aDeadKeysCharacterIsNotAMismatch(tmp_path: Path):
	"""A dead key types nothing, and the letter after it inserts the accented one; the compose key and
	the two keys after it insert one character. Neither is a mismatch, and the keys after are compared
	as ever. A dead key that goes down before NVDA gets to the insert of the letter before it still
	marks the letter after it."""
	value = runCheck(
		tmp_path,
		r"""
key("", vk=0xDE)
key("e")
insert("é")
result["dead"] = counts()
key("t")
insert("t")
result["after dead"] = counts()
key("", vk=0x5D)
key("'")
key("e")
insert("é")
key("t")
insert("t")
result["compose"] = counts()
key("", vk=0xA0)
key("T")
insert("Y")
result["after shift"] = counts()
key("", vk=0x08)
key("a")
key("", vk=0xDE)
insert("a")
key("e")
insert("é")
result["dead before insert"] = counts()
""",
	)
	assert value == {
		"dead": [0, 0],
		"after dead": [1, 0],
		"compose": [2, 0],
		"after shift": [2, 1],
		"dead before insert": [3, 1],
	}


def test_anX11SessionHasNoCheck(tmp_path: Path):
	"""In an X session nothing is noted and nothing compared: typedChars has no check, and the key
	extension point has no handler of its."""
	done = harness.runChild(
		tmp_path,
		r"""
import NVDAHelper  # noqa: F401
import inputCore
from linuxPlatform import displayServer
from linuxPlatform.atspi import typedChars

result = {
	"kind": displayServer.kind(),
	"check": typedChars._layoutCheck is None,
	"matches": typedChars.layoutMatches(),
	"handlers": sorted(
		getattr(handler, "__qualname__", repr(handler)) for handler in inputCore.decide_handleRawKey.handlers
	),
}
""",
		env=OWNER_SESSION,
		timeout=60,
	)
	done.assertClean()
	assert done.value == {"kind": "x11", "check": True, "matches": 0, "handlers": ["_noteTypingKey"]}


# Linda itself, told it is in a Wayland session, typing into GTK 3's fields through the stand-in.

#: Logs the check's counts each time a comparison changes them, so that the test knows them.
PROBE_PLUGIN = """
import globalPluginHandler
from linuxPlatform.atspi import typedChars
from logHandler import log


class GlobalPlugin(globalPluginHandler.GlobalPlugin):
	def __init__(self):
		super().__init__()
		check = typedChars._layoutCheck
		log.info(f"layout check probe: {'ready' if check is not None else 'absent'}")
		if check is None:
			return
		compare = check.compare

		def counted(*args, **kwargs):
			before = (check.matches, check.mismatches)
			compare(*args, **kwargs)
			if (check.matches, check.mismatches) != before:
				log.info(f"layout check probe: {check.matches} {check.mismatches}")

		check.compare = counted
"""


@pytest.fixture
def desktop(tmp_path: Path) -> Iterator[DesktopSession]:
	with DesktopSession(tmp_path / "desktop") as session:
		yield session


@pytest.fixture
def keyboard() -> Iterator[StandIn]:
	"""The stand-in for linda-keyd, putting the keys Linda passes on out on the X display."""
	directory = Path(tempfile.mkdtemp(prefix="keyd", dir="/tmp"))
	try:
		with StandIn(directory / "socket") as standIn:
			yield standIn
	finally:
		shutil.rmtree(directory, ignore_errors=True)


def probeCounts(nvda: harness.Launch) -> tuple[int, int]:
	found = re.findall(r"layout check probe: (\d+) (\d+)", nvda.text("nvda.log"))
	return (int(found[-1][0]), int(found[-1][1])) if found else (0, 0)


def entryText() -> str:
	return desktopSession._atspi().Text.get_text(findField(WINDOW_TITLE, "entry"), 0, -1)


def typeInto(keyboard: StandIn, name: str, text: str) -> None:
	field = findField(WINDOW_TITLE, name)
	assert desktopSession._atspi().Component.grab_focus(field)
	time.sleep(0.5)
	keyboard.type(text)
	time.sleep(SETTLE)


def typeIntoPassword(desktop: DesktopSession, keyboard: StandIn, nvda: harness.Launch) -> None:
	"""Type MARKER into the password field, which compares nothing; each character is still echoed as
	a star, so its insert reached typedChars."""
	typeInto(keyboard, "password", MARKER)
	stars = [text.strip() for text in desktop.spokenSince(0)].count("star")
	assert stars == len(MARKER), desktop.spokenSince(0)
	assert probeCounts(nvda) == (0, 0), nvda.text("nvda.log")[-4000:]


def startLinda(desktop: DesktopSession, keyboard: StandIn, tmp_path: Path, layout: str) -> harness.Launch:
	hooks = tmp_path / "hooks"
	(hooks / "globalPlugins").mkdir(parents=True)
	(hooks / "synthDrivers").symlink_to(harness.HERE / "synthDrivers")
	(hooks / "globalPlugins" / "layoutCheckProbe.py").write_text(PROBE_PLUGIN)
	config = tmp_path / "nvda.ini"
	config.write_text(configText("desktop", 7))
	desktop.startApp(desktop.fixture("textapp", "gtk3"), title=WINDOW_TITLE)
	nvda = desktop.startNVDA(
		config,
		env={
			"LINDA_TEST_HOOKS_DIR": str(hooks),
			"LINDA_TEST_KEYD_SOCKET": keyboard.path,
			"LINDA_SESSION_TYPE": "wayland",
			"XDG_CURRENT_DESKTOP": None,
			"XKB_DEFAULT_LAYOUT": layout,
		},
	)
	nvda.waitForLog("layout check probe: ready", STARTUP_TIMEOUT)
	keyboard.waitFor(lambda: keyboard.capturing, 10)
	nvda.waitForLog("Keys come from the kernel through linda-keyd", 10)
	return nvda


def finish(nvda: harness.Launch) -> str:
	nvda.process.terminate()
	nvda.waitForExit(20)
	nvda.assertClean(logNames=("nvda.log",))
	log = nvda.text("nvda.log")
	assert MARKER not in log
	assert MARKER_IN_FRENCH not in log
	assert "Keys are read from the X display" not in log
	return log


@pytest.mark.rigSession("x11")
def test_aMatchingLayoutGivesMatchesAndNoLine(desktop: DesktopSession, keyboard: StandIn, tmp_path: Path):
	"""Linda's layout is the display's: typing into the password field compares nothing, and typing
	into the entry matches every character, with nothing said."""
	nvda = startLinda(desktop, keyboard, tmp_path, "us")
	try:
		typeIntoPassword(desktop, keyboard, nvda)
		typeInto(keyboard, "entry", MARKER)
		assert entryText() == MARKER
		matches, mismatches = probeCounts(nvda)
	finally:
		log = finish(nvda)
	assert mismatches == 0
	assert matches >= len(MARKER) - 2, (matches, log[-4000:])
	assert desktop.spokenSince(0).count(SAID) == 0
	assert not RECORD.search(log)


@pytest.mark.rigSession("x11")
def test_aMismatchedLayoutIsSaidOnce(desktop: DesktopSession, keyboard: StandIn, tmp_path: Path):
	"""Linda reads the keys as French while the display types US English: typing into the password field
	compares nothing; typing digits into the entry says the line once, with one record of counts, and
	typing more says nothing again."""
	nvda = startLinda(desktop, keyboard, tmp_path, "fr")
	try:
		typeIntoPassword(desktop, keyboard, nvda)
		typeInto(keyboard, "entry", MARKER)
		assert entryText() == MARKER
		desktop.expect(SAID, since=0)
		keyboard.type(MARKER)
		time.sleep(SETTLE)
		matches, mismatches = probeCounts(nvda)
	finally:
		log = finish(nvda)
	assert matches == 0, log[-4000:]
	assert mismatches >= 2 * len(MARKER) - 4, (mismatches, log[-4000:])
	assert desktop.spokenSince(0).count(SAID) == 1
	records = RECORD.findall(log)
	assert len(records) == 1, records
	differed, compared, matched, _total = records[0]
	assert (differed, compared, matched) == ("5", "5", "0")
