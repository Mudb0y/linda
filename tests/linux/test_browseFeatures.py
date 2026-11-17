"""What NVDA does on web pages beyond reading them: live regions and alerts spoken as they change,
tables read cell by cell, the elements list, and the language a page marks.

BASEDREADER_RIG_WM=marco nix develop . -c tests/linux/rig python3 -m pytest -q tests/linux/test_browseFeatures.py

Each page gets its own NVDA and Firefox, NVDA first, as on a desktop where the screen reader is
already running. Within a page the tests are steps in order, each starting where the one before
left the page.
"""

import os
import subprocess
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import desktopSession
import harness
import pytest
import webSession
from desktopSession import DesktopSession

#: Seconds from starting NVDA to its being ready.
STARTUP_BOUND = 60.0
#: Seconds a step's speech has to come.
STEP_BOUND = 5.0
#: Seconds to wait after a step for anything more it starts.
SETTLE = 0.5
#: Seconds the page's own reading on load has to start.
LOAD_BOUND = 10.0
#: marco's keys for moving between workspaces are Control+Alt+arrows, which NVDA moves between table
#: cells with: a desktop where NVDA reads tables has them off, set where GSettings' keyfile backend
#: reads them.
WORKSPACE_KEYS_OFF = "[org/mate/marco/global-keybindings]\n" + "".join(
	f"switch-to-workspace-{direction}='disabled'\n" for direction in ("left", "right", "up", "down")
)


@dataclass
class Session:
	desktop: DesktopSession
	nvda: harness.Launch
	firefox: webSession.FirefoxProcess
	#: Whether marco leaves Control+Alt+arrows alone.
	workspaceKeysOff: bool = False


def _startWindowManagerWithoutWorkspaceKeys(desktop: DesktopSession) -> None:
	config = desktop.directory / "config"
	keyfile = config / "glib-2.0" / "settings" / "keyfile"
	keyfile.parent.mkdir(parents=True)
	keyfile.write_text(WORKSPACE_KEYS_OFF)
	with open(desktop.directory / "wm.log", "w") as log:
		# The session's close stops the window manager it holds.
		desktop._windowManager = subprocess.Popen(
			["marco", "--no-force-fullscreen"],
			stdout=log,
			stderr=subprocess.STDOUT,
			env=dict(os.environ, GSETTINGS_BACKEND="keyfile", XDG_CONFIG_HOME=str(config)),
		)
	deadline = time.monotonic() + desktopSession.WINDOW_MANAGER_TIMEOUT
	while not desktopSession.windowManagerIsRunning():
		assert desktop._windowManager.poll() is None, (desktop.directory / "wm.log").read_text()
		assert time.monotonic() < deadline, "marco did not set up its desktops"
		time.sleep(0.05)


def _openPage(
	tmp_path_factory: pytest.TempPathFactory,
	page: str,
	firstLine: str,
	*,
	workspaceKeysOff: bool = False,
) -> Iterator[Session]:
	webSession.requireBrowseLibraries()
	directory = tmp_path_factory.mktemp(Path(page).stem)
	ownWindowManager = workspaceKeysOff and not desktopSession.windowManagerIsRunning()
	with DesktopSession(directory / "desktop", windowManager=not ownWindowManager) as desktop:
		if ownWindowManager:
			_startWindowManagerWithoutWorkspaceKeys(desktop)
		nvda = desktop.startNVDA(webSession.BROWSE_CONFIG)
		nvda.waitForLog("NVDA initialized", STARTUP_BOUND)
		mark = desktop.mark()
		firefox = webSession.startFirefox(desktop, page)
		desktop.expect(firstLine, since=mark, timeout=LOAD_BOUND)
		# Say all on load is cut short, so that what a step says is not queued behind it.
		desktop.press("Control_L")
		time.sleep(SETTLE)
		yield Session(desktop, nvda, firefox, ownWindowManager)


def step(session: Session, chord: str, *expected: str, timeout: float = STEP_BOUND) -> int:
	"""Press chord and wait for expected, in order; return the mark taken before the key."""
	desktop = session.desktop
	mark = desktop.mark()
	desktop.press(chord)
	desktop.expect(*expected, since=mark, timeout=timeout)
	time.sleep(SETTLE)
	return mark


def stepSaying(session: Session, chord: str, fragment: str, timeout: float = STEP_BOUND) -> int:
	"""Press chord and wait for something said that holds fragment; return the mark taken before the
	key. For keys whose speech starts with where the caret came from: "out of table"."""
	desktop = session.desktop
	mark = desktop.mark()
	desktop.press(chord)
	deadline = time.monotonic() + timeout
	while not any(fragment in text for text in desktop.spokenSince(mark)):
		assert time.monotonic() < deadline, (
			f"NVDA did not say {fragment!r} within {timeout} s; it spoke {desktop.spokenSince(mark)}"
		)
		time.sleep(0.05)
	time.sleep(SETTLE)
	return mark


def press(session: Session, button: str, *expected: str, timeout: float = STEP_BOUND) -> int:
	"""Move to the button named button by b, press it with Enter, and wait for expected."""
	desktop = session.desktop
	step(session, "Control_L+Home", "…")
	for _ in range(12):
		mark = desktop.mark()
		desktop.press("b")
		desktop.expect("…", since=mark)
		time.sleep(0.2)
		if any(text.startswith(f"{button} button") for text in desktop.spokenSince(mark)):
			break
	else:
		raise AssertionError(f"b never reached the button {button}")
	return step(session, "Return", *expected, timeout=timeout)


@pytest.fixture(scope="class")
def live(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Session]:
	yield from _openPage(tmp_path_factory, "featureLive.html", "heading level 1 Live regions")


