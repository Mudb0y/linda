"""Package HV's light probe on GNOME, with a real kernel, seat and helper: a fresh helper lights its new
virtual keyboard for Linda, which asks for the probe, GNOME Shell writes its own lights over them, and
the helper says SEEN, after which Linda says no rescue hint and remembers the proof. The probe's lights
never reach a keyboard, and the virtual keyboard is left with GNOME's, Num Lock included. A Linda that
comes to a virtual keyboard made before it hears OLD and holds. A virtual keyboard GNOME does not read,
which a udev rule hides from libinput, is UNSEEN on a desktop seen before, and Linda lets go and says
so; the rule gone and the helper started again, Linda holds once more.

Its figures go to the log as one JSON object, after "HV figures".
"""

import json
import re
from typing import TYPE_CHECKING

from ground import KERNEL_ROUTE, VIRTUAL_KEYBOARD, WINDOW_TITLE, Mark
from wl import lindaAcceptance as acceptance
from wl.helperSafety import lights, typedKeys, uhidLights
from wl.lindaAcceptance import HINT, LOST, ONCE, UHID, lindaKeyWorks, said, saidCount, typedThrough

if TYPE_CHECKING:
	from ground import FakeKeyboard, Ground

KEYD = "linda-keyd"
RECORD = "/home/alice/.local/state/linda/keyDelivery"
OLD_LOG = "/tmp/ground/linda/nvda-old.log"
#: The file the case's udev rule looks for, which hides the helper's virtual keyboard from libinput.
IGNORE = "/run/ground-ignore-virtual"
WRITTEN = re.compile(r"the virtual keyboard's lights were written (\d+) ms after it was made")
UNWRITTEN = "nothing wrote the virtual keyboard's lights"
SEEN_LOG = "linda-keyd's probe of its virtual keyboard's lights: the desktop wrote them"
OLD_LOG = "linda-keyd's probe of its virtual keyboard's lights: nothing can be seen"
UNSEEN_LOG = "linda-keyd's probe of its virtual keyboard's lights: nothing wrote them"
PRESENT = "Keys NVDA passes on reach the desktop: the desktop wrote the lights of linda-keyd's keyboard"
ABSENT_LOG = "Keys NVDA passes on do not reach the desktop: the desktop did not write the lights"
NOT_HINTED = "The rescue hint is not said"
ABSENT = (
	"Keys Linda passes on do not reach the desktop, so Linda has let go of the keyboard. "
	"Start Linda again once that is put right."
)
NUM_LIGHT = 1 << 0


def written(ground: "Ground", since: Mark) -> int:
	"""How many milliseconds after the helper made its virtual keyboard GNOME wrote its lights."""
	_t, line = ground.waitForHelperLog("the virtual keyboard's lights were written", since, timeout=30)
	found = WRITTEN.search(line)
	assert found, line
	return int(found.group(1))


def restartHelper(ground: "Ground") -> Mark:
	"""Start the helper afresh, as a new one makes its virtual keyboard anew for Linda; answers the mark
	from before it."""
	mark = ground.mark()
	ground.shell(f"systemctl reset-failed {KEYD}; systemctl restart {KEYD}")
	ground.waitForHelper()
	return mark


def litBeyond(keyboard: "FakeKeyboard", since: Mark, allowed: int) -> list[int]:
	"""The lights the kernel set on keyboard after since that are other than allowed."""
	return [r["leds"] for r in keyboard.records(since) if "leds" in r and r["leds"] & ~allowed]


def freshHelper(ground: "Ground", keyboard: "FakeKeyboard", figures: dict) -> None:
	with ground.subtest(
		"a fresh helper sees GNOME write its virtual keyboard's lights, and Linda says no hint"
	):
		ground.shell(f"rm -f {RECORD}")
		mark = Mark(0, ground.now())
		ground.startLinda()
		ground.waitForLindaLog(KERNEL_ROUTE, timeout=30)
		figures["writtenMs"] = written(ground, mark)
		assert figures["writtenMs"] < 1000, figures
		ground.waitForLindaLog(SEEN_LOG)
		ground.waitForLindaLog(PRESENT)
		ground.settle(ONCE)
		assert saidCount(ground, mark, HINT) == 0, said(ground, mark)
		record = ground.shell(f"cat {RECORD}").split()
		figures["record"] = record
		assert record[0] == "gnome" and record[2:] == ["probed"], record
		lindaKeyWorks(ground, keyboard)
		typedThrough(ground, keyboard, "KEY_A", "a", echo="a")

	with ground.subtest("the probe's lights reach no keyboard, and the virtual keyboard has GNOME's"):
		figures["virtualLights"] = lights(ground, VIRTUAL_KEYBOARD)
		figures["uhidLights"] = uhidLights(keyboard)
		assert figures["virtualLights"] == 0, figures
		assert not litBeyond(keyboard, mark, 0), figures["uhidLights"]


