"""A desktop in the rig for tests that read applications: a window manager, applications started and
waited for, keys typed on the rig's display, and NVDA with what it speaks.

Inside the rig only. On the rig's X display keys go through xdotool and windows are activated with
wmctrl. In the rig's Wayland sessions the compositor is the window manager, keys go through the rig's
virtual keyboard (waylandKeyboard.py) in the pl layout, and windows are activated as each compositor
allows: by swaymsg under sway, and under plain Mutter, which has no way to name a window to it, by its
own window switching keys. A session given a stand-in for linda-keyd presses and types on the stand-in
instead, in either kind of session, as a person types on a keyboard the helper holds. Every process
this starts is one of its own children, which close stops by its process ID.
"""

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Sequence
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING, Self

import harness
import waylandKeyboard

if TYPE_CHECKING:
	from keydStandIn import StandIn

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
#: Seconds Mutter has to move the keyboard after its window switching key, which takes it a few
#: hundredths of one.
SWITCH_TIMEOUT = 2.0
#: Seconds each key is held on the rig's Wayland keyboard or a stand-in, and between characters typed,
#: as typeText's xdotool types on the X display.
KEY_HOLD = 0.03
TYPE_DELAY = 0.04
#: Mutter's key for its switch-applications-backward binding, which activates the window used longest
#: ago at once; with no shell there is no switcher to choose in, and pressed once for each window it
#: brings every window to the front in turn.
MUTTER_SWITCH_BACK = "shift+alt+Tab"
#: evdev codes of the modifiers that pick a key's levels in the pl layout: none, Shift, AltGr, and both.
_SHIFT, _ALTGR = 42, 100
_LEVEL_MODIFIERS = ((), (_SHIFT,), (_ALTGR,), (_SHIFT, _ALTGR))


def compositor() -> str | None:
	"""mutter or sway when the rig gave a Wayland session, None on its X display."""
	session = harness.rigSession()
	return None if session == "x11" else session


def windowManagerIsRunning() -> bool:
	"""Whether a window manager has named itself on the root window, set up its desktops, and is still
	there. Under a compositor, which is the window manager and runs for the rig's whole life, always."""
	if compositor() is not None:
		return True
	shown = subprocess.run(
		["xprop", "-root", "_NET_SUPPORTING_WM_CHECK", "_NET_NUMBER_OF_DESKTOPS"],
		capture_output=True,
		text=True,
		check=False,
	).stdout
	checkWindow = re.search(r"_NET_SUPPORTING_WM_CHECK\(WINDOW\): window id # (0x[0-9a-fA-F]+)", shown)
	if checkWindow is None or "_NET_NUMBER_OF_DESKTOPS(CARDINAL)" not in shown:
		return False
	# marco leaves both properties on the root window when it exits; the window it names goes with it.
	return subprocess.run(["xprop", "-id", checkWindow[1]], capture_output=True, check=False).returncode == 0


def capsLockIsOn() -> bool:
	"""Whether the rig's display has Caps Lock on; under a compositor, whether the rig's virtual
	keyboard has it on, by the XKB state it keeps beside the compositor's."""
	if compositor() is not None:
		return waylandKeyboard.lights()["caps"]
	from Xlib import display

	connection = display.Display()
	try:
		return bool(connection.get_keyboard_control().led_mask & 1)
	finally:
		connection.close()


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


def activeTopLevelNames() -> list[str]:
	"""The names of the top-level windows that say they are active, read afresh."""
	Atspi = _atspi()
	from gi.repository import GLib

	desktop = Atspi.get_desktop(0)
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
				if window is not None and window.get_state_set().contains(Atspi.StateType.ACTIVE):
					names.append(window.get_name() or "")
		except GLib.Error:
			continue
	return names


def activate(title: str, timeout: float = 10) -> None:
	"""Give the keyboard to the top-level window named title, and wait until it is the only window that
	says it is active.

	marco may not give the keyboard to a window that asks for it with no key press behind it, as a
	window a test starts does; asked as a pager asks, it does. Qt also says for a moment that its
	window is active again after losing the keyboard.
	"""
	deadline = time.monotonic() + timeout
	while True:
		askToActivate(title)
		active = activeTopLevelNames()
		if active == [title]:
			return
		assert time.monotonic() < deadline, f"{title!r} did not become active within {timeout} s: {active}"
		time.sleep(0.2)


