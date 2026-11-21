"""Linda on the Wayland route under the rig's compositors, with every key through a stand-in for
linda-keyd that puts what Linda passes out on the session's own keyboard, as the helper's virtual
keyboard would: it starts and speaks with the kernel route named in its log and no X display, its
commands and kept keys work with Insert and with Caps Lock, its typing echo follows the pl layout, and
its menu opens from the Linda key with N. Every test types the marker and holds Linda's log at debug
level to not having it.

Under Mutter and sway, each with and without Xwayland, through tests/linux/wayland-gate SCRATCH, or one
of them:

	LINDA_RIG_COMPOSITOR=sway nix develop . -c tests/linux/rig python3 -m pytest -q tests/linux/test_waylandKeys.py
"""

import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import desktopSession
import harness
import keydStandIn
import pytest
import waylandKeyboard
from desktopSession import PREFIX_MARK, DesktopSession
from keydStandIn import StandIn
from test_waylandMenu import KeyProbe, _holdInOrder

pytestmark = pytest.mark.rigSession("wayland")

SESSION_CONFIG = harness.HERE / "fixtures" / "sessionConfig" / "nvda.ini"
#: Seconds from starting Linda to its first words; a start under the rig takes about five.
STARTUP_BOUND = 60.0
#: Seconds Linda has to say what a key asked for.
STEP_BOUND = 5.0
#: Seconds to wait for anything more after what a key was expected to do.
SETTLE = 0.5
#: Seconds between two presses of the Linda key that make a double press, and more than NVDA's
#: multiple press timeout, so that the next press is a first one again.
DOUBLE_GAP = 0.15
APART = 0.7
#: Typed into a program in every test, and never to be found in Linda's log.
MARKER = "kqzmxjwv"
#: What the kernel source's route logs once the helper holds the keyboard for it.
KERNEL_ROUTE = "Keys come from the kernel through linda-keyd: linda-keyd admitted NVDA and holds the keyboard"
#: The rescue hint, said at the first capture of a run.
HINT = "If typing stops reaching programs, hold Backspace, Escape and Enter together."
#: Of NVDA's menu window: its title, its tree, and its first item, as the tree speaks them.
MENU = ("Linda dialog", "tree view", "Preferences collapsed 1 of 9")


def lindaSays(text: str) -> str:
	return text.replace("NVDA", "Linda")


def processTree(root: int) -> set[int]:
	"""root and every process descended from it that is still there."""
	parents: dict[int, int] = {}
	for entry in os.scandir("/proc"):
		if not entry.name.isdecimal():
			continue
		try:
			with open(f"/proc/{entry.name}/stat", "rb") as stat:
				parents[int(entry.name)] = int(stat.read().rsplit(b")", 1)[1].split()[1])
		except (OSError, ValueError, IndexError):
			continue
	tree = {root}
	grew = True
	while grew:
		grew = False
		for pid, parent in parents.items():
			if parent in tree and pid not in tree:
				tree.add(pid)
				grew = True
	return tree


def xClients() -> set[int]:
	"""The process of every client of the compositor's Xwayland, as Xwayland itself counts them, this
	query's own among them, so that an empty answer cannot pass for none."""
	from Xlib import display
	from Xlib.ext import res

	connection = display.Display()
	try:
		ids = connection.res_query_client_ids([{"client": 0, "mask": res.LocalClientPIDMask}])
		clients = {value.value[0] for value in ids.ids if value.spec.mask == res.LocalClientPIDMask}
	finally:
		connection.close()
	assert os.getpid() in clients, clients
	return clients - {os.getpid()}


def xwaylandProcesses() -> list[int]:
	found = []
	for entry in os.scandir("/proc"):
		try:
			if entry.name.isdecimal() and Path(entry.path, "comm").read_text().strip() == "Xwayland":
				found.append(int(entry.name))
		except OSError:
			continue
	return found


