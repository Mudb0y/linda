"""The Wayland route: keys from linda-keyd alone, with no X source and no X connection, the layout from
the desktop's settings and a document's keys from AT-SPI; and an X session getting exactly today's
route. Linda is told the session is Wayland while the rig gives it the X display, for its own windows
and for the programs it reads, so every test here carries the X session's marker, and the wayland
gate leaves them out under a compositor. keydStandIn stands in for the helper, putting keys out on a
NullSink, or on the X display where a program must get them. Run under the rig:

	nix develop . -c tests/linux/rig python3 -m pytest -q tests/linux/test_waylandRoute.py
"""

import shutil
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

import harness
import pytest
import test_browseKeys as browseKeys
import webSession
from desktopSession import PREFIX_MARK, DesktopSession
from keydStandIn import VERDICT_KEEP, NullSink, StandIn, code
from test_keys import STARTUP_TIMEOUT, configText

pytestmark = pytest.mark.rigSession("x11")

#: The owner's own session, results/owner-session.txt of the Wayland work: the variables it has, and
#: those it does not.
OWNER_SESSION = {
	"XDG_SESSION_TYPE": "x11",
	"XDG_CURRENT_DESKTOP": "MATE",
	"DISPLAY": ":0",
	"WAYLAND_DISPLAY": None,
	"LINDA_SESSION_TYPE": None,
	"GAMESCOPE_WAYLAND_DISPLAY": None,
}
#: A Wayland session as Linda is told of it here, on a desktop with no feed of its own.
WAYLAND = {"LINDA_SESSION_TYPE": "wayland", "XDG_CURRENT_DESKTOP": None, "XKB_DEFAULT_LAYOUT": "pl"}

NO_HELPER = (
	"NVDA cannot take keys in this Wayland session until linda-keyd runs. It reads the screen "
	"meanwhile. The readme says how to install linda-keyd."
)
HAS_KEYBOARD = "NVDA has the keyboard"
HINT = "If typing stops reaching programs, hold Backspace, Escape and Enter together."
LOST = "NVDA has lost the keyboard and takes it again as soon as linda-keyd lets it."
KEEPS_STOPPING = "linda-keyd keeps stopping. NVDA has no keys until it runs again."
EMERGENCY = (
	"NVDA has let go of the keyboard and has no keys now. "
	"To give it the keyboard back, start NVDA again, or run linda --take-keyboard."
)
EMERGENCY_RUN_DIALOG = (
	"NVDA has let go of the keyboard and has no keys now. "
	"To give it the keyboard back, press Alt+F2, type linda and press Enter."
)
BUSY = (
	"Another program holds linda-keyd for this session, so NVDA has no keys. "
	"If you started no other screen reader, something may be reading your keys."
)
REPLACED = "Another program took NVDA's place with linda-keyd and reads every key now."
TURNED_AWAY = (
	"NVDA cannot take keys in this Wayland session: linda-keyd turned it away, and the log says why. "
	"It reads the screen meanwhile."
)
UNHEARD = "NVDA cannot hear one of the keyboards, so its keys reach programs without NVDA."
NO_KEYBOARD = (
	"NVDA has no keys in this Wayland session, as linda-keyd leaves alone a keyboard that is also a "
	"pointing device. NVDA still reads the screen."
)
NO_SENDER = "NVDA cannot type into programs here."
US_NOTICE = "NVDA cannot find this desktop's keyboard layout, so it reads keys as US English."
#: GOODBYE's and DEVICE's numbers, as protocol.rs gives them.
GOODBYE_VERSION, GOODBYE_NOT_IN_FRONT, GOODBYE_NOT_AUTHORIZED, GOODBYE_BUSY, GOODBYE_REPLACED = 1, 2, 3, 4, 9
DEVICE_WAITING, DEVICE_BUSY, DEVICE_IGNORED = 2, 3, 5

