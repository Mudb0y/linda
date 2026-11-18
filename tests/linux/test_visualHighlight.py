"""Visual Highlight draws NVDA's outlines on the rig's X display, read back pixel by pixel: the focus,
the navigator object and the browse mode cursor in NVDA's colours and line styles, around and never
over what they highlight, with or without a compositing manager; every click passes through them;
the focus and what is spoken are what they are without them; the Vision settings switch them on and
off; and outlines off the screen, or a connection the X server drops, raise nothing.

nix develop . -c tests/linux/rig python3 -m pytest -q tests/linux/test_visualHighlight.py

The outlines' pixels are read from the root window, which Xvfb draws everything on, the compositing
manager's own window included. Where each control is comes from AT-SPI, as Linda asks it.
"""

import itertools
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import desktopSession
import harness
import pytest
import webSession
from desktopSession import DesktopSession
from Xlib import X, display, error

SESSION_CONFIG = harness.HERE / "fixtures" / "sessionConfig" / "nvda.ini"
#: What a configuration gains to start with Visual Highlight on, every highlight with it.
HIGHLIGHT_ON = """[vision]
	[[NVDAHighlighter]]
		enabled = True
		highlightFocus = True
		highlightNavigator = True
		highlightBrowseMode = True
"""
STARTUP_BOUND = 60.0
#: Seconds the outlines have to follow a change: NVDA's highlighter looks every tenth of one.
FOLLOW_BOUND = 3.0
SETTLE = 1.0
#: NVDA's colours, as 0xRRGGBB: the focus, the navigator object, the two when they are one, and the
#: browse mode cursor.
BLUE = 0x0336FF
PINK = 0xFF0266
YELLOW = 0xFFDE03
COLOURS = (BLUE, PINK, YELLOW)
#: How far outside what it highlights each of NVDA's outlines may reach: its margin and half its
#: line's width, with a pixel to spare.
REACH = {BLUE: 9, PINK: 9, YELLOW: 4}
#: The width of each outline's line.
LINE = {BLUE: 5, PINK: 5, YELLOW: 2}
#: The class of the outlines' windows, as the Windows window's.
WINDOW_CLASS = "NVDAHighlighter"

TITLE = "Highlight window"
AT_START = (TITLE, "First button")
#: A window whose pad prints each press of a mouse button on it, with two buttons on the pad far
#: enough from its edges and from each other that their outlines lie on the pad.
FIXTURE = """
import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk


def say(text):
	print(text, flush=True)


win = Gtk.Window(title="Highlight window")
win.set_default_size(420, 300)
win.connect("destroy", Gtk.main_quit)
pad = Gtk.EventBox()
pad.connect("button-press-event", lambda _w, e: say(f"press {e.button}") or True)
box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=40)
box.set_border_width(40)
pad.add(box)
win.add(pad)
for name in ("First", "Second"):
	button = Gtk.Button(label=name)
	button.connect("clicked", lambda b: say(f"clicked {b.get_label()}"))
	box.pack_start(button, False, False, 0)
win.show_all()


def announce():
	win.present()
	say("ready")
	return False


GLib.timeout_add(200, announce)
Gtk.main()
"""

Rect = tuple[int, int, int, int]


@pytest.fixture
def desktop(tmp_path: Path) -> Iterator[DesktopSession]:
	with DesktopSession(tmp_path / "desktop") as session:
		yield session


@pytest.fixture(params=["composited", "plain"])
def anyDesktop(request, tmp_path: Path) -> Iterator[DesktopSession]:
	"""A desktop under marco compositing, as MATE has it by default, and one under marco without."""
	if request.param == "composited":
		with DesktopSession(tmp_path / "desktop") as session:
			assert compositorRunning()
			yield session
		return
	if desktopSession.windowManagerIsRunning():
		pytest.skip("the rig's own window manager composites")
	log = (tmp_path / "wm.log").open("w")
	wm = subprocess.Popen(["marco", "--no-composite", "--no-force-fullscreen"], stdout=log, stderr=log)
	try:
		deadline = time.monotonic() + desktopSession.WINDOW_MANAGER_TIMEOUT
		while not desktopSession.windowManagerIsRunning():
			assert wm.poll() is None and time.monotonic() < deadline, "marco did not start"
			time.sleep(0.05)
		assert not compositorRunning()
		with DesktopSession(tmp_path / "desktop") as session:
			yield session
	finally:
		wm.terminate()
		try:
			wm.wait(5)
		except subprocess.TimeoutExpired:
			wm.kill()
			wm.wait()
		log.close()


