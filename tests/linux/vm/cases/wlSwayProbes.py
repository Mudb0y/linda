"""The experiments that turn on sway, with linda-keyd's own test client and no Linda:

- E6, what sway writes to a new keyboard's lock lights, by guest/ledProbe.py's experiment, which
  decides whether package HV's light probe can prove sway reads the virtual keyboard;
- E16, Caps Lock turned on through the helper and a letter typed after the helper lets go, by STOP
  and by its death, which decides whether package HL must put the real keyboards' lights back as the
  helper lets go;
- E15, keys typed from the moment a switch back from a text console begins, which decides whether
  package HL adds a re-settle before the helper grabs again.

Each experiment's figures go to the log as one JSON object, after "E6 sway", "E16 sway" and
"E15 sway". A case fails only when an experiment could not be made; what it found is written down.
"""

import json
import shlex
from typing import TYPE_CHECKING

from ground import AT_KEYBOARD, VIRTIO_KEYBOARD, VIRTUAL_KEYBOARD, WINDOW_TITLE, Mark
from wl.helperSafety import CAPS, appKeyvals, lights, restartHelper, typedKeys

if TYPE_CHECKING:
	from ground import FakeKeyboard, Ground, KeydClient

UHID = "Ground uhid keyboard"
#: sway's own process, by its comm, the one reader of the probe that is the desktop.
READER = "^sway$"
#: Thirty-six keys, each typed once, from a little before a switch back until well after it.
STREAM = [f"KEY_{c}" for c in "QWERTYUIOPASDFGHJKLZXCVBNM1234567890"]
STREAM_HOLD = 0.01
STREAM_GAP = 0.05
#: Seconds of the stream typed on the text console before chvt brings the session back.
STREAM_LEAD = 0.2
TRIALS = 3
CONTROL_TRIALS = 2
CONSOLE_TEXT = "/tmp/ground/console.txt"
TYPED = "/tmp/ground/typed.json"


def lightProbe(ground: "Ground", desktop: str) -> None:
	with ground.subtest("E6: what the desktop writes to a new keyboard's lights"):
		result = json.loads(ground.shell(f"ground-led-probe experiment --reader {shlex.quote(READER)}"))
		ground.machine.log(f"E6 {desktop} " + json.dumps(result))
		assert result["presserOpened"], f"the desktop never opened the presser: {result}"
		for run in result["runs"]:
			ground.machine.log(
				f"E6 {desktop}, Num Lock {'on' if run['numLock'] else 'off'}, preset {run['preset']} "
				f"({run['presetBits']}): the desktop opened it {run['desktop'] and run['desktop']['after']} s "
				f"after it was made and wrote {[(e['code'], e['value'], e['after']) for e in run['ledsByDesktop']]}, "
				f"changing bits {run['desktopChangedBits']}; before it, {run['ledsBeforeDesktop']}; echoed "
				f"{run['echoed']}; the probe ended with {run['final']}; the reference keyboard had "
				f"{run['referenceBefore']} before and {run['referenceAfter']} after"
			)
			assert run["desktop"] is not None, f"the desktop never opened the probe: {run['openers']}"


def keypad(ground: "Ground") -> list[str]:
	"""What the program had the numpad's 1 as, from QEMU's keyboard: KP_1 with Num Lock on."""
	mark = ground.mark()
	ground.press("kp_1")
	return appKeyvals(ground.waitForKeys("app", typedKeys("KEY_KP1"), mark))


def numLock(ground: "Ground", on: bool) -> None:
	"""Put the desktop's Num Lock as asked, judged by what the numpad types, not by the lights."""
	for _press in range(2):
		if (keypad(ground) == ["KP_1"]) == on:
			return
		ground.press("num_lock")
		ground.settle(0.3)
	assert (keypad(ground) == ["KP_1"]) == on, f"Num Lock would not go {'on' if on else 'off'}"


def keyboardAdded(ground: "Ground", desktop: str) -> None:
	with ground.subtest("E6: a keyboard added, and the desktop's own Num Lock"):
		numLock(ground, True)
		before = lights(ground, VIRTIO_KEYBOARD)
		ground.keyboard("Ground added keyboard")
		ground.settle(0.5)
		seen = {
			"lightsBefore": before,
			"lightsAfter": lights(ground, VIRTIO_KEYBOARD),
			"keypadAfter": keypad(ground),
		}
		ground.machine.log(f"E6 {desktop} a keyboard added with Num Lock on: {json.dumps(seen)}")
		numLock(ground, False)


