"""What NVDA does on web pages beyond reading them: live regions and alerts spoken as they change,
tables read cell by cell, the elements list, and the language a page marks.

BASEDREADER_RIG_WM=marco nix develop . -c tests/linux/rig python3 -m pytest -q tests/linux/test_browseFeatures.py

Each page gets its own NVDA and Firefox, NVDA first, as on a desktop where the screen reader is
already running. Within a page the tests are steps in order, each starting where the one before
left the page.
"""

import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

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


@dataclass
class Session:
	desktop: DesktopSession
	nvda: harness.Launch
	firefox: webSession.FirefoxProcess


def _openPage(tmp_path_factory: pytest.TempPathFactory, page: str, firstLine: str) -> Iterator[Session]:
	webSession.requireBrowseLibraries()
	directory = tmp_path_factory.mktemp(Path(page).stem)
	with DesktopSession(directory / "desktop") as desktop:
		nvda = desktop.startNVDA(webSession.BROWSE_CONFIG)
		nvda.waitForLog("NVDA initialized", STARTUP_BOUND)
		mark = desktop.mark()
		firefox = webSession.startFirefox(desktop, page)
		desktop.expect(firstLine, since=mark, timeout=LOAD_BOUND)
		# Say all on load is cut short, so that what a step says is not queued behind it.
		desktop.press("Control_L")
		time.sleep(SETTLE)
		yield Session(desktop, nvda, firefox)


def step(session: Session, chord: str, *expected: str, timeout: float = STEP_BOUND) -> int:
	"""Press chord and wait for expected, in order; return the mark taken before the key."""
	desktop = session.desktop
	mark = desktop.mark()
	desktop.press(chord)
	desktop.expect(*expected, since=mark, timeout=timeout)
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
