"""NVDA starts and quits clean in a Wayland session under the rig's compositors: its log holds no record
at ERROR or above, nothing raises NoLinuxImplementation, even where it is caught, and standard error
holds nothing outside the harness's allowed lines. Once with no linda-keyd, as a person starts it
before installing the helper, and once with a stand-in for it holding the keyboard, so that the
kernel route, the layout feed and the capture feed start too. A native GTK 3 window has the keyboard
as NVDA starts, so that the first focus, the mouse's object and the review cursor are found there.

tests/linux/wayland-gate runs this file under Mutter and sway, each with and without Xwayland:

	nix develop . -c tests/linux/wayland-gate SCRATCH -k StartClean
"""

import shutil
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

import desktopSession
import harness
import keydStandIn
import pytest
from desktopSession import DesktopSession
from keydStandIn import StandIn

pytestmark = pytest.mark.rigSession("wayland")

SESSION_CONFIG = harness.HERE / "fixtures" / "sessionConfig" / "nvda.ini"
#: Seconds from starting NVDA to its first words; a start under the rig takes about three.
STARTUP_BOUND = 60.0
#: Seconds NVDA runs on after its first words before it is quit: past the capture feed's keeper's
#: first check, and the first of NVDA's own looks for a focus after it starts.
RUN_ON = 4.0
#: Seconds for the stand-in to be asked to capture once NVDA has started.
CAPTURE_BOUND = 10.0
GTK = ("gtkFixture", "Fixture window")
AT_START = ("Fixture window", "Press me button Does nothing Alt plus P")
NO_HELPER = (
	"Linda cannot take keys in this Wayland session until linda-keyd runs. It reads the screen "
	"meanwhile. The readme says how to install linda-keyd."
)
HINT = "If typing stops reaching programs, hold Backspace, Escape and Enter together."


@pytest.fixture
def desktop(tmp_path: Path) -> Iterator[DesktopSession]:
	with DesktopSession(tmp_path / "desktop") as session:
		yield session


@pytest.fixture
def socketDir() -> Iterator[Path]:
	"""Where the stand-in listens: a socket's path may be no longer than 107 bytes, and pytest's
	folders can be longer."""
	directory = Path(tempfile.mkdtemp(prefix="keyd", dir="/tmp"))
	try:
		yield directory
	finally:
		shutil.rmtree(directory, ignore_errors=True)


def start(desktop: DesktopSession, socketPath: Path) -> harness.Launch:
	app = desktop.startApp(desktop.fixture(GTK[0]), title=GTK[1])
	assert app.stdout.readline() == "ready\n"
	desktopSession.activate(GTK[1])
	nvda = desktop.startNVDA(SESSION_CONFIG, env={"LINDA_TEST_KEYD_SOCKET": str(socketPath)})
	desktop.expect(*AT_START, since=0, timeout=STARTUP_BOUND)
	return nvda


def quitClean(desktop: DesktopSession, nvda: harness.Launch) -> None:
	time.sleep(RUN_ON)
	desktop.close()
	assert nvda.process.returncode == 0, nvda.describe()
	nvda.assertClean(logNames=("nvda.log",))
	log = nvda.text("nvda.log")
	assert "Keys are read from the X display" not in log, log[-6000:]


def test_startsCleanWithNoHelper(desktop: DesktopSession, socketDir: Path) -> None:
	nvda = start(desktop, socketDir / "socket")
	desktop.expect(NO_HELPER, since=0)
	quitClean(desktop, nvda)
	assert desktop.spokenSince(0).count(NO_HELPER) == 1


def test_startsCleanWithTheHelperHoldingTheKeyboard(desktop: DesktopSession, socketDir: Path) -> None:
	with StandIn(socketDir / "socket", sink=keydStandIn.compositorSink()) as keyboard:
		nvda = start(desktop, socketDir / "socket")
		keyboard.waitFor(lambda: keyboard.capturing, CAPTURE_BOUND)
		desktop.expect(HINT, since=0)
		mark = desktop.mark()
		keyboard.press("Insert+t")
		desktop.expect(GTK[1], since=mark)
		quitClean(desktop, nvda)
	assert NO_HELPER not in desktop.spokenSince(0)
	assert (
		"Keys come from the kernel through linda-keyd: linda-keyd admitted NVDA and holds the keyboard"
		in (nvda.text("nvda.log"))
	)