#: The route in a child, built by createSource, its NVDA keeping every captured press and recording each
#: event. The keys package may not open the X display: Display raises when called from it, and records
#: the call; so does building an X source. The stand-in is the child's own, on a NullSink in the pl
#: layout, unless STAND_IN is None.
ROUTE_CHILD = r"""
import logging, os, sys, tempfile, threading, time
sys.path.insert(0, str(harness.HERE))
import Xlib.display

_display = Xlib.display.Display
opened = []


def guarded(*args, **kwargs):
	caller = sys._getframe(1).f_globals.get("__name__", "")
	if caller.startswith("linuxPlatform.keys"):
		opened.append(caller)
		raise OSError("the keys package is not to open the X display")
	return _display(*args, **kwargs)


Xlib.display.Display = guarded
from keydStandIn import NullSink, StandIn, code
from logHandler import log
from linuxPlatform import displayServer, testHooks
import linuxPlatform.keys as keys
from linuxPlatform.keys import x11

built = []
_x11Init = x11.X11KeySource.__init__


def recordX(self, *args, **kwargs):
	built.append(True)
	_x11Init(self, *args, **kwargs)


x11.X11KeySource.__init__ = recordX


class Lines(logging.Handler):
	def __init__(self):
		super().__init__(logging.DEBUG)
		self.lines = []

	def emit(self, record):
		self.lines.append(record.getMessage())


logged = Lines()
log.addHandler(logged)
log.setLevel(logging.DEBUG)
socketDir = tempfile.mkdtemp(prefix="keyd", dir="/tmp")
path = socketDir + "/socket"
sink = NullSink(layout="pl")
keyboard = None


def standIn(**kwargs):
	global keyboard
	keyboard = StandIn(path, sink=sink, **{**(STAND_IN or {}), **kwargs}).start()
	return keyboard


if STAND_IN is not None:
	standIn()
delivered = []
said = []
keep = lambda event: True


def deliver(event):
	delivered.append(
		{
			"vk": event.vkCode,
			"up": event.isUp,
			"repeat": event.repeat,
			"captured": event.captured,
			"nvda": event.nvdaKeyGrab,
			"reached": event.reachedApplication,
			"text": event.text,
			"t": time.monotonic(),
		}
	)
	if event.isUp or not event.captured:
		return True
	return not keep(event)


testHooks.keydSocketPath = lambda: path


def startRoute(route="auto", waitForKernel=True, consumed=x11.EXTENDED_INSERT | x11.CAPS_LOCK):
	source = keys.createSource(route, said.append)
	source.start(deliver, lambda: "off")
	source.setConsumed(consumed, True)
	if waitForKernel:
		wait(lambda: source.isKernel, 10, "the route did not take the helper's keys")
	time.sleep(0.2)
	return source


def wait(condition, timeout, what):
	deadline = time.monotonic() + timeout
	while not condition():
		assert time.monotonic() < deadline, what
		time.sleep(0.01)


def downs(vk=None):
	return [e for e in delivered if not e["up"] and not e["repeat"] and (vk is None or e["vk"] == vk)]


def admissions():
	return [e[1] for e in keyboard.events if e[1] in ("admitted", "refused")]
"""


def runRouteChild(
	tmp_path: Path,
	code: str,
	*,
	standIn: dict | None = None,
	env: dict | None = None,
	timeout: float = 120,
) -> dict:
	done = harness.runChild(
		tmp_path,
		f"STAND_IN = {standIn!r}\n" + ROUTE_CHILD + code,
		env={**WAYLAND, **(env or {})},
		timeout=timeout,
	)
	assert done.error is None, f"{done.error}\n{done.stderr[-6000:]}"
	return done.value


# An X session.

OWNER_CHILD = r"""
import os, sys
from logHandler import log
import logging
from linuxPlatform import displayServer
import linuxPlatform.keys as keys
from linuxPlatform.keys import route

lines = []
handler = logging.Handler(logging.DEBUG)
handler.emit = lambda record: lines.append(record.getMessage())
log.addHandler(handler)
log.setLevel(logging.DEBUG)
built = keys.createSource("auto", lambda message: None)
reason = displayServer.reason()
# Started on the rig's own display, which the owner's ":0" is not; the rest of their environment stays.
displayServer._decision.cache_clear()
os.environ["DISPLAY"] = RIG_DISPLAY
started = keys.createSource("auto", lambda message: None)
started.start(lambda event: True)
time.sleep(0.5)
started.stop()
result = {
	"type": f"{type(built).__module__}.{type(built).__qualname__}",
	"startedType": f"{type(started).__module__}.{type(started).__qualname__}",
	"reason": reason,
	"lines": [line for line in lines if line.startswith("Keys ")],
	"modules": sorted(
		name
		for name in sys.modules
		if name.rsplit(".", 1)[-1] in ("waylandRoute", "waylandEnvironment", "atspiCapture", "desktopLayout")
	),
}
"""


def test_ownerSessionGetsTodaysRoute(tmp_path: Path):
	"""The owner's environment builds exactly RoutedKeySource, by rule 2, and starts it as before: no
	Wayland route, capture feed, layout feed or environment is made, or even imported."""
	import os

	done = harness.runChild(
		tmp_path,
		f"import time\nRIG_DISPLAY = {os.environ['DISPLAY']!r}\n" + OWNER_CHILD,
		env=OWNER_SESSION,
		timeout=60,
	)
	assert done.error is None, f"{done.error}\n{done.stderr[-6000:]}"
	value = done.value
	assert value["type"] == "linuxPlatform.keys.route.RoutedKeySource"
	assert value["startedType"] == "linuxPlatform.keys.route.RoutedKeySource"
	assert value["reason"].startswith("x11 by rule 2:"), value["reason"]
	assert value["lines"][0] == (
		"Keys are read from the X display: linda-keyd is not running, as there is no /run/linda-keyd/socket"
	)
	assert value["modules"] == []


# A Wayland session, in a child.

CHORDS_CHILD = r"""
source = startRoute()
kind = type(source).__name__
keyboard.press("Insert+t")
keyboard.press("Caps_Lock+t")
time.sleep(0.3)
chords = sink.passed()
keyboard.press("ISO_Level3_Shift+a")
time.sleep(0.3)
source.stop()
result = {
	"kind": kind,
	"opened": opened,
	"built": built,
	"chords": chords,
	"passed": sink.passed(),
	"downs": [[e["vk"], e["captured"], e["nvda"], e["reached"], e["text"]] for e in downs()],
	"session": displayServer.reason(),
	"said": said,
	"lines": [line for line in logged.lines if line.startswith("NVDA ")],
}
"""


