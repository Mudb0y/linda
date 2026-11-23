"""Menus of a Java Swing program, walked with the keyboard through the Java ATK wrapper, as a JetBrains
IDE's are (issue 13): Swing keeps the keyboard focus on the window's root pane while a menu is open, and
the wrapper says which item is highlighted with a focus of its own.

LINDA_JAVA=/path/to/bin/java LINDA_JAVA_ATK_WRAPPER=/path/to/java-atk-wrapper \
nix develop . -c tests/linux/rig python3 -m pytest -q tests/linux/test_javaMenus.py

LINDA_JAVA names a JDK's java, which runs the fixture from its source, and LINDA_JAVA_ATK_WRAPPER the
wrapper's installation, with lib/libatk-wrapper.so and share/java/java-atk-wrapper.jar.

The tests of IntelliJ IDEA itself run when LINDA_INTELLIJ_IDEA names nixpkgs' intellij-idea package, which
is not free software and so not in the dev shell:

NIXPKGS_ALLOW_UNFREE=1 nix build --impure -o /some/dir/idea nixpkgs#intellij-idea
LINDA_INTELLIJ_IDEA=/some/dir/idea nix develop . -c tests/linux/rig --bind /some/dir python3 -m pytest ...
"""

import os
import re
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import desktopSession
import harness
import pytest
from desktopSession import DesktopSession

SESSION_CONFIG = harness.HERE / "fixtures" / "sessionConfig" / "nvda.ini"
FIXTURE = harness.HERE / "fixtures" / "apps" / "javaMenus.java"
TITLE = "Java menus"
#: Seconds from starting NVDA to its first words; a start under the rig takes about three.
STARTUP_BOUND = 60.0
#: Seconds a JVM takes to show the fixture's window, which it compiles first.
JAVA_START_BOUND = 60.0
#: Seconds with nothing new spoken after which NVDA is taken to have finished speaking.
QUIET = 0.8

#: The project the IDE opens, a folder of one text file, and the IDE's window's name with it open.
IDEA_PROJECT = "demo"
IDEA_TITLE = "demo - IntelliJ IDEA"
IDEA_FILE = "notes.txt"
#: The start of what NVDA says for the editor of IDEA_FILE, as the transcript synth spells it.
IDEA_EDITOR = "Editor for notes dot txt…"
#: Seconds IntelliJ IDEA takes to show its window under the rig, about thirty when nothing else runs.
IDEA_START_BOUND = 240.0
#: Seconds a fresh IDE takes to index its runtime once its editor is up, during which it answered AT-SPI
#: about a key in its editor too late for NVDA, which then said nothing of the key.
IDEA_SETTLE = 40.0

pytestmark = [pytest.mark.needsVariable("LINDA_JAVA"), pytest.mark.needsVariable("LINDA_JAVA_ATK_WRAPPER")]
needsIdea = pytest.mark.needsVariable("LINDA_INTELLIJ_IDEA")


@pytest.fixture
def desktop(tmp_path: Path) -> Iterator[DesktopSession]:
	with DesktopSession(tmp_path / "desktop") as session:
		yield session


def _javaCommand(*args: str) -> list[str]:
	wrapper = Path(os.environ["LINDA_JAVA_ATK_WRAPPER"])
	return [
		os.environ["LINDA_JAVA"],
		"-cp",
		str(wrapper / "share" / "java" / "java-atk-wrapper.jar"),
		f"-Djava.library.path={wrapper / 'lib'}",
		"-Djavax.accessibility.assistive_technologies=org.GNOME.Accessibility.AtkWrapper",
		str(FIXTURE),
		*args,
	]


def _start(desktop: DesktopSession, *args: str) -> None:
	app = desktop.startApp(_javaCommand(*args), title=TITLE, timeout=JAVA_START_BOUND)
	assert app.stdout.readline() == "ready\n"
	desktopSession.activate(TITLE)
	desktop.startNVDA(SESSION_CONFIG)
	desktop.expect(TITLE, "Press me button", since=0, timeout=STARTUP_BOUND)
	_waitForQuiet(desktop)


def _waitForQuiet(desktop: DesktopSession, limit: float = 10) -> None:
	"""Wait until NVDA has said nothing new for QUIET seconds."""
	end = time.monotonic() + limit
	count = desktop.mark()
	last = time.monotonic()
	while time.monotonic() < end:
		time.sleep(0.05)
		now = desktop.mark()
		if now != count:
			count, last = now, time.monotonic()
		elif time.monotonic() - last >= QUIET:
			return


