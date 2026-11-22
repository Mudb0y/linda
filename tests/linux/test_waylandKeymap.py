"""The keymap the compositor compiled, read over a Wayland connection of Linda's own, and the names
that make it found for the kernel source: the desktop's own where they make it, else the compositor's
layouts named again with the desktop's options, or with the options its keymap carries.

The tests for the X session hold the naming to keymaps xkbcommon makes as the compositors make them,
and show that an X session never loads the module. The tests marked for Mutter and sway run the feed
against the rig's compositors: Mutter given the keymap GNOME's shell compiles for two input sources,
sway a keyboard whose keymap has options its IPC does not report. Run under the rig:

	nix develop . -c tests/linux/rig python3 -m pytest tests/linux/test_waylandKeymap.py
"""

import os
import re
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import harness
import pytest
import waylandKeyboard
import waylandKeymapDump

DEBUG = 10
#: GNOME's default model, and the option NixOS's GNOME sets, as package AG's guest had them
#: (E5-gnome.md).
GNOME_MODEL = "pc105+inet"
GNOME_OPTIONS = "terminate:ctrl_alt_bksp"
#: The three keys E5 found the names feed wrong in with US English in use beside Polish: the space bar,
#: the numpad's Delete and the right Alt key.
E5_KEYS = [57, 83, 100]
#: The options a sway configuration may give that its IPC does not report (E21.md, DW-delivery-watch.txt).
SWAY_OPTIONS = "caps:escape,grp:alt_shift_toggle"
US_NOTICE = "NVDA cannot find this desktop's keyboard layout, so it reads keys as US English."
#: How long a keymap change may take to reach onChange: Mutter's handling of SetKeymap and the feed's
#: naming, which compiles and compares two keymaps.
CHANGE_BOUND = 2.0
#: Every record the module may make.
ALLOWED_RECORDS = [
	re.compile(p)
	for p in (
		r"Keyboard layout: the compositor's keymap has \d+ layouts",
		r"Keyboard layout: the compositor has given no keymap, so the desktop's layout stands",
		(
			r"Keyboard layout: no names make the compositor's keymap, so the desktop's layout stands; "
			r"\d+ keys differ"
		),
		(
			r"Keyboard layout: the compositor's keymap does not hold the desktop's layout in use, so the "
			r"desktop's layout stands"
		),
		r"Keyboard layout: the compositor's keymap is the desktop's layout",
		r"Keyboard layout: named from the compositor's keymap: [\w,()-]+, in use [\w-]+( [\w-]+)?",
		r"Keyboard layout: no compositor socket to read its keymap from",
		r"Keyboard layout: pywayland is not installed, so the compositor's keymap is not read",
		r"Keyboard layout: no connection to the compositor for its keymap: \w+",
		r"Keyboard layout: the compositor offers no seat",
	)
]
#: What the tests set that is not a layout's name, none of which may be logged.
UNLOGGED = ("terminate", "ctrl_alt_bksp", "caps:escape", "grp:alt_shift_toggle", "pc105")

#: Before each child's code: the module, keymaps made as compositors make them, and the comparison.
PRELUDE = r"""
import os, threading, time
from logHandler import log
log.setLevel(log.DEBUG)
from xkbcommon import xkb
from linuxPlatform.keys import kernel, waylandKeymap
from linuxPlatform.keys.desktopLayout import LayoutSnapshot
import waylandKeymapDump

CODES = waylandKeymapDump.namedCodes(waylandKeymapDump.KEY_MAP)
changes = []


def onChange(snapshot):
	changes.append((time.monotonic(), threading.current_thread().name, snapshot))


def quiet():
	context = xkb.Context()
	context.set_log_level(xkb.lib.XKB_LOG_LEVEL_CRITICAL)
	return context


def compositorKeymap(names):
	text = quiet().keymap_new_from_names(*names).get_as_string()
	return xkb.Context().keymap_new_from_string(text), text


def tables(keymap, group):
	made = kernel._tables(keymap, (), (0, 0, group))
	return [sorted(made.keyMods.items()), sorted(made.levelShift)]


def follows(text, snapshot):
	# Whether the kernel source's tables of snapshot are those of the keymap text, in every layout,
	# and in the snapshot's group.
	compositor = xkb.Context().keymap_new_from_string(text)
	fed = xkb.Context().keymap_new_from_names(*snapshot.names)
	return {
		"described": waylandKeymapDump.describe(fed.get_as_string(), CODES)
		== waylandKeymapDump.describe(text, CODES),
		"tables": tables(fed, snapshot.group) == tables(compositor, snapshot.group),
	}


def plain(snapshot):
	repeat = None if snapshot.repeat is None else list(snapshot.repeat)
	return [
		list(snapshot.names), snapshot.group, repeat, snapshot.locksGuess, snapshot.source,
		snapshot.inputMethod, snapshot.notice,
	]


def feedThreads():
	return [t.name for t in threading.enumerate() if t.name == "lindaKeyboardLayout"]
"""


