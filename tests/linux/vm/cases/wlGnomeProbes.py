"""The experiments that turn on GNOME Shell in safe mode, with linda-keyd's own test client and no
Linda:

- E6, what mutter writes to a new keyboard's lock lights, by guest/ledProbe.py's experiment, which
  decides whether package HV's light probe can prove GNOME reads the virtual keyboard;
- E12, a plain process that owns org.gnome.Orca.KeyboardMonitor and calls mutter's
  org.freedesktop.a11y.KeyboardMonitor: whether it may watch and grab, whether its KeyEvents follow
  keys the helper passes through the virtual keyboard, and what Orca does with the name when it
  starts, which decides whether package KM can be built;
- E15, keys typed from the moment a switch back from a text console begins, which decides whether
  package HL adds a re-settle before the helper grabs again.

Each experiment's figures go to the log as one JSON object, after "E6 gnome", "E12 gnome" and
"E15 gnome". A case fails only when an experiment could not be made; what it found is written down.
"""

import json
import shlex
from typing import TYPE_CHECKING

from ground import VIRTIO_KEYBOARD, VIRTUAL_KEYBOARD, WINDOW_TITLE, Mark, keyEvents
from wl.helperSafety import NUM, appKeyvals, lights, typedKeys

if TYPE_CHECKING:
	from ground import FakeKeyboard, Ground, KeydClient

UHID = "Ground uhid keyboard"
#: gnome-shell's own process, by its comm, which Nix's wrapper makes .gnome-shell-wr.
READER = "gnome-shell"
#: Thirty-six keys, each typed once, from a little before a switch back until well after it.
STREAM = [f"KEY_{c}" for c in "QWERTYUIOPASDFGHJKLZXCVBNM1234567890"]
STREAM_HOLD = 0.01
STREAM_GAP = 0.05
#: Seconds of the stream typed on the text console before chvt brings the session back.
STREAM_LEAD = 0.2
TRIALS = 3
CONSOLE_TEXT = "/tmp/ground/console.txt"
TYPED = "/tmp/ground/typed.json"
MONITOR_SOCKET = "/tmp/ground/keyMonitor.sock"
MONITOR_LOG = "/tmp/ground/keyMonitor.jsonl"
#: The keysyms of the Linda key, as package KM would grab them: Insert, the numpad's Insert and Caps
#: Lock.
LINDA_KEYS = [0xFF63, 0xFF9E, 0xFFE5]
ORCA = "/run/current-system/sw/bin/orca"
#: Seconds Orca is given to start far enough to ask for the name.
ORCA_START = 60


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


def keyboardAdded(ground: "Ground", desktop: str) -> None:
	with ground.subtest("E6: a keyboard added, and the desktop's own Num Lock"):
		ground.press("num_lock")
		ground.waitFor(lambda: lights(ground, VIRTIO_KEYBOARD) & NUM, "Num Lock on in the desktop")
		before = keypad(ground)
		ground.keyboard("Ground added keyboard")
		ground.settle(0.5)
		seen = {
			"keypadBefore": before,
			"lightsAfter": lights(ground, VIRTIO_KEYBOARD),
			"keypadAfter": keypad(ground),
		}
		ground.machine.log(f"E6 {desktop} a keyboard added with Num Lock on: {json.dumps(seen)}")
		assert before == ["KP_1"], seen
		if seen["keypadAfter"] == ["KP_1"]:
			ground.press("num_lock")
		ground.waitFor(lambda: not lights(ground, VIRTIO_KEYBOARD) & NUM, "Num Lock off in the desktop")
		assert keypad(ground) != ["KP_1"], "Num Lock would not go off"


def ask(ground: "Ground", op: str, **fields: object) -> dict:
	request = json.dumps({"op": op, **fields})
	answer = json.loads(ground.shell(f"ground-led-probe call {MONITOR_SOCKET} {shlex.quote(request)}"))
	assert "failed" not in answer, answer
	return answer


def keyEventsSince(ground: "Ground", since: Mark) -> list[dict]:
	return [r for r in ground.readJsonLines(MONITOR_LOG) if r["kind"] == "key" and r["t"] >= since.t]


