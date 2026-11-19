"""A window's title said once as the keyboard switches to it, under marco, in GTK, Qt, Firefox and Chromium.

marco's switcher names the window Alt+Tab chooses while Alt is held, and the window's title is said
again as NVDA's focus enters it; Firefox also has GTK say that a grouping of GTK's own has the focus
as its window is activated. Each would say the title a second time, the first cut off by the second.

LINDA_RIG_WM=marco nix develop . -c tests/linux/rig python3 -m pytest -q tests/linux/test_windowTitles.py

Speech is timed, 10 ms a character, so that what cancels a title before it is finished shows.
"""

import errno
import os
import subprocess
import textwrap
import time
from collections.abc import Iterator
from pathlib import Path

import chromiumSession
import desktopSession
import harness
import pytest
import webSession
from desktopSession import PREFIX_MARK, DesktopSession

#: Seconds from starting NVDA to its being ready; a start under the rig takes about three.
STARTUP_BOUND = 60.0
#: Seconds with nothing new spoken after which NVDA is taken to have finished speaking.
QUIET = 1.0
#: Seconds from Tab, Alt held, to the window chosen being named; about 0.05 s.
SWITCHER_BOUND = 0.3
#: Seconds Alt stays down after Tab, as a person's does: long enough for marco's switcher to name the
#: window chosen.
ALT_HELD = 0.15
#: Switches into each program and back.
SWITCHES = 3
MS_PER_CHAR = 10
TIMED_SPEECH = {"LINDA_TRANSCRIPT_MODE": "timed", "LINDA_TRANSCRIPT_MS_PER_CHAR": str(MS_PER_CHAR)}
GTK_TITLE = "Fixture window"
QT_TITLE = "Qt fixture window"
PRESS_ME = "Press me button Does nothing" + PREFIX_MARK
FIREFOX_TITLE = "Basic page \N{EM DASH} Mozilla Firefox"
CHROMIUM_TITLE = "Basic page - Chromium"
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

	import controlTypes
	import eventHandler
	import globalPluginHandler
	import gi
	import queueHandler
	import wx
	from linuxPlatform.atspi import client, handler

	gi.require_version("Atspi", "2.0")
	from gi.repository import Atspi

	ROOT_PATH = "/org/a11y/atspi/accessible/root"


	class GlobalPlugin(globalPluginHandler.GlobalPlugin):
		def __init__(self):
			super().__init__()
			self.ghost = None
			self.label = None
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

		def event_nameChange(self, obj, nextHandler):
			if obj.appModule.appName == "marco" and obj.role == controlTypes.Role.STATUSBAR:
				self.label = obj
			nextHandler()

		def replay(self, what):
			if what == "ghost" and self.ghost is not None:
				handler._onEvent("object:state-changed:focused", self.ghost, 1, 0, None)
			elif what == "switcher" and self.label is not None:
				queueHandler.queueFunction(queueHandler.eventQueue, eventHandler.queueEvent, "nameChange", self.label)

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
		env={**TIMED_SPEECH, "LINDA_TEST_HOOKS_DIR": str(hooks), "LINDA_TITLES_PROBE": str(fifo)},
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


def altTab(tabs: int = 1, held: float = ALT_HELD) -> float:
	"""Hold Alt, press Tab tabs times, and let go of Alt held seconds after the last; return when the
	first Tab was pressed."""
	subprocess.run(["xdotool", "keydown", "alt"], check=True)
	try:
		pressed = 0.0
		for _ in range(tabs):
			time.sleep(0.05)
			pressed = pressed or time.monotonic()
			subprocess.run(["xdotool", "key", "Tab"], check=True)
		time.sleep(held)
	finally:
		subprocess.run(["xdotool", "keyup", "alt"], check=True)
	return pressed


def records(nvda: harness.Launch) -> list[dict]:
	"""Every record of the transcript, cancels among them, in order."""
	path = nvda.directory / "transcript.jsonl"
	return harness.transcriptRecords().readRecords(path) if path.exists() else []


def assertSaidOnceWhole(nvda: harness.Launch, since: int, title: str, focus: str, pressed: float) -> None:
	"""Since the speak record since, title was said once, first, soon after Tab, and to its end before
	anything cancelled it; focus came after it."""
	spokenText = harness.transcriptRecords().spokenText
	allRecords = records(nvda)
	speaks = [i for i, r in enumerate(allRecords) if r["kind"] == "speak"][since:]
	texts = [spokenText(allRecords[i]) for i in speaks]
	assert texts.count(title) == 1, texts
	assert texts[0] == title, texts
	assert desktopSession._inOrder([title, focus], texts), texts
	at = allRecords[speaks[0]]["t"]
	assert at - pressed < SWITCHER_BOUND, (at - pressed, texts)
	nextSpeak = speaks[1] if len(speaks) > 1 else len(allRecords)
	cancels = [r["t"] for r in allRecords[speaks[0] + 1 : nextSpeak] if r["kind"] == "cancel"]
	if cancels:
		assert cancels[0] - at >= len(title) * MS_PER_CHAR / 1000, (
			f"{title!r} was cut off {cancels[0] - at:.3f} s after it began",
			texts,
		)


def switchTo(desktop: DesktopSession, nvda: harness.Launch, title: str, focus: str) -> None:
	waitForQuiet(desktop)
	mark = desktop.mark()
	pressed = altTab()
	desktop.expect(title, focus, since=mark)
	waitForQuiet(desktop)
	assertSaidOnceWhole(nvda, mark, title, focus, pressed)