def _step(desktop: DesktopSession, key: str, *expected: str) -> None:
	"""Press key; expected, and nothing more, is what NVDA says."""
	mark = desktop.mark()
	desktop.press(key)
	if expected:
		desktop.expect(*expected, since=mark)
	_waitForQuiet(desktop)
	assert desktop.spokenSince(mark) == list(expected), desktop.spokenSince(mark)


def startIdea(desktop: DesktopSession, directory: Path) -> None:
	"""Start IntelliJ IDEA with its support for screen readers on, the wrapper loaded, every setting in
	directory and its first run's tour left out, open IDEA_FILE in it and start NVDA on its editor.

	The IDE's own runtime has no wrapper; the jar goes on its boot class path, which finds its library on
	the boot library path."""
	package = Path(os.environ["LINDA_INTELLIJ_IDEA"])
	wrapper = Path(os.environ["LINDA_JAVA_ATK_WRAPPER"])
	home = package / "intellij-idea"
	settings = directory / "settings"
	names = ("config", "system", "plugins", "log")
	for name in names:
		(settings / name).mkdir(parents=True)
	properties = settings / "idea.properties"
	properties.write_text(
		"".join(f"idea.{name}.path={settings / name}\n" for name in names)
		+ "ide.support.screenreaders.enabled=true\n"
	)
	options = settings / "idea.vmoptions"
	options.write_text(
		(home / "bin" / "idea64.vmoptions").read_text()
		+ "\n-Djavax.accessibility.assistive_technologies=org.GNOME.Accessibility.AtkWrapper\n"
		+ f"-Xbootclasspath/a:{wrapper / 'share' / 'java' / 'java-atk-wrapper.jar'}\n"
		+ f"-Dsun.boot.library.path={home / 'jbr' / 'lib'}:{wrapper / 'lib'}\n"
		+ "-Djb.consents.confirmation.enabled=false\n"
		+ "-Djb.privacy.policy.text=<!--999.999-->\n"
		+ "-Dide.experimental.ui.onboarding=false\n"
		+ "-Didea.trust.all.projects=true\n"
	)
	project = directory / IDEA_PROJECT
	project.mkdir()
	(project / IDEA_FILE).write_text("hello world\n")
	launch = [
		"env",
		f"IDEA_PROPERTIES={properties}",
		f"IDEA_VM_OPTIONS={options}",
		str(package / "bin" / "intellij-idea"),
	]
	# Its standard output goes to a file, not to startApp's pipe, which nothing reads and which would fill.
	output = directory / "idea.stdout.txt"
	desktop.startApp(
		["sh", "-c", 'exec "$@" > "$0"', str(output), *launch, str(project)],
		title=IDEA_TITLE,
		timeout=IDEA_START_BOUND,
	)
	desktopSession.activate(IDEA_TITLE)
	desktop.startNVDA(SESSION_CONFIG)
	desktop.expect("Project structure tree tree view", since=0, timeout=STARTUP_BOUND)
	time.sleep(IDEA_SETTLE)
	_waitForQuiet(desktop)
	# The file is opened once NVDA runs: NVDA starting with the editor already focused finds another
	# object for it than the one its text changes come from, and says nothing of them.
	mark = desktop.mark()
	# A second start hands the file to the first and quits.
	subprocess.run([*launch, str(project / IDEA_FILE)], stdout=subprocess.DEVNULL, check=True, timeout=120)
	desktop.expect(IDEA_EDITOR, since=mark, timeout=IDEA_START_BOUND)
	_waitForQuiet(desktop)


def _says(desktop: DesktopSession, key: str, *expected: str) -> None:
	"""Press key; NVDA says expected, in order, among what it says."""
	mark = desktop.mark()
	desktop.press(key)
	desktop.expect(*expected, since=mark)
	_waitForQuiet(desktop)


def _back(popups: str) -> tuple[str, ...]:
	"""What closing the popup menu says: the window again after a popup in a window of its own, whose
	items are that window's, as after GTK's context menus, then the button. A menu bar's items are its
	menu's in either kind."""
	return (TITLE, "Press me button") if popups == "heavyweight" else ("Press me button",)


def _closeClean(desktop: DesktopSession) -> None:
	nvda = desktop.nvda
	desktop.close()
	assert nvda.process.returncode == 0, nvda.describe()
	nvda.assertClean(logNames=("nvda.log",))


