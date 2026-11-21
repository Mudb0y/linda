"""Typing in a Wayland session: the keys NVDA sends on, and those Remote Access types, go out on
linda-keyd's virtual keyboard through TYPE. A key NVDA sends on goes with TYPE_INJECT, in line already
passed, and never comes back to NVDA; one Remote Access types goes with TYPE_DECIDE and comes back as
a key to decide, which NVDA has as injected, so that the controlling computer's chords with the NVDA
key run here. A numpad key typed under the other state of Num Lock is wrapped in Num Lock taps.

keydStandIn stands in for the helper, granting typing when a test asks. The route's own tests run in a
child with the session forced to Wayland and the keys put out on a NullSink. Linda's run in the X rig
with the session forced, the keys put out on the X display, and under the rig's compositors natively,
the keys put out on the compositor's keyboard:

	nix develop . -c tests/linux/rig python3 -m pytest -q tests/linux/test_waylandTyping.py
	nix develop . -c tests/linux/wayland-gate SCRATCH tests/linux/test_waylandTyping.py
"""

import shutil
import signal
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import desktopSession
import harness
import keydStandIn
import pytest
import remotePeer
import test_browseKeys as browseKeys
import webSession
from desktopSession import PREFIX_MARK, DesktopSession
from keydStandIn import FEATURE_LATE_PASS, FEATURE_TYPING, FEATURE_WAIT, StandIn, code
from remotePeer import RemotePeer
from test_remoteAccess import CHANNEL, PORT, remoteConfig
from test_waylandMenu import KEY_PROBE, Lines
from test_waylandRoute import NO_SENDER, lindaSays, runRouteChild

forcedSession = pytest.mark.rigSession("x11")
everySession = pytest.mark.rigSession("x11", "wayland")

#: What the route asks for, and the stand-in grants when told to.
TYPING = FEATURE_WAIT | FEATURE_TYPING | FEATURE_LATE_PASS
TYPED_OUT, TYPED_KEPT, TYPED_REFUSED_KEY, TYPED_NOT_DOWN, TYPED_LOST = 1, 2, 4, 6, 7
VK_TAB, VK_SHIFT, VK_INSERT, VK_SNAPSHOT = 0x09, 0x10, 0x2D, 0x2C
VK_HOME, VK_NUMPAD5, VK_NUMLOCK, VK_LSHIFT = 0x24, 0x65, 0x90, 0xA0
VK_LMENU = 0xA4
TAB, SHIFT, ALT, NUM_LOCK = code("Tab"), code("shift"), code("alt"), code("Num_Lock")
SYSRQ = keydStandIn.SYSRQ
KP_HOME, KP_5 = code("KP_Home"), code("KP_Begin")
#: The bit of Num Lock in the protocol's LOCKS.
LOCK_NUM = 1
#: Seconds NVDA has to start and speak the focus.
STARTUP_TIMEOUT = 60.0
#: Typed through Remote Access, and never to be found in Linda's log.
MARKER = "kqzmxjwv"

#: The child's own record of each key, with whether it came injected.
RECORDING = r"""
from linuxPlatform import inputHook


def deliver(event):
	delivered.append(
		{
			"vk": event.vkCode,
			"up": event.isUp,
			"repeat": event.repeat,
			"captured": event.captured,
			"nvda": event.nvdaKeyGrab,
			"reached": event.reachedApplication,
			"injected": event.injected,
			"text": event.text,
			"t": time.monotonic(),
		}
	)
	if event.isUp or not event.captured:
		return True
	return not keep(event)


def typedLines():
	return [line for line in logged.lines if "typed" in line]


def outcomes():
	return [outcome for _tag, _seq, outcome in keyboard.typed]
"""


def runTypingChild(tmp_path: Path, code: str, *, features: int = TYPING) -> dict:
	return runRouteChild(tmp_path, RECORDING + code, standIn={"features": features})


# The route, in a child.

INJECT_CHILD = r"""
announced = []
inputHook._announce = announced.append
source = startRoute()
inputHook._source = source
canSend = source.canSend
inputHook.keybdEvent(VK_TAB, 0x0F, 0, 0)
inputHook.keybdEvent(VK_TAB, 0x0F, 2, 0)
time.sleep(0.3)
tab = {"passed": sink.passed(), "delivered": len(delivered), "outcomes": outcomes()}
# Shift injected down: a key typed meanwhile comes under it, as the desktop has it.
inputHook.keybdEvent(VK_SHIFT, 0x2A, 0, 0)
time.sleep(0.2)
shiftDown = bool(source.keyState(VK_SHIFT) & 0x8000)
keyboard.press("a")
inputHook.keybdEvent(VK_SHIFT, 0x2A, 2, 0)
time.sleep(0.2)
shiftUp = bool(source.keyState(VK_SHIFT) & 0x8000)
keyboard.press("a")
time.sleep(0.3)
inputHook._source = None
source.stop()
result = {
	"canSend": canSend,
	"tab": tab,
	"shift": [shiftDown, shiftUp],
	"texts": [e["text"] for e in downs(ord("A"))],
	"passed": sink.passed(),
	"outcomes": outcomes(),
	"announced": announced,
	"lines": typedLines(),
}
"""


