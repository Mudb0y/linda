"""The group of a keymap of several layouts where neither localed nor the XKB_DEFAULT variables say which
is in use: the localed and environment feeds follow a switch an xkb option makes from the keys NVDA
passes on, through an XKB state of their own fed as the compositor feeds its own.

The feeds alone run in a child. Then the Wayland route, told the session is Wayland while the rig
gives the X display, with the stand-in for linda-keyd putting the keys it passes out on a NullSink
whose XKB state is the compositor's. Then Linda itself: in the X rig, on an X display set to the same
two layouts and option, which switches its group as a compositor would; and in rig sway, on a
virtual keyboard of the test's own, which keeps its XKB state as sway keeps a real keyboard's. Run
under the rig, the last under sway too:

	nix develop . -c tests/linux/rig python3 -m pytest -q tests/linux/test_shadowGroup.py
	LINDA_RIG_COMPOSITOR=sway nix develop . -c tests/linux/rig python3 -m pytest -q tests/linux/test_shadowGroup.py
"""

import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import desktopSession
import harness
import pytest
import waylandKeyboard
from desktopSession import DesktopSession
from keydStandIn import StandIn
from test_deletedCharacter import findField
from test_desktopLayout import noSystemBus, standIn
from test_keys import STARTUP_TIMEOUT, configText
from test_waylandKeys import HINT, SESSION_CONFIG, lindaSays
from test_waylandMenu import KeyProbe
from test_waylandRoute import OWNER_CHILD, OWNER_SESSION, runRouteChild

DEBUG = 10
#: The option every test switches groups with, which must never be logged.
TOGGLE = "grp:alt_shift_toggle"
#: Within which a switch must reach onChange while the main thread is kept busy: a switch that waited
#: for the main thread would take the 0.3 s it is kept busy at a time.
CHANGE_BOUND = 0.1
SWITCHES = 6
#: Every record the shadow may make: layout names and where they came from.
ALLOWED_RECORDS = re.compile(r"Keyboard layout from (localed|environment): [\w-]+( [\w-]+)?")

#: The feeds in a child, with xkb key codes of the keys the tests pass on.
PRELUDE = r"""
import os, threading, time
from logHandler import log
log.setLevel(log.DEBUG)
from linuxPlatform.keys import desktopLayout, shadowGroup
from linuxPlatform.keys.desktopLayout import LayoutSnapshot

SHIFT, ALT, ALT_GR, A = 50, 64, 108, 38
changes = []


def onChange(snapshot):
	changes.append([time.monotonic(), threading.current_thread().name, snapshot.group, list(snapshot.names), snapshot.source])


def chord(feed, *keycodes):
	for keycode in keycodes:
		feed.notePassed(keycode, False)
	for keycode in reversed(keycodes):
		feed.notePassed(keycode, True)


def toggle(feed):
	chord(feed, SHIFT, ALT)


def settle(count, timeout=2.0):
	deadline = time.monotonic() + timeout
	while len(changes) < count and time.monotonic() < deadline:
		time.sleep(0.005)
	time.sleep(0.05)


def groups():
	return [change[2] for change in changes]


def busy(seconds):
	end = time.monotonic() + seconds
	count = 0
	while time.monotonic() < end:
		count += 1


def feedThreads():
	return [t.name for t in threading.enumerate() if t.name == "lindaKeyboardLayout"]


def names(layout, options=""):
	return LayoutSnapshot(("evdev", "pc105", layout, "", options), source="environment")
"""


def runFeeds(tmp_path: Path, code: str, env: dict[str, str | None] | None = None) -> harness.ChildResult:
	result = harness.runChild(tmp_path, PRELUDE + code, env=env, recordLevel=DEBUG, timeout=120)
	result.assertClean()
	assertNothingTypedLogged(result)
	return result


def shadowRecords(result: harness.ChildResult) -> list[str]:
	return [
		e["message"]
		for e in result.events
		if e["kind"] == "log" and (e.get("codepath") or "").startswith("linuxPlatform.keys.shadowGroup")
	]


def assertNothingTypedLogged(result: harness.ChildResult) -> None:
	for message in shadowRecords(result):
		assert ALLOWED_RECORDS.fullmatch(message), f"an unexpected record: {message!r}"
	for record in result.logRecords:
		assert TOGGLE not in record, record


