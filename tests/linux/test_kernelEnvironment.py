"""The kernel key source with an environment in place of the X server, as in a Wayland session: the
layout and a document's keys come from a fake environment, keydStandIn stands in for the helper and
puts nothing out, and the X display is never opened. Run under the rig:

	nix develop . -c tests/linux/rig python3 -m pytest tests/linux/test_kernelEnvironment.py
"""

import re
from pathlib import Path

import harness
import pytest

PROTOCOL = Path(__file__).resolve().parents[2] / "engine/crates/keyd/src/protocol.rs"

#: The kernel source on a connection to the stand-in, with a fake environment and no X display: the
#: variable is unset, and opening one raises and is recorded. Its NVDA keeps every captured press
#: once it has taken nvdaTakes seconds over it, and records each event as it comes.
ENVIRONMENT_CHILD = r"""
import os, struct, sys, tempfile, threading, time
from dataclasses import dataclass
sys.path.insert(0, str(harness.HERE))
import Xlib.display

opened = []


def refuse(*args, **kwargs):
	opened.append(True)
	raise OSError("the X display is not to be opened")


Xlib.display.Display = refuse
from keydStandIn import FEATURE_WAIT, LOCKS, StandIn
from linuxPlatform.keys import keydClient, kernel, x11

socketDir = tempfile.mkdtemp(prefix="keyd", dir="/tmp")
path = socketDir + "/socket"
keyboard = StandIn(path, toX=False, **STAND_IN).start()
delivered = []
nvdaTakes = {"seconds": 0.0}


def deliver(event):
	delivered.append(
		{
			"vk": event.vkCode,
			"up": event.isUp,
			"repeat": event.repeat,
			"captured": event.captured,
			"nvda": event.nvdaKeyGrab,
			"reached": event.reachedApplication,
			"t": time.monotonic(),
		}
	)
	if event.isUp or not event.captured:
		return True
	time.sleep(nvdaTakes["seconds"])
	return False


@dataclass
class Layout:
	names: tuple = ("evdev", "pc105", "us", "", "")
	group: int = 0
	repeat: tuple | None = None
	locksGuess: int | None = None
	source: str = "test"
	inputMethod: bool = False
	notice: str | None = None


#: The window a document's keys are taken on, away from the focus until a look finds it there.
AWAY = x11.CaptureState(1, None, False, "browse", None, None, None)


class FakeEnvironment:
	def __init__(
		self, layout=Layout(), giveLayout=True, lookAfter=None, known=True, foundFocused=True, until=0.3, state=x11.NO_CAPTURE
	):
		self.layout = layout
		self.giveLayout = giveLayout
		self.lookAfter = lookAfter
		self.known = known
		self.foundFocused = foundFocused
		self.until = until
		self.state = state
		self.canLook = True
		self.source = None
		self.calls = []
		self.looks = []
		self.lookEnds = []

	def _note(self, *what):
		self.calls.append([time.monotonic(), *what])

	def attach(self, source):
		self._note("attach", keyboard.starts)
		self.source = source
		if self.giveLayout:
			source.useLayout(self.layout)

	def detach(self, source):
		self._note("detach")

	@property
	def captureState(self):
		return self.state

	def captureNow(self, now):
		return self.state

	def requestLook(self, number, now):
		self.looks.append([number, now])
		if self.lookAfter is not None:
			threading.Timer(self.lookAfter, self._answer, (number,)).start()
		return now + self.until

	def _answer(self, number):
		if self.foundFocused:
			self.state = self.state._replace(focused=True)
		self.lookEnds.append(time.monotonic())
		self.source.lookDone(number, self.known)

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


def startSource(environment, consumed=x11.EXTENDED_INSERT, mode="browse"):
	sock, welcome = keydClient.connect(path, 5)
	source = kernel.KernelKeySource(sock, welcome, environment=environment)
	source.setConsumed(consumed, False)
	source.setCapture(mode)
	source.start(deliver, lambda: mode)
	keyboard.waitFor(lambda: keyboard.capturing)
	time.sleep(0.1)
	return source


def keySeqs(evdev):
	return [e[2] for e in keyboard.events if e[1] == "key" and e[3] == evdev and e[4] == 1]


def verdictOf(seq):
	return [[keep, latency] for s, keep, latency in keyboard.verdicts if s == seq]


def expired():
	return [e[2] for e in keyboard.events if e[1] == "expired"]
"""


