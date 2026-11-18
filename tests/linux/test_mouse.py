"""The mouse commands act at the pointer on the X display, and their keys reach NVDA alone; with mouse
tracking on, NVDA reads what the pointer moves over, and a storm of pointer motion delays no key.

The commands' tests run once with Insert as the NVDA key and the desktop layout, and once with Caps
Lock as the NVDA key and the laptop layout. Run under the rig:

	nix develop . -c tests/linux/rig python3 -m pytest tests/linux/test_mouse.py
"""

import re
import statistics
import subprocess
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import desktopSession
import harness
import pytest
import webSession
from desktopSession import DesktopSession
from test_inputAtRest import OBJECTS, PRELUDE
from test_keys import LAYOUTS, configText
from Xlib import X, display
from Xlib.ext import xtest

STARTUP_TIMEOUT = 60.0
TITLE = "Pointer window"
#: The commands' keys in each layout, as xdotool names them.
KEYS = {
	"desktop": {
		"left": "KP_Divide",
		"right": "KP_Multiply",
		"lockLeft": "shift+KP_Divide",
		"lockRight": "shift+KP_Multiply",
		"toObject": "Insert+KP_Divide",
		"toMouse": "Insert+KP_Multiply",
	},
	"laptop": {
		"left": "Caps_Lock+bracketleft",
		"right": "Caps_Lock+bracketright",
		"lockLeft": "Caps_Lock+control+bracketleft",
		"lockRight": "Caps_Lock+control+bracketright",
		"toObject": "Caps_Lock+shift+m",
		"toMouse": "Caps_Lock+shift+n",
	},
}
#: Keys bound to the scroll commands, which have none by default.
SCROLL_GESTURES = {
	"mouseScrollUp": ("NVDA+alt+shift+u", "Insert+alt+shift+u", "up"),
	"mouseScrollDown": ("NVDA+alt+shift+d", "Insert+alt+shift+d", "down"),
	"mouseScrollLeft": ("NVDA+alt+shift+l", "Insert+alt+shift+l", "left"),
	"mouseScrollRight": ("NVDA+alt+shift+r", "Insert+alt+shift+r", "right"),
}


class Fixture:
	"""The pointer fixture, and the lines it prints, read as they come."""

	def __init__(self, desktop: DesktopSession, *args: str) -> None:
		self.process = desktop.startApp(desktop.fixture("pointerFixture", *args), title=TITLE)
		self.lines: list[str] = []
		self._thread = threading.Thread(target=self._read, daemon=True)
		self._thread.start()
		self.expect("ready", since=0)

	def _read(self) -> None:
		for line in self.process.stdout:
			self.lines.append(line.rstrip("\n"))

	def mark(self) -> int:
		return len(self.lines)

	def expect(self, *lines: str, since: int, timeout: float = 5) -> None:
		deadline = time.monotonic() + timeout
		while True:
			seen = self.lines[since:]
			wanted = iter(seen)
			if all(line in wanted for line in lines):
				return
			assert time.monotonic() < deadline, f"the fixture printed {seen}, not {list(lines)} in order"
			time.sleep(0.05)


def nodeExtents(role: str, name: str) -> tuple[int, int, int, int]:
	"""The screen extents of the fixture's control with this role and name, read through AT-SPI."""
	Atspi = desktopSession._atspi()

	def find(node):
		if node.get_role_name() == role and node.get_name() == name:
			return node
		for index in range(node.get_child_count()):
			child = node.get_child_at_index(index)
			if child is not None and (found := find(child)) is not None:
				return found
		return None

	desktop = Atspi.get_desktop(0)
	for appIndex in range(desktop.get_child_count()):
		app = desktop.get_child_at_index(appIndex)
		for index in range(app.get_child_count() if app else 0):
			window = app.get_child_at_index(index)
			if window is not None and window.get_name() == TITLE and (node := find(window)):
				rect = Atspi.Component.get_extents(node, Atspi.CoordType.SCREEN)
				return rect.x, rect.y, rect.width, rect.height
	raise AssertionError(f"no {role} {name!r} in the fixture")


