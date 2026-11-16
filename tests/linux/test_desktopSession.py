"""The desktop tests read applications on: marco, the fixture applications on the accessibility
desktop, keys typed into them, and NVDA started beside them.

nix develop . -c tests/linux/rig python3 -m pytest tests/linux/test_desktopSession.py
"""

import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import desktopSession
import harness
import pytest
from desktopSession import DesktopSession

BOOT_CONFIG = harness.HERE / "fixtures" / "bootConfig" / "nvda.ini"
#: Seconds from starting NVDA to its first words; a start under the rig takes about three.
STARTUP_BOUND = 60.0


@pytest.fixture
def desktop(tmp_path: Path) -> Iterator[DesktopSession]:
	with DesktopSession(tmp_path / "desktop") as session:
		yield session


def test_windowManagerIsMarco(desktop: DesktopSession):
	shown = subprocess.run(["wmctrl", "-m"], capture_output=True, text=True, check=True).stdout
	# It names itself "Metacity (Marco)", after the window manager it was forked from.
	assert shown.startswith("Name: ") and "Marco" in shown.splitlines()[0], shown


@pytest.mark.parametrize(
	("name", "args", "title"),
	[
		("gtkFixture", (), "Fixture window"),
		("qtFixture", (), "Qt fixture window"),
		("wxfixture", (), "wx fixture"),
		("gtk4", (), "GTK4 fixture"),
	],
)
def test_fixtureAppearsOnTheAccessibilityDesktop(
	desktop: DesktopSession,
	name: str,
	args: tuple[str, ...],
	title: str,
):
	app = desktop.startApp(desktop.fixture(name, *args), title=title, timeout=5)
	assert app.poll() is None


def _entryText(windowTitle: str) -> str:
	"""The text of the first editable text field in the window, read through AT-SPI."""
	Atspi = desktopSession._atspi()
	desktop = Atspi.get_desktop(0)
	for appIndex in range(desktop.get_child_count()):
		app = desktop.get_child_at_index(appIndex)
		for index in range(app.get_child_count() if app else 0):
			window = app.get_child_at_index(index)
			if window is not None and window.get_name() == windowTitle:
				found = _firstEntry(window)
				assert found is not None, f"{windowTitle!r} has no editable text field"
				return Atspi.Text.get_text(found, 0, -1)
	raise AssertionError(f"no window {windowTitle!r}")


def _firstEntry(node):
	if node.get_role_name() == "text" and node.get_state_set().contains(
		desktopSession._atspi().StateType.EDITABLE
	):
		return node
	for index in range(node.get_child_count()):
		child = node.get_child_at_index(index)
		if child is not None and (found := _firstEntry(child)) is not None:
			return found
	return None


def test_typedTextReachesTheGtkEntry(desktop: DesktopSession):
	app = desktop.startApp(desktop.fixture("gtkFixture"), title="Fixture window")
	# It is on the accessibility desktop before it has presented itself and has the keyboard.
	assert app.stdout.readline() == "ready\n"
	# The mnemonic of the entry's label, which focuses the entry and selects all of "Alice".
	desktop.press("alt+n")
	desktop.typeText("Bob")
	text = ""
	for _ in range(40):
		text = _entryText("Fixture window")
		if text == "Bob":
			break
		time.sleep(0.05)
	assert text == "Bob"


def test_nvdaSpeaksTheFixturesFirstControl(desktop: DesktopSession, tmp_path: Path):
	desktop.startApp(desktop.fixture("gtkFixture"), title="Fixture window")
	config = tmp_path / "nvda.ini"
	text = BOOT_CONFIG.read_text(encoding="utf-8")
	# The welcome dialog would take the focus from the fixture.
	config.write_text(
		text.replace("[general]\n", "[general]\n\tshowWelcomeDialogAtStartup = False\n", 1),
		encoding="utf-8",
	)
	nvda = desktop.startNVDA(config)
	# The transcript holds what the synthesizer is given, after symbols are spoken as words.
	desktop.expect(
		"Fixture window", "Press me button Does nothing Alt plus P", since=0, timeout=STARTUP_BOUND
	)
	desktop.close()
	assert nvda.process.returncode == 0, nvda.describe()
	nvda.assertClean(logNames=("nvda.log",))