def compositorRunning() -> bool:
	connection = display.Display()
	try:
		return bool(connection.get_selection_owner(connection.intern_atom("_NET_WM_CM_S0")))
	finally:
		connection.close()


def configWith(directory: Path, base: Path = SESSION_CONFIG, *, highlight: bool = True) -> Path:
	directory.mkdir(parents=True, exist_ok=True)
	config = directory / "nvda.ini"
	text = base.read_text(encoding="utf-8")
	config.write_text(text + HIGHLIGHT_ON if highlight else text, encoding="utf-8")
	return config


class Fixture:
	"""The highlight fixture, and the lines it prints, read as they come."""

	def __init__(self, desktop: DesktopSession, directory: Path) -> None:
		script = directory / "highlightFixture.py"
		script.write_text(FIXTURE, encoding="utf-8")
		self.process = desktop.startApp([sys.executable, str(script)], title=TITLE)
		self.lines: list[str] = []
		threading.Thread(target=self._read, daemon=True).start()
		self.expect("ready", since=0)
		desktopSession.activate(TITLE)

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


def findNode(role: str, name: str):
	"""The first control on the accessibility desktop with this role and name."""
	Atspi = desktopSession._atspi()

	def find(node, depth: int = 0):
		if node.get_role_name() == role and node.get_name() == name:
			return node
		if depth > 30:
			return None
		for index in range(node.get_child_count()):
			child = node.get_child_at_index(index)
			if child is not None and (found := find(child, depth + 1)) is not None:
				return found
		return None

	desktop = Atspi.get_desktop(0)
	desktop.clear_cache()
	for appIndex in range(desktop.get_child_count()):
		app = desktop.get_child_at_index(appIndex)
		if app is not None and (node := find(app)) is not None:
			return node
	raise AssertionError(f"no {role} {name!r} on the accessibility desktop")


def extents(name: str, role: str = "button") -> Rect:
	"""(left, top, right, bottom) on the screen of the control with this role and name."""
	Atspi = desktopSession._atspi()
	rect = Atspi.Component.get_extents(findNode(role, name), Atspi.CoordType.SCREEN)
	return rect.x, rect.y, rect.x + rect.width, rect.y + rect.height


def focusedName() -> str | None:
	"""The name of the fixture's button that says it has the focus."""
	Atspi = desktopSession._atspi()
	for name in ("First", "Second"):
		if findNode("button", name).get_state_set().contains(Atspi.StateType.FOCUSED):
			return name
	return None


class Screen:
	"""The rig's screen, its pixels and windows read from the root window."""

	def __init__(self) -> None:
		self.display = display.Display()
		self.root = self.display.screen().root
		geometry = self.root.get_geometry()
		self.width, self.height = geometry.width, geometry.height

	def close(self) -> None:
		self.display.close()

	def pixels(self, left: int, top: int, right: int, bottom: int) -> dict[tuple[int, int], int]:
		"""Every pixel from (left, top) to (right, bottom) on the screen, as 0xRRGGBB, by (x, y)."""
		left, top = max(left, 0), max(top, 0)
		right, bottom = min(right, self.width), min(bottom, self.height)
		width, height = right - left, bottom - top
		data = self.root.get_image(left, top, width, height, X.ZPixmap, 0xFFFFFFFF).data
		return {
			(left + column, top + row): int.from_bytes(
				data[(row * width + column) * 4 : (row * width + column) * 4 + 3], "little"
			)
			for row in range(height)
			for column in range(width)
		}

	def outlineWindows(self) -> list:
		"""The windows of the outlines' class, mapped or not."""
		found = []
		for window in self.root.query_tree().children:
			try:
				wmClass = window.get_wm_class()
			except error.BadWindow:
				# Gone since the tree was read.
				wmClass = None
			if wmClass and wmClass[1] == WINDOW_CLASS:
				found.append(window)
		return found

	def colourBoxes(self) -> dict[str, tuple[int, int, int, int, int]]:
		"""Where on the screen each highlight colour is: its bounding box, and how many pixels have it."""
		boxes = {}
		for (x, y), colour in self.pixels(0, 0, self.width, self.height).items():
			if colour in COLOURS:
				left, top, right, bottom, count = boxes.get(f"{colour:06x}", (x, y, x, y, 0))
				boxes[f"{colour:06x}"] = (min(left, x), min(top, y), max(right, x), max(bottom, y), count + 1)
		return boxes


