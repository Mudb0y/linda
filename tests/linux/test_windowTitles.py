"""Windows coming to the front under marco: Firefox's own GTK grouping is not taken for the focus, each of
a program's windows is named, and a quick switch there and back says only where it ends.

LINDA_RIG_WM=marco nix develop . -c tests/linux/rig python3 -m pytest -q tests/linux/test_windowTitles.py
"""

import errno
import os
import subprocess
import textwrap
import time
from collections.abc import Iterator
from pathlib import Path

import desktopSession
import harness
import pytest
import webSession
from desktopSession import PREFIX_MARK, DesktopSession

#: Seconds from starting NVDA to its being ready; a start under the rig takes about three.
STARTUP_BOUND = 60.0
#: Seconds with nothing new spoken after which NVDA is taken to have finished speaking.
QUIET = 1.0
#: Seconds Alt stays down after Tab, as a person's does: long enough for marco's switcher to name the
#: window chosen.
ALT_HELD = 0.15
#: Switches into each program and back.
SWITCHES = 3
GTK_TITLE = "Fixture window"
QT_TITLE = "Qt fixture window"
PRESS_ME = "Press me button Does nothing" + PREFIX_MARK
FIREFOX_TITLE = "Basic page \N{EM DASH} Mozilla Firefox"
PAGE = "Basic page document"
FIRST = "First window"
SECOND = "Second window"
ALPHA = "Alpha button Alt plus A"
GAMMA = "Gamma check box not checked Alt plus G"

#: A global plugin that replays, for each line written to the FIFO LINDA_TITLES_PROBE names, an event
#: Linda has had: "ghost" the newest focus GTK gave an accessible of its own in a tree beside its
#: application's, "switcher" the newest name marco's window switcher gave its label.
PROBE = textwrap.dedent(
	'''\
	"""Replays events Linda has had, on lines written to the FIFO LINDA_TITLES_PROBE names."""

	import os
	import threading

	import globalPluginHandler
	import gi
	import wx
	from linuxPlatform.atspi import client, handler

	gi.require_version("Atspi", "2.0")
	from gi.repository import Atspi

	ROOT_PATH = "/org/a11y/atspi/accessible/root"


	class GlobalPlugin(globalPluginHandler.GlobalPlugin):
		def __init__(self):
			super().__init__()
			self.ghost = None
			self.listener = Atspi.EventListener.new(self.onFocus)
			self.listener.register("object:state-changed:focused")
			path = os.environ.get("LINDA_TITLES_PROBE")
			if path:
				threading.Thread(target=self.read, args=(path,), name="titlesProbe", daemon=True).start()

		def onFocus(self, event):
			if not event.detail1:
				return
			c = client.client
			try:
				node = c.node(event.source)
				parent = c.parent(node)
				top = c.parent(parent) if parent is not None else None
				if top is not None and top.path != ROOT_PATH and c.role(top) == "application":
					self.ghost = node
			except Exception:
				pass

		def replay(self, what):
			if what == "ghost" and self.ghost is not None:
				handler._onEvent("object:state-changed:focused", self.ghost, 1, 0, None)

		def read(self, path):
			with open(path, encoding="utf-8") as f:
				for line in f:
					wx.CallAfter(self.replay, line.strip())
	''',
)


class Probe:
	"""The FIFO the probe plugin reads from."""

	def __init__(self, path: Path) -> None:
		self.path = path
		self._fd: int | None = None

	def replay(self, what: str, timeout: float = 30) -> None:
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
		os.write(self._fd, f"{what}\n".encode())

	def close(self) -> None:
		if self._fd is not None:
			os.close(self._fd)
			self._fd = None


@pytest.fixture
def desktop(tmp_path: Path) -> Iterator[DesktopSession]:
	with DesktopSession(tmp_path / "desktop") as session:
		yield session


def startNVDA(desktop: DesktopSession, tmp_path: Path) -> tuple[harness.Launch, Probe]:
	hooks = tmp_path / "hooks"
	(hooks / "globalPlugins").mkdir(parents=True)
	(hooks / "globalPlugins" / "titlesProbe.py").write_text(PROBE, encoding="utf-8")
	(hooks / "synthDrivers").symlink_to(harness.HERE / "synthDrivers")
	fifo = tmp_path / "probe.fifo"
	os.mkfifo(fifo)
	config = tmp_path / "nvda.ini"
	# Read from the top by the keys alone, so that a page's window is announced as any other.
	config.write_text(
		webSession.BROWSE_CONFIG.read_text().replace(
			"[virtualBuffers]\n", "[virtualBuffers]\n\tautoSayAllOnPageLoad = False\n", 1
		),
	)
	nvda = desktop.startNVDA(
		config,
		env={"LINDA_TEST_HOOKS_DIR": str(hooks), "LINDA_TITLES_PROBE": str(fifo)},
	)
	nvda.waitForLog("NVDA initialized", STARTUP_BOUND)
	return nvda, Probe(fifo)


def waitForQuiet(desktop: DesktopSession, limit: float = 10) -> None:
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


def altTab(tabs: int = 1, held: float = ALT_HELD) -> None:
	"""Hold Alt, press Tab tabs times, and let go of Alt held seconds after the last."""
	subprocess.run(["xdotool", "keydown", "alt"], check=True)
	try:
		for _ in range(tabs):
			time.sleep(0.05)
			subprocess.run(["xdotool", "key", "Tab"], check=True)
		time.sleep(held)
	finally:
		subprocess.run(["xdotool", "keyup", "alt"], check=True)