@forcedSession
def test_aKeyNVDASendsGoesOutPassedAndNeverComesBack(tmp_path: Path):
	value = runTypingChild(tmp_path, f"VK_TAB, VK_SHIFT = {VK_TAB}, {VK_SHIFT}\n" + INJECT_CHILD)
	assert value["canSend"]
	assert value["tab"] == {
		"passed": [[TAB, True], [TAB, False]],
		"delivered": 0,
		"outcomes": [TYPED_OUT, TYPED_OUT],
	}
	assert value["shift"] == [True, False]
	# The injected Shift is counted in the state the user's a is read in.
	assert value["texts"] == ["A", "a"]
	a = code("a")
	assert value["passed"][2:] == [
		[SHIFT, True],
		[a, True],
		[a, False],
		[SHIFT, False],
		[a, True],
		[a, False],
	]
	assert value["outcomes"] == [TYPED_OUT] * 4
	assert value["announced"] == []
	assert value["lines"] == []


DECIDE_CHILD = r"""
source = startRoute()
source.typeKey(ord("B"), False, False)
source.typeKey(ord("B"), False, True)
time.sleep(0.3)
plain = {
	"events": [[e["vk"], e["up"], e["injected"], e["reached"]] for e in delivered],
	"passed": sink.passed(),
}
delivered.clear()
before = len(sink.passed())
source.typeKey(VK_INSERT, True, False)
source.typeKey(ord("T"), False, False)
source.typeKey(ord("T"), False, True)
source.typeKey(VK_INSERT, True, True)
time.sleep(0.3)
chord = {
	"events": [[e["vk"], e["up"], e["nvda"], e["injected"]] for e in delivered],
	"passed": sink.passed()[before:],
}
before = len(sink.passed())
source.typeKey(ord("C"), False, False)
# Pressed again, as the controlling computer repeats a key held.
source.typeKey(ord("C"), False, False)
time.sleep(0.2)
held = sink.passed()[before:]
source.releaseTypedKeys()
time.sleep(0.3)
result = {
	"plain": plain,
	"chord": chord,
	"held": held,
	"released": sink.passed()[before:],
	"outcomes": outcomes(),
	"lines": typedLines(),
}
source.stop()
"""


@forcedSession
def test_typedKeysComeToNVDAAsInjectedAndThenTheApplication(tmp_path: Path):
	value = runTypingChild(tmp_path, f"VK_INSERT = {VK_INSERT}\n" + DECIDE_CHILD)
	b, c = code("b"), code("c")
	assert value["plain"] == {
		"events": [[ord("B"), False, True, True], [ord("B"), True, True, True]],
		"passed": [[b, True], [b, False]],
	}
	# The NVDA key with T is NVDA's command: neither key reaches the desktop.
	assert value["chord"] == {
		"events": [
			[VK_INSERT, False, True, True],
			[ord("T"), False, True, True],
			[ord("T"), True, False, True],
			[VK_INSERT, True, False, True],
		],
		"passed": [],
	}
	assert value["held"] == [[c, True]]
	assert value["released"] == [[c, True], [c, False]]
	# The second press of C, down already, was never sent.
	assert value["outcomes"] == [TYPED_OUT, TYPED_OUT] + [TYPED_KEPT] * 4 + [TYPED_OUT, TYPED_OUT]
	assert value["lines"] == []


SENT_ON_CHILD = r"""
source = startRoute()
# Every key NVDA's, as with input help on, so that the typed Tab is kept, as browse mode keeps it.
source.holdKeyboard(True)
source.typeKey(VK_TAB, False, False)
time.sleep(0.2)
kept = sink.passed()
# NVDA sends the Tab on while the controlling computer still holds it.
source.inject(VK_TAB, 0x0F, False, False)
source.inject(VK_TAB, 0x0F, False, True)
time.sleep(0.2)
sent = sink.passed()
source.typeKey(VK_TAB, False, True)
time.sleep(0.3)
result = {
	"kept": kept,
	"sent": sent,
	"after": sink.passed(),
	"events": [[e["vk"], e["up"], e["injected"]] for e in delivered],
	"outcomes": outcomes(),
}
source.stop()
"""