@pytest.fixture
def screen() -> Iterator[Screen]:
	s = Screen()
	yield s
	s.close()


def outlineOf(screen: Screen, rect: Rect, colour: int) -> dict[str, int]:
	"""How many pixels of colour lie along each side of rect, no further from it than an outline of
	that colour reaches."""
	left, top, right, bottom = rect
	reach = REACH[colour]
	sides = {"top": 0, "bottom": 0, "left": 0, "right": 0}
	for (x, y), found in screen.pixels(left - reach, top - reach, right + reach, bottom + reach).items():
		if found != colour or (left <= x < right and top <= y < bottom):
			continue
		if y < top:
			sides["top"] += 1
		elif y >= bottom:
			sides["bottom"] += 1
		elif x < left:
			sides["left"] += 1
		else:
			sides["right"] += 1
	return sides


def waitForOutline(screen: Screen, rect: Rect, colour: int, *, dashed: bool = False) -> None:
	"""Wait until rect has an outline of colour, its line as wide as NVDA's on each side and along at
	least half of it, then check that it is solid or dashed."""
	left, top, right, bottom = rect
	width = LINE[colour]
	needed = {
		"top": width * (right - left) // 2,
		"bottom": width * (right - left) // 2,
		"left": width * (bottom - top) // 2,
		"right": width * (bottom - top) // 2,
	}
	deadline = time.monotonic() + FOLLOW_BOUND
	while True:
		sides = outlineOf(screen, rect, colour)
		if all(sides[side] >= needed[side] for side in sides):
			break
		assert time.monotonic() < deadline, (
			f"no outline of {colour:06x} round {rect}: {sides}, where the colours are {screen.colourBoxes()}"
		)
		time.sleep(0.1)
	along = lineAbove(screen, rect, colour)
	share = sum(along) / len(along)
	if dashed:
		gaps = sum(1 for before, after in itertools.pairwise(along) if before and not after)
		assert 0.6 < share < 0.9 and gaps >= 3, (share, gaps)
	else:
		assert share > 0.95, share


def lineAbove(screen: Screen, rect: Rect, colour: int) -> list[bool]:
	"""Whether each pixel along the middle of the outline's line above rect has colour."""
	left, top, right, _bottom = rect
	reach = REACH[colour]
	rows = screen.pixels(left - reach, top - reach, right + reach, top)
	counts = {
		y: sum(1 for (_x, rowY), c in rows.items() if rowY == y and c == colour)
		for y in range(top - reach, top)
	}
	row = max(counts, key=counts.get)
	return [rows[x, row] == colour for x in range(left, right)]


def waitForNoOutline(screen: Screen, rect: Rect, colour: int) -> None:
	deadline = time.monotonic() + FOLLOW_BOUND
	while any((sides := outlineOf(screen, rect, colour)).values()):
		assert time.monotonic() < deadline, f"an outline of {colour:06x} stayed round {rect}: {sides}"
		time.sleep(0.1)


def assertNoOutlineInside(screen: Screen, rect: Rect, colour: int) -> None:
	inside = screen.pixels(*rect)
	assert not [point for point, found in inside.items() if found == colour]


def start(desktop: DesktopSession, config: Path) -> harness.Launch:
	nvda = desktop.startNVDA(config)
	desktop.expect(*AT_START, since=0, timeout=STARTUP_BOUND)
	time.sleep(SETTLE)
	return nvda


def step(desktop: DesktopSession, chord: str, *expected: str, timeout: float = 5) -> int:
	mark = desktop.mark()
	desktop.press(chord)
	desktop.expect(*expected, since=mark, timeout=timeout)
	return mark


def quit(desktop: DesktopSession, nvda: harness.Launch) -> None:
	desktop.close()
	assert nvda.process.returncode == 0, nvda.describe()
	nvda.assertClean(logNames=("nvda.log",))


