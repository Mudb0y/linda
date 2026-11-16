"""Focus and the foreground read from AT-SPI under marco: what NVDA says at start, on Alt+Tab and when a
dialog closes, and how it copes with an application that stops answering.

nix develop . -c tests/linux/rig python3 -m pytest tests/linux/test_atspiFocus.py
"""

import errno
import os
import signal
import subprocess
import textwrap
import time
from collections.abc import Iterator
from pathlib import Path

import harness
import pytest
from desktopSession import DesktopSession

BOOT_CONFIG = harness.HERE / "fixtures" / "bootConfig" / "nvda.ini"
#: Seconds from starting NVDA to its first words; a start under the rig takes about three.
STARTUP_BOUND = 60.0
#: Seconds with nothing new spoken after which NVDA is taken to have finished speaking.
QUIET = 0.8
GTK_TITLE = "Fixture window"
QT_TITLE = "Qt fixture window"
#: Both fixtures start with their first button focused, and name and describe it alike.
PRESS_ME = "Press me button Does nothing Alt plus P"

#: A global plugin that runs key gestures sent to it through a FIFO as if their keys had been pressed,
#: for commands whose keys NVDA cannot take from the rig's display by itself.
PROBE = textwrap.dedent(
	'''\
	"""Runs a gesture for each line "gesture NAME" written to the FIFO BASEDREADER_FOCUS_PROBE names."""

	import os
	import threading

	import globalPluginHandler
	import inputCore
	import keyboardHandler
	import queueHandler


	def _run(name):
		inputCore.manager.executeGesture(keyboardHandler.KeyboardInputGesture.fromName(name))


	class GlobalPlugin(globalPluginHandler.GlobalPlugin):
		def __init__(self):
			super().__init__()
			path = os.environ.get("BASEDREADER_FOCUS_PROBE")
			if path:
				threading.Thread(target=self._read, args=(path,), name="focusProbe", daemon=True).start()

		def _read(self, path):
			with open(path, encoding="utf-8") as f:
				for line in f:
					command, _, argument = line.strip().partition(" ")
					if command == "gesture":
						queueHandler.queueFunction(queueHandler.eventQueue, _run, argument)
	''',
)


@pytest.fixture
def desktop(tmp_path: Path) -> Iterator[DesktopSession]:
	with DesktopSession(tmp_path / "desktop") as session:
		yield session


class Probe:
	"""The FIFO the probe plugin reads gestures from."""

	def __init__(self, path: Path) -> None:
		self.path = path
		self._fd: int | None = None

	def gesture(self, name: str, timeout: float = 30) -> None:
		if self._fd is None:
			deadline = time.monotonic() + timeout
			while True:
				try:
					self._fd = os.open(self.path, os.O_WRONLY | os.O_NONBLOCK)
					break
				except OSError as e:
					# No reader yet: the plugin has not started.
					if e.errno != errno.ENXIO or time.monotonic() >= deadline:
						raise
					time.sleep(0.05)
		os.write(self._fd, f"gesture {name}\n".encode())

	def close(self) -> None:
		if self._fd is not None:
			os.close(self._fd)
			self._fd = None


def _startNVDA(desktop: DesktopSession, tmp_path: Path) -> tuple[harness.Launch, Probe]:
	config = tmp_path / "nvda.ini"
	text = BOOT_CONFIG.read_text(encoding="utf-8")
	# The welcome dialog would take the focus from the fixtures.
	config.write_text(
		text.replace("[general]\n", "[general]\n\tshowWelcomeDialogAtStartup = False\n", 1),
		encoding="utf-8",
	)
	hooks = tmp_path / "hooks"
	(hooks / "globalPlugins").mkdir(parents=True)
	(hooks / "globalPlugins" / "focusProbe.py").write_text(PROBE, encoding="utf-8")
	(hooks / "synthDrivers").symlink_to(harness.HERE / "synthDrivers")
	fifo = tmp_path / "probe.fifo"
	os.mkfifo(fifo)
	launch = desktop.startNVDA(
		config,
		env={"BASEDREADER_TEST_HOOKS_DIR": str(hooks), "BASEDREADER_FOCUS_PROBE": str(fifo)},
	)
	return launch, Probe(fifo)


def _waitForQuiet(desktop: DesktopSession, limit: float = 10) -> None:
	"""Wait until NVDA has said nothing new for QUIET seconds."""
	end = time.monotonic() + limit
	count = desktop.mark()
	last = time.monotonic()
	while time.monotonic() < end:
		time.sleep(0.05)
		now = desktop.mark()
		if now != count:
			count, last = now, time.monotonic()
		elif time.monotonic() - last >= QUIET:
			return


def _firstRecordAfter(nvda: harness.Launch, mark: int, timeout: float) -> dict:
	deadline = time.monotonic() + timeout
	while True:
		records = nvda.spoken()[mark:]
		if records:
			return records[0]
		assert time.monotonic() < deadline, f"nothing spoken within {timeout} s\n{nvda.describe()}"
		time.sleep(0.01)


