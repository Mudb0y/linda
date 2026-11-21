"""Linda's acceptance in a Wayland guest with the real helper: what the cases wlGnomeLinda and
wlGnomeOrca check of a Linda started in the session as a user starts it, with linda-keyd holding a
uhid keyboard, and the experiments that ride on the same boot (E4, E5, E8 and E11).

A Wayland guest has no layer that sees every key. That a key Linda keeps reaches nobody is shown by
three records all lacking it: the test window's, the evdev recorder's of "Linda virtual keyboard",
through which everything Linda passes goes out, and the evdev recorder's of the uhid keyboard itself,
which no reader has while the helper grabs it. Lights are the kernel's, as the uhid keyboard records
what it is told to light.

The file has two halves. On the host the cases use what is defined here. In the guest, as
ground-linda-acceptance, which cases/wlGnomeLinda.nix installs with the paths it needs in the
environment:

	lindaAcceptance.py xsockets PID
	lindaAcceptance.py pointer-keyboard NAME
	lindaAcceptance.py layouts
	lindaAcceptance.py listen OUT

xsockets runs as root and prints, as JSON, the Unix sockets of process PID and its descendants and
those of them whose peer is a socket an X server listens on (a name under /tmp/.X11-unix, abstract or
not), with the processes that do hold X connections counted by name for the log. pointer-keyboard
runs as root and makes, through uhid, a keyboard whose one HID application also carries relative
axes and buttons, as a keyboard with a pointing stick has, which the helper leaves alone as a
pointer; it is then served as guest/fakeKeyboard.py serves its own, so Ground's FakeKeyboard drives
it. layouts runs in the session and prints, as JSON, what Linda's own GNOME layout feed reads from
the desktop's settings, the keymap those names compile to, and the keymap the compositor gives its
clients, by tests/linux/waylandKeymapDump.py, with every key whose symbols differ. listen runs in the
session and writes each window:activate and window:deactivate it hears on a connection of its own to
OUT, one JSON object a line, stamped as it was handed the event, on the guest's CLOCK_MONOTONIC.
"""

import importlib.util
import json
import os
import re
import shlex
import statistics
import subprocess
import sys
import time
import types
from types import ModuleType
from typing import TYPE_CHECKING

if __name__ != "__main__":
	from ground import (
		PREFIX_MARK,
		VIRTUAL_KEYBOARD,
		WINDOW_TITLE,
		FakeKeyboard,
		Mark,
		unitName,
	)

if TYPE_CHECKING:
	from ground import Ground

TOOL = "ground-linda-acceptance"
UHID = "Ground uhid keyboard"
CHORD_KEYS = ("KEY_INSERT", "KEY_T")
#: Longer than NVDA's double press, after which a second chord is a new one.
APART = 0.8
#: Seconds to listen for speech that must come once and not twice.
ONCE = 2.0
#: Typed into a program and looked for in every log: nothing typed is ever written to one.
MARKER = "kqzmxjwv"

#: What Linda says, as the synthesizer is given it: the source's NVDA is spoken as Linda. A text ending
#: in PREFIX_MARK need only start a speak record.
NO_HELPER = "Linda cannot take keys in this Wayland session until linda-keyd runs\N{HORIZONTAL ELLIPSIS}"
HAS_KEYBOARD = "Linda has the keyboard"
HINT = "If typing stops reaching programs, hold Backspace, Escape and Enter together."
LOST = "Linda has lost the keyboard and takes it again as soon as linda-keyd lets it."
LET_GO = "Linda has let go of the keyboard and has no keys now.\N{HORIZONTAL ELLIPSIS}"
UNHEARD = "Linda cannot hear one of the keyboards, so its keys reach programs without Linda."
#: What Linda logs on the Wayland route with no helper holding the keyboard.
WITHOUT_KEYS = "NVDA has no keys in this Wayland session"
LAYOUT_LOG = "Keyboard layout from gnome: "
#: Where the linda that Alt+F2 starts writes its process number, in ground's LINDA_DIRECTORY, as
#: cases/wlGnomeLinda.nix has it.
STARTED_FROM_PATH = "/tmp/ground/linda/startedFromPath.pid"
#: The kernel's light bit of Caps Lock, as a uhid keyboard records its lights.
CAPS_LIGHT = 1 << 1
#: An NVDA log record at ERROR or above, as tests/linux/harness.py finds them.
ERROR_RECORD = re.compile(r"^(?:ERROR|CRITICAL) - .*$", re.MULTILINE)
#: What listen writes, in ground's GROUND.
WINDOW_EVENTS = "/tmp/ground/windowEvents.jsonl"


