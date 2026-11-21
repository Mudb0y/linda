"""Only displayServer decides what kind of session this is: no other file under source/ names the variables
it decides from, outside the files listed here with the reason each may.

The check compares every string and bytes constant in the parsed code, so a name that merely contains
one of the variables, or a docstring that mentions one, is not caught, and a variable read however it
is spelt in code is.

nix develop . -c tests/linux/rig python3 -m pytest -q tests/linux/test_sessionSeam.py
"""

import ast
import os
from pathlib import Path

import harness

SESSION_VARIABLES = frozenset({"DISPLAY", "WAYLAND_DISPLAY", "XDG_SESSION_TYPE"})

#: Files that name the variables for good, by their path under source/, with why.
PERMANENT = {
	"linuxPlatform/displayServer.py": "it decides the kind from them",
	"linuxPlatform/gammaCurtain.py": "its refusal of any session that says Wayland is stricter than the "
	"kind, which test_screenCurtain.py pins, and it records the X display whose ramps it changed",
	"linuxPlatform/sessionLock.py": "it reads other processes' environments, not its own",
	"linuxPlatform/singleInstance.py": "it names its locks after the variables' raw values",
	"linuxPlatform/keys/kernel.py": "the X mode's own check that it has a display to follow",
}
#: Files that still name them until the package that owns each asks displayServer instead. Their
#: literals are allowed and not expected, so each entry goes when its file no longer needs it.
TOLERATED = {
	"linuxPlatform/keys/__init__.py": "createSource tests DISPLAY until package WR asks for the kind",
	"linuxPlatform/pointer.py": "isAvailable tests DISPLAY until package SW asks for the kind",
	"linuxPlatform/visualHighlight.py": "canStart tests XDG_SESSION_TYPE until package SW asks saysWayland",
}


def namings(code: bytes | str, filename: str = "<code>") -> list[tuple[int, str]]:
	"""The line and value of each constant in code equal to one of SESSION_VARIABLES."""
	found = []
	for node in ast.walk(ast.parse(code, filename)):
		if isinstance(node, ast.Constant) and isinstance(node.value, (str, bytes)):
			value = node.value.decode("latin-1") if isinstance(node.value, bytes) else node.value
			if value in SESSION_VARIABLES:
				found.append((node.lineno, value))
	return sorted(found)


def pythonFiles() -> list[str]:
	"""Every Python file under source/, by its path there."""
	files = []
	for directory, subdirectories, names in os.walk(harness.SOURCE):
		subdirectories[:] = [name for name in subdirectories if name != "__pycache__"]
		for name in names:
			if name.endswith((".py", ".pyw")):
				files.append(Path(directory, name).relative_to(harness.SOURCE).as_posix())
	return sorted(files)


def namingsIn(relative: str) -> list[tuple[int, str]]:
	path = harness.SOURCE / relative
	return namings(path.read_bytes(), str(path))


def test_onlyTheListedFilesNameTheSessionVariables():
	files = pythonFiles()
	# A walk that found almost nothing would pass for the wrong reason.
	assert len(files) > 500, len(files)
	unlisted = {}
	for relative in files:
		if relative in PERMANENT or relative in TOLERATED:
			continue
		found = namingsIn(relative)
		if found:
			unlisted[relative] = found
	assert unlisted == {}, (
		"ask linuxPlatform.displayServer instead of reading these, or list the file with its reason: "
		f"{unlisted}"
	)


def test_everyListedFileIsThereAndEachPermanentOneStillNamesThem():
	for relative in (*PERMANENT, *TOLERATED):
		assert (harness.SOURCE / relative).is_file(), relative
	assert {relative: bool(namingsIn(relative)) for relative in PERMANENT} == dict.fromkeys(PERMANENT, True)


def test_namesThatOnlyLookAlikeDoNotTrip():
	protocol = (harness.SOURCE / "_remoteClient" / "protocol.py").read_text(encoding="utf-8")
	assert '\tDISPLAY = "display"\n' in protocol
	x11 = (harness.SOURCE / "linuxPlatform" / "keys" / "x11.py").read_text(encoding="utf-8")
	assert '"""A KeySource reading the X display named by DISPLAY."""' in x11
	assert namingsIn("_remoteClient/protocol.py") == []
	assert namingsIn("linuxPlatform/keys/x11.py") == []


def test_everyWayOfReadingThemInCodeIsCaught():
	code = """
import os
a = os.environ.get("DISPLAY")
b = os.environ['WAYLAND_DISPLAY']
c = os.getenv("XDG_SESSION_TYPE", "")
d = b"DISPLAY" in open("/proc/1/environ", "rb").read()
e = os.environ[f"DISPLAY"]
f = {"WAYLAND_DISPLAY": None}
"""
	assert namings(code) == [
		(3, "DISPLAY"),
		(4, "WAYLAND_DISPLAY"),
		(5, "XDG_SESSION_TYPE"),
		(6, "DISPLAY"),
		(7, "DISPLAY"),
		(8, "WAYLAND_DISPLAY"),
	]
	assert namings('DISPLAY = "display"\n"""Named by DISPLAY."""\nx = "DISPLAY_SIZE"\n') == []