def allLights(ground: "Ground", virtual: bool = True) -> dict[str, int]:
	names = [UHID, VIRTIO_KEYBOARD, AT_KEYBOARD] + ([VIRTUAL_KEYBOARD] if virtual else [])
	return {name: lights(ground, name) for name in names}


def letter(ground: "Ground", press) -> list[str]:
	"""What the program had A as, typed by press."""
	mark = ground.mark()
	press()
	return appKeyvals(ground.waitForKeys("app", typedKeys("KEY_A"), mark))


def capsOff(ground: "Ground", keyboard: "FakeKeyboard") -> list[str]:
	"""Tap Caps Lock on the keyboard itself until A comes as a; answers what each A came as."""
	came = []
	for _attempt in range(3):
		came += letter(ground, lambda: keyboard.tap("KEY_A"))
		if came[-1] == "a":
			return came
		keyboard.tap("KEY_CAPSLOCK")
		ground.settle(0.3)
	raise AssertionError(f"Caps Lock would not go off: A came as {came}")


def capsAfterLetGo(ground: "Ground", keyboard: "FakeKeyboard", client: "KeydClient", desktop: str) -> None:
	seen: dict[str, object] = {}
	for how in ("stop", "kill"):
		with ground.subtest(f"E16: Caps Lock on through the helper, then a letter once it lets go ({how})"):
			mark = ground.mark()
			client.capture(UHID, VIRTIO_KEYBOARD, AT_KEYBOARD, since=mark)
			ground.settle(0.5)
			atStart = allLights(ground)
			tapped = ground.mark()
			keyboard.tap("KEY_CAPSLOCK")
			ground.waitForKeys("evdev", typedKeys("KEY_CAPSLOCK"), tapped, device=VIRTUAL_KEYBOARD)
			told = client.waitFor("LOCKS", tapped, lambda r: r["leds"] & CAPS)
			ground.settle(0.5)
			held = allLights(ground)
			heldCase = letter(ground, lambda: keyboard.tap("KEY_A"))
			mark = ground.mark()
			if how == "stop":
				client.stop()
				client.waitFor("RELEASED", mark, lambda r: r["reason"] == "STOPPED")
			else:
				restartHelper(ground, client, "systemctl kill -s KILL linda-keyd")
			ground.settle(0.5)
			after = allLights(ground, virtual=False)
			uhidCase = letter(ground, lambda: keyboard.tap("KEY_A"))
			virtioCase = letter(ground, lambda: ground.press("a"))
			# The user, hearing capitals or seeing the light, presses Caps Lock on the keyboard itself.
			keyboard.tap("KEY_CAPSLOCK")
			ground.settle(0.5)
			afterPress = allLights(ground, virtual=False)
			pressedCase = letter(ground, lambda: keyboard.tap("KEY_A"))
			seen[how] = {
				"lightsAtStart": atStart,
				"toldCapsAfter": round(told["t"] - tapped.t, 3),
				"lightsHeld": held,
				"letterHeld": heldCase,
				"lightsAfterLetGo": after,
				"letterAfterLetGoUhid": uhidCase,
				"letterAfterLetGoVirtio": virtioCase,
				"lightsAfterCapsPressed": afterPress,
				"letterAfterCapsPressed": pressedCase,
				"capsOffAgain": capsOff(ground, keyboard),
			}
			ground.machine.log(f"E16 {desktop} {how}: {json.dumps(seen[how])}")
			assert heldCase == ["A"], seen[how]
	ground.machine.log(f"E16 {desktop} " + json.dumps(seen, sort_keys=True))