def centre(extents: tuple[int, int, int, int]) -> tuple[int, int]:
	x, y, width, height = extents
	return x + width // 2, y + height // 2


def pointerNow() -> tuple[int, int, int]:
	"""(x, y, button mask) of the pointer on the rig's display."""
	connection = display.Display()
	try:
		reply = connection.screen().root.query_pointer()
		return reply.root_x, reply.root_y, reply.mask
	finally:
		connection.close()


def movePointer(x: int, y: int) -> None:
	"""Move the pointer as another program does, through XTEST."""
	subprocess.run(["xdotool", "mousemove", str(x), str(y)], check=True)
	time.sleep(0.2)


def startSession(desktop: DesktopSession, tmpPath: Path, layoutName: str, extraConfig: str = "") -> Fixture:
	layout, nvdaKey, _key = LAYOUTS[layoutName]
	configDir = tmpPath / "config"
	configDir.mkdir()
	(configDir / "nvda.ini").write_text(configText(layout, nvdaKey) + extraConfig)
	(configDir / "gestures.ini").write_text(
		"[globalCommands.GlobalCommands]\n"
		+ "".join(f"{script} = kb:{gesture}\n" for script, (gesture, _x, _d) in SCROLL_GESTURES.items()),
	)
	fixture = Fixture(desktop)
	desktop.startNVDA(configDir)
	desktop.expect(TITLE, since=0, timeout=STARTUP_TIMEOUT)
	return fixture


#: Mouse tracking off, so that only the commands speak.
NO_TRACKING = "[mouse]\n\tenableMouseTracking = False\n"


@pytest.fixture
def desktop(tmp_path: Path) -> Iterator[DesktopSession]:
	with DesktopSession(tmp_path / "desktop") as session:
		yield session


@pytest.mark.parametrize("layoutName", LAYOUTS)
def test_clicksAndLocksAtThePointer(desktop: DesktopSession, tmp_path: Path, layoutName: str):
	keys = KEYS[layoutName]
	fixture = startSession(desktop, tmp_path, layoutName, NO_TRACKING)
	movePointer(*centre(nodeExtents("label", "Nothing here")))
	for key, spoken, button in (("left", "Left click", 1), ("right", "Right click", 3)):
		mark, printed = desktop.mark(), fixture.mark()
		desktop.press(keys[key])
		desktop.expect(spoken, since=mark)
		fixture.expect(f"press {button}", f"release {button}", since=printed)
	for key, locked, unlocked, button in (
		("lockLeft", "Left mouse button lock", "Left mouse button unlock", 1),
		("lockRight", "Right mouse button locked", "Right mouse button unlocked", 3),
	):
		mark, printed = desktop.mark(), fixture.mark()
		desktop.press(keys[key])
		desktop.expect(locked, since=mark)
		fixture.expect(f"press {button}", since=printed)
		assert pointerNow()[2] & (1 << (7 + button)), "the button is not held down"
		mark, printed = desktop.mark(), fixture.mark()
		desktop.press(keys[key])
		desktop.expect(unlocked, since=mark)
		fixture.expect(f"release {button}", since=printed)
		assert not pointerNow()[2] & (1 << (7 + button)), "the button is still held down"
	# None of the keys typed in the text field, which had the focus throughout.
	assert not [line for line in fixture.lines if line.startswith("text")]
	launch = desktop.nvda
	launch.process.terminate()
	launch.waitForExit(20)
	launch.assertClean(logNames=("nvda.log",))


