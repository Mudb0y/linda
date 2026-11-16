"""A desktop in the rig for tests that read applications: a window manager, applications started and
waited for, keys typed on the rig's display, and NVDA with what it speaks.

Inside the rig only. Keys go through xdotool to the rig's own X display, and every process this
starts is one of its own children, which close stops by its process ID.
"""

import os
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Self

import harness

#: The applications tests read, each a program fixture names.
APPS = harness.HERE / "fixtures" / "apps"
#: Ends an expected text that only has to start a speak record.
PREFIX_MARK = "\N{HORIZONTAL ELLIPSIS}"
#: Seconds to wait for marco to set up its desktops, which takes it about a tenth of one.
WINDOW_MANAGER_TIMEOUT = 5.0
#: Seconds NVDA has to exit on SIGTERM, and an application to exit once asked to, before it is
#: killed.
NVDA_EXIT_TIMEOUT = 10.0
APP_EXIT_TIMEOUT = 3.0
#: Seconds any one AT-SPI call waits for an application that does not answer.
ATSPI_TIMEOUT_MS = 1000


def windowManagerIsRunning() -> bool:
	"""Whether a window manager has named itself on the root window and set up its desktops."""
	shown = subprocess.run(
		["xprop", "-root", "_NET_SUPPORTING_WM_CHECK", "_NET_NUMBER_OF_DESKTOPS"],
		capture_output=True,
		text=True,
		check=False,
	).stdout
	return "_NET_NUMBER_OF_DESKTOPS(CARDINAL)" in shown


def _atspi():
	import gi

	gi.require_version("Atspi", "2.0")
	from gi.repository import Atspi

	Atspi.init()
	Atspi.set_timeout(ATSPI_TIMEOUT_MS, ATSPI_TIMEOUT_MS)
	return Atspi


def topLevelNames() -> list[str]:
	"""The names of every application's top-level windows on the accessibility desktop, read afresh."""
	Atspi = _atspi()
	from gi.repository import GLib

	desktop = Atspi.get_desktop(0)
	# libatspi keeps a desktop's children up to date from events that only a running main loop
	# delivers; without one it would answer from what it read first.
	desktop.clear_cache()
	names = []
	for appIndex in range(desktop.get_child_count()):
		try:
			app = desktop.get_child_at_index(appIndex)
			if app is None:
				continue
			app.clear_cache()
			for index in range(app.get_child_count()):
				window = app.get_child_at_index(index)
				if window is not None:
					names.append(window.get_name() or "")
		except GLib.Error:
			continue
	return names


def _matches(expected: str, spoken: str) -> bool:
	if expected.endswith(PREFIX_MARK):
		return spoken.startswith(expected[: -len(PREFIX_MARK)])
	return spoken == expected


def _inOrder(expected: Sequence[str], spoken: Sequence[str]) -> bool:
	"""Whether each expected text matches a speak record, each after the one the previous matched."""
	records = iter(spoken)
	return all(any(_matches(text, record) for record in records) for text in expected)


