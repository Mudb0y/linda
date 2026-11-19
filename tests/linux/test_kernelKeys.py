"""Keys from the kernel through linda-keyd, with keydStandIn standing in for the helper, since the
rig has no input devices: the kernel key source does for NVDA and the user what the X source does, and
keeps the client's own duties to the helper. Run under the rig:

	nix develop . -c tests/linux/rig python3 -m pytest tests/linux/test_kernelKeys.py
"""

import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import desktopSession
import harness
import pytest
import test_browseKeys as browseKeys
import webSession
from desktopSession import PREFIX_MARK, DesktopSession
from keydStandIn import StandIn
from test_keys import ENTRY_TEXT, LAYOUTS, STARTUP_TIMEOUT, configText, entryState, transcript, waitForEntry

#: Seconds within which the helper must have a verdict, which it passes a press on after.
VERDICT_TIMEOUT = 0.1
#: What NVDA says after the emergency chord.
LET_GO_MESSAGE = (
	"NVDA has let go of the keyboard, and now reads keys from the X display. "
	"Use the command to take the keyboard back when you want it to take them again"
)

#: The kernel key source in a child, behind the route NVDA uses, and a window of the child's own that
#: records each key the X display gives it. Its NVDA keeps every captured press, unless keep says
#: otherwise, and records each event as it comes. The stand-in is the child's own unless SOCKET names
#: one the test runs.
KERNEL_CHILD = r"""
import os, sys, tempfile, threading, time
from pathlib import Path
sys.path.insert(0, str(harness.HERE))
from Xlib import X, XK, Xatom, display
from keydStandIn import StandIn, code
from linuxPlatform.keys import route, x11

if SOCKET is None:
	socketDir = tempfile.mkdtemp(prefix="keyd", dir="/tmp")
	path = socketDir + "/socket"
	keyboard = StandIn(path, **STAND_IN).start()
else:
	path = SOCKET
	keyboard = None
delivered = []
said = []
keep = lambda event: True


def deliver(event):
	delivered.append(
		{
			"vk": event.vkCode,
			"up": event.isUp,
			"repeat": event.repeat,
			"reached": event.reachedApplication,
			"captured": event.captured,
			"nvda": event.nvdaKeyGrab,
			"injected": event.injected,
			"text": event.text,
			"t": time.monotonic(),
		}
	)
	if event.isUp or not event.captured:
		return True
	return not keep(event)


mode = {"now": "off"}
app = display.Display()
root = app.screen().root
pidAtom = app.intern_atom("_NET_WM_PID")


def makeWindow():
	window = root.create_window(
		0, 0, 200, 200, 0, app.screen().root_depth, event_mask=X.KeyPressMask | X.KeyReleaseMask
	)
	window.change_property(pidAtom, Xatom.CARDINAL, 32, [1])
	window.map()
	app.sync()
	time.sleep(0.3)
	window.set_input_focus(X.RevertToParent, X.CurrentTime)
	app.sync()
	time.sleep(0.2)
	return window


def windowKeys():
	keys = []
	while app.pending_events():
		event = app.next_event()
		if event.type in (X.KeyPress, X.KeyRelease):
			keys.append([event.detail - 8, event.type == X.KeyPress])
	return keys


def presses(vk=None):
	return [e for e in delivered if not e["up"] and not e["repeat"] and (vk is None or e["vk"] == vk)]


def startSource(routeName="auto", consumed=x11.EXTENDED_INSERT | x11.CAPS_LOCK):
	source = route.RoutedKeySource(routeName, path, said.append)
	source.start(deliver, lambda: mode["now"])
	source.setConsumed(consumed, True)
	if keyboard is not None and source.isKernel:
		keyboard.waitFor(lambda: keyboard.capturing)
	time.sleep(0.3)
	return source


def signal(name):
	(Path(FLAGS) / name).touch()


def awaitSignal(name, timeout=60):
	deadline = time.monotonic() + timeout
	while not (Path(FLAGS) / name).exists():
		assert time.monotonic() < deadline, f"no {name} from the test"
		time.sleep(0.01)


window = makeWindow()
windowKeys()
"""


def runKernelChild(tmp_path: Path, code: str, standIn: dict | None = None, timeout: float = 90) -> dict:
	"""Run code after KERNEL_CHILD, with the stand-in in the child."""
	with DesktopSession(tmp_path / "desktop", windowManager=False):
		done = harness.runChild(
			tmp_path,
			f"STAND_IN = {standIn or {}!r}\nSOCKET = None\nFLAGS = None\n" + KERNEL_CHILD + code,
			timeout=timeout,
		)
	assert done.error is None, f"{done.error}\n{done.stderr[-6000:]}"
	return done.value


def runBesideStandIn(tmp_path: Path, code: str, drive, standIn: dict | None = None) -> dict:
	"""Run code after KERNEL_CHILD with the stand-in in this process, so that whatever loads the child
	holds up only the client, as the helper is a process of its own. drive(standIn, signal, awaitSignal)
	presses the keys; the child and it wait for each other through flag files."""
	socketDir = Path(tempfile.mkdtemp(prefix="keyd", dir="/tmp"))
	flags = tmp_path / "flags"
	flags.mkdir()

	def signal(name: str) -> None:
		(flags / name).touch()

	def awaitSignal(name: str, timeout: float = 60) -> None:
		deadline = time.monotonic() + timeout
		while not (flags / name).exists():
			assert time.monotonic() < deadline, f"no {name} from the child"
			assert child.is_alive(), "the child ended"
			time.sleep(0.01)

	outcome = {}
	try:
		with (
			DesktopSession(tmp_path / "desktop", windowManager=False),
			StandIn(socketDir / "socket", **(standIn or {})) as keyboard,
		):
			prelude = f"STAND_IN = None\nSOCKET = {str(socketDir / 'socket')!r}\nFLAGS = {str(flags)!r}\n"
			child = threading.Thread(
				target=lambda: outcome.update(
					done=harness.runChild(tmp_path, prelude + KERNEL_CHILD + code, timeout=120)
				)
			)
			child.start()
			try:
				drive(keyboard, signal, awaitSignal)
			finally:
				signal("done")
				child.join(180)
	finally:
		shutil.rmtree(socketDir, ignore_errors=True)
	done = outcome["done"]
	assert done.error is None, f"{done.error}\n{done.stderr[-6000:]}"
	return done.value


CHORD_CHILD = r"""
source = startSource()
kernel = source.isKernel
keyboard.press("Insert+t")
keyboard.press("a")
time.sleep(0.3)
result = {"kernel": kernel, "delivered": delivered, "window": windowKeys(), "verdicts": keyboard.verdicts}
source.stop()
keyboard.close()
"""