def runEnvironmentChild(
	tmp_path: Path, code: str, standIn: dict | None = None, **kwargs
) -> harness.ChildResult:
	done = harness.runChild(
		tmp_path,
		f"STAND_IN = {standIn or {}!r}\n" + ENVIRONMENT_CHILD + code,
		env={"DISPLAY": None},
		timeout=90,
		**kwargs,
	)
	assert done.error is None, f"{done.error}\n{done.stderr[-6000:]}"
	return done


CHORD_CHILD = r"""
environment = FakeEnvironment()
source = startSource(environment)
startedAt = [e[0] for e in keyboard.events if e[1] == "start"]
keyboard.press("Insert+t")
time.sleep(0.3)
result = {
	"opened": opened,
	"display": os.environ.get("DISPLAY"),
	"attachedBeforeStart": environment.calls[0][2],
	"attachedAt": environment.calls[0][0],
	"startedAt": startedAt,
	"downs": [[e["vk"], e["captured"], e["nvda"], e["reached"]] for e in delivered if not e["up"]],
	"verdicts": [keep for _seq, keep, _latency in keyboard.verdicts],
	"emitted": keyboard.emitted(),
	"devices": [call[2] for call in environment.calls if call[1] == "device"],
}
source.stop()
result["detached"] = [call[1] for call in environment.calls].count("detach")
keyboard.close()
"""


def test_nvdaChordIsKeptAndDeliveredWithNoXDisplay(tmp_path: Path):
	value = runEnvironmentChild(tmp_path, CHORD_CHILD).value
	assert value["opened"] == [] and value["display"] is None
	# The layout was there before the helper was asked to capture.
	assert value["attachedBeforeStart"] == 0
	assert len(value["startedAt"]) == 1 and value["attachedAt"] < value["startedAt"][0]
	assert value["downs"] == [[0x2D, True, True, False], [ord("T"), True, True, False]]
	assert value["verdicts"] == [True, True]
	assert value["emitted"] == []
	assert value["devices"] == [1]
	assert value["detached"] == 1


NO_LAYOUT_CHILD = r"""
outcomes = []
for environment in (FakeEnvironment(giveLayout=False), FakeEnvironment(layout=Layout(names=("evdev", "pc105", "no such layout at all", "", "")))):
	sock, welcome = keydClient.connect(path, 5)
	source = kernel.KernelKeySource(sock, welcome, environment=environment)
	try:
		source.start(deliver)
		outcome = "started"
	except Exception as e:
		outcome = type(e).__name__
	source.stop()
	keyboard.waitFor(lambda: keyboard._client is None)
	outcomes.append([outcome, [call[1] for call in environment.calls]])
sock, welcome = keydClient.connect(path, 5)
try:
	kernel.KernelKeySource(sock, welcome, xSource=object(), environment=FakeEnvironment())
	both = "made"
except ValueError:
	both = "ValueError"
sock.close()
result = {"outcomes": outcomes, "starts": keyboard.starts, "both": both, "opened": opened}
keyboard.close()
"""


def test_noKeyComesBeforeTheEnvironmentGivesALayout(tmp_path: Path):
	value = runEnvironmentChild(tmp_path, NO_LAYOUT_CHILD).value
	# An environment that gives no layout, or one that cannot be compiled, keeps the source from
	# starting, and the helper is never asked to capture.
	assert value["outcomes"][0] == ["RuntimeError", ["attach", "detach"]]
	assert value["outcomes"][1][0] != "started" and value["outcomes"][1][1] == ["attach", "detach"]
	assert value["starts"] == 0
	assert value["both"] == "ValueError"
	assert value["opened"] == []


