"""Swapping line (Up/Down) and paragraph (Ctrl+Up/Down) navigation in browse mode and Document Navigation settings.

LINDA_RIG_WM=marco nix develop . -c tests/linux/rig python3 -m pytest -q tests/linux/test_swapParagraphNavigation.py
"""

import time
from collections.abc import Iterator
from pathlib import Path

import desktopSession
import harness
import pytest
import webSession
from desktopSession import DesktopSession

SESSION_CONFIG = harness.HERE / "fixtures" / "sessionConfig" / "nvda.ini"
STARTUP_BOUND = 60.0
LOAD_BOUND = 10.0
STEP_BOUND = 5.0
SETTLE = 0.5
GTK = ("gtkFixture", "Fixture window")
AT_START = ("Fixture window", "Press me button Does nothing Alt plus P")

PARA1_FULL = (
	"Alpha one alpha two alpha three alpha four alpha five alpha six alpha seven "
	"alpha eight alpha nine alpha ten alpha eleven alpha twelve."
)
PARA2_FULL = (
	"Bravo one bravo two bravo three bravo four bravo five bravo six bravo seven "
	"bravo eight bravo nine bravo ten bravo eleven bravo twelve."
)


@pytest.fixture
def desktop(tmp_path: Path) -> Iterator[DesktopSession]:
	with DesktopSession(tmp_path / "desktop") as session:
		yield session


def _step(desktop: DesktopSession, chord: str, *expected: str, timeout: float = STEP_BOUND) -> int:
	mark = desktop.mark()
	desktop.press(chord)
	desktop.expect(*expected, since=mark, timeout=timeout)
	time.sleep(SETTLE)
	return mark


def _stepText(desktop: DesktopSession, chord: str, timeout: float = STEP_BOUND) -> str:
	mark = desktop.mark()
	desktop.press(chord)
	deadline = time.monotonic() + timeout
	while not desktop.spokenSince(mark):
		assert time.monotonic() < deadline, f"{chord} spoke nothing"
		time.sleep(0.05)
	time.sleep(SETTLE)
	return " ".join(desktop.spokenSince(mark))


def test_swapSettingIsExposedAndSavedInDocumentNavigation(desktop: DesktopSession, tmp_path: Path):
	"""Document Navigation settings exposes 'Swap line and paragraph navigation' (off by default)
	and saves it to nvda.ini."""
	configDir = tmp_path / "config"
	configDir.mkdir()
	(configDir / "nvda.ini").write_text(SESSION_CONFIG.read_text(encoding="utf-8"), encoding="utf-8")

	app = desktop.startApp(desktop.fixture(GTK[0]), title=GTK[1])
	assert app.stdout.readline() == "ready\n"
	desktopSession.activate(GTK[1])
	nvda = desktop.startNVDA(configDir)
	desktop.expect(*AT_START, since=0, timeout=STARTUP_BOUND)

	_step(desktop, "Insert+n", "Preferences sub Menu P 1 of 9")
	_step(desktop, "Return", "Settings... S 1 of 4")
	_step(
		desktop,
		"Return",
		"Linda Settings: General (normal configuration) dialog",
		"Categories: list",
		"General 1 of 18",
	)
	for index, category in enumerate(
		(
			"Speech",
			"Braille",
			"Audio",
			"Sound Themes",
			"Privacy and Security",
			"Vision",
			"Keyboard",
			"Mouse",
			"Review Cursor",
			"Object Presentation",
			"Browse Mode",
			"Document Formatting",
			"Document Navigation",
		),
		start=2,
	):
		_step(desktop, "Down", f"{category} {index} of 18")

	_step(desktop, "Tab", "Paragraph style: combo box…")
	_step(desktop, "Tab", "Swap line and paragraph navigation check box not checked Alt plus S")
	_step(desktop, "space", "checked")
	_step(desktop, "Return", *AT_START)

	_step(desktop, "Insert+control+c", "Configuration saved")
	saved = (nvda.directory / "config" / "nvda.ini").read_text(encoding="utf-8")
	assert "[documentNavigation]" in saved and "swapLineAndParagraphNavigation = True" in saved, saved

	desktop.close()
	assert nvda.process.returncode == 0, nvda.describe()
	nvda.assertClean(logNames=("nvda.log",))