def runModule(tmp_path: Path, code: str, env: dict[str, str | None] | None = None, **clean) -> object:
	result = harness.runChild(
		tmp_path,
		f"import sys\nsys.path.insert(0, {str(harness.HERE)!r})\n" + PRELUDE + code,
		env=env,
		recordLevel=DEBUG,
		timeout=300,
	)
	result.assertClean(**clean)
	assertOnlyNamesLogged(result)
	return result


def moduleRecords(result) -> list[str]:
	return [
		e["message"]
		for e in result.events
		if e["kind"] == "log" and (e.get("codepath") or "").startswith("linuxPlatform.keys.waylandKeymap")
	]


def assertOnlyNamesLogged(result) -> None:
	for message in moduleRecords(result):
		assert any(p.fullmatch(message) for p in ALLOWED_RECORDS), f"an unexpected record: {message!r}"
		for value in UNLOGGED:
			assert value not in message, f"{value!r} was logged: {message!r}"


def gnomeNames(layouts: str) -> tuple[str, str, str, str, str]:
	count = len(layouts.split(","))
	return ("evdev", GNOME_MODEL, layouts, "," * (count - 1), GNOME_OPTIONS)


def describedNames(names: tuple[str, ...]) -> list[str]:
	from xkbcommon import xkb

	keymap = xkb.Context().keymap_new_from_names(*names)
	return waylandKeymapDump.describe(keymap.get_as_string(), codes())


def codes() -> list[int]:
	return waylandKeymapDump.namedCodes(waylandKeymapDump.KEY_MAP)


# The naming, against keymaps made here.


@pytest.mark.rigSession("x11")
def test_gnomesTwoSourcesAreNamedWhole(tmp_path: Path):
	"""E5's case: GNOME's shell compiles Polish and US English into one keymap, and the names feed gives
	the source in use alone, which is wrong in three keys with US English in use. Named from the
	compositor's keymap, every code keyMap names matches in both groups, as do the kernel source's
	tables, and the group is the source's."""
	result = runModule(
		tmp_path,
		rf"""
keymap, text = compositorKeymap({gnomeNames("pl,us")!r})
result = {{}}
for layout in ("us", "pl"):
	base = LayoutSnapshot(("evdev", {GNOME_MODEL!r}, layout, "", {GNOME_OPTIONS!r}), source="gnome")
	naming = waylandKeymap.nameKeymap(keymap, text, base)
	group = waylandKeymap.mapGroup(naming, base, keymap)
	own = waylandKeymap.signature(xkb.Context().keymap_new_from_names(*base.names))[0]
	theirs = waylandKeymap.signature(keymap)[group]
	result[layout] = {{
		"names": list(naming.names),
		"desktops": naming.desktops,
		"group": group,
		"follows": follows(text, base._replace(names=naming.names, group=group)),
		"aloneFollows": follows(text, base),
		"aloneDiffers": sorted(
			a[0] - 8 for a, b in zip(own, theirs) if a != b and a is not None and a[0] - 8 in CODES
		),
	}}
""",
	)
	value = result.value
	for layout, group in (("us", 1), ("pl", 0)):
		assert value[layout]["names"] == list(gnomeNames("pl,us")), value[layout]
		assert value[layout]["desktops"] is False
		assert value[layout]["group"] == group
		assert value[layout]["follows"] == {"described": True, "tables": True}
	# The control: the names feed alone is the compositor's keymap in Polish, and not in US English
	# beside it, where the right Alt key is Alt and not AltGr.
	assert value["pl"]["aloneDiffers"] == []
	assert value["us"]["aloneDiffers"] == E5_KEYS
	assert value["us"]["aloneFollows"]["tables"] is False