def test_waylandSessionReadsKeysWithNoXDisplay(tmp_path: Path):
	value = runRouteChild(tmp_path, CHORDS_CHILD, standIn={})
	assert value["kind"] == "WaylandRoute"
	assert value["session"].startswith("wayland by rule 1:")
	assert value["opened"] == []
	assert value["built"] == []
	# The NVDA key chords are NVDA's alone: neither key reached the desktop.
	assert value["chords"] == []
	downs = value["downs"]
	insert, capsLock, t, a = 0x2D, 0x14, ord("T"), ord("A")
	assert [d[:4] for d in downs[:4]] == [
		[insert, True, True, False],
		[t, True, True, False],
		[capsLock, True, True, False],
		[t, True, True, False],
	]
	# AltGr+a, typing, went out to the desktop, and in the pl layout types a with ogonek, which NVDA is given as the key's text.
	typed = [d for d in downs if d[0] == a]
	assert typed and typed[-1][4] == "\N{LATIN SMALL LETTER A WITH OGONEK}", downs
	assert value["passed"] == [
		[code("ISO_Level3_Shift"), True],
		[code("a"), True],
		[code("a"), False],
		[code("ISO_Level3_Shift"), False],
	]
	assert (
		value["lines"][0] == "NVDA has no keys in this Wayland session: until linda-keyd holds the keyboard"
	)
	assert value["said"] == [HINT]


NO_HELPER_CHILD = r"""
source = startRoute(waitForKernel=False)
time.sleep(1)
before = list(said)
started = time.monotonic()
standIn()
keyboard.waitFor(lambda: keyboard.capturing, 5)
keyboard.press("Insert+t")
wait(lambda: downs(ord("T")), 5, "no key came")
reached = downs(ord("T"))[0]["t"] - started
# The helper goes and comes back: the hint is not said again.
keyboard.vanish()
time.sleep(0.5)
keyboard.comeBack()
keyboard.waitFor(lambda: keyboard.capturing, 5)
time.sleep(0.5)
source.stop()
result = {"before": before, "reached": reached, "said": said}
"""


def test_noHelperAtStartThenOneStartedLater(tmp_path: Path):
	value = runRouteChild(tmp_path, NO_HELPER_CHILD)
	assert value["before"] == [NO_HELPER]
	assert value["reached"] <= 2.5, value["reached"]
	assert value["said"] == [NO_HELPER, HAS_KEYBOARD, HINT, LOST]


EMERGENCY_CHILD = r"""
source = startRoute()
for key in ("BackSpace", "Escape", "Return"):
	keyboard.down(key)
	time.sleep(0.05)
for key in ("Return", "Escape", "BackSpace"):
	keyboard.up(key)
time.sleep(5)
result = {"said": list(said), "starts": keyboard.starts, "admissions": admissions(), "kernel": source.isKernel}
source.takeBack()
keyboard.waitFor(lambda: keyboard.capturing, 5)
time.sleep(0.3)
result["afterTakeBack"] = said[len(result["said"]):]
result["startsAfter"] = keyboard.starts
source.stop()
"""


def test_emergencyChordIsSaidOnceAndTheHelperNotAskedAgain(tmp_path: Path):
	value = runRouteChild(tmp_path, EMERGENCY_CHILD, standIn={})
	assert value["said"] == [HINT, EMERGENCY]
	assert value["starts"] == 1
	assert value["admissions"] == ["admitted"]
	assert not value["kernel"]
	assert value["afterTakeBack"] == ["NVDA has the keyboard again"]
	assert value["startsAfter"] == 2


EMERGENCY_WORDING_CHILD = r"""
from linuxPlatform.keys import waylandRoute

wording = {}
for name in ("gnome", "kde", "sway", "other"):
	displayServer.compositor = lambda name=name: name
	route = waylandRoute.WaylandRoute("auto", path, said.append)
	route._sayEmergency()
	wording[name] = said.pop()
result = wording
"""


def test_emergencyWordingFollowsTheDesktop(tmp_path: Path):
	value = runRouteChild(tmp_path, EMERGENCY_WORDING_CHILD)
	assert value == {
		"gnome": EMERGENCY_RUN_DIALOG,
		"kde": EMERGENCY_RUN_DIALOG,
		"sway": EMERGENCY,
		"other": EMERGENCY,
	}


BUSY_CHILD = r"""
keyboard.refusal = (GOODBYE_BUSY, "another client")
source = startRoute(waitForKernel=False)
time.sleep(5)
busy = {"said": list(said), "refused": admissions().count("refused")}
keyboard.refusal = None
wait(lambda: source.isKernel, 5, "the route did not take the helper's keys")
time.sleep(0.3)
keyboard.goodbye(GOODBYE_REPLACED, "replaced")
keyboard.refusal = (GOODBYE_BUSY, "another client")
time.sleep(5)
replaced = {"said": said[len(busy["said"]):], "refused": admissions().count("refused") - busy["refused"]}
source.stop()
result = {"busy": busy, "replaced": replaced}
"""


def test_busyAndReplacedAreEachSaidOnce(tmp_path: Path):
	value = runRouteChild(
		tmp_path,
		f"GOODBYE_BUSY, GOODBYE_REPLACED = {GOODBYE_BUSY}, {GOODBYE_REPLACED}\n" + BUSY_CHILD,
		standIn={},
	)
	busy, replaced = value["busy"], value["replaced"]
	# Asked again every two seconds, and turned away each time.
	assert busy["refused"] >= 2
	assert busy["said"] == [BUSY]
	assert replaced["refused"] >= 2
	assert replaced["said"] == [HINT, REPLACED]


