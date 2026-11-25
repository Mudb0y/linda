"""Alt+Tab under marco with Alt held while the switcher names the windows, as someone listening to each
name before letting go holds it. The window left says it is inactive as the switcher takes the keyboard,
and the window chosen takes it only once Alt is let go. marco 1.26's switcher never says it is active,
so a second after the window left said so Linda found no active window and said "Desktop desktop pane"
in the middle of the switch. fixtures/globalPlugins/quietSwitcher.py makes the rig's marco 1.28, whose
switcher says it is active, quiet as 1.26's.

Input help does the same: NVDA holds the keyboard itself while it is on, and the window it was in says
it is inactive until input help is turned off.

nix develop . -c tests/linux/rig python3 -m pytest -q tests/linux/test_altTabKeepsTheWindow.py
"""

import subprocess
import time
from pathlib import Path

import desktopSession
import harness
import pytest
from desktopSession import DesktopSession

SESSION_CONFIG = harness.HERE / "fixtures" / "sessionConfig" / "nvda.ini"
#: Seconds from starting NVDA to its first words; a start under the rig takes about three.
STARTUP_BOUND = 60.0
GTK3 = "Fixture window"
GTK4 = "GTK4 fixture"
#: How Linda says that title.
GTK4_SAID = "GTK 4 fixture"
DESKTOP = "Desktop desktop pane"
#: Seconds Alt is held after the switcher names the window, longer than Linda's last look for an active
#: window, a second after the window left said it was inactive.
HELD = 2.0
#: Seconds after Alt is let go by which the window chosen has said it is active, or the desktop has been
#: given the focus.
SETTLE = 1.5


def altTab(desktop: DesktopSession, chosen: str, held: float) -> int:
	"""Alt+Tab, Alt held for held seconds after marco's switcher has named the window chosen; the mark
	before it."""
	mark = desktop.mark()
	subprocess.run(["xdotool", "keydown", "alt"], check=True)
	try:
		subprocess.run(["xdotool", "key", "Tab"], check=True)
		desktop.expect(chosen, since=mark)
		time.sleep(held)
	finally:
		subprocess.run(["xdotool", "keyup", "alt"], check=True)
	time.sleep(SETTLE)
	return mark


@pytest.mark.parametrize("switcher", ["quiet", "active"])
def test_noDesktopWhileTheSwitcherIsOpen(tmp_path: Path, switcher: str):
	with DesktopSession(tmp_path / "desktop") as desktop:
		gtk4 = desktop.startApp(desktop.fixture("gtk4"), title=GTK4)
		assert gtk4.stdout.readline() == "ready\n"
		gtk3 = desktop.startApp(desktop.fixture("gtkFixture"), title=GTK3)
		assert gtk3.stdout.readline() == "ready\n"
		desktopSession.activate(GTK3)
		env = {"LINDA_QUIET_SWITCHER": "1"} if switcher == "quiet" else {}
		nvda = desktop.startNVDA(SESSION_CONFIG, env=env)
		desktop.expect(GTK3, since=0, timeout=STARTUP_BOUND)
		said = []
		for chosen, held in ((GTK4_SAID, 0.3), (GTK3, 0.3), (GTK4_SAID, HELD), (GTK3, HELD)):
			mark = altTab(desktop, chosen, held)
			said.append((chosen, desktop.spokenSince(mark)))
		assert not [(chosen, spoken) for chosen, spoken in said if DESKTOP in spoken], said
		desktop.close()
		assert nvda.process.returncode == 0, nvda.describe()
		nvda.assertClean(logNames=("nvda.log",))


#: What NVDA says as input help is turned on and off.
INPUT_HELP_ON = "input help on\N{HORIZONTAL ELLIPSIS}"
INPUT_HELP_OFF = "input help off"


def test_noDesktopWhileInputHelpIsOn(tmp_path: Path):
	with DesktopSession(tmp_path / "desktop") as desktop:
		gtk3 = desktop.startApp(desktop.fixture("gtkFixture"), title=GTK3)
		assert gtk3.stdout.readline() == "ready\n"
		desktopSession.activate(GTK3)
		nvda = desktop.startNVDA(SESSION_CONFIG)
		desktop.expect(GTK3, since=0, timeout=STARTUP_BOUND)
		time.sleep(SETTLE)
		mark = desktop.mark()
		desktop.press("Insert+1")
		desktop.expect(INPUT_HELP_ON, since=mark)
		time.sleep(HELD + SETTLE)
		desktop.press("Insert+1")
		desktop.expect("Linda plus 1", INPUT_HELP_OFF, since=mark)
		time.sleep(SETTLE)
		spoken = desktop.spokenSince(mark)
		assert DESKTOP not in spoken, spoken
		# The focus never left the window, so nothing more is said as NVDA lets go, as on Windows.
		assert spoken[-1] == INPUT_HELP_OFF, spoken
		desktop.close()
		assert nvda.process.returncode == 0, nvda.describe()
		nvda.assertClean(logNames=("nvda.log",))