#: A look the environment answers 200 ms after the press, finding the focus on the document's window
#: or not, or not finding where it is; NVDA takes 60 ms over the key it is given.
LOOK_CHILD = r"""
from logHandler import log

log.setLevel(10)
environment = FakeEnvironment(lookAfter=0.2, known=KNOWN, foundFocused=FOCUSED, state=AWAY)
source = startSource(environment)
nvdaTakes["seconds"] = 0.06
keyboard.down("h")
time.sleep(0.15)
seq = keySeqs(35)[0]
at150 = {"verdict": verdictOf(seq), "expired": expired(), "waits": [[s, latency] for s, latency in keyboard.waits]}
time.sleep(0.6)
keyboard.up("h")
time.sleep(0.2)
pressed = [e for e in keyboard.events if e[1] == "key" and e[2] == seq][0][0]
h = [e for e in delivered if e["vk"] == ord("H") and not e["up"] and not e["repeat"]]
result = {
	"at150": at150,
	"verdict": verdictOf(seq),
	"expired": expired(),
	"lookEndedAfter": environment.lookEnds[0] - pressed,
	"looks": len(environment.looks),
	"h": [[e["captured"], e["reached"], e["t"] - pressed] for e in h],
}
source.stop()
keyboard.close()
"""


def runLook(tmp_path: Path, known: bool, focused: bool, **kwargs) -> harness.ChildResult:
	return runEnvironmentChild(tmp_path, f"KNOWN = {known}\nFOCUSED = {focused}\n" + LOOK_CHILD, **kwargs)


def test_aKeyWaitsForALookAndNVDAHasItsTimeAfter(tmp_path: Path):
	value = runLook(tmp_path, known=True, focused=True).value
	assert value["looks"] == 1
	# WAIT went to the helper as the press came, and at 150 ms the press was still undecided.
	assert len(value["at150"]["waits"]) == 1 and value["at150"]["waits"][0][1] < 0.05, value["at150"]
	assert value["at150"]["verdict"] == [] and value["at150"]["expired"] == []
	# The look found the focus on the document's window: NVDA had the key once the look ended, took
	# 60 ms over it, and kept it; it was not answered for it at CAPTURE_TIMEOUT from its coming.
	assert value["lookEndedAfter"] >= 0.2
	assert len(value["h"]) == 1 and value["h"][0][0] is True and value["h"][0][2] >= 0.2, value["h"]
	[[keep, latency]] = value["verdict"]
	assert keep is True and value["lookEndedAfter"] + 0.06 <= latency < 0.5, value
	assert value["expired"] == []


def test_aLookThatFindsTheFocusElsewherePassesTheKey(tmp_path: Path):
	value = runLook(tmp_path, known=True, focused=False).value
	[[keep, latency]] = value["verdict"]
	assert keep is False and value["lookEndedAfter"] <= latency < 0.5, value
	# NVDA has it as the program's key, for its echo.
	assert [[captured, reached] for captured, reached, _t in value["h"]] == [[False, True]]
	assert value["expired"] == []


def test_aLookAnsweredUnknownPassesTheKeyAndCountsIt(tmp_path: Path):
	done = runLook(tmp_path, known=False, focused=True, recordLevel=10)
	value = done.value
	[[keep, latency]] = value["verdict"]
	assert keep is False and value["lookEndedAfter"] <= latency < 0.5, value
	assert [[captured, reached] for captured, reached, _t in value["h"]] == [[False, True]]
	counted = [r for r in done.logRecords if "was not found out" in r]
	assert counted == ["DEBUG: Where the focus is was not found out for a key, 1 so far; passed on"], counted