class TestLiveRegions:
	def test_politeAddition(self, live: Session):
		press(live, "Add polite", "Polite message 1")
		mark = press(live, "Add polite", "Polite message 2")
		assert "Polite message 1" not in live.desktop.spokenSince(mark)

	def test_assertiveAddition(self, live: Session):
		press(live, "Add assertive", "Assertive message 1")

	def test_atomicRegionIsReadWhole(self, live: Session):
		"""Only the number changes, and the whole status is read."""
		press(live, "Count", "Results: 4 found")

	def test_alertIsSpokenWithItsRole(self, live: Session):
		press(live, "Show alert", "alert Name is required")

	def test_outputElement(self, live: Session):
		press(live, "Set output", "5")

	def test_textChangeWhereOnlyTextIsRelevant(self, live: Session):
		press(live, "Change text", "New words")

	def test_nothingWhileBusy(self, live: Session):
		"""What is added while the region is busy is not read; what is added as it stops being busy
		is."""
		mark = press(live, "Busy update", "Done part 1")
		spoken = live.desktop.spokenSince(mark)
		assert "Busy part 1" not in spoken, spoken

	def test_logKeepsUpAndStopsWithControl(self, live: Session):
		"""A log that gets a line every half second is read line by line; Control silences the line
		being read, and the next line is read all the same."""
		desktop = live.desktop
		mark = press(live, "Start log", "Log line 1", "Log line 2", "Log line 3")
		desktop.press("Control_L")
		stopped = len(desktop.spokenSince(mark))
		desktop.expect("Log line …", since=mark + stopped, timeout=2.0)
		press(live, "Stop log")
		time.sleep(1.0)
		last = desktop.mark()
		time.sleep(1.5)
		assert not any(text.startswith("Log line") for text in desktop.spokenSince(last))

	def test_nothingFromABackgroundTab(self, live: Session):
		desktop = live.desktop
		press(live, "Start log", "Log line …")
		desktop.press("ctrl+t")
		time.sleep(2.0)
		mark = desktop.mark()
		time.sleep(2.0)
		spoken = desktop.spokenSince(mark)
		desktop.press("ctrl+w")
		time.sleep(2.0)
		press(live, "Stop log")
		assert not any(text.startswith("Log line") for text in spoken), spoken


@pytest.fixture(scope="class")
def tables(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Session]:
	yield from _openPage(
		tmp_path_factory, "featureTables.html", "heading level 1 Tables", workspaceKeysOff=True
	)


def requireTableKeys(session: Session) -> None:
	if not session.workspaceKeysOff:
		pytest.xfail("marco was started with its workspace keys, Control+Alt+arrows, before the test")


class TestTables:
	def test_tableSaysItsSize(self, tables: Session):
		stepSaying(tables, "Control_L+Home", "heading level 1 Tables")
		step(tables, "t", "Rooms table with 4 rows and 3 columns caption Rooms")

	def test_moveByCell(self, tables: Session):
		"""Each move says the cell and what changed: the row or column, with its header."""
		requireTableKeys(tables)
		stepSaying(tables, "Control_L+Home", "heading level 1 Tables")
		step(tables, "t", "Rooms table…")
		step(tables, "Down", "out of caption row 1 column 1 Room")
		step(tables, "Control_L+Alt_L+Right", "column 2 Monday")
		step(tables, "Control_L+Alt_L+Right", "column 3 Tuesday")
		step(tables, "Control_L+Alt_L+Down", "Kitchen row 2 Clean")
		step(tables, "Control_L+Alt_L+Down", "Hall row 3 Monday column 2 through 3 Paint all week")
		step(tables, "Control_L+Alt_L+Right", "Edge of table")
		step(tables, "Control_L+Alt_L+Left", "Room column 1 Hall")

	def test_tableInATable(self, tables: Session):
		requireTableKeys(tables)
		stepSaying(tables, "Control_L+Home", "heading level 1 Tables")
		step(tables, "t", "Rooms table…")
		step(tables, "Down", "out of caption row 1 column 1 Room")
		for _ in range(3):
			step(tables, "Control_L+Alt_L+Down", "…")
		step(tables, "Control_L+Alt_L+Right", "Monday column 2 Dig")
		step(tables, "Down", "Tuesday column 3 table with 2 rows and 2 columns caption Tools")
		step(tables, "Down", "out of caption row 1 column 1 Tool")
		step(tables, "Control_L+Alt_L+Down", "row 2 Spade")
		step(tables, "Control_L+Alt_L+Right", "Count column 2 2")

	def test_layoutTableIsNotATable(self, tables: Session):
		"""t passes over a table used for layout, and its cells are read as text."""
		requireTableKeys(tables)
		stepSaying(tables, "Control_L+Home", "heading level 1 Tables")
		step(tables, "t", "Rooms table…")
		stepSaying(tables, "t", "Tools table")
		stepSaying(tables, "t", "Scores table with 2 rows and 2 columns")
		stepSaying(tables, "Shift+t", "Tools table")
		stepSaying(tables, "Control_L+End", "After the tables.")
		for _ in range(6):
			mark = step(tables, "Up", "…")
			if "Layout below" in tables.desktop.spokenSince(mark):
				break
		else:
			raise AssertionError("Up never reached the layout table")
		step(tables, "Control_L+Alt_L+Right", "Not in a table cell")

	def test_ariaGrid(self, tables: Session):
		requireTableKeys(tables)
		stepSaying(tables, "Control_L+Home", "heading level 1 Tables")
		step(tables, "t", "Rooms table…")
		stepSaying(tables, "t", "Tools table")
		stepSaying(tables, "t", "Scores table with 2 rows and 2 columns")
		stepSaying(tables, "Down", "Player")
		stepSaying(tables, "Control_L+Alt_L+Down", "Ann")
		step(tables, "Control_L+Alt_L+Right", "Points column 2 7")