@pytest.mark.rigSession("x11")
def test_theDesktopsOwnNamesStandWhereTheyMakeTheKeymap(tmp_path: Path):
	result = runModule(
		tmp_path,
		r"""
names = ("evdev", "pc104", "pl,de", ",nodeadkeys", "")
keymap, text = compositorKeymap(names)
base = LayoutSnapshot(names, group=1, source="kde")
naming = waylandKeymap.nameKeymap(keymap, text, base)
result = [list(naming.names), naming.desktops, waylandKeymap.mapGroup(naming, base, keymap)]
""",
	)
	assert result.value == [["evdev", "pc104", "pl,de", ",nodeadkeys", ""], True, 1]


@pytest.mark.rigSession("x11")
def test_optionsTheDesktopDoesNotSayAreFoundInTheKeymap(tmp_path: Path):
	"""sway's IPC reports no options, so its feed gives the environment's; the keymap's own section names
	carry the components the options added, and the options found from them make the keymap."""
	result = runModule(
		tmp_path,
		rf"""
names = ("evdev", "pc105", "pl,us", ",", {SWAY_OPTIONS!r})
keymap, text = compositorKeymap(names)
base = LayoutSnapshot(("evdev", "pc105", "pl,us", ",", ""), group=1, source="sway")
naming = waylandKeymap.nameKeymap(keymap, text, base)
group = waylandKeymap.mapGroup(naming, base, keymap)
result = {{
	"found": waylandKeymap.optionsIn(text),
	"names": list(naming.names),
	"group": group,
	"follows": follows(text, base._replace(names=naming.names, group=group)),
}}
""",
	)
	assert result.value == {
		"found": SWAY_OPTIONS,
		"names": ["evdev", "pc105", "pl,us", ",", SWAY_OPTIONS],
		"group": 1,
		"follows": {"described": True, "tables": True},
	}


@pytest.mark.rigSession("x11")
def test_everyOptionOfTheRulesIsFoundAgain(tmp_path: Path):
	"""Each option the rules file names, set alone on pl and us, is named from the keymap it makes, for a
	desktop that names no options; only those that change nothing, or name a file the system has not
	got, are left."""
	result = runModule(
		tmp_path,
		r"""
plainKeymap = waylandKeymap.signature(xkb.Context().keymap_new_from_names("evdev", "pc105", "pl,us", ","))
base = LayoutSnapshot(("evdev", "pc105", "pl,us", ",", ""), source="sway")
result = {"options": 0, "unchanged": 0, "uncompiled": [], "unnamed": []}
for option in waylandKeymap.optionTable("evdev"):
	result["options"] += 1
	try:
		keymap, text = compositorKeymap(("evdev", "pc105", "pl,us", ",", option))
	except xkb.XKBError:
		result["uncompiled"].append(option)
		continue
	if waylandKeymap.signature(keymap) == plainKeymap:
		result["unchanged"] += 1
		continue
	naming = waylandKeymap.nameKeymap(keymap, text, base)
	if naming is None or not follows(text, base._replace(names=naming.names))["described"]:
		result["unnamed"].append(option)
""",
	)
	value = result.value
	print(f"options: {value}")
	assert value["options"] > 200
	assert value["uncompiled"] == ["custom:types"]
	assert value["unnamed"] == []