def switchBack(ground: "Ground", keyboard: "FakeKeyboard", client: "KeydClient", desktop: str) -> None:
	mark = ground.mark()
	client.capture(UHID, VIRTIO_KEYBOARD, since=mark)
	session = int(ground.shell("fgconsole").strip())
	text = next(n for n in (3, 4, 5) if n != session)
	ground.shell(
		f"rm -f {CONSOLE_TEXT}; openvt -c {text} -- sh -c 'stty raw -echo; exec cat > {CONSOLE_TEXT}'; "
		f"until [ -e {CONSOLE_TEXT} ]; do sleep 0.05; done"
	)
	trials = []
	steps = json.dumps(
		[s for key in STREAM for s in ({"tap": key, "hold": STREAM_HOLD}, {"sleep": STREAM_GAP})]
	)
	for trial in range(1, TRIALS + 1):
		with ground.subtest(f"E15: keys typed from the moment of a switch back, trial {trial}"):
			mark = ground.mark()
			ground.shell(f"chvt {text}")
			client.waitFor("RELEASED", mark, lambda r: r["reason"] == "BACKGROUND")
			ground.settle(1)
			before = len(ground.shell(f"cat {CONSOLE_TEXT}"))
			back = ground.mark()
			switched = float(
				ground.shell(
					f"ground-keyboard call {shlex.quote(UHID)} {shlex.quote(steps)} > {TYPED} & "
					f"sleep {STREAM_LEAD}; ground-record now; chvt {session}; wait"
				).split()[0]
			)
			written = json.loads(ground.shell(f"cat {TYPED}"))["written"]
			held = client.waitForDevice(UHID, "HELD", back)
			ground.settle(1)
			console = ground.shell(f"cat {CONSOLE_TEXT}")[before:]
			result = streamFates(ground, client, back, switched, held["t"], written, console)
			trials.append(result)
			ground.machine.log(f"E15 {desktop} trial {trial}: " + json.dumps(result))
			assert result["typedAfterHeld"], f"the stream ended before the helper held again: {result}"
	# The same with no capture, to tell what the desktop loses by itself from what the helper adds.
	mark = ground.mark()
	client.stop()
	client.waitFor("RELEASED", mark, lambda r: r["reason"] == "STOPPED")
	alone = []
	for trial in range(1, CONTROL_TRIALS + 1):
		with ground.subtest(
			f"E15: keys typed from the moment of a switch back with nothing held, trial {trial}"
		):
			ground.shell(f"chvt {text}")
			ground.settle(1)
			before = len(ground.shell(f"cat {CONSOLE_TEXT}"))
			back = ground.mark()
			switched = float(
				ground.shell(
					f"ground-keyboard call {shlex.quote(UHID)} {shlex.quote(steps)} > {TYPED} & "
					f"sleep {STREAM_LEAD}; ground-record now; chvt {session}; wait"
				).split()[0]
			)
			written = json.loads(ground.shell(f"cat {TYPED}"))["written"]
			ground.settle(1)
			console = ground.shell(f"cat {CONSOLE_TEXT}")[before:]
			result = streamFates(ground, client, back, switched, switched, written, console)
			alone.append(result)
			ground.machine.log(f"E15 {desktop} with nothing held, trial {trial}: " + json.dumps(result))
	ground.machine.log(
		f"E15 {desktop} "
		+ json.dumps({"session": session, "text": text, "trials": trials, "nothingHeld": alone})
	)


def streamFates(
	ground: "Ground",
	client: "KeydClient",
	since: Mark,
	switched: float,
	held: float,
	written: list[dict],
	console: str,
) -> dict:
	"""Where each key of the stream went, by when it was typed from chvt and from the helper's grab."""
	heard = {r["key"] for r in client.keys(since) if r["value"] == 1}
	virtual = {r["key"] for r in ground.keys("evdev", since, device=VIRTUAL_KEYBOARD) if r["value"] == 1}
	app = ground.keys("app", since)
	pressed = {r["key"] for r in app if r["value"] == 1}
	released = {r["key"] for r in app if r["value"] == 0}
	keys = []
	for r in written:
		if r["value"] != 1:
			continue
		key = r["key"]
		keys.append(
			{
				"key": key,
				"fromSwitch": round(r["t"] - switched, 3),
				"fromHeld": round(r["t"] - held, 3),
				"console": key[4:].lower() in console,
				"client": key in heard,
				"virtual": key in virtual,
				"program": key in pressed,
				"releaseOnly": key in released and key not in pressed,
			}
		)
	front = [t for t, line in ground.helperLog(since) if "in front on seat0" in line]
	afterHeld = [k for k in keys if k["fromHeld"] >= 0]
	return {
		"heldAfterSwitch": round(held - switched, 3),
		"logindSaidAfterSwitch": round(front[0] - switched, 3) if front else None,
		"keys": keys,
		"lost": [k["key"] for k in keys if not k["program"] and not k["console"]],
		"releaseOnly": [k["key"] for k in keys if k["releaseOnly"]],
		"typedAfterHeld": len(afterHeld),
		"firstAfterHeld": afterHeld[0] if afterHeld else None,
		"passedButLost": [k["key"] for k in keys if k["virtual"] and not k["program"]],
	}


def run(ground: "Ground") -> None:
	ground.machine.log(f"the compositor: {json.dumps(ground.focus('facts'))}")
	ground.openWindow()
	ground.activate(WINDOW_TITLE)
	ground.waitForHelper()
	lightProbe(ground, "sway")
	keyboardAdded(ground, "sway")
	keyboard = ground.uhidKeyboard(UHID)
	client = ground.keydClient("main")
	assert client.connect()["welcome"]
	capsAfterLetGo(ground, keyboard, client, "sway")
	switchBack(ground, keyboard, client, "sway")