def matches(expected: str, spoken: str) -> bool:
	if expected.endswith(PREFIX_MARK):
		return spoken.startswith(expected[: -len(PREFIX_MARK)])
	return spoken == expected


def said(ground: "Ground", since: "Mark") -> list[str]:
	return [text for _t, text in ground.speech(since)]


def saidCount(ground: "Ground", since: "Mark", expected: str) -> int:
	return sum(matches(expected, text) for text in said(ground, since))


def saidOnce(ground: "Ground", since: "Mark", expected: str, timeout: float = 10) -> float:
	"""Wait until Linda says expected, then for ONCE more seconds, and fail unless it said it once;
	answers when it was said."""
	at = ground.waitForSpeech(expected, since, timeout)
	ground.settle(ONCE)
	count = saidCount(ground, since, expected)
	assert count == 1, f"Linda said {expected!r} {count} times: {said(ground, since)}"
	return at


def assertKept(ground: "Ground", since: "Mark", keys: tuple[str, ...], keyboard: str = UHID) -> None:
	"""None of keys reached the test windows, went out through the virtual keyboard, or came to a
	reader of the keyboard they were typed on."""
	for source, device in (
		("app", None),
		("app4", None),
		("evdev", VIRTUAL_KEYBOARD),
		("evdev", keyboard),
	):
		reached = [
			(r["key"], r["value"]) for r in ground.keys(source, since, device=device) if r["key"] in keys
		]
		assert not reached, f"{source} {device or ''} had {reached}"


def lindaKeyWorks(ground: "Ground", keyboard: "FakeKeyboard", nvdaKey: str = "KEY_INSERT") -> "Mark":
	"""The Linda key with T reads the window's title, and neither key reaches anything."""
	ground.settle(APART)
	mark = ground.mark()
	keyboard.chord(nvdaKey, "KEY_T")
	ground.waitForSpeech(WINDOW_TITLE, mark)
	ground.settle(0.5)
	assertKept(ground, mark, (nvdaKey, "KEY_T"), keyboard.name)
	return mark


def typedThrough(
	ground: "Ground",
	keyboard: "FakeKeyboard",
	key: str,
	keyval: str,
	*,
	through: str | None = None,
	echo: str | None = None,
	log: str = "app",
) -> list[dict]:
	"""A key typed reaches the program as keyval by way of the device through, the virtual keyboard by
	default, and with echo Linda says it."""
	through = through or VIRTUAL_KEYBOARD
	mark = ground.mark()
	keyboard.tap(key)
	expected = [(key, 1), (key, 0)]
	ground.waitForKeys("evdev", expected, mark, device=through)
	app = ground.waitForKeys(log, expected, mark)
	assert app[0]["keyval"] == keyval, app
	if echo is not None:
		ground.waitForSpeech(echo, mark)
	return app


def lindaPid(ground: "Ground") -> int:
	"""The process the session runner started as Linda."""
	return ground.session({"op": "status", "name": "linda"})["pid"]


def processRunning(ground: "Ground", pid: int) -> bool:
	return ground.machine.execute(f"test -d /proc/{pid} && ! grep -q '^State:.*Z' /proc/{pid}/status")[0] == 0


def xSockets(ground: "Ground", pid: int) -> dict:
	return json.loads(ground.shell(f"{TOOL} xsockets {pid}"))


def assertNoXConnection(ground: "Ground", pid: int, when: str) -> dict:
	"""Experiment E8: Linda's processes hold no connection to an X socket, though Xwayland runs."""
	found = xSockets(ground, pid)
	ground.machine.log(
		f"E8 {when}: Linda's processes {found['pids']} hold {found['sockets']} Unix sockets, "
		f"{len(found['toX'])} to an X server; the X server's clients by name: {found['xClients']}"
	)
	assert found["sockets"], found
	assert not found["toX"], found
	return found


