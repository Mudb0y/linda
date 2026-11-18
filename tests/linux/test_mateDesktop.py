"""MATE's own programs walked with the keyboard, as someone using Linda as their only screen reader
walks them: what NVDA says for each step, compared with what it says for the same kind of control on
Windows. One walk per program, with keys pressed on the rig's display under marco.

nix develop . -c tests/linux/rig python3 -m pytest -q tests/linux/test_mateDesktop.py

The transcript holds what the synthesizer is given after symbols are spoken as words, so "a.txt" is
"a dot txt" and "Alt+N" is "Alt plus N".
"""

import os
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import desktopSession
import harness
import pytest
from desktopSession import DesktopSession

SESSION_CONFIG = harness.HERE / "fixtures" / "sessionConfig" / "nvda.ini"
#: Seconds from starting NVDA to its first words; a start under the rig takes about three.
STARTUP_BOUND = 60.0
#: Seconds to wait for a window a program opens.
WINDOW_TIMEOUT = 30.0
#: Files in the folder that must not slow NVDA down.
BIG_FOLDER_FILES = 3000


@pytest.fixture
def desktop(tmp_path: Path) -> Iterator[DesktopSession]:
	with DesktopSession(tmp_path / "desktop") as session:
		yield session


def step(desktop: DesktopSession, chord: str, *expected: str, timeout: float = 5) -> int:
	"""Press chord and wait for expected, in order; return the mark taken before the key."""
	mark = desktop.mark()
	desktop.press(chord)
	desktop.expect(*expected, since=mark, timeout=timeout)
	return mark


def switchWorkspace(desktop: DesktopSession, arrow: str, workspace: str, *expected: str) -> None:
	"""Press Control+Alt+arrow holding Control and Alt until NVDA names workspace, then wait for
	expected.

	marco shows the popup that names the workspace only if Alt is still down when it handles the
	arrow, as it is for a person. xdotool's chord lets go of Alt some 12 ms after the arrow, which a
	busy marco misses, and then switches with nothing named."""
	mark = desktop.mark()
	subprocess.run(["xdotool", "keydown", "control+alt"], check=True)
	try:
		desktop.press(arrow)
		desktop.expect(workspace, since=mark)
	finally:
		subprocess.run(["xdotool", "keyup", "control+alt"], check=True)
	desktop.expect(workspace, *expected, since=mark)


def waitForWindow(title: str, timeout: float = WINDOW_TIMEOUT) -> None:
	"""Wait for a top-level window named title, from a program that may hand its work to a copy of
	itself already running, and give it the keyboard."""
	deadline = time.monotonic() + timeout
	while title not in desktopSession.topLevelNames():
		assert time.monotonic() < deadline, f"no window {title!r}: {desktopSession.topLevelNames()}"
		time.sleep(0.1)
	desktopSession.activate(title)


def startNVDA(desktop: DesktopSession, *expected: str) -> harness.Launch:
	"""Start NVDA and wait for expected, or with nothing expected, until it has started."""
	nvda = desktop.startNVDA(SESSION_CONFIG)
	if expected:
		desktop.expect(*expected, since=0, timeout=STARTUP_BOUND)
	else:
		nvda.waitForLog("NVDA initialized", STARTUP_BOUND)
	return nvda


def assertQuitCleanly(desktop: DesktopSession, nvda: harness.Launch) -> None:
	desktop.close()
	assert nvda.process.returncode == 0, nvda.describe()
	nvda.assertClean(logNames=("nvda.log",))


def makeFolder(path: Path, *files: str) -> Path:
	path.mkdir(parents=True, exist_ok=True)
	for name in files:
		if name.endswith("/"):
			(path / name).mkdir()
		else:
			(path / name).write_text("hello\n")
	return path


