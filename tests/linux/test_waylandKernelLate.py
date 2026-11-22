"""The kernel source's late pass, with an environment in place of the X server, as in a Wayland session:
an Alt or Super kept under the NVDA key goes out on linda-keyd's virtual keyboard by LATE_PASS just
ahead of a key NVDA passes, when the helper grants FEATURE_LATE_PASS, and its release follows the hand.
Also the source's look at the focus while a chord that may have moved it is unsettled, and a typed
press the helper loses as it lets go.

keydStandIn stands in for the helper. Most tests build the kernel source in a child with a fake
environment and no X display, and run in any of the rig's sessions; one runs the Wayland route itself,
the session forced, in the X rig; under sway one puts the keys out on the compositor's keyboard, where
the GTK 4 key probe shows what a program is given:

	nix develop . -c tests/linux/rig python3 -m pytest -q tests/linux/test_waylandKernelLate.py
	nix develop . -c tests/linux/wayland-gate SCRATCH -k test_waylandKernelLate
"""

import time
from pathlib import Path

import harness
import pytest
from keydStandIn import FEATURE_LATE_PASS, FEATURE_TYPING, FEATURE_WAIT, code
from test_waylandRoute import runRouteChild

ALL = FEATURE_WAIT | FEATURE_TYPING | FEATURE_LATE_PASS
NO_LATE_PASS = FEATURE_WAIT | FEATURE_TYPING
ALT, SUPER, TAB, INSERT, X_KEY, F12 = (code(name) for name in ("alt", "super", "Tab", "Insert", "x", "F12"))
#: The Menu key, which altwin:menu_win makes Super, and which LATE_PASS may not name.
MENU = 127
VK_TAB, VK_MENU, VK_LWIN, VK_F12, VK_X = 0x09, 0x12, 0x5B, 0x7B, 0x58
RELEASED_BACKGROUND, TYPED_LOST = 3, 7
#: The process NVDA names as the document's.
DOCUMENT_PROCESS = 4321