@forcedSession
def test_aTypedKeyStillHeldIsLetGoOfBeforeNVDASendsItOn(tmp_path: Path):
	"""The helper has a key typed down once: one the other computer still holds, kept, is let go of
	before NVDA sends it on, and the other computer's own release of it later types nothing."""
	value = runTypingChild(tmp_path, f"VK_TAB = {VK_TAB}\n" + SENT_ON_CHILD)
	assert value["kept"] == []
	assert value["sent"] == [[TAB, True], [TAB, False]]
	assert value["after"] == value["sent"]
	assert value["events"] == [[VK_TAB, False, True], [VK_TAB, True, True]]
	assert value["outcomes"] == [TYPED_KEPT, TYPED_KEPT, TYPED_OUT, TYPED_OUT]


NUMPAD_CHILD = r"""
source = startRoute()


def numLock():
	return bool(source.keyState(VK_NUMLOCK) & 1)


def typeTap(vk):
	before = len(sink.passed())
	mark = len(delivered)
	source.typeKey(vk, False, False)
	time.sleep(0.05)
	source.typeKey(vk, False, True)
	time.sleep(0.4)
	return {
		"passed": sink.passed()[before:],
		"downs": [[e["vk"], e["text"], e["injected"]] for e in delivered[mark:] if not e["up"]],
		"lights": sink.lights() & LOCK_NUM,
		"numLock": numLock(),
	}


off = typeTap(VK_NUMPAD5)
keyboard.press("Num_Lock")
wait(numLock, 3, "Num Lock did not come on")
on = typeTap(VK_NUMPAD5)
home = typeTap(VK_HOME)
source.stop()
result = {"off": off, "on": on, "home": home, "outcomes": outcomes(), "lines": typedLines()}
"""


@forcedSession
def test_aNumpadKeyTypedUnderTheOtherNumLockIsWrappedInTaps(tmp_path: Path):
	value = runTypingChild(
		tmp_path,
		f"VK_NUMPAD5, VK_HOME, VK_NUMLOCK, LOCK_NUM = {VK_NUMPAD5}, {VK_HOME}, {VK_NUMLOCK}, {LOCK_NUM}\n"
		+ NUMPAD_CHILD,
	)
	taps = [[NUM_LOCK, True], [NUM_LOCK, False]]
	# Num Lock off: turned on for the key and off again after it, and NVDA hears no Num Lock.
	assert value["off"] == {
		"passed": taps + [[KP_5, True], [KP_5, False]] + taps,
		"downs": [[VK_NUMPAD5, "5", True]],
		"lights": 0,
		"numLock": False,
	}
	assert value["on"] == {
		"passed": [[KP_5, True], [KP_5, False]],
		"downs": [[VK_NUMPAD5, "5", True]],
		"lights": LOCK_NUM,
		"numLock": True,
	}
	# Home off the numpad with Num Lock on: turned off for it, which makes it NVDA's review key, kept.
	assert value["home"]["passed"] == taps + taps
	assert value["home"]["downs"] == [[VK_HOME, "", True]]
	assert value["home"]["lights"] == LOCK_NUM and value["home"]["numLock"]
	assert value["lines"] == []


REFUSED_CHILD = r"""
source = startRoute()
keyboard.down("alt")
time.sleep(0.2)
source.typeKey(VK_SNAPSHOT, False, False)
time.sleep(0.3)
source.typeKey(VK_SNAPSHOT, False, True)
time.sleep(0.2)
keyboard.up("alt")
time.sleep(0.2)
result = {"typed": keyboard.typed, "passed": sink.passed(), "lines": typedLines(), "canSend": source.canSend}
source.stop()
"""


@forcedSession
def test_aRefusedKeyIsForgottenAndLoggedByItsTagAlone(tmp_path: Path):
	"""SysRq with Alt down on the virtual keyboard is refused; its release is then not typed at all, and
	the log names the TYPE's tag, never the key."""
	value = runTypingChild(tmp_path, f"VK_SNAPSHOT = {VK_SNAPSHOT}\n" + REFUSED_CHILD)
	assert value["typed"] == [[1, 0, TYPED_REFUSED_KEY]]
	assert value["passed"] == [[ALT, True], [ALT, False]]
	assert value["lines"] == [
		"linda-keyd's typed key 1 was refused, as the helper does not type that key now"
	]
	assert value["canSend"]


