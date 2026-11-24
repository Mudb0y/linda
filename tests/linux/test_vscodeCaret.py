"""The arrows through a line in VSCodium's editor, VS Code built from its free sources: what the caret
moves to is said once, in order, and the last thing said is where it stopped, however fast the keys come
(issue 24).

LINDA_VSCODIUM=/path/to/vscodium/lib/vscode/codium LINDA_RIG_WM=marco nix develop . -c tests/linux/rig python3 -m pytest -q tests/linux/test_vscodeCaret.py

LINDA_VSCODIUM names VSCodium's own Electron program rather than bin/codium, which starts it in a
process group of its own and exits. Its accessibility support is switched on in its settings, as a
person using a screen reader with it has it.
"""

import json
import os
import subprocess
import time
from collections.abc import Iterator

import chromiumSession
import pytest
import webSession
from desktopSession import DesktopSession
from test_browseUsable import EXIT_BOUND, SETTLE, STARTUP_BOUND, STEP_BOUND, TIMED_SPEECH, Session, step

#: The reporter's line, alone in its file.
LINE = "1234567890"
FILE_NAME = "digits.txt"
#: What the caret at the end of the line, past its last character, says.
LINE_END = "blank"
#: Milliseconds between the keys: a held key repeats some 25 times a second, and faster than that.
HELD_KEY_DELAY = 40
FAST_KEY_DELAY = 15
SETTINGS = {
	"editor.accessibilitySupport": "on",
	# The window is named for the file alone, to be found by that name.
	"window.title": "${activeEditorShort}",
	"workbench.startupEditor": "none",
	"workbench.tips.enabled": False,
	"security.workspace.trust.enabled": False,
	"update.mode": "none",
	"telemetry.telemetryLevel": "off",
	"extensions.autoCheckUpdates": False,
}

# Under a compositor the rig holds every key down and up for a while, so it cannot press at a held
# key's rate, let alone faster.
pytestmark = [pytest.mark.needsVariable("LINDA_VSCODIUM"), pytest.mark.rigSession("x11")]


@pytest.fixture(scope="module")
def session(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Session]:
	webSession.requireBrowseLibraries()
	codium = os.environ["LINDA_VSCODIUM"]
	directory = tmp_path_factory.mktemp("vscode")
	userData = directory / "userData"
	(userData / "User").mkdir(parents=True)
	(userData / "User" / "settings.json").write_text(json.dumps(SETTINGS))
	document = directory / FILE_NAME
	document.write_text(LINE)
	with DesktopSession(directory / "desktop") as desktop:
		chromiumSession.clearAccessibilityStatus()
		nvda = desktop.startNVDA(webSession.BROWSE_CONFIG, env=TIMED_SPEECH)
		nvda.waitForLog("NVDA initialized", STARTUP_BOUND)
		editor = chromiumSession._start(
			desktop,
			[
				codium,
				*chromiumSession.ELECTRON_SWITCHES,
				*chromiumSession._platformSwitches(),
				f"--user-data-dir={userData}",
				f"--extensions-dir={directory / 'extensions'}",
				"--disable-workspace-trust",
				# The rig has no login shell to read the user's environment from.
				"--force-disable-user-env",
				"--skip-welcome",
				"--skip-release-notes",
				"--new-window",
				str(document),
			],
			window=FILE_NAME,
			logName="codium.log",
		)
		desktop.expect(f"{FILE_NAME.replace('.', ' dot ')} edit…", since=0, timeout=STARTUP_BOUND)
		time.sleep(SETTLE)
		yield Session(desktop, nvda, editor, directory)


@pytest.fixture(autouse=True)
def recoverAfterAFailure(request: pytest.FixtureRequest, session: Session) -> Iterator[None]:
	failed = request.session.testsfailed
	yield
	if request.session.testsfailed > failed and session.nvda.process.poll() is None:
		session.desktop.press("Escape")
		time.sleep(SETTLE)


def pressRepeatedly(session: Session, key: str, delay: int) -> list[str]:
	"""Press key ten times, delay milliseconds apart, and return what was spoken by the time the
	character the caret stops on was, or STEP_BOUND later, without the empty records the end of each
	character's utterance leaves."""
	mark = session.desktop.mark()
	subprocess.run(["xdotool", "key", "--delay", str(delay), *[key] * len(LINE)], check=True)
	last = LINE_END if key == "Right" else LINE[0]
	deadline = time.monotonic() + STEP_BOUND
	while last not in session.desktop.spokenSince(mark) and time.monotonic() < deadline:
		time.sleep(0.05)
	time.sleep(SETTLE)
	return [text for text in session.desktop.spokenSince(mark) if text.strip()]


