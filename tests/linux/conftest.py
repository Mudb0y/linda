"""Stops a test run that was started outside tests/linux/rig, before any test is collected, fails a
test that skips unless its skip is expected, and gives every test the launches fixture."""

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


#: The tests that may skip in the rig, by file and function, each with why its skip is honest. Any
#: other skip fails: the rig supplies everything a test needs, so a skip there hides a test that
#: stopped running.
EXPECTED_SKIPS = {
	"test_processIdentity.py::test_processFacts": "the expected architecture is spelled out only for "
	"x86_64 and aarch64, so on any other machine the test cannot say what to expect",
}


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo) -> Iterator[None]:
	outcome = yield
	report = outcome.get_result()
	name = f"{item.path.name}::{getattr(item, 'originalname', item.name)}"
	if report.skipped and not hasattr(report, "wasxfail") and name not in EXPECTED_SKIPS:
		reason = report.longrepr[2] if isinstance(report.longrepr, tuple) else report.longrepr
		report.outcome = "failed"
		report.longrepr = f"{name} skipped, which in the rig only EXPECTED_SKIPS may: {reason}"


@pytest.fixture
def launches(tmp_path: Path) -> Iterator:
	"""launches(name, *arguments, env=None) starts the launcher through harness.startLauncher; none
	outlives the test, and the test fails if one changed the repository."""
	yield from harness.launches(tmp_path / "launches")