def click(x: int, y: int) -> None:
	subprocess.run(["xdotool", "mousemove", str(x), str(y), "click", "1"], check=True)


def test_outlinesFollowTheFocusAndTheNavigatorObjectAndLetClicksThrough(
	anyDesktop: DesktopSession, screen: Screen, tmp_path: Path
):
	desktop = anyDesktop
	fixture = Fixture(desktop, tmp_path)
	# Away from the buttons, which the pointer would light up.
	subprocess.run(["xdotool", "mousemove", "0", "0"], check=True)
	time.sleep(SETTLE)
	first, second = extents("First"), extents("Second")
	firstBefore, secondBefore = screen.pixels(*first), screen.pixels(*second)
	# Started with Visual Highlight on in its configuration.
	nvda = start(desktop, configWith(tmp_path / "config"))
	# The focus is the navigator object: one solid blue outline.
	waitForOutline(screen, first, BLUE)
	assert screen.pixels(*first) == firstBefore
	assert not any(outlineOf(screen, second, BLUE).values())

	step(desktop, "Insert+KP_Right", "Second button")
	waitForOutline(screen, second, PINK)
	waitForOutline(screen, first, BLUE, dashed=True)
	assert screen.pixels(*first) == firstBefore
	assert screen.pixels(*second) == secondBefore

	# A pixel of the navigator object's outline, on the pad.
	left, top, _right, _bottom = second
	row = top - 5
	assert screen.pixels(left, row, left + 1, row + 1)[left, row] == PINK
	child = screen.root.translate_coords(screen.root, left, row).child
	assert child and child.id not in {window.id for window in screen.outlineWindows()}
	mark = fixture.mark()
	click(left, row)
	fixture.expect("press 1", since=mark)
	time.sleep(SETTLE)
	assert fixture.lines[mark:] == ["press 1"]
	assert focusedName() == "First"

	for window in screen.outlineWindows():
		assert window.get_attributes().override_redirect
	clients = screen.root.get_full_property(screen.display.intern_atom("_NET_CLIENT_LIST"), X.AnyPropertyType)
	assert not {window.id for window in screen.outlineWindows()} & set(clients.value)
	assert desktopSession.activeTopLevelNames() == [TITLE]
	assert not [name for name in desktopSession.topLevelNames() if "ighlight" in name and name != TITLE]

	step(desktop, "Tab", "Second button")
	waitForOutline(screen, second, BLUE)
	waitForNoOutline(screen, first, BLUE)
	waitForNoOutline(screen, second, PINK)
	assertNoOutlineInside(screen, second, BLUE)
	quit(desktop, nvda)


#: Keys pressed with and without Visual Highlight, and the pause after each.
KEYS = ("Tab", "Tab", "Insert+KP_Right", "Insert+KP_Left", "Insert+Tab", "shift+Tab", "Insert+KP_Up")


def readThrough(tmp_path: Path, *, highlight: bool) -> tuple[list[str], list[str], str | None]:
	"""What Linda says and the fixture prints as KEYS are pressed and the pad is clicked where the
	first button's outline is, and which button has the focus after."""
	with DesktopSession(tmp_path / "desktop") as desktop:
		fixture = Fixture(desktop, tmp_path)
		left, top, _right, _bottom = extents("First")
		nvda = start(desktop, configWith(tmp_path / "config", highlight=highlight))
		mark = desktop.mark()
		for key in KEYS:
			desktop.press(key)
			time.sleep(SETTLE)
		click(left + 10, top - 5)
		time.sleep(SETTLE)
		spoken = desktop.spokenSince(mark)
		focused = focusedName()
		lines = list(fixture.lines)
		quit(desktop, nvda)
	return spoken, lines, focused


def test_focusAndSpeechAreTheSameWithAndWithoutIt(tmp_path: Path):
	without = readThrough(tmp_path / "without", highlight=False)
	withIt = readThrough(tmp_path / "with", highlight=True)
	assert withIt == without
	spoken, lines, focused = withIt
	assert lines == ["ready", "press 1"]
	assert focused == "Second"
	assert len(spoken) >= len(KEYS), spoken


