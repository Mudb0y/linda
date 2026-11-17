"""NVDA's dialogs close from the keyboard, and NVDA exits, restarts or is replaced by a new copy while
one of them is open, on the rig's display under marco.

nix develop . -c tests/linux/rig python3 -m pytest -q tests/linux/test_modalDialogs.py

The session configuration points the add-on store at a port nothing listens on, which is how the
store behaves without a network.
"""

import signal
import time
from collections.abc import Iterator
from pathlib import Path

import desktopSession
import harness
import pytest
from desktopSession import DesktopSession

SESSION_CONFIG = harness.HERE / "fixtures" / "sessionConfig" / "nvda.ini"
#: Seconds from starting NVDA to its first words; a start under the rig takes about three.
STARTUP_BOUND = 60.0
#: Seconds from starting a second copy to its first words, with the first copy in its way.
TAKEOVER_BOUND = 15.0
#: Seconds NVDA has to exit once asked.
EXIT_BOUND = 10.0
#: Seconds to leave for something that may yet be said.
SETTLE = 1.0

GTK = ("gtkFixture", "Fixture window")
AT_START = ("Fixture window", "Press me button Does nothing Alt plus P")
WARNING = "Add-on Store Warning dialog Add-ons are created by the NVDA community…"
WARNING_CHECK_BOX = "Don't show this message again check box not checked Alt plus D"
FETCH_FAILURE = "Add-on data update failure dialog Unable to fetch latest add-on data…"
FETCH_FAILURE_TITLE = "Add-on data update failure"
STORE = "Add-on Store - …"
WELCOME = "Welcome to Linda dialog Welcome to Linda!…"
DESKTOP_LAYOUT = "Keyboard layout: combo box desktop collapsed Alt plus K"
EXIT_DIALOG = ("Exit Linda dialog", "What would you like to do? combo box Exit collapsed Alt plus D")
REFUSED = "Action unavailable while a dialog requires a response"


@pytest.fixture
def desktop(tmp_path: Path) -> Iterator[DesktopSession]:
	with DesktopSession(tmp_path / "desktop") as session:
		yield session
	# A copy NVDA started to restart itself is no child of the test.
	harness.stopRunningCopy()


def start(desktop: DesktopSession) -> harness.Launch:
	app = desktop.startApp(desktop.fixture(GTK[0]), title=GTK[1])
	assert app.stdout.readline() == "ready\n"
	desktopSession.activate(GTK[1])
	nvda = desktop.startNVDA(SESSION_CONFIG)
	desktop.expect(*AT_START, since=0, timeout=STARTUP_BOUND)
	return nvda


def step(desktop: DesktopSession, chord: str, *expected: str, timeout: float = 5) -> int:
	"""Press chord and wait for expected, in order; return the mark taken before the key."""
	mark = desktop.mark()
	desktop.press(chord)
	desktop.expect(*expected, since=mark, timeout=timeout)
	return mark


def openAddonStore(desktop: DesktopSession, *expected: str) -> int:
	step(desktop, "Insert+n", "Preferences sub Menu P 1 of 9")
	step(desktop, "t", "View log L 1 of 6")
	return step(desktop, "a", *expected)


def answerFetchFailure(desktop: DesktopSession, since: int) -> None:
	"""Wait for the store to say it could not fetch its list, which it says in a message box of its own
	over or under the warning, and answer the box with Enter."""
	desktop.expect(FETCH_FAILURE, since=since, timeout=10)
	deadline = time.monotonic() + 10
	while FETCH_FAILURE_TITLE in desktopSession.topLevelNames():
		assert time.monotonic() < deadline, desktopSession.topLevelNames()
		if desktopSession.activeTopLevelNames() == [FETCH_FAILURE_TITLE]:
			desktop.press("Return")
		time.sleep(0.5)


def openWelcomeDialog(desktop: DesktopSession) -> None:
	step(desktop, "Insert+n", "Preferences sub Menu P 1 of 9")
	step(desktop, "h", "User Guide U 1 of 9")
	step(desktop, "Up", "About... A 9 of 9")
	step(desktop, "Up", "Welcome dialog... L 8 of 9")
	step(desktop, "Return", WELCOME, DESKTOP_LAYOUT)


def heardFromTheNewCopy(desktop: DesktopSession, mark: int, text: str) -> None:
	"""Wait for text from the copy a restart started, which speaks into the first copy's transcript;
	expect stops reading it once the first copy has exited."""
	deadline = time.monotonic() + TAKEOVER_BOUND
	while text not in desktop.spokenSince(mark):
		assert time.monotonic() < deadline, f"the new copy did not say {text!r}\n{desktop.nvda.describe()}"
		time.sleep(0.05)


def assertExitedCleanly(nvda: harness.Launch, *logNames: str) -> None:
	assert nvda.process.returncode == 0, nvda.describe()
	nvda.assertClean(logNames=logNames or ("nvda.log",))