WAIT_AT_ARRIVAL_CHILD = r"""
environment = FakeEnvironment(lookAfter=0.2, state=AWAY)
source = startSource(environment)
keyboard.press("h")
time.sleep(0.6)
result = {"waits": [latency for _seq, latency in keyboard.waits]}
source.stop()
keyboard.close()
"""


def test_aKeyWaitingOnALookAsksTheHelperToHoldItAtOnce(tmp_path: Path):
	# With a bound of 0.3 s the share an NVDA key waits is 0.12 s, so a WAIT sent then is told apart
	# from one sent as the press came: a WAIT late on the source's clock may reach the helper too late.
	value = runEnvironmentChild(tmp_path, WAIT_AT_ARRIVAL_CHILD, standIn={"verdictTimeout": 0.3}).value
	[latency] = value["waits"]
	assert latency < 0.06, value


#: NVDA holds the key longer than its bound, so that the source answers it itself, CAPTURE_TIMEOUT
#: after the look ended; lateness of this source's own only makes the answer later.
FULL_TIME_CHILD = r"""
environment = FakeEnvironment(lookAfter=0.1, until=0.3, state=AWAY)
source = startSource(environment)
nvdaTakes["seconds"] = 0.3
keyboard.press("h")
time.sleep(0.8)
seq = keySeqs(35)[0]
pressed = [e for e in keyboard.events if e[1] == "key" and e[2] == seq][0][0]
result = {"verdict": verdictOf(seq), "expired": expired(), "lookEndedAfter": environment.lookEnds[0] - pressed}
source.stop()
keyboard.close()
"""


def test_NVDAHasAFullCaptureTimeoutFromTheLooksEnd(tmp_path: Path):
	value = runEnvironmentChild(tmp_path, FULL_TIME_CHILD).value
	[[keep, latency]] = value["verdict"]
	# CAPTURE_TIMEOUT is 0.1 s; passed by the source, as NVDA did not answer in it.
	assert keep is False and latency - value["lookEndedAfter"] >= 0.095, value
	assert latency < 0.5 and value["expired"] == [], value


#: A look whose environment says it may take five seconds: the press is answered by this source by
#: DOCUMENT_WAIT from its coming, within the helper's bound for a press it was asked to hold.
LONG_LOOK_CHILD = r"""
environment = FakeEnvironment(lookAfter=None, until=5.0, state=AWAY)
source = startSource(environment)
keyboard.press("h")
time.sleep(0.8)
seq = keySeqs(35)[0]
result = {"verdict": verdictOf(seq), "expired": expired(), "waits": len(keyboard.waits)}
source.stop()
keyboard.close()
"""


def test_aLongLookIsCutWithinTheHelpersBound(tmp_path: Path):
	value = runEnvironmentChild(tmp_path, LONG_LOOK_CHILD).value
	[[keep, latency]] = value["verdict"]
	assert keep is False and 0.45 <= latency < 0.6, value
	assert value["waits"] == 1 and value["expired"] == []


def test_theLongestLookLeavesTheHelpersBound(tmp_path: Path):
	done = harness.runChild(
		tmp_path,
		"from linuxPlatform.keys import kernel, x11\n"
		"result = [kernel.LOOK_GRACE_MOST, x11.CAPTURE_TIMEOUT, x11.DOCUMENT_WAIT]\n",
	)
	assert done.error is None, done.error
	most, captureTimeout, documentWait = done.value
	waitTimeout = re.search(
		r"VERDICT_WAIT_TIMEOUT: Duration = Duration::from_millis\((\d+)\)", PROTOCOL.read_text()
	)
	assert waitTimeout is not None
	assert most == pytest.approx(0.4)
	assert most + captureTimeout == pytest.approx(documentWait)
	assert documentWait < int(waitTimeout.group(1)) / 1000


NO_WAIT_CHILD = r"""
environment = FakeEnvironment(lookAfter=0.2, state=AWAY)
source = startSource(environment)
keyboard.press("h")
time.sleep(0.5)
seq = keySeqs(35)[0]
result = {"verdict": verdictOf(seq), "expired": expired(), "waits": len(keyboard.waits), "granted": keyboard.granted}
source.stop()
keyboard.close()
"""