CATEGORIES = [
	f"{name} {index} of 16"
	for index, name in enumerate(
		(
			"General",
			"Speech",
			"Braille",
			"Audio",
			"Privacy and Security",
			"Vision",
			"Keyboard",
			"Mouse",
			"Review Cursor",
			"Object Presentation",
			"Browse Mode",
			"Document Formatting",
			"Document Navigation",
			"Remote Access",
			"NVDA Add-on Store",
			"Advanced",
		),
		1,
	)
]


def openVisionSettings(desktop: DesktopSession) -> None:
	step(desktop, "Insert+n", "Preferences sub Menu P 1 of 9")
	step(desktop, "Return", "Settings... S 1 of 4")
	step(
		desktop,
		"Return",
		"Linda Settings: General (normal configuration) dialog",
		"Categories: list",
		CATEGORIES[0],
	)
	for category in CATEGORIES[1:6]:
		step(desktop, "Down", category)


def test_switchedOnAndOffFromTheVisionSettings(desktop: DesktopSession, screen: Screen, tmp_path: Path):
	Fixture(desktop, tmp_path)
	first = extents("First")
	nvda = start(desktop, configWith(tmp_path / "config", highlight=False))
	assert screen.outlineWindows() == []

	openVisionSettings(desktop)
	step(desktop, "Tab", "Enable Highlighting check box not checked…")
	step(desktop, "space", "checked")
	assert screen.outlineWindows()
	step(desktop, "Return", *AT_START)
	waitForOutline(screen, first, BLUE)

	openVisionSettings(desktop)
	step(desktop, "Tab", "Enable Highlighting check box checked…")
	step(desktop, "space", "not checked")
	step(desktop, "Return", *AT_START)
	deadline = time.monotonic() + FOLLOW_BOUND
	while screen.outlineWindows():
		assert time.monotonic() < deadline, "the outlines' windows stayed"
		time.sleep(0.1)
	assert screen.colourBoxes() == {}
	quit(desktop, nvda)


def test_browseModeCursorIsOutlined(desktop: DesktopSession, screen: Screen, tmp_path: Path):
	webSession.requireBrowseLibraries()
	webSession.startFirefox(desktop, "basic.html")
	webSession.waitForDocument("Basic page")
	nvda = desktop.startNVDA(configWith(tmp_path / "config", webSession.BROWSE_CONFIG))
	nvda.waitForLog("Buffer rendered", STARTUP_BOUND)
	desktop.expect("Basic page", since=0, timeout=STARTUP_BOUND)
	time.sleep(SETTLE)
	step(desktop, "control+Home", "heading level 1 Basic page")
	heading = extents("Basic page", "heading")
	waitForOutline(screen, heading, YELLOW)
	assertNoOutlineInside(screen, heading, YELLOW)
	step(desktop, "h", "main landmark Text heading level 2")
	second = extents("Text", "heading")
	waitForOutline(screen, second, YELLOW)
	waitForNoOutline(screen, heading, YELLOW)
	assertNoOutlineInside(screen, second, YELLOW)
	quit(desktop, nvda)


