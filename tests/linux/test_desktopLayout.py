"""The keyboard layout of a session with no X server, read from the desktop's own settings: GNOME's
input sources from GSettings, KWin's kxkbrc, kcminputrc and org.kde.keyboard, localed, and the
XKB_DEFAULT variables. Each feed runs in a child with the rig's memory GSettings backend and
gsettings-desktop-schemas from the store; stand-ins for org.kde.keyboard and localed answer on the
rig's private session bus. Run under the rig:

	nix develop . -c tests/linux/rig python3 -m pytest tests/linux/test_desktopLayout.py
"""

import os
import re
import select
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import harness
import pytest

#: Within which a change must reach onChange while the main thread is kept busy.
CHANGE_BOUND = 0.1
#: Changes each busy test makes of each kind.
SWITCHES = 5
DEBUG = 10
US_NOTICE = "NVDA cannot find this desktop's keyboard layout, so it reads keys as US English."
LOCK_NUM = 1
#: GNOME's defaults in gsettings-desktop-schemas: repeat on after 500 ms every 30 ms, Num Lock
#: remembered and off.
GNOME_REPEAT = [True, 0.5, 0.03]
GNOME_MODEL = "pc105+inet"
#: KWin's defaults when kcminputrc says nothing.
KDE_REPEAT = [True, 0.6, 0.04]
#: What the tests set that is not a layout's name or where it came from, none of which may be logged.
UNLOGGED = ("caps:none", "grp:alt_shift_toggle", "anthy", "0.3", "300", "0.25", "250", "pc104", "pc105")
#: Every record the module may make.
ALLOWED_RECORDS = [
	re.compile(p)
	for p in (
		r"Keyboard layout from (gnome|kde|localed|environment|sway|keymap): [\w-]+( [\w-]+)?",
		r"Keyboard layout: no (waylandKeymap|swayLayout|gamescopeLayout) module",
		r"Keyboard layout: the (waylandKeymap|swayLayout|gamescopeLayout) module could not be loaded",
		r"Keyboard layout: taken from the (waylandKeymap|swayLayout|gamescopeLayout) module",
		r"Keyboard layout: the (waylandKeymap|swayLayout|gamescopeLayout) feed did not start",
		r"Keyboard layout: GNOME's input sources schema is not installed",
		r"Keyboard layout: GSettings has no dconf module here, so GNOME's input sources cannot be read",
		r"Keyboard layout: no system bus to ask localed on: .*",
		r"Keyboard layout: localed did not answer: .*",
		r"Keyboard layout: localed names no layout",
		r"Keyboard layout: KWin did not say which layout is in use: .*",
	)
]

#: Before each child's code: the module, a recording onChange, and setters for the memory backend.
PRELUDE = r"""
import os, threading, time
from logHandler import log
log.setLevel(log.DEBUG)
import gi
gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib
from linuxPlatform.keys import desktopLayout

changes = []


def onChange(snapshot):
	changes.append((time.monotonic(), threading.current_thread().name, snapshot))


def plain(snapshot):
	repeat = None if snapshot.repeat is None else list(snapshot.repeat)
	return [
		list(snapshot.names), snapshot.group, repeat, snapshot.locksGuess, snapshot.source,
		snapshot.inputMethod, snapshot.notice,
	]


GNOME_KEYS = {
	"sources": ("input", "sources", "a(ss)"),
	"mru": ("input", "mru-sources", "a(ss)"),
	"options": ("input", "xkb-options", "as"),
	"model": ("input", "xkb-model", "s"),
	"repeat": ("keyboard", "repeat", "b"),
	"delay": ("keyboard", "delay", "u"),
	"interval": ("keyboard", "repeat-interval", "u"),
	"remember": ("keyboard", "remember-numlock-state", "b"),
	"numlock": ("keyboard", "numlock-state", "b"),
}


def gnomeSettings():
	return {
		"input": Gio.Settings.new(desktopLayout.INPUT_SOURCES_SCHEMA),
		"keyboard": Gio.Settings.new(desktopLayout.KEYBOARD_SCHEMA),
	}


def gnome(**values):
	settings = gnomeSettings()
	for which, key, _type in GNOME_KEYS.values():
		settings[which].reset(key)
	setGnome(settings, **values)


def setGnome(settings, **values):
	for name, value in values.items():
		which, key, valueType = GNOME_KEYS[name]
		settings[which].set_value(key, GLib.Variant(valueType, value))


def started(feed):
	snapshot = feed.start(onChange)
	feed.stop()
	return plain(snapshot)


def busy(seconds):
	end = time.monotonic() + seconds
	count = 0
	while time.monotonic() < end:
		count += 1


def feedThreads():
	return [t.name for t in threading.enumerate() if t.name == "lindaKeyboardLayout"]
"""