def test_withoutWaitTheLookIsCutAtThePressShare(tmp_path: Path):
	value = runEnvironmentChild(tmp_path, NO_WAIT_CHILD, standIn={"features": 0}).value
	assert value["granted"] == 0
	[[keep, latency]] = value["verdict"]
	# 70 ms, the share of the helper's bound a browse mode key waits without WAIT.
	assert keep is False and 0.065 <= latency < 0.1, value
	assert value["waits"] == 0 and value["expired"] == []


#: Caps Lock passed on, in a layout where it is a lock, in one where caps:escape makes it Escape, in
#: one where caps:ctrl_modifier makes it Control with its keysym left Caps_Lock, and in one where
#: ctrl:swapcaps makes left Control the lock; then Num Lock guessed on by the layout until the helper's
#: word, and the helper's word reported.
LOCKS_CHILD = r"""
outcomes = {}
for name, options in (
	("plain", ""),
	("escape", "caps:escape"),
	("control", "caps:ctrl_modifier"),
	("swapped", "ctrl:swapcaps"),
):
	environment = FakeEnvironment(layout=Layout(names=("evdev", "pc105", "us", "", options)))
	source = startSource(environment)
	keyboard.press("Caps_Lock")
	time.sleep(0.2)
	outcomes[name] = {
		"capsLock": source.keyState(0x14) & 1,
		"notes": [call[1:] for call in environment.calls if call[1] in ("passed", "passedLock")],
		"verdicts": [keep for _seq, keep, _latency in keyboard.verdicts],
		"lockKeys": sorted(source._lockKeys.items()),
	}
	keyboard.verdicts.clear()
	source.stop()
	keyboard.waitFor(lambda: keyboard._client is None)
environment = FakeEnvironment(layout=Layout(locksGuess=keydClient.LOCK_NUM))
source = startSource(environment)
guessed = source.keyState(0x90) & 1
keyboard._sendClient(struct.pack("<BH", LOCKS, keydClient.LOCK_CAPS))
time.sleep(0.2)
told = [source.keyState(0x90) & 1, source.keyState(0x14) & 1]
source.useLayout(Layout(locksGuess=keydClient.LOCK_NUM))
time.sleep(0.2)
afterNewLayout = source.keyState(0x90) & 1
result = {
	"outcomes": outcomes,
	"guessed": guessed,
	"told": told,
	"afterNewLayout": afterNewLayout,
	"locksNoted": [call[2] for call in environment.calls if call[1] == "locks"],
}
source.stop()
keyboard.close()
"""


def test_aLockIsALockByTheLayout(tmp_path: Path):
	value = runEnvironmentChild(tmp_path, LOCKS_CHILD).value
	plain, escape = value["outcomes"]["plain"], value["outcomes"]["escape"]
	# Caps Lock is 66 as X numbers keys, and LOCK_CAPS is 2.
	assert plain["capsLock"] == 1
	assert plain["notes"] == [["passedLock", 2], ["passed", 66, False], ["passed", 66, True]]
	assert escape["capsLock"] == 0
	assert escape["notes"] == [["passed", 66, False], ["passed", 66, True]]
	assert plain["verdicts"] == escape["verdicts"] == [False]
	# Control by what it does, though its keysym is still Caps_Lock: no light flips.
	control = value["outcomes"]["control"]
	assert control["capsLock"] == 0
	assert control["notes"] == [["passed", 66, False], ["passed", 66, True]]
	# Num Lock is 77 and Scroll Lock 78; LOCK_NUM is 1 and LOCK_SCROLL 4.
	assert plain["lockKeys"] == [[66, 2], [77, 1], [78, 4]]
	assert escape["lockKeys"] == control["lockKeys"] == [[77, 1], [78, 4]]
	# Left Control, 37, is the Caps Lock key there.
	swapped = value["outcomes"]["swapped"]
	assert swapped["lockKeys"] == [[37, 2], [77, 1], [78, 4]]
	assert swapped["capsLock"] == 0
	# The layout's guess holds until the helper's word, which a later layout does not override.
	assert value["guessed"] == 1
	assert value["told"] == [0, 1]
	assert value["afterNewLayout"] == 0
	assert value["locksNoted"] == [2]