def _assertNoFreeze(nvda: harness.Launch) -> None:
	log = nvda.text("nvda.log")
	assert "freeze recovery" not in log and "Recovered from freeze" not in log, log[-6000:]


def _closeClean(desktop: DesktopSession, nvda: harness.Launch) -> None:
	desktop.close()
	assert nvda.process.returncode == 0, nvda.describe()
	nvda.assertClean(logNames=("nvda.log",))
	_assertNoFreeze(nvda)


def test_startSpeaksTheActiveWindowAndItsFocus(desktop: DesktopSession, tmp_path: Path):
	desktop.startApp(desktop.fixture("gtkFixture"), title=GTK_TITLE)
	nvda, probe = _startNVDA(desktop, tmp_path)
	desktop.expect(GTK_TITLE, PRESS_ME, since=0, timeout=STARTUP_BOUND)
	probe.close()
	_closeClean(desktop, nvda)


def test_altTabSpeaksTitleAndFocusOnce(desktop: DesktopSession, tmp_path: Path):
	desktop.startApp(desktop.fixture("qtFixture"), title=QT_TITLE)
	gtk = desktop.startApp(desktop.fixture("gtkFixture"), title=GTK_TITLE)
	# Started last, it is presented last and is the active window.
	assert gtk.stdout.readline() == "ready\n"
	nvda, probe = _startNVDA(desktop, tmp_path)
	desktop.expect(GTK_TITLE, PRESS_ME, since=0, timeout=STARTUP_BOUND)
	_waitForQuiet(desktop)
	for title in (QT_TITLE, GTK_TITLE, QT_TITLE, GTK_TITLE):
		mark = desktop.mark()
		desktop.press("alt+Tab")
		desktop.expect(title, PRESS_ME, since=mark)
		_waitForQuiet(desktop)
		assert desktop.spokenSince(mark) == [title, PRESS_ME]
	probe.close()
	_closeClean(desktop, nvda)


def test_closingADialogReturnsToTheWindowsFocus(desktop: DesktopSession, tmp_path: Path):
	app = desktop.startApp(desktop.fixture("twoWindows"), title="Second window")
	assert app.stdout.readline() == "ready\n"

	def command(line: str) -> None:
		app.stdin.write(line + "\n")
		app.stdin.flush()
		assert app.stdout.readline() == f"done {line}\n"

	nvda, probe = _startNVDA(desktop, tmp_path)
	_waitForQuiet(desktop, limit=STARTUP_BOUND)
	mark = desktop.mark()
	command("first")
	desktop.expect("First window", "Alpha button Alt plus A", since=mark)
	mark = desktop.mark()
	desktop.press("Tab")
	desktop.expect("Beta button Alt plus B", since=mark)
	mark = desktop.mark()
	command("dialog")
	desktop.expect("Cancel button Alt plus C", since=mark)
	_waitForQuiet(desktop)
	mark = desktop.mark()
	command("close")
	desktop.expect("First window", "Beta button Alt plus B", since=mark)
	_waitForQuiet(desktop)
	assert desktop.spokenSince(mark) == ["First window", "Beta button Alt plus B"]
	probe.close()
	_closeClean(desktop, nvda)


def test_stoppedApplicationsDoNotStallSpeech(desktop: DesktopSession, tmp_path: Path):
	qt = desktop.startApp(desktop.fixture("qtFixture"), title=QT_TITLE)
	gtk = desktop.startApp(desktop.fixture("gtkFixture"), title=GTK_TITLE)
	assert gtk.stdout.readline() == "ready\n"
	nvda, probe = _startNVDA(desktop, tmp_path)
	desktop.expect(GTK_TITLE, PRESS_ME, since=0, timeout=STARTUP_BOUND)
	_waitForQuiet(desktop)
	try:
		qt.send_signal(signal.SIGSTOP)
		mark = desktop.mark()
		pressed = time.monotonic()
		subprocess.run(["xdotool", "key", "Tab"], check=True)
		record = _firstRecordAfter(nvda, mark, timeout=5)
		assert record["t"] - pressed < 0.3, (record, nvda.describe())
		_waitForQuiet(desktop)
		qt.send_signal(signal.SIGCONT)
		gtk.send_signal(signal.SIGSTOP)
		mark = desktop.mark()
		asked = time.monotonic()
		probe.gesture("NVDA+t")
		record = _firstRecordAfter(nvda, mark, timeout=5)
		assert record["t"] - asked < 1.0, (record, nvda.describe())
		# Asked again, the application already known not to answer is not waited for.
		_waitForQuiet(desktop)
		mark = desktop.mark()
		asked = time.monotonic()
		probe.gesture("NVDA+t")
		record = _firstRecordAfter(nvda, mark, timeout=5)
		assert record["t"] - asked < 0.3, (record, nvda.describe())
	finally:
		qt.send_signal(signal.SIGCONT)
		gtk.send_signal(signal.SIGCONT)
	probe.close()
	_closeClean(desktop, nvda)
