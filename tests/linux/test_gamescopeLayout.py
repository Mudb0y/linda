"""The layout gamescope gives every keyboard, for a session forced onto the Wayland route under
gamescope: Steam's GAMESCOPE_KEYBOARD_LAYOUT on the root window of gamescope's Xwayland, in place of the
XKB_DEFAULT variables' layout and variant as gamescope 3.17 takes it, followed through PropertyNotify.

The rig has no gamescope, so its Xvfb carries the property; the tests force the Wayland session and name
gamescope the way its session does. Run under the rig:

	nix develop . -c tests/linux/rig python3 -m pytest tests/linux/test_gamescopeLayout.py
"""

import re
from pathlib import Path

import harness
import pytest

# Forced onto the Wayland route while the rig gives Xvfb, which a compositor's rig does not.
pytestmark = pytest.mark.rigSession("x11")

#: Within which a change must reach onChange while the main thread is kept busy for 0.3 s at a time.
CHANGE_BOUND = 0.25
#: How long a change that must not come is waited for.
QUIET = 1.0
DEBUG = 10
US_NOTICE = "NVDA cannot find this desktop's keyboard layout, so it reads keys as US English."
#: Every record the module may make.
ALLOWED_RECORDS = [
	re.compile(p)
	for p in (
		r"Keyboard layout from (gamescope|environment): [\w-]+( [\w-]+)?",
		r"Keyboard layout: gamescope's Xwayland names a layout that does not compile",
		r"Keyboard layout: gamescope's Xwayland closed the connection",
	)
]
#: What the tests set that is neither a layout's name nor where it came from, none of which may be
#: logged.
UNLOGGED = ("caps:none", "pc104", "pc105", "evdev")

GAMESCOPE = {
	"LINDA_SESSION_TYPE": "wayland",
	"XDG_CURRENT_DESKTOP": "gamescope",
	"XKB_DEFAULT_RULES": "evdev",
	"XKB_DEFAULT_MODEL": "pc104",
	"XKB_DEFAULT_LAYOUT": "de",
	"XKB_DEFAULT_VARIANT": "nodeadkeys",
	"XKB_DEFAULT_OPTIONS": "caps:none",
}

#: Before each child's code: a recording onChange, and Steam's side, which writes the property on a
#: connection of its own as gamescope's set_string_prop does. The property outlives the child on the
#: rig's X server, so each child starts and ends without it.
PRELUDE = r"""
import os, threading, time
from logHandler import log
log.setLevel(log.DEBUG)
from Xlib import display
from linuxPlatform import displayServer
from linuxPlatform.keys import desktopLayout, gamescopeLayout

changes = []
steam = display.Display()
ROOT = steam.screen().root
PROPERTY = steam.intern_atom(gamescopeLayout.LAYOUT_PROPERTY)
UTF8_STRING = steam.intern_atom("UTF8_STRING")


def onChange(snapshot):
	changes.append((time.monotonic(), threading.current_thread().name, snapshot))


def plain(snapshot):
	repeat = None if snapshot.repeat is None else list(snapshot.repeat)
	return [
		list(snapshot.names), snapshot.group, repeat, snapshot.locksGuess, snapshot.source,
		snapshot.inputMethod, snapshot.notice,
	]


def setLayout(value):
	if value is None:
		ROOT.delete_property(PROPERTY)
	else:
		ROOT.change_property(PROPERTY, UTF8_STRING, 8, value.encode())
	steam.sync()


def busy(seconds):
	end = time.monotonic() + seconds
	count = 0
	while time.monotonic() < end:
		count += 1


def waitFor(count, seconds):
	end = time.monotonic() + seconds
	while len(changes) < count and time.monotonic() < end:
		time.sleep(0.01)


def feedThreads():
	return [t.name for t in threading.enumerate() if t.name == "lindaKeyboardLayout"]


def started(value):
	setLayout(value)
	feed = desktopLayout.create(os.environ.get("XDG_CURRENT_DESKTOP"))
	try:
		return plain(feed.start(onChange))
	finally:
		feed.stop()


setLayout(None)
"""

EPILOGUE = r"""
setLayout(None)
"""


def snap(
	names: tuple[str, str, str, str, str],
	source: str = "gamescope",
	notice: str | None = None,
) -> list:
	return [list(names), 0, None, None, source, False, notice]


def runFeed(tmp_path: Path, code: str, env: dict[str, str | None] | None = None) -> object:
	result = harness.runChild(
		tmp_path,
		PRELUDE + "try:\n" + _indent(code) + "\nfinally:\n" + _indent(EPILOGUE),
		env={**GAMESCOPE, **(env or {})},
		recordLevel=DEBUG,
		timeout=120,
	)
	result.assertClean()
	return result


def _indent(code: str) -> str:
	return "\n".join("\t" + line for line in code.strip("\n").splitlines()) or "\tpass"


def moduleRecords(result, module: str = "gamescopeLayout") -> list[str]:
	return [
		e["message"]
		for e in result.events
		if e["kind"] == "log" and (e.get("codepath") or "").startswith(f"linuxPlatform.keys.{module}")
	]