def test_addonStoreWarningClosesAndTheStoreIsReadAndCloses(desktop: DesktopSession):
	nvda = start(desktop)
	mark = openAddonStore(desktop, WARNING, WARNING_CHECK_BOX)
	answerFetchFailure(desktop, mark)
	# The warning waits for an answer, as on Windows.
	step(desktop, "Insert+q", REFUSED)
	# Enter on its check box presses OK, as on Windows.
	step(desktop, "Return", STORE)
	step(desktop, "control+Tab", "Installed incompatible add-ons tab selected 4 of 4")
	step(desktop, "Escape", *AT_START)

	mark = openAddonStore(desktop, WARNING, WARNING_CHECK_BOX)
	answerFetchFailure(desktop, mark)
	step(desktop, "space", "checked")
	step(desktop, "alt+o", STORE)
	# The store's tabs share one page on Windows, which crashed NVDA as the store was closed.
	step(desktop, "alt+F4", *AT_START)

	# Not shown again once its check box was ticked.
	mark = openAddonStore(desktop, STORE)
	time.sleep(SETTLE)
	assert not [text for text in desktop.spokenSince(mark) if text.startswith("Add-on Store Warning")]
	desktop.close()
	assertExitedCleanly(nvda)


def test_exitDialogOverAModalDialogRestartsNVDA(desktop: DesktopSession):
	nvda = start(desktop)
	openWelcomeDialog(desktop)
	step(desktop, "Insert+q", *EXIT_DIALOG)
	# The keys reach the Exit dialog, not the welcome dialog under it.
	step(desktop, "Down", "Restart")
	mark = desktop.mark()
	desktop.press("Return")
	assert nvda.waitForExit(EXIT_BOUND) == 0, nvda.describe()
	# The new copy speaks into the same transcript.
	heardFromTheNewCopy(desktop, mark, "Fixture window")
	mark = desktop.mark()
	desktop.press("Insert+t")
	heardFromTheNewCopy(desktop, mark, "Fixture window")
	saved = (nvda.directory / "config" / "nvda.ini").read_text(encoding="utf-8")
	assert "keyboardLayout" not in saved, saved
	harness.stopRunningCopy()
	nvda.assertClean(logNames=("nvda-old.log", "nvda.log"))
	assert "Starting new NVDA instance" in nvda.text("nvda-old.log")


def openAddonStoreWarning(desktop: DesktopSession) -> None:
	mark = openAddonStore(desktop, WARNING, WARNING_CHECK_BOX)
	answerFetchFailure(desktop, mark)


def openUnsavedSettings(desktop: DesktopSession) -> None:
	step(
		desktop, "Insert+control+k", "Linda Settings: Keyboard (normal configuration) dialog", DESKTOP_LAYOUT
	)
	step(desktop, "Down", "laptop")


def openFileDialog(desktop: DesktopSession) -> None:
	step(desktop, "Insert+n", "Preferences sub Menu P 1 of 9")
	step(desktop, "t", "View log L 1 of 6")
	step(desktop, "l", "Linda Log Viewer")
	step(desktop, "control+s", "Save As dialog")


def openExitDialog(desktop: DesktopSession) -> None:
	step(desktop, "Insert+q", *EXIT_DIALOG)


def openNvdaMenu(desktop: DesktopSession) -> None:
	step(desktop, "Insert+n", "Preferences sub Menu P 1 of 9")


OPENERS = {
	"welcome": openWelcomeDialog,
	"addonStoreWarning": openAddonStoreWarning,
	"unsavedSettings": openUnsavedSettings,
	"fileDialog": openFileDialog,
	"exitDialog": openExitDialog,
	"nvdaMenu": openNvdaMenu,
}


@pytest.mark.parametrize("opened", [*OPENERS, "stopped"])
def test_startingAgainTakesOverWhateverTheRunningCopyIsDoing(desktop: DesktopSession, opened: str):
	first = start(desktop)
	if opened == "stopped":
		first.process.send_signal(signal.SIGSTOP)
	else:
		OPENERS[opened](desktop)
	asked = time.monotonic()
	second = desktop.startNVDA(SESSION_CONFIG)
	desktop.expect("Fixture window", since=0, timeout=TAKEOVER_BOUND)
	tookOver = time.monotonic() - asked
	first.waitForExit(EXIT_BOUND)
	# The keys the first copy had taken are free for the second.
	step(desktop, "Insert+t", "Fixture window")
	if opened == "stopped":
		assert first.process.returncode == -signal.SIGKILL, first.describe()
	else:
		assertExitedCleanly(first)
		assert "Exiting on SIGTERM" in first.text("nvda.log")
	if opened == "unsavedSettings":
		# Cancelled, as an unanswered dialog is on Windows at the end of the session.
		saved = (first.directory / "config" / "nvda.ini").read_text(encoding="utf-8")
		assert "keyboardLayout" not in saved, saved
	desktop.close()
	assertExitedCleanly(second)
	print(f"a second copy spoke {tookOver:.1f} s after it was started over {opened}")