@pytest.mark.parametrize("layoutName", LAYOUTS)
def test_pointerToNavigatorAndBack(desktop: DesktopSession, tmp_path: Path, layoutName: str):
	keys = KEYS[layoutName]
	fixture = startSession(desktop, tmp_path, layoutName, NO_TRACKING)
	movePointer(*centre(nodeExtents("label", "Nothing here")))
	mark = desktop.mark()
	desktop.press("Tab")
	desktop.expect("Target\N{HORIZONTAL ELLIPSIS}", since=mark)
	desktop.press(keys["toObject"])
	time.sleep(0.3)
	x, y, _mask = pointerNow()
	left, top, width, height = nodeExtents("button", "Target")
	assert left <= x < left + width and top <= y < top + height, (
		f"the pointer is at {x}, {y}, not on the button at {left}, {top}, {width} by {height}"
	)
	printed = fixture.mark()
	desktop.press(keys["left"])
	fixture.expect("clicked", since=printed)
	# Back over the pad, the navigator goes to what is under the pointer and is read.
	movePointer(*centre(nodeExtents("label", "Nothing here")))
	mark = desktop.mark()
	desktop.press(keys["toMouse"])
	desktop.expect("Move navigator object to mouse", "Nothing here\N{HORIZONTAL ELLIPSIS}", since=mark)
	launch = desktop.nvda
	launch.process.terminate()
	launch.waitForExit(20)
	launch.assertClean(logNames=("nvda.log",))


def test_scrollCommands(desktop: DesktopSession, tmp_path: Path):
	fixture = startSession(desktop, tmp_path, "desktop", NO_TRACKING)
	movePointer(*centre(nodeExtents("label", "Nothing here")))
	for _gesture, chord, direction in SCROLL_GESTURES.values():
		printed = fixture.mark()
		desktop.press(chord)
		fixture.expect(f"scroll {direction}", since=printed)
	assert not [line for line in fixture.lines if line.startswith("text")]
	quitClean(desktop)


def quitClean(desktop: DesktopSession) -> None:
	launch = desktop.nvda
	launch.process.terminate()
	launch.waitForExit(20)
	launch.assertClean(logNames=("nvda.log",))


def trackingConfig(**settings: object) -> str:
	"""The [mouse] section with tracking on, audio coordinates off unless asked, and settings."""
	values = {"enableMouseTracking": True, "audioCoordinatesOnMouseMove": False, **settings}
	return "[mouse]\n" + "".join(f"\t{name} = {value}\n" for name, value in values.items())


def test_trackingReadsUnderThePointer(desktop: DesktopSession, tmp_path: Path):
	startSession(desktop, tmp_path, "desktop", trackingConfig())
	mark = desktop.mark()
	movePointer(*centre(nodeExtents("button", "Target")))
	desktop.expect("Target", since=mark)
	mark = desktop.mark()
	movePointer(*centre(nodeExtents("label", "Nothing here")))
	desktop.expect("Nothing here", since=mark)
	quitClean(desktop)


def test_trackingReportsRoleOnEntering(desktop: DesktopSession, tmp_path: Path):
	startSession(desktop, tmp_path, "desktop", trackingConfig(reportObjectRoleOnMouseEnter=True))
	mark = desktop.mark()
	movePointer(*centre(nodeExtents("button", "Target")))
	# The role as the object is entered, then its text, as on Windows.
	desktop.expect("button", "Target", since=mark)
	quitClean(desktop)


def test_trackingIgnoresOtherPrograms(desktop: DesktopSession, tmp_path: Path):
	# xdotool moves the pointer through XTEST, as a program does; a mouse's moves would still be read.
	startSession(desktop, tmp_path, "desktop", trackingConfig(ignoreInjectedMouseInput=True))
	desktop.expect("edit\N{HORIZONTAL ELLIPSIS}", since=0)
	mark = desktop.mark()
	movePointer(*centre(nodeExtents("button", "Target")))
	movePointer(*centre(nodeExtents("label", "Nothing here")))
	time.sleep(0.5)
	assert desktop.spokenSince(mark) == []
	quitClean(desktop)