def test_cajaFolderWindow(desktop: DesktopSession, tmp_path: Path):
	folder = makeFolder(tmp_path / "docs", "sub/", "alpha.txt", "beta.txt")
	(folder / "sub" / "inner.txt").write_text("x")
	desktop.startApp(["caja", "--no-desktop", str(folder)], title="docs", timeout=WINDOW_TIMEOUT)
	desktopSession.activate("docs")
	nvda = startNVDA(desktop, "docs", "Icon View list")

	# Icon view: Windows Explorer's items in icon mode, each a list item with its place.
	step(desktop, "Down", "sub 1 of 3")
	step(desktop, "Right", "alpha dot txt 2 of 3")
	step(desktop, "Right", "beta dot txt 3 of 3")

	# The context menu opens on no item, as GTK's do, and closing it gives the file back.
	step(desktop, "shift+F10", "menu")
	step(desktop, "Down", "Open…")
	step(desktop, "Escape", "docs", "beta dot txt 3 of 3")

	# Renaming.
	step(desktop, "F2", "beta dot txt edit…")
	step(desktop, "Escape", "beta dot txt 3 of 3")

	# The properties dialog, where the file's type is a label the keyboard reaches, read with its title.
	step(
		desktop, "alt+Return", "beta dot txt Properties dialog", "Name: edit Alt plus N selected beta dot txt"
	)
	step(desktop, "Tab", "Type: text slash plain…")
	step(desktop, "Escape", "docs", "beta dot txt 3 of 3")

	# List view: the item of a Windows list view in details, the columns after the first by header.
	step(desktop, "control+2", "List View list")
	# Caja is still filling the new view when it says it has the focus; a key in the first tens of
	# milliseconds moves its cursor without a word.
	time.sleep(0.5)
	step(desktop, "Up", "alpha dot txt; Size: 6 bytes; Type: …")

	# Into a folder and back, which Caja does in the same view without moving the focus.
	step(desktop, "control+1", "Icon View list")
	step(desktop, "Home", "sub 1 of 3")
	step(desktop, "Return", "sub")
	step(desktop, "Down", "inner dot txt 1 of 1")
	step(desktop, "alt+Up", "docs", "sub 1 of 3")

	# The menu bar.
	step(desktop, "alt+f", "New Tab Ctrl plus T T 1 of…")
	step(desktop, "Right", "Cut Ctrl plus X T…")
	step(desktop, "Escape", "sub 1 of 3")
	assertQuitCleanly(desktop, nvda)


def test_cajaBigFolder(desktop: DesktopSession, tmp_path: Path):
	folder = tmp_path / "big"
	folder.mkdir()
	for number in range(BIG_FOLDER_FILES):
		(folder / f"file{number:04d}.txt").write_text("x")
	desktop.startApp(["caja", "--no-desktop", str(folder)], title="big", timeout=WINDOW_TIMEOUT)
	desktopSession.activate("big")
	nvda = startNVDA(desktop, "big", "Icon View list")
	step(desktop, "Down", "file 0000 dot txt 1 of 3000")
	step(desktop, "End", "file 2999 dot txt 3000 of 3000")
	step(desktop, "Home", "file 0000 dot txt 1 of 3000")
	# Caja is busy for a second building its list view, and the keys pressed meanwhile reach it late.
	step(desktop, "control+2", "List View list", timeout=10)
	step(desktop, "End", "file 2999 dot txt; Size: 1 byte; …", timeout=10)
	step(desktop, "Up", "file 2998 dot txt; Size: 1 byte; …")
	step(desktop, "Insert+t", "big")
	assertQuitCleanly(desktop, nvda)


def test_cajaDesktop(desktop: DesktopSession, tmp_path: Path):
	makeFolder(Path.home() / "Desktop", "notes.txt", "photos/")
	desktop.startApp(
		["caja", "--force-desktop", "--no-default-window"], title="Desktop", timeout=WINDOW_TIMEOUT
	)
	nvda = startNVDA(desktop)
	step(desktop, "control+alt+d", "Desktop", "Icon View list")
	step(desktop, "Down", "Computer 1 of 5")
	step(desktop, "End", "Trash 5 of 5")
	step(desktop, "Insert+t", "Desktop")
	assertQuitCleanly(desktop, nvda)


def startPluma(desktop: DesktopSession, *paths: Path) -> str:
	"""Start Pluma on paths and give it the keyboard; return its window's title."""
	title = f"{paths[0].name} (~/{paths[0].parent.relative_to(Path.home())}) - Pluma"
	desktop.startApp(["pluma", *map(str, paths)], title=title, timeout=WINDOW_TIMEOUT)
	desktopSession.activate(title)
	return title