def test_nvdaChordIsNVDAsAndTypingReachesTheApplication(tmp_path: Path):
	value = runKernelChild(tmp_path, CHORD_CHILD)
	assert value["kernel"]
	downs = [(e["vk"], e["captured"], e["nvda"], e["reached"]) for e in value["delivered"] if not e["up"]]
	# Insert and t taken for NVDA, which keeps them; a reaching the window, and NVDA after it.
	assert downs == [(0x2D, True, True, False), (ord("T"), True, True, False), (ord("A"), False, False, True)]
	assert value["window"] == [[30, True], [30, False]]
	assert all(latency < VERDICT_TIMEOUT for _seq, _keep, latency in value["verdicts"])


#: Ready, the child waits while the test presses a key NVDA does not take and an NVDA chord NVDA
#: answers at once; then it makes NVDA take 300 ms over every key, with a thread of its own kept busy
#: in Python all the while, as NVDA's main thread can be, as the test presses browse mode's h and the
#: chord.
LOAD_CHILD = r"""
stop = threading.Event()


def spin():
	n = 0
	while not stop.is_set():
		n += 1


def slowly(event):
	time.sleep(0.3)
	return True


source = startSource()
source.setCapture("browse")
mode["now"] = "browse"
# When each press came to the client, and when its verdict left, as the client has them.
import struct
active = source._active
received, answered = {}, {}
onKey, send = active._onKey, active._send


def timedKey(key, now):
	if key.value == 1:
		received[key.seq] = time.monotonic()
	return onKey(key, now)


def timedSend(message):
	if message[0] == 0x04:
		answered[struct.unpack("<BQB", message)[1]] = time.monotonic()
	return send(message)


active._onKey, active._send = timedKey, timedSend
signal("ready")
awaitSignal("load")
received.clear()
answered.clear()
spinners = [threading.Thread(target=spin, daemon=True)]
for spinner in spinners:
	spinner.start()
keep = slowly
signal("loaded")
awaitSignal("done", timeout=90)
stop.set()
result = {
	"kernel": source.isKernel,
	"window": windowKeys(),
	"client": [answered[seq] - received[seq] for seq in sorted(received) if seq in answered],
}
source.stop()
"""


def test_everyPressIsAnsweredInTimeUnderLoad(tmp_path: Path):
	measured = {}

	def drive(keyboard: StandIn, signal, awaitSignal) -> None:
		awaitSignal("ready")
		for _ in range(20):
			keyboard.press("F5", hold=0.02, gap=0.01)
		measured["plain"] = [latency for _s, _k, latency in keyboard.verdicts]
		keyboard.verdicts.clear()
		for _ in range(10):
			keyboard.press("Insert+t", hold=0.02, gap=0.01)
		measured["chord"] = [latency for _s, _k, latency in keyboard.verdicts]
		keyboard.verdicts.clear()
		signal("load")
		awaitSignal("loaded")
		for _ in range(10):
			keyboard.press("h", hold=0.02, gap=0.01)
			keyboard.press("Insert+t", hold=0.02, gap=0.01)
		time.sleep(10)
		codes = {e[2]: e[3] for e in keyboard.events if e[1] == "key" and e[4] == 1}
		measured["loaded"] = [(codes[seq], kept, latency) for seq, kept, latency in sorted(keyboard.verdicts)]
		measured["expired"] = [codes[e[2]] for e in keyboard.events if e[1] == "expired"]
		measured["dropped"] = [
			e[1:] for e in keyboard.events if e[1] in ("goodbye", "client closed", "let go")
		]

	value = runBesideStandIn(tmp_path, LOAD_CHILD, drive)
	assert value["kernel"], f"the helper gave NVDA up: {measured}"
	assert not measured["dropped"]
	for name in ("plain", "chord"):
		latencies = sorted(measured[name])
		print(
			f"{name}: median {latencies[len(latencies) // 2] * 1000:.2f} ms, most {latencies[-1] * 1000:.2f} ms"
		)
		assert latencies[-1] < VERDICT_TIMEOUT / 2
	loaded = measured["loaded"]
	chord = sorted(latency for evdev, _kept, latency in loaded if evdev != 35)
	client = sorted(value["client"])
	print(
		f"under load, the chord's keys kept without NVDA in a median {chord[len(chord) // 2] * 1000:.1f} ms "
		f"and at most {chord[-1] * 1000:.1f} ms; in the client, median {client[len(client) // 2] * 1000:.1f} ms, "
		f"most {client[-1] * 1000:.1f} ms; passed on by the helper: {measured['expired']}"
	)
	# The NVDA chord kept, as on the X route when NVDA is slow. The machine the tests run on is shared, and
	# a press its scheduler held up for half the bound reaches the helper late; one in twenty is allowed.
	assert [evdev for evdev, kept, _latency in loaded if not kept and evdev != 35] == []
	assert len([evdev for evdev in measured["expired"] if evdev != 35]) <= 1
	assert len(chord) >= 19
	# Browse mode's h passed on, by this source or by the helper, which is the same to the page.
	assert all(not kept for evdev, kept, _latency in loaded if evdev == 35)
	assert value["window"].count([35, True]) == 10


HEARTBEAT_CHILD = r"""
source = startSource()
time.sleep(2)
idle = len(keyboard.heartbeats)
keyboard.heartbeats.clear()
blocked = threading.Event()


def stuck(event):
	blocked.set()
	time.sleep(2.5)
	return True


keep = stuck
keyboard.press("Insert+t")
blocked.wait(2)
time.sleep(2.6)
busy = len(keyboard.heartbeats)
kernelThen = source.isKernel
# Now the thread that answers keys hangs, while the decider is free.
active = source._active
answer = active._answer
hung = {"once": True}


def hang(now):
	if hung.pop("once", False):
		time.sleep(3)
	return answer(now)


active._answer = hang
active._wake()
time.sleep(4.5)
result = {
	"idle": idle,
	"busy": busy,
	"kernelThen": kernelThen,
	"kernelAfter": source.isKernel,
	"goodbyes": [e[2] for e in keyboard.events if e[1] == "goodbye"],
}
source.stop()
keyboard.close()
"""


def test_heartbeatComesFromTheThreadThatAnswersKeys(tmp_path: Path):
	value = runKernelChild(tmp_path, HEARTBEAT_CHILD)
	# At least one every half second, whether NVDA is idle or stuck over a key.
	assert value["idle"] >= 4, value
	assert value["busy"] >= 4, value
	assert value["kernelThen"]
	# With that thread hung, the helper hears nothing, lets go, and NVDA reads keys from X.
	assert value["goodbyes"] == [6]
	assert not value["kernelAfter"]


BUSY_CHILD = r"""
import socket, struct
other = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
other.connect(path)
other.send(struct.pack("<BHI", 1, 1, 0))
other.recv(256)
source = startSource()
first = source.isKernel
keyboard.press("t")
time.sleep(0.3)
throughX = [e for e in presses() if e["vk"] == ord("T")]
other.close()
deadline = time.monotonic() + 6
while not (source.isKernel and keyboard.capturing) and time.monotonic() < deadline:
	time.sleep(0.05)
result = {
	"first": first,
	"throughX": len(throughX),
	"after": source.isKernel,
	"refused": [e[2] for e in keyboard.events if e[1] == "refused"],
	"starts": keyboard.starts,
}
source.stop()
keyboard.close()
"""