def test_browseModeSwapsLineAndParagraphNavigationAndSelection(tmp_path: Path):
	"""With swap off, Up/Down move by line and Ctrl+Up/Down move by paragraph. With swap on,
	Up/Down move by paragraph and Ctrl+Up/Down move by line (and Shift selection swaps to match)."""
	webSession.requireBrowseLibraries()
	for swapped in (False, True):
		runDir = tmp_path / ("swapped" if swapped else "default")
		configDir = runDir / "config"
		configDir.mkdir(parents=True)
		ini = webSession.BROWSE_CONFIG.read_text(encoding="utf-8")
		if swapped:
			ini += "\n[documentNavigation]\n\tswapLineAndParagraphNavigation = True\n"
		(configDir / "nvda.ini").write_text(ini, encoding="utf-8")

		with DesktopSession(runDir / "desktop") as desktop:
			nvda = desktop.startNVDA(configDir)
			nvda.waitForLog("NVDA initialized", STARTUP_BOUND)
			mark = desktop.mark()
			webSession.startFirefox(desktop, "paragraphs.html")
			desktop.expect("Paragraph navigation", since=mark, timeout=LOAD_BOUND)
			desktop.press("Control_L")
			time.sleep(SETTLE)

			# Start at top of document (heading "Paragraph navigation").
			_step(desktop, "control+Home", "heading level 1 Paragraph navigation")

			if not swapped:
				# Plain Down moves by wrapped line (< full paragraph).
				line1 = _stepText(desktop, "Down")
				assert "Alpha one" in line1 and "alpha twelve" not in line1, line1
				line2 = _stepText(desktop, "Down")
				assert "alpha twelve" in line2 and "Alpha one" not in line2, line2

				# Ctrl+Down moves to paragraph 2 as a whole paragraph.
				para2 = _stepText(desktop, "control+Down")
				assert "Bravo one" in para2 and "bravo twelve" in para2, para2

				# Ctrl+Up moves back to paragraph 1 as a whole paragraph.
				para1 = _stepText(desktop, "control+Up")
				assert "Alpha one" in para1 and "alpha twelve" in para1, para1

				# Shift+Down selects one line; Shift+Ctrl+Down selects the whole paragraph.
				selLine = _stepText(desktop, "shift+Down")
				assert "Alpha one" in selLine and "alpha twelve" not in selLine, selLine
				_step(desktop, "control+Home", "heading level 1 Paragraph navigation")
				_step(desktop, "Down", "Alpha one…")
				selPara = _stepText(desktop, "shift+control+Down")
				assert "Alpha one" in selPara and "alpha twelve" in selPara, selPara
			else:
				# Plain Down moves by whole paragraph!
				para1 = _stepText(desktop, "Down")
				assert "Alpha one" in para1 and "alpha twelve" in para1, para1
				para2 = _stepText(desktop, "Down")
				assert "Bravo one" in para2 and "bravo twelve" in para2, para2
				para3 = _stepText(desktop, "Down")
				assert "Charlie final paragraph" in para3, para3

				# Plain Up moves back by whole paragraph!
				back2 = _stepText(desktop, "Up")
				assert "Bravo one" in back2 and "bravo twelve" in back2, back2
				back1 = _stepText(desktop, "Up")
				assert "Alpha one" in back1 and "alpha twelve" in back1, back1

				# Ctrl+Down moves by wrapped line (from line 1 of para1 to line 2 of para1).
				line2 = _stepText(desktop, "control+Down")
				assert "alpha twelve" in line2 and "Alpha one" not in line2, line2

				# Ctrl+Up moves back to line 1 of para1.
				line1 = _stepText(desktop, "control+Up")
				assert "Alpha one" in line1 and "alpha twelve" not in line1, line1

				# Shift+Down selects the whole paragraph; Shift+Ctrl+Down selects one line.
				selPara = _stepText(desktop, "shift+Down")
				assert "Alpha one" in selPara and "alpha twelve" in selPara, selPara
				_step(desktop, "control+Home", "heading level 1 Paragraph navigation")
				_step(desktop, "Down", "Alpha one…")
				selLine = _stepText(desktop, "shift+control+Down")
				assert "Alpha one" in selLine and "alpha twelve" not in selLine, selLine
			desktop.close()
			assert nvda.process.returncode == 0, nvda.describe()
			nvda.assertClean(logNames=("nvda.log",))