def assertKernelRouteAlone(log: str) -> None:
	"""Linda's log names the kernel route and nothing of an X display."""
	assert KERNEL_ROUTE in log, log[-6000:]
	named = [line for line in log.splitlines() if "X display" in line]
	assert not named, named


@dataclass
class WaylandLinda:
	"""The rig's compositor with the key probe, a stand-in for linda-keyd on the session's keyboard, and
	Linda once started."""

	desktop: DesktopSession
	keyboard: StandIn
	sink: object
	probe: KeyProbe | None
	directory: Path
	nvda: harness.Launch | None = None

	def start(
		self,
		config: Path = SESSION_CONFIG,
		*,
		plugins: dict[str, str] | None = None,
		env: dict[str, str | None] | None = None,
	) -> harness.Launch:
		"""Start Linda with the stand-in as its helper and the transcript synth, and wait for its words of
		the probe and the hint that it has the keyboard."""
		hooks = self.directory / f"hooks{len(self.desktop._launches)}"
		(hooks / "globalPlugins").mkdir(parents=True)
		(hooks / "synthDrivers").symlink_to(harness.HERE / "synthDrivers")
		for name, text in (plugins or {}).items():
			(hooks / "globalPlugins" / f"{name}.py").write_text(text, encoding="utf-8")
		self.nvda = self.desktop.startNVDA(
			config,
			env={
				"LINDA_TEST_HOOKS_DIR": str(hooks),
				"LINDA_TEST_KEYD_SOCKET": self.keyboard.path,
				# Read by the desktop's layout feed only where no compositor's own feed answers: rig Mutter
				# has no system bus for localed, and sway's feed reads sway.
				"XKB_DEFAULT_LAYOUT": "pl",
				**(env or {}),
			},
		)
		self.desktop.expect(lindaSays(HINT), since=0, timeout=STARTUP_BOUND)
		self.keyboard.waitFor(lambda: self.keyboard.capturing, STEP_BOUND)
		return self.nvda

	def step(self, chord: str, *expected: str, timeout: float = STEP_BOUND) -> int:
		mark = self.desktop.mark()
		self.desktop.press(chord)
		self.desktop.expect(*expected, since=mark, timeout=timeout)
		time.sleep(SETTLE)
		return mark

	def stepHolding(self, chord: str, *fragments: str, timeout: float = STEP_BOUND) -> None:
		"""step, for words that are each somewhere in a record, as a tree item says its level beside its
		name."""
		mark = self.desktop.mark()
		self.desktop.press(chord)
		deadline = time.monotonic() + timeout
		while not _holdInOrder(fragments, self.desktop.spokenSince(mark)):
			assert time.monotonic() < deadline, (
				f"Linda did not say {list(fragments)} in order; it spoke {self.desktop.spokenSince(mark)}"
			)
			time.sleep(0.05)
		time.sleep(SETTLE)

	def passedMark(self) -> int:
		return len(self.sink.passed())

	def pressesPassed(self, since: int) -> list[int]:
		"""The evdev codes of the presses Linda passed on to the session since passedMark gave since."""
		return [evdev for evdev, down in self.sink.passed()[since:] if down]

	def answers(self, evdev: int, since: float) -> list[dict]:
		"""What became of each press of evdev the stand-in told Linda of after since: the verdicts Linda
		gave, whether its bound ran out first, and how long a WAIT held it."""
		keyboard = self.keyboard
		events = list(keyboard.events)
		return [
			{
				"seq": seq,
				"t": round(event[0] - since, 3),
				"given": keyboard.given.get(seq),
				"expired": any(e[1] == "expired" and e[2] == seq for e in events),
				"waited": [round(held, 3) for waited, held in keyboard.waits if waited == seq],
			}
			for event in events
			if event[1] == "key" and event[3] == evdev and event[4] == 1 and event[0] >= since
			for seq in (event[2],)
		]

	def probeLines(self) -> int:
		return len(self.probe.lines)

	def given(self, since: int) -> list[str]:
		"""The keys the probe was given since, by its own lines."""
		return [line for line in self.probe.lines[since:] if line.startswith(("pressed ", "released "))]

	def typeMarker(self) -> None:
		"""Type the marker into the probe through Linda and wait until the probe holds it."""
		since = self.probeLines()
		self.desktop.typeText(MARKER)
		self.probe.waitFor(lambda line: line.startswith("text ") and line.endswith(MARKER), STEP_BOUND, since)

	def quit(self) -> None:
		"""Stop Linda as the session's close would, and hold it to a clean exit and log, with the kernel
		route alone in it and nothing typed."""
		nvda = self.nvda
		assert nvda is not None
		nvda.process.terminate()
		nvda.waitForExit(20)
		nvda.assertClean(logNames=("nvda.log",))
		log = nvda.text("nvda.log")
		assertKernelRouteAlone(log)
		harness.assertMarkerAbsent(MARKER, {"nvda.log": log})