#: The kernel source on a connection to the stand-in, with a fake environment and no X display: opening
#: one raises and is recorded. NVDA passes the captured presses whose virtual keys are in passing and
#: keeps every other. The stand-in grants GRANTS of what the source asks, which is all the route asks.
LATE_CHILD = r"""
import logging, sys, tempfile, threading, time
from dataclasses import dataclass

sys.path.insert(0, str(harness.HERE))
import Xlib.display

opened = []


def refuse(*args, **kwargs):
	opened.append(True)
	raise OSError("the X display is not to be opened")


Xlib.display.Display = refuse
import keydStandIn
from keydStandIn import StandIn
from logHandler import log
from linuxPlatform.keys import keydClient, kernel, x11

ASKED = (
	keydClient.FEATURE_WAIT
	| keydClient.FEATURE_TYPING
	| keydClient.FEATURE_LATE_PASS
	| keydClient.FEATURE_VIRTUAL_SEEN
)


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
sink = keydStandIn.compositorSink() if ON_COMPOSITOR else None
keyboard = StandIn(path, toX=ON_COMPOSITOR, sink=sink, features=GRANTS).start()
delivered = []
passing = set()


def deliver(event):
	delivered.append(
		{
			"vk": event.vkCode,
			"up": event.isUp,
			"captured": event.captured,
			"reached": event.reachedApplication,
			"injected": event.injected,
		}
	)
	if event.isUp or not event.captured:
		return True
	return event.vkCode in passing


@dataclass
class Layout:
	names: tuple = ("evdev", "pc105", "us", "", "")
	group: int = 0
	repeat: tuple | None = None
	locksGuess: int | None = None
	source: str = "test"
	inputMethod: bool = False
	notice: str | None = None


class Environment:
	def __init__(self, layout=Layout(), state=x11.NO_CAPTURE, after=None, lookAfter=None, until=0.3):
		self.layout = layout
		self.state = state
		self.after = after
		self.lookAfter = lookAfter
		self.until = until
		self.canLook = True
		self.source = None
		self.calls = []
		self.looks = []

	def _note(self, *what):
		self.calls.append([time.monotonic(), *what])

	def attach(self, source):
		self.source = source
		source.useLayout(self.layout)

	def detach(self, source):
		self._note("detach")

	@property
	def captureState(self):
		return self.state

	def captureNow(self, now):
		return self.state

	def requestLook(self, number, now):
		self.looks.append(number)
		if self.lookAfter is not None:
			threading.Timer(self.lookAfter, self._answer, (number,)).start()
		return now + self.until

	def _answer(self, number):
		if self.after is not None:
			self.state = self.after
		self.source.lookDone(number, True)

	def noteFocusMayMove(self, now):
		self._note("focusMayMove")

	def noteDevice(self, status):
		self._note("device", status)

	def notePassed(self, keycode, isUp):
		self._note("passed", keycode, isUp)

	def notePassedLock(self, light, now):
		self._note("passedLock", light)

	def noteLocks(self, locks, now):
		self._note("locks", locks)

	def noteVirtual(self, status):
		self._note("virtual", status)


class SettlingEnvironment(Environment):
	def __init__(self, settled, **kwargs):
		super().__init__(**kwargs)
		self.isSettled = settled
		self.asked = 0

	def settled(self, now):
		self.asked += 1
		if self.isSettled == "raises":
			raise RuntimeError("settled")
		return self.isSettled


def startSource(environment, mode="browse"):
	sock, welcome = keydClient.connect(path, 5, ASKED)
	source = kernel.KernelKeySource(sock, welcome, environment=environment)
	source.setConsumed(x11.EXTENDED_INSERT, False)
	source.setCapture(mode)
	source.start(deliver, lambda: mode)
	keyboard.waitFor(lambda: keyboard.capturing)
	time.sleep(0.1)
	return source


def step(action, key, pause=0.06):
	getattr(keyboard, action)(key)
	time.sleep(pause)


def emitted():
	return [[evdev, down] for evdev, down in keyboard.emitted()]


def latePasses():
	return [list(entry) for entry in keyboard.latePasses]


def pressSeqs(evdev):
	return [e[2] for e in keyboard.events if e[1] == "key" and e[3] == evdev and e[4] == 1]


def passedNotes(environment):
	return [call[2:] for call in environment.calls if call[1] == "passed"]


def goodbyes():
	return [e[2] for e in keyboard.events if e[1] == "goodbye"]
"""


def runLateChild(
	tmp_path: Path, code: str, *, grants: int = ALL, onCompositor: bool = False
) -> harness.ChildResult:
	constants = (
		f"ON_COMPOSITOR = {onCompositor!r}\nGRANTS = {grants!r}\n"
		f"ALT, SUPER, TAB, INSERT, X_KEY, F12, MENU = {ALT}, {SUPER}, {TAB}, {INSERT}, {X_KEY}, {F12}, {MENU}\n"
		f"VK_TAB, VK_MENU, VK_LWIN, VK_F12, VK_X = {VK_TAB}, {VK_MENU}, {VK_LWIN}, {VK_F12}, {VK_X}\n"
		f"RELEASED_BACKGROUND, DOCUMENT_PROCESS = {RELEASED_BACKGROUND}, {DOCUMENT_PROCESS}\n"
	)
	done = harness.runChild(tmp_path, constants + LATE_CHILD + code, env={"DISPLAY": None}, timeout=90)
	assert done.error is None, f"{done.error}\n{done.stderr[-6000:]}"
	assert done.value["opened"] == []
	return done


# The late pass.