REFUSED_OUT_CHILD = r"""
source = startRoute()
k = source._kernel
alt = k._keycodeToType(VK_LMENU, False)
sysrq = k._keycodeToType(VK_SNAPSHOT, False)
# In one turn of the source's thread, as the controlling computer's Alt+Print come in one read: SysRq is
# taken, as no Alt is out yet, comes back and is passed, but the Alt ahead of it went out first.
k._post(lambda: (k._typeKey(alt, VK_LMENU, False, False), k._typeKey(sysrq, VK_SNAPSHOT, False, False)))
time.sleep(0.5)
source.typeKey(VK_SNAPSHOT, False, True)
time.sleep(0.1)
source.typeKey(VK_LMENU, False, True)
time.sleep(0.6)
events = [[e["vk"], e["up"], e["repeat"]] for e in delivered if e["vk"] == VK_SNAPSHOT]
held = {"events": events, "presses": sorted(k._presses), "typed": keyboard.typed[:]}
before = len(sink.passed())
source.typeKey(VK_SNAPSHOT, False, False)
time.sleep(0.2)
source.typeKey(VK_SNAPSHOT, False, True)
time.sleep(0.3)
result = {"held": held, "again": sink.passed()[before:], "outcomes": outcomes(), "lines": typedLines()}
source.stop()
"""


@forcedSession
def test_aTypedPressRefusedAsItGoesOutIsStillReleased(tmp_path: Path):
	"""A typed press the helper refuses only as it goes out, as keys ahead of it changed what is down,
	stays typed down there: its release still goes out, ends NVDA's press of it, and the key can be typed
	again."""
	value = runTypingChild(
		tmp_path, f"VK_LMENU, VK_SNAPSHOT = {VK_LMENU}, {VK_SNAPSHOT}\n" + REFUSED_OUT_CHILD
	)
	held = value["held"]
	# Refused as it went out, after its KEY came back.
	assert held["typed"][1][1] != 0
	assert [outcome for _tag, _seq, outcome in held["typed"]] == [
		TYPED_OUT,
		TYPED_REFUSED_KEY,
		TYPED_NOT_DOWN,
		TYPED_OUT,
	]
	events = held["events"]
	assert events[0] == [VK_SNAPSHOT, False, False]
	assert events[-1] == [VK_SNAPSHOT, True, False]
	assert [VK_SNAPSHOT, True, False] not in events[:-1]
	assert held["presses"] == []
	assert value["again"] == [[SYSRQ, True], [SYSRQ, False]]
	assert value["outcomes"][4:] == [TYPED_OUT, TYPED_OUT]
	assert value["lines"] == [
		"linda-keyd's typed key 2 was refused, as the helper does not type that key now",
		"linda-keyd's typed key 3 was refused, as its key was not typed down",
	]


INJECT_REFUSED_OUT_CHILD = r"""
source = startRoute()
k = source._kernel
alt = k._keycodeToType(VK_LMENU, False)
sysrq = k._keycodeToType(VK_SNAPSHOT, False)
# NVDA sends SysRq on while a typed Alt waits ahead of it: taken, then refused as the Alt goes out first.
k._post(lambda: (k._typeKey(alt, VK_LMENU, False, False), k._inject(sysrq, VK_SNAPSHOT, False)))
time.sleep(0.3)
source.inject(VK_SNAPSHOT, 0x37, False, True)
time.sleep(0.2)
source.typeKey(VK_LMENU, False, True)
time.sleep(0.2)
before = len(sink.passed())
source.inject(VK_SNAPSHOT, 0x37, False, False)
source.inject(VK_SNAPSHOT, 0x37, False, True)
time.sleep(0.3)
result = {"typed": keyboard.typed, "again": sink.passed()[before:]}
source.stop()
"""


@forcedSession
def test_anInjectedPressRefusedIsStillReleased(tmp_path: Path):
	"""An injected press may be refused as it goes out, when the helper has it typed down already, and
	no TYPED says which refusal it was: its release is sent all the same, so the key can be sent again."""
	value = runTypingChild(
		tmp_path, f"VK_LMENU, VK_SNAPSHOT = {VK_LMENU}, {VK_SNAPSHOT}\n" + INJECT_REFUSED_OUT_CHILD
	)
	assert [outcome for _tag, _seq, outcome in value["typed"]] == [
		TYPED_OUT,
		TYPED_REFUSED_KEY,
		TYPED_NOT_DOWN,
		TYPED_OUT,
		TYPED_OUT,
		TYPED_OUT,
	]
	assert value["again"] == [[SYSRQ, True], [SYSRQ, False]]


