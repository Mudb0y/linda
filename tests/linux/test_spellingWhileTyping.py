"""NVDA's check for a spelling error behind the caret as a word is typed, in the editable objects of a
page that are not plain fields: a text area with the combobox role, as search boxes are made, and an
editable element without a role. Both are editable and have the caret, as IAccessible2's text gives
them on Windows; NVDA reads them with the object's text and plays its error sound after a misspelt
word.

LINDA_RIG_WM=marco nix develop . -c tests/linux/rig python3 -m pytest -q tests/linux/test_spellingWhileTyping.py
"""

import time
from pathlib import Path

import desktopSession
import harness
import pytest
import webSession
from desktopSession import DesktopSession
from test_deletedCharacter import findField

PAGE = "spellingFields.html"
PAGE_TITLE = "Spelling fields"
STARTUP_BOUND = 60.0
#: Seconds for Firefox to mark a word and NVDA to look at it, which it does 50 ms after the space.
SPELLING_BOUND = 5.0

WAVE_PLUGIN = """
import nvwave
import globalPluginHandler
from logHandler import log


class GlobalPlugin(globalPluginHandler.GlobalPlugin):
	def __init__(self):
		super().__init__()
		nvwave.decide_playWaveFile.register(self._logWave)

	def terminate(self):
		nvwave.decide_playWaveFile.unregister(self._logWave)

	def _logWave(self, fileName, **kwargs):
		log.info(f"wave probe: {fileName}")
		return True
"""


@pytest.mark.parametrize(("name", "spoken"), [("search", "search combo box"), ("note", "note section")])
def test_misspeltWordIsSignalled(tmp_path: Path, name: str, spoken: str):
	webSession.requireBrowseLibraries()
	hooks = tmp_path / "hooks"
	(hooks / "globalPlugins").mkdir(parents=True)
	(hooks / "synthDrivers").symlink_to(harness.HERE / "synthDrivers")
	(hooks / "globalPlugins" / "waveProbe.py").write_text(WAVE_PLUGIN)
	with DesktopSession(tmp_path / "desktop") as desktop:
		nvda = desktop.startNVDA(webSession.BROWSE_CONFIG, env={"LINDA_TEST_HOOKS_DIR": str(hooks)})
		nvda.waitForLog("NVDA initialized", STARTUP_BOUND)
		webSession.startFirefox(desktop, PAGE)
		desktop.expect("Fields to misspell in.", since=0, timeout=STARTUP_BOUND)
		mark = desktop.mark()
		assert desktopSession._atspi().Component.grab_focus(findField(PAGE_TITLE, name))
		desktop.expect(f"{spoken}…", since=mark)
		time.sleep(0.5)
		desktop.press("End")
		# NVDA looks 50 ms after the space, and Firefox's first check of a field, which loads its
		# dictionary, can mark the word later than that on a loaded machine; the next is marked in time.
		for word in ("wrnog", "mispelt", "speling"):
			desktop.typeText(f" {word} ")
			try:
				nvda.waitForLog("textError.wav", SPELLING_BOUND)
				break
			except AssertionError:
				continue
		assert "textError.wav" in nvda.text("nvda.log"), nvda.describe()
		# The caret is the object's own: the character left of it is read.
		mark = desktop.mark()
		desktop.press("Left")
		desktop.expect("space", since=mark)
		desktop.close()
		assert nvda.process.returncode == 0, nvda.describe()
		assert "Error fetching last character" not in nvda.text("nvda.log")