def gsettings(ground: "Ground", *arguments: str) -> str:
	answer = ground.run(["gsettings", *arguments])
	assert answer.returncode == 0, answer
	return answer.stdout.strip()


def layoutChanges(ground: "Ground", layout: str) -> int:
	return len(ground.lindaLogRecords(f"{LAYOUT_LOG}{layout}\n"))


def gestureName(ground: "Ground", keyboard: "FakeKeyboard", *chord: str) -> list[str]:
	"""What input help says for chord, which reaches no program."""
	ground.settle(APART)
	mark = ground.mark()
	keyboard.chord("KEY_INSERT", "KEY_1")
	ground.waitForSpeech("input help on\N{HORIZONTAL ELLIPSIS}", mark)
	ground.settle(APART)
	mark = ground.mark()
	keyboard.chord(*chord)
	ground.waitFor(lambda: ground.speech(mark), f"input help to name {chord}")
	ground.settle(0.5)
	name = said(ground, mark)
	assertKept(ground, mark, chord)
	ground.settle(APART)
	off = ground.mark()
	keyboard.chord("KEY_INSERT", "KEY_1")
	ground.waitForSpeech("input help off", off)
	return name


def layouts(ground: "Ground") -> dict:
	"""Experiment E5's two keymaps, as the guest half's layouts compares them."""
	answer = ground.run([TOOL, "layouts"], timeout=60)
	assert answer.returncode == 0, answer
	return json.loads(answer.stdout)


def logLayouts(ground: "Ground", when: str) -> dict:
	found = layouts(ground)
	ground.machine.log(
		f"E5 {when}: the feed read {found['feed']}; the compositor's layouts {found['compositorLayouts']}; "
		f"{found['compared']} keys compared, {len(found['differences'])} differ: {found['differences'][:20]}"
	)
	return found


def pointerKeyboard(ground: "Ground", name: str) -> "FakeKeyboard":
	"""A uhid keyboard that is also a pointer, which the helper leaves alone."""
	ground.machine.succeed(
		f"systemd-run --collect --unit={unitName('ground-pointer-', name)} {TOOL} pointer-keyboard "
		+ shlex.quote(name)
	)
	return FakeKeyboard(ground, name, started=True)


def logErrors(text: str) -> list[str]:
	return ERROR_RECORD.findall(text)


def _letters(text: str) -> str:
	return "".join(c for c in text.casefold() if c.isalnum())


def _keyPerLine(text: str) -> str:
	"""The last one-character word of each line, a run of one taken once, as tests/linux/harness.py's
	marker check reads a log that wrote each key on a line of its own."""
	keys = []
	for line in text.splitlines():
		if re.match(r"^[A-Z_]+ - .+? \(\d\d:\d\d:\d\d\.\d{3}\) - .*:$", line):
			continue
		words = re.findall(r"[^\W_]+", line.casefold())
		if words and len(words[-1]) == 1:
			keys.append(words[-1])
	joined = "".join(keys)
	return "".join(c for i, c in enumerate(joined) if i == 0 or joined[i - 1] != c)


def holdingMarker(texts: dict[str, str]) -> list[str]:
	"""The names of the texts that hold MARKER, matched the three ways tests/linux/harness.py's
	textsHoldingMarker matches it, which the cases cannot import."""
	return [
		name
		for name, text in texts.items()
		if MARKER in text.casefold() or MARKER in _letters(text) or MARKER in _keyPerLine(text)
	]


#: How speech.speak writes what it says at IO level, which --debug-logging takes in: a field's text
#: is in it whenever the field is read aloud, whatever was typed into it and however.
SPOKEN_LINE = "Speaking ["


def withoutSpokenLines(text: str) -> str:
	return "\n".join(line for line in text.splitlines() if not line.startswith(SPOKEN_LINE))


def logSize(ground: "Ground", path: str) -> int:
	return int(ground.shell(f"stat -c %s {shlex.quote(path)} 2>/dev/null || echo 0").strip())


def logSince(ground: "Ground", path: str, offset: int) -> str:
	return ground.shell(f"tail -c +{offset + 1} {shlex.quote(path)} 2>/dev/null || true")