REFUSED_CHILD = r"""
keyboard.refusal = (REASON, "turned away")
source = startRoute(waitForKernel=False)
time.sleep(5)
refused = {"said": list(said), "refused": admissions().count("refused"), "kernel": source.isKernel}
keyboard.refusal = None
wait(lambda: source.isKernel, 5, "the route did not take the helper's keys")
time.sleep(0.3)
taken = list(said)
# Once the route has been taken, losing the helper has its own line, and a refusal adds none.
keyboard.refusal = (REASON, "turned away")
keyboard.vanish()
time.sleep(0.3)
keyboard.comeBack()
time.sleep(4)
source.stop()
result = {"refused": refused, "taken": taken, "said": said, "refusedAfter": admissions().count("refused")}
"""


@pytest.mark.parametrize("reason", [GOODBYE_VERSION, GOODBYE_NOT_IN_FRONT, GOODBYE_NOT_AUTHORIZED])
def test_aRefusalAtStartIsSaidOnce(tmp_path: Path, reason: int):
	"""Turned away for any reason but BUSY, NVDA has no keys at all, which is said once; and that it has
	the keyboard once the helper admits it."""
	value = runRouteChild(tmp_path, f"REASON = {reason}\n" + REFUSED_CHILD, standIn={})
	refused = value["refused"]
	assert refused["refused"] >= 2
	assert refused["said"] == [TURNED_AWAY]
	assert not refused["kernel"]
	assert value["taken"] == [TURNED_AWAY, HAS_KEYBOARD, HINT]
	assert value["refusedAfter"] > refused["refused"]
	assert value["said"] == [TURNED_AWAY, HAS_KEYBOARD, HINT, LOST]


STALE_SOCKET_CHILD = r"""
import socket

# What a helper that stopped without removing its socket leaves: a socket file nothing listens on.
stale = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
stale.bind(path)
stale.close()
source = startRoute(waitForKernel=False)
time.sleep(5)
before = {"said": list(said), "left": os.path.exists(path), "kernel": source.isKernel}
standIn()
wait(lambda: source.isKernel, 5, "the route did not take the helper's keys")
time.sleep(0.3)
source.stop()
result = {"before": before, "said": said}
"""


def test_aSocketWithNoHelperIsSaidAsNoHelper(tmp_path: Path):
	value = runRouteChild(tmp_path, STALE_SOCKET_CHILD)
	assert value["before"] == {"said": [NO_HELPER], "left": True, "kernel": False}
	assert value["said"] == [NO_HELPER, HAS_KEYBOARD, HINT]


LOSSES_CHILD = r"""
source = startRoute()
for _ in range(4):
	keyboard.vanish()
	time.sleep(0.3)
	keyboard.comeBack()
	keyboard.waitFor(lambda: keyboard.capturing, 6)
	wait(lambda: source.isKernel, 5, "the route did not take the helper's keys again")
	time.sleep(0.3)
source.stop()
result = {"said": said}
"""


def test_threeLossesWithinTwoMinutesAreSaidOnce(tmp_path: Path):
	value = runRouteChild(tmp_path, LOSSES_CHILD, standIn={})
	assert value["said"] == [HINT, LOST, LOST, KEEPS_STOPPING]


BLOCK_CHILD = r"""
keyboard.refusal = (GOODBYE_NOT_IN_FRONT, "not in front")
source = startRoute(waitForKernel=False)
wait(lambda: admissions(), 5, "the route did not ask the helper")
source._blocked = source._socketIdentity()
asked = len(admissions())
keyboard.refusal = None
time.sleep(10)
held = {"asked": len(admissions()) - asked, "kernel": source.isKernel, "blocked": source._blocked is not None}
source.takeBack()
wait(lambda: source.isKernel, 5, "takeBack did not take the keyboard")
result = {"held": held, "blocked": source._blocked, "said": said}
source.stop()
"""


def test_aBlockHoldsUntilTheUserTakesTheKeyboardBack(tmp_path: Path):
	value = runRouteChild(
		tmp_path,
		f"GOODBYE_NOT_IN_FRONT = {GOODBYE_NOT_IN_FRONT}\n" + BLOCK_CHILD,
		standIn={},
	)
	assert value["held"] == {"asked": 0, "kernel": False, "blocked": True}
	assert value["blocked"] is None
	# The refusal at start is said; then only take-back's own line, not "has the keyboard" as well.
	assert value["said"] == [TURNED_AWAY, "NVDA has the keyboard again", HINT]


DEVICE_CHILD = r"""
source = startRoute()
for status in (DEVICE_BUSY, DEVICE_IGNORED, DEVICE_BUSY):
	with keyboard._lock:
		keyboard._sendDevice(status, device=7)
	time.sleep(0.3)
source.stop()
result = {"said": said}
"""


def test_anUnheardKeyboardIsSaidOnce(tmp_path: Path):
	value = runRouteChild(
		tmp_path,
		f"DEVICE_BUSY, DEVICE_IGNORED = {DEVICE_BUSY}, {DEVICE_IGNORED}\n" + DEVICE_CHILD,
		standIn={},
	)
	assert value["said"] == [HINT, UNHEARD]