#: Alt+Tab passed on, then Super alone; then the helper's goodbye.
FOCUS_CHILD = r"""
environment = FakeEnvironment()
source = startSource(environment)
keyboard.press("Alt_L+Tab")
time.sleep(0.1)
afterAltTab = [call[1] for call in environment.calls].count("focusMayMove")
keyboard.press("Super_L")
time.sleep(0.1)
afterSuper = [call[1] for call in environment.calls].count("focusMayMove")
keyboard.press("a")
time.sleep(0.1)
afterA = [call[1] for call in environment.calls].count("focusMayMove")
lost = []
source.setCallbacks(lambda why, emergency: lost.append(source.lostReason), None)
keyboard.goodbye(4, "another client")
keyboard.waitFor(lambda: lost)
time.sleep(0.1)
result = {
	"afterAltTab": afterAltTab,
	"afterSuper": afterSuper,
	"afterA": afterA,
	"lost": lost,
	"detached": [call[1] for call in environment.calls].count("detach"),
	"passed": [call[2:] for call in environment.calls if call[1] == "passed"],
}
source.stop()
result["detachedAfterStop"] = [call[1] for call in environment.calls].count("detach")
keyboard.close()
"""


def test_chordsThatMayMoveTheFocusAreNoted(tmp_path: Path):
	value = runEnvironmentChild(tmp_path, FOCUS_CHILD).value
	# Tab under Alt, and Alt's release; Super's release; nothing for a plain letter.
	assert value["afterAltTab"] == 2
	assert value["afterSuper"] == 3
	assert value["afterA"] == 3
	# Alt, Tab, Super and a, each press and its release, as X numbers keys.
	assert value["passed"] == [
		[64, False],
		[23, False],
		[23, True],
		[64, True],
		[133, False],
		[133, True],
		[38, False],
		[38, True],
	]
	assert value["lost"] == [4]
	assert value["detached"] == 1 and value["detachedAfterStop"] == 1


#: An environment that fails as one keeping a set of keys down would, with a KeyError of the key code.
RAISING_NOTE_CHILD = r"""
class Raising(FakeEnvironment):
	def notePassed(self, keycode, isUp):
		raise KeyError(keycode)


environment = Raising()
source = startSource(environment)
keyboard.press("a")
time.sleep(0.3)
result = {"verdicts": [keep for _seq, keep, _latency in keyboard.verdicts]}
source.stop()
keyboard.close()
"""


def test_whatANoteRaisesIsLoggedWithoutTheKey(tmp_path: Path):
	done = runEnvironmentChild(tmp_path, RAISING_NOTE_CHILD)
	assert done.value["verdicts"] == [False]
	records = [e for e in done.events if e["kind"] == "log" and e["levelno"] >= 40]
	# The press and its release, each told and each failing.
	assert len(records) == 2, records
	for record in records:
		assert not record["exception"], record
		heading, *frames = record["message"].splitlines()
		assert heading == "Telling the kernel key source's environment: KeyError", record
		assert frames and all(line.startswith("  ") for line in frames), record
		# 38 is a's X key code; a frame's file and line number may hold the digits too.
		assert "38" not in re.sub(r'File "[^"]*", line \d+', "", record["message"]), record