def typedMarker(ground: "Ground", keyboard: "FakeKeyboard", log: str) -> None:
	"""Type MARKER into the program, and fail if the log at path log, from just before it was typed to a
	second after, holds it: the window in which the keys go through Linda, before anything reads the
	field they went into back aloud."""
	offset = logSize(ground, log)
	mark = ground.mark()
	keys = [f"KEY_{c.upper()}" for c in MARKER]
	keyboard.tap(*keys)
	ground.waitForKeys("app", [(key, value) for key in keys for value in (1, 0)], mark)
	ground.settle(1)
	holding = holdingMarker({"Linda's log as the marker was typed": logSince(ground, log, offset)})
	assert not holding, f"what the test typed is written in {', '.join(holding)}"
	holding = holdingMarker({"linda-keyd's journal": "\n".join(m for _t, m in ground.helperLog(mark))})
	assert not holding, f"what the test typed is written in {', '.join(holding)}"


def linesHoldingMarker(text: str) -> list[str]:
	return [line for line in text.splitlines() if MARKER in _letters(line)]


# Experiment E4: window events against the keys that cause them.


def windowEvents(ground: "Ground", since: float) -> list[dict]:
	return [r for r in ground.readJsonLines(WINDOW_EVENTS) if r.get("t", 0) >= since]


def startListening(ground: "Ground") -> None:
	ground.shell(f"rm -f {WINDOW_EVENTS}")
	ground.start("windowEvents", [TOOL, "listen", WINDOW_EVENTS])
	ground.waitFor(
		lambda: any(r.get("kind") == "ready" for r in ground.readJsonLines(WINDOW_EVENTS)),
		"the window event listener",
		30,
	)


def eventAfter(
	ground: "Ground", since: float, type: str, title: str | None, timeout: float = 5
) -> dict | None:
	"""The first window event of type after since, from a window whose title starts with title, or from
	any program's window but the shell's with title None; None when none comes within timeout."""
	deadline = time.monotonic() + timeout
	while True:
		for event in windowEvents(ground, since):
			if event.get("type") != type:
				continue
			if title is None and event.get("application") != "gnome-shell":
				return event
			if title is not None and event.get("title", "").startswith(title):
				return event
		if time.monotonic() >= deadline:
			return None
		time.sleep(0.1)


def written(records: list[dict], key: str, value: int) -> float:
	return next(r["t"] for r in records if r["key"] == key and r["value"] == value)


def summary(delays: list[float | None]) -> dict:
	heard = sorted(d for d in delays if d is not None)
	if not heard:
		return {"count": 0, "unheard": len(delays)}
	return {
		"count": len(heard),
		"unheard": len(delays) - len(heard),
		"median": round(statistics.median(heard), 3),
		"most": round(heard[-1], 3),
		"least": round(heard[0], 3),
	}


def activeTitle(ground: "Ground") -> str | None:
	window = ground.activeWindow()
	return window["title"] if window and not window.get("shell") else None


def _after(event: dict | None, t: float) -> float | None:
	return None if event is None else round(event["t"] - t, 3)


def altTab(ground: "Ground", keyboard: "FakeKeyboard", key: str = "KEY_TAB") -> dict:
	"""Alt with key, the switcher held up a moment: the old window's deactivate timed from key's press,
	when the switcher takes the keyboard, and the new one's activate from Alt's release, when it
	closes."""
	before = activeTitle(ground)
	steps = [
		{"press": "KEY_LEFTALT"},
		{"sleep": 0.05},
		{"tap": key, "hold": 0.03},
		{"sleep": 0.4},
		{"release": "KEY_LEFTALT"},
	]
	keys = keyboard.run(*steps)
	start, pressed, up = keys[0]["t"], written(keys, key, 1), written(keys, "KEY_LEFTALT", 0)
	gone = eventAfter(ground, start, "window:deactivate", before)
	came = eventAfter(ground, up, "window:activate", None)
	ground.settle(1.0)
	after = activeTitle(ground)
	events = [e for e in windowEvents(ground, start) if "type" in e]
	assert after is not None and after != before, (before, after, events)
	return {
		"from": before,
		"to": after,
		"deactivate": _after(gone, pressed),
		"activate": _after(came, up),
		"events": [[round(e["t"] - start, 3), e["type"], e["application"], e["title"]] for e in events],
	}