def numLock(ground: "Ground", keyboard: "FakeKeyboard", figures: dict) -> None:
	with ground.subtest("with Num Lock on, a new helper's probe leaves Num Lock lit and no other light"):
		mark = ground.mark()
		keyboard.tap("KEY_NUMLOCK")
		ground.waitFor(lambda: lights(ground, VIRTUAL_KEYBOARD) == NUM_LIGHT, "GNOME to light Num Lock")
		routes = len(ground.lindaLogRecords(KERNEL_ROUTE))
		seen = len(ground.lindaLogRecords(SEEN_LOG))
		mark = restartHelper(ground)
		ground.waitForLindaLog(KERNEL_ROUTE, count=routes + 1, timeout=30)
		figures["writtenWithNumLockMs"] = written(ground, mark)
		ground.waitForLindaLog(SEEN_LOG, count=seen + 1)
		figures["virtualLightsWithNumLock"] = lights(ground, VIRTUAL_KEYBOARD)
		figures["uhidLightsWithNumLock"] = [r["leds"] for r in keyboard.records(mark) if "leds" in r]
		assert figures["virtualLightsWithNumLock"] == NUM_LIGHT, figures
		assert not litBeyond(keyboard, mark, NUM_LIGHT), figures["uhidLightsWithNumLock"]
		ground.waitForSpeech(LOST, mark)
		assert saidCount(ground, mark, HINT) == 0, said(ground, mark)
		lindaKeyWorks(ground, keyboard)
		mark = ground.mark()
		keyboard.tap("KEY_NUMLOCK")
		ground.waitFor(lambda: lights(ground, VIRTUAL_KEYBOARD) == 0, "GNOME to put Num Lock out")


def secondLinda(ground: "Ground", keyboard: "FakeKeyboard") -> None:
	with ground.subtest("a Linda started after the virtual keyboard was made hears OLD and holds"):
		ground.stopLinda()
		mark = Mark(0, ground.now())
		# The new copy moves the first one's log aside as it starts.
		ground.startLinda()
		ground.waitForLindaLog(KERNEL_ROUTE, timeout=30)
		ground.waitForLindaLog(OLD_LOG)
		ground.waitForLindaLog(NOT_HINTED)
		ground.settle(ONCE)
		assert saidCount(ground, mark, HINT) == 0, said(ground, mark)
		probed = [m for _t, m in ground.helperLog(mark) if "lights were written" in m or UNWRITTEN in m]
		assert not probed, probed
		lindaKeyWorks(ground, keyboard)
		errors = acceptance.logErrors(ground.shell(f"cat {OLD_LOG}"))
		assert not errors, f"the first copy's log holds {len(errors)} records at ERROR or above: {errors}"


def unread(ground: "Ground", keyboard: "FakeKeyboard", figures: dict) -> None:
	with ground.subtest("a virtual keyboard GNOME does not read is UNSEEN, and Linda lets go and says so"):
		ground.shell(f"touch {IGNORE}")
		mark = restartHelper(ground)
		_t, line = ground.waitForHelperLog(UNWRITTEN, mark, timeout=30)
		figures["unwritten"] = line
		ground.waitForLindaLog(UNSEEN_LOG)
		ground.waitForLindaLog(ABSENT_LOG)
		ground.waitForSpeech(ABSENT, mark)
		figures["gnomeHasVirtual"] = VIRTUAL_KEYBOARD in ground.displayDevices()
		assert not figures["gnomeHasVirtual"], ground.displayDevices()
		assert not litBeyond(keyboard, mark, 0), [r for r in keyboard.records(mark) if "leds" in r]
		ground.settle(1)
		typed = ground.mark()
		keyboard.tap("KEY_U")
		app = ground.waitForKeys("app", typedKeys("KEY_U"), typed)
		assert app[0]["keyval"] == "u", app
		assert not ground.keys("evdev", typed, device=VIRTUAL_KEYBOARD)

	with ground.subtest("the rule gone and the helper started again, Linda holds the keyboard once more"):
		ground.shell(f"rm -f {IGNORE}")
		routes = len(ground.lindaLogRecords(KERNEL_ROUTE))
		mark = restartHelper(ground)
		ground.waitForLindaLog(KERNEL_ROUTE, count=routes + 1, timeout=30)
		written(ground, mark)
		lindaKeyWorks(ground, keyboard)
		typedThrough(ground, keyboard, "KEY_B", "b", echo="b")


def run(ground: "Ground") -> None:
	ground.openWindow()
	ground.activate(WINDOW_TITLE)
	ground.waitForHelper()
	keyboard = ground.uhidKeyboard(UHID)
	figures: dict = {}
	freshHelper(ground, keyboard, figures)
	numLock(ground, keyboard, figures)
	secondLinda(ground, keyboard)
	unread(ground, keyboard, figures)
	ground.machine.log("HV figures " + json.dumps(figures, sort_keys=True))
