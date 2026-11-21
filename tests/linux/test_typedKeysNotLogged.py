"""No log line and no exception text names a key or a character asked to be typed: the X source and
inputHook, asked for a key the layout lacks, a key with no source to type it, or a character no source
knows, say what failed without saying which key or character it was. Run under the rig:

	nix develop . -c tests/linux/rig python3 -m pytest tests/linux/test_typedKeysNotLogged.py
"""

import ast
import re
from pathlib import Path

import harness
from desktopSession import DesktopSession

#: logHandler.Logger.DEBUGWARNING, the level NVDA logs its expected failures at.
DEBUGWARNING = 15
#: A character on no layout the rig's display has.
CHAR = "\N{SNOWMAN}"
#: Names that hold a key or a character in the files the test reads.
KEY_NAMES = frozenset(("vk", "vkCode", "char", "ch", "keycode", "scan", "scanCode"))
#: The files whose log and exception texts are held here.
FILES = (
	harness.SOURCE / "linuxPlatform" / "keys" / "x11.py",
	harness.SOURCE / "linuxPlatform" / "inputHook.py",
)

#: The X source on the rig's display, set as inputHook's source, then taken away. Every record made and
#: every exception raised while asking is kept, with its whole text.
CHILD = r"""
import logging, time, traceback
from logHandler import log
from linuxPlatform import NoLinuxImplementation, inputHook
from linuxPlatform.keys import x11

# The failures are logged at debugWarning, and a record below its logger's level is never made.
log.setLevel(log.DEBUG)
CHAR = __CHAR__

class Keep(logging.Handler):
	def __init__(self):
		super().__init__(logging.DEBUG)
		self.records = []

	def emit(self, record):
		text = record.getMessage()
		if record.exc_info and record.exc_info[1] is not None:
			text += "\n" + "".join(traceback.format_exception(*record.exc_info))
		self.records.append((record.levelno, text))

source = x11.X11KeySource()
source.start(lambda event: True)
deadline = time.monotonic() + 20
while source._translator is None and time.monotonic() < deadline:
	time.sleep(0.05)
translator = source._translator
# The unassigned codes first, then anything the layout gives no key for.
missing = next(
	vk
	for vk in [*range(0x88, 0x90), *range(0x01, 0xFF)]
	if translator.keycodeFor(vk, False) is None and translator.keycodeFor(vk, True) is None
)

raised = {}

def ask(name, call):
	try:
		call()
	except NoLinuxImplementation as error:
		raised[name] = {
			"what": error.what,
			"text": str(error),
			"repr": repr(error),
			"args": repr(error.args),
			# Without the lines naming files and line numbers, which say nothing of the key.
			"traceback": "".join(
				line
				for line in traceback.format_exception(error)
				if not line.lstrip().startswith('File "')
			),
		}

keep = Keep()
log.addHandler(keep)
try:
	inputHook._source = source
	ask("inject", lambda: source.inject(missing, 0, False, False))
	ask("typeKey", lambda: source.typeKey(missing, False, False))
	ask("keybdEvent", lambda: inputHook.keybdEvent(missing, 0, 0, 0))
	ask("typeKey through inputHook", lambda: inputHook.typeKey(missing, False, False))
	ask("vkKeyScan", lambda: inputHook.vkKeyScan(CHAR))
	assert source.awaitCommands(10)
	inputHook._source = None
	ask("typeKey with no source", lambda: inputHook.typeKey(missing, False, False))
	ask("vkKeyScan with no source", lambda: inputHook.vkKeyScan(CHAR))
	ask("mapVirtualKeyToChar with no source", lambda: inputHook.mapVirtualKeyToChar(missing))
	ask("getKeyState with no source", lambda: inputHook.getKeyState(missing))
finally:
	log.removeHandler(keep)
	source.stop()

result = {"missing": missing, "records": keep.records, "raised": raised}
"""


def namings(vk: int) -> list[str]:
	"""Every way a text could name vk or CHAR, as patterns."""
	forms = [
		rf"\b0[xX]0*{vk:x}\b",
		rf"\b0[xX]0*{vk:X}\b",
		rf"\b{vk}\b",
		rf"\b{ord(CHAR)}\b",
		rf"\b0[xX]0*{ord(CHAR):x}\b",
	]
	return forms + [
		re.escape(text) for text in (CHAR, repr(CHAR), ascii(CHAR), CHAR.encode("unicode_escape").decode())
	]


def test_failuresNameNoKey(tmp_path: Path):
	with DesktopSession(tmp_path / "desktop", windowManager=False):
		done = harness.runChild(tmp_path, CHILD.replace("__CHAR__", repr(CHAR)), timeout=60)
	assert done.error is None, done.error
	done.assertClean(ignoring=("noLinuxImplementation",))
	assert done.noLinuxImplementation == ["keyboard layout lookup"] * 3 + ["keyboard state"]
	value = done.value
	vk = value["missing"]
	texts = [text for _level, text in value["records"]]
	for exception in value["raised"].values():
		texts += [exception["text"], exception["repr"], exception["args"], exception["traceback"]]
	for pattern in namings(vk):
		named = [text for text in texts if re.search(pattern, text)]
		assert not named, (pattern, named)
	# What was said instead, so that the test cannot pass by nothing being logged or raised.
	warnings = [text for level, text in value["records"] if level == DEBUGWARNING]
	assert warnings == ["No key on this keyboard sends the virtual key asked for"] * 4 + [
		"No keyboard to type a key on"
	], warnings
	assert {name: (raised["what"], raised["text"]) for name, raised in value["raised"].items()} == {
		"vkKeyScan": (
			"keyboard layout lookup",
			"which key types this character depends on the keyboard layout",
		),
		"vkKeyScan with no source": (
			"keyboard layout lookup",
			"which key types this character depends on the keyboard layout",
		),
		"mapVirtualKeyToChar with no source": (
			"keyboard layout lookup",
			"what a virtual key types depends on the keyboard layout",
		),
		"getKeyState with no source": ("keyboard state", "the state of a key needs the live keyboard"),
	}
	# The character no source knows went through the source first, and that was not chained on.
	assert "LookupError" not in value["raised"]["vkKeyScan"]["traceback"]


def keyNamesIn(node: ast.JoinedStr) -> set[str]:
	return {
		name.id
		for part in node.values
		if isinstance(part, ast.FormattedValue)
		for name in ast.walk(part.value)
		if isinstance(name, ast.Name) and name.id in KEY_NAMES
	}


def isTextSink(call: ast.Call) -> bool:
	"""Whether the call logs its arguments or makes an exception of them."""
	func = call.func
	if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and func.value.id == "log":
		return True
	name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""
	return name.endswith(("Error", "Exception", "Implementation", "Warning"))


def test_noTextInterpolatesAKey():
	found = []
	for path in FILES:
		for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
			if not isinstance(node, ast.Call) or not isTextSink(node):
				continue
			for argument in [*node.args, *(keyword.value for keyword in node.keywords)]:
				for text in ast.walk(argument):
					if isinstance(text, ast.JoinedStr) and keyNamesIn(text):
						found.append(f"{path.name}:{node.lineno} {sorted(keyNamesIn(text))}")
	assert not found, found