def awayAndBack(ground: "Ground", keyboard: "FakeKeyboard", away: list[dict], back: list[dict]) -> dict:
	"""A shell surface that takes the keyboard, opened by away's keys and closed by back's: the window's
	deactivate timed from away's last key event, and its activate from back's."""
	window = activeTitle(ground)
	opened = keyboard.run(*away)
	gone = eventAfter(ground, opened[0]["t"], "window:deactivate", window)
	ground.settle(1.0)
	closed = keyboard.run(*back)
	came = eventAfter(ground, closed[0]["t"], "window:activate", window)
	ground.settle(1.0)
	assert activeTitle(ground) == window, (window, activeTitle(ground))
	return {
		"window": window,
		"deactivate": _after(gone, opened[-1]["t"]),
		"activate": _after(came, closed[-1]["t"]),
	}


def overview(ground: "Ground", keyboard: "FakeKeyboard") -> dict:
	tap = [{"tap": "KEY_LEFTMETA", "hold": 0.03}]
	return awayAndBack(ground, keyboard, tap, tap)


def runDialog(ground: "Ground", keyboard: "FakeKeyboard") -> dict:
	return awayAndBack(
		ground,
		keyboard,
		[{"press": "KEY_LEFTALT"}, {"sleep": 0.03}, {"press": "KEY_F2"}],
		[{"release": "KEY_F2"}, {"release": "KEY_LEFTALT"}, {"sleep": 0.5}, {"tap": "KEY_ESC", "hold": 0.03}],
	)


GTK4_TITLE = "Ground window 4"
PAGE = "/tmp/ground/page.html"
PAGE_TITLE = "Ground page"
PROFILE = "/tmp/ground/firefox"
#: Alternations timed of each kind.
SWITCHES = 6
#: A fresh profile's preferences, as cases/lindaBrowseKeys.py gives Firefox: no first-run pages, no
#: updates, no network.
FIREFOX_PREFERENCES = {
	"browser.shell.checkDefaultBrowser": False,
	"browser.startup.homepage_override.mstone": "ignore",
	"browser.aboutwelcome.enabled": False,
	"trailhead.firstrun.didSeeAboutWelcome": True,
	"datareporting.policy.dataSubmissionEnabled": False,
	"toolkit.telemetry.reportingpolicy.firstRun": False,
	"app.update.auto": False,
	"app.normandy.enabled": False,
	"network.dns.disabled": True,
	"browser.sessionstore.resume_from_crash": False,
}


def startFirefox(ground: "Ground") -> str:
	"""Firefox, native on Wayland, with a page long enough for say all to run a while; answers its
	window's title once it is active."""
	paragraphs = "".join(
		f"<p>Paragraph {n} of the ground page, read aloud from the top.</p>" for n in range(300)
	)
	page = f'<!doctype html><html lang="en"><head><meta charset="utf-8"><title>{PAGE_TITLE}</title></head><body>{paragraphs}</body></html>'
	prefs = "\n".join(f"user_pref({json.dumps(k)}, {json.dumps(v)});" for k, v in FIREFOX_PREFERENCES.items())
	ground.shell(
		f"install -d -o alice {PROFILE} && echo {shlex.quote(prefs)} > {PROFILE}/user.js && "
		f"echo {shlex.quote(page)} > {PAGE} && chown alice {PROFILE}/user.js {PAGE}"
	)
	ground.start(
		"firefox",
		["firefox", "--new-instance", "--profile", PROFILE, f"file://{PAGE}"],
		env={"MOZ_ENABLE_WAYLAND": "1"},
	)
	return ground.waitFor(
		lambda: (title := activeTitle(ground)) and title.startswith(PAGE_TITLE) and title,
		"Firefox's window to be active",
		180,
	)


def sayAllThenAltTab(ground: "Ground", keyboard: "FakeKeyboard", firefox: str) -> dict:
	if activeTitle(ground) != firefox:
		ground.activate(firefox)
	ground.settle(APART)
	mark = ground.mark()
	keyboard.chord("KEY_INSERT", "KEY_DOWN")
	ground.waitFor(lambda: len(ground.speech(mark)) >= 3, "say all to be under way", 20)
	switch = altTab(ground, keyboard)
	switch["spokenBeforeTheSwitch"] = len(ground.speech(mark))
	return switch