class DesktopSession:
	"""The rig's display with a window manager, the applications a test started, and its NVDA.

	Every process it starts is stopped by close, NVDA first, by the process IDs it started.
	"""

	def __init__(self, directory: Path, *, windowManager: bool = True) -> None:
		"""
		:param directory: Where NVDA's runs and the applications' standard error go.
		:param windowManager: Whether to have marco running, which the rig starts when
			BASEDREADER_RIG_WM=marco is in its caller's environment, and this does otherwise.
		"""
		harness.requireRig()
		self.directory = directory
		directory.mkdir(parents=True, exist_ok=True)
		self._apps: list[subprocess.Popen] = []
		self._launches: list[harness.Launch] = []
		self._windowManager: subprocess.Popen | None = None
		if windowManager and not windowManagerIsRunning():
			self._startWindowManager()

	def __enter__(self) -> Self:
		return self

	def __exit__(self, *exception: object) -> None:
		self.close()

	def _startWindowManager(self) -> None:
		with open(self.directory / "wm.log", "w") as log:
			self._windowManager = subprocess.Popen(
				["marco", "--no-force-fullscreen"],
				stdout=log,
				stderr=subprocess.STDOUT,
			)
		deadline = time.monotonic() + WINDOW_MANAGER_TIMEOUT
		while not windowManagerIsRunning():
			if self._windowManager.poll() is not None or time.monotonic() >= deadline:
				raise AssertionError(
					f"marco did not set up its desktops within {WINDOW_MANAGER_TIMEOUT} s, "
					f"{harness.describeExit(self._windowManager.poll())}\n"
					f"{(self.directory / 'wm.log').read_text()}",
				)
			time.sleep(0.05)

	@property
	def nvda(self) -> harness.Launch | None:
		"""The NVDA startNVDA started last."""
		return self._launches[-1] if self._launches else None

	def fixture(self, name: str, *args: str) -> list[str]:
		"""The command that runs tests/linux/fixtures/apps/<name>, ".py" optional, with args."""
		path = APPS / name
		if not path.exists() and (APPS / f"{name}.py").exists():
			path = APPS / f"{name}.py"
		assert path.is_file(), f"no fixture {name} in {APPS}"
		return [sys.executable, str(path), *args]

	def startApp(self, command: Sequence[str], *, title: str, timeout: float = 10) -> subprocess.Popen:
		"""Start command and wait until a top-level window named title is on the accessibility desktop.

		Its standard input and output are pipes in text mode, for the fixtures' commands and answers;
		a fixture's first line is "ready", and one built on commands.py quits when its input closes.
		Its standard error goes to a file in the session's directory.
		"""
		env = dict(os.environ)
		qtPlugins = os.environ.get("BASEDREADER_QT6_PLUGIN_PATH")
		if qtPlugins and "QT_PLUGIN_PATH" not in env:
			# Without its platform plugin a PyQt6 program aborts before it opens a window.
			env["QT_PLUGIN_PATH"] = qtPlugins
		stderrPath = self.directory / f"app{len(self._apps)}.stderr.txt"
		started = time.monotonic()
		with open(stderrPath, "w") as stderr:
			process = subprocess.Popen(
				list(command),
				stdin=subprocess.PIPE,
				stdout=subprocess.PIPE,
				stderr=stderr,
				text=True,
				env=env,
			)
		self._apps.append(process)
		while title not in (names := topLevelNames()):
			if process.poll() is not None or time.monotonic() - started >= timeout:
				raise AssertionError(
					f"no window {title!r} within {timeout} s of starting {list(command)}, "
					f"{harness.describeExit(process.poll())}; windows: {names}\n"
					f"{stderrPath.read_text()[-3000:]}",
				)
			time.sleep(0.05)
		return process

	def startNVDA(
		self,
		config: Path,
		*arguments: str,
		env: dict[str, str | None] | None = None,
	) -> harness.Launch:
		"""Start NVDA through harness.startLauncher, with the transcript synth, debug logging, and a
		configuration directory copied from config, a directory or an nvda.ini.

		Its log is nvda.log in the launch's directory. The run becomes the one mark, spokenSince and
		expect read.
		"""
		directory = self.directory / f"nvda{len(self._launches)}"
		configDir = directory / "config"
		if config.is_dir():
			shutil.copytree(config, configDir)
		else:
			configDir.mkdir(parents=True)
			shutil.copyfile(config, configDir / "nvda.ini")
		launch = harness.startLauncher(
			directory,
			"-c",
			str(configDir),
			"-f",
			str(directory / "nvda.log"),
			"--debug-logging",
			*arguments,
			env=env,
		)
		self._launches.append(launch)
		return launch

	def press(self, *chords: str, gap: float = 0.15) -> None:
		"""Press each chord in turn on the rig's display, with gap seconds after each.

		Chords use xdotool's key names: "Tab", "Insert+t", "KP_Up", "alt+Tab".
		"""
		for chord in chords:
			subprocess.run(["xdotool", "key", chord], check=True)
			time.sleep(gap)

	def typeText(self, text: str) -> None:
		"""Type text on the rig's display, one key at a time, as a person would."""
		subprocess.run(["xdotool", "type", "--delay", "40", text], check=True)

	def mark(self) -> int:
		"""How many speak records NVDA has made so far, to give spokenSince or expect."""
		return len(self.nvda.spoken()) if self.nvda else 0

	def spokenSince(self, mark: int) -> list[str]:
		"""The text of each speak record made after mark."""
		return self.nvda.spokenTexts()[mark:] if self.nvda else []

	def expect(self, *texts: str, since: int, timeout: float = 5) -> None:
		"""Wait until NVDA has spoken texts, in this order, after the mark since.

		Each text must be a whole speak record, or its start when it ends with "…". Other records may
		come between them.
		"""
		nvda = self.nvda
		assert nvda is not None, "NVDA was not started"
		deadline = time.monotonic() + timeout
		while True:
			exited = nvda.process.poll() is not None
			# Read after the poll, so that what NVDA spoke before it exited is seen.
			spoken = self.spokenSince(since)
			if _inOrder(texts, spoken):
				return
			if exited or time.monotonic() >= deadline:
				raise AssertionError(
					f"NVDA did not speak {list(texts)} in order within {timeout} s; it spoke {spoken}\n"
					f"{nvda.describe()}",
				)
			time.sleep(0.05)

	def close(self) -> None:
		"""Quit NVDA with SIGTERM, then the applications, then marco if this started it, each killed
		when it does not exit in time."""
		for launch in self._launches:
			_stop(launch.process, signal.SIGTERM, NVDA_EXIT_TIMEOUT)
		for process in reversed(self._apps):
			if process.stdin and not process.stdin.closed:
				try:
					process.stdin.close()
				except BrokenPipeError:
					pass
			_stop(process, signal.SIGTERM, APP_EXIT_TIMEOUT)
			if process.stdout:
				process.stdout.close()
		if self._windowManager is not None:
			_stop(self._windowManager, signal.SIGTERM, APP_EXIT_TIMEOUT)
		self._apps.clear()
		self._windowManager = None


def _stop(process: subprocess.Popen, signalNumber: signal.Signals, timeout: float) -> None:
	if process.poll() is None:
		process.send_signal(signalNumber)
		try:
			process.wait(timeout)
		except subprocess.TimeoutExpired:
			process.kill()
			process.wait()