@pytest.fixture
def socketDir() -> Iterator[Path]:
	# Under the rig's own /tmp: a path under pytest's directory is too long for a socket.
	directory = Path(tempfile.mkdtemp(prefix="keyd", dir="/tmp"))
	try:
		yield directory
	finally:
		shutil.rmtree(directory, ignore_errors=True)


@contextlib.contextmanager
def waylandLinda(directory: Path, socketDir: Path, *, probe: bool = True) -> Iterator[WaylandLinda]:
	"""The stand-in, the desktop session pressing keys on it, and the key probe unless probe is False;
	Linda is started by the test."""
	harness.requireRig()
	sink = keydStandIn.compositorSink()
	with (
		StandIn(socketDir / "socket", sink=sink) as keyboard,
		DesktopSession(directory / "desktop", standIn=keyboard) as desktop,
	):
		keyProbe = KeyProbe(directory) if probe else None
		try:
			yield WaylandLinda(desktop, keyboard, sink, keyProbe, directory)
		finally:
			desktop.close()
			if keyProbe is not None:
				keyProbe.stop()


@pytest.fixture
def linda(tmp_path: Path, socketDir: Path) -> Iterator[WaylandLinda]:
	with waylandLinda(tmp_path, socketDir) as session:
		yield session


def test_startsAndSpeaksOnTheKernelRouteAlone(linda: WaylandLinda):
	"""Linda starts, speaks the probe, and has the keyboard from the helper; its log names the kernel route
	and has no ERROR record. With Xwayland, Linda starts none (experiment E8): sway starts it on demand,
	and none runs once Linda has spoken; the rig's Mutter has it running from the first, and no process
	of Linda's is among its clients."""
	before = xwaylandProcesses() if harness.rigHasXwayland() else []
	nvda = linda.start()
	linda.desktop.expect("Key probe" + PREFIX_MARK, since=0, timeout=STARTUP_BOUND)
	time.sleep(SETTLE)
	spoken = linda.desktop.spokenSince(0)
	assert spoken.count(lindaSays(HINT)) == 1, spoken
	if harness.rigHasXwayland():
		# Before asking Xwayland anything, which would start it.
		assert xwaylandProcesses() == before
		if harness.rigSession() == "sway":
			assert before == []
		lindas = processTree(nvda.process.pid)
		clients = xClients()
		assert not lindas & clients, (lindas, clients)
	linda.typeMarker()
	linda.quit()