#: An environment that cannot say where a document's keys go nor whether it can look.
RAISING_CAPTURE_CHILD = r"""
class Raising(FakeEnvironment):
	@property
	def captureState(self):
		raise RuntimeError("the feed is not ready")

	def captureNow(self, now):
		raise RuntimeError("the feed is not ready")

	@property
	def canLook(self):
		raise RuntimeError("the feed is not ready")

	@canLook.setter
	def canLook(self, value):
		pass


environment = Raising()
source = startSource(environment)
keyboard.press("Insert+t")
time.sleep(0.3)
keyboard.press("h")
time.sleep(0.3)
result = {
	"downs": [[e["vk"], e["captured"], e["nvda"], e["reached"]] for e in delivered if not e["up"]],
	"verdicts": [keep for _seq, keep, _latency in keyboard.verdicts],
	"expired": expired(),
	"emitted": keyboard.emitted(),
}
source.stop()
keyboard.close()
"""


def test_anEnvironmentThatCannotSayLeavesNVDAItsKeys(tmp_path: Path):
	done = runEnvironmentChild(tmp_path, RAISING_CAPTURE_CHILD)
	value = done.value
	# Insert and T are NVDA's and kept; h is the program's, passed on with no look.
	assert value["downs"] == [
		[0x2D, True, True, False],
		[ord("T"), True, True, False],
		[ord("H"), False, False, True],
	]
	assert value["verdicts"] == [True, True, False]
	# Only h reached the program, Insert and T being kept: 35 is h as the kernel numbers keys.
	assert value["expired"] == [] and value["emitted"] == [[35, True], [35, False]]
	headings = {record.split("\n")[0] for record in done.logErrors}
	assert headings == {
		"ERROR: Asking the kernel key source's environment where a document's keys go: RuntimeError",
		"ERROR: Asking the kernel key source's environment whether it can look at the focus: RuntimeError",
	}, done.logErrors


#: h held on an open look and decided by NVDA as kept, its verdict not yet sent, as the helper lets go
#: of the keyboard, which passes it on; then h released.
RELEASE_ALL_CHILD = r"""
from keydStandIn import RELEASED, RELEASED_BACKGROUND

environment = FakeEnvironment(lookAfter=None, until=5.0, state=AWAY)
source = startSource(environment)
keyboard.down("h")
time.sleep(0.05)
pending = list(source._pending)
pending[0].decide(False)
keyboard._sendClient(struct.pack("<BB", RELEASED, RELEASED_BACKGROUND))
time.sleep(0.8)
keyboard.up("h")
time.sleep(0.2)
result = {
	"pending": len(pending),
	"verdicts": len(keyboard.verdicts),
	"passed": [call[2:] for call in environment.calls if call[1] == "passed"],
}
source.stop()
keyboard.close()
"""


def test_aKeyTheHelperPassesOnAsItLetsGoIsToldOfDownAndUp(tmp_path: Path):
	value = runEnvironmentChild(tmp_path, RELEASE_ALL_CHILD).value
	# Its keep was never sent, so the helper passed it on: 43 is h as X numbers keys.
	assert value["pending"] == 1 and value["verdicts"] == 0, value
	assert value["passed"] == [[43, False], [43, True]], value


REPEAT_CHILD = r"""
environment = FakeEnvironment(layout=Layout(repeat=(True, 0.2, 0.05)))
source = startSource(environment)
settings = source._repeatSettings
keyboard.down("a")
time.sleep(0.5)
keyboard.up("a")
time.sleep(0.1)
result = {
	"delay": settings.delay,
	"interval": settings.interval,
	"aRepeats": settings.repeats(38),
	"shiftRepeats": settings.repeats(50),
	"repeats": len([e for e in delivered if e["vk"] == ord("A") and e["repeat"]]),
}
source.stop()
keyboard.close()
"""


def test_theRepeatIsTheLayouts(tmp_path: Path):
	value = runEnvironmentChild(tmp_path, REPEAT_CHILD).value
	assert value["delay"] == 0.2 and value["interval"] == 0.05
	assert value["aRepeats"] and not value["shiftRepeats"]
	# Half a second held: the first after 0.2 s, then every 0.05 s.
	assert 4 <= value["repeats"] <= 7, value
