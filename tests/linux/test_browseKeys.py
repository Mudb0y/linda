"""Browse mode's keys on a web page: NVDA takes them alone in browse mode, and gives them to the page in
focus mode, through synchronous grabs on the rig's display, which a slow or stopped NVDA must never
hold for long.

BASEDREADER_VBUF_LIB=ROOT/engine/target/vbuf/libnvdaVbuf.so \
BASEDREADER_VBUF_ENGINE=ROOT/engine/target/release/libbre_vbuf.so \
BASEDREADER_RIG_WM=marco nix develop . -c tests/linux/rig python3 -m pytest -q tests/linux/test_browseKeys.py
"""

import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import desktopSession
import harness
import pytest
import webSession
from desktopSession import PREFIX_MARK, DesktopSession

#: Seconds from NVDA's start to the page's buffer being built.
BUFFER_BOUND = 60.0
#: Seconds for anything a key starts to settle before the next key.
SETTLE = 0.5
#: Seconds the test hook blocks NVDA's main thread for.
BLOCK_SECONDS = 5
#: Seconds the X server held a captured key for before it was replayed without NVDA's answer, as the
#: key source has it, and how much later a key may arrive than that.
CAPTURE_TIMEOUT = 0.1
ARRIVAL_SLACK = 0.15

#: A global plugin for these tests only: a command that blocks NVDA's main thread, and, in place of
#: Windows' default sound for a key browse mode traps, a log record.
PROBE_PLUGIN = f"""
import time

import browseMode
import globalPluginHandler
from logHandler import log
from scriptHandler import script


def _trap(self, gesture):
	log.info("Trapped a key in browse mode")


browseMode.BrowseModeTreeInterceptor.script_trapNonCommandGesture = _trap


class GlobalPlugin(globalPluginHandler.GlobalPlugin):
	@script(gesture="kb:NVDA+shift+f11")
	def script_blockMainThread(self, gesture):
		log.info("Blocking the main thread")
		time.sleep({BLOCK_SECONDS})
		log.info("The main thread is free again")
"""

#: A GTK entry that prints each change of its text with the time it came, for the X-only test.
ENTRY_APP = """
import time
import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk
window = Gtk.Window(title="Held keys entry")
entry = Gtk.Entry()
window.add(entry)
entry.connect("changed", lambda e: print(f"{time.monotonic()} {e.get_text()}", flush=True))
window.connect("destroy", Gtk.main_quit)
window.show_all()
print("ready", flush=True)
Gtk.main()
"""

#: Run with the key source alone: a decider that takes 300 ms over each captured key, then one that
#: answers at once, dropping x and passing y.
SLOW_DECIDER_CHILD = r"""
import subprocess
import time

from linuxPlatform.keys import x11

decided = []
slow = {"on": True}


def deliver(event):
	if not event.captured:
		return True
	decided.append(event.vkCode)
	if slow["on"]:
		time.sleep(0.3)
		return False
	return chr(event.vkCode) != "X"


source = x11.X11KeySource()
source.start(deliver)
source.setCapture("browse")
time.sleep(0.5)
pressed = []
for key in "hjk":
	pressed.append(time.monotonic())
	subprocess.run(["xdotool", "key", key], check=True)
	time.sleep(0.5)
subprocess.run(["xdotool", "type", "--delay", "20", "abc"], check=True)
time.sleep(1.5)
slow["on"] = False
subprocess.run(["xdotool", "key", "x", "y"], check=True)
time.sleep(0.5)
source.stop()
result = {"pressed": pressed, "decided": [chr(vk) for vk in decided]}
"""


def browseConfig(directory: Path) -> Path:
	"""webSession's browse configuration, without say all as a page loads, so that nothing reads while
	keys are pressed."""
	text = webSession.BROWSE_CONFIG.read_text()
	text = text.replace("[virtualBuffers]\n", "[virtualBuffers]\n\tautoSayAllOnPageLoad = False\n")
	assert "autoSayAllOnPageLoad" in text
	path = directory / "nvda.ini"
	path.write_text(text)
	return path