def test_outlinesOffTheScreenRaiseNothingAndALostConnectionStopsThem(tmp_path: Path):
	done = harness.runChild(
		tmp_path,
		"""
		import time
		from collections import namedtuple

		import languageHandler

		languageHandler.setLanguage("en")
		import NVDAHelper  # noqa: F401
		from colors import RGB
		from linuxPlatform import visualHighlight
		from locationHelper import RectLTRB
		from Xlib import X, display

		Style = namedtuple("Style", "color width style margin")
		pink = Style(RGB(0xFF, 0x02, 0x66), 5, 0, 5)
		blue = Style(RGB(0x03, 0x36, 0xFF), 5, 1, 5)
		yellow = Style(RGB(0xFF, 0xDE, 0x03), 2, 0, 2)
		contents = [
			(blue, RectLTRB(-5000, -5000, -4000, -4000)),
			(pink, RectLTRB(-100, 100, 50, 200)),
			(yellow, RectLTRB(10**9, 10**9, 10**9 + 10, 10**9 + 10)),
			(pink, RectLTRB(-(10**9), -(10**9), 10**9, 10**9)),
			(blue, RectLTRB(600, 500, 600, 500)),
		]
		overlay = visualHighlight.Overlay(lambda: contents, 0.1)
		overlay.start()
		time.sleep(1)
		connection = display.Display()
		root = connection.screen().root

		def windows():
			found = []
			for window in root.query_tree().children:
				wmClass = window.get_wm_class()
				if wmClass and wmClass[1] == visualHighlight.WINDOW_CLASS:
					geometry = window.get_geometry()
					found.append(
						(
							window.id,
							[geometry.x, geometry.y, geometry.width, geometry.height],
							window.get_attributes().map_state == X.IsViewable,
						),
					)
			return found

		shown = windows()
		screen = RectLTRB(0, 0, root.get_geometry().width, root.get_geometry().height)
		expected = [
			list(visualHighlight.outline(rect, s.width, s.style, s.margin, screen).window)
			for s, rect in contents
			if visualHighlight.outline(rect, s.width, s.style, s.margin, screen)
		]
		# A change of the pointer's mapping is told to every client, with no window in it.
		connection.set_pointer_mapping(connection.get_pointer_mapping())
		connection.sync()
		time.sleep(0.3)
		aliveAfterMapping = overlay._thread.is_alive()

		# A window that raises itself whenever something comes directly above it, over the outlines.
		fight = display.Display()
		fightRoot = fight.screen().root
		fightRoot.change_attributes(event_mask=X.SubstructureNotifyMask)
		fighter = fightRoot.create_window(
			0, 50, 200, 200, 0, X.CopyFromParent, X.InputOutput, X.CopyFromParent,
			override_redirect=True,
			background_pixel=fight.screen().white_pixel,
		)
		fighter.map()
		fight.sync()
		fighterRaises = 0
		end = time.monotonic() + 4
		while time.monotonic() < end:
			while fight.pending_events():
				event = fight.next_event()
				if (
					event.type == X.ConfigureNotify
					and getattr(event.above_sibling, "id", event.above_sibling) == fighter.id
				):
					fighter.configure(stack_mode=X.Above)
					fighterRaises += 1
			fight.flush()
			time.sleep(0.01)
		fighter.destroy()
		fight.close()

		connection.create_resource_object("window", shown[0][0]).kill_client()
		connection.sync()
		time.sleep(1)
		alive = overlay._thread.is_alive()
		overlay.refresh()
		overlay.stop()
		result = {
			"shown": sorted(geometry for _id, geometry, mapped in shown if mapped),
			"expected": sorted(expected),
			"afterKill": windows(),
			"alive": alive,
			"aliveAfterMapping": aliveAfterMapping,
			"fighterRaises": fighterRaises,
			"startable": visualHighlight.canStart(),
		}
		""",
		recordLevel=30,
	)
	done.assertClean()
	value = done.value
	assert value["shown"] == value["expected"]
	assert len(value["shown"]) == 3, value
	assert value["afterKill"] == []
	assert not value["alive"]
	assert value["aliveAfterMapping"]
	# Raised over at once, then a quarter of a second later, then each time twice as late.
	assert 3 <= value["fighterRaises"] <= 8, value
	assert value["startable"]
	assert [record for record in done.logRecords if "closed Visual Highlight's connection" in record] == [
		"WARNING: The X server closed Visual Highlight's connection; nothing is highlighted any more"
	]


def test_notStartableWithoutAnXDisplay(tmp_path: Path):
	done = harness.runChild(
		tmp_path,
		"""
		import languageHandler

		languageHandler.setLanguage("en")
		import NVDAHelper  # noqa: F401
		from visionEnhancementProviders.NVDAHighlighter import NVDAHighlighter

		from linuxPlatform import visualHighlight

		overlay = visualHighlight.Overlay(lambda: [], 0.1)
		try:
			overlay.start()
			started = True
		except RuntimeError:
			started = False
		overlay._thread.join(5)
		result = {
			"canStart": NVDAHighlighter.canStart(),
			"started": started,
			"pipe": [overlay._wakeRead, overlay._wakeWrite],
		}
		""",
		env={"DISPLAY": None},
	)
	done.assertClean(ignoring={"logErrors"})
	assert done.value == {"canStart": False, "started": False, "pipe": [-1, -1]}
	assert [record.splitlines()[0] for record in done.logErrors] == [
		"ERROR: Visual Highlight could not connect to the X display"
	]
