"""What Linda offers of NVDA's features that Linux lacks, met from the keyboard as a person meets it: the
NVDA menu's Tools and Help, the settings dialog's categories and the panels that lose controls, the
braille display dialog, the Input Gestures dialog, and a hidden command still reached by a gesture.
platformFeatures decides each of these. Every key is pressed on the rig's display under marco.

nix develop . -c tests/linux/rig env LINDA_RIG_WM=marco python3 -m pytest -q tests/linux/test_platformFeatures.py

Without LINDA_RIG_WM=marco, DesktopSession starts marco itself. The transcript holds what the
synthesizer is given, after symbols are spoken as words, so "Alt+P" is "Alt plus P".
"""

import os
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
#: Seconds for anything that should have happened by now to show.
SETTLE = 1.0
ISSUES_URL = "https://github.com/Mudb0y/linda/issues"
UNAVAILABLE = "Not available on Linux."

GTK = ("gtkFixture", "Fixture window")
AT_START = ("Fixture window", "Press me button Does nothing Alt plus P")


@pytest.fixture
def desktop(tmp_path: Path) -> Iterator[DesktopSession]:
	with DesktopSession(tmp_path / "desktop") as session:
		yield session


def start(desktop: DesktopSession, config: Path = SESSION_CONFIG, env: dict[str, str] | None = None):
	app = desktop.startApp(desktop.fixture(GTK[0]), title=GTK[1])
	# On the accessibility desktop before it has presented itself and has the keyboard.
	assert app.stdout.readline() == "ready\n"
	desktopSession.activate(GTK[1])
	nvda = desktop.startNVDA(config, env=env)
	desktop.expect(*AT_START, since=0, timeout=STARTUP_BOUND)
	return nvda


def step(desktop: DesktopSession, chord: str, *expected: str, timeout: float = 5) -> int:
	"""Press chord and wait for expected, in order; return the mark taken before the key."""
	mark = desktop.mark()
	desktop.press(chord)
	desktop.expect(*expected, since=mark, timeout=timeout)
	return mark


def walk(desktop: DesktopSession, key: str, items: list[str]) -> None:
	"""Press key once for each of items after the first, hearing each, and once more to come back to the
	first, so that nothing else is in the list."""
	for item in [*items[1:], items[0]]:
		step(desktop, key, item)


def assertExitedCleanly(nvda: harness.Launch) -> None:
	assert nvda.process.returncode == 0, nvda.describe()
	nvda.assertClean(logNames=("nvda.log",))


def fakeOpener(directory: Path) -> tuple[dict[str, str], Path]:
	"""An environment whose xdg-open writes what it is asked to open to the returned file and opens
	nothing."""
	bin = directory / "bin"
	bin.mkdir(parents=True)
	opened = directory / "opened"
	opener = bin / "xdg-open"
	opener.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" >> {opened}\n', encoding="utf-8")
	opener.chmod(0o755)
	return {"PATH": f"{bin}:{os.environ['PATH']}"}, opened


def test_toolsAndHelpMenusOfferOnlyWhatWorksAndHelpWithLinda(desktop: DesktopSession, tmp_path: Path):
	env, opened = fakeOpener(tmp_path)
	nvda = start(desktop, env=env)
	step(desktop, "Insert+n", "Preferences sub Menu P 1 of 9")
	step(desktop, "Down", "Tools sub Menu T 2 of 9")
	step(desktop, "Right", "View log L 1 of 6")
	walk(
		desktop,
		"Down",
		[
			"View log L 1 of 6",
			"Speech viewer not checked S 2 of 6",
			"Braille viewer not checked B 3 of 6",
			"Add-on store... A 4 of 6",
			"Python console P 5 of 6",
			"Reload plugins 6 of 6",
		],
	)
	step(desktop, "Escape", "Tools sub Menu T 2 of 9")
	step(desktop, "Down", "Help sub Menu H 3 of 9")
	step(desktop, "Right", "User Guide U 1 of 8")
	walk(
		desktop,
		"Down",
		[
			"User Guide U 1 of 8",
			"Commands Quick Reference Q 2 of 8",
			"What's new N 3 of 8",
			"NV Access web site W 4 of 8",
			"Report a problem with Linda R 5 of 8",
			"License I 6 of 8",
			"Welcome dialog... L 7 of 8",
			"About... A 8 of 8",
		],
	)
	step(desktop, "r", *AT_START)
	deadline = time.monotonic() + 10
	while not opened.exists() and time.monotonic() < deadline:
		time.sleep(0.1)
	assert opened.read_text(encoding="utf-8").splitlines() == [ISSUES_URL]
	desktop.close()
	assertExitedCleanly(nvda)
