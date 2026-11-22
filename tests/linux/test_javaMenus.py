"""Menus of a Java Swing program, walked with the keyboard through the Java ATK wrapper, as a JetBrains
IDE's are (issue 13): Swing keeps the keyboard focus on the window's root pane while a menu is open, and
the wrapper says which item is highlighted with a focus of its own.

LINDA_JAVA=/path/to/bin/java LINDA_JAVA_ATK_WRAPPER=/path/to/java-atk-wrapper \
nix develop . -c tests/linux/rig python3 -m pytest -q tests/linux/test_javaMenus.py

LINDA_JAVA names a JDK's java, which runs the fixture from its source, and LINDA_JAVA_ATK_WRAPPER the
wrapper's installation, with lib/libatk-wrapper.so and share/java/java-atk-wrapper.jar.
"""

import os
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

pytestmark = pytest.mark.skipif(
	not (os.environ.get("LINDA_JAVA") and os.environ.get("LINDA_JAVA_ATK_WRAPPER")),
	reason="LINDA_JAVA and LINDA_JAVA_ATK_WRAPPER name no JDK and Java ATK wrapper",
)


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
	items, into its submenu and across to the next menu, and Escape closes it."""
	_start(desktop, popups)
	# Swing says a menu is checked while it is open, and that is read as it says it.
	_step(desktop, "F10", "File checked sub Menu 1 of 2")
	_step(desktop, "Down", "New 1 of 4")
	_step(desktop, "Down", "Open 2 of 4")
	_step(desktop, "Down", "Recent checked sub Menu 3 of 4")
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