def switchTo(desktop: DesktopSession, title: str, focus: str) -> None:
	waitForQuiet(desktop)
	mark = desktop.mark()
	altTab()
	desktop.expect(title, focus, since=mark)
	waitForQuiet(desktop)


def quitClean(desktop: DesktopSession, nvda: harness.Launch, probe: Probe) -> None:
	probe.close()
	desktop.close()
	assert nvda.process.returncode == 0, nvda.describe()
	nvda.assertClean(logNames=("nvda.log",))


def test_firefoxsOwnGtkGroupingIsNotTheFocus(desktop: DesktopSession, tmp_path: Path):
	"""GTK says a grouping of its own has the focus as Firefox's window is activated, in a frame named as
	the window under an application object of GTK's, beside Firefox's own tree; heard after Firefox has
	said the page has it, as it sometimes is, it would move NVDA's focus out of the page, saying the
	title and "grouping", and back, saying the title again."""
	webSession.requireBrowseLibraries()
	gtk = desktop.startApp(desktop.fixture("gtkFixture"), title=GTK_TITLE)
	assert gtk.stdout.readline() == "ready\n"
	nvda, probe = startNVDA(desktop, tmp_path)
	webSession.startFirefox(desktop, "basic.html")
	desktop.expect(PAGE, since=0, timeout=STARTUP_BOUND)
	switchTo(desktop, GTK_TITLE, PRESS_ME)
	switchTo(desktop, FIREFOX_TITLE, PAGE)
	mark = desktop.mark()
	probe.replay("ghost")
	time.sleep(1)
	assert desktop.spokenSince(mark) == []
	assert "in a tree beside its application's" in nvda.text("nvda.log")
	desktop.press("Insert+Tab")
	desktop.expect(PAGE + PREFIX_MARK, since=mark)
	quitClean(desktop, nvda, probe)


def test_twoWindowsOfOneProgram(desktop: DesktopSession, tmp_path: Path):
	"""Each of a program's windows is named as the keyboard goes to it, by Alt+Tab or as the program
	brings it to the front, and a dialog opening in one is read with its text."""
	app = desktop.startApp(desktop.fixture("twoWindows"), title=SECOND)
	assert app.stdout.readline() == "ready\n"

	def command(line: str) -> None:
		app.stdin.write(line + "\n")
		app.stdin.flush()
		assert app.stdout.readline() == f"done {line}\n"

	desktopSession.activate(SECOND)
	nvda, probe = startNVDA(desktop, tmp_path)
	desktop.expect(SECOND, GAMMA, since=0, timeout=STARTUP_BOUND)
	waitForQuiet(desktop)
	mark = desktop.mark()
	command("first")
	desktopSession.activate(FIRST)
	desktop.expect(FIRST, ALPHA, since=mark)
	for _ in range(SWITCHES):
		switchTo(desktop, SECOND, GAMMA)
		switchTo(desktop, FIRST, ALPHA)
	waitForQuiet(desktop)
	mark = desktop.mark()
	command("second")
	desktopSession.activate(SECOND)
	desktop.expect(SECOND, GAMMA, since=mark)
	waitForQuiet(desktop)
	assert desktop.spokenSince(mark).count(SECOND) == 1, desktop.spokenSince(mark)
	switchTo(desktop, FIRST, ALPHA)
	# A key press, which marco needs before it gives a window the program opens the keyboard.
	mark = desktop.mark()
	desktop.press("Tab")
	desktop.expect("Beta button Alt plus B", since=mark)
	waitForQuiet(desktop)
	mark = desktop.mark()
	command("dialog")
	desktop.expect(
		"Question dialog Save changes? Your edits will be lost otherwise.",
		"Cancel button Alt plus C",
		since=mark,
	)
	waitForQuiet(desktop)
	mark = desktop.mark()
	command("close")
	desktop.expect(FIRST, "Beta button Alt plus B", since=mark)
	quitClean(desktop, nvda, probe)


def test_thereAndBackSaysOnlyWhereItEnds(desktop: DesktopSession, tmp_path: Path):
	# A fixture asks for the keyboard as it says it is ready, so the second window is started after that,
	# or the first can take the keyboard back from it.
	qt = desktop.startApp(desktop.fixture("qtFixture"), title=QT_TITLE)
	assert qt.stdout.readline() == "ready\n"
	gtk = desktop.startApp(desktop.fixture("gtkFixture"), title=GTK_TITLE)
	assert gtk.stdout.readline() == "ready\n"
	desktopSession.activate(GTK_TITLE)
	nvda, probe = startNVDA(desktop, tmp_path)
	desktop.expect(GTK_TITLE, PRESS_ME, since=0, timeout=STARTUP_BOUND)
	for there in (0.05, 0.15, 0.3):
		waitForQuiet(desktop)
		mark = desktop.mark()
		altTab()
		time.sleep(there)
		altTab()
		desktop.expect(GTK_TITLE, PRESS_ME, since=mark)
		waitForQuiet(desktop)
		spoken = desktop.spokenSince(mark)
		last = len(spoken) - 1 - spoken[::-1].index(GTK_TITLE)
		assert len(spoken) == last + 2 and spoken[last + 1].startswith(PRESS_ME[:-1]), spoken
		assert desktopSession.activeTopLevelNames() == [GTK_TITLE]
	quitClean(desktop, nvda, probe)