def test_pluma(desktop: DesktopSession):
	folder = makeFolder(Path.home() / "pluma")
	document = folder / "a.txt"
	document.write_text("first line\nsecond line here\nthird\n")
	startPluma(desktop, document)
	nvda = startNVDA(desktop, "a dot txt (  slash pluma) - Pluma", "edit multi line first line")
	step(desktop, "Down", "second line here")
	step(desktop, "Insert+Up", "second line here")
	for letter in "xy":
		mark = desktop.mark()
		desktop.typeText(letter)
		desktop.expect(letter, since=mark)

	step(desktop, "F10", "File sub Menu Alt plus F 1 of 7")
	step(desktop, "Down", "New Ctrl plus N N 1 of 10")
	step(desktop, "Right", "Undo Ctrl plus Z U 1 of…")
	step(desktop, "Escape", "edit multi line…")

	step(desktop, "control+f", "Find dialog Search for:", "Search for: combo box collapsed", "edit…")
	step(desktop, "Escape", "star a dot txt (  slash pluma) - Pluma", "edit multi line…")
	step(desktop, "control+h", "Replace dialog…", "Search for: combo box collapsed", "edit…")
	step(desktop, "Escape", "star a dot txt (  slash pluma) - Pluma", "edit multi line…")

	# Documents are tabs, and switching between them says which one the focus is in.
	step(desktop, "control+n", "Unsaved Document 1 tab selected 2 of 2", "edit multi line blank")
	step(desktop, "control+alt+Page_Up", "star a dot txt tab selected 1 of 2", "edit multi line…")
	assertQuitCleanly(desktop, nvda)


def test_plumaBigFile(desktop: DesktopSession):
	document = makeFolder(Path.home() / "bigtext") / "big.txt"
	document.write_text("".join(f"Line {number} of a big file.\n" for number in range(20000)))
	startPluma(desktop, document)
	nvda = startNVDA(
		desktop, "big dot txt (  slash bigtext) - Pluma", "edit multi line Line 0 of a big file."
	)
	# Pluma keeps a file's last newline out of its text.
	step(desktop, "control+End", "Line 19999 of a big file.")
	step(desktop, "Up", "Line 19998 of a big file.")
	step(desktop, "control+Home", "Line 0 of a big file.")
	step(desktop, "Insert+Down", "Line 0 of a big file.", "Line 1 of a big file.", "Line 2 of a big file.")
	desktop.press("Control_L")
	assertQuitCleanly(desktop, nvda)


def test_gtkDialogs(desktop: DesktopSession):
	folder = makeFolder(Path.home() / "dialogs", "sub/", "a.txt", "b.txt")
	startPluma(desktop, folder / "a.txt")
	nvda = startNVDA(desktop, "a dot txt (  slash dialogs) - Pluma", "edit multi line hello")

	# GTK's open dialog, with its files spoken as a Windows list view's.
	step(desktop, "control+o", "Open Files dialog", "Files list")
	step(desktop, "Down", "b dot txt; Size: 6 bytes; Type: …")
	step(desktop, "Tab", "Character Encoding: combo box Automatically Detected collapsed Alt plus H")
	step(desktop, "Escape", "a dot txt (  slash dialogs) - Pluma", "edit multi line hello")

	step(desktop, "control+shift+s", "Save As... dialog", "Name: edit Alt plus N selected a")
	step(desktop, "Escape", "a dot txt (  slash dialogs) - Pluma", "edit multi line hello")

	# A message dialog reads its message as it opens, as NVDA reads a Windows message box.
	mark = desktop.mark()
	desktop.typeText("z")
	desktop.expect("z", since=mark)
	step(
		desktop,
		"control+w",
		"Question dialog Save changes to document  a dot txt  before closing?…",
		"Save button Alt plus S",
	)
	step(desktop, "Tab", "Close without Saving button Alt plus W")
	step(desktop, "Escape", "star a dot txt (  slash dialogs) - Pluma", "edit multi line…")

	step(desktop, "alt+h", "Contents F 1 C 1 of 2")
	step(desktop, "Up", "About A 2 of 2")
	step(desktop, "Return", "About Pluma dialog Pluma 1.…", "Credits toggle button not pressed Alt plus R")
	step(desktop, "Escape", "star a dot txt (  slash dialogs) - Pluma", "edit multi line…")
	assertQuitCleanly(desktop, nvda)