def test_busyHelperIsAskedAgain(tmp_path: Path):
	value = runKernelChild(tmp_path, BUSY_CHILD)
	assert value == {"first": False, "throughX": 1, "after": True, "refused": [4], "starts": 1}


GOODBYE_CHILD = r"""
source = startSource()
first = source.isKernel
# Another client of the session in front took this one's place.
keyboard.goodbye(9, "replaced")
deadline = time.monotonic() + 5
while (source.isKernel or source._active is None) and time.monotonic() < deadline:
	time.sleep(0.05)
afterGoodbye = source.isKernel
keyboard.press("Insert+t")
time.sleep(0.4)
throughX = [[e["vk"], e["nvda"]] for e in presses()]
deadline = time.monotonic() + 6
while not (source.isKernel and keyboard.capturing) and time.monotonic() < deadline:
	time.sleep(0.05)
result = {"first": first, "afterGoodbye": afterGoodbye, "throughX": throughX, "back": source.isKernel}
source.stop()
keyboard.close()
"""


def test_goodbyeFallsBackToXAndTheHelperIsAskedAgain(tmp_path: Path):
	value = runKernelChild(tmp_path, GOODBYE_CHILD)
	assert value == {
		"first": True,
		"afterGoodbye": False,
		# Read from the X display, Insert is still the NVDA key.
		"throughX": [[0x2D, True], [ord("T"), True]],
		"back": True,
	}


NUMPAD_INSERT_CHILD = r"""
source = startSource(consumed=x11.NUMPAD_INSERT)
result = {}
for modifier in ("Control_L", "Alt_L", "Super_L"):
	windowKeys()
	keyboard.down(modifier)
	keyboard.down("KP_Insert")
	keyboard.press("f")
	keyboard.up("KP_Insert")
	keyboard.up(modifier)
	time.sleep(0.3)
	result[modifier] = [key for key in windowKeys() if key[1]]
delivered.clear()
keyboard.press("Num_Lock")
time.sleep(0.2)
windowKeys()
keyboard.press("KP_Insert")
time.sleep(0.3)
result["numLock"] = {"window": windowKeys(), "nvda": [e["nvda"] for e in presses(0x60)]}
keyboard.press("Num_Lock")
source.stop()
keyboard.close()
"""


def test_numpadInsertAfterAModifierIsNVDAs(tmp_path: Path):
	value = runKernelChild(tmp_path, NUMPAD_INSERT_CHILD)
	# Only the modifier reaches the window, neither numpad Insert nor f.
	assert value["Control_L"] == [[29, True]]
	assert value["Alt_L"] == [[56, True]]
	assert value["Super_L"] == [[125, True]]
	# With Num Lock on the key is 0, the application's.
	assert value["numLock"] == {"window": [[82, True], [82, False]], "nvda": [False]}


VANISH_CHILD = r"""
source = startSource()
kernelBefore = source.isKernel
keyboard.down("Insert")
time.sleep(0.2)
keyboard.vanish()
deadline = time.monotonic() + 5
while (source.isKernel or source._active is None) and time.monotonic() < deadline:
	time.sleep(0.05)
time.sleep(0.5)
insert = [[e["up"], e["reached"]] for e in delivered if e["vk"] == 0x2D]
keyboard.up("Insert")
time.sleep(0.2)
windowKeys()
mark = len(delivered)
keyboard.press("t")
time.sleep(0.5)
afterVanish = [[e["vk"], e["up"], e["nvda"]] for e in delivered[mark:]]
typed = windowKeys()
keyboard.comeBack()
deadline = time.monotonic() + 6
while not (source.isKernel and keyboard.capturing) and time.monotonic() < deadline:
	time.sleep(0.05)
mark = len(delivered)
keyboard.press("Insert+t")
time.sleep(0.4)
back = [[e["vk"], e["nvda"]] for e in delivered[mark:] if not e["up"]]
result = {
	"kernelBefore": kernelBefore,
	"insert": insert,
	"afterVanish": afterVanish,
	"typed": typed,
	"back": source.isKernel,
	"chordBack": back,
	"window": windowKeys(),
}
source.stop()
keyboard.close()
"""


def test_helperVanishingInAChordLeavesNoKeyStuck(tmp_path: Path):
	value = runKernelChild(tmp_path, VANISH_CHILD)
	assert value["kernelBefore"]
	# Insert, held as the helper went, is released to NVDA: NVDA's own, never the application's.
	assert value["insert"] == [[False, False], [True, False]]
	# The keyboard reaches X directly now, and t is a plain t, not NVDA+t.
	assert value["afterVanish"] == [[ord("T"), False, False], [ord("T"), True, False]]
	assert value["typed"] == [[20, True], [20, False]]
	assert value["back"]
	assert value["chordBack"] == [[0x2D, True], [ord("T"), True]]
	assert value["window"] == []


EMERGENCY_CHILD = r"""
source = startSource()
keyboard.down("BackSpace")
keyboard.down("Escape")
keyboard.down("Return")
time.sleep(0.5)
for name in ("Return", "Escape", "BackSpace"):
	keyboard.up(name)
deadline = time.monotonic() + 5
while (source.isKernel or source._active is None) and time.monotonic() < deadline:
	time.sleep(0.05)
time.sleep(3)
afterChord = {"kernel": source.isKernel, "starts": keyboard.starts, "said": list(said)}
heldThen = [[e["vk"], e["up"]] for e in delivered]
windowKeys()
keyboard.press("t")
time.sleep(0.4)
typed = windowKeys()
source.takeBack()
deadline = time.monotonic() + 5
while not (source.isKernel and keyboard.capturing) and time.monotonic() < deadline:
	time.sleep(0.05)
time.sleep(0.3)
result = {
	"afterChord": afterChord,
	"held": heldThen,
	"typed": typed,
	"back": source.isKernel,
	"starts": keyboard.starts,
	"said": said,
}
source.stop()
keyboard.close()
"""


def test_emergencyChordLetsGoUntilTheUserTakesTheKeyboardBack(tmp_path: Path):
	value = runKernelChild(tmp_path, EMERGENCY_CHILD)
	after = value["afterChord"]
	assert not after["kernel"]
	# Not taken back by itself, though the helper would admit NVDA.
	assert after["starts"] == 1
	assert after["said"] == [LET_GO_MESSAGE]
	# Backspace and Escape went to NVDA and were released to it; Enter, which made the chord, never came.
	assert value["held"] == [[0x08, False], [0x1B, False], [0x1B, True], [0x08, True]]
	assert value["typed"] == [[20, True], [20, False]]
	assert value["back"]
	assert value["starts"] == 2
	assert value["said"][-1] == "NVDA has the keyboard again"