#: The stand-in's capture with its own keyboard, device 3, left alone in place of held, as linda-keyd
#: leaves a keyboard that is also a pointer; with LEFT_ALONE_FIRST, a keyboard 2 is left alone and the
#: stand-in's own is held just after it, in the one scan.
LEFT_ALONE_CHILD = r"""
from keydStandIn import StandIn

_start = threading.Thread.start
startedByReader = []


def recordStart(thread):
	# The kernel source's reading thread answers keys, and a note it makes may only record and return.
	if threading.current_thread().name == "KernelKeySource":
		startedByReader.append(thread.name)
	_start(thread)


threading.Thread.start = recordStart


def leftAlone():
	keyboard._startWaiting = False
	if LEFT_ALONE_FIRST:
		keyboard._sendDevice(DEVICE_IGNORED, device=2)
		# Well inside the settling, so that without it the route would hear of no keyboard held.
		time.sleep(0.1)
		StandIn._capture(keyboard)
	else:
		keyboard._sendDevice(DEVICE_IGNORED)


keyboard._capture = leftAlone
source = startRoute(waitForKernel=LEFT_ALONE_FIRST)
wait(lambda: said, 5, "nothing was said of the keyboard left alone")
time.sleep(1)
before = {"said": list(said), "kernel": source.isKernel}
if not LEFT_ALONE_FIRST:
	# Said again, as at every capture, and a keyboard held at last.
	with keyboard._lock:
		keyboard._sendDevice(DEVICE_IGNORED)
	time.sleep(1)
	with keyboard._lock:
		StandIn._capture(keyboard)
	wait(lambda: source.isKernel, 5, "the route did not take the keyboard held later")
	time.sleep(0.3)
source.stop()
result = {"before": before, "said": said, "startedByReader": startedByReader}
"""


def leftAloneChild(tmp_path: Path, first: bool) -> dict:
	return runRouteChild(
		tmp_path,
		f"DEVICE_IGNORED = {DEVICE_IGNORED}\nLEFT_ALONE_FIRST = {first}\n" + LEFT_ALONE_CHILD,
		standIn={},
	)


def test_theOnlyKeyboardLeftAloneLeavesNoKeysUntilOneIsHeld(tmp_path: Path):
	value = leftAloneChild(tmp_path, first=False)
	assert value["before"] == {"said": [NO_KEYBOARD], "kernel": False}
	assert value["said"] == [NO_KEYBOARD, HAS_KEYBOARD, HINT]
	assert value["startedByReader"] == []


def test_oneOfTwoKeyboardsLeftAloneIsUnheard(tmp_path: Path):
	"""Left alone just before the other is held, it is still only one of them that NVDA cannot hear."""
	value = leftAloneChild(tmp_path, first=True)
	said = value["said"]
	assert sorted(said) == sorted([HINT, UNHEARD]), said
	assert value["before"]["kernel"]
	assert value["startedByReader"] == []


#: The stand-in's capture with a keyboard 2 left alone and its own still waiting for a key to come up.
WAITING_CHILD = r"""
def scan():
	keyboard._startWaiting = False
	keyboard._sendDevice(DEVICE_IGNORED, device=2)
	keyboard._sendDevice(DEVICE_WAITING)


keyboard._capture = scan
source = startRoute(waitForKernel=False)
wait(lambda: said, 5, "nothing was said of the keyboards")
time.sleep(1)
result = {"said": said, "kernel": source.isKernel}
source.stop()
"""


def test_aKeyboardLeftAloneBesideOneWaitingIsUnheard(tmp_path: Path):
	"""Nothing is held yet, but the keyboard waiting for its keys to come up will be: NVDA is not
	without keys, it only cannot hear the one left alone."""
	value = runRouteChild(
		tmp_path,
		f"DEVICE_IGNORED, DEVICE_WAITING = {DEVICE_IGNORED}, {DEVICE_WAITING}\n" + WAITING_CHILD,
		standIn={},
	)
	assert value == {"said": [UNHEARD], "kernel": False}


MODE_CHILD = r"""
source = startRoute(waitForKernel=False)
source.setCapture("browse", 4242)
feed = source._environment.feed
state = feed.captureState
result = {"mode": feed._mode, "process": state.processID, "kernel": source._kernel is not None}
source.stop()
"""


def test_theModeReachesTheFeedBeforeAnyHelper(tmp_path: Path):
	value = runRouteChild(tmp_path, MODE_CHILD)
	assert value == {"mode": "browse", "process": 4242, "kernel": False}


LAYOUT_CHILD = r"""
from linuxPlatform.keys import desktopLayout, waylandEnvironment, waylandRoute

environment = waylandEnvironment.WaylandEnvironment(None, layoutFeed=desktopLayout.EnvironmentLayoutFeed())
source = waylandRoute.WaylandRoute("x11", path, said.append, environment=environment)
source.start(deliver)
time.sleep(0.5)
asked = {
	"route": source._route,
	"char": source.charForVk(ord("Y")),
	"keyName": source.keyName(0x15, False),
	"caps": source.keyState(0x14),
	"vkForA": source.vkForChar("a"),
}
source.setRoute("x11")
source.stop()
result = {
	"asked": asked,
	"said": said,
	"routeLines": [line for line in logged.lines if "key route is set to the X display" in line],
}
"""


