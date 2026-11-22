"""Two faults of the X route with linda-keyd, each shown against the stand-in for the helper: another
program having the helper went unsaid, and a Caps Lock key an xkb option makes something else was
still taken for Caps Lock. Each test fails while the fault is there. Run under the rig:

	nix develop . -c tests/linux/rig python3 -m pytest tests/linux/test_kernelKeysReview6.py
"""

from pathlib import Path

import harness
import pytest
from test_kernelKeys import runKernelChild

#: keydClient's lights, as the tests do not import the product.
LOCK_NUM, LOCK_CAPS, LOCK_SCROLL = 1, 2, 4

BUSY_MESSAGE = (
	"Another program holds linda-keyd for this session, so NVDA takes its keys from the X display. "
	"If you started no other screen reader, something may be reading your keys."
)
REPLACED_MESSAGE = (
	"Another program took NVDA's place with linda-keyd and reads every key now. "
	"NVDA takes its keys from the X display."
)
FELL_BACK_MESSAGE = "NVDA now reads keys from the X display"

#: Another client of the session, which the stand-in serves, and which beats so as not to be dropped
#: for its silence.
OTHER_CLIENT = r"""
import socket, struct


def otherClient():
	other = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
	other.connect(path)
	other.send(struct.pack("<BHI", 1, 1, 0))
	assert other.recv(256)[0] == 0x81, "the stand-in did not admit the other client"
	stop = threading.Event()

	def beat():
		counter = 0
		while not stop.wait(0.3):
			counter += 1
			try:
				other.send(struct.pack("<BI", 0x05, counter))
			except OSError:
				return

	threading.Thread(target=beat, daemon=True).start()

	def close():
		stop.set()
		other.close()

	return close


def waitForKernel(source):
	deadline = time.monotonic() + 6
	while not (source.isKernel and keyboard.capturing) and time.monotonic() < deadline:
		time.sleep(0.05)
	return source.isKernel
"""

#: Another program has the helper as NVDA starts, and keeps it through three of NVDA's looks. Once it
#: lets go and NVDA has the helper, the helper drops NVDA, and another program has it again first.
BUSY_CHILD = (
	OTHER_CLIENT
	+ r"""
closeOther = otherClient()
source = startSource(waitForKernel=False)
time.sleep(4.5)
keyboard.press("Insert+t")
time.sleep(0.4)
throughX = [[e["vk"], e["nvda"]] for e in presses()]
whileBusy = list(said)
closeOther()
after = waitForKernel(source)
saidAfter = list(said)
keyboard.goodbye(8, "unanswered")
closeOther = otherClient()
deadline = time.monotonic() + 5
while source.isKernel and time.monotonic() < deadline:
	time.sleep(0.05)
time.sleep(4.5)
again = list(said)
closeOther()
result = {
	"refused": [e[2] for e in keyboard.events if e[1] == "refused"],
	"throughX": throughX,
	"whileBusy": whileBusy,
	"after": after,
	"said": saidAfter,
	"again": again,
}
source.stop()
keyboard.close()
"""
)


def test_fault_anotherProgramHoldingTheHelperIsNotSaid(tmp_path: Path):
	value = runKernelChild(tmp_path, BUSY_CHILD)
	assert len(value["refused"]) >= 2 and set(value["refused"]) == {4}, value
	assert value["whileBusy"] == [BUSY_MESSAGE], (
		f"linda-keyd turned NVDA away as another program of the session has it, which can read every key: "
		f"NVDA said nothing, or said it more than once: {value}"
	)
	# Read from the X display meanwhile, Insert is still the NVDA key.
	assert value["throughX"] == [[0x2D, True], [ord("T"), True]], value
	assert value["after"], value
	assert value["said"] == [BUSY_MESSAGE], value
	# Each spell of another program having the helper is its own, which NVDA's having it in between ends.
	assert value["again"] == [BUSY_MESSAGE, FELL_BACK_MESSAGE, BUSY_MESSAGE], (
		f"another program had linda-keyd again after NVDA had it, and NVDA did not say so: {value}"
	)