GONE_CHILD = r"""
source = startSource()
keyboard.down("Insert")
keyboard.down("Shift_L")
time.sleep(0.2)
keyboard.keyboardGone()
time.sleep(0.3)
result = {
	"events": [[e["vk"], e["up"]] for e in delivered],
	"kernel": source.isKernel,
	"shift": source.keyState(0x10),
}
source.stop()
keyboard.close()
"""


def test_keyboardVanishingInAChordReleasesItsKeys(tmp_path: Path):
	value = runKernelChild(tmp_path, GONE_CHILD)
	assert value == {
		"events": [[0x2D, False], [0xA0, False], [0xA0, True], [0x2D, True]],
		"kernel": True,
		"shift": 0,
	}


LOCKS_CHILD = r"""
import desktopSession
last = {"t": 0.0}


def twice(event):
	# As keyboardHandler does: the NVDA key pressed twice quickly is passed on.
	now = time.monotonic()
	passed = event.vkCode == 0x14 and now - last["t"] < 0.5
	last["t"] = 0.0 if passed else (now if event.vkCode == 0x14 else 0.0)
	return not passed


keep = twice
source = startSource()
keyboard.press("Caps_Lock")
time.sleep(0.3)
once = {"lit": desktopSession.capsLockIsOn(), "state": source.keyState(0x14)}
time.sleep(0.6)
keyboard.press("Caps_Lock")
time.sleep(0.15)
keyboard.press("Caps_Lock")
time.sleep(0.4)
twiceState = {"lit": desktopSession.capsLockIsOn(), "state": source.keyState(0x14)}
time.sleep(0.6)
keyboard.press("Caps_Lock")
time.sleep(0.15)
keyboard.press("Caps_Lock")
time.sleep(0.4)
off = {"lit": desktopSession.capsLockIsOn(), "state": source.keyState(0x14)}
keyboard.press("KP_Up")
time.sleep(0.2)
numLockOff = [[e["vk"], e["nvda"]] for e in presses()[-1:]]
keyboard.press("Num_Lock")
time.sleep(0.3)
numState = source.keyState(0x90)
keyboard.press("KP_Up")
time.sleep(0.2)
numLockOn = [[e["vk"], e["nvda"]] for e in presses()[-1:]]
keyboard.press("Num_Lock")
time.sleep(0.3)
result = {
	"once": once,
	"twice": twiceState,
	"off": off,
	"numLockOff": numLockOff,
	"numState": numState,
	"numLockOn": numLockOn,
	"numAfter": source.keyState(0x90),
}
source.stop()
keyboard.close()
"""


def test_capsLockKeptDoesNotToggleAndLocksFollowTheLights(tmp_path: Path):
	value = runKernelChild(tmp_path, LOCKS_CHILD)
	assert value["once"] == {"lit": False, "state": 0}
	assert value["twice"] == {"lit": True, "state": 1}
	assert value["off"] == {"lit": False, "state": 0}
	# With Num Lock off, numpad 8 is the review cursor's key, NVDA's; with it on, the digit 8.
	assert value["numLockOff"] == [[0x26, True]]
	assert value["numState"] & 1
	assert value["numLockOn"] == [[0x68, False]]
	assert not value["numAfter"] & 1


LAYOUT_CHILD = r"""
import subprocess
source = startSource()
subprocess.run(["setxkbmap", "pl"], check=True)
time.sleep(0.5)
keyboard.press("ISO_Level3_Shift+a")
keyboard.press("shift+ISO_Level3_Shift+l")
time.sleep(0.3)
polish = [[e["text"], e["vk"], e["captured"]] for e in presses() if e["text"]]
delivered.clear()
subprocess.run(["setxkbmap", "-layout", "us,pl"], check=True)
time.sleep(0.5)
keyboard.press("ISO_Level3_Shift+l")
first = [e["text"] for e in presses() if e["text"]]
delivered.clear()
# The second group locked as a panel would, with no key.
info = app.query_extension("XKEYBOARD")
x11._XkbUseExtension(display=app.display, opcode=info.major_opcode, wantedMajor=1, wantedMinor=0)
x11._XkbLatchLockState(
	display=app.display,
	opcode=info.major_opcode,
	deviceSpec=x11.XKB_CORE_KEYBOARD,
	affectModLocks=0,
	modLocks=0,
	lockGroup=True,
	groupLock=1,
	affectModLatches=0,
	modLatches=0,
	latchGroup=False,
	groupLatch=0,
)
app.sync()
time.sleep(0.3)
keyboard.press("ISO_Level3_Shift+l")
second = [e["text"] for e in presses() if e["text"]]
subprocess.run(["setxkbmap", "us"], check=True)
result = {"polish": polish, "first": first, "second": second}
source.stop()
keyboard.close()
"""


def test_layoutAndGroupFollowTheXServer(tmp_path: Path):
	value = runKernelChild(tmp_path, LAYOUT_CHILD)
	# AltGr letters are typing, with no Alt for NVDA to take them for commands.
	assert value["polish"] == [["ą", ord("A"), False], ["Ł", ord("L"), False]]
	assert value["first"] == ["l"]
	assert value["second"] == ["ł"]


REPEAT_CHILD = r"""
source = startSource()
keyboard.down("Insert")
for _ in range(3):
	keyboard.repeat("Insert")
	time.sleep(0.03)
keyboard.up("Insert")
keyboard.down("a")
for _ in range(2):
	keyboard.repeat("a")
	time.sleep(0.03)
keyboard.up("a")
time.sleep(0.3)
result = {
	"events": [[e["vk"], e["up"], e["repeat"], e["reached"], e["nvda"]] for e in delivered],
	"window": windowKeys(),
}
source.stop()
keyboard.close()
"""


def test_repeatsComeToNVDAAlone(tmp_path: Path):
	value = runKernelChild(tmp_path, REPEAT_CHILD)
	assert value["events"] == [
		[0x2D, False, False, False, True],
		*[[0x2D, False, True, False, True]] * 3,
		[0x2D, True, False, False, False],
		[ord("A"), False, False, True, False],
		*[[ord("A"), False, True, True, False]] * 2,
		[ord("A"), True, False, True, False],
	]
	# The X display repeats a key by itself; the helper puts out only the press and the release.
	assert value["window"] == [[30, True], [30, False]]