@pytest.mark.rigSession("x11")
def test_aKeymapNoNamesMakeIsLeftUnnamed(tmp_path: Path):
	"""A keymap the user wrote, as sway's xkb_file gives one: no names make it, and none are given."""
	result = runModule(
		tmp_path,
		r"""
keymap, text = compositorKeymap(("evdev", "pc105", "pl", "", ""))
import re

# The A key types b at its first level, which no layout of the rules has it do.
edited = re.sub(r"(key <AC01>[^\[]*\[\s*)(a|0x61)\b", r"\g<1>0x62", text, count=1)
assert edited != text
keymap = xkb.Context().keymap_new_from_string(edited)
base = LayoutSnapshot(("evdev", "pc105", "pl", "", ""), source="environment")
result = waylandKeymap.nameKeymap(keymap, edited, base)
""",
	)
	assert result.value is None


@pytest.mark.rigSession("x11")
def test_aLayoutTheRegistryDoesNotNameIsTheDesktops(tmp_path: Path):
	"""Of the descriptions the registry gives two layouts, the desktop's own layout wins; and a keymap
	whose layouts neither knows is not named."""
	result = runModule(
		tmp_path,
		r"""
from linuxPlatform.keys import swayLayout

registry = swayLayout.registry("evdev")
# hr(glagyz) calls itself what hr(glag) calls itself, and the registry has hr(glag) for it (E21.md).
names = ("evdev", "pc105", "pl,hr", ",glagyz", "")
keymap, text = compositorKeymap(names)
description = keymap.layout_get_name(1)
known = LayoutSnapshot(names, source="sway")
fromRegistry = LayoutSnapshot(("evdev", "pc105", "pl", "", ""), source="gnome")
result = {
	"registry": list(registry[description]),
	"known": list(waylandKeymap.nameKeymap(keymap, text, known).names),
	"fromRegistry": waylandKeymap.nameKeymap(keymap, text, fromRegistry),
}
""",
	)
	assert result.value["registry"] == ["hr", "glag"]
	assert result.value["known"] == ["evdev", "pc105", "pl,hr", ",glagyz", ""]
	# hr(glag) does not make hr(glagyz)'s keys, so nothing is named, and the desktop's layout stands.
	assert result.value["fromRegistry"] is None


@pytest.mark.rigSession("x11")
def test_noCompositorNoFeed(tmp_path: Path):
	"""Told the session is Wayland with no compositor socket, or with pywayland missing, the module
	offers no feed, and desktopLayout goes on to the next."""
	result = runModule(
		tmp_path,
		r"""
import sys
first = waylandKeymap.create(None)
os.environ["WAYLAND_DISPLAY"] = os.path.join(os.environ["XDG_RUNTIME_DIR"], "no-such-socket")
second = waylandKeymap.create(None)
result = [first, second]
""",
		env={"LINDA_SESSION_TYPE": "wayland", "WAYLAND_DISPLAY": None},
	)
	assert result.value == [None, None]
	assert moduleRecords(result).count("Keyboard layout: no compositor socket to read its keymap from") == 2


#: Linda's key route in an X session, as the owner's runs it, and what it imported.
X11_CHILD = r"""
import os, sys, time
import linuxPlatform.keys as keys

os.environ["DISPLAY"] = RIG_DISPLAY
source = keys.createSource("auto", lambda message: None)
source.start(lambda event: True)
time.sleep(0.5)
source.stop()
result = {
	"type": type(source).__name__,
	"modules": sorted(
		name for name in sys.modules if name.endswith("waylandKeymap") or name.split(".")[0] == "pywayland"
	),
}
"""


@pytest.mark.rigSession("x11")
def test_anX11SessionNeverLoadsTheModule(tmp_path: Path):
	done = harness.runChild(
		tmp_path,
		f"RIG_DISPLAY = {os.environ['DISPLAY']!r}\n" + X11_CHILD,
		env={
			"XDG_SESSION_TYPE": "x11",
			"XDG_CURRENT_DESKTOP": "MATE",
			"WAYLAND_DISPLAY": None,
			"LINDA_SESSION_TYPE": None,
		},
		timeout=60,
	)
	assert done.error is None, f"{done.error}\n{done.stderr[-6000:]}"
	assert done.value == {"type": "RoutedKeySource", "modules": []}