def twoWindows(ground: "Ground", keyboard: "FakeKeyboard", firefox: str) -> list[dict]:
	"""Two windows of one program, switched between with Alt and the key above Tab."""
	if activeTitle(ground) != firefox:
		ground.activate(firefox)
	keyboard.chord("KEY_LEFTCTRL", "KEY_N")
	ground.waitFor(lambda: (title := activeTitle(ground)) and title != firefox, "Firefox's second window", 30)
	switches = [altTab(ground, keyboard, "KEY_GRAVE") for _ in range(4)]
	second = next(s["from"] for s in switches if s["from"] != firefox)
	if activeTitle(ground) != second:
		altTab(ground, keyboard, "KEY_GRAVE")
	keyboard.chord("KEY_LEFTCTRL", "KEY_LEFTSHIFT", "KEY_W")
	ground.waitFor(lambda: activeTitle(ground) == firefox, "the second window to close", 15)
	return switches


def _kinds(figures: dict) -> dict:
	return {
		kind: {
			"deactivate": summary([s["deactivate"] for s in samples]),
			"activate": summary([s["activate"] for s in samples]),
		}
		for kind, samples in figures.items()
	}


def windowTimings(ground: "Ground", keyboard: "FakeKeyboard") -> dict:
	"""Experiment E4 on the guest, with Linda running and holding the keyboard: when each toolkit and
	the shell say a window was activated and deactivated, against the key that did it, heard on a
	connection of the experiment's own."""
	figures: dict[str, list[dict]] = {}
	startListening(ground)
	ground.openWindow(GTK4_TITLE, name="window4", gtk4=True)
	with ground.subtest("E4: Alt+Tab between a GTK 3 and a GTK 4 window, Linda idle"):
		ground.activate(WINDOW_TITLE)
		figures["GTK Alt+Tab"] = [altTab(ground, keyboard) for _ in range(SWITCHES)]
	with ground.subtest("E4: the overview and the run dialog over a GTK 3 window"):
		figures["GTK 3 overview"] = [overview(ground, keyboard) for _ in range(3)]
		figures["GTK 3 run dialog"] = [runDialog(ground, keyboard) for _ in range(3)]
	with ground.subtest("E4: Firefox against Alt+Tab, the overview and the run dialog, idle and in say all"):
		firefox = startFirefox(ground)
		ground.settle(3)
		figures["Firefox Alt+Tab"] = [altTab(ground, keyboard) for _ in range(SWITCHES)]
		ground.activate(firefox)
		figures["Firefox overview"] = [overview(ground, keyboard) for _ in range(3)]
		figures["Firefox run dialog"] = [runDialog(ground, keyboard) for _ in range(3)]
		figures["Alt+Tab from Firefox in say all"] = [
			sayAllThenAltTab(ground, keyboard, firefox) for _ in range(3)
		]
		figures["two Firefox windows"] = twoWindows(ground, keyboard, firefox)
	for job in ("firefox", "window4", "windowEvents"):
		ground.session({"op": "signal", "name": job, "signal": "SIGTERM"})
		ground.waitFor(lambda job=job: not ground.running(job), f"{job} to end", 30)
	return {"summary": _kinds(figures), "samples": figures}


# The guest half.


def _load(name: str, path: str) -> ModuleType:
	spec = importlib.util.spec_from_file_location(name, path)
	module = importlib.util.module_from_spec(spec)
	sys.modules[name] = module
	spec.loader.exec_module(module)
	return module


def _descendants(root: int) -> list[int]:
	parents: dict[int, int] = {}
	for entry in os.listdir("/proc"):
		if not entry.isdigit():
			continue
		try:
			with open(f"/proc/{entry}/stat") as f:
				# The command name, in parentheses, may itself hold spaces and parentheses.
				parents[int(entry)] = int(f.read().rsplit(")", 1)[1].split()[1])
		except (OSError, IndexError, ValueError):
			continue
	found = [root]
	for pid in found:
		found += [child for child, parent in parents.items() if parent == pid and child not in found]
	return found