BROWSE_CHILD = r"""
source = startSource()
source.setCapture("browse", None)
mode["now"] = "browse"
keyboard.press("h")
keyboard.press("shift+h")
keyboard.press("Down")
keyboard.press("F5")
time.sleep(0.3)
browse = {"events": [[e["vk"], e["captured"], e["reached"]] for e in presses()], "window": windowKeys()}
delivered.clear()
mode["now"] = "focus"
source.setCapture("focus", None)
keyboard.press("h")
keyboard.press("Escape")
time.sleep(0.3)
focus = {"events": [[e["vk"], e["captured"], e["reached"]] for e in presses()], "window": windowKeys()}
delivered.clear()
mode["now"] = "browse"
source.setCapture("browse", 4242)
keyboard.press("h")
time.sleep(0.3)
otherApp = {"events": [[e["vk"], e["captured"], e["reached"]] for e in presses()], "window": windowKeys()}
delivered.clear()
source.setCapture("off")
mode["now"] = "off"
keyboard.press("h")
time.sleep(0.3)
off = {"events": [[e["vk"], e["captured"], e["reached"]] for e in presses()], "window": windowKeys()}
result = {"browse": browse, "focus": focus, "otherApp": otherApp, "off": off}
source.stop()
keyboard.close()
"""


def test_browseModeKeysFollowTheModeNow(tmp_path: Path):
	value = runKernelChild(tmp_path, BROWSE_CHILD)
	h, down, shift, f5 = ord("H"), 0x28, 0xA0, 0x74
	assert value["browse"] == {
		"events": [
			[h, True, False],
			[shift, False, True],
			[h, True, False],
			[down, True, False],
			[f5, False, True],
		],
		"window": [[42, True], [42, False], [63, True], [63, False]],
	}
	# In focus mode h reaches the page at once; Escape, which leaves focus mode, is NVDA's.
	assert value["focus"] == {
		"events": [[h, False, True], [0x1B, True, False]],
		"window": [[35, True], [35, False]],
	}
	# The focus is in a window of another application than the document's.
	assert value["otherApp"] == {"events": [[h, False, True]], "window": [[35, True], [35, False]]}
	assert value["off"] == {"events": [[h, False, True]], "window": [[35, True], [35, False]]}


HOLD_CHILD = r"""
source = startSource()
source.holdKeyboard(True)
time.sleep(0.1)
keyboard.press("b")
keyboard.press("Insert")
time.sleep(0.3)
held = {"events": [[e["vk"], e["up"], e["captured"], e["reached"]] for e in delivered], "window": windowKeys()}
delivered.clear()
source.holdKeyboard(False)
keyboard.press("b")
time.sleep(0.3)
free = {"events": [[e["vk"], e["up"], e["reached"]] for e in delivered], "window": windowKeys()}
result = {"held": held, "free": free}
source.stop()
keyboard.close()
"""


def test_inputHelpHoldsEveryKey(tmp_path: Path):
	value = runKernelChild(tmp_path, HOLD_CHILD)
	b = ord("B")
	assert value["held"] == {
		"events": [
			[b, False, False, False],
			[b, True, False, False],
			[0x2D, False, False, False],
			[0x2D, True, False, False],
		],
		"window": [],
	}
	assert value["free"] == {
		"events": [[b, False, True], [b, True, True]],
		"window": [[48, True], [48, False]],
	}


TYPED_CHILD = r"""
source = startSource()
source.typeKey(ord("B"), False, False)
source.typeKey(ord("B"), False, True)
time.sleep(0.3)
plain = {"events": [[e["vk"], e["up"], e["injected"], e["reached"]] for e in delivered], "window": windowKeys()}
delivered.clear()
source.typeKey(0x2D, True, False)
source.typeKey(ord("T"), False, False)
source.typeKey(ord("T"), False, True)
source.typeKey(0x2D, True, True)
time.sleep(0.3)
chord = {"events": [[e["vk"], e["up"], e["nvda"]] for e in delivered], "window": windowKeys()}
source.typeKey(ord("C"), False, False)
time.sleep(0.2)
source.releaseTypedKeys()
time.sleep(0.3)
result = {"plain": plain, "chord": chord, "released": windowKeys()}
source.stop()
keyboard.close()
"""


def test_typedKeysComeToNVDAAndThenTheApplication(tmp_path: Path):
	value = runKernelChild(tmp_path, TYPED_CHILD)
	b = ord("B")
	assert value["plain"] == {
		"events": [[b, False, True, True], [b, True, True, True]],
		"window": [[48, True], [48, False]],
	}
	assert value["chord"] == {
		"events": [
			[0x2D, False, True],
			[ord("T"), False, True],
			[ord("T"), True, False],
			[0x2D, True, False],
		],
		"window": [],
	}
	assert value["released"] == [[46, True], [46, False]]


ROUTE_CHILD = r"""
source = startSource("x11")
x11Only = {"kernel": source.isKernel, "admitted": keyboard.admitted.is_set()}
source.setRoute("auto")
deadline = time.monotonic() + 5
while not (source.isKernel and keyboard.capturing) and time.monotonic() < deadline:
	time.sleep(0.05)
auto = source.isKernel
keyboard.down("Insert")
time.sleep(0.2)
source.setRoute("x11")
deadline = time.monotonic() + 5
while (source.isKernel or source._active is None) and time.monotonic() < deadline:
	time.sleep(0.05)
time.sleep(0.3)
keyboard.up("Insert")
back = {
	"kernel": source.isKernel,
	"gone": keyboard.gone.is_set(),
	"insert": [e["up"] for e in delivered if e["vk"] == 0x2D],
	"all": [[e["vk"], e["up"], e["repeat"], e["t"]] for e in delivered],
}
source.stop()
keyboard.refusal = (3, "not allowed")
said.clear()
forced = startSource("kernel")
time.sleep(0.5)
delivered.clear()
windowKeys()
keyboard.press("t")
time.sleep(0.3)
strict = {"kernel": forced.isKernel, "said": said, "delivered": len(delivered), "window": windowKeys()}
forced.stop()
result = {"x11Only": x11Only, "auto": auto, "back": back, "strict": strict}
keyboard.close()
"""


def test_routeSettingForcesEither(tmp_path: Path):
	value = runKernelChild(tmp_path, ROUTE_CHILD)
	assert value["x11Only"] == {"kernel": False, "admitted": False}
	assert value["auto"]
	assert {k: v for k, v in value["back"].items() if k != "all"} == {
		"kernel": False,
		"gone": True,
		"insert": [False, True],
	}, value["back"]
	# Set to the kernel alone, and turned away: no keys read at all, which NVDA says.
	assert value["strict"] == {
		"kernel": False,
		"said": ["NVDA is set to take keys from the kernel alone, and cannot; it reads no keys"],
		"delivered": 0,
		"window": [[20, True], [20, False]],
	}