# The feed, against the rig's compositors.


def compositorDump() -> list[str]:
	"""The keymap the compositor gives a client's keyboard, as tests/linux/waylandKeymapDump.py prints it."""
	dumped = subprocess.run(
		[sys.executable, str(harness.HERE / "waylandKeymapDump.py")],
		capture_output=True,
		text=True,
		timeout=30,
		check=True,
	)
	return dumped.stdout.splitlines()


def rigKeymapText() -> str:
	keymap, _state = waylandKeyboard._keymap()
	return keymap.get_as_string()


class MutterKeymap:
	"""A remote desktop session of the test's own, through which it sets the keymap Mutter compiles, as
	gnome-shell sets the one it builds from the input sources."""

	def __init__(self) -> None:
		from gi.repository import Gio, GLib

		self._gio, self._glib = Gio, GLib
		self._bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
		(self._session,) = self._call(
			"/org/gnome/Mutter/RemoteDesktop", waylandKeyboard.REMOTE_DESKTOP, "CreateSession", reply="(o)"
		).unpack()
		self._interface = waylandKeyboard.REMOTE_DESKTOP + ".Session"
		self._call(self._session, self._interface, "Start")

	def _call(self, objectPath, interface, method, args=None, reply=None):
		return self._bus.call_sync(
			waylandKeyboard.REMOTE_DESKTOP,
			objectPath,
			interface,
			method,
			args,
			self._glib.VariantType(reply) if reply else None,
			self._gio.DBusCallFlags.NONE,
			5000,
			None,
		)

	def set(self, text: str, layoutIndex: int = 0) -> None:
		Gio, GLib = self._gio, self._glib
		fd = waylandKeyboard._keymapFile(text)
		fds = Gio.UnixFDList.new()
		index = fds.append(fd)
		os.close(fd)
		options = {
			"keymap-type": GLib.Variant("u", waylandKeyboard.KEYMAP_TYPE_XKB),
			"xkb-keymap-format": GLib.Variant("u", waylandKeyboard.XKB_KEYMAP_FORMAT_TEXT_V1),
			"xkb-keymap": GLib.Variant("h", index),
			"xkb-keymap-layout-index": GLib.Variant("u", layoutIndex),
		}
		self._bus.call_with_unix_fd_list_sync(
			waylandKeyboard.REMOTE_DESKTOP,
			self._session,
			self._interface,
			"SetKeymap",
			GLib.Variant("(a{sv})", (options,)),
			None,
			Gio.DBusCallFlags.NONE,
			5000,
			fds,
			None,
		)

	def close(self) -> None:
		try:
			# The rig's keyboard types in pl for every later test.
			self.set(rigKeymapText())
		finally:
			self._call(self._session, self._interface, "Stop")


@contextmanager
def mutterKeymap(names: tuple[str, ...], layoutIndex: int) -> Iterator[MutterKeymap]:
	from xkbcommon import xkb

	keymap = MutterKeymap()
	try:
		keymap.set(xkb.Context().keymap_new_from_names(*names).get_as_string(), layoutIndex)
		time.sleep(0.3)
		yield keymap
	finally:
		keymap.close()


#: GNOME's feed in the child, on the rig's memory GSettings backend, with sources pl and us.
GNOME_CHILD = r"""
import gi
gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib

inputSources = Gio.Settings.new("org.gnome.desktop.input-sources")
inputSources.set_value("sources", GLib.Variant("a(ss)", [("xkb", "pl"), ("xkb", "us")]))
inputSources.set_value("mru-sources", GLib.Variant("a(ss)", [("xkb", "us"), ("xkb", "pl")]))
inputSources.set_value("xkb-options", GLib.Variant("as", [OPTIONS]))
feed = waylandKeymap.create("GNOME")
text = waylandKeymapDump.readKeymap(5)
first = feed.start(onChange)
result = {"first": plain(first), "firstFollows": follows(text, first), "type": type(feed).__name__}
# Super+Space to Polish, as GNOME's shell writes it.
inputSources.set_value("mru-sources", GLib.Variant("a(ss)", [("xkb", "pl"), ("xkb", "us")]))
deadline = time.monotonic() + 5
while not changes and time.monotonic() < deadline:
	time.sleep(0.02)
feed.stop()
result["changes"] = [[plain(s), follows(text, s), thread] for _t, thread, s in changes]
result["threads"] = feedThreads()
"""


