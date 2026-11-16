"""Stops a test run that was started outside tests/linux/rig, before any test is collected, and gives
every test the launches fixture."""

from collections.abc import Iterator
from pathlib import Path

import harness
import pytest


def pytest_sessionstart(session: pytest.Session) -> None:
	why = harness.whyNotInRig()
	if why is not None:
		pytest.exit(
			f"refusing to run outside the rig ({why}). "
			"Use: nix develop . -c tests/linux/rig python -m pytest tests/linux",
			returncode=pytest.ExitCode.USAGE_ERROR,
		)


@pytest.fixture
def launches(tmp_path: Path) -> Iterator:
	"""launches(name, *arguments, env=None) starts the launcher through harness.startLauncher; none
	outlives the test, and the test fails if one changed the repository."""
	yield from harness.launches(tmp_path / "launches")