def askToActivate(title: str) -> None:
	"""Ask the window manager once to give the keyboard to the top-level window named title, and return
	without waiting to see it done, except under Mutter."""
	match compositor():
		case None:
			# -F matches the whole title: without it "Fixture window" would also pick "Qt fixture window".
			subprocess.run(["wmctrl", "-F", "-a", title], check=False, capture_output=True)
		case "sway":
			_swayFocus(title)
		case "mutter":
			_mutterSwitch(title)


def _swayWindows(node: dict):
	"""Every window in a node of swaymsg's tree; a window is a node with a process."""
	if node.get("pid"):
		yield node
	for child in node.get("nodes", []) + node.get("floating_nodes", []):
		yield from _swayWindows(child)


def _swayFocus(title: str) -> None:
	# By the container's number rather than a title criterion, which sway takes as a regular expression.
	tree = json.loads(
		subprocess.run(["swaymsg", "-t", "get_tree"], capture_output=True, text=True, check=True).stdout
	)
	for window in _swayWindows(tree):
		if window.get("name") == title:
			subprocess.run(["swaymsg", f"[con_id={window['id']}] focus"], check=False, capture_output=True)
			return


def _mutterSwitch(title: str) -> None:
	"""One step of Mutter's backward window switching, unless title has the keyboard already, and the
	wait for the keyboard to move: pressed again before AT-SPI says it has, the key may take it on
	past the very window it had just brought forward."""
	before = activeTopLevelNames()
	if before == [title]:
		return
	waylandKeyboard.press(MUTTER_SWITCH_BACK, gap=0)
	deadline = time.monotonic() + SWITCH_TIMEOUT
	while activeTopLevelNames() == before and time.monotonic() < deadline:
		time.sleep(0.05)


@cache
def _layoutKeys() -> tuple[dict[str, tuple[int, ...]], dict[int, tuple[int, ...]]]:
	"""What the pl layout types and which key symbols it gives, each with the evdev codes that make it,
	modifiers first; with Caps Lock off, as DesktopSession leaves it. A key symbol is found on the key
	that gives it with the fewest modifiers, a character on the lowest key that types it, as xdotool
	types one.

	The fewest modifiers would put characters on keys a person does not type them on, which reach
	Linda as other keys: plus and asterisk on the keypad, which NVDA binds to commands of its own, the
	parentheses on the keypad's own keys, the dollar on KEY_DOLLAR. The keypad's key symbols type no
	character at all, for the same reason."""
	from xkbcommon import xkb

	keymap = xkb.Context().keymap_new_from_names(layout=waylandKeyboard.LAYOUT)
	characters: dict[str, tuple[int, ...]] = {}
	keysyms: dict[int, tuple[int, ...]] = {}
	typed: list[tuple[int, str, tuple[int, ...]]] = []
	for modifiers in _LEVEL_MODIFIERS:
		for keycode in range(keymap.min_keycode(), keymap.max_keycode() + 1):
			if keycode - 8 in modifiers:
				continue
			state = keymap.state_new()
			for modifier in modifiers:
				state.update_key(modifier + 8, xkb.KeyDirection.XKB_KEY_DOWN)
			codes = (*modifiers, keycode - 8)
			if (keysym := state.key_get_one_sym(keycode)) == 0:
				continue
			keysyms.setdefault(keysym, codes)
			if xkb.keysym_get_name(keysym).startswith("KP_"):
				continue
			if len(character := state.key_get_string(keycode)) == 1:
				typed.append((keycode, character, codes))
	# A stable sort: on one key, the fewest modifiers still come first.
	for _keycode, character, codes in sorted(typed, key=lambda entry: entry[0]):
		characters.setdefault(character, codes)
	# xdotool types a new line with Return, which gives a carriage return, not with the Linefeed key.
	characters["\n"] = characters["\r"]
	return characters, keysyms


def chordCodes(chord: str) -> list[int]:
	"""The evdev codes of a chord in xdotool's key names, as "alt+Tab" or "shift+question", in the order
	they go down: a name the rig's keyboard has no code for is found in the pl layout by its key symbol,
	with what selects its level, as xdotool finds one."""
	codes: list[int] = []
	for name in chord.split("+"):
		try:
			found: tuple[int, ...] = (waylandKeyboard.code(name),)
		except ValueError:
			from xkbcommon import xkb

			try:
				found = _layoutKeys()[1][xkb.keysym_from_name(name)]
			except (KeyError, xkb.XKBInvalidKeysym):
				raise ValueError(f"no key in the {waylandKeyboard.LAYOUT} layout for {name!r}") from None
		codes.extend(code for code in found if code not in codes)
	return codes