def heard(ground: "Ground", since: Mark, settle: float = 1.0) -> list[tuple[str, int]]:
	"""The KeyEvents the plain process was sent after since, once settle seconds have passed."""
	ground.settle(settle)
	return keyEvents(keyEventsSince(ground, since))


def waitForKeyEvents(ground: "Ground", expected: list[tuple[str, int]], since: Mark) -> list[dict]:
	found: list[dict] = []

	def arrived() -> bool:
		found[:] = keyEventsSince(ground, since)
		return [(r["key"], r["value"]) for r in found][: len(expected)] == expected

	try:
		ground.waitFor(arrived, f"KeyEvents {expected}", 5)
	except AssertionError as e:
		raise AssertionError(f"{e}; the compositor sent {[(r['key'], r['value']) for r in found]}") from None
	return found


def keyMonitor(ground: "Ground", keyboard: "FakeKeyboard", client: "KeydClient", desktop: str) -> None:
	seen: dict[str, object] = {}
	ground.start("keyMonitor", ["ground-led-probe", "monitor", MONITOR_SOCKET, MONITOR_LOG])
	ground.waitFor(lambda: ground.machine.execute(f"test -S {MONITOR_SOCKET}")[0] == 0, "the key monitor")

	with ground.subtest("E12: the compositor's key monitor refuses a process that owns no name"):
		seen["before"] = ask(ground, "state")
		seen["strangerWatch"] = ask(ground, "stranger")
		ground.machine.log(
			f"E12 {desktop} before: {seen['before']}; a stranger's watch: {seen['strangerWatch']}"
		)
		assert seen["before"]["manager"]["owner"], f"nothing owns org.freedesktop.a11y.Manager: {seen}"

	with ground.subtest("E12: a plain process owns Orca's key-monitor name, watches and grabs"):
		seen["own"] = ask(ground, "own")
		seen["owned"] = ask(ground, "state")
		seen["watch"] = ask(ground, "watch")
		seen["grabs"] = ask(ground, "grabs", modifiers=LINDA_KEYS, keystrokes=[])
		seen["strangerWhileOwned"] = ask(ground, "stranger")
		ground.machine.log(
			f"E12 {desktop} owned: {seen['own']} {seen['owned']}; watch {seen['watch']}; grabs "
			f"{seen['grabs']}; a stranger meanwhile: {seen['strangerWhileOwned']}"
		)
		assert seen["owned"]["name"]["owner"] == seen["owned"]["unique"], seen["owned"]

	admitted = "error" not in seen["watch"]
	if admitted:
		with ground.subtest("E12: KeyEvents for keys from a keyboard nothing holds"):
			mark = ground.mark()
			written = keyboard.tap("KEY_B")
			events = waitForKeyEvents(ground, typedKeys("KEY_B"), mark)
			app = ground.waitForKeys("app", typedKeys("KEY_B"), mark)
			seen["unheld"] = {
				"keyEventAfterWrite": round(events[0]["t"] - written[0]["t"], 4),
				"programAfterWrite": round(app[0]["t"] - written[0]["t"], 4),
				"program": appKeyvals(app),
			}

		with ground.subtest("E12: the Linda key's grab keeps Insert with T from the program"):
			mark = ground.mark()
			keyboard.chord("KEY_INSERT", "KEY_T")
			events = heard(ground, mark)
			seen["insertT"] = {"keyEvents": events, "program": keyEvents(ground.keys("app", mark))}
			mark = ground.mark()
			keyboard.tap("KEY_INSERT", "KEY_INSERT", hold=0.03, gap=0.05)
			events = heard(ground, mark)
			seen["doubleInsert"] = {"keyEvents": events, "program": keyEvents(ground.keys("app", mark))}
			ground.machine.log(
				f"E12 {desktop} Insert with T: {seen['insertT']}; Insert twice: {seen['doubleInsert']}"
			)

		with ground.subtest("E12: KeyEvents follow keys the helper passes through the virtual keyboard"):
			client.verdicts("pass", KEY_K="keep")
			mark = ground.mark()
			client.capture(UHID, VIRTIO_KEYBOARD, since=mark)
			mark = ground.mark()
			keyboard.tap("KEY_B")
			virtual = ground.waitForKeys("evdev", typedKeys("KEY_B"), mark, device=VIRTUAL_KEYBOARD)
			events = waitForKeyEvents(ground, typedKeys("KEY_B"), mark)
			app = ground.waitForKeys("app", typedKeys("KEY_B"), mark)
			kept = ground.mark()
			keyboard.tap("KEY_K")
			client.waitFor("KEY", kept, lambda r: r["key"] == "KEY_K" and r["value"] == 0)
			seen["throughHelper"] = {
				"keyEventAfterVirtual": round(events[0]["t"] - virtual[0]["t"], 4),
				"programAfterVirtual": round(app[0]["t"] - virtual[0]["t"], 4),
				"program": appKeyvals(app),
				"keptKeyEvents": heard(ground, kept),
			}
			ground.machine.log(f"E12 {desktop} through the helper: {seen['throughHelper']}")
			assert not seen["throughHelper"]["keptKeyEvents"], seen["throughHelper"]
			client.verdicts("pass")
			mark = ground.mark()
			client.stop()
			client.waitFor("RELEASED", mark, lambda r: r["reason"] == "STOPPED")

	with ground.subtest("E12: what Orca does with the name when it starts"):
		ground.start("orca", [ORCA, "--replace"])
		ours = seen["owned"]["unique"]

		def asked() -> dict | None:
			state = ask(ground, "state")
			return (len(state["queue"]) > 1 or state["name"]["owner"] != ours) and state

		try:
			seen["orcaStarted"] = ground.waitFor(asked, "Orca to ask for the name", ORCA_START)
		except AssertionError:
			seen["orcaStarted"] = {"never": ask(ground, "state"), "orcaRunning": ground.running("orca")}
		if admitted:
			mark = ground.mark()
			keyboard.tap("KEY_B")
			seen["oursWithOrca"] = heard(ground, mark)
		seen["release"] = ask(ground, "release")
		ground.settle(2)
		seen["afterRelease"] = ask(ground, "state")
		if admitted:
			mark = ground.mark()
			keyboard.tap("KEY_B")
			seen["oursAfterRelease"] = heard(ground, mark)
		seen["watchAfterRelease"] = ask(ground, "watch")
		ground.machine.log(
			f"E12 {desktop} with Orca: {seen['orcaStarted']}; ours meanwhile {seen.get('oursWithOrca')}; "
			f"released {seen['release']}, then {seen['afterRelease']}; ours after "
			f"{seen.get('oursAfterRelease')}; a watch asked again {seen['watchAfterRelease']}"
		)
		if ground.running("orca"):
			ground.session({"op": "signal", "name": "orca", "signal": "SIGTERM"})
			ground.waitFor(lambda: not ground.running("orca"), "Orca to exit", 30)
		seen["orcaErrors"] = ground.shell("tail -c 2000 /tmp/ground/jobs/orca.err || true")
	ground.session({"op": "signal", "name": "keyMonitor", "signal": "SIGTERM"})
	ground.machine.log(f"E12 {desktop} " + json.dumps(seen, sort_keys=True))


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
	ground.machine.log(f"E15 {desktop} " + json.dumps({"session": session, "text": text, "trials": trials}))


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
	heardKeys = {r["key"] for r in client.keys(since) if r["value"] == 1}
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
				"client": key in heardKeys,
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
	facts = ground.focus("facts")
	ground.machine.log(f"the shell: {json.dumps(facts)}")
	assert facts["unsafeMode"] is False, facts
	ground.openWindow()
	ground.activate(WINDOW_TITLE)
	ground.waitForHelper()
	lightProbe(ground, "gnome")
	keyboardAdded(ground, "gnome")
	keyboard = ground.uhidKeyboard(UHID)
	client = ground.keydClient("main")
	assert client.connect()["welcome"]
	keyMonitor(ground, keyboard, client, "gnome")
	ground.activate(WINDOW_TITLE)
	switchBack(ground, keyboard, client, "gnome")