EXPIRY_CHILD = r"""
def slowly(event):
	time.sleep(0.05)
	return False


keep = slowly
source = startSource()
keyboard.press("Insert+t")
time.sleep(0.5)
once = {
	"events": [[e["vk"], e["up"], e["reached"]] for e in delivered],
	"window": windowKeys(),
	"expired": len([e for e in keyboard.events if e[1] == "expired"]),
}
for _ in range(15):
	keyboard.press("Insert", hold=0.02, gap=0.01)
	time.sleep(0.05)
deadline = time.monotonic() + 5
while (source.isKernel or source._active is None) and time.monotonic() < deadline:
	time.sleep(0.05)
result = {"once": once, "goodbyes": [e[2] for e in keyboard.events if e[1] == "goodbye"], "kernel": source.isKernel}
source.stop()
keyboard.close()
"""


def test_pressesTheHelperPassedOnAreNotNVDAsToo(tmp_path: Path):
	# A helper that keeps a shorter bound than it says, as a client late by more than its margin finds:
	# each press is passed on before the verdict comes.
	value = runKernelChild(
		tmp_path, EXPIRY_CHILD, standIn={"verdictTimeout": 0.005, "announcedVerdictTimeout": 0.1}
	)
	once = value["once"]
	assert once["expired"] == 1
	assert once["window"] == [[110, True], [20, True], [20, False], [110, False]]
	# NVDA heard of Insert as it was read, and was told the application had it; t, no chord now that
	# Insert is the application's, went to the application at once.
	assert [[vk, up] for vk, up, _reached in once["events"]] == [
		[0x2D, False],
		[ord("T"), False],
		[ord("T"), True],
		[0x2D, True],
	]
	assert [reached for _vk, up, reached in once["events"] if up] == [True, True]
	# Left unanswered for a second, the helper says goodbye, and NVDA reads keys from X.
	assert value["goodbyes"] == [8]
	assert not value["kernel"]


# NVDA itself, its keys taken from the stand-in, which is the keyboard: the X source's own tests in
# test_keys.py, held to the kernel source.


@pytest.fixture
def desktop(tmp_path: Path) -> Iterator[DesktopSession]:
	with DesktopSession(tmp_path / "desktop") as session:
		yield session


@pytest.fixture
def keyboard(desktop: DesktopSession) -> Iterator[StandIn]:
	socketDir = Path(tempfile.mkdtemp(prefix="keyd", dir="/tmp"))
	try:
		with StandIn(socketDir / "socket") as standIn:
			yield standIn
	finally:
		shutil.rmtree(socketDir, ignore_errors=True)


#: A command bound to the one with no gesture of its own that takes the keyboard back.
TAKE_BACK_GESTURES = "[globalCommands.GlobalCommands]\n\ttakeKeyboardBack = kb:NVDA+shift+f12\n"


class KernelSession:
	"""The GTK fixture with its text field focused, and NVDA started beside it, taking its keys from
	the stand-in."""

	def __init__(
		self,
		desktop: DesktopSession,
		keyboard: StandIn,
		tmpPath: Path,
		layoutName: str,
		*,
		config: str = "",
		gestures: str = "",
		env: dict | None = None,
	) -> None:
		self.desktop = desktop
		self.keyboard = keyboard
		self.layout, nvdaKey, self.nvda = LAYOUTS[layoutName]
		configDir = tmpPath / "config"
		configDir.mkdir()
		(configDir / "nvda.ini").write_text(
			configText(self.layout, nvdaKey).replace("[keyboard]\n", "[keyboard]\n" + config)
		)
		if gestures:
			(configDir / "gestures.ini").write_text(gestures)
		hooks = tmpPath / "hooks"
		(hooks / "globalPlugins").mkdir(parents=True)
		(hooks / "synthDrivers").symlink_to(harness.HERE / "synthDrivers")
		environment = {
			"LINDA_TEST_HOOKS_DIR": str(hooks),
			"LINDA_TEST_KEYD_SOCKET": keyboard.path,
			**(env or {}),
		}
		desktop.startApp(desktop.fixture("gtkFixture"), title="Fixture window")
		self.launch = desktop.startNVDA(configDir, env=environment)
		desktop.expect("Fixture window", since=0, timeout=STARTUP_TIMEOUT)
		keyboard.waitFor(lambda: keyboard.capturing, 10)
		self.launch.waitForLog("Keys come from the kernel through linda-keyd: linda-keyd admitted NVDA", 5)
		mark = desktop.mark()
		keyboard.press("Tab", "Tab", "Tab", "Tab")
		desktop.expect("Name: edit\N{HORIZONTAL ELLIPSIS}", since=mark)
		assert entryState() == (ENTRY_TEXT, len(ENTRY_TEXT))

	def chord(self, key: str) -> str:
		return f"{self.nvda}+{key}"

	def quit(self) -> harness.Launch:
		self.launch.process.terminate()
		self.launch.waitForExit(20)
		return self.launch


@pytest.mark.parametrize("layoutName", LAYOUTS)
def test_kernelNvdaChordReachesNVDAAlone(
	desktop: DesktopSession, keyboard: StandIn, tmp_path: Path, layoutName: str
):
	keys = KernelSession(desktop, keyboard, tmp_path, layoutName)
	mark = desktop.mark()
	keyboard.press(keys.chord("t"))
	desktop.expect("Fixture window", since=mark)
	time.sleep(0.3)
	assert entryState()[0] == ENTRY_TEXT
	# Caps Lock kept for NVDA never reached the X display, so it locked nothing.
	assert not desktopSession.capsLockIsOn()
	keyboard.press("a")
	waitForEntry("a")
	launch = keys.quit()
	launch.assertClean(logNames=("nvda.log",))
	log = launch.text("nvda.log")
	assert f"Input: kb({keys.layout}):NVDA+t" in log
	assert f"Input: kb({keys.layout}):(typing key)" in log
	assert "Keys are read from the X display" not in log


@pytest.mark.parametrize("layoutName", LAYOUTS)
def test_kernelNvdaChordWithoutACommandReachesTheApplication(
	desktop: DesktopSession, keyboard: StandIn, tmp_path: Path, layoutName: str
):
	keys = KernelSession(desktop, keyboard, tmp_path, layoutName)
	keyboard.press(keys.chord("z"))
	waitForEntry("z")
	mark = desktop.mark()
	keyboard.down(keys.nvda)
	keyboard.press("y")
	time.sleep(0.3)
	keyboard.press("t")
	time.sleep(0.3)
	keyboard.up(keys.nvda)
	desktop.expect("Fixture window", since=mark)
	time.sleep(0.3)
	assert entryState()[0] == "zy"
	assert not desktopSession.capsLockIsOn()
	keyboard.press("a")
	waitForEntry("zya")
	launch = keys.quit()
	launch.assertClean(logNames=("nvda.log",))
	assert f"Input: kb({keys.layout}):NVDA+z" in launch.text("nvda.log")


def passedTwice(keyboard: StandIn, key: str) -> None:
	keyboard.press(key)
	time.sleep(0.15)
	keyboard.press(key)
	time.sleep(0.3)