def characterCodes(character: str) -> tuple[int, ...]:
	"""The evdev codes that type character in the pl layout, modifiers first."""
	try:
		return _layoutKeys()[0][character]
	except KeyError:
		raise ValueError(f"the {waylandKeyboard.LAYOUT} layout cannot type {character!r}") from None


def _standInNames(chord: str) -> bool:
	"""Whether the stand-in for linda-keyd has a key by each name in chord."""
	import keydStandIn

	try:
		for name in chord.split("+"):
			keydStandIn.code(name)
	except KeyError:
		return False
	return True


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

	def __init__(
		self, directory: Path, *, windowManager: bool = True, standIn: "StandIn | None" = None
	) -> None:
		"""
		:param directory: Where NVDA's runs and the applications' standard error go.
		:param windowManager: Whether to have marco running, which the rig starts when
			LINDA_RIG_WM=marco is in its caller's environment, and this does otherwise. Under a
			compositor there is no other window manager to have.
		:param standIn: The stand-in for linda-keyd that press and typeText press keys on, which a
			test may also set later as standIn; without one, keys go to the rig's display.
		"""
		harness.requireRig()
		self.directory = directory
		directory.mkdir(parents=True, exist_ok=True)
		self.standIn = standIn
		self._apps: list[subprocess.Popen] = []
		self._launches: list[harness.Launch] = []
		self._windowManager: subprocess.Popen | None = None
		if windowManager and not windowManagerIsRunning():
			self._startWindowManager()
		# The rig's display outlives a test, and a test that failed with Caps Lock on would leave
		# every later one typing capitals.
		if capsLockIsOn():
			if compositor() is None:
				subprocess.run(["xdotool", "key", "Caps_Lock"], check=True)
			else:
				waylandKeyboard.press("Caps_Lock", gap=0)

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
		qtPlugins = os.environ.get("LINDA_QT6_PLUGIN_PATH")
		if qtPlugins and "QT_PLUGIN_PATH" not in env:
			# Without its platform plugin a PyQt6 program aborts before it opens a window.
			env["QT_PLUGIN_PATH"] = qtPlugins
		if compositor() is not None:
			# The Qt fixture asks for xcb unless told otherwise, and a Wayland run must not pass
			# through Xwayland quietly, or fail for want of it.
			env.setdefault("QT_QPA_PLATFORM", "wayland")
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
		"""Press each chord in turn on the rig's display, or on the stand-in when the session has one,
		with gap seconds after each.

		Chords use xdotool's key names: "Tab", "Insert+t", "KP_Up", "alt+Tab". The stand-in presses them
		by its own press, and a chord with a name it does not have, as "ctrl" or "AltGr", key by key on
		the keys the pl layout gives that name.
		"""
		for chord in chords:
			if self.standIn is not None:
				if _standInNames(chord):
					self.standIn.press(chord)
				else:
					self._pressCodes(chordCodes(chord))
			elif compositor() is not None:
				self._pressCodes(chordCodes(chord))
			else:
				subprocess.run(["xdotool", "key", chord], check=True)
			time.sleep(gap)

	def typeText(self, text: str) -> None:
		"""Type text on the rig's display, or on the stand-in when the session has one, one key at a
		time, as a person would. On the stand-in or a compositor's keyboard, each character is typed on
		the keys the pl layout has it on; the test owns the stand-in, and stops it itself."""
		if self.standIn is None and compositor() is None:
			subprocess.run(["xdotool", "type", "--delay", "40", text], check=True)
			return
		for character in text:
			self._pressCodes(characterCodes(character))
			time.sleep(TYPE_DELAY)

	def _pressCodes(self, codes: Sequence[int]) -> None:
		"""Each of codes down in order, then up in the reverse, on the stand-in or the rig's keyboard."""
		standIn = self.standIn
		for code in codes:
			if standIn is not None:
				standIn.down(code)
			else:
				waylandKeyboard.key(code, True)
			time.sleep(KEY_HOLD)
		for code in reversed(codes):
			if standIn is not None:
				standIn.up(code)
			else:
				waylandKeyboard.key(code, False)
			time.sleep(KEY_HOLD)

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