ALT_TAB_CHILD = r"""
environment = Environment()
source = startSource(environment)
passing.add(VK_TAB)
step("down", "Insert")
step("down", "alt")
step("down", "Tab")
step("up", "Tab")
held = bool(source.keyState(VK_MENU) & 0x8000)
altUpAt = time.monotonic()
step("up", "alt")
step("up", "Insert", 0.3)
altOut = [e[0] for e in keyboard.events if e[1] == "x" and e[2] == ALT and not e[3]]
result = {
	"opened": opened,
	"latePasses": latePasses(),
	"seqs": [pressSeqs(ALT), pressSeqs(TAB)],
	"emitted": emitted(),
	"altOutAfterHand": [t >= altUpAt for t in altOut],
	"held": held,
	"heldAfter": bool(source.keyState(VK_MENU) & 0x8000),
	"passed": passedNotes(environment),
	"mayMoveAfterAltUp": [call[0] >= altUpAt for call in environment.calls if call[1] == "focusMayMove"],
	"goodbyes": goodbyes(),
	"holding": source.isHolding,
	"lines": [line for line in logged.lines if "goes out ahead" in line],
}
source.stop()
"""


def test_aKeptAltGoesOutAheadOfAKeyPassedAndUpWithTheHand(tmp_path: Path):
	"""Insert and Alt kept, then Tab passed: Alt goes on the virtual keyboard just before Tab, by
	LATE_PASS, is down to the applications from then on, and its release goes out only when the user
	lets it go, which the environment hears may move the focus."""
	value = runLateChild(tmp_path, ALT_TAB_CHILD).value
	[alt], [tab] = value["seqs"]
	assert value["latePasses"] == [[alt, tab, True]]
	assert value["emitted"] == [[ALT, True], [TAB, True], [TAB, False], [ALT, False]]
	assert value["altOutAfterHand"] == [True]
	assert value["held"] and not value["heldAfter"]
	assert value["passed"] == [[ALT + 8, False], [TAB + 8, False], [TAB + 8, True], [ALT + 8, True]]
	assert True in value["mayMoveAfterAltUp"]
	assert value["goodbyes"] == [] and value["holding"]
	assert value["lines"] == [f"linda-keyd's key {alt} goes out ahead of key {tab}"]


def test_withoutTheFeatureNothingIsSentAndTheKeyGoesAlone(tmp_path: Path):
	value = runLateChild(tmp_path, ALT_TAB_CHILD, grants=NO_LATE_PASS).value
	assert value["latePasses"] == []
	assert value["emitted"] == [[TAB, True], [TAB, False]]
	assert not value["held"]
	assert value["passed"] == [[TAB + 8, False], [TAB + 8, True]]
	assert value["goodbyes"] == [] and value["holding"]
	assert value["lines"] == []


TWICE_CHILD = r"""
environment = Environment()
source = startSource(environment)
passing.add(VK_TAB)
step("down", "Insert")
step("down", "alt")
for _ in range(2):
	step("down", "Tab")
	step("up", "Tab")
step("up", "alt")
step("up", "Insert", 0.3)
result = {
	"opened": opened,
	"latePasses": latePasses(),
	"seqs": [pressSeqs(ALT), pressSeqs(TAB)],
	"emitted": emitted(),
	"goodbyes": goodbyes(),
	"holding": source.isHolding,
}
source.stop()
"""


def test_aKeptAltIsNamedOnceForEveryKeyPassedWithIt(tmp_path: Path):
	"""The helper ends the connection over a LATE_PASS that names a kept press twice: Alt+Tab+Tab under
	the NVDA key names Alt once, and the second Tab goes out under the Alt already down."""
	value = runLateChild(tmp_path, TWICE_CHILD).value
	[alt], [first, _second] = value["seqs"]
	assert value["latePasses"] == [[alt, first, True]]
	assert value["emitted"] == [
		[ALT, True],
		[TAB, True],
		[TAB, False],
		[TAB, True],
		[TAB, False],
		[ALT, False],
	]
	assert value["goodbyes"] == [] and value["holding"]


BOTH_CHILD = r"""
environment = Environment()
source = startSource(environment)
passing.add(VK_TAB)
step("down", "Insert")
step("down", "alt")
step("down", "super")
step("down", "Tab")
step("up", "Tab")
step("up", "super")
step("up", "alt")
step("up", "Insert", 0.3)
result = {
	"opened": opened,
	"latePasses": latePasses(),
	"seqs": [pressSeqs(ALT), pressSeqs(SUPER), pressSeqs(TAB)],
	"emitted": emitted(),
	"goodbyes": goodbyes(),
}
source.stop()
"""