#: A stand-in, on the session bus, for KWin's org.kde.keyboard or for localed, by its first argument.
STAND_IN = r"""
import sys
import gi
gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib

kind = sys.argv[1]
if kind == "kde":
	NAME, PATH, INTERFACE = "org.kde.keyboard", "/Layouts", "org.kde.KeyboardLayouts"
	XML = '''<node><interface name="org.kde.KeyboardLayouts">
		<method name="getLayout"><arg direction="out" type="u"/></method>
		<method name="setLayout"><arg direction="in" type="u"/><arg direction="out" type="b"/></method>
		<method name="reconfigureForTest"/>
		<signal name="layoutChanged"><arg type="u"/></signal>
		<signal name="layoutListChanged"/>
	</interface></node>'''
	state = {"layout": int(sys.argv[2])}
else:
	NAME, PATH, INTERFACE = "org.freedesktop.locale1", "/org/freedesktop/locale1", "org.freedesktop.DBus.Properties"
	XML = '''<node><interface name="org.freedesktop.DBus.Properties">
		<method name="GetAll"><arg direction="in" type="s"/><arg direction="out" type="a{sv}"/></method>
	</interface></node>'''
	properties = dict(zip(("X11Layout", "X11Model", "X11Variant", "X11Options"), sys.argv[2:6]))


def onCall(connection, sender, path, interface, method, parameters, invocation):
	if method == "getLayout":
		invocation.return_value(GLib.Variant("(u)", (state["layout"],)))
	elif method == "setLayout":
		(state["layout"],) = parameters.unpack()
		connection.emit_signal(None, PATH, INTERFACE, "layoutChanged", GLib.Variant("(u)", (state["layout"],)))
		invocation.return_value(GLib.Variant("(b)", (True,)))
	elif method == "reconfigureForTest":
		connection.emit_signal(None, PATH, INTERFACE, "layoutListChanged", None)
		invocation.return_value(None)
	else:
		values = {name: GLib.Variant("s", value) for name, value in properties.items()}
		invocation.return_value(GLib.Variant("(a{sv})", (values,)))


connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
connection.register_object(PATH, Gio.DBusNodeInfo.new_for_xml(XML).interfaces[0], onCall, None, None)
(reply,) = connection.call_sync(
	"org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "RequestName",
	GLib.Variant("(su)", (NAME, 4)), GLib.VariantType.new("(u)"), 0, 2000, None,
).unpack()
assert reply == 1, reply
print("ready", flush=True)
GLib.MainLoop().run()
"""


@contextmanager
def standIn(*arguments: str) -> Iterator[None]:
	process = subprocess.Popen(
		[sys.executable, "-I", "-c", STAND_IN, *arguments],
		stdout=subprocess.PIPE,
		text=True,
	)
	try:
		readable, _w, _x = select.select([process.stdout], [], [], 10)
		assert readable and process.stdout.readline().strip() == "ready", "the stand-in did not start"
		yield
	finally:
		process.terminate()
		process.wait(10)


def snap(
	names: tuple[str, str, str, str, str],
	group: int = 0,
	repeat: list | None = None,
	locks: int | None = None,
	source: str = "gnome",
	inputMethod: bool = False,
	notice: str | None = None,
) -> list:
	return [list(names), group, repeat, locks, source, inputMethod, notice]


def runFeeds(tmp_path: Path, code: str, env: dict[str, str | None] | None = None, **clean) -> object:
	result = harness.runChild(tmp_path, PRELUDE + code, env=env, recordLevel=DEBUG, timeout=120)
	result.assertClean(**clean)
	assertOnlyNamesLogged(result)
	return result