@pytest.mark.parametrize("delay", [HELD_KEY_DELAY, FAST_KEY_DELAY], ids=["held", "fast"])
@pytest.mark.parametrize("key", ["Right", "Left"])
def test_eachCharacterIsSaidOnceInOrder(session: Session, key: str, delay: int):
	# A key whose script is still waiting for the editor to move the caret when the next key comes says
	# nothing, as in NVDA on Windows, so that a reader who falls behind the keys catches up with them;
	# what is said is never said twice, and follows the caret along the line to where it stopped.
	line = [*LINE, LINE_END]
	if key == "Left":
		line.reverse()
	step(session, "ctrl+Home" if key == "Right" else "ctrl+End", LINE if key == "Right" else LINE_END)
	spoken = pressRepeatedly(session, key, delay)
	assert spoken and spoken[-1] == line[-1], f"the caret stopped on {line[-1]!r}; NVDA said {spoken}"
	assert set(spoken) <= set(line), spoken
	positions = [line.index(text) for text in spoken]
	assert positions == sorted(set(positions)), f"not each once, in order: {spoken}"


def _waitForSpeech(desktop: DesktopSession, mark: int) -> list[str]:
	deadline = time.monotonic() + STEP_BOUND
	while not desktop.spokenSince(mark) and time.monotonic() < deadline:
		time.sleep(0.05)
	time.sleep(SETTLE)
	return desktop.spokenSince(mark)


def _clickDontSave(desktop: DesktopSession) -> None:
	"""Tab to the dialog's Don't Save button and activate it so focus returns to digits.txt."""
	for _ in range(4):
		if desktop.spokenSince(0)[-1].startswith("Don't Save button"):
			break
		mark = desktop.mark()
		desktop.press("Tab")
		_waitForSpeech(desktop, mark)
	assert desktop.spokenSince(0)[-1].startswith("Don't Save button"), desktop.spokenSince(0)[-5:]
	mark = desktop.mark()
	desktop.press("space")
	desktop.expect(f"{FILE_NAME.replace('.', ' dot ')} edit…", since=mark, timeout=STEP_BOUND)
	time.sleep(SETTLE)


def _openSaveDialog(desktop: DesktopSession) -> None:
	mark = desktop.mark()
	desktop.press("ctrl+w")
	desktop.expect(
		"Warning dialog…",
		"Save button Alt plus S",
		since=mark,
		timeout=STEP_BOUND,
	)
	time.sleep(SETTLE)


def test_saveDialogIsRead(session: Session):
	"""Closing an unsaved buffer opens Electron's native GTK confirmation dialog, which Linda reads
	instead of dropping as a secondary GTK application tree (issue 34)."""
	desktop = session.desktop
	step(session, "ctrl+n", "Untitled-…")
	mark = desktop.mark()
	desktop.typeText("hello")
	desktop.expect("o", since=mark)
	time.sleep(SETTLE)
	mark = desktop.mark()
	desktop.press("ctrl+w")
	desktop.expect(
		"Warning dialog Do you want to save the changes you made to hello?…",
		"Save button Alt plus S",
		since=mark,
		timeout=STEP_BOUND,
	)
	time.sleep(SETTLE)
	tabbed: list[str] = []
	for _ in range(3):
		mark = desktop.mark()
		desktop.press("Tab")
		tabbed.extend(_waitForSpeech(desktop, mark))
	assert any("Cancel button" in text for text in tabbed), tabbed
	assert any("Don't Save button" in text for text in tabbed), tabbed
	_clickDontSave(desktop)


def test_wordsWithSeparators(session: Session):
	"""Ctrl+Right and Ctrl+Left across path separators in VS Code speak each segment's word rather
	than the punctuation alone (issue 8)."""
	desktop = session.desktop
	step(session, "ctrl+n", "Untitled-…")
	mark = desktop.mark()
	desktop.typeText("/usr/bin/bash")
	desktop.expect("h", since=mark)
	time.sleep(SETTLE)
	desktop.press("Home")
	time.sleep(SETTLE)
	forward = []
	for _ in range(4):
		mark = desktop.mark()
		desktop.press("ctrl+Right")
		forward.append(_waitForSpeech(desktop, mark)[-1])
	assert any("usr" in text for text in forward) and any("bin" in text for text in forward), forward
	assert not any(text == "slash" for text in forward), forward
	_openSaveDialog(desktop)
	_clickDontSave(desktop)


def test_exitIsClean(session: Session):
	step(session, "Insert+q", "Exit Linda…")
	session.desktop.press("Return")
	nvda = session.nvda
	nvda.waitForExit(EXIT_BOUND)
	assert nvda.process.returncode == 0, nvda.describe()
	nvda.assertClean(logNames=("nvda.log",))