INJECT_NOTED_CHILD = r"""
noted, locks = [], []
source = startRoute()
k = source._kernel
source._environment.onPassed = lambda keycode, isUp: noted.append([keycode - 8, isUp])
source._environment.onPassedLock = lambda light, now: locks.append(light)
source.inject(VK_TAB, 0x0F, False, False)
source.inject(VK_TAB, 0x0F, False, True)
time.sleep(0.3)
tab = noted[:]
noted.clear()
keyboard.down("alt")
time.sleep(0.2)
source.inject(VK_SNAPSHOT, 0x37, False, False)
source.inject(VK_SNAPSHOT, 0x37, False, True)
time.sleep(0.2)
sysrq = [n for n in noted if n[0] == SYSRQ]
# SysRq with Alt down makes the kernel's SysRq handler active, which refuses every typed press.
keyboard.down(SYSRQ)
time.sleep(0.2)
source.inject(VK_NUMLOCK, 0x45, False, False)
source.inject(VK_NUMLOCK, 0x45, False, True)
time.sleep(0.2)
keyboard.up(SYSRQ)
keyboard.up("alt")
time.sleep(0.2)
result = {
	"tab": tab,
	"refused": sysrq + [n for n in noted if n[0] == NUM_LOCK],
	"locks": locks,
	"numLock": bool(source.keyState(VK_NUMLOCK) & 1),
	"outcomes": outcomes(),
}
source.stop()
"""


@forcedSession
def test_anInjectedKeyIsToldOfOnlyOnceItWentOut(tmp_path: Path):
	"""The environment hears of a key NVDA sends on as of a key passed, but only once the helper says it
	went out: one refused never reached the desktop, and a lock refused changed no light."""
	value = runTypingChild(
		tmp_path,
		f"VK_TAB, VK_SNAPSHOT, VK_NUMLOCK = {VK_TAB}, {VK_SNAPSHOT}, {VK_NUMLOCK}\n"
		f"SYSRQ, NUM_LOCK = {SYSRQ}, {NUM_LOCK}\n" + INJECT_NOTED_CHILD,
	)
	assert value["tab"] == [[TAB, False], [TAB, True]]
	assert value["outcomes"][:2] == [TYPED_OUT, TYPED_OUT]
	assert TYPED_OUT not in value["outcomes"][2:]
	assert value["refused"] == []
	assert value["locks"] == []
	assert not value["numLock"]


LOST_CHILD = r"""
from keydStandIn import RELEASED_BACKGROUND

noted = []
source = startRoute()
k = source._kernel
source._environment.onPassed = lambda keycode, isUp: noted.append([keycode - 8, isUp])
answer = threading.Event()


def keep(event):
	answer.wait(1.0)
	return True


source.typeKey(VK_INSERT, True, False)
wait(lambda: len(k._pending) > 0, 2, "no typed press waited")
keyboard.letGo(RELEASED_BACKGROUND)
time.sleep(0.5)
answer.set()
time.sleep(0.3)
result = {
	"typed": keyboard.typed,
	"noted": noted,
	"passed": sink.passed(),
	"events": [[e["vk"], e["up"], e["reached"]] for e in delivered],
}
source.stop()
"""


@forcedSession
def test_aTypedPressLostAsTheHelperLetsGoIsNotPassed(tmp_path: Path):
	"""The helper drops a typed press still waiting for NVDA as it lets go, where the user's own go out:
	it is told of to no one as passed, and NVDA hears it never reached the application."""
	value = runTypingChild(tmp_path, f"VK_INSERT = {VK_INSERT}\n" + LOST_CHILD)
	assert value["typed"] == [[1, 1, TYPED_LOST]]
	assert value["passed"] == []
	assert value["noted"] == []
	assert value["events"] == [[VK_INSERT, False, False], [VK_INSERT, True, False]]


WITHOUT_CHILD = r"""
announced = []
inputHook._announce = announced.append
source = startRoute()
inputHook._source = source
for _ in range(2):
	inputHook.keybdEvent(VK_TAB, 0x0F, 0, 0)
	inputHook.keybdEvent(VK_TAB, 0x0F, 2, 0)
	inputHook.typeKey(VK_TAB, False, False)
	inputHook.typeKey(VK_TAB, False, True)
time.sleep(0.3)
inputHook._source = None
result = {
	"announced": announced,
	"canType": source._kernel.canType,
	"passed": sink.passed(),
	"granted": keyboard.granted,
}
source.stop()
"""


@forcedSession
def test_aHelperThatGrantsNoTypingTypesNothing(tmp_path: Path):
	value = runTypingChild(tmp_path, f"VK_TAB = {VK_TAB}\n" + WITHOUT_CHILD, features=FEATURE_WAIT)
	assert value == {"announced": [NO_SENDER], "canType": False, "passed": [], "granted": FEATURE_WAIT}