@pytest.mark.rigSession("mutter")
def test_gnomesKeymapUnderMutter(tmp_path: Path):
	"""Mutter given the keymap gnome-shell compiles for Polish and US English with US English in use:
	the feed gives GNOME's own layout in use, named from Mutter's keymap, and follows Super+Space."""
	names = gnomeNames("pl,us")
	with mutterKeymap(names, layoutIndex=1):
		dumped = compositorDump()
		result = runModule(
			tmp_path,
			GNOME_CHILD.replace("OPTIONS", repr(GNOME_OPTIONS)),
			env={"XDG_CURRENT_DESKTOP": "GNOME"},
		)
	value = result.value
	print(f"{harness.rigSession()}: Mutter's layouts {dumped[0]!r}, feed {value}")
	assert dumped == describedNames(names), "Mutter does not give clients the keymap it was set"
	assert value["type"] == "WaylandKeymapFeed"
	assert value["first"][:2] == [list(names), 1]
	assert value["first"][4] == "gnome"
	assert value["firstFollows"] == {"described": True, "tables": True}
	assert value["changes"] == [
		[[list(names), 0, *value["first"][2:]], {"described": True, "tables": True}, "lindaKeyboardLayout"]
	]
	assert value["threads"] == []
	records = moduleRecords(result)
	assert "Keyboard layout: named from the compositor's keymap: pl,us, in use us" in records


class Child:
	"""runModule's child in the background, which tells the test it has started its feed and waits for
	the test's word, both through files."""

	def __init__(self, tmp_path: Path, code: str, env: dict[str, str | None] | None = None) -> None:
		self._started = tmp_path / "started"
		self._go = tmp_path / "go"
		self._tmp_path = tmp_path
		self._code = (
			f"STARTED, GO = {str(self._started)!r}, {str(self._go)!r}\n"
			"def started():\n"
			"	open(STARTED, 'w').close()\n"
			"	deadline = time.monotonic() + 60\n"
			"	while not os.path.exists(GO) and time.monotonic() < deadline:\n"
			"		time.sleep(0.01)\n" + code
		)
		self._env = env
		self._outcome: dict[str, object] = {}
		self._thread = threading.Thread(target=self._run)
		self._thread.start()

	def _run(self) -> None:
		try:
			self._outcome["result"] = runModule(self._tmp_path, self._code, env=self._env)
		except BaseException as e:  # noqa: BLE001
			self._outcome["error"] = e

	def waitStarted(self, timeout: float = 60) -> None:
		deadline = time.monotonic() + timeout
		while not self._started.exists():
			assert self._thread.is_alive() or self._outcome, "the child ended before it started"
			if "error" in self._outcome:
				raise self._outcome["error"]
			assert time.monotonic() < deadline, "the child did not start its feed"
			time.sleep(0.01)

	def go(self):
		self._go.touch()
		self._thread.join(120)
		if "error" in self._outcome:
			raise self._outcome["error"]
		return self._outcome["result"]


#: A feed whose desktop feed is the environment's, as on a compositor with no listing of its own.
FOLLOW_CHILD = r"""
feed = waylandKeymap.create(None)
first = feed.start(onChange)
result = {"first": plain(first), "firstFollows": follows(waylandKeymapDump.readKeymap(5), first)}
started()
deadline = time.monotonic() + 5
while not changes and time.monotonic() < deadline:
	time.sleep(0.01)
text = waylandKeymapDump.readKeymap(5)
result["changed"] = [[plain(s), follows(text, s), thread, at] for at, thread, s in changes]
# Alt and Shift together, passed on, switch the group, as the keymap's option has them.
del changes[:]
for keycode, isUp in ((64, False), (50, False), (50, True), (64, True)):
	feed.notePassed(keycode, isUp)
deadline = time.monotonic() + 5
while not changes and time.monotonic() < deadline:
	time.sleep(0.01)
result["switched"] = [plain(s) for _at, _thread, s in changes]
feed.stop()
result["threads"] = feedThreads()
"""