def test_altAndSuperKeptGoOutInTheOrderTheyWentDown(tmp_path: Path):
	value = runLateChild(tmp_path, BOTH_CHILD).value
	[alt], [win], [tab] = value["seqs"]
	assert value["latePasses"] == [[alt, tab, True], [win, tab, True]]
	assert value["emitted"] == [
		[ALT, True],
		[SUPER, True],
		[TAB, True],
		[TAB, False],
		[SUPER, False],
		[ALT, False],
	]
	assert value["goodbyes"] == []


MENU_CHILD = r"""
environment = Environment(layout=Layout(names=("evdev", "pc105", "us", "", "altwin:menu_win")))
source = startSource(environment)
passing.add(VK_TAB)
step("down", "Insert")
step("down", MENU)
step("down", "Tab")
step("up", "Tab")
step("up", MENU)
step("up", "Insert", 0.3)
result = {
	"opened": opened,
	"menuIsSuper": bool(source._tables.keyMods.get(MENU + 8, 0) & 0x40),
	"latePasses": latePasses(),
	"emitted": emitted(),
	"goodbyes": goodbyes(),
	"holding": source.isHolding,
}
source.stop()
"""


def test_aKeptModifierTheHelperCannotNameStaysKept(tmp_path: Path):
	"""Under altwin:menu_win the Menu key is Super and is kept under the NVDA key as Super is, but it is
	not among the helper's modifiers, and a LATE_PASS naming it would end the connection: none is sent,
	and Tab goes out alone, as without the feature."""
	value = runLateChild(tmp_path, MENU_CHILD).value
	assert value["menuIsSuper"]
	assert value["latePasses"] == []
	assert value["emitted"] == [[TAB, True], [TAB, False]]
	assert value["goodbyes"] == [] and value["holding"]


@pytest.mark.rigSession("x11")
def test_theWaylandRoutePutsAltOutAheadOfTheKey(tmp_path: Path):
	"""The route itself, the session forced, asks for the feature and late-passes as the source does."""
	value = runRouteChild(
		tmp_path,
		f"VK_TAB = {VK_TAB}\n"
		+ r"""
keep = lambda event: event.vkCode != VK_TAB
source = startRoute()
for action, key in (("down", "Insert"), ("down", "alt"), ("down", "Tab"), ("up", "Tab"), ("up", "alt"), ("up", "Insert")):
	getattr(keyboard, action)(key)
	time.sleep(0.06)
time.sleep(0.3)
result = {"passed": sink.passed(), "latePasses": [list(entry) for entry in keyboard.latePasses], "opened": opened}
source.stop()
""",
		standIn={"features": ALL},
	)
	assert value["opened"] == []
	assert value["passed"] == [[ALT, True], [TAB, True], [TAB, False], [ALT, False]]
	assert [applied for _kept, _waiting, applied in value["latePasses"]] == [True]


@pytest.fixture(scope="module")
def probe():
	from test_waylandRig import Probe

	p = Probe()
	yield p
	p.close()


@pytest.mark.rigSession("sway")
@pytest.mark.parametrize("grants", [ALL, NO_LATE_PASS], ids=["granted", "notGranted"])
def test_underSwayTheProgramIsGivenAltWithTheKey(tmp_path: Path, probe, grants: int):
	probe.drain()
	value = runLateChild(tmp_path, ALT_TAB_CHILD, grants=grants, onCompositor=True).value
	time.sleep(0.3)
	keys = [line for line in probe.drain() if not line.startswith("text ")]
	if grants & FEATURE_LATE_PASS:
		assert keys == ["pressed Alt_L", "pressed Tab", "released Tab", "released Alt_L"]
		assert len(value["latePasses"]) == 1
	else:
		assert keys == ["pressed Tab", "released Tab"]
		assert value["latePasses"] == []


# The look while a chord that may move the focus is unsettled.