def _socketInodes(pid: int) -> set[int]:
	inodes = set()
	try:
		fds = os.listdir(f"/proc/{pid}/fd")
	except OSError:
		return inodes
	for fd in fds:
		try:
			target = os.readlink(f"/proc/{pid}/fd/{fd}")
		except OSError:
			continue
		if target.startswith("socket:["):
			inodes.add(int(target[8:-1]))
	return inodes


def _unixSockets() -> list[dict]:
	"""Every Unix socket as ss lists it: its address, its inode, its peer's inode and its holders."""
	text = subprocess.run(
		["ss", "-x", "-a", "-n", "-p", "-H"], capture_output=True, text=True, check=True
	).stdout
	sockets = []
	for line in text.splitlines():
		fields = line.split()
		if len(fields) < 8:
			continue
		try:
			inode, peer = int(fields[5]), int(fields[7])
		except ValueError:
			continue
		holders = re.findall(r'\("([^"]*)",pid=(\d+)', " ".join(fields[8:]))
		sockets.append(
			{
				"address": fields[4],
				"inode": inode,
				"peer": peer,
				"holders": [(name, int(pid)) for name, pid in holders],
			}
		)
	return sockets


def xsockets(root: int) -> dict:
	pids = _descendants(root)
	mine = {inode: pid for pid in pids for inode in _socketInodes(pid)}
	sockets = _unixSockets()
	address = {s["inode"]: s["address"] for s in sockets}
	toX = []
	for s in sockets:
		if s["inode"] in mine and ".X11-unix" in address.get(s["peer"], "") + s["address"]:
			toX.append({"pid": mine[s["inode"]], "inode": s["inode"], "peer": address.get(s["peer"])})
	byInode = {s["inode"]: s for s in sockets}
	xClients: dict[str, int] = {}
	for s in sockets:
		if ".X11-unix" in s["address"] and s["peer"] in byInode:
			for name, _pid in byInode[s["peer"]]["holders"]:
				xClients[name] = xClients.get(name, 0) + 1
	return {"pids": pids, "sockets": len(mine), "toX": toX, "xClients": xClients}


#: The HID boot keyboard of guest/fakeKeyboard.py, its application collection left open, then three
#: buttons and relative X and Y in a physical pointer collection, and the application closed.
POINTER_KEYBOARD_DESCRIPTOR = bytes.fromhex(
	"05010906a101050719e029e71500250175019508810295017508810195057501"
	"050819012905910295017503910195067508150025650507190029658100"
	"05010901a100"
	"050919012903150025017501950381027505950181010501093009311581257f750895028106"
	"c0c0"
)


def servePointerKeyboard(name: str) -> None:
	fake = _load("fakeKeyboard", os.environ["GROUND_FAKE_KEYBOARD"])
	fake.BOOT_KEYBOARD = POINTER_KEYBOARD_DESCRIPTOR

	class PointerKeyboard(fake.UhidKeyboard):
		def send(self, code: int, value: int) -> None:
			if code in fake.HID_MODIFIERS:
				bit = 1 << fake.HID_MODIFIERS.index(code)
				self.modifiers = self.modifiers | bit if value else self.modifiers & ~bit
			elif value and code not in self.down:
				self.down.append(code)
			elif not value and code in self.down:
				self.down.remove(code)
			usages = [fake.USAGES[c] for c in self.down] + [0] * (6 - len(self.down))
			# The keys, then no button and no movement.
			report = bytes([self.modifiers, 0, *usages, 0, 0, 0])
			os.write(self.fd, fake.struct.pack("<IH", fake.UHID_INPUT2, len(report)) + report)

	os.makedirs(fake.DIRECTORY, exist_ok=True)
	fake.serve(PointerKeyboard(name))


def _feedSnapshot(source: str):
	"""What Linda's GNOME feed reads, run from the package's own source with its log put aside."""

	class Log:
		def __getattr__(self, _name):
			return lambda *_args, **_kwargs: None

	import builtins

	builtins._ = lambda text: text
	sys.modules["logHandler"] = types.SimpleNamespace(log=Log())
	for package, path in (
		("linuxPlatform", f"{source}/linuxPlatform"),
		("linuxPlatform.keys", f"{source}/linuxPlatform/keys"),
	):
		module = types.ModuleType(package)
		module.__path__ = [path]
		sys.modules[package] = module
	desktopLayout = importlib.import_module("linuxPlatform.keys.desktopLayout")
	feed = desktopLayout.GnomeLayoutFeed()
	try:
		return feed.start(lambda _snapshot: None)
	finally:
		feed.stop()