def test_kernelInsertPassedThrough(desktop: DesktopSession, keyboard: StandIn, tmp_path: Path):
	keys = KernelSession(desktop, keyboard, tmp_path, "desktop")
	keyboard.press("Home")
	mark = desktop.mark()
	keyboard.press(keys.chord("F2"))
	desktop.expect("Pass next key through", since=mark)
	keyboard.press("Insert")
	keyboard.type("x")
	waitForEntry("xlice")
	assert entryState() == ("xlice", 1)
	passedTwice(keyboard, "Insert")
	keyboard.type("y")
	waitForEntry("xylice")
	mark = desktop.mark()
	keyboard.press(keys.chord("F2"))
	desktop.expect("Pass next key through", since=mark)
	keyboard.press(keys.chord("t"))
	waitForEntry("xytice")
	assert entryState() == ("xytice", 3)
	assert "Fixture window" not in desktop.spokenSince(mark)
	keys.quit().assertClean(logNames=("nvda.log",))


def test_kernelCapsLockPassedThrough(desktop: DesktopSession, keyboard: StandIn, tmp_path: Path):
	keys = KernelSession(desktop, keyboard, tmp_path, "laptop")
	keyboard.press("Home")
	mark = desktop.mark()
	keyboard.press(keys.chord("F2"))
	desktop.expect("Pass next key through", since=mark)
	keyboard.press("Caps_Lock")
	time.sleep(0.3)
	assert desktopSession.capsLockIsOn()
	keyboard.type("x")
	waitForEntry("XAlice")
	mark = desktop.mark()
	passedTwice(keyboard, "Caps_Lock")
	desktop.expect("caps lock off", since=mark)
	assert not desktopSession.capsLockIsOn()
	passedTwice(keyboard, "Caps_Lock")
	desktop.expect("caps lock off", "caps lock on", since=mark)
	assert desktopSession.capsLockIsOn()
	passedTwice(keyboard, "Caps_Lock")
	assert not desktopSession.capsLockIsOn()
	keyboard.press(keys.chord("t"))
	keyboard.type("y")
	waitForEntry("XyAlice")
	assert entryState() == ("XyAlice", 2)
	keys.quit().assertClean(logNames=("nvda.log",))


@pytest.mark.parametrize("layoutName", LAYOUTS)
def test_kernelNumpadReviewKeyLeavesTheCaret(
	desktop: DesktopSession, keyboard: StandIn, tmp_path: Path, layoutName: str
):
	keys = KernelSession(desktop, keyboard, tmp_path, layoutName)
	keyboard.press("End")
	time.sleep(0.3)
	before = entryState()
	mark = desktop.mark()
	keyboard.press("KP_Up")
	desktop.expect(ENTRY_TEXT, since=mark)
	time.sleep(0.3)
	assert entryState() == before
	# With Num Lock on, the key is the digit 8, the field's.
	keyboard.press("Num_Lock")
	keyboard.press("KP_Up")
	waitForEntry(ENTRY_TEXT + "8")
	keyboard.press("Num_Lock")
	keys.quit().assertClean(logNames=("nvda.log",))


@pytest.mark.parametrize("layoutName", LAYOUTS)
def test_kernelInputHelpHoldsTheKeyboard(
	desktop: DesktopSession, keyboard: StandIn, tmp_path: Path, layoutName: str
):
	keys = KernelSession(desktop, keyboard, tmp_path, layoutName)
	mark = desktop.mark()
	keyboard.press(keys.chord("1"))
	on = "input help on\N{HORIZONTAL ELLIPSIS}"
	desktop.expect(on, since=mark)
	keyboard.press("t")
	desktop.expect(on, "t\N{HORIZONTAL ELLIPSIS}", since=mark)
	keyboard.press(keys.chord("1"))
	desktop.expect(on, "t\N{HORIZONTAL ELLIPSIS}", "input help off", since=mark)
	time.sleep(0.3)
	assert entryState()[0] == ENTRY_TEXT
	keyboard.press("b")
	waitForEntry("b")
	keys.quit().assertClean(logNames=("nvda.log",))


@pytest.mark.parametrize("layoutName", LAYOUTS)
def test_kernelControlCancelsReading(
	desktop: DesktopSession, keyboard: StandIn, tmp_path: Path, layoutName: str
):
	keys = KernelSession(
		desktop,
		keyboard,
		tmp_path,
		layoutName,
		env={"LINDA_TRANSCRIPT_MODE": "timed", "LINDA_TRANSCRIPT_MS_PER_CHAR": "10"},
	)
	launch = keys.launch
	mark = desktop.mark()
	keyboard.press(keys.chord("b"), gap=0)
	deadline = time.monotonic() + 10
	while len(desktop.spokenSince(mark)) == 0:
		assert time.monotonic() < deadline, "reading the window did not start"
		time.sleep(0.01)
	pressed = time.monotonic()
	keyboard.press("Control_L")
	time.sleep(1.5)
	records = [r for r in transcript(launch) if r["t"] >= pressed]
	cancels = [r for r in records if r["kind"] == "cancel"]
	assert cancels, f"no cancel after Control: {records}"
	assert cancels[0]["t"] - pressed < 0.1, f"the cancel came {cancels[0]['t'] - pressed:.3f} s after Control"
	assert not [r for r in records if r["kind"] == "speak" and r["t"] > cancels[0]["t"]]
	keys.quit().assertClean(logNames=("nvda.log",))


def test_kernelPolishAltGrLettersAreTyping(desktop: DesktopSession, keyboard: StandIn, tmp_path: Path):
	subprocess.run(["setxkbmap", "pl"], check=True)
	try:
		keys = KernelSession(desktop, keyboard, tmp_path, "desktop")
		keyboard.press("ISO_Level3_Shift+a", "ISO_Level3_Shift+l")
		waitForEntry("ął")
		launch = keys.quit()
		launch.assertClean(logNames=("nvda.log",))
		log = launch.text("nvda.log")
		assert log.count("Input: kb(desktop):(typing key)") >= 2
		assert "alt+a" not in log and "alt+l" not in log
	finally:
		subprocess.run(["setxkbmap", "us"], check=True)


def test_kernelTypedCharactersAndWordsAreSpoken(desktop: DesktopSession, keyboard: StandIn, tmp_path: Path):
	keys = KernelSession(
		desktop,
		keyboard,
		tmp_path,
		"desktop",
		config="\tspeakTypedCharacters = 1\n\tspeakTypedWords = 1\n",
	)
	keyboard.press("End")
	time.sleep(0.3)
	mark = desktop.mark()
	keyboard.type("ab")
	keyboard.press("space")
	desktop.expect("a", "b", "ab", since=mark)
	keys.quit().assertClean(logNames=("nvda.log",))