#: tones.beep's debug record: pitch, length, left and right volume.
BEEP = re.compile(r"Beep at pitch (\S+), for (\d+) ms, left volume (\d+), right volume (\d+)")


def beeps(launch: harness.Launch) -> list[tuple[float, int, int]]:
	return [(float(m[1]), int(m[3]), int(m[4])) for m in BEEP.finditer(launch.text("nvda.log"))]


def test_audioCoordinates(desktop: DesktopSession, tmp_path: Path):
	startSession(desktop, tmp_path, "desktop", trackingConfig(audioCoordinatesOnMouseMove=True))
	connection = display.Display()
	width = connection.screen().width_in_pixels
	connection.close()
	launch = desktop.nvda
	before = len(beeps(launch))
	movePointer(width // 8, 100)
	movePointer(width - width // 8, 900)
	time.sleep(0.5)
	played = beeps(launch)[before:]
	assert len(played) >= 2, f"NVDA played {played}"
	(highPitch, leftOfLeft, rightOfLeft), (lowPitch, leftOfRight, rightOfRight) = played[-2:]
	# Left to right moves the sound from the left speaker to the right; top to bottom lowers it.
	assert leftOfLeft > rightOfLeft and rightOfRight > leftOfRight
	assert highPitch > lowPitch
	quitClean(desktop)


def test_screenBrightness(tmp_path: Path):
	"""What audio coordinates read the brightness from: the pixel under the pointer, as X has it."""
	code = (
		PRELUDE
		+ OBJECTS
		+ """
import screenBitmap
from linuxPlatform.screenBitmap import ScreenBitmap
from Xlib import display

# The rig's own display: its root painted a known colour where nothing covers it.
connection = display.Display()
root = connection.screen().root
root.change_attributes(background_pixel=0xFF8040)
root.clear_area(0, 0, 0, 0)
connection.sync()
pixel = ScreenBitmap(1, 1).captureImage(0, 0, 1, 1)[0][0]
root.change_attributes(background_pixel=0)
root.clear_area(0, 0, 0, 0)
connection.close()
result = [pixel.rgbRed, pixel.rgbGreen, pixel.rgbBlue, screenBitmap.rgbPixelBrightness(pixel)]
"""
	)
	done = harness.runChild(tmp_path, code)
	assert done.error is None, done.error
	assert done.value == [0xFF, 0x80, 0x40, int(0.3 * 0x40 + 0.59 * 0x80 + 0.11 * 0xFF)]


def test_processAtPoint(desktop: DesktopSession, tmp_path: Path):
	"""The hit test tries first the windows of the process whose window marco's frame holds at the point."""
	fixture = Fixture(desktop)
	x, y = centre(nodeExtents("button", "Target"))
	done = harness.runChild(
		tmp_path,
		PRELUDE
		+ f"from linuxPlatform import pointer\nresult = [pointer.processAt({x}, {y}), pointer.processAt(0, 0)]\n",
	)
	assert done.error is None, done.error
	assert done.value == [fixture.process.pid, None]


class MotionStorm:
	"""The pointer moved back and forth between two points about a thousand times a second, on a
	thread of its own."""

	def __init__(self, first: tuple[int, int], second: tuple[int, int]) -> None:
		self._points = (first, second)
		self._stop = threading.Event()
		self.moves = 0
		self._thread = threading.Thread(target=self._run, daemon=True)
		self._thread.start()

	def _run(self) -> None:
		connection = display.Display()
		try:
			while not self._stop.is_set():
				for x, y in self._points:
					xtest.fake_input(connection, X.MotionNotify, False, x=x, y=y)
					connection.flush()
					self.moves += 1
					time.sleep(0.001)
		finally:
			connection.close()

	def stop(self) -> None:
		self._stop.set()
		self._thread.join(5)


def keyLatency(desktop: DesktopSession, times: int = 6) -> list[float]:
	"""Seconds from pressing NVDA+t to NVDA speaking the window's title, each of times."""
	latencies = []
	for _ in range(times):
		mark = desktop.mark()
		started = time.monotonic()
		subprocess.run(["xdotool", "key", "Insert+t"], check=True)
		while TITLE not in desktop.spokenSince(mark):
			assert time.monotonic() - started < 5, "NVDA did not say the title"
			time.sleep(0.005)
		latencies.append(time.monotonic() - started)
		# Pressed again sooner, NVDA+t spells the title.
		time.sleep(0.7)
	return latencies


def test_motionStormDelaysNoKey(desktop: DesktopSession, tmp_path: Path):
	startSession(desktop, tmp_path, "desktop", trackingConfig())
	button, pad = centre(nodeExtents("button", "Target")), centre(nodeExtents("label", "Nothing here"))
	quiet = keyLatency(desktop)
	storm = MotionStorm(button, pad)
	try:
		time.sleep(0.5)
		stormy = keyLatency(desktop)
	finally:
		storm.stop()
	print(
		f"\nNVDA+t answered in a median {statistics.median(quiet) * 1000:.0f} ms at rest and "
		f"{statistics.median(stormy) * 1000:.0f} ms under {storm.moves} pointer moves",
	)
	assert statistics.median(stormy) < statistics.median(quiet) + 0.1, (quiet, stormy)
	quitClean(desktop)


def timings(launch: harness.Launch) -> list[tuple[float, float]]:
	"""(total, hit test) milliseconds of each move the mouseTiming plugin logged."""
	pattern = re.compile(r"mouse move: ([\d.]+) ms, hit test ([\d.]+) ms")
	return [(float(m[1]), float(m[2])) for m in pattern.finditer(launch.text("nvda.log"))]


def sweep(extents: tuple[int, int, int, int], steps: int = 12) -> None:
	"""Move the pointer over a grid of points in extents, waiting after each for NVDA to follow."""
	left, top, width, height = extents
	for row in range(steps):
		for column in range(steps):
			x = left + width * (2 * column + 1) // (2 * steps)
			y = top + height * (2 * row + 1) // (2 * steps)
			subprocess.run(["xdotool", "mousemove", str(x), str(y)], check=True)
			time.sleep(0.06)


def summary(name: str, measured: list[tuple[float, float]]) -> str:
	totals = sorted(t for t, _h in measured)
	hits = sorted(h for _t, h in measured)
	return (
		f"{name}: {len(measured)} moves, total median {statistics.median(totals):.1f} ms, "
		f"90th {totals[len(totals) * 9 // 10]:.1f} ms, max {totals[-1]:.1f} ms; "
		f"hit test median {statistics.median(hits):.1f} ms, max {hits[-1]:.1f} ms"
	)


def test_trackingCost(desktop: DesktopSession, tmp_path: Path):
	"""What NVDA's main thread spends on each move it follows, over a GTK window and over Firefox."""
	webSession.requireBrowseLibraries()
	config = tmp_path / "config"
	config.mkdir()
	(config / "nvda.ini").write_text(configText("desktop", 6) + trackingConfig())
	Fixture(desktop)
	desktop.startNVDA(config, env={"LINDA_MOUSE_TIMING": "1"})
	desktop.expect(TITLE, since=0, timeout=STARTUP_TIMEOUT)
	launch = desktop.nvda
	before = len(timings(launch))
	sweep(nodeExtents("frame", TITLE))
	gtk = timings(launch)[before:]
	webSession.startFirefox(desktop, "realSayAll.html")
	webSession.waitForDocument("Say all page")
	time.sleep(1)
	before = len(timings(launch))
	sweep((100, 150, 900, 700))
	gecko = timings(launch)[before:]
	print(f"\n{summary('GTK', gtk)}\n{summary('Firefox', gecko)}")
	assert gtk and gecko
	quitClean(desktop)