X_SESSION_CHILD = r"""
source = startSource()
result = {
	"granted": keyboard.granted,
	"canType": source._kernel.canType,
	"sender": type(source._sender()).__name__,
}
source.stop()
keyboard.close()
"""


@forcedSession
def test_anXSessionAsksForNoTyping(tmp_path: Path):
	"""In an X session the kernel source never types: the route asks the helper for WAIT alone, as
	before, even of one that would grant typing, and keys are sent through the X source."""
	import test_kernelKeys

	value = test_kernelKeys.runKernelChild(tmp_path, X_SESSION_CHILD, standIn={"features": TYPING})
	assert value == {"granted": FEATURE_WAIT, "canType": False, "sender": "X11KeySource"}


# Linda, in the X rig with the session forced, or natively under a compositor.


@pytest.fixture
def desktop(tmp_path: Path) -> Iterator[DesktopSession]:
	with DesktopSession(tmp_path / "desktop") as session:
		yield session


@contextmanager
def standIn(features: int) -> Iterator[StandIn]:
	"""The stand-in, putting keys out where the session's programs have them."""
	directory = Path(tempfile.mkdtemp(prefix="keyd", dir="/tmp"))
	sink = keydStandIn.compositorSink() if desktopSession.compositor() is not None else None
	try:
		with StandIn(directory / "socket", features=features, sink=sink) as keyboard:
			yield keyboard
	finally:
		shutil.rmtree(directory, ignore_errors=True)


def lindaEnv(keyboard: StandIn) -> dict[str, str | None]:
	"""Linda's environment beside keyboard: in the X rig told it is in a Wayland session, with the X
	display's US English; under a compositor as it is, in the pl layout the rig's keyboard types."""
	if desktopSession.compositor() is None:
		return {
			"LINDA_TEST_KEYD_SOCKET": keyboard.path,
			"LINDA_SESSION_TYPE": "wayland",
			"XDG_CURRENT_DESKTOP": None,
			"XKB_DEFAULT_LAYOUT": "us",
		}
	return {"LINDA_TEST_KEYD_SOCKET": keyboard.path, "XKB_DEFAULT_LAYOUT": "pl"}


def numLockIsOn(keyboard: StandIn) -> bool:
	return bool(keyboard._sink.lights() & LOCK_NUM)


#: A page with two links, a menu button with its ARIA menu, and logs of the keys that reach it, where
#: its focus went and what its menu did.
TYPING_PAGE = """\
<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Typing page</title></head>
<body>
<h1>Typing page</h1>
<p><a href="#first" id="first">First link</a></p>
<p><a href="#second" id="second">Second link</a></p>
<p><button id="menuButton" aria-haspopup="menu" aria-expanded="false">Actions</button></p>
<ul id="menu" role="menu" aria-label="Actions" hidden>
<li role="menuitem" tabindex="-1">Open</li>
<li role="menuitem" tabindex="-1">Save</li>
</ul>
<div id="log" aria-label="Key log"></div>
<div id="focusLog" aria-label="Focus log"></div>
<div id="menuLog" aria-label="Menu log"></div>
<script>
const note = (id, name) => {
	const log = document.getElementById(id);
	log.textContent = log.textContent ? log.textContent + " " + name : name;
};
const modifiers = new Set(["Alt", "AltGraph", "CapsLock", "Control", "Meta", "NumLock", "Shift", "Super"]);
document.addEventListener("keydown", (event) => {
	if (!modifiers.has(event.key)) {
		note("log", event.key === " " ? "Space" : event.key);
	}
}, true);
document.addEventListener("focusin", (event) => {
	if (event.target.id) {
		note("focusLog", event.target.id);
	}
});
const button = document.getElementById("menuButton");
const menu = document.getElementById("menu");
const items = [...menu.querySelectorAll("[role=menuitem]")];
const showMenu = (open) => {
	if (menu.hidden !== open) {
		return;
	}
	menu.hidden = !open;
	button.setAttribute("aria-expanded", String(open));
	note("menuLog", open ? "menu-open" : "menu-closed");
	if (open) {
		items[0].focus();
	}
};
button.addEventListener("click", () => showMenu(menu.hidden));
menu.addEventListener("focusout", (event) => {
	if (!menu.contains(event.relatedTarget)) {
		showMenu(false);
	}
});
menu.addEventListener("keydown", (event) => {
	if (event.key === "Escape") {
		button.focus();
	}
});
</script>
</body></html>
"""