def probeHooks(directory: Path) -> dict[str, str]:
	"""The environment that gives NVDA the transcript synth and this file's probe plugin."""
	hooks = directory / "hooks"
	(hooks / "globalPlugins").mkdir(parents=True)
	(hooks / "synthDrivers").symlink_to(harness.HERE / "synthDrivers")
	(hooks / "globalPlugins" / "browseKeysProbe.py").write_text(PROBE_PLUGIN)
	return {"BASEDREADER_TEST_HOOKS_DIR": str(hooks)}


def expect(desktop: DesktopSession, *texts: str, since: int, timeout: float = 5) -> None:
	"""desktop.expect, read again when it met a transcript line the synth was still writing."""
	for _ in range(50):
		try:
			return desktop.expect(*texts, since=since, timeout=timeout)
		except json.JSONDecodeError:
			time.sleep(0.01)
	return desktop.expect(*texts, since=since, timeout=timeout)


def step(desktop: DesktopSession, chord: str, *expected: str, timeout: float = 5) -> int:
	"""Press chord, wait for what NVDA says to it, and let it settle; returns the mark before it."""
	mark = desktop.mark()
	desktop.press(chord)
	expect(desktop, *expected, since=mark, timeout=timeout)
	time.sleep(SETTLE)
	return mark


def captureMode(nvda: harness.Launch) -> str | None:
	"""The keys NVDA captures now, as its log last said."""
	found = re.findall(r"Keys captured for (\w+) mode", nvda.text("nvda.log"))
	return found[-1] if found else None


def waitForCapture(nvda: harness.Launch, mode: str, timeout: float = 5) -> None:
	deadline = time.monotonic() + timeout
	while captureMode(nvda) != mode:
		assert time.monotonic() < deadline, f"NVDA did not capture {mode} mode's keys:\n{nvda.describe()}"
		time.sleep(0.05)


def waitForPageLog(expected: list[str], timeout: float = 3) -> float:
	"""Wait until the keys page has logged expected; returns when it had."""
	deadline = time.monotonic() + timeout
	while (logged := webSession.pageKeyLog()) != expected:
		assert time.monotonic() < deadline, f"the page logged {logged}, not {expected}"
		time.sleep(0.02)
	return time.monotonic()


def fieldText() -> str:
	"""What the keys page's text field holds, read over the accessibility bus."""
	Atspi = desktopSession._atspi()
	desktop = Atspi.get_desktop(0)
	desktop.clear_cache()
	rule = Atspi.MatchRule.new(
		None,
		Atspi.CollectionMatchType.ALL,
		None,
		Atspi.CollectionMatchType.ALL,
		[Atspi.Role.ENTRY],
		Atspi.CollectionMatchType.ANY,
		None,
		Atspi.CollectionMatchType.ALL,
		False,
	)
	for index in range(desktop.get_child_count()):
		app = desktop.get_child_at_index(index)
		if app is None or app.get_toolkit_name() != "Gecko":
			continue
		for entry in Atspi.Collection.get_matches(app, rule, Atspi.CollectionSortOrder.CANONICAL, 0, True):
			entry.clear_cache()
			if entry.get_name() == "Field":
				return Atspi.Text.get_text(entry, 0, -1)
	raise AssertionError("the keys page has no text field named Field")


def waitForField(text: str, timeout: float = 3) -> float:
	deadline = time.monotonic() + timeout
	while (found := fieldText()) != text:
		assert time.monotonic() < deadline, f"the field holds {found!r}, not {text!r}"
		time.sleep(0.02)
	return time.monotonic()


def startPage(desktop: DesktopSession, tmp_path: Path) -> harness.Launch:
	"""Firefox on the keys page, then NVDA, with its buffer built and browse mode's keys captured."""
	webSession.requireBrowseLibraries()
	webSession.startFirefox(desktop, "keys.html")
	nvda = desktop.startNVDA(browseConfig(tmp_path), env=probeHooks(tmp_path))
	nvda.waitForLog("Buffer rendered", BUFFER_BOUND)
	waitForCapture(nvda, "browse", timeout=BUFFER_BOUND)
	time.sleep(SETTLE)
	assert webSession.pageKeyLog() == []
	return nvda