@pytest.mark.rigSession("mutter")
def test_aNewKeymapIsFollowedAndItsGroupByTheKeys(tmp_path: Path):
	"""Mutter's keymap changed while the feed runs: the change reaches onChange, named with the option
	the keymap carries; and where the desktop's feed does not say the group, the keys passed on switch
	it as that option has them."""
	from xkbcommon import xkb

	names = ("evdev", "pc105", "pl,us", ",", "grp:alt_shift_toggle")
	child = Child(tmp_path, FOLLOW_CHILD, env={"XDG_CURRENT_DESKTOP": None, "XKB_DEFAULT_LAYOUT": "pl"})
	keymap = MutterKeymap()
	try:
		child.waitStarted()
		before = time.monotonic()
		keymap.set(xkb.Context().keymap_new_from_names(*names).get_as_string())
		result = child.go()
	finally:
		keymap.close()
	value = result.value
	print(f"{harness.rigSession()}: {value}")
	pl = ["evdev", "pc105", "pl", "", ""]
	first = value["first"]
	assert first[0] == pl and first[1] == 0 and first[4:] == ["environment", False, None], first
	assert value["firstFollows"] == {"described": True, "tables": True}
	((changed, changedFollows, thread, at),) = value["changed"]
	assert changed == [list(names), 0, *first[2:]]
	assert changedFollows == {"described": True, "tables": True}
	assert thread == "lindaKeyboardLayout"
	print(f"{harness.rigSession()}: the new keymap reached onChange {at - before:.3f} s after SetKeymap")
	assert 0 < at - before < CHANGE_BOUND, f"the change took {at - before:.3f} s"
	assert value["switched"] == [[list(names), 1, *first[2:]]]
	assert value["threads"] == []


@pytest.mark.rigSession("wayland")
def test_theUSGuessGivesWayToTheCompositorsLayout(tmp_path: Path):
	"""With nothing to say the layout, the environment's feed guesses US English and says so; the
	compositor's keymap, the rig's pl, names it, and there is nothing to say."""
	result = runModule(
		tmp_path,
		r"""
feed = waylandKeymap.create(None)
first = feed.start(onChange)
feed.stop()
result = {"first": plain(first), "follows": follows(waylandKeymapDump.readKeymap(5), first)}
""",
		env={"XDG_CURRENT_DESKTOP": None, "XKB_DEFAULT_LAYOUT": None, "SWAYSOCK": None},
	)
	first = result.value["first"]
	assert first[0] == ["evdev", "pc105", "pl", "", ""]
	assert first[4] == "environment"
	assert first[6] is None
	assert result.value["follows"] == {"described": True, "tables": True}


class TestKeyboard:
	"""A virtual keyboard of the test's own under sway, with the keymap text given."""

	__test__ = False

	def __init__(self, text: str) -> None:
		from pywayland.protocol.wayland import WlSeat

		protocol = waylandKeyboard.loadProtocol(
			os.environ["LINDA_VIRTUAL_KEYBOARD_PROTOCOL"], "virtual_keyboard_unstable_v1"
		)
		self.display, registry, found = waylandKeyboard.connectWayland()
		manager = protocol.ZwpVirtualKeyboardManagerV1
		seatName, seatVersion = found["wl_seat"]
		seat = registry.bind(seatName, WlSeat, min(seatVersion, 7))
		self.keyboard = registry.bind(found[manager.name][0], manager, 1).create_virtual_keyboard(seat)
		fd = waylandKeyboard._keymapFile(text)
		self.keyboard.keymap(waylandKeyboard.WL_KEYMAP_FORMAT_XKB_V1, fd, len(text.encode()) + 1)
		os.close(fd)
		self.keyboard.modifiers(0, 0, 0, 0)
		self.display.roundtrip()

	def tap(self, evdev: int = 42) -> None:
		"""A key down and up, Left Shift by default, after which the seat's keymap is this keyboard's."""
		for state in (waylandKeyboard.PRESSED, waylandKeyboard.RELEASED):
			self.keyboard.key(int(time.monotonic() * 1000) & 0xFFFFFFFF, evdev, state)
			self.display.roundtrip()

	def close(self) -> None:
		self.keyboard.destroy()
		self.display.roundtrip()
		self.display.disconnect()