class TypingPage:
	"""The typing page's logs, read straight from Firefox."""

	def __init__(self) -> None:
		Atspi = self._Atspi = desktopSession._atspi()
		root = Atspi.get_desktop(0)
		root.clear_cache()
		for index in range(root.get_child_count()):
			app = root.get_child_at_index(index)
			if app is None or app.get_toolkit_name() != "Gecko":
				continue
			documents = [
				d
				for d in browseKeys._matches(Atspi, app, Atspi.Role.DOCUMENT_WEB)
				if d.get_name() == "Typing page"
			]
			if documents:
				break
		else:
			raise AssertionError("no Firefox shows the typing page")
		self._logs = {name: browseKeys._byId(documents[0], name) for name in ("log", "focusLog", "menuLog")}
		assert all(self._logs.values()), self._logs
		for name in self._logs:
			self.read(name)
		time.sleep(webSession.CACHE_FILL_SECONDS)

	def read(self, name: str) -> list[str]:
		return self._Atspi.Text.get_text(self._logs[name], 0, -1).split()

	def waitFor(self, name: str, expected: list[str], timeout: float = 3) -> None:
		deadline = time.monotonic() + timeout
		while (logged := self.read(name)) != expected:
			assert time.monotonic() < deadline, f"the page's {name} holds {logged}, not {expected}"
			time.sleep(0.02)


def startOnPage(
	desktop: DesktopSession, tmp_path: Path, keyboard: StandIn
) -> tuple[harness.Launch, TypingPage]:
	webSession.requireBrowseLibraries()
	page = tmp_path / "typing.html"
	page.write_text(TYPING_PAGE, encoding="utf-8")
	webSession.startFirefox(desktop, page.as_uri())
	nvda = desktop.startNVDA(
		browseKeys.browseConfig(tmp_path), env={**browseKeys.probeHooks(tmp_path), **lindaEnv(keyboard)}
	)
	nvda.waitForLog("Buffer rendered", browseKeys.BUFFER_BOUND)
	browseKeys.waitForCapture(nvda, "browse", timeout=browseKeys.BUFFER_BOUND)
	keyboard.waitFor(lambda: keyboard.capturing, 10)
	time.sleep(browseKeys.SETTLE)
	return nvda, TypingPage()


def stepOn(desktop: DesktopSession, keyboard: StandIn, chord: str, *expected: str) -> int:
	mark = desktop.mark()
	keyboard.press(chord)
	browseKeys.expect(desktop, *expected, since=mark)
	time.sleep(browseKeys.SETTLE)
	return mark


@everySession
def test_browseModeSendsTabAndEscapeOnceEach(desktop: DesktopSession, tmp_path: Path):
	"""Tab in browse mode moves Firefox's focus once, by NVDA's own move to the first link and by the
	Tab NVDA sends on from there; Escape, sent on in an ARIA menu, reaches the page and closes it."""
	with standIn(TYPING) as keyboard:
		nvda, page = startOnPage(desktop, tmp_path, keyboard)
		stepOn(desktop, keyboard, "control+Home", "heading level 1 Typing page")
		stepOn(desktop, keyboard, "Tab", "First link" + PREFIX_MARK)
		page.waitFor("focusLog", ["first"])
		assert page.read("log") == []
		stepOn(desktop, keyboard, "Tab", "Second link" + PREFIX_MARK)
		page.waitFor("focusLog", ["first", "second"])
		page.waitFor("log", ["Tab"])
		time.sleep(browseKeys.SETTLE)
		assert page.read("focusLog") == ["first", "second"]
		assert page.read("log") == ["Tab"]

		stepOn(desktop, keyboard, "b", "Actions" + PREFIX_MARK)
		keyboard.press("Return")
		page.waitFor("menuLog", ["menu-open"])
		browseKeys.waitForCapture(nvda, "focus")
		time.sleep(browseKeys.SETTLE)
		keyboard.press("Escape")
		page.waitFor("menuLog", ["menu-open", "menu-closed"])
		time.sleep(browseKeys.SETTLE)
		assert page.read("log").count("Escape") == 1, page.read("log")
		assert page.read("focusLog")[-1] == "menuButton", page.read("focusLog")
		nvda.process.terminate()
		nvda.waitForExit(20)
	nvda.assertClean(logNames=("nvda.log",))
	assert "linda-keyd's typed key" not in nvda.text("nvda.log")