def test_commandsAndKeptKeysWithEitherLindaKey(linda: WaylandLinda):
	"""Insert and Caps Lock are each the Linda key: a chord with either speaks the title and neither of its
	keys reaches the probe; either pressed twice reaches it as itself, and Caps Lock twice turns it on and
	then off."""
	linda.start()
	for key in ("Insert", "Caps_Lock"):
		since = linda.probeLines()
		passed = len(linda.sink.passed())
		linda.step(f"{key}+t", "Key probe" + PREFIX_MARK)
		assert linda.given(since) == []
		assert linda.sink.passed()[passed:] == []
		assert not desktopSession.capsLockIsOn()
		time.sleep(APART)
	since = linda.probeLines()
	linda.desktop.press("Insert", gap=DOUBLE_GAP)
	linda.desktop.press("Insert", gap=SETTLE)
	linda.probe.waitFor(lambda line: line == "pressed Insert", STEP_BOUND, since)
	assert linda.given(since) == ["pressed Insert", "released Insert"]
	time.sleep(APART)
	for light, said in ((True, "caps lock on"), (False, "caps lock off")):
		mark = linda.desktop.mark()
		linda.desktop.press("Caps_Lock", gap=DOUBLE_GAP)
		linda.desktop.press("Caps_Lock", gap=SETTLE)
		linda.desktop.expect(said, since=mark)
		assert desktopSession.capsLockIsOn() is light
		time.sleep(APART)
	linda.typeMarker()
	linda.quit()


def test_typingEchoFollowsThePlLayout(linda: WaylandLinda):
	"""Letters typed through Linda reach the probe in the pl layout, AltGr's among them, and each is
	echoed once."""
	linda.start()
	mark = linda.desktop.mark()
	since = linda.probeLines()
	ogonek, stroke = "\N{LATIN SMALL LETTER A WITH OGONEK}", "\N{LATIN SMALL LETTER L WITH STROKE}"
	linda.desktop.typeText(f"z{ogonek}{stroke}")
	linda.probe.waitFor(lambda line: line == f"text z{ogonek}{stroke}", STEP_BOUND, since)
	time.sleep(SETTLE)
	spoken = linda.desktop.spokenSince(mark)
	assert [text for text in spoken if text in ("z", ogonek, stroke)] == ["z", ogonek, stroke], spoken
	assert "pressed ISO_Level3_Shift" in linda.given(since)
	linda.typeMarker()
	linda.quit()


def test_menuFromTheLindaKeyWithN(linda: WaylandLinda):
	"""The Linda key with N opens Linda's menu window from the keyboard, the arrows move in it, and Escape
	gives the probe the keyboard back."""
	linda.start()
	since = linda.probeLines()
	passed = len(linda.sink.passed())
	linda.stepHolding("Insert+n", *MENU)
	assert linda.sink.passed()[passed:] == []
	# The arrows are passed on, and reach Linda's own window, not the probe.
	linda.stepHolding("Down", "Tools collapsed 2 of 9")
	linda.stepHolding("Up", "Preferences collapsed 1 of 9")
	assert linda.given(since) == []
	linda.stepHolding("Escape", "Key probe")
	linda.typeMarker()
	linda.quit()


#: The sway layout feed's snapshot, read in a child with Linda's tree, as Linda's route starts it.
SWAY_FEED_CHILD = r"""
import time
from linuxPlatform.keys import swayLayout

feed = swayLayout.create(None)
changes = []
first = feed.start(changes.append)
time.sleep(0.5)
feed.stop()
last = changes[-1] if changes else first
result = {"names": list(last.names), "group": last.group, "source": last.source}
"""


def compositorKeymap() -> list[str]:
	"""The keymap sway gives a client's keyboard, as tests/linux/waylandKeymapDump.py prints it."""
	dumped = subprocess.run(
		[sys.executable, str(harness.HERE / "waylandKeymapDump.py")],
		capture_output=True,
		text=True,
		timeout=30,
		check=True,
	)
	return dumped.stdout.splitlines()


def feedKeymap(tmp_path: Path) -> tuple[list[str], dict]:
	"""What the sway feed's snapshot compiles to, in the dump's terms, and the snapshot."""
	import waylandKeymapDump
	from xkbcommon import xkb

	done = harness.runChild(tmp_path, SWAY_FEED_CHILD, timeout=60)
	done.assertClean()
	snapshot = done.value
	rules, model, layout, variant, options = snapshot["names"]
	keymap = xkb.Context().keymap_new_from_names(
		rules=rules, model=model, layout=layout, variant=variant, options=options or None
	)
	codes = waylandKeymapDump.namedCodes(waylandKeymapDump.KEY_MAP)
	return waylandKeymapDump.describe(keymap.get_as_string(), codes), snapshot


