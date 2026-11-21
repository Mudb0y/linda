"""The experiments that turn on KDE Plasma, with linda-keyd's own test client and no Linda:

- E6, what KWin writes to a new keyboard's lock lights, by guest/ledProbe.py's experiment, which
  decides whether package HV's light probe can prove KWin reads the virtual keyboard;
- E12, a plain process that owns org.gnome.Orca.KeyboardMonitor and calls KWin's
  org.freedesktop.a11y.KeyboardMonitor: whether it may watch and grab, whether its KeyEvents follow
  keys the helper passes through the virtual keyboard, and what Orca does with the name when it
  starts, which decides whether package KM can be built.

Each experiment's figures go to the log as one JSON object, after "E6 plasma" and "E12 plasma". A
case fails only when an experiment could not be made; what it found is written down.
"""

import itertools
import json
import shlex
from typing import TYPE_CHECKING

from ground import VIRTIO_KEYBOARD, VIRTUAL_KEYBOARD, WINDOW_TITLE, Mark, keyEvents
from wl.helperSafety import appKeyvals, lights, typedKeys

if TYPE_CHECKING:
	from ground import FakeKeyboard, Ground, KeydClient

UHID = "Ground uhid keyboard"
#: KWin's own process, by its comm, which Nix's wrapper makes .kwin_wayland-w.
READER = "kwin_wayland"
MONITOR_SOCKET = "/tmp/ground/keyMonitor.sock"
MONITOR_LOG = "/tmp/ground/keyMonitor.jsonl"
#: The keysyms of the Linda key, as package KM would grab them: Insert, the numpad's Insert and Caps
#: Lock.
LINDA_KEYS = [0xFF63, 0xFF9E, 0xFFE5]
#: Seconds within which a KeyEvent the same as the one before is the same key event sent again.
TWICE = 0.05
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


def ask(ground: "Ground", op: str, **fields: object) -> dict:
	request = json.dumps({"op": op, **fields})
	answer = json.loads(ground.shell(f"ground-led-probe call {MONITOR_SOCKET} {shlex.quote(request)}"))
	assert "failed" not in answer, answer
	return answer


def keyEventsSince(ground: "Ground", since: Mark) -> list[dict]:
	return [r for r in ground.readJsonLines(MONITOR_LOG) if r["kind"] == "key" and r["t"] >= since.t]


def folded(records: list[dict]) -> list[dict]:
	"""KeyEvents with each one the compositor sent twice running counted once: mutter 51 sends a
	watcher every key event twice, though it keeps one grabber for each connection, and a compositor
	that sends it once loses nothing by this."""
	kept: list[dict] = []
	for r in records:
		if (
			kept
			and (kept[-1]["key"], kept[-1]["value"]) == (r["key"], r["value"])
			and r["t"] - kept[-1]["t"] < TWICE
		):
			continue
		kept.append(r)
	return kept


def heard(ground: "Ground", since: Mark, settle: float = 1.0) -> list[tuple[str, int]]:
	"""The KeyEvents the plain process was sent after since, once settle seconds have passed."""
	ground.settle(settle)
	return keyEvents(folded(keyEventsSince(ground, since)))


def sentTwice(records: list[dict]) -> list[float]:
	"""For each KeyEvent sent a second time running, the seconds between the two."""
	return [
		round(b["t"] - a["t"], 4)
		for a, b in itertools.pairwise(records)
		if (a["key"], a["value"]) == (b["key"], b["value"])
	]


def waitForKeyEvents(ground: "Ground", expected: list[tuple[str, int]], since: Mark) -> list[dict]:
	found: list[dict] = []

	def arrived() -> bool:
		found[:] = keyEventsSince(ground, since)
		return [(r["key"], r["value"]) for r in folded(found)][: len(expected)] == expected

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
				"sent": [(r["key"], r["value"]) for r in events],
				"sentTwiceApart": sentTwice(events),
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
				"sentTwiceApart": sentTwice(events),
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
		# Yielded as package KM would yield: its grabs dropped first, while it may still drop them.
		seen["grabsDropped"] = ask(ground, "grabs", modifiers=[], keystrokes=[])
		seen["release"] = ask(ground, "release")
		ground.settle(2)
		seen["afterRelease"] = ask(ground, "state")
		if admitted:
			mark = ground.mark()
			keyboard.tap("KEY_B")
			seen["oursAfterRelease"] = heard(ground, mark)
			# Orca asked for its grabs while it waited for the name; whether it holds any once it has it.
			mark = ground.mark()
			keyboard.chord("KEY_INSERT", "KEY_T")
			ground.settle(1)
			seen["insertTWithOrcaOwning"] = keyEvents(ground.keys("app", mark))
		seen["watchAfterRelease"] = ask(ground, "watch")
		# Asked back with replacement, from Orca, which asked for the name allowing none.
		seen["ownAgain"] = ask(ground, "own", flags=3)
		seen["afterOwnAgain"] = ask(ground, "state")
		ground.machine.log(
			f"E12 {desktop} with Orca: {seen['orcaStarted']}; ours meanwhile {seen.get('oursWithOrca')}; "
			f"released {seen['release']}, then {seen['afterRelease']}; ours after "
			f"{seen.get('oursAfterRelease')}; Insert with T at the program {seen.get('insertTWithOrcaOwning')}; "
			f"a watch asked again {seen['watchAfterRelease']}; the name asked back {seen['ownAgain']}, then "
			f"{seen['afterOwnAgain']}"
		)
		if ground.running("orca"):
			ground.session({"op": "signal", "name": "orca", "signal": "SIGTERM"})
			ground.waitFor(lambda: not ground.running("orca"), "Orca to exit", 30)
		seen["orcaErrors"] = ground.shell("tail -c 2000 /tmp/ground/jobs/orca.err || true")
	ground.session({"op": "signal", "name": "keyMonitor", "signal": "SIGTERM"})
	ground.machine.log(f"E12 {desktop} " + json.dumps(seen, sort_keys=True))


def run(ground: "Ground") -> None:
	ground.machine.log(f"the desktop: {json.dumps(ground.focus('facts'))}")
	ground.openWindow()
	ground.activate(WINDOW_TITLE)
	ground.waitForHelper()
	lightProbe(ground, "plasma")
	keyboardAdded(ground, "plasma")
	keyboard = ground.uhidKeyboard(UHID)
	client = ground.keydClient("main")
	assert client.connect()["welcome"]
	keyMonitor(ground, keyboard, client, "plasma")