def swaySwitch(index: int) -> None:
	subprocess.run(
		["swaymsg", f"input 0:0:wlr_virtual_keyboard_v1 xkb_switch_layout {index}"],
		capture_output=True,
		timeout=5,
		check=True,
	)


@contextmanager
def swayKeyboard(text: str) -> Iterator[TestKeyboard]:
	keyboard = TestKeyboard(text)
	try:
		keyboard.tap()
		yield keyboard
	finally:
		try:
			swaySwitch(0)
		finally:
			keyboard.close()
			# The rig's keyboard is the seat's again once it types.
			waylandKeyboard.press("Shift_L", gap=0)


#: sway's own feed, in front of which the module stands.
SWAY_CHILD = r"""
feed = waylandKeymap.create(None)
first = feed.start(onChange)
result = {"first": plain(first), "follows": follows(waylandKeymapDump.readKeymap(5), first)}
started()
deadline = time.monotonic() + 5
while not changes and time.monotonic() < deadline:
	time.sleep(0.01)
feed.stop()
result["changes"] = [plain(s) for _at, _thread, s in changes]
result["threads"] = feedThreads()
"""


@pytest.mark.rigSession("sway")
def test_swaysOptionsItsIPCDoesNotSay(tmp_path: Path):
	"""A keyboard whose keymap has caps:escape and grp:alt_shift_toggle, switched to US English: sway's
	feed gives its layouts and group with no options, and the feed names the keymap sway gives programs
	with the options it carries; a switch back is followed."""
	from xkbcommon import xkb

	names = ("evdev", "pc105", "pl,us", ",", SWAY_OPTIONS)
	text = xkb.Context().keymap_new_from_names(*names).get_as_string()
	with swayKeyboard(text) as keyboard:
		swaySwitch(1)
		keyboard.tap()
		time.sleep(0.2)
		child = Child(tmp_path, SWAY_CHILD, env={"XKB_DEFAULT_OPTIONS": None})
		child.waitStarted()
		swaySwitch(0)
		result = child.go()
	value = result.value
	print(f"{harness.rigSession()}: {value}")
	first = value["first"]
	assert first[:2] == [list(names), 1]
	assert first[4] == "sway"
	assert value["follows"] == {"described": True, "tables": True}
	assert value["changes"] == [[list(names), 0, *first[2:]]]
	assert value["threads"] == []
	assert "Keyboard layout: named from the compositor's keymap: pl,us, in use us" in moduleRecords(result)


@pytest.mark.rigSession("sway")
def test_aKeymapNoNamesMakeLeavesTheDesktopsLayout(tmp_path: Path):
	"""A keymap the user wrote, as sway's xkb_file gives one: sway's own feed's layout stands, and the
	log says once how many keys differ."""
	from xkbcommon import xkb

	text = xkb.Context().keymap_new_from_names("evdev", "pc105", "pl").get_as_string()
	edited = re.sub(r"(key <AC01>[^\[]*\[\s*)(a|0x61)\b", r"\g<1>0x62", text, count=1)
	assert edited != text
	with swayKeyboard(edited):
		time.sleep(0.2)
		result = runModule(
			tmp_path,
			r"""
feed = waylandKeymap.create(None)
first = feed.start(onChange)
feed.stop()
result = plain(first)
""",
		)
	assert result.value[0] == ["evdev", "pc105", "pl", "", ""]
	assert result.value[4] == "sway"
	records = moduleRecords(result)
	assert (
		"Keyboard layout: no names make the compositor's keymap, so the desktop's layout stands; 1 keys differ"
		in records
	), records