def test_layoutFromTheEnvironmentAndItsNoticeOnce(tmp_path: Path):
	"""No layout named: US English, said once; the route's own translator answers with no kernel source;
	and the route set to the X display is taken as auto, with one line in the log."""
	value = runRouteChild(tmp_path, LAYOUT_CHILD, env={"XKB_DEFAULT_LAYOUT": None})
	assert value["asked"] == {"route": "auto", "char": ord("y"), "keyName": "y", "caps": 0, "vkForA": [0, 65]}
	assert value["said"] == [NO_HELPER, US_NOTICE]
	assert len(value["routeLines"]) == 1


BROKEN_LAYOUT_CHILD = r"""
from linuxPlatform.keys import desktopLayout, waylandEnvironment, waylandRoute

environment = waylandEnvironment.WaylandEnvironment(None, layoutFeed=desktopLayout.EnvironmentLayoutFeed())
source = waylandRoute.WaylandRoute("auto", path, said.append, environment=environment)
source.start(deliver)
source.setConsumed(x11.EXTENDED_INSERT, True)
wait(lambda: source.isKernel, 10, "the route did not take the helper's keys")
keyboard.press("Insert+t")
time.sleep(0.3)
source.stop()
result = {"said": said, "downs": [[e["vk"], e["nvda"]] for e in downs()], "layout": environment.layout.names}
"""


def test_aLayoutThatCannotBeCompiledLeavesUSEnglish(tmp_path: Path):
	"""A layout xkbcommon cannot compile would keep every kernel source from starting: US English is
	read instead, and said."""
	value = runRouteChild(
		tmp_path, BROKEN_LAYOUT_CHILD, standIn={}, env={"XKB_DEFAULT_LAYOUT": "nosuchlayout"}
	)
	assert value["layout"] == ["evdev", "pc105", "us", "", ""]
	assert value["said"] == [US_NOTICE, HINT]
	assert value["downs"] == [[0x2D, True], [ord("T"), True]]


POLISH_QUERIES_CHILD = r"""
source = startRoute(waitForKernel=False)
result = {
	"ogonek": source.vkForChar("\N{LATIN SMALL LETTER A WITH OGONEK}"),
	"z": source.charForVk(ord("Z")),
}
source.stop()
"""


def test_routesOwnTranslatorFollowsTheDesktopsLayout(tmp_path: Path):
	value = runRouteChild(tmp_path, POLISH_QUERIES_CHILD)
	# AltGr is the third level, which VkKeyScanEx gives as Control and Alt.
	assert value == {"ogonek": [6, ord("A")], "z": ord("z")}


LAYOUT_CHANGE_CHILD = r"""
from linuxPlatform.keys import desktopLayout, waylandEnvironment, waylandRoute


class Feed:
	def start(self, onChange):
		self.onChange = onChange
		return desktopLayout.LayoutSnapshot(("evdev", "pc105", "pl", "", ""))

	def stop(self):
		pass


feed = Feed()
environment = waylandEnvironment.WaylandEnvironment(None, layoutFeed=feed)
source = waylandRoute.WaylandRoute("auto", path, said.append, environment=environment)
source.start(deliver)
source.setConsumed(x11.EXTENDED_INSERT, True)
wait(lambda: source.isKernel, 10, "the route did not take the helper's keys")


def now():
	try:
		vk = source.vkForChar("\N{LATIN SMALL LETTER A WITH OGONEK}")
	except LookupError:
		vk = None
	before = len(delivered)
	keyboard.press("ISO_Level3_Shift+a")
	time.sleep(0.3)
	typed = [e["text"] for e in delivered[before:] if e["vk"] == ord("A") and not e["up"]]
	return {"vk": vk, "kernel": source._kernel._tables.names[2], "typed": typed}


states = {"pl": now()}
feed.onChange(desktopLayout.LayoutSnapshot(("evdev", "pc105", "us", "", "")))
states["us"] = now()
feed.onChange(desktopLayout.LayoutSnapshot(("evdev", "pc105", "nosuchlayout", "", "")))
states["broken"] = now()
source.stop()
result = states
"""


def test_aLayoutChangeReachesTheRouteAndTheKernelSource(tmp_path: Path):
	"""A change from the desktop reaches the route's own translator and the attached kernel source; one
	that cannot be compiled leaves the layout before it."""
	value = runRouteChild(tmp_path, LAYOUT_CHANGE_CHILD, standIn={})
	ogonek = "\N{LATIN SMALL LETTER A WITH OGONEK}"
	assert value["pl"] == {"vk": [6, ord("A")], "kernel": "pl", "typed": [ogonek]}
	assert value["us"] == {"vk": None, "kernel": "us", "typed": ["a"]}
	assert value["broken"] == value["us"]


KEEPER_CHILD = r"""
from gi.repository import GLib
from linuxPlatform.keys import waylandEnvironment

source = startRoute(waitForKernel=False)
feed = source._environment.feed
wait(lambda: feed.canLook, 10, "the capture feed never connected")
first = feed._thread
# As when the accessibility bus goes away: the feed's loop ends, and nothing in the feed starts it again.
ending = GLib.idle_source_new()
ending.set_callback(lambda *_args: feed._loop.quit() or GLib.SOURCE_REMOVE)
ending.attach(feed._context)
wait(lambda: not feed.canLook, 5, "the capture feed did not stop")
lost = time.monotonic()
wait(lambda: feed.canLook, 10, "the capture feed was not started again")
back = time.monotonic() - lost
again = feed._thread is not first
source.stop()
left = sorted(t.name for t in threading.enumerate() if t.name in ("lindaCaptureKeeper", "lindaAtspiCapture"))
result = {"back": back, "again": again, "left": left, "interval": waylandEnvironment.KEEP_INTERVAL}
"""