def moduleRecords(result) -> list[str]:
	return [
		e["message"]
		for e in result.events
		if e["kind"] == "log" and (e.get("codepath") or "").startswith("linuxPlatform.keys.desktopLayout")
	]


def assertOnlyNamesLogged(result) -> None:
	records = moduleRecords(result)
	assert records, "the module logged nothing, so the check below would prove nothing"
	for message in records:
		assert any(p.fullmatch(message) for p in ALLOWED_RECORDS), f"an unexpected record: {message!r}"
		for value in UNLOGGED:
			assert value not in message, f"{value!r} was logged: {message!r}"


def noSystemBus() -> dict[str, str | None]:
	return {"XKB_DEFAULT_LAYOUT": None, "XKB_DEFAULT_VARIANT": None, "XKB_DEFAULT_OPTIONS": None}


def test_gnomeSnapshots(tmp_path: Path):
	result = runFeeds(
		tmp_path,
		r"""
result = {}
for name, values in {
	"single": {"sources": [("xkb", "pl")]},
	"recent": {"sources": [("xkb", "pl"), ("xkb", "us")], "mru": [("xkb", "us"), ("xkb", "pl")]},
	"recentGone": {"sources": [("xkb", "pl"), ("xkb", "us")], "mru": [("xkb", "de")]},
	"recentGoneOfMany": {
		"sources": [("xkb", "pl"), ("xkb", "us")],
		"mru": [("xkb", "de"), ("xkb", "us"), ("xkb", "pl")],
	},
	"recentGoneOneLeft": {"sources": [("xkb", "pl"), ("xkb", "us")], "mru": [("xkb", "de"), ("xkb", "us")]},
	"variant": {"sources": [("xkb", "de+nodeadkeys")], "model": "pc104"},
	"capsNone": {"sources": [("xkb", "pl")], "options": ["caps:none"]},
	"empty": {},
	"ibus": {"sources": [("xkb", "pl"), ("ibus", "anthy")], "mru": [("ibus", "anthy"), ("xkb", "pl")]},
	"keyboard": {"sources": [("xkb", "pl")], "repeat": False, "delay": 250, "interval": 40, "numlock": True},
	"forgetful": {"sources": [("xkb", "pl")], "remember": False, "numlock": True},
}.items():
	gnome(**values)
	result[name] = started(desktopLayout.GnomeLayoutFeed())
result["threads"] = feedThreads()
""",
		env=noSystemBus(),
	)
	pl = ("evdev", GNOME_MODEL, "pl", "", "")
	assert result.value == {
		"single": snap(pl, repeat=GNOME_REPEAT, locks=0),
		"recent": snap(("evdev", GNOME_MODEL, "us", "", ""), repeat=GNOME_REPEAT, locks=0),
		"recentGone": snap(pl, repeat=GNOME_REPEAT, locks=0),
		# The shell passes over mru entries that are no longer sources, and goes by the list only while
		# more than one entry is left (keyboard.js, _updateMruSources).
		"recentGoneOfMany": snap(("evdev", GNOME_MODEL, "us", "", ""), repeat=GNOME_REPEAT, locks=0),
		"recentGoneOneLeft": snap(pl, repeat=GNOME_REPEAT, locks=0),
		"variant": snap(("evdev", "pc104", "de", "nodeadkeys", ""), repeat=GNOME_REPEAT, locks=0),
		"capsNone": snap(("evdev", GNOME_MODEL, "pl", "", "caps:none"), repeat=GNOME_REPEAT, locks=0),
		# No source: localed's layout, which the rig has no system bus for, then US English.
		"empty": snap(
			("evdev", "pc105", "us", "", ""),
			repeat=GNOME_REPEAT,
			locks=0,
			source="environment",
			notice=US_NOTICE,
		),
		"ibus": snap(pl, repeat=GNOME_REPEAT, locks=0, inputMethod=True),
		"keyboard": snap(pl, repeat=[False, 0.25, 0.04], locks=LOCK_NUM),
		"forgetful": snap(pl, repeat=GNOME_REPEAT, locks=None),
		"threads": [],
	}