def swayActiveLayout() -> int:
	"""sway's active layout index of the newest keyboard, the one the feed follows once there is no
	keyboard of linda-keyd's."""
	inputs = json.loads(
		subprocess.run(
			["swaymsg", "-t", "get_inputs"], capture_output=True, text=True, timeout=5, check=True
		).stdout
	)
	return next(device for device in inputs if device.get("type") == "keyboard")["xkb_active_layout_index"]


class SecondKeyboard:
	"""A virtual keyboard of the test's own, with two layouts, pl and us; the rig's keyboard has pl alone."""

	def __init__(self) -> None:
		from pywayland.protocol.wayland import WlSeat
		from xkbcommon import xkb

		protocol = waylandKeyboard.loadProtocol(
			os.environ["LINDA_VIRTUAL_KEYBOARD_PROTOCOL"], "virtual_keyboard_unstable_v1"
		)
		self.display, registry, found = waylandKeyboard.connectWayland()
		manager = protocol.ZwpVirtualKeyboardManagerV1
		seatName, seatVersion = found["wl_seat"]
		seat = registry.bind(seatName, WlSeat, min(seatVersion, 7))
		self.keyboard = registry.bind(found[manager.name][0], manager, 1).create_virtual_keyboard(seat)
		text = xkb.Context().keymap_new_from_names(layout="pl,us").get_as_string()
		fd = waylandKeyboard._keymapFile(text)
		self.keyboard.keymap(waylandKeyboard.WL_KEYMAP_FORMAT_XKB_V1, fd, len(text.encode()) + 1)
		os.close(fd)
		self.keyboard.modifiers(0, 0, 0, 0)
		self.display.roundtrip()

	def tap(self, evdev: int) -> None:
		"""A key down and up, after which the seat's keymap is this keyboard's."""
		for state in (waylandKeyboard.PRESSED, waylandKeyboard.RELEASED):
			self.keyboard.key(int(time.monotonic() * 1000) & 0xFFFFFFFF, evdev, state)
			self.display.roundtrip()

	def close(self) -> None:
		self.keyboard.destroy()
		self.display.roundtrip()
		self.display.disconnect()


@pytest.mark.rigSession("sway")
def test_swaysKeymapIsTheFeedsForPlAndAfterASwitch(tmp_path: Path):
	"""Experiment E5, sway's half: the keysyms sway gives programs for every code keyMap.py names are
	those the layout feed's snapshot compiles to, for the rig's pl keyboard, and for a keyboard with pl
	and us after sway switched it to us."""
	harness.requireRig()
	dumped = compositorKeymap()
	fed, snapshot = feedKeymap(tmp_path)
	assert snapshot["source"] == "sway" and snapshot["names"][2] == "pl", snapshot
	assert fed == dumped
	second = SecondKeyboard()
	try:
		leftShift = 42
		second.tap(leftShift)
		subprocess.run(
			["swaymsg", "input 0:0:wlr_virtual_keyboard_v1 xkb_switch_layout 1"],
			capture_output=True,
			timeout=5,
			check=True,
		)
		second.tap(leftShift)
		time.sleep(SETTLE)
		dumped = compositorKeymap()
		fed, snapshot = feedKeymap(tmp_path)
		print(f"{harness.rigSession()}: E5 after the switch: feed {snapshot}, sway's layouts {dumped[0]!r}")
		assert snapshot["names"][2:4] == ["pl,us", ","], snapshot
		assert snapshot["group"] == swayActiveLayout() == 1
		assert fed == dumped
	finally:
		subprocess.run(
			["swaymsg", "input 0:0:wlr_virtual_keyboard_v1 xkb_switch_layout 0"],
			check=False,
			capture_output=True,
			timeout=5,
		)
		second.close()
		# The rig's keyboard is the seat's again once it types.
		waylandKeyboard.press("Shift_L", gap=0)