@everySession
def test_withoutTypingTheCannotTypeLineIsSpokenOnce(desktop: DesktopSession, tmp_path: Path):
	with standIn(FEATURE_WAIT) as keyboard:
		nvda, page = startOnPage(desktop, tmp_path, keyboard)
		stepOn(desktop, keyboard, "control+Home", "heading level 1 Typing page")
		mark = stepOn(desktop, keyboard, "Tab", "First link" + PREFIX_MARK)
		cannot = lindaSays(NO_SENDER)
		stepOn(desktop, keyboard, "Tab", cannot)
		keyboard.press("Tab")
		time.sleep(1)
		assert desktop.spokenSince(mark).count(cannot) == 1, desktop.spokenSince(mark)
		assert page.read("log") == []
		assert page.read("focusLog") == ["first"]
		nvda.process.terminate()
		nvda.waitForExit(20)
	nvda.assertClean(logNames=("nvda.log",))


class Follower:
	"""The GTK 4 key probe holding the keyboard, Linda beside it as Remote Access's follower, and the
	peer joined as the leader controlling it."""

	def __init__(self, desktop: DesktopSession, tmp_path: Path, keyboard: StandIn) -> None:
		self.probe = Lines([sys.executable, str(KEY_PROBE)], tmp_path / "probe.stderr.txt")
		if desktopSession.compositor() is None:
			desktopSession.activate("Key probe")
		self.probe.waitFor(lambda line: line == "active", 15)
		hooks = tmp_path / "hooks"
		(hooks / "globalPlugins").mkdir(parents=True)
		(hooks / "synthDrivers").symlink_to(harness.HERE / "synthDrivers")
		self.nvda = desktop.startNVDA(
			remoteConfig(tmp_path / "config", 0),
			env={"LINDA_TEST_HOOKS_DIR": str(hooks), **lindaEnv(keyboard)},
		)
		desktop.expect("Key probe", since=0, timeout=STARTUP_TIMEOUT)
		keyboard.waitFor(lambda: keyboard.capturing, 10)
		self.nvda.waitForLog("Control connector connected", STARTUP_TIMEOUT)
		self.peer = RemotePeer(PORT, CHANNEL, remotePeer.LEADER)
		self.nvda.waitForLog("Client connected: {'id': 2", 5)

	def tap(self, vk: int, extended: bool = False, mods: tuple[tuple[int, bool], ...] = ()) -> None:
		for mod, modExtended in mods:
			self.peer.key(mod, True, modExtended)
			time.sleep(0.03)
		self.peer.key(vk, True, extended)
		time.sleep(0.03)
		self.peer.key(vk, False, extended)
		time.sleep(0.03)
		for mod, modExtended in reversed(mods):
			self.peer.key(mod, False, modExtended)
			time.sleep(0.03)

	def type(self, text: str) -> None:
		for char in text:
			self.tap(ord(char.upper()), mods=((VK_LSHIFT, False),) if char.isupper() else ())

	def waitForText(self, text: str, timeout: float = 5) -> None:
		self.probe.waitFor(lambda line: line == f"text {text}", timeout)

	def close(self) -> harness.Launch:
		self.peer.close()
		self.nvda.process.send_signal(signal.SIGTERM)
		self.nvda.waitForExit(20)
		self.probe.stop()
		return self.nvda


@everySession
def test_remoteAccessTypesIntoAGtk4Entry(desktop: DesktopSession, tmp_path: Path):
	"""The leader types Ab into the GTK 4 entry, its NVDA key with T reads the follower's title, and a
	numpad 5 typed with the follower's Num Lock off arrives as 5 and leaves Num Lock off."""
	with standIn(TYPING) as keyboard:
		follower = Follower(desktop, tmp_path, keyboard)
		try:
			follower.type("Ab")
			follower.waitForText("Ab")
			mark = follower.peer.mark()
			desktopMark = desktop.mark()
			follower.tap(ord("T"), mods=((VK_INSERT, True),))
			desktop.expect("Key probe", since=desktopMark)
			follower.peer.waitFor(
				lambda m: m["type"] == "speak" and "Key probe" in remotePeer.spokenText(m), since=mark
			)
			time.sleep(0.5)
			texts = [line for line in follower.probe.lines if line.startswith("text ")]
			assert texts[-1] == "text Ab", follower.probe.lines[-5:]

			assert not numLockIsOn(keyboard)
			given = len(follower.probe.lines)
			follower.tap(VK_NUMPAD5)
			follower.waitForText("Ab5")
			time.sleep(0.5)
			assert "pressed KP_5" in follower.probe.lines[given:], follower.probe.lines[given:]
			assert not numLockIsOn(keyboard)

			follower.type(MARKER)
			follower.waitForText("Ab5" + MARKER)
		finally:
			launch = follower.close()
	launch.assertClean(logNames=("nvda.log",))
	log = launch.text("nvda.log")
	assert "Input: kb(desktop):NVDA+t" in log
	harness.assertMarkerAbsent(MARKER, {"nvda.log": log})
