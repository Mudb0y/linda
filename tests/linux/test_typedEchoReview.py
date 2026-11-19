"""A fault found in review of the echo of typed characters cutting the echo before it: the test fails
while the fault is there. Run under the rig:

	nix develop . -c tests/linux/rig python3 -m pytest tests/linux/test_typedEchoReview.py
"""

import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import harness
import pytest
from desktopSession import DesktopSession
from test_keys import Keyboard, KeySession, transcript, waitForEntry

#: Keeps NVDA's main thread busy for a moment, as speaking, a focus change or a slow application does,
#: so that the keys typed meanwhile are all down before NVDA gets to the inserts they made.
BUSY_PLUGIN = """
import time

import globalPluginHandler
from logHandler import log
from scriptHandler import script


class GlobalPlugin(globalPluginHandler.GlobalPlugin):
	@script(gesture="kb:NVDA+control+shift+f11")
	def script_busy(self, gesture):
		log.info("echo review: busy")
		time.sleep(0.4)
"""
#: Milliseconds the transcript synth takes over each character, so that the title takes a while.
MS_PER_CHAR = "40"
TITLE = "Fixture window"


@pytest.fixture
def desktop(tmp_path: Path) -> Iterator[DesktopSession]:
	with DesktopSession(tmp_path / "desktop") as session:
		yield session


def _hooks(directory: Path) -> dict[str, str]:
	hooks = directory / "hooks"
	(hooks / "globalPlugins").mkdir(parents=True)
	(hooks / "synthDrivers").symlink_to(harness.HERE / "synthDrivers")
	(hooks / "globalPlugins" / "echoReview.py").write_text(BUSY_PLUGIN)
	return {
		"LINDA_TEST_HOOKS_DIR": str(hooks),
		"LINDA_TRANSCRIPT_MODE": "timed",
		"LINDA_TRANSCRIPT_MS_PER_CHAR": MS_PER_CHAR,
	}


def _text(record: dict) -> str:
	return " ".join(item.strip() for item in record["data"] if isinstance(item, str) and item.strip())


def test_lateEchoesDoNotCutACommandsSpeech(desktop: DesktopSession, tmp_path: Path):
	"""q and w typed, then NVDA+T, all before NVDA has got to the inserts q and w made: NVDA speaks the
	title, then the two echoes come. On Windows the echoes come with their keys and NVDA+T's interrupt
	cuts them; the title is heard whole. The second echo must not cut the title."""
	keys = KeySession(desktop, tmp_path, "desktop", env=_hooks(tmp_path))
	board = Keyboard()
	try:
		board.press("End")
		time.sleep(0.8)
		before = len(transcript(desktop.nvda))
		subprocess.run(["xdotool", "key", "Insert+control+shift+F11"], check=True)
		deadline = time.monotonic() + 5
		while "echo review: busy" not in desktop.nvda.text("nvda.log"):
			assert time.monotonic() < deadline, "NVDA did not run the busy command"
			time.sleep(0.005)
		for key in ("q", "w"):
			board._send(key, True)
			board._send(key, False)
		board._send("Insert", True)
		board._send("t", True)
		board._send("t", False)
		board._send("Insert", False)
		waitForEntry("Aliceqw")
		deadline = time.monotonic() + 5
		while not any(r["kind"] == "speak" and _text(r) == "w" for r in transcript(desktop.nvda)[before:]):
			assert time.monotonic() < deadline, f"w was never echoed: {transcript(desktop.nvda)[before:]}"
			time.sleep(0.05)
		time.sleep(1.5)
		records = transcript(desktop.nvda)[before:]
	finally:
		board.close()
		launch = keys.quit()
	summary = [(r["kind"], _text(r) if r["kind"] == "speak" else None, round(r["t"], 3)) for r in records]
	titles = [i for i, r in enumerate(records) if r["kind"] == "speak" and _text(r) == TITLE]
	assert titles, f"NVDA+T did not speak the title: {summary}"
	title = titles[-1]
	echoes = [i for i, r in enumerate(records) if r["kind"] == "speak" and _text(r) in ("q", "w")]
	assert echoes and echoes[-1] > title, (
		f"this run did not reach the case: the echoes were spoken before the title: {summary}"
	)
	cuts = [r for r in records[title + 1 :] if r["kind"] == "cancel"]
	assert not cuts, (
		"the echo of w, which came after NVDA+T's title because NVDA got to the inserts late, cancelled "
		f"the title {round((cuts[0]['t'] - records[title]['t']) * 1000)} ms into it, though nothing was "
		f"typed after NVDA+T: {summary}"
	)
	launch.assertClean(logNames=("nvda.log",))
