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

import desktopSession
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
#: Both fixtures start with their first button focused, and name and describe it alike; how its
#: shortcut is spoken depends on the toolkit.
PRESS_ME = "Press me button Does nothing Alt plus P"
PRESS_ME_ANY = "Press me button Does nothing\N{HORIZONTAL ELLIPSIS}"

#: A global plugin that runs key gestures sent to it through a FIFO as if their keys had been pressed,
#: so that what a command says is timed apart from how its keys are captured.
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


def _windowManagerIsAlive() -> bool:
	"""Whether the window manager the root window names still exists; its properties outlive it."""
	shown = subprocess.run(
		["xprop", "-root", "_NET_SUPPORTING_WM_CHECK"], capture_output=True, text=True, check=False
	).stdout
	if "window id #" not in shown:
		return False
	window = shown.split("#")[-1].strip()
	return subprocess.run(["xprop", "-id", window], capture_output=True, check=False).returncode == 0


@pytest.fixture(scope="module", autouse=True)
def windowManager(tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
	"""One marco for every test here, unless the rig started one: a DesktopSession takes the
	properties a marco it stopped left on the root window for a running one, and starts none. This
	one's are removed once it stops."""
	if _windowManagerIsAlive():
		yield
		return
	log = tmp_path_factory.mktemp("marco") / "marco.log"
	with open(log, "w") as output:
		marco = subprocess.Popen(["marco", "--no-force-fullscreen"], stdout=output, stderr=subprocess.STDOUT)
	try:
		deadline = time.monotonic() + 5
		while not (_windowManagerIsAlive() and desktopSession.windowManagerIsRunning()):
			assert marco.poll() is None and time.monotonic() < deadline, log.read_text()
			time.sleep(0.05)
		yield
	finally:
		marco.terminate()
		try:
			marco.wait(3)
		except subprocess.TimeoutExpired:
			marco.kill()
			marco.wait()
		# So that the next DesktopSession starts a window manager of its own.
		for name in ("_NET_SUPPORTING_WM_CHECK", "_NET_NUMBER_OF_DESKTOPS"):
			subprocess.run(["xprop", "-root", "-remove", name], check=False)


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


def _waitUntilActive(title: str, timeout: float = 10) -> None:
	"""Wait until the top-level window named title is the only one that says it is active, as it does
	once it has the keyboard; a fixture says it is ready before that, and Qt says for a moment that its
	window is active again after losing the keyboard."""
	Atspi = desktopSession._atspi()
	deadline = time.monotonic() + timeout
	while True:
		# marco may not give the keyboard to a window that asks for it with no key press behind it;
		# asked as a pager would ask, it does, once the window is mapped.
		subprocess.run(["wmctrl", "-F", "-a", title], check=False, capture_output=True)
		desktop = Atspi.get_desktop(0)
		active = []
		for appIndex in range(desktop.get_child_count()):
			app = desktop.get_child_at_index(appIndex)
			for index in range(app.get_child_count() if app else 0):
				window = app.get_child_at_index(index)
				if window is not None and window.get_state_set().contains(Atspi.StateType.ACTIVE):
					active.append(window.get_name())
		if active == [title]:
			return
		assert time.monotonic() < deadline, f"{title!r} did not become active within {timeout} s: {active}"
		time.sleep(0.2)


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
	_waitUntilActive(GTK_TITLE)
	nvda, probe = _startNVDA(desktop, tmp_path)
	desktop.expect(GTK_TITLE, PRESS_ME, since=0, timeout=STARTUP_BOUND)
	probe.close()
	_closeClean(desktop, nvda)


def test_altTabSpeaksTitleAndFocusOnce(desktop: DesktopSession, tmp_path: Path):
	desktop.startApp(desktop.fixture("qtFixture"), title=QT_TITLE)
	gtk = desktop.startApp(desktop.fixture("gtkFixture"), title=GTK_TITLE)
	assert gtk.stdout.readline() == "ready\n"
	_waitUntilActive(GTK_TITLE)
	nvda, probe = _startNVDA(desktop, tmp_path)
	desktop.expect(GTK_TITLE, PRESS_ME, since=0, timeout=STARTUP_BOUND)
	_waitForQuiet(desktop)
	for title in (QT_TITLE, GTK_TITLE, QT_TITLE, GTK_TITLE):
		mark = desktop.mark()
		desktop.press("alt+Tab")
		desktop.expect(title, PRESS_ME_ANY, since=mark)
		_waitForQuiet(desktop)
		spoken = desktop.spokenSince(mark)
		assert len(spoken) == 2 and spoken[0] == title, spoken
		assert spoken[1].startswith(PRESS_ME_ANY[:-1]), spoken
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
	_waitUntilActive("First window")
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
	_waitUntilActive(GTK_TITLE)
	nvda, probe = _startNVDA(desktop, tmp_path)
	desktop.expect(GTK_TITLE, PRESS_ME, since=0, timeout=STARTUP_BOUND)
	_waitForQuiet(desktop)
	foreground, background = gtk, qt
	try:
		background.send_signal(signal.SIGSTOP)
		mark = desktop.mark()
		pressed = time.monotonic()
		subprocess.run(["xdotool", "key", "Tab"], check=True)
		record = _firstRecordAfter(nvda, mark, timeout=5)
		assert record["t"] - pressed < 0.3, (record, nvda.describe())
		_waitForQuiet(desktop)
		background.send_signal(signal.SIGCONT)
		foreground.send_signal(signal.SIGSTOP)
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