def test_gnomeWithNoSourcesTakesLocaled(tmp_path: Path):
	with standIn("localed", "de", "pc104", "nodeadkeys", ""):
		result = runFeeds(
			tmp_path,
			r"""
gnome(repeat=False)
result = started(desktopLayout.GnomeLayoutFeed())
""",
			env={"DBUS_SYSTEM_BUS_ADDRESS": os.environ["DBUS_SESSION_BUS_ADDRESS"]},
		)
	assert result.value == snap(
		("evdev", "pc104", "de", "nodeadkeys", ""),
		repeat=[False, 0.5, 0.03],
		locks=0,
		source="localed",
	)


def test_gnomeChangesReachOnChangeWhileTheMainThreadIsBusy(tmp_path: Path):
	result = runFeeds(
		tmp_path,
		r"""
gnome(sources=[("xkb", "pl"), ("xkb", "us")], mru=[("xkb", "pl"), ("xkb", "us")])
feed = desktopLayout.GnomeLayoutFeed()
first = feed.start(onChange)
settings = gnomeSettings()
asked = []
for turn in range(SWITCHES):
	recent = [("xkb", "us"), ("xkb", "pl")] if turn % 2 == 0 else [("xkb", "pl"), ("xkb", "us")]
	for name, value in (("mru", recent), ("delay", 300 + turn)):
		asked.append(time.monotonic())
		setGnome(settings, **{name: value})
		busy(0.3)
feed.stop()
setGnome(settings, mru=[("xkb", "pl"), ("xkb", "us")])
time.sleep(0.3)
result = {
	"first": plain(first),
	"changes": [[t, name, plain(s)] for t, name, s in changes],
	"asked": asked,
	"threads": feedThreads(),
}
""".replace("SWITCHES", str(SWITCHES)),
		env=noSystemBus(),
	)
	value = result.value
	assert value["first"] == snap(("evdev", GNOME_MODEL, "pl", "", ""), repeat=GNOME_REPEAT, locks=0)
	expected = []
	for turn in range(SWITCHES):
		layout = "us" if turn % 2 == 0 else "pl"
		names = ("evdev", GNOME_MODEL, layout, "", "")
		expected.append(snap(names, repeat=[True, (300 + turn - 1) / 1000 if turn else 0.5, 0.03], locks=0))
		expected.append(snap(names, repeat=[True, (300 + turn) / 1000, 0.03], locks=0))
	assert [c[1:] for c in value["changes"]] == [["lindaKeyboardLayout", s] for s in expected]
	delays = [c[0] - t for c, t in zip(value["changes"], value["asked"])]
	assert max(delays) < CHANGE_BOUND, delays
	assert value["threads"] == []


def test_gnomeWithoutItsSchemaTakesTheSystemLayout(tmp_path: Path):
	dataDirs = ":".join(
		d for d in os.environ["XDG_DATA_DIRS"].split(":") if "gsettings-desktop-schemas" not in d
	)
	result = runFeeds(
		tmp_path,
		r"""
schemas = Gio.SettingsSchemaSource.get_default()
assert schemas is None or schemas.lookup(desktopLayout.INPUT_SOURCES_SCHEMA, True) is None
result = started(desktopLayout.GnomeLayoutFeed())
""",
		env={"XDG_DATA_DIRS": dataDirs, "XKB_DEFAULT_LAYOUT": "pl"},
	)
	assert result.value == snap(("evdev", "pc105", "pl", "", ""), source="environment")
	assert "Keyboard layout: GNOME's input sources schema is not installed" in moduleRecords(result)


#: What GLib prints when GSETTINGS_BACKEND names a module it has not got, or it falls to memory.
BACKEND_MESSAGES = re.compile(
	r"\n?\(process:\d+\): GLib-GIO-WARNING \*\*: [\d:.]+: Can't find module 'dconf' specified in "
	r"GSETTINGS_BACKEND\n"
	r"|\n?GLib-GIO-Message: [\d:.]+: Using the 'memory' GSettings backend\.  Your settings will not be "
	r"saved or shared with other applications\.\n"
)
NO_DCONF = "Keyboard layout: GSettings has no dconf module here, so GNOME's input sources cannot be read"
BACKEND_CODE = r"""
from gi.repository import GObject
result = {
	"backend": GObject.type_name(Gio.SettingsBackend.get_default()),
	"snapshot": started(desktopLayout.GnomeLayoutFeed()),
}
"""