def test_kernelSleepModePassesEveryKey(desktop: DesktopSession, keyboard: StandIn, tmp_path: Path):
	keys = KernelSession(desktop, keyboard, tmp_path, "desktop")
	keyboard.press("Home")
	mark = desktop.mark()
	keyboard.press(keys.chord("shift+s"))
	desktop.expect("Sleep mode on", since=mark)
	mark = desktop.mark()
	# The NVDA key is still NVDA's, as on Windows, and NVDA+t, no command now, the field's.
	keyboard.press(keys.chord("t"))
	waitForEntry("tAlice")
	assert "Fixture window" not in desktop.spokenSince(mark)
	keyboard.press(keys.chord("shift+s"))
	desktop.expect("Sleep mode off", since=mark)
	keys.quit().assertClean(logNames=("nvda.log",))


def test_kernelEmergencyChordLeavesAWorkingReader(desktop: DesktopSession, keyboard: StandIn, tmp_path: Path):
	keys = KernelSession(desktop, keyboard, tmp_path, "desktop", gestures=TAKE_BACK_GESTURES)
	launch = keys.launch
	mark = desktop.mark()
	for key in ("BackSpace", "Escape", "Return"):
		keyboard.down(key)
		time.sleep(0.05)
	for key in ("Return", "Escape", "BackSpace"):
		keyboard.up(key)
	desktop.expect(LET_GO_MESSAGE.replace("NVDA", "Linda"), since=mark)
	launch.waitForLog(
		"Keys are read from the X display: linda-keyd was given up: it let go of the keyboard", 5
	)
	# The X route works: the keyboard reaches the X display directly now.
	time.sleep(0.5)
	mark = desktop.mark()
	keyboard.press(keys.chord("t"))
	desktop.expect("Fixture window", since=mark)
	# And NVDA does not take the keyboard back by itself.
	time.sleep(3)
	assert keyboard.starts == 1
	mark = desktop.mark()
	keyboard.press(keys.chord("shift+F12"))
	desktop.expect("Linda has the keyboard again", since=mark)
	assert keyboard.starts == 2
	keyboard.waitFor(lambda: keyboard.capturing)
	time.sleep(0.5)
	mark = desktop.mark()
	keyboard.press(keys.chord("t"))
	desktop.expect("Fixture window", since=mark)
	launch = keys.quit()
	launch.assertClean(logNames=("nvda.log",))
	assert launch.text("nvda.log").count("Keys come from the kernel through linda-keyd") == 2


def test_kernelHelperGoneAndBack(desktop: DesktopSession, keyboard: StandIn, tmp_path: Path):
	keys = KernelSession(desktop, keyboard, tmp_path, "desktop")
	launch = keys.launch
	keyboard.down("Insert")
	time.sleep(0.2)
	keyboard.vanish()
	launch.waitForLog(
		"Keys are read from the X display: linda-keyd was given up: it closed the connection", 5
	)
	keyboard.up("Insert")
	time.sleep(0.5)
	# Insert, held as the helper went, is not still taken for held: t is a plain t.
	mark = desktop.mark()
	keyboard.press("t")
	waitForEntry("t")
	time.sleep(0.3)
	assert "Fixture window" not in desktop.spokenSince(mark)
	keyboard.press(keys.chord("t"))
	desktop.expect("Fixture window", since=mark)
	keyboard.comeBack()
	keyboard.waitFor(lambda: keyboard.capturing, 6)
	time.sleep(0.5)
	mark = desktop.mark()
	keyboard.press(keys.chord("t"))
	desktop.expect("Fixture window", since=mark)
	keyboard.press("u")
	waitForEntry("tu")
	launch = keys.quit()
	launch.assertClean(logNames=("nvda.log",))
	log = launch.text("nvda.log")
	assert log.count("Keys come from the kernel through linda-keyd") == 2
	assert f"Input: kb({keys.layout}):NVDA+t" in log


def test_withoutTheHelperKeysComeFromX(desktop: DesktopSession, tmp_path: Path):
	config = tmp_path / "nvda.ini"
	config.write_text(configText("desktop", 6))
	desktop.startApp(desktop.fixture("gtkFixture"), title="Fixture window")
	launch = desktop.startNVDA(config)
	desktop.expect("Fixture window", since=0, timeout=STARTUP_TIMEOUT)
	launch.waitForLog(
		"Keys are read from the X display: linda-keyd is not running, as there is no /run/linda-keyd/socket",
		5,
	)
	mark = desktop.mark()
	desktop.press("Insert+t")
	desktop.expect("Fixture window", since=mark)
	launch.process.terminate()
	launch.waitForExit(20)
	launch.assertClean(logNames=("nvda.log",))


def test_kernelBrowseAndFocusModeKeys(desktop: DesktopSession, keyboard: StandIn, tmp_path: Path):
	"""test_browseKeys.test_browseAndFocusModeKeys, the keys typed on the stand-in."""
	webSession.requireBrowseLibraries()
	webSession.startFirefox(desktop, "keys.html")
	env = {**browseKeys.probeHooks(tmp_path), "LINDA_TEST_KEYD_SOCKET": keyboard.path}
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

	step("Tab", "Field edit" + PREFIX_MARK, "Focus mode")
	browseKeys.waitForCapture(nvda, "focus")
	keyboard.type("ab")
	page.waitForLog(["a", "b"])
	page.waitForField("ab")
	step("Escape", "Browse mode")
	browseKeys.waitForCapture(nvda, "browse")

	step("control+Home", "heading level 1 Keys page")
	step("Down", "Every key that reaches this page is written to the log at its end.")
	step("h", "Field and link heading level 2")
	typed = nvda.text("nvda.log").count("Input: kb(desktop):(typing key)")
	keyboard.press("space")
	time.sleep(browseKeys.SETTLE)
	assert nvda.text("nvda.log").count("Input: kb(desktop):(typing key)") == typed + 1
	step("1", "No next heading at level 1")
	assert page.log() == ["a", "b"]
	assert page.field() == "ab"

	step("control+Home", "heading level 1 Keys page")
	# Down held: the kernel's repeats come to NVDA alone, each a line.
	mark = desktop.mark()
	keyboard.down("Down")
	for _ in range(4):
		time.sleep(0.15)
		keyboard.repeat("Down")
	keyboard.up("Down")
	time.sleep(browseKeys.SETTLE)
	assert len(desktop.spokenSince(mark)) >= 3, desktop.spokenSince(mark)
	assert page.log() == ["a", "b"]

	step("Insert+shift+space", "Single letter navigation off")
	keyboard.press("h")
	page.waitForLog(["a", "b", "h"])
	step("Insert+shift+space", "Single letter navigation on")
	assert page.log() == ["a", "b", "h"]

	step("control+l", browseKeys.ADDRESS_BAR + PREFIX_MARK)
	browseKeys.waitForCapture(nvda, "off")
	assert page.log() == ["a", "b", "h", "Control+l"]
	step("Insert+q", "Exit Linda" + PREFIX_MARK)
	keyboard.press("Return")
	nvda.waitForExit(10)
	nvda.assertClean(logNames=("nvda.log",))