def test_aLostCaptureFeedIsStartedAgain(tmp_path: Path):
	value = runRouteChild(tmp_path, KEEPER_CHILD)
	assert value["again"]
	assert value["back"] <= value["interval"] + 2, value["back"]
	assert value["left"] == []


FEATURES_CHILD = r"""
source = startRoute()
result = {"granted": keyboard.granted}
source.stop()
"""


def test_theRouteAsksForEveryFeature(tmp_path: Path):
	typing, wait, latePass, virtualSeen = 1, 2, 4, 8
	value = runRouteChild(
		tmp_path, FEATURES_CHILD, standIn={"features": typing | wait | latePass | virtualSeen}
	)
	assert value["granted"] == typing | wait | latePass | virtualSeen


NO_SENDER_CHILD = r"""
from linuxPlatform import inputHook

announced = []
inputHook._announce = announced.append
source = startRoute()
inputHook._source = source
inputHook.keybdEvent(0x41, 0x1E, 0, 0)
inputHook.keybdEvent(0x41, 0x1E, 2, 0)
inputHook.typeKey(0x41, False, False)
inputHook._source = None
time.sleep(0.3)
result = {"announced": announced, "canSend": source.canSend, "passed": sink.passed()}
source.stop()
"""


def test_aKeySentWithNoSenderIsSaidOnce(tmp_path: Path):
	value = runRouteChild(tmp_path, NO_SENDER_CHILD, standIn={})
	assert value == {"announced": [NO_SENDER], "canSend": False, "passed": []}


CAPTURE_DELAY_CHILD = r"""
import globalVars
from linuxPlatform.keys import waylandRoute

hooks = tempfile.mkdtemp()
values = {}
for hooksDir, delay in ((None, "250"), (hooks, "250"), (hooks, "soon"), (hooks, "-5")):
	for name, value in (("LINDA_TEST_HOOKS_DIR", hooksDir), ("LINDA_TEST_CAPTURE_DELAY_MS", delay)):
		if value is None:
			os.environ.pop(name, None)
		else:
			os.environ[name] = value
	values[f"{hooksDir is not None} {delay}"] = testHooks.captureDelay()
globalVars.appArgs.secure = True
values["secure"] = testHooks.captureDelay()
globalVars.appArgs.secure = False
os.environ["LINDA_TEST_CAPTURE_DELAY_MS"] = "250"
source = waylandRoute.WaylandRoute("auto", path, said.append)
source.start(deliver)
values["held"] = source._environment.feed._holdEvents
source.stop()
result = values
"""


def test_captureDelayOnlyWhereTestHooksAre(tmp_path: Path):
	value = runRouteChild(tmp_path, CAPTURE_DELAY_CHILD)
	assert value == {
		"False 250": 0,
		"True 250": 250,
		"True soon": 0,
		"True -5": 0,
		"secure": 0,
		"held": 0.25,
	}


GRACE_CHILD = r"""
from linuxPlatform.keys import atspiCapture, kernel

result = {
	"grace": atspiCapture.FOCUS_MOVE_GRACE,
	"capture": x11.CAPTURE_TIMEOUT,
	"document": x11.DOCUMENT_WAIT,
	"lookMost": kernel.LOOK_GRACE_MOST,
}
"""


def test_theFocusGraceLeavesNVDAItsTime(tmp_path: Path):
	value = runRouteChild(tmp_path, GRACE_CHILD)
	assert value["grace"] + value["capture"] <= value["document"] + 1e-9
	assert value["grace"] <= value["lookMost"]


CHOICE_CHILD = r"""
from linuxPlatform import keyRouteChoice

wayland = keyRouteChoice._labels()
displayServer._decision.cache_clear()
os.environ["LINDA_SESSION_TYPE"] = "x11"
x = keyRouteChoice._labels()
result = {"wayland": wayland, "x11": x}
"""


def test_theRouteChoiceHasWaylandLabelsInAWaylandSession(tmp_path: Path):
	value = runRouteChild(tmp_path, CHOICE_CHILD)
	assert value["x11"] == ["linda-keyd when it lets NVDA, otherwise the X display", "the X display alone"]
	assert value["wayland"] == ["linda-keyd", "the X display alone in an X session, linda-keyd in this one"]


# Linda itself, told it is in a Wayland session.


@pytest.fixture
def desktop(tmp_path: Path) -> Iterator[DesktopSession]:
	with DesktopSession(tmp_path / "desktop") as session:
		yield session


@pytest.fixture
def socketDir() -> Iterator[Path]:
	directory = Path(tempfile.mkdtemp(prefix="keyd", dir="/tmp"))
	try:
		yield directory
	finally:
		shutil.rmtree(directory, ignore_errors=True)


def lindaSays(text: str) -> str:
	return text.replace("NVDA", "Linda")


def waylandHooks(tmp_path: Path, socketPath: Path) -> dict[str, str]:
	hooks = tmp_path / "hooks"
	(hooks / "globalPlugins").mkdir(parents=True)
	(hooks / "synthDrivers").symlink_to(harness.HERE / "synthDrivers")
	return {
		"LINDA_TEST_HOOKS_DIR": str(hooks),
		"LINDA_TEST_KEYD_SOCKET": str(socketPath),
		**WAYLAND,
		"XKB_DEFAULT_LAYOUT": "us",
	}


