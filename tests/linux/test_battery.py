"""Report battery status reads UPower, and the charger going in or out is announced, as on Windows.

The rig has no system bus, so NVDA is pointed at the rig's session bus, where a stand-in for UPower
answers. Run under the rig:

	nix develop . -c tests/linux/rig python3 -m pytest tests/linux/test_battery.py
"""

import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import harness
import pytest
from desktopSession import DesktopSession
from test_keys import configText

STARTUP_TIMEOUT = 60.0
STAND_IN = harness.HERE / "fixtures" / "upower.py"
REPORT = "Insert+shift+b"


class UPower:
	"""The stand-in UPower, told what to say a line at a time."""

	def __init__(self, directory: Path) -> None:
		self._stderr = open(directory / "upower.stderr.txt", "w")  # noqa: SIM115
		self.process = subprocess.Popen(
			[sys.executable, str(STAND_IN)],
			stdin=subprocess.PIPE,
			stdout=subprocess.PIPE,
			stderr=self._stderr,
			text=True,
		)
		assert self.process.stdout.readline().strip() == "ready", "the stand-in UPower did not start"

	def set(self, **properties) -> None:
		line = " ".join(
			f"{name} {int(value) if isinstance(value, bool) else value}" for name, value in properties.items()
		)
		self.process.stdin.write(line + "\n")
		self.process.stdin.flush()
		assert self.process.stdout.readline().strip() == "ok"

	def close(self) -> None:
		self.process.stdin.close()
		try:
			self.process.wait(3)
		except subprocess.TimeoutExpired:
			self.process.kill()
			self.process.wait()
		self.process.stdout.close()
		self._stderr.close()


@pytest.fixture
def desktop(tmp_path: Path) -> Iterator[DesktopSession]:
	with DesktopSession(tmp_path / "desktop") as session:
		yield session


@pytest.fixture
def upower(tmp_path: Path) -> Iterator[UPower]:
	service = UPower(tmp_path)
	yield service
	service.close()


def startNVDA(desktop: DesktopSession, tmpPath: Path, env: dict[str, str | None]) -> None:
	config = tmpPath / "nvda.ini"
	config.write_text(configText("desktop", 6))
	desktop.startApp(desktop.fixture("gtkFixture"), title="Fixture window")
	desktop.startNVDA(config, env=env)
	desktop.expect("Fixture window", since=0, timeout=STARTUP_TIMEOUT)


def report(desktop: DesktopSession, expected: str) -> None:
	mark = desktop.mark()
	desktop.press(REPORT)
	desktop.expect(expected, since=mark)


def quit(desktop: DesktopSession) -> None:
	launch = desktop.nvda
	launch.process.terminate()
	launch.waitForExit(20)
	launch.assertClean(logNames=("nvda.log",))


def test_batteryReportAndCharger(desktop: DesktopSession, upower: UPower, tmp_path: Path):
	startNVDA(desktop, tmp_path, {"DBUS_SYSTEM_BUS_ADDRESS": os.environ["DBUS_SESSION_BUS_ADDRESS"]})
	report(desktop, "No system battery")
	upower.set(IsPresent=True, Type=2, Percentage=80.4, State=1, TimeToFull=1200)
	report(desktop, "80 percent Plugged in")
	# The charger pulled out is announced, the new state first, and without a key.
	mark = desktop.mark()
	upower.set(Percentage=79, State=2, TimeToEmpty=5400, TimeToFull=0, OnBattery=True)
	desktop.expect("Unplugged 79 percent 1 hour and 30 minutes remaining", since=mark)
	report(desktop, "79 percent 1 hour and 30 minutes remaining Unplugged")
	upower.set(Percentage=20, TimeToEmpty=1800)
	report(desktop, "20 percent 30 minutes remaining Unplugged")
	upower.set(Percentage=3, TimeToEmpty=7200)
	report(desktop, "3 percent 2 hours remaining Unplugged")
	# Only a change of charger is announced, not the battery running down.
	mark = desktop.mark()
	upower.set(Percentage=2, TimeToEmpty=30)
	report(desktop, "2 percent Less than 1 minute remaining Unplugged")
	assert desktop.spokenSince(mark) == ["2 percent Less than 1 minute remaining Unplugged"]
	mark = desktop.mark()
	upower.set(State=1, TimeToEmpty=0, TimeToFull=3600, OnBattery=False)
	desktop.expect("Plugged in 2 percent", since=mark)
	quit(desktop)


def test_noUPower(desktop: DesktopSession, tmp_path: Path):
	# A system bus with no UPower on it.
	startNVDA(desktop, tmp_path, {"DBUS_SYSTEM_BUS_ADDRESS": os.environ["DBUS_SESSION_BUS_ADDRESS"]})
	report(desktop, "Unknown power status")
	quit(desktop)


def test_noSystemBus(desktop: DesktopSession, tmp_path: Path):
	startNVDA(desktop, tmp_path, {"DBUS_SYSTEM_BUS_ADDRESS": "unix:path=/nonexistent/system_bus_socket"})
	report(desktop, "Unknown power status")
	quit(desktop)