@pytest.mark.parametrize("asked", [None, "dconf"])
def test_gnomeOnABackendThatIsNotDconfNobodyAskedFor(tmp_path: Path, asked: str | None):
	"""GLib falls back on a key file of its own, or on memory, when it has no dconf module: neither is
	what GNOME's shell reads."""
	result = runFeeds(
		tmp_path,
		BACKEND_CODE,
		env={
			"GSETTINGS_BACKEND": asked,
			"GIO_EXTRA_MODULES": None,
			"XKB_DEFAULT_LAYOUT": "pl",
			"XDG_CONFIG_HOME": str(tmp_path / "config"),
		},
		ignoring=("stderr",),
	)
	assert result.value["backend"] in ("GKeyfileSettingsBackend", "GMemorySettingsBackend")
	assert result.value["snapshot"] == snap(("evdev", "pc105", "pl", "", ""), source="environment")
	assert NO_DCONF in moduleRecords(result)
	assert not harness.unexpectedStandardError(BACKEND_MESSAGES.sub("", result.stderr)), result.stderr


def test_gnomeOnABackendAskedFor(tmp_path: Path):
	result = runFeeds(
		tmp_path,
		BACKEND_CODE,
		env={**noSystemBus(), "GSETTINGS_BACKEND": "keyfile", "XDG_CONFIG_HOME": str(tmp_path / "config")},
	)
	assert result.value["backend"] == "GKeyfileSettingsBackend"
	assert result.value["snapshot"] == snap(
		("evdev", "pc105", "us", "", ""),
		repeat=GNOME_REPEAT,
		locks=0,
		source="environment",
		notice=US_NOTICE,
	)
	assert NO_DCONF not in moduleRecords(result)


def writeConfig(directory: Path, name: str, text: str) -> None:
	directory.mkdir(parents=True, exist_ok=True)
	(directory / name).write_text(text)


PLASMA_CODE = r"""
from pathlib import Path
home = Path(os.environ["XDG_CONFIG_HOME"])
system = Path(os.environ["XDG_CONFIG_DIRS"])


def write(directory, name, text):
	directory.mkdir(parents=True, exist_ok=True)
	(directory / name).write_text(text)


def clear():
	for directory in (home, system):
		for name in ("kxkbrc", "kcminputrc"):
			(directory / name).unlink(missing_ok=True)


result = {}
clear()
write(home, "kxkbrc", "[Layout]\nLayoutList=pl,us\nVariantList=,\nModel=pc105\n"
	"Options=caps:none,grp:alt_shift_toggle\nResetOldOptions=true\nUse=true\n")
write(home, "kcminputrc", "[Keyboard]\nKeyRepeat=repeat\nRepeatDelay=300\nRepeatRate=30\nNumLock=0\n")
result["full"] = started(desktopLayout.PlasmaLayoutFeed())
clear()
write(home, "kxkbrc", "[Layout]\nUse=false\nLayoutList=pl\nOptions=caps:none\n")
write(home, "kcminputrc", "[Keyboard]\nKeyRepeat=accent\nNumLock=1\n")
result["useFalse"] = started(desktopLayout.PlasmaLayoutFeed())
clear()
result["nothing"] = started(desktopLayout.PlasmaLayoutFeed())
os.environ["XKB_DEFAULT_LAYOUT"] = "fr"
result["environment"] = started(desktopLayout.PlasmaLayoutFeed())
del os.environ["XKB_DEFAULT_LAYOUT"]
write(system, "kxkbrc", "[Layout]\nLayoutList=de,us\nModel=pc105\n")
write(home, "kxkbrc", "# The user's own\n[Layout][$i]\nVariantList=nodeadkeys,\n")
result["cascade"] = started(desktopLayout.PlasmaLayoutFeed())
clear()
os.environ["XKB_DEFAULT_VARIANT"] = "dvorak"
os.environ["XKB_DEFAULT_OPTIONS"] = "grp:alt_shift_toggle"
write(home, "kxkbrc", "[Layout]\nLayoutList=pl\nResetOldOptions=true\n")
result["environmentUnused"] = started(desktopLayout.PlasmaLayoutFeed())
write(home, "kxkbrc", "[Layout]\nOptions=caps:none\nResetOldOptions=Yes\n")
result["variantWithoutLayout"] = started(desktopLayout.PlasmaLayoutFeed())
os.environ["XKB_DEFAULT_LAYOUT"] = "fr"
result["environmentLayout"] = started(desktopLayout.PlasmaLayoutFeed())
write(home, "kxkbrc", "[Layout]\nLayoutList=pl\nOptions=caps:none\nResetOldOptions=off\n")
result["environmentOptions"] = started(desktopLayout.PlasmaLayoutFeed())
for name in ("XKB_DEFAULT_LAYOUT", "XKB_DEFAULT_VARIANT", "XKB_DEFAULT_OPTIONS"):
	del os.environ[name]
result["threads"] = feedThreads()
"""