def test_matePanel(desktop: DesktopSession):
	folder = makeFolder(Path.home() / "panel", "a.txt")
	desktop.startApp(["mate-panel"], title="Bottom Panel", timeout=WINDOW_TIMEOUT)
	title = startPluma(desktop, folder / "a.txt")
	spokenTitle = "a dot txt (  slash panel) - Pluma"
	nvda = startNVDA(desktop, spokenTitle, "edit multi line hello")

	# The main menu; its Applications submenu is filled from mate-menus, which the rig does not have.
	step(desktop, "alt+F1", "Top Panel", "Applications sub Menu 1 of 3")
	step(desktop, "Right", "Places sub Menu Alt plus P 2 of 3")
	step(desktop, "Down", "Home Folder H 1 of…")
	step(desktop, "Escape", spokenTitle, "edit multi line hello")

	step(desktop, "alt+F2", "Run Application dialog", "combo box collapsed", "edit blank")
	for letter in "plu":
		mark = desktop.mark()
		desktop.typeText(letter)
		desktop.expect(letter, since=mark)
	step(desktop, "Tab", "Run in terminal check box not checked…")
	step(desktop, "Escape", spokenTitle, "edit multi line hello")

	# The keyboard on the panel, and the window list on it.
	step(desktop, "control+alt+Tab", "Bottom Panel", "panel")
	step(desktop, "Tab", "Show Desktop Button toggle button…")
	step(desktop, "Tab", "Window List grouping…", f"{spokenTitle} toggle button not pressed")
	step(desktop, "Escape")
	desktopSession.activate(title)
	assertQuitCleanly(desktop, nvda)


def notificationDaemon() -> list[str]:
	"""The command that runs mate-notification-daemon, which its package keeps in libexec, off PATH; the
	dev shell names the package in XDG_DATA_DIRS for its settings schemas."""
	for directory in os.environ.get("XDG_DATA_DIRS", "").split(":"):
		if "-mate-notification-daemon-" in directory:
			daemon = Path(directory.split("/share/")[0]) / "libexec" / "mate-notification-daemon"
			if daemon.is_file():
				return [str(daemon)]
	raise AssertionError("mate-notification-daemon is not in the dev shell")


def test_notifications(desktop: DesktopSession):
	folder = makeFolder(Path.home() / "notify", "a.txt")
	daemon = subprocess.Popen(notificationDaemon(), stderr=subprocess.DEVNULL)
	try:
		startPluma(desktop, folder / "a.txt")
		nvda = startNVDA(desktop, "a dot txt (  slash notify) - Pluma", "edit multi line hello")
		# Spoken as NVDA speaks a Windows toast: the title and the text, once, and the focus stays.
		mark = desktop.mark()
		subprocess.run(["notify-send", "Backup finished", "All 12 files were copied."], check=True)
		desktop.expect("Backup finished All 12 files were copied.", since=mark)
		subprocess.run(["notify-send", "Second", "Another one."], check=True)
		desktop.expect("Second Another one.", since=mark)
		time.sleep(1)
		spoken = desktop.spokenSince(mark)
		assert spoken == ["Backup finished All 12 files were copied.", "Second Another one."], spoken
		# Nothing takes the keyboard focus while Insert is held, and a query keeps the focused state.
		step(desktop, "Insert+Tab", "edit focused multi line hello")
		assertQuitCleanly(desktop, nvda)
	finally:
		daemon.terminate()
		daemon.wait(5)


def test_windowsComingAndGoing(desktop: DesktopSession):
	folder = makeFolder(Path.home() / "windows", "sub/", "a.txt")
	desktop.startApp(["caja", "--no-desktop", str(folder)], title="windows", timeout=WINDOW_TIMEOUT)
	startPluma(desktop, folder / "a.txt")
	nvda = startNVDA(desktop, "a dot txt (  slash windows) - Pluma", "edit multi line hello")

	# Switching workspaces, which marco names in a popup gone before its name can be read back; an
	# empty workspace leaves the desktop with the focus.
	switchWorkspace(desktop, "Right", "Workspace 2", "Desktop desktop pane")
	switchWorkspace(
		desktop, "Left", "Workspace 1", "a dot txt (  slash windows) - Pluma", "edit multi line hello"
	)

	# The focused application quits, and the window marco gives the keyboard to is read.
	step(desktop, "control+q", "windows", "Icon View list")

	# A tree view: Caja's side pane showing the folder tree.
	step(desktop, "F6", "Places toggle button not pressed")
	step(desktop, "space", "menu")
	step(desktop, "Down", "Places 1 of 7")
	step(desktop, "Down", "Information 2 of 7")
	step(desktop, "Down", "Tree 3 of 7")
	step(desktop, "Return", "windows", "Tree toggle button not pressed")
	step(desktop, "Tab", "button Close the side pane")
	step(desktop, "Tab", "tree view", "windows collapsed…")
	step(desktop, "shift+Right", "expanded")
	step(desktop, "Down", "sub collapsed…")

	# The last window closes, and the desktop has the focus.
	step(desktop, "control+w", "Desktop desktop pane")
	assertQuitCleanly(desktop, nvda)
