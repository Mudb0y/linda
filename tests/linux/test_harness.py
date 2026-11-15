"""The harness reports what a child did, including what upstream would have swallowed.

nix develop . -c tests/linux/rig python -m pytest tests/linux/test_harness.py
"""

from pathlib import Path

import harness


def test_valueComesBack(tmp_path: Path):
	done = harness.runChild(tmp_path, "result = {'sum': 1 + 2}", prepare="none")
	done.assertClean()
	assert done.value == {"sum": 3}


def test_exceptionIsReported(tmp_path: Path):
	done = harness.runChild(tmp_path, "raise ValueError('expected by the test')", prepare="none")
	assert done.error is not None and "expected by the test" in done.error


def test_swallowedWindowsCallIsReported(tmp_path: Path):
	done = harness.runChild(
		tmp_path,
		"""
		import ctypes
		try:
			ctypes.windll.user32.GetForegroundWindow()
		except NotImplementedError:
			pass
		result = "carried on"
		""",
		prepare="bootstrap",
	)
	assert done.error is None, done.error
	assert done.value == "carried on"
	assert any("GetForegroundWindow" in what for what in done.noLinuxImplementation)


def test_loggedErrorIsReported(tmp_path: Path):
	done = harness.runChild(
		tmp_path,
		"""
		from logHandler import log
		log.error("expected by the test")
		""",
	)
	assert any("expected by the test" in record for record in done.logErrors)