def test_plasmaSnapshots(tmp_path: Path):
	with standIn("kde", "1"):
		result = runFeeds(
			tmp_path,
			PLASMA_CODE,
			env={
				**noSystemBus(),
				"XDG_CONFIG_HOME": str(tmp_path / "config"),
				"XDG_CONFIG_DIRS": str(tmp_path / "system"),
			},
		)
	assert result.value == {
		"full": snap(
			("evdev", "pc105", "pl,us", ",", "caps:none,grp:alt_shift_toggle"),
			group=1,
			repeat=[True, 0.3, 1 / 30],
			locks=LOCK_NUM,
			source="kde",
		),
		# KWin reads no Use key, and takes Options only with ResetOldOptions; the stand-in's group 1
		# is past the single layout.
		"useFalse": snap(("evdev", "pc104", "pl", "", ""), repeat=[False, 0.6, 0.04], locks=0, source="kde"),
		"nothing": snap(("evdev", "pc104", "us", "", ""), repeat=KDE_REPEAT, source="kde"),
		"environment": snap(("evdev", "pc104", "fr", "", ""), repeat=KDE_REPEAT, source="kde"),
		"cascade": snap(
			("evdev", "pc105", "de,us", "nodeadkeys,", ""), group=1, repeat=KDE_REPEAT, source="kde"
		),
		# KWin's applyEnvironmentRules takes the variant only with the layout, and the options only
		# when ResetOldOptions, read as KConfig reads a bool, is false; xkbcommon then drops a variant
		# that came without a layout.
		"environmentUnused": snap(("evdev", "pc104", "pl", "", ""), repeat=KDE_REPEAT, source="kde"),
		"variantWithoutLayout": snap(
			("evdev", "pc104", "us", "", "caps:none"), repeat=KDE_REPEAT, source="kde"
		),
		"environmentLayout": snap(
			("evdev", "pc104", "fr", "dvorak", "caps:none"), repeat=KDE_REPEAT, source="kde"
		),
		"environmentOptions": snap(
			("evdev", "pc104", "pl", "", "grp:alt_shift_toggle"), repeat=KDE_REPEAT, source="kde"
		),
		"threads": [],
	}


def test_plasmaWithoutKWinTakesTheFirstLayout(tmp_path: Path):
	writeConfig(tmp_path / "config", "kxkbrc", "[Layout]\nLayoutList=pl,us\n")
	result = runFeeds(
		tmp_path,
		r"""
result = started(desktopLayout.PlasmaLayoutFeed())
""",
		env={"XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_CONFIG_DIRS": str(tmp_path / "system")},
	)
	assert result.value == snap(("evdev", "pc104", "pl,us", "", ""), repeat=KDE_REPEAT, source="kde")
	assert any(r.startswith("Keyboard layout: KWin did not say") for r in moduleRecords(result))


PLASMA_LAYOUTS = {
	"three": ("[Layout]\nLayoutList=de,pl,us\n", ("evdev", "pc104", "de,pl,us", "", "")),
	"two": ("[Layout]\nLayoutList=pl,us\nVariantList=,\n", ("evdev", "pc104", "pl,us", ",", "")),
}