def twoLayouts(layout: str = "pl,us") -> dict[str, str | None]:
	return {**noSystemBus(), "XKB_DEFAULT_LAYOUT": layout, "XKB_DEFAULT_OPTIONS": TOGGLE}


def test_aToggleByOptionSwitchesTheGroupOnTheFeedsThread(tmp_path: Path):
	"""Shift with Alt, either first, locks the next group of pl and us, and of pl, us and de in turn; a
	chord that switches nothing, and keys passed alone, change nothing; each switch reaches onChange on
	the feed's own thread while the main thread is busy; after stop nothing comes, and no thread is
	left."""
	result = runFeeds(
		tmp_path,
		r"""
feed = desktopLayout.EnvironmentLayoutFeed()
first = feed.start(onChange)
threads = feedThreads()
chord(feed, SHIFT, A)
chord(feed, ALT_GR, A)
feed.notePassed(ALT, False)
feed.notePassed(ALT, True)
time.sleep(0.2)
quiet = list(changes)
toggle(feed)
settle(1)
chord(feed, ALT, SHIFT)
settle(2)
two = groups()
asked = []
for turn in range(SWITCHES):
	asked.append(time.monotonic())
	toggle(feed)
	busy(0.3)
settle(2 + SWITCHES)
delays = [change[0] - at for change, at in zip(changes[2:], asked)]
onThread = sorted({change[1] for change in changes})
feed.stop()
toggle(feed)
time.sleep(0.2)
result = {
	"first": [first.group, list(first.names), first.source],
	"threads": threads,
	"quiet": quiet,
	"two": two,
	"busy": groups()[2:],
	"delays": delays,
	"onThread": onThread,
	"afterStop": len(changes),
	"left": feedThreads(),
}
os.environ["XKB_DEFAULT_LAYOUT"] = "pl,us,de"
changes.clear()
feed = desktopLayout.EnvironmentLayoutFeed()
feed.start(onChange)
for turn in range(3):
	toggle(feed)
	settle(turn + 1)
feed.stop()
result["three"] = groups()
""".replace("SWITCHES", str(SWITCHES)),
		env=twoLayouts(),
	)
	value = result.value
	assert value["first"] == [0, ["evdev", "pc105", "pl,us", "", TOGGLE], "environment"]
	assert value["threads"] == ["lindaKeyboardLayout"]
	assert value["quiet"] == []
	assert value["two"] == [1, 0]
	assert value["busy"] == [1, 0] * (SWITCHES // 2)
	assert max(value["delays"]) < CHANGE_BOUND, value["delays"]
	assert value["onThread"] == ["lindaKeyboardLayout"]
	assert value["afterStop"] == 2 + SWITCHES
	assert value["left"] == []
	assert value["three"] == [1, 2, 0]
	assert "Keyboard layout from environment: us" in shadowRecords(result)


def test_localedsLayoutIsFollowedToo(tmp_path: Path):
	"""localed's pl and us with the option, from a stand-in on the rig's bus: the localed feed follows
	the switch as the environment's does."""
	code = r"""
feed = desktopLayout.LocaledLayoutFeed()
first = feed.start(onChange)
toggle(feed)
settle(1)
toggle(feed)
settle(2)
feed.stop()
result = {"first": [first.group, list(first.names), first.source], "changes": [c[1:] for c in changes]}
"""
	with standIn("localed", "pl,us", "", "", TOGGLE):
		result = runFeeds(tmp_path, code, env={**noSystemBus(), "DBUS_SYSTEM_BUS_ADDRESS": _sessionBus()})
	names = ["evdev", "pc105", "pl,us", "", TOGGLE]
	assert result.value == {
		"first": [0, names, "localed"],
		"changes": [
			["lindaKeyboardLayout", 1, names, "localed"],
			["lindaKeyboardLayout", 0, names, "localed"],
		],
	}
	assert "Keyboard layout from localed: us" in shadowRecords(result)


def _sessionBus() -> str:
	import os

	return os.environ["DBUS_SESSION_BUS_ADDRESS"]


def test_oneLayoutHasNoGroupToFollow(tmp_path: Path):
	"""pl alone, with the option: no thread is started for it, and the toggle's keys change nothing."""
	result = runFeeds(
		tmp_path,
		r"""
result = {}
for kind in ("EnvironmentLayoutFeed", "LocaledLayoutFeed"):
	feed = getattr(desktopLayout, kind)()
	feed.start(onChange)
	time.sleep(0.1)
	threads = feedThreads()
	toggle(feed)
	time.sleep(0.2)
	feed.stop()
	result[kind] = [threads, len(changes)]
""",
		env={**twoLayouts("pl")},
	)
	assert result.value == {"EnvironmentLayoutFeed": [[], 0], "LocaledLayoutFeed": [[], 0]}


def test_aPressHeardAgainBeforeItsReleaseIsDownOnce(tmp_path: Path):
	"""A press of Shift heard twice and one release, as when the helper let go of it unheard: Shift is up
	after, so Alt alone switches nothing, and Shift with Alt still does."""
	result = runFeeds(
		tmp_path,
		r"""
feed = desktopLayout.EnvironmentLayoutFeed()
feed.start(onChange)
feed.notePassed(SHIFT, False)
feed.notePassed(SHIFT, False)
feed.notePassed(SHIFT, True)
chord(feed, ALT)
time.sleep(0.2)
alone = groups()
toggle(feed)
settle(1)
feed.stop()
result = {"alone": alone, "after": groups()}
""",
		env=twoLayouts(),
	)
	assert result.value == {"alone": [], "after": [1]}


def test_aLaterLayoutIsPassedOnAndItsGroupFollowed(tmp_path: Path):
	"""A layout the desktop gives later, as localed's answer after start's wait, is passed on on the
	feed's thread with its own group; the same names again keep the group the keys switched to; and a
	single layout is passed on with nothing more followed."""
	result = runFeeds(
		tmp_path,
		r"""
shadow = shadowGroup.ShadowGroup(onChange)
shadow.start(names("pl,us", OPTIONS))
toggle(shadow)
settle(1)
shadow.change(names("us,de", OPTIONS))
settle(2)
toggle(shadow)
settle(3)
shadow.change(names("us,de", OPTIONS))
time.sleep(0.2)
same = len(changes)
shadow.change(names("fr"))
settle(4)
toggle(shadow)
time.sleep(0.2)
shadow.stop()
result = {"changes": [[c[1], c[2], c[3][2]] for c in changes], "same": same}
""".replace("OPTIONS", repr(TOGGLE)),
		env=twoLayouts(),
	)
	assert result.value == {
		"changes": [
			["lindaKeyboardLayout", 1, "pl,us"],
			["lindaKeyboardLayout", 0, "us,de"],
			["lindaKeyboardLayout", 1, "us,de"],
			["lindaKeyboardLayout", 0, "fr"],
		],
		"same": 3,
	}


#: A module's feed that cannot start, for desktopLayout's fallback to take the place of.
BROKEN = r"""
class Broken:
	def start(self, onChange):
		raise RuntimeError("a feed that cannot start")

	def stop(self):
		pass


class Plain:
	def start(self, onChange):
		return names("pl,us", OPTIONS)

	def stop(self):
		pass
""".replace("OPTIONS", repr(TOGGLE))


def test_keysReachTheFeedThatTookAModulesPlace(tmp_path: Path):
	"""desktopLayout's fallback for a module's feed passes keys to the desktop's feed that took its
	place, and to a module's feed that has no use for them, nothing."""
	result = runFeeds(
		tmp_path,
		BROKEN
		+ r"""
fallback = desktopLayout._WithFallback("broken", Broken(), desktopLayout.EnvironmentLayoutFeed)
fallback.start(onChange)
toggle(fallback)
settle(1)
fallback.stop()
plain = desktopLayout._WithFallback("plain", Plain(), desktopLayout.EnvironmentLayoutFeed)
plain.start(onChange)
toggle(plain)
time.sleep(0.2)
plain.stop()
result = {"groups": groups(), "desktops": [type(desktopLayout.desktopFeed(d)).__name__ for d in ("sway", None, "GNOME", "KDE")]}
""",
		env=twoLayouts(),
	)
	assert result.value == {
		"groups": [1],
		"desktops": ["LocaledLayoutFeed", "LocaledLayoutFeed", "GnomeLayoutFeed", "PlasmaLayoutFeed"],
	}


@pytest.mark.rigSession("x11")
def test_anX11SessionLoadsNoShadow(tmp_path: Path):
	"""The owner's session builds and starts today's route, and neither the shadow nor the layout feeds it
	lives in are imported."""
	done = harness.runChild(
		tmp_path,
		f"import time\nRIG_DISPLAY = {os.environ['DISPLAY']!r}\n"
		+ OWNER_CHILD
		+ 'result["shadow"] = sorted(n for n in sys.modules if n.rsplit(".", 1)[-1] in ("shadowGroup", "desktopLayout"))\n',
		env=OWNER_SESSION,
		timeout=60,
	)
	done.assertClean()
	assert done.value["startedType"] == "linuxPlatform.keys.route.RoutedKeySource"
	assert done.value["shadow"] == []


# The Wayland route, with the stand-in putting keys out on a NullSink that switches groups as the
# compositor would.

ROUTE_CODE = r"""
from xkbcommon import xkb

sink = NullSink(layout=LAYOUT, options=OPTIONS)
standIn()
source = startRoute()
environment = source._environment
if WIRE:
	# What WaylandEnvironment.notePassed is asked to do itself.
	environment.onPassed = environment._layoutFeed.notePassed


def sinkGroup():
	return sink._state.serialize_layout(xkb.StateComponent.XKB_STATE_LAYOUT_LOCKED)


def altGrA():
	before = len(delivered)
	keyboard.press("AltGr+a")
	time.sleep(0.3)
	return [[e["vk"], e["text"]] for e in delivered[before:] if not e["up"]]


turns = []
for turn in range(3):
	turns.append({"desktop": sinkGroup(), "linda": environment.layout.group, "altGrA": altGrA()})
	keyboard.press("shift+alt")
	time.sleep(0.3)
source.stop()
result = {"turns": turns, "source": environment.layout.source}
"""

#: VK_RMENU and the A key. AltGr, which picks a level, is not told of at all.
RIGHT_ALT, KEY_A = 0xA5, 0x41
OGONEK = "\N{LATIN SMALL LETTER A WITH OGONEK}"


def routeTurns(tmp_path: Path, layout: str, *, wire: bool = True) -> dict:
	return runRouteChild(
		tmp_path,
		f"LAYOUT = {layout!r}\nOPTIONS = {TOGGLE!r}\nWIRE = {wire!r}\n" + ROUTE_CODE,
		env={**noSystemBus(), "XKB_DEFAULT_LAYOUT": layout, "XKB_DEFAULT_OPTIONS": TOGGLE},
	)


@pytest.mark.rigSession("x11")
def test_theRouteReadsKeysInTheGroupTheDesktopSwitchedTo(tmp_path: Path):
	"""us and pl: AltGr with a is right Alt with a in us, which types nothing; after Shift with Alt the
	desktop is in pl, and so is Linda, for which the same keys are AltGr typing a with ogonek; after
	the toggle again both are back in us."""
	value = routeTurns(tmp_path, "us,pl")
	assertFollowed(value)


def assertFollowed(value: dict) -> None:
	assert value["source"] == "environment"
	assert [[t["desktop"], t["linda"]] for t in value["turns"]] == [[0, 0], [1, 1], [0, 0]]
	us = [[RIGHT_ALT, ""], [KEY_A, "a"]]
	assert [t["altGrA"] for t in value["turns"]] == [us, [[KEY_A, OGONEK]], us]


@pytest.mark.rigSession("x11")
@pytest.mark.xfail(
	strict=True,
	reason="WaylandEnvironment.notePassed does not yet pass keys to the layout feed (package SG's needs)",
)
def test_theEnvironmentGivesTheFeedThePassedKeys(tmp_path: Path):
	"""As test_theRouteReadsKeysInTheGroupTheDesktopSwitchedTo, with nothing but the environment itself
	to give the layout feed the keys passed on. Without it the desktop is in pl after the toggle while
	Linda stays in us, and AltGr with a, typing a with ogonek there, is right Alt with a to Linda."""
	assertFollowed(routeTurns(tmp_path, "us,pl", wire=False))


# Linda itself.

#: Stands in for WaylandEnvironment.notePassed giving the layout feed the keys passed on, which package
#: SG asked of that file's owner; remove it once that is there.
WIRING_PLUGIN = """
import globalPluginHandler
from linuxPlatform import inputHook
from logHandler import log


class GlobalPlugin(globalPluginHandler.GlobalPlugin):
	def __init__(self):
		super().__init__()
		environment = getattr(inputHook._source, "_environment", None)
		notePassed = getattr(getattr(environment, "_layoutFeed", None), "notePassed", None)
		if notePassed is not None:
			environment.onPassed = notePassed
		log.info(f"shadow group probe: {'ready' if notePassed is not None else 'absent'}")
"""
#: Typed into the field in every test, and never to be found in Linda's log.
MARKER = "5038264917"
WINDOW_TITLE = "TextFixture"
#: Seconds for a key to reach the field and Linda to say what it says of it.
SETTLE = 1.0


def lindaHooks(directory: Path) -> Path:
	hooks = directory / "hooks"
	(hooks / "globalPlugins").mkdir(parents=True)
	(hooks / "synthDrivers").symlink_to(harness.HERE / "synthDrivers")
	(hooks / "globalPlugins" / "shadowGroupProbe.py").write_text(WIRING_PLUGIN)
	return hooks


def lindaEnvironment(hooks: Path, keyboard: StandIn, layout: str) -> dict[str, str | None]:
	return {
		"LINDA_TEST_HOOKS_DIR": str(hooks),
		"LINDA_TEST_KEYD_SOCKET": keyboard.path,
		"LINDA_SESSION_TYPE": "wayland",
		# Neither a desktop nor a compositor with a feed of its own: localed's, which the rig has no system
		# bus for, then the environment's.
		"XDG_CURRENT_DESKTOP": None,
		"SWAYSOCK": None,
		"XKB_DEFAULT_LAYOUT": layout,
		"XKB_DEFAULT_VARIANT": None,
		"XKB_DEFAULT_OPTIONS": TOGGLE,
	}


#: What input help calls AltGr with a where the key is right Alt, as in a us group of us and pl.
RIGHT_ALT_WITH_A = ["right alt", "alt plus a"]


def altGrWithA(letter: str) -> list[str]:
	"""What input help calls AltGr with a where the key is AltGr, as Windows' hook reports it, with a left
	Control first, and then the letter it types there: in a pl group, and in a us group of pl and us,
	where pl's AltGr stands as us leaves the key undefined (results/E5-gnome.md)."""
	return ["left control", "ctrl plus right alt", "ctrl plus alt plus a", letter]


def namesAndEcho(desktop: DesktopSession, keyboard: StandIn, text: Callable[[], str]) -> list[dict]:
	"""For each of three turns, Shift with Alt between them: what Linda says of AltGr with a in input
	help, then what it echoes once the key goes to the program, and the program's text after it."""
	turns = []
	for turn in range(3):
		mark = desktop.mark()
		keyboard.press("Insert+1")
		desktop.expect("input help on…", since=mark)
		named = desktop.mark()
		keyboard.press("AltGr+a")
		time.sleep(SETTLE)
		name = desktop.spokenSince(named)
		mark = desktop.mark()
		keyboard.press("Insert+1")
		desktop.expect("input help off…", since=mark)
		mark = desktop.mark()
		keyboard.press("AltGr+a")
		time.sleep(SETTLE)
		turns.append({"name": name, "echo": desktop.spokenSince(mark), "text": text()})
		if turn < 2:
			keyboard.press("shift+alt")
			time.sleep(SETTLE)
	return turns


def heard(turns: list[dict]) -> list[list]:
	"""Of each turn, the name input help gave, the characters echoed, and the program's text."""
	return [[turn["name"], [said for said in turn["echo"] if len(said) == 1], turn["text"]] for turn in turns]


@pytest.fixture
def socketDir() -> Iterator[Path]:
	# Under the rig's own /tmp: a path under pytest's directory is too long for a socket.
	directory = Path(tempfile.mkdtemp(prefix="keyd", dir="/tmp"))
	try:
		yield directory
	finally:
		shutil.rmtree(directory, ignore_errors=True)


def entryText() -> str:
	return desktopSession._atspi().Text.get_text(findField(WINDOW_TITLE, "entry"), 0, -1)


@pytest.mark.rigSession("x11")
def test_lindaNamesAndEchoesKeysInTheGroupTheDesktopSwitchedTo(tmp_path: Path, socketDir: Path):
	"""The X display set to us and pl with the option, switching its group as a compositor would, and
	Linda told the session is Wayland, with the same names: Shift with Alt changes what input help calls
	AltGr with a, and whether typing it echoes a with ogonek, as the field gets one or not."""
	subprocess.run(["setxkbmap", "-layout", "us,pl", "-option", "", "-option", TOGGLE], check=True)
	try:
		with (
			StandIn(socketDir / "socket") as keyboard,
			DesktopSession(tmp_path / "desktop", standIn=keyboard) as desktop,
		):
			config = tmp_path / "nvda.ini"
			config.write_text(configText("desktop", 7))
			desktop.startApp(desktop.fixture("textapp", "gtk3"), title=WINDOW_TITLE)
			nvda = desktop.startNVDA(config, env=lindaEnvironment(lindaHooks(tmp_path), keyboard, "us,pl"))
			nvda.waitForLog("shadow group probe: ready", STARTUP_TIMEOUT)
			keyboard.waitFor(lambda: keyboard.capturing, 10)
			assert desktopSession._atspi().Component.grab_focus(findField(WINDOW_TITLE, "entry"))
			time.sleep(SETTLE)
			before = entryText()
			turns = namesAndEcho(desktop, keyboard, entryText)
			# A with ogonek takes the place of the field's text, which starts selected.
			assert OGONEK not in before
			assert heard(turns) == [
				[RIGHT_ALT_WITH_A, [], before],
				[altGrWithA(OGONEK), [OGONEK], OGONEK],
				[RIGHT_ALT_WITH_A, [], OGONEK],
			]
			keyboard.type(MARKER)
			time.sleep(SETTLE)
			assert entryText().endswith(MARKER)
			nvda.process.terminate()
			nvda.waitForExit(20)
			nvda.assertClean(logNames=("nvda.log",))
			log = nvda.text("nvda.log")
	finally:
		subprocess.run(["setxkbmap", "-layout", "us", "-option", ""], check=True)
	harness.assertMarkerAbsent(MARKER, {"nvda.log": log})
	assert TOGGLE not in log


class GroupKeyboard:
	"""A sink for the stand-in under rig sway: a virtual keyboard of the test's own with pl and us and the
	option, which keeps the XKB state sway keeps for a real keyboard and sends the modifiers and group
	from it after every key. sway keeps no state for a virtual keyboard, so the rig's own, with pl
	alone, sends its own modifiers too; this one switches its group as sway switches a real one's."""

	def __init__(self, layout: str) -> None:
		from pywayland.protocol.wayland import WlSeat
		from xkbcommon import xkb

		self._xkb = xkb
		protocol = waylandKeyboard.loadProtocol(
			os.environ["LINDA_VIRTUAL_KEYBOARD_PROTOCOL"], "virtual_keyboard_unstable_v1"
		)
		self._display, registry, found = waylandKeyboard.connectWayland()
		manager = protocol.ZwpVirtualKeyboardManagerV1
		seatName, seatVersion = found["wl_seat"]
		seat = registry.bind(seatName, WlSeat, min(seatVersion, 7))
		self._keyboard = registry.bind(found[manager.name][0], manager, 1).create_virtual_keyboard(seat)
		keymap = xkb.Context().keymap_new_from_names(layout=layout, options=TOGGLE)
		self._state = keymap.state_new()
		text = keymap.get_as_string()
		fd = waylandKeyboard._keymapFile(text)
		self._keyboard.keymap(waylandKeyboard.WL_KEYMAP_FORMAT_XKB_V1, fd, len(text.encode()) + 1)
		os.close(fd)
		self._keyboard.modifiers(0, 0, 0, 0)
		self._display.roundtrip()
		self._lock = threading.Lock()
		self._closed = False
		self.sent: list[tuple[int, bool, bool]] = []
		self.kept: list[tuple[int, bool]] = []

	def send(self, evdev: int, down: bool, virtual: bool) -> None:
		xkb = self._xkb
		with self._lock:
			self._keyboard.key(
				int(time.monotonic() * 1000) & 0xFFFFFFFF,
				evdev,
				waylandKeyboard.PRESSED if down else waylandKeyboard.RELEASED,
			)
			self._state.update_key(
				evdev + 8, xkb.KeyDirection.XKB_KEY_DOWN if down else xkb.KeyDirection.XKB_KEY_UP
			)
			self._keyboard.modifiers(
				self._state.serialize_mods(xkb.StateComponent.XKB_STATE_MODS_DEPRESSED),
				self._state.serialize_mods(xkb.StateComponent.XKB_STATE_MODS_LATCHED),
				self._state.serialize_mods(xkb.StateComponent.XKB_STATE_MODS_LOCKED),
				self._state.serialize_layout(xkb.StateComponent.XKB_STATE_LAYOUT_EFFECTIVE),
			)
			self._display.roundtrip()
			self.sent.append((evdev, down, virtual))

	def keep(self, evdev: int, down: bool) -> None:
		self.kept.append((evdev, down))

	def lights(self) -> int:
		with self._lock:
			lit = {
				name: self._state.led_name_is_active(name)
				for name in ("Num Lock", "Caps Lock", "Scroll Lock")
			}
		return (
			(1 if lit["Num Lock"] else 0) | (2 if lit["Caps Lock"] else 0) | (4 if lit["Scroll Lock"] else 0)
		)

	def passed(self) -> list[tuple[int, bool]]:
		return [(evdev, down) for evdev, down, virtual in self.sent if virtual]

	def close(self) -> None:
		with self._lock:
			if self._closed:
				return
			self._closed = True
			self._keyboard.destroy()
			self._display.roundtrip()
			self._display.disconnect()


@pytest.mark.rigSession("sway")
def test_lindaFollowsTheGroupUnderSway(tmp_path: Path, socketDir: Path):
	"""Under rig sway with pl and us and the option, from the environment with sway's own feed out of the
	way: what input help calls AltGr with a, its echo, and what reaches the GTK 4 probe follow Shift with
	Alt from pl to us and back. In the us group of pl and us AltGr is still AltGr, and with a types a."""
	harness.requireRig()
	sink = GroupKeyboard("pl,us")
	probe = None
	try:
		with (
			StandIn(socketDir / "socket", sink=sink) as keyboard,
			DesktopSession(tmp_path / "desktop", standIn=keyboard) as desktop,
		):
			probe = KeyProbe(tmp_path)
			hooks = lindaHooks(tmp_path)
			nvda = desktop.startNVDA(SESSION_CONFIG, env=lindaEnvironment(hooks, keyboard, "pl,us"))
			desktop.expect(lindaSays(HINT), since=0, timeout=STARTUP_TIMEOUT)
			nvda.waitForLog("shadow group probe: ready", STARTUP_TIMEOUT)
			keyboard.waitFor(lambda: keyboard.capturing, 10)
			time.sleep(SETTLE)

			def probeText() -> str:
				texts = [line[len("text ") :] for line in probe.lines if line.startswith("text ")]
				return texts[-1] if texts else ""

			turns = namesAndEcho(desktop, keyboard, probeText)
			assert heard(turns) == [
				[altGrWithA(OGONEK), [OGONEK], OGONEK],
				[altGrWithA("a"), ["a"], OGONEK + "a"],
				[altGrWithA(OGONEK), [OGONEK], OGONEK + "a" + OGONEK],
			]
			keyboard.type(MARKER)
			probe.waitFor(lambda line: line.startswith("text ") and line.endswith(MARKER), 5)
			nvda.process.terminate()
			nvda.waitForExit(20)
			nvda.assertClean(logNames=("nvda.log",))
			log = nvda.text("nvda.log")
			assert "Keyboard layout from environment: pl" in log
	finally:
		if probe is not None:
			probe.stop()
		# The stand-in closed it, unless it never started.
		sink.close()
		# The rig's keyboard is the seat's again once it types.
		waylandKeyboard.press("Shift_L", gap=0)
	harness.assertMarkerAbsent(MARKER, {"nvda.log": log})
	assert TOGGLE not in log