LOOK_CHILD = r"""
named = x11.CaptureState(0, None, False, MODE, None, None, DOCUMENT_PROCESS)
document = x11.CaptureState(1, DOCUMENT_PROCESS, True, "browse", None, None, DOCUMENT_PROCESS)
if SETTLED == "absent":
	environment = Environment(state=named, after=document, lookAfter=0.05)
else:
	environment = SettlingEnvironment(SETTLED, state=named, after=document, lookAfter=0.05)
source = startSource(environment, mode=MODE)
step("down", "x")
step("up", "x", 0.4)
result = {
	"opened": opened,
	"looks": len(environment.looks),
	"asked": getattr(environment, "asked", None),
	"waits": len(keyboard.waits),
	"emitted": emitted(),
	"x": [[e["up"], e["captured"]] for e in delivered if e["vk"] == VK_X],
}
source.stop()
"""


@pytest.mark.parametrize(
	("settled", "mode", "looks"),
	[
		(False, "browse", True),
		(False, "off", False),
		(True, "browse", False),
		("absent", "browse", False),
		("raises", "browse", False),
	],
)
def test_aLetterLooksWhileNVDAHasNamedADocumentAndTheFocusIsUnsettled(
	tmp_path: Path, settled: bool | str, mode: str, looks: bool
):
	"""With no window known for a document's keys, NVDA naming one in browse mode while a chord that may
	move the focus is unsettled makes a letter wait for the environment's look, which here finds the
	document's window focused, and NVDA keeps the letter. NVDA in no document, a settled focus, an
	environment that cannot say, and one that raises asking, pass it at once, as before."""
	done = runLateChild(
		tmp_path, f"SETTLED, MODE = {settled!r}, {mode!r}\n" + LOOK_CHILD, grants=FEATURE_WAIT
	)
	value = done.value
	if looks:
		assert value["looks"] == 1 and value["waits"] == 1
		assert value["emitted"] == []
		assert value["x"] == [[False, True], [True, False]]
	else:
		assert value["looks"] == 0 and value["waits"] == 0
		assert value["emitted"] == [[X_KEY, True], [X_KEY, False]]
		assert [captured for _up, captured in value["x"]] == [False, False]
	if settled in (False, True) and mode == "browse":
		assert value["asked"] == 1
	errors = [line for line in done.logErrors if "whether the focus is settled" in line]
	assert len(errors) == (1 if settled == "raises" else 0), done.logErrors
	assert all("RuntimeError" in line for line in errors)


# A typed press passed, lost as the helper lets go.

LOST_CHILD = r"""
away = x11.CaptureState(1, None, False, "browse", None, None, None)
environment = Environment(state=away)
source = startSource(environment)
step("down", "x", 0.03)
source.typeKey(VK_F12, False, False)
keyboard.waitFor(lambda: any(e[1] == "key" and e[3] == F12 for e in keyboard.events))
time.sleep(0.05)
keyboard.letGo(RELEASED_BACKGROUND)
time.sleep(0.3)
step("up", "x", 0.1)
result = {
	"opened": opened,
	"typed": [list(entry) for entry in keyboard.typed],
	"seq": pressSeqs(F12),
	"f12": [[e["up"], e["injected"], e["reached"]] for e in delivered if e["vk"] == VK_F12],
	"passed": [note for note in passedNotes(environment) if note[0] == F12 + 8],
	"emitted": [entry for entry in emitted() if entry[0] == F12],
}
source.stop()
"""


def test_aTypedPressLostAsTheHelperLetsGoNeverReachedTheApplication(tmp_path: Path):
	"""A typed key passed, still behind a press the helper holds as it lets go, is answered LOST. The
	press held is a letter waiting on a look, which NVDA is given first, so NVDA has not yet been given
	the typed key: it never is, nor its release, as the application never had it. Before, NVDA was given
	both, as having reached the application. The environment, told it passed, hears of its release."""
	value = runLateChild(tmp_path, LOST_CHILD).value
	[seq] = value["seq"]
	assert value["typed"] == [[1, seq, TYPED_LOST]]
	assert value["emitted"] == []
	assert value["f12"] == []
	assert value["passed"] == [[F12 + 8, False], [F12 + 8, True]]