def test_plasmaChangesReachOnChangeWhileTheMainThreadIsBusy(tmp_path: Path):
	writeConfig(tmp_path / "config", "kxkbrc", PLASMA_LAYOUTS["two"][0])
	with standIn("kde", "0"):
		result = runFeeds(
			tmp_path,
			rf"""
from pathlib import Path
layouts = {PLASMA_LAYOUTS!r}
feed = desktopLayout.PlasmaLayoutFeed()
first = feed.start(onChange)
bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)


def call(method, parameters=None):
	bus.call_sync(
		"org.kde.keyboard", "/Layouts", "org.kde.KeyboardLayouts", method, parameters, None, 0, 2000, None
	)


asked = []
for turn in range({SWITCHES}):
	asked.append(time.monotonic())
	call("setLayout", GLib.Variant("(u)", (1 - turn % 2,)))
	busy(0.3)
	(Path(os.environ["XDG_CONFIG_HOME"]) / "kxkbrc").write_text(layouts["three" if turn % 2 == 0 else "two"][0])
	asked.append(time.monotonic())
	call("reconfigureForTest")
	busy(0.3)
feed.stop()
call("setLayout", GLib.Variant("(u)", (1,)))
time.sleep(0.3)
result = {{
	"first": plain(first),
	"changes": [[t, name, plain(s)] for t, name, s in changes],
	"asked": asked,
	"threads": feedThreads(),
}}
""",
			env={"XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_CONFIG_DIRS": str(tmp_path / "system")},
		)
	value = result.value
	two, three = PLASMA_LAYOUTS["two"][1], PLASMA_LAYOUTS["three"][1]
	assert value["first"] == snap(two, repeat=KDE_REPEAT, source="kde")
	expected = []
	for turn in range(SWITCHES):
		group = 1 - turn % 2
		expected.append(snap(two if turn % 2 == 0 else three, group=group, repeat=KDE_REPEAT, source="kde"))
		expected.append(snap(three if turn % 2 == 0 else two, group=group, repeat=KDE_REPEAT, source="kde"))
	assert [c[1:] for c in value["changes"]] == [["lindaKeyboardLayout", s] for s in expected]
	delays = [c[0] - t for c, t in zip(value["changes"], value["asked"])]
	assert max(delays) < CHANGE_BOUND, delays
	assert value["threads"] == []


def test_localedThenTheEnvironment(tmp_path: Path):
	code = r"""
result = started(desktopLayout.LocaledLayoutFeed())
"""
	systemBus = {"DBUS_SYSTEM_BUS_ADDRESS": os.environ["DBUS_SESSION_BUS_ADDRESS"]}
	with standIn("localed", "pl,us", "", ",dvorak", "grp:alt_shift_toggle"):
		named = runFeeds(tmp_path, code, env=systemBus).value
	with standIn("localed", "", "", "", ""):
		unnamed = runFeeds(tmp_path, code, env={**systemBus, "XKB_DEFAULT_LAYOUT": "cz"}).value
	noBus = runFeeds(tmp_path, code, env=noSystemBus()).value
	assert named == snap(("evdev", "pc105", "pl,us", ",dvorak", "grp:alt_shift_toggle"), source="localed")
	assert unnamed == snap(("evdev", "pc105", "cz", "", ""), source="environment")
	assert noBus == snap(("evdev", "pc105", "us", "", ""), source="environment", notice=US_NOTICE)