def test_lindaInAWaylandSession(desktop: DesktopSession, socketDir: Path, tmp_path: Path):
	"""No helper at start, said once; a stand-in started later has the keys within 2.5 seconds; the
	hint once at that first capture; and the NVDA key chords, Insert's and Caps Lock's, speak the title
	with neither key reaching the desktop."""
	config = tmp_path / "nvda.ini"
	config.write_text(configText("desktop", 7))
	desktop.startApp(desktop.fixture("gtkFixture"), title="Fixture window")
	nvda = desktop.startNVDA(config, env=waylandHooks(tmp_path, socketDir / "socket"))
	desktop.expect("Fixture window", since=0, timeout=STARTUP_TIMEOUT)
	desktop.expect(lindaSays(NO_HELPER), since=0)
	nvda.waitForLog(
		"NVDA has no keys in this Wayland session: linda-keyd is not running, as there is no "
		f"{socketDir / 'socket'}",
		5,
	)
	sink = NullSink()
	mark = desktop.mark()
	started = time.monotonic()
	with StandIn(socketDir / "socket", sink=sink) as keyboard:
		keyboard.waitFor(lambda: keyboard.capturing, 5)
		desktop.expect(lindaSays(HAS_KEYBOARD), lindaSays(HINT), since=mark)
		keyboard.press("Insert+t")
		desktop.expect("Fixture window", since=mark)
		assert time.monotonic() - started <= 2.5 + 1.0
		taken = [e[0] for e in keyboard.events if e[1] == "key" and e[3] == code("t")]
		assert taken[0] - started <= 2.5, taken[0] - started
		# Apart by more than NVDA's multiple press timeout, so that it is not a second press of the first.
		time.sleep(0.7)
		mark = desktop.mark()
		keyboard.press("Caps_Lock+t")
		desktop.expect("Fixture window", since=mark)
		time.sleep(0.3)
		assert sink.passed() == []
		time.sleep(0.7)
		keyboard.press("Insert")
		time.sleep(0.15)
		keyboard.press("Insert")
		time.sleep(0.3)
		assert sink.passed() == [(code("Insert"), True), (code("Insert"), False)]
		nvda.process.terminate()
		nvda.waitForExit(20)
	nvda.assertClean(logNames=("nvda.log",))
	spoken = desktop.spokenSince(0)
	assert spoken.count(lindaSays(NO_HELPER)) == 1
	assert spoken.count(lindaSays(HINT)) == 1
	log = nvda.text("nvda.log")
	assert "Input: kb(desktop):NVDA+t" in log
	assert (
		"Keys come from the kernel through linda-keyd: linda-keyd admitted NVDA and holds the keyboard" in log
	)
	assert "Keys are read from the X display" not in log


def test_lindaBrowseModeKeysInAWaylandSession(desktop: DesktopSession, socketDir: Path, tmp_path: Path):
	"""Browse mode's single letters on Firefox's keys page, the keys put out on the X display for Firefox
	to have: h is NVDA's and never reaches the page, until NVDA+space gives the page its keys."""
	webSession.requireBrowseLibraries()
	webSession.startFirefox(desktop, "keys.html")
	with StandIn(socketDir / "socket") as keyboard:
		env = {
			**browseKeys.probeHooks(tmp_path),
			"LINDA_TEST_KEYD_SOCKET": keyboard.path,
			**WAYLAND,
			"XKB_DEFAULT_LAYOUT": "us",
		}
		nvda = desktop.startNVDA(browseKeys.browseConfig(tmp_path), env=env)
		nvda.waitForLog("Buffer rendered", browseKeys.BUFFER_BOUND)
		browseKeys.waitForCapture(nvda, "browse", timeout=browseKeys.BUFFER_BOUND)
		keyboard.waitFor(lambda: keyboard.capturing)
		time.sleep(browseKeys.SETTLE)
		page = browseKeys.KeysPage()
		assert page.log() == []

		def step(chord: str, *expected: str) -> int:
			mark = desktop.mark()
			keyboard.press(chord)
			browseKeys.expect(desktop, *expected, since=mark)
			time.sleep(browseKeys.SETTLE)
			return mark

		step("control+Home", "heading level 1 Keys page")
		seqs = {e[2] for e in keyboard.events if e[1] == "key" and e[3] == code("h") and e[4] == 1}
		step("h", "Field and link heading level 2")
		(seq,) = {e[2] for e in keyboard.events if e[1] == "key" and e[3] == code("h") and e[4] == 1} - seqs
		assert VERDICT_KEEP in keyboard.given[seq], keyboard.given.get(seq)
		time.sleep(browseKeys.SETTLE)
		assert page.log() == []
		step("Insert+space", "Focus mode")
		browseKeys.waitForCapture(nvda, "focus")
		keyboard.press("h")
		page.waitForLog(["h"])
		step("Insert+q", "Exit Linda" + PREFIX_MARK)
		keyboard.press("Return")
		nvda.waitForExit(10)
	nvda.assertClean(logNames=("nvda.log",))
	assert "Keys are read from the X display" not in nvda.text("nvda.log")