def quitNVDA(desktop: DesktopSession, nvda: harness.Launch) -> None:
	step(desktop, "Insert+q", "Exit NVDA" + PREFIX_MARK)
	desktop.press("Return")
	nvda.waitForExit(10)
	nvda.assertClean(logNames=("nvda.log",))


@pytest.fixture
def desktop(tmp_path: Path) -> Iterator[DesktopSession]:
	with DesktopSession(tmp_path / "desktop") as session:
		yield session


def test_heldKeysAreReplayedWithoutAnAnswer(desktop: DesktopSession, tmp_path: Path):
	app = tmp_path / "entry.py"
	app.write_text(ENTRY_APP)
	entry = desktop.startApp([sys.executable, str(app)], title="Held keys entry")
	assert entry.stdout.readline().strip() == "ready"
	desktopSession.activate("Held keys entry")
	result = harness.runChild(tmp_path, SLOW_DECIDER_CHILD, prepare="config", timeout=60)
	result.assertClean()
	os.set_blocking(entry.stdout.fileno(), False)
	changes = [line.split(" ", 1) for line in (entry.stdout.read() or "").splitlines()]
	texts = [text for _when, text in changes]
	assert texts == ["h", "hj", "hjk", "hjka", "hjkab", "hjkabc", "hjkabcy"], texts
	for (when, _text), pressed in zip(changes, result.value["pressed"]):
		late = float(when) - pressed
		assert CAPTURE_TIMEOUT <= late <= CAPTURE_TIMEOUT + ARRIVAL_SLACK, (
			f"replayed {late:.3f} s after the press"
		)
	# b and c reached the entry while the decider was still over a, so it never asked about them.
	assert result.value["decided"] == list("HJKAXY")


def test_browseAndFocusModeKeys(desktop: DesktopSession, tmp_path: Path):
	nvda = startPage(desktop, tmp_path)

	step(desktop, "Tab", "Focus mode")
	waitForCapture(nvda, "focus")
	desktop.typeText("ab")
	waitForPageLog(["a", "b"])
	waitForField("ab")
	step(desktop, "Escape", "Browse mode")
	waitForCapture(nvda, "browse")

	step(desktop, "control+Home", "heading level 1 Keys page")
	step(desktop, "Down", "Every key that reaches this page is written to the log at its end.")
	step(desktop, "h", "Field and link heading level 2")
	# Space activates the heading, which does nothing to be heard; that NVDA had it, the page not, is
	# what counts.
	typed = nvda.text("nvda.log").count("Input: kb(desktop):(typing key)")
	desktop.press("space", gap=SETTLE)
	assert nvda.text("nvda.log").count("Input: kb(desktop):(typing key)") == typed + 1
	step(desktop, "1", "No next heading at level 1")
	assert webSession.pageKeyLog() == ["a", "b"]
	assert fieldText() == "ab"

	mark = desktop.mark()
	subprocess.run(["xdotool", "keydown", "Down"], check=True)
	time.sleep(1)
	subprocess.run(["xdotool", "keyup", "Down"], check=True)
	time.sleep(SETTLE)
	assert len(desktop.spokenSince(mark)) >= 3, desktop.spokenSince(mark)
	assert webSession.pageKeyLog() == ["a", "b"]

	step(desktop, "Insert+shift+space", "Single letter navigation off")
	desktop.press("h")
	waitForPageLog(["a", "b", "h"])
	step(desktop, "Insert+shift+space", "Single letter navigation on")
	time.sleep(SETTLE)
	assert webSession.pageKeyLog() == ["a", "b", "h"]

	step(desktop, "control+l", "combo box Search with Google or enter address" + PREFIX_MARK)
	waitForCapture(nvda, "off")
	# Firefox shows a page the shortcuts it does not reserve before acting on them, as it does with no
	# screen reader running; once is right.
	assert webSession.pageKeyLog() == ["a", "b", "h", "Control+l"]
	quitNVDA(desktop, nvda)