def quitClean(desktop: DesktopSession, nvda: harness.Launch, probe: Probe) -> None:
	probe.close()
	desktop.close()
	assert nvda.process.returncode == 0, nvda.describe()
	nvda.assertClean(logNames=("nvda.log",))


def test_altTabSaysTheTitleOfAGtkAndAQtWindowOnce(desktop: DesktopSession, tmp_path: Path):
	desktop.startApp(desktop.fixture("qtFixture"), title=QT_TITLE)
	gtk = desktop.startApp(desktop.fixture("gtkFixture"), title=GTK_TITLE)
	assert gtk.stdout.readline() == "ready\n"
	desktopSession.activate(GTK_TITLE)
	nvda, probe = startNVDA(desktop, tmp_path)
	desktop.expect(GTK_TITLE, PRESS_ME, since=0, timeout=STARTUP_BOUND)
	for _ in range(SWITCHES):
		switchTo(desktop, nvda, QT_TITLE, PRESS_ME)
		switchTo(desktop, nvda, GTK_TITLE, PRESS_ME)
	quitClean(desktop, nvda, probe)


def test_altTabSaysTheTitleOfFirefoxOnce(desktop: DesktopSession, tmp_path: Path):
	webSession.requireBrowseLibraries()
	gtk = desktop.startApp(desktop.fixture("gtkFixture"), title=GTK_TITLE)
	assert gtk.stdout.readline() == "ready\n"
	nvda, probe = startNVDA(desktop, tmp_path)
	webSession.startFirefox(desktop, "basic.html")
	desktop.expect(PAGE, since=0, timeout=STARTUP_BOUND)
	for _ in range(SWITCHES):
		switchTo(desktop, nvda, GTK_TITLE, PRESS_ME)
		switchTo(desktop, nvda, FIREFOX_TITLE, PAGE)

	# The title a page gives the window as a link is followed is said as before.
	mark = desktop.mark()
	desktop.press("control+Home")
	desktop.expect("heading level 1 Basic page", since=mark)
	mark = desktop.mark()
	desktop.press("k", "k")
	desktop.expect("Pages navigation landmark list with 2 items The second page link", since=mark)
	mark = desktop.mark()
	desktop.press("Return")
	desktop.expect("Second page document", "heading level 1 Second page", since=mark, timeout=10)
	quitClean(desktop, nvda, probe)


def test_altTabSaysTheTitleOfChromiumOnce(desktop: DesktopSession, tmp_path: Path):
	webSession.requireBrowseLibraries()
	gtk = desktop.startApp(desktop.fixture("gtkFixture"), title=GTK_TITLE)
	assert gtk.stdout.readline() == "ready\n"
	chromiumSession.clearAccessibilityStatus()
	nvda, probe = startNVDA(desktop, tmp_path)
	chromiumSession.startChromium(desktop, "basic.html")
	desktop.expect(PAGE, since=0, timeout=STARTUP_BOUND)
	for _ in range(SWITCHES):
		switchTo(desktop, nvda, GTK_TITLE, PRESS_ME)
		switchTo(desktop, nvda, CHROMIUM_TITLE, PAGE)
	quitClean(desktop, nvda, probe)


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
	switchTo(desktop, nvda, GTK_TITLE, PRESS_ME)
	switchTo(desktop, nvda, FIREFOX_TITLE, PAGE)
	mark = desktop.mark()
	probe.replay("ghost")
	time.sleep(1)
	assert desktop.spokenSince(mark) == []
	assert "in a tree beside its application's" in nvda.text("nvda.log")
	desktop.press("Insert+Tab")
	desktop.expect(PAGE + PREFIX_MARK, since=mark)
	quitClean(desktop, nvda, probe)


def test_switcherHeardLateSaysNothingAgain(desktop: DesktopSession, tmp_path: Path):
	"""A busy marco's naming of the window chosen can be heard after the window is in front and its focus
	is being said: the window is not named again, nor what is being said cut off."""
	desktop.startApp(desktop.fixture("qtFixture"), title=QT_TITLE)
	gtk = desktop.startApp(desktop.fixture("gtkFixture"), title=GTK_TITLE)
	assert gtk.stdout.readline() == "ready\n"
	desktopSession.activate(GTK_TITLE)
	nvda, probe = startNVDA(desktop, tmp_path)
	desktop.expect(GTK_TITLE, PRESS_ME, since=0, timeout=STARTUP_BOUND)
	waitForQuiet(desktop)
	mark = desktop.mark()
	altTab()
	desktop.expect(QT_TITLE, PRESS_ME, since=mark)
	mark = desktop.mark()
	before = len(records(nvda))
	probe.replay("switcher")
	time.sleep(1)
	assert desktop.spokenSince(mark) == []
	assert [r for r in records(nvda)[before:] if r["kind"] == "cancel"] == []
	assert "marco's switcher named 'Qt fixture window' after it was gone" in nvda.text("nvda.log")
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
		switchTo(desktop, nvda, SECOND, GAMMA)
		switchTo(desktop, nvda, FIRST, ALPHA)
	waitForQuiet(desktop)
	mark = desktop.mark()
	command("second")
	desktopSession.activate(SECOND)
	desktop.expect(SECOND, GAMMA, since=mark)
	waitForQuiet(desktop)
	assert desktop.spokenSince(mark).count(SECOND) == 1, desktop.spokenSince(mark)
	switchTo(desktop, nvda, FIRST, ALPHA)
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
	desktop.startApp(desktop.fixture("qtFixture"), title=QT_TITLE)
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