def assertOnlyNamesLogged(result) -> None:
	records = moduleRecords(result)
	assert records, "the module logged nothing, so the check below would prove nothing"
	for message in records:
		assert any(p.fullmatch(message) for p in ALLOWED_RECORDS), f"an unexpected record: {message!r}"
		for value in UNLOGGED:
			assert value not in message, f"{value!r} was logged: {message!r}"


def test_createIsOnlyForGamescopeOnTheWaylandRoute(tmp_path: Path):
	result = runFeed(
		tmp_path,
		r"""
def kind(**env):
	for name, value in env.items():
		if value is None:
			os.environ.pop(name, None)
		else:
			os.environ[name] = value
	displayServer._decision.cache_clear()
	return type(gamescopeLayout.create(os.environ.get("XDG_CURRENT_DESKTOP"))).__name__

result = {
	"desktop": kind(),
	"marker": kind(XDG_CURRENT_DESKTOP=None, GAMESCOPE_WAYLAND_DISPLAY="gamescope-0"),
	"sway": kind(XDG_CURRENT_DESKTOP="sway", GAMESCOPE_WAYLAND_DISPLAY=None),
	"gnome": kind(XDG_CURRENT_DESKTOP="GNOME"),
	"forcedX": kind(XDG_CURRENT_DESKTOP="gamescope", LINDA_SESSION_TYPE="x11"),
	# Rule 3: gamescope's marker and an X display keep the X route, which reads the X server's layout.
	"x11": kind(LINDA_SESSION_TYPE=None),
}
os.environ["LINDA_SESSION_TYPE"] = "wayland"
displayServer._decision.cache_clear()
feed = desktopLayout.create("gamescope")
result["chosen"] = type(feed._feed).__name__
""",
	)
	assert result.value == {
		"desktop": "GamescopeLayoutFeed",
		"marker": "GamescopeLayoutFeed",
		"sway": "NoneType",
		"gnome": "NoneType",
		"forcedX": "NoneType",
		"x11": "NoneType",
		"chosen": "GamescopeLayoutFeed",
	}
	assert "Keyboard layout: taken from the gamescopeLayout module" in moduleRecords(result, "desktopLayout")


def test_namesAreTheRootPropertysAndFollowIt(tmp_path: Path):
	result = runFeed(
		tmp_path,
		rf"""
setLayout("fr:bepo")
feed = desktopLayout.create(os.environ["XDG_CURRENT_DESKTOP"])
first = feed.start(onChange)
asked = []
for value in ("pl", "us:dvorak", None):
	asked.append(time.monotonic())
	setLayout(value)
	busy(0.3)
waitFor(3, 5)
setLayout("xx")
time.sleep({QUIET})
# A layout that does not compile is kept from gamescope, which goes on with the one before.
afterBroken = len(changes)
asked.append(time.monotonic())
setLayout("cz")
busy(0.3)
waitFor(4, 5)
feed.stop()
threads = feedThreads()
setLayout("pl")
time.sleep({QUIET})
result = {{
	"first": plain(first),
	"changes": [plain(c[2]) for c in changes],
	"threads": [c[1] for c in changes],
	"delays": [c[0] - t for c, t in zip(changes, asked)],
	"afterBroken": afterBroken,
	"left": threads,
}}
""",
	)
	value = result.value
	assert value["first"] == snap(("evdev", "pc104", "fr", "bepo", "caps:none"))
	assert value["changes"] == [
		# gamescope takes the variant with the layout, never the environment's.
		snap(("evdev", "pc104", "pl", "", "caps:none")),
		snap(("evdev", "pc104", "us", "dvorak", "caps:none")),
		snap(("evdev", "pc104", "de", "nodeadkeys", "caps:none"), source="environment"),
		snap(("evdev", "pc104", "cz", "", "caps:none")),
	]
	assert value["afterBroken"] == 3
	assert value["threads"] == ["lindaKeyboardLayout"] * 4
	assert max(value["delays"]) < CHANGE_BOUND, value["delays"]
	# Nothing came after stop, and its thread is gone.
	assert value["left"] == []
	assertOnlyNamesLogged(result)
	assert "Keyboard layout: gamescope's Xwayland names a layout that does not compile" in moduleRecords(
		result
	)


def test_withoutALayoutGamescopeGoesByTheEnvironment(tmp_path: Path):
	code = r"""
result = [started(None), started(":bepo"), started("xx")]
"""
	named = runFeed(tmp_path, code).value
	unnamed = runFeed(
		tmp_path,
		code,
		env={name: None for name in GAMESCOPE if name.startswith("XKB_DEFAULT_")},
	)
	de = snap(("evdev", "pc104", "de", "nodeadkeys", "caps:none"), source="environment")
	assert named == [de, de, de]
	us = snap(("evdev", "pc105", "us", "", ""), source="environment", notice=US_NOTICE)
	assert unnamed.value == [us, us, us]
	assertOnlyNamesLogged(unnamed)


def test_withoutItsXwaylandTheDesktopsFeedIsTaken(tmp_path: Path):
	result = runFeed(
		tmp_path,
		r"""
os.environ["DISPLAY"] = ":42"
result = started(None)
""",
	)
	assert result.value == snap(("evdev", "pc104", "de", "nodeadkeys", "caps:none"), source="environment")
	assert "Keyboard layout: the gamescopeLayout feed did not start" in moduleRecords(result, "desktopLayout")