#: NVDA has the helper, and another program of the session takes its place with it, and keeps it
#: through NVDA's next looks.
REPLACED_CHILD = (
	OTHER_CLIENT
	+ r"""
source = startSource()
first = source.isKernel
keyboard.goodbye(9, "a client of the session now in front took this one's place")
closeOther = otherClient()
deadline = time.monotonic() + 5
while source.isKernel and time.monotonic() < deadline:
	time.sleep(0.05)
time.sleep(4.5)
delivered.clear()
keyboard.press("Insert+t")
time.sleep(0.4)
throughX = [[e["vk"], e["nvda"]] for e in presses()]
whileReplaced = list(said)
closeOther()
result = {
	"first": first,
	"refused": [e[2] for e in keyboard.events if e[1] == "refused"],
	"throughX": throughX,
	"whileReplaced": whileReplaced,
	"after": waitForKernel(source),
	"said": list(said),
}
source.stop()
keyboard.close()
"""
)


def test_fault_anotherProgramTakingNVDAsPlaceIsNotSaid(tmp_path: Path):
	value = runKernelChild(tmp_path, REPLACED_CHILD)
	assert value["first"], value
	assert len(value["refused"]) >= 2 and set(value["refused"]) == {4}, value
	assert value["whileReplaced"] == [REPLACED_MESSAGE], (
		"another program took NVDA's place with linda-keyd and can read every key; NVDA said only that it "
		f"reads keys from the X display, or said the other program has it again at each look: {value}"
	)
	assert value["throughX"] == [[0x2D, True], [ord("T"), True]], value
	assert value["after"], value
	assert value["said"] == [REPLACED_MESSAGE], value


#: The layout has OPTION, Caps Lock is off, Caps Lock is not an NVDA key, and the Caps Lock key is
#: pressed twice: whether NVDA takes Caps Lock to be on after each press.
LOCK_CHILD = r"""
import subprocess
subprocess.run(["setxkbmap", "-layout", "us", "-option", ""] + (["-option", OPTION] if OPTION else []), check=True)
time.sleep(0.3)
source = startSource(consumed=x11.EXTENDED_INSERT)
locks = [source.keyState(0x14) & 1]
for _ in range(2):
	keyboard.press("Caps_Lock")
	time.sleep(0.5)
	locks.append(source.keyState(0x14) & 1)
result = {"kernel": source.isKernel, "locks": locks}
subprocess.run(["setxkbmap", "-layout", "us", "-option", ""], check=True)
source.stop()
keyboard.close()
"""


@pytest.mark.parametrize("option", ("caps:escape", "ctrl:nocaps", None))
def test_fault_capsLockKeyAnOptionRemapsFlipsCapsLock(tmp_path: Path, option: str | None):
	value = runKernelChild(tmp_path, f"OPTION = {option!r}\n" + LOCK_CHILD)
	assert value["kernel"], value
	expected = [0, 0, 0] if option else [0, 1, 0]
	assert value["locks"] == expected, (
		f"under {option or 'no option'} NVDA took Caps Lock to be {value['locks']} before the presses and "
		f"after each, where the layout has it {expected}: {value}"
	)


#: Under ctrl:nocaps, which makes the Caps Lock key Control, Caps Lock is not an NVDA key, and h is
#: pressed with it held in browse mode: whether NVDA takes h for its own or the application has
#: Control+h.
MODIFIER_CHILD = r"""
import subprocess
subprocess.run(["setxkbmap", "-layout", "us", "-option", "", "-option", "ctrl:nocaps"], check=True)
time.sleep(0.3)
source = startSource(ROUTE, consumed=x11.EXTENDED_INSERT)
source.setCapture("browse", None)
mode["now"] = "browse"
time.sleep(0.1)
windowKeys()
delivered.clear()
keyboard.down("Caps_Lock")
time.sleep(0.1)
keyboard.press("h")
time.sleep(0.1)
keyboard.up("Caps_Lock")
time.sleep(0.3)
result = {
	"kernel": source.isKernel,
	"h": [[e["captured"], e["reached"]] for e in presses(ord("H"))],
	"window": windowKeys(),
}
subprocess.run(["setxkbmap", "-layout", "us", "-option", ""], check=True)
source.stop()
keyboard.close()
"""


