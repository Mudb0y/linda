"""The mouse commands act at the pointer on the X display, and their keys reach NVDA alone.

Every test runs once with Insert as the NVDA key and the desktop layout, and once with Caps Lock as
the NVDA key and the laptop layout. Run under the rig:

	nix develop . -c tests/linux/rig python3 -m pytest tests/linux/test_mouse.py
"""

import subprocess
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import desktopSession
import pytest
from desktopSession import DesktopSession
from test_keys import LAYOUTS, configText
from Xlib import display

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
	launch = desktop.nvda
	launch.process.terminate()
	launch.waitForExit(20)
	launch.assertClean(logNames=("nvda.log",))