def test_environment(tmp_path: Path):
	result = runFeeds(
		tmp_path,
		r"""
result = [started(desktopLayout.EnvironmentLayoutFeed())]
for name in ("XKB_DEFAULT_RULES", "XKB_DEFAULT_MODEL", "XKB_DEFAULT_LAYOUT", "XKB_DEFAULT_VARIANT", "XKB_DEFAULT_OPTIONS"):
	os.environ.pop(name)
result.append(started(desktopLayout.EnvironmentLayoutFeed()))
os.environ["XKB_DEFAULT_VARIANT"] = "dvorak"
result.append(started(desktopLayout.EnvironmentLayoutFeed()))
""",
		env={
			"XKB_DEFAULT_RULES": "evdev",
			"XKB_DEFAULT_MODEL": "pc104",
			"XKB_DEFAULT_LAYOUT": "pl,us",
			"XKB_DEFAULT_VARIANT": ",dvorak",
			"XKB_DEFAULT_OPTIONS": "grp:alt_shift_toggle",
		},
	)
	assert result.value == [
		snap(("evdev", "pc104", "pl,us", ",dvorak", "grp:alt_shift_toggle"), source="environment"),
		snap(("evdev", "pc105", "us", "", ""), source="environment", notice=US_NOTICE),
		# A variant with no layout is xkbcommon's to ignore too.
		snap(("evdev", "pc105", "us", "", ""), source="environment", notice=US_NOTICE),
	]


FAKE_FEED = r"""
from linuxPlatform.keys.desktopLayout import LayoutSnapshot


class Feed:
	def __init__(self, broken):
		self.broken = broken

	def start(self, onChange):
		if self.broken:
			raise RuntimeError("a feed that cannot start")
		return LayoutSnapshot(("evdev", "pc105", "pl", "", ""), source=SOURCE)

	def stop(self):
		pass


def create(currentDesktop):
	if currentDesktop in ("sway", "broken"):
		return Feed(currentDesktop == "broken")
	return None
"""


def test_createChoosesOptionalModulesFirst(tmp_path: Path):
	fakes = tmp_path / "fakes"
	fakes.mkdir()
	result = runFeeds(
		tmp_path,
		rf"""
import importlib
import linuxPlatform.keys

def kind(desktop):
	return type(desktopLayout.create(desktop)).__name__

result = {{"absent": {{d: kind(d) for d in ("GNOME", "ubuntu:GNOME", "KDE", "sway", "", None)}}}}
fakes = {str(fakes)!r}
with open(fakes + "/swayLayout.py", "w") as f:
	f.write({FAKE_FEED!r}.replace("SOURCE", repr("sway")))
with open(fakes + "/gamescopeLayout.py", "w") as f:
	f.write("import aModuleThatIsNotThere\n")
linuxPlatform.keys.__path__.append(fakes)
importlib.invalidate_caches()
os.environ["XKB_DEFAULT_LAYOUT"] = "de"
result["sway"] = started(desktopLayout.create("sway"))
result["gnome"] = kind("GNOME")
result["broken"] = started(desktopLayout.create("broken"))
with open(fakes + "/waylandKeymap.py", "w") as f:
	f.write({FAKE_FEED!r}.replace("SOURCE", repr("keymap")))
importlib.invalidate_caches()
result["keymap"] = started(desktopLayout.create("sway"))
""",
		env=noSystemBus(),
	)
	assert result.value["absent"] == {
		"GNOME": "GnomeLayoutFeed",
		"ubuntu:GNOME": "GnomeLayoutFeed",
		"KDE": "PlasmaLayoutFeed",
		"sway": "LocaledLayoutFeed",
		"": "LocaledLayoutFeed",
		"null": "LocaledLayoutFeed",
	}
	pl = ("evdev", "pc105", "pl", "", "")
	assert result.value["sway"] == snap(pl, source="sway")
	assert result.value["gnome"] == "GnomeLayoutFeed"
	# A module's feed that fails to start gives way to the desktop's.
	assert result.value["broken"] == snap(("evdev", "pc105", "de", "", ""), source="environment")
	assert result.value["keymap"] == snap(pl, source="keymap")
	records = moduleRecords(result)
	for name in ("waylandKeymap", "swayLayout", "gamescopeLayout"):
		assert f"Keyboard layout: no {name} module" in records
	# A module that is there but fails its own imports is broken, not absent: both are absent only for
	# the six desktops asked before the fakes were written.
	absent = [
		records.count(f"Keyboard layout: no {name} module") for name in ("swayLayout", "gamescopeLayout")
	]
	assert absent == [6, 6]
	assert "Keyboard layout: the gamescopeLayout module could not be loaded" in records
	assert "Keyboard layout: taken from the swayLayout module" in records
	assert "Keyboard layout: the swayLayout feed did not start" in records
