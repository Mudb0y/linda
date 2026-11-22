"""Stops a test run that was started outside tests/linux/rig, before any test is collected, fails a
test that skips unless its skip is expected, leaves out a test marked for another of the rig's sessions
than this run's or for a variable that is unset, and gives every test the launches fixture and, from pipewireFixture, the pipewire
fixture."""

import os
from collections.abc import Iterator
from pathlib import Path

import harness
import network
import pytest
from pipewireFixture import runningPipewire  # noqa: F401


def pytest_sessionstart(session: pytest.Session) -> None:
	why = harness.whyNotInRig()
	if why is not None:
		pytest.exit(
			f"refusing to run outside the rig ({why}). "
			"Use: nix develop . -c tests/linux/rig python -m pytest tests/linux",
			returncode=pytest.ExitCode.USAGE_ERROR,
		)


def pytest_configure(config: pytest.Config) -> None:
	config.addinivalue_line(
		"markers",
		"rigSession(*sessions): the test runs only in these of the rig's sessions, x11, wayland, mutter "
		"and sway, and is deselected in any other. A test that forces Linda's session type while the rig "
		"gives it the X display is marked x11.",
	)
	config.addinivalue_line(
		"markers",
		"needsVariable(name): the test reads what is built outside the rig and named to it in this "
		"environment variable, and is deselected while the variable is unset.",
	)


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
	# Deselected rather than skipped: a skip fails here, and a test for another session has not stopped
	# running, it runs in that session.
	kept, left = [], []
	for item in items:
		marks = list(item.iter_markers("rigSession"))
		variables = [mark.args[0] for mark in item.iter_markers("needsVariable")]
		if all(harness.rigSessionIsOneOf(mark.args) for mark in marks) and all(
			os.environ.get(name) for name in variables
		):
			kept.append(item)
		else:
			left.append(item)
	if left:
		config.hook.pytest_deselected(items=left)
		items[:] = kept


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
		# A rig without the internet is the usual one, so a test that needs it skips honestly.
		if network.SKIP_REASON in str(reason):
			return
		report.outcome = "failed"
		report.longrepr = f"{name} skipped, which in the rig only EXPECTED_SKIPS may: {reason}"


@pytest.fixture
def launches(tmp_path: Path) -> Iterator:
	"""launches(name, *arguments, env=None) starts the launcher through harness.startLauncher; none
	outlives the test, and the test fails if one changed the repository."""
	yield from harness.launches(tmp_path / "launches")