@pytest.mark.parametrize("popups", ["lightweight", "heavyweight"])
def test_menuBar(desktop: DesktopSession, popups: str):
	"""F10 opens the File menu, which Swing opens with no item highlighted; the arrows go through its
	items, into its submenu and out of it again, and across to the next menu, and Escape closes it."""
	_start(desktop, popups)
	# Swing says a menu is checked while it is open, and that is read as it says it.
	_step(desktop, "F10", "File checked sub Menu 1 of 2")
	_step(desktop, "Down", "New 1 of 4")
	_step(desktop, "Down", "Open 2 of 4")
	_step(desktop, "Down", "Recent checked sub Menu 3 of 4")
	_step(desktop, "Right", "First project 1 of 2")
	# Left closes the submenu and Swing highlights its menu again, which the wrapper does not announce.
	_step(desktop, "Left", "Recent checked sub Menu 3 of 4")
	_step(desktop, "Right", "First project 1 of 2")
	_step(desktop, "Down", "Second project 2 of 2")
	_step(desktop, "Escape", "Press me button")
	_step(desktop, "F10", "File checked sub Menu 1 of 2")
	_step(desktop, "Right", "Edit checked sub Menu 2 of 2")
	_step(desktop, "Down", "Cut 1 of 2")
	_step(desktop, "Left", "File checked sub Menu 1 of 2")
	_step(desktop, "Escape", "Press me button")
	_closeClean(desktop)


@pytest.mark.parametrize("popups", ["lightweight", "heavyweight"])
def test_submenuOfPopupItems(desktop: DesktopSession, popups: str):
	"""A submenu whose items give its popup menu as their parent, as New in a JetBrains IDE's File menu
	does (issue 13): Left closes it and says the submenu again, after the window when the items were in
	a window of their own, and Escape, which closes every menu, says nothing of the submenu."""
	_start(desktop, popups, "popupItems")
	_step(desktop, "F10", "File checked sub Menu 1 of 2")
	_step(desktop, "Down", "New 1 of 4")
	_step(desktop, "Down", "Open 2 of 4")
	_step(desktop, "Down", "Recent checked sub Menu 3 of 4")
	_step(desktop, "Right", "First project 1 of 2")
	_step(desktop, "Down", "Second project 2 of 2")
	window = (TITLE,) if popups == "heavyweight" else ()
	_step(desktop, "Left", *window, "Recent checked sub Menu 3 of 4")
	_step(desktop, "Right", "First project 1 of 2")
	_step(desktop, "Escape", "Press me button")
	_closeClean(desktop)


@pytest.mark.parametrize("popups", ["lightweight", "heavyweight"])
def test_popupMenu(desktop: DesktopSession, popups: str):
	"""Shift+F10 opens the button's popup menu, which Swing opens with no item highlighted, and says
	nothing of the root pane the keyboard focus goes to; the arrows go through its items."""
	_start(desktop, popups)
	_step(desktop, "shift+F10")
	_step(desktop, "Down", "Alpha 1 of 3")
	_step(desktop, "Down", "Beta 2 of 3")
	_step(desktop, "Up", "Alpha 1 of 3")
	_step(desktop, "Escape", *_back(popups))
	_closeClean(desktop)


@needsIdea
def test_ideaMenus(desktop: DesktopSession, tmp_path: Path):
	"""IntelliJ IDEA's File menu, its New submenu and the editor's context menu (issue 13): Left and
	Escape close New's submenu and say New again, and the context menu's items are read."""
	startIdea(desktop, tmp_path)
	_says(desktop, "alt+f", "New sub Menu…")
	_says(desktop, "Right", "Project... 1 of…")
	_says(desktop, "Down", "Project from Existing Sources... 2 of…")
	_says(desktop, "Left", "New sub Menu…")
	_says(desktop, "Right", "Project... 1 of…")
	_says(desktop, "Escape", "New sub Menu…")
	_says(desktop, "Escape", IDEA_EDITOR)
	# The menu opens under the pointer, which may highlight an item at once.
	desktop.press("Menu")
	_waitForQuiet(desktop)
	mark = desktop.mark()
	desktop.press("Down")
	_waitForQuiet(desktop)
	spoken = desktop.spokenSince(mark)
	assert any(re.search(r" \d+ of \d+$", text) for text in spoken), spoken
	_says(desktop, "Escape", IDEA_EDITOR)
	_closeClean(desktop)