@pytest.mark.parametrize("route", ("x11", "auto"))
def test_fault_capsLockKeyMadeControlIsNoModifier(tmp_path: Path, route: str):
	value = runKernelChild(tmp_path, f"ROUTE = {route!r}\n" + MODIFIER_CHILD)
	assert value["kernel"] == (route == "auto")
	assert value["h"] == [[False, True]], (
		f"on the {route} route h was pressed with the Caps Lock key held, which ctrl:nocaps makes Control: "
		f"NVDA took it for browse mode's h and not Control+h: {value}"
	)
	assert value["window"] == [[58, True], [35, True], [35, False], [58, False]], value


#: The tables kernel.py builds for the owner's layouts, with and without the option NixOS sets: which
#: modifiers the Caps Lock, Num Lock and Scroll Lock keys count as, and which light each key changes.
OWNER_TABLES_CHILD = r"""
from xkbcommon import xkb
from linuxPlatform.keys import kernel

result = []
for model in ("pc104", "pc105"):
	for layout in ("pl", "us"):
		for options in (None, "terminate:ctrl_alt_bksp"):
			keymap = xkb.Context().keymap_new_from_names(rules="evdev", model=model, layout=layout, options=options)
			tables = kernel._tables(keymap, (), (0, 0, 0))
			result.append(
				{
					"names": [model, layout, options],
					"lockMods": {str(k): tables.keyMods[k] for k in (66, 77, 78) if k in tables.keyMods},
					"lockLights": {str(k): v for k, v in tables.lockLights.items()},
				}
			)
"""


def test_ownerLockKeysAreNoModifiers(tmp_path: Path):
	done = harness.runChild(tmp_path, OWNER_TABLES_CHILD)
	assert done.error is None, f"{done.error}\n{done.stderr[-6000:]}"
	lights = {"66": LOCK_CAPS, "77": LOCK_NUM, "78": LOCK_SCROLL}
	for tables in done.value:
		# Held, a lock key holds no modifier: the Caps Lock key counted as Lock would be put down through
		# XTEST under a key NVDA passes on, and turn Caps Lock on in the X server.
		assert tables["lockMods"] == {}, tables
		assert tables["lockLights"] == lights, tables


#: Insert is the only NVDA key, and Insert+Caps_Lock+x is pressed, of which NVDA passes x on: whether
#: Caps Lock in the X server is as it was, and what the window has.
CAPS_UNDER_NVDA_KEY_CHILD = r"""
import desktopSession
keep = lambda event: event.vkCode != ord("X")
source = startSource(consumed=x11.EXTENDED_INSERT)
before = desktopSession.capsLockIsOn()
keyboard.press("Insert+Caps_Lock+x")
time.sleep(0.5)
result = {
	"kernel": source.isKernel,
	"before": before,
	"after": desktopSession.capsLockIsOn(),
	"window": windowKeys(),
}
source.stop()
keyboard.close()
"""


def test_capsLockUnderTheNVDAKeyStaysOutOfTheXServer(tmp_path: Path):
	value = runKernelChild(tmp_path, CAPS_UNDER_NVDA_KEY_CHILD)
	assert value["kernel"], value
	assert value["after"] == value["before"], (
		f"Insert+Caps_Lock+x with x passed on changed Caps Lock in the X server: {value}"
	)
	assert value["window"] == [[45, True], [45, False]], value