def _rows(lines: list[str]) -> tuple[list[str], dict[tuple[int, int], list[str]]]:
	names: list[str] = []
	rows = {}
	for line in lines:
		fields = line.split("\t")
		if fields[0] == "layouts":
			names = fields[1:]
		else:
			rows[(int(fields[0]), int(fields[1]))] = fields[2:]
	return names, rows


def layoutsNow() -> dict:
	from xkbcommon import xkb

	source = os.environ["GROUND_LINDA_SOURCE"]
	dump = _load("waylandKeymapDump", os.environ["GROUND_KEYMAP_DUMP"])
	codes = dump.namedCodes(f"{source}/linuxPlatform/keys/keyMap.py")
	snapshot = _feedSnapshot(source)
	rules, model, layout, variant, options = snapshot.names
	keymap = xkb.Context().keymap_new_from_names(
		rules=rules or None,
		model=model or None,
		layout=layout or None,
		variant=variant or None,
		options=options or None,
	)
	feedNames, feedRows = _rows(dump.describe(keymap.get_as_string(), codes))
	text = dump.readKeymap(5)
	compositorNames, compositorRows = _rows(dump.describe(text, codes)) if text else ([], {})
	# The feed compiles the active source alone, as group 0: it is held to the compositor's layout of
	# the same name, whichever group the compositor gives it.
	group = compositorNames.index(feedNames[0]) if feedNames and feedNames[0] in compositorNames else None
	differences = []
	compared = 0
	if group is not None:
		layoutsOf: dict[int, int] = {}
		for code, layout in compositorRows:
			layoutsOf[code] = max(layoutsOf.get(code, 0), layout + 1)
		for (code, _layout), levels in sorted(feedRows.items()):
			# A key with fewer layouts than the keymap has takes the group wrapped, as xkbcommon's
			# default out-of-range rule does.
			theirs = compositorRows.get((code, group % layoutsOf[code])) if code in layoutsOf else None
			compared += 1
			if theirs != levels:
				differences.append([code, levels, theirs])
	return {
		"feed": {
			"names": list(snapshot.names),
			"group": snapshot.group,
			"source": snapshot.source,
			"inputMethod": snapshot.inputMethod,
			"layoutName": feedNames[0] if feedNames else None,
		},
		"compositorLayouts": compositorNames,
		"compositorGroup": group,
		"compared": compared,
		"differences": differences,
	}


def listen(out: str) -> None:
	import gi

	gi.require_version("Atspi", "2.0")
	from gi.repository import Atspi, GLib

	stream = open(out, "a", buffering=1)  # noqa: SIM115

	def write(record: dict) -> None:
		stream.write(json.dumps(record) + "\n")

	def onEvent(event: Atspi.Event) -> None:
		t = time.monotonic()
		try:
			title = event.source.get_name()
			application = event.source.get_application().get_name()
		except GLib.Error:
			title, application = "?", "?"
		write({"t": t, "type": event.type, "title": title, "application": application})

	Atspi.init()
	listener = Atspi.EventListener.new(onEvent)
	for name in ("window:activate", "window:deactivate"):
		listener.register(name)
	write({"kind": "ready", "t": time.monotonic()})
	Atspi.event_main()


if __name__ == "__main__":
	command = sys.argv[1:2]
	if command == ["xsockets"] and len(sys.argv) == 3:
		print(json.dumps(xsockets(int(sys.argv[2]))))
	elif command == ["pointer-keyboard"] and len(sys.argv) == 3:
		servePointerKeyboard(sys.argv[2])
	elif command == ["layouts"] and len(sys.argv) == 2:
		print(json.dumps(layoutsNow()))
	elif command == ["listen"] and len(sys.argv) == 3:
		listen(sys.argv[2])
	else:
		sys.exit("usage: lindaAcceptance.py xsockets PID | pointer-keyboard NAME | layouts | listen OUT")
