"""Stops a test run that was started outside tests/linux/rig, before any test is collected."""

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
