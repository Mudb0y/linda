"""A character typed fast is echoed though the next key, one that types nothing, went down before NVDA
got to the application's insert of it. Run under the rig:

	nix develop . -c tests/linux/rig python3 -m pytest tests/linux/test_echoRollover.py
"""

import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import harness
import pytest
from desktopSession import DesktopSession
from test_keys import Keyboard, KeySession, entryState, waitForEntry

#: A command that keeps NVDA's main thread busy for a moment, as speaking the last character does,
#: shorter than the time within which an insert counts as typed.
BUSY_PLUGIN = """
import time

import globalPluginHandler
from logHandler import log
from scriptHandler import script


class GlobalPlugin(globalPluginHandler.GlobalPlugin):
	@script(gesture="kb:NVDA+shift+f12")
	def script_busy(self, gesture):
		log.info("echo probe: busy")
		time.sleep(0.25)
"""


@pytest.fixture
def desktop(tmp_path: Path) -> Iterator[DesktopSession]:
	with DesktopSession(tmp_path / "desktop") as session:
		yield session


def busyHooks(directory: Path) -> dict[str, str]:
	hooks = directory / "hooks"
	(hooks / "globalPlugins").mkdir(parents=True)
	(hooks / "synthDrivers").symlink_to(harness.HERE / "synthDrivers")
	(hooks / "globalPlugins" / "echoProbe.py").write_text(BUSY_PLUGIN)
	return {"LINDA_TEST_HOOKS_DIR": str(hooks)}


def test_characterIsEchoedThoughAKeyThatTypesNothingFollowsIt(desktop: DesktopSession, tmp_path: Path):
	keys = KeySession(desktop, tmp_path, "desktop", env=busyHooks(tmp_path))
	board = Keyboard()
	try:
		board.press("End")
		time.sleep(0.5)
		typed = "Alice"
		for count, (letter, after) in enumerate((("q", "Shift_L"), ("w", "Right"), ("e", "Shift_L")), 1):
			subprocess.run(["xdotool", "key", "Insert+shift+F12"], check=True)
			deadline = time.monotonic() + 5
			while desktop.nvda.text("nvda.log").count("echo probe: busy") < count:
				assert time.monotonic() < deadline, "NVDA did not run the busy command"
				time.sleep(0.01)
			mark = desktop.mark()
			board._send(letter, True)
			board._send(after, True)
			board._send(letter, False)
			board._send(after, False)
			typed += letter
			waitForEntry(typed)
			deadline = time.monotonic() + 3
			while letter not in desktop.spokenSince(mark):
				assert time.monotonic() < deadline, (
					f"{letter} was not echoed with {after} down after it: {desktop.spokenSince(mark)}"
				)
				time.sleep(0.05)
		assert entryState()[0] == "Aliceqwe"
	finally:
		board.close()
		launch = keys.quit()
	launch.assertClean(logNames=("nvda.log",))
