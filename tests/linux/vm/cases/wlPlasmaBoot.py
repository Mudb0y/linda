"""The Plasma guest is a ground: Plasma Login Manager signs alice into a Plasma 6 Wayland session,
plasmashell runs, a native GTK 3 window and a native Qt 6 window record keys from a uhid keyboard in
the Polish layout KWin takes from /etc/xdg/kxkbrc, Alt+Tab moves the focus as AT-SPI reports it, and
linda-keyd admits a client the session's autostarted runner starts and passes its keys to the Qt
window through its virtual keyboard.

Along the way it writes down experiment E7 on Plasma (logind's facts about the session and whether
XDG_VTNR reaches an autostarted program), the Plasma half of E9 (the shell probe: are kwin_wayland
and plasmashell on the AT-SPI desktop, and what an Alt+Tab gives), E20 (how long the guest takes to
start and how much memory it uses), whether KWin honours the system-wide kxkbrc, whether KWin gives a
new window the focus by itself, and whether kscreenlocker sets logind's LockedHint.
"""

import json
import shlex
from typing import TYPE_CHECKING

from ground import GROUND, LOGS, VIRTUAL_KEYBOARD, Mark
from wl import shellProbe

if TYPE_CHECKING:
	from ground import Ground

#: Ground.records finds a recorder by its name in LOGS; the Qt window is this guest's alone.
LOGS.setdefault("appqt", f"{GROUND}/appqt.jsonl")

UHID = "Ground uhid keyboard"
GTK3 = "Ground window"
QT = "Ground Qt window"
WINDOWS = {
	GTK3: ("app", "window", ["ground-window", LOGS["app"], GTK3]),
	QT: ("appqt", "windowQt", ["ground-window-qt", LOGS["appqt"], QT]),
}
#: Seconds a new window is given to take the focus by itself before kdotool gives it.
NEW_WINDOW_FOCUS = 10
#: Seconds the lock screen is given to come or go, and LockedHint to follow it.
LOCK_TIMEOUT = 30
#: The session facts E7 asks for, and a few more that say where the session came from.
SESSION_PROPERTIES = ("Type", "Class", "VTNr", "Seat", "Remote", "Service", "Desktop", "State")
#: The runner's environment that says which session it is of, as Linda would read it.
SESSION_VARIABLES = (
	"XDG_VTNR",
	"XDG_SESSION_ID",
	"XDG_SESSION_TYPE",
	"XDG_SESSION_CLASS",
	"XDG_SEAT",
	"XDG_CURRENT_DESKTOP",
	"WAYLAND_DISPLAY",
	"DISPLAY",
	"QT_PLUGIN_PATH",
	"XDG_CONFIG_DIRS",
)
USER_KXKBRC = "/home/alice/.config/kxkbrc"
#: How the lock screen's greeter shows in the process list.
GREETER = "kscreenlocker_greet"


def memory(ground: "Ground", when: str) -> None:
	info = {
		line.split(":")[0]: int(line.split()[1])
		for line in ground.shell("cat /proc/meminfo").splitlines()
		if line.split(":")[0] in ("MemTotal", "MemAvailable")
	}
	used = (info["MemTotal"] - info["MemAvailable"]) / 1024
	ground.machine.log(f"E20 memory {when}: {used:.0f} MiB in use of {info['MemTotal'] / 1024:.0f} MiB")
	ground.machine.log(ground.shell("ps -eo rss,user,comm --sort=-rss | head -n 12"))


def openWindow(ground: "Ground", title: str) -> str:
	"""Start the window titled title on the compositor and wait until its field has the focus;
	answers "itself" when KWin gave it the focus, "kdotool" when the guest's driver had to."""
	log, name, argv = WINDOWS[title]
	mark = Mark(0, ground.now())
	ground.start(name, argv, env={"GDK_BACKEND": "wayland"})

	def ready() -> bool:
		return any(r["kind"] == "ready" for r in ground.records(log, mark))

	try:
		ground.waitFor(ready, f"{title} to take the focus by itself", NEW_WINDOW_FOCUS)
		return "itself"
	except AssertionError:
		pass
	ground.activate(title)
	ground.waitFor(ready, f"{title} to have the focus", 30)
	return "kdotool"


def typedTo(ground: "Ground", keyboard: object, title: str, device: str = UHID) -> None:
	"""B, then AltGr with A, which the Polish layout makes a with ogonek and the US one a plain a."""
	mark = ground.mark()
	keyboard.tap("KEY_B")
	keyboard.chord("KEY_RIGHTALT", "KEY_A")
	expected = [
		("KEY_B", 1),
		("KEY_B", 0),
		("KEY_RIGHTALT", 1),
		("KEY_A", 1),
		("KEY_A", 0),
		("KEY_RIGHTALT", 0),
	]
	ground.waitForKeys("evdev", expected, mark, device=device)
	log = WINDOWS[title][0]
	app = ground.waitForKeys(log, expected, mark)
	presses = [r for r in app if r["value"] == 1]
	assert [r["keyval"] for r in presses[:3]] == ["b", "ISO_Level3_Shift", "aogonek"], app
	assert {r["window"] for r in app} == {title}, app
	for other, _name, _argv in WINDOWS.values():
		if other != log:
			assert not ground.keys(other, mark), (other, ground.keys(other, mark))
	ground.machine.log(f"{title}: {[(r['key'], r['value'], r['keyval']) for r in app]}")


def programPattern(name: str) -> str:
	"""A process whose program, by its command line's first word, is name or name's wrapped binary:
	Plasma's programs are wrapped, so their process names are the wrappers'."""
	return f"^[^ ]*{name}(-wrapped)?( |$)"


def lockedHint(ground: "Ground", session: str) -> str:
	return ground.shell(f"loginctl show-session {session} -P LockedHint").strip()


def greeterRuns(ground: "Ground") -> bool:
	return ground.machine.execute(f"pgrep -u alice -f {shlex.quote(programPattern(GREETER))}")[0] == 0


def run(ground: "Ground") -> None:
	ground.machine.log(f"E20 start-up, seconds from starting the guest: {ground.bootTimes}")

	with ground.subtest("the Plasma session is up, and plasmashell runs"):
		for program in ("plasmashell", "kwin_wayland"):
			ground.shell(f"pgrep -u alice -f {shlex.quote(programPattern(program))}")
		facts = ground.focus("facts")
		ground.machine.log(f"the desktop: {json.dumps(facts)}")
		memory(ground, "once the session is ready")

	with ground.subtest("KWin takes the Polish layout from /etc/xdg/kxkbrc"):
		own = ground.machine.execute(f"cat {USER_KXKBRC}")
		ground.machine.log(
			f"kxkbrc: KWin's layouts {facts['layouts']}, the active one {facts['layout']}; "
			+ (f"alice's own {USER_KXKBRC}: {own[1]!r}" if own[0] == 0 else f"alice has no {USER_KXKBRC}")
		)
		assert isinstance(facts["layouts"], list), facts
		assert [entry[0] for entry in facts["layouts"]] == ["pl"], facts

	with ground.subtest("logind's facts about the session, and the autostarted runner's environment"):
		front = ground.shell("loginctl show-seat seat0 -P ActiveSession").strip()
		session = dict(
			line.split("=", 1)
			for line in ground.shell(
				f"loginctl show-session {front} " + " ".join(f"-p {p}" for p in SESSION_PROPERTIES)
			).splitlines()
			if "=" in line
		)
		display = ground.shell("loginctl show-user alice -P Display").strip()
		environment = ground.session({"op": "env"})["env"]
		variables = {name: environment.get(name) for name in SESSION_VARIABLES}
		runner = ground.shell("pgrep -u alice -f 'groundSession.py serve'").split()[0]
		cgroup = ground.shell(f"cat /proc/{runner}/cgroup").strip()
		ground.machine.log(
			"E7 "
			+ json.dumps(
				{
					"activeSession": front,
					"session": session,
					"userDisplay": display,
					"runner": variables,
					"runnerCgroup": cgroup,
				}
			)
		)
		assert session["Type"] == "wayland", session
		assert session["Class"] == "user", session
		assert session["Seat"] == "seat0", session
		assert display == front, (display, front)

	with ground.subtest("a native GTK 3 window and a native Qt 6 window take the focus"):
		for title in (GTK3, QT):
			how = openWindow(ground, title)
			ground.machine.log(f"{title} was given the focus by {how}")
		shown = [r for r in ground.records("appqt") if r["kind"] == "shown"]
		ground.machine.log(f"the Qt window: {shown}")
		assert shown and shown[0]["platform"] == "wayland", shown
		ground.machine.log(f"the windows AT-SPI lists: {ground.focus('windows')}")
		memory(ground, "with the two windows open")
	keyboard = ground.uhidKeyboard(UHID)

	with ground.subtest("a key from the uhid keyboard reaches the GTK 3 and Qt 6 windows in the pl layout"):
		for title in (QT, GTK3):
			ground.activate(title)
			typedTo(ground, keyboard, title)

	with ground.subtest("the shell probe lists the desktop and counts the events of an Alt+Tab"):
		result = shellProbe.probe(ground, lambda: ground.altTab())
		for phase in ("before", "claimed", "after"):
			names = [a.get("name") for a in result[f"applications {phase}"]["applications"]]
			ground.machine.log(f"E9 applications on the desktop, {phase}: {names}")
		for phase in ("before", "claimed", "listening", "after", "restored"):
			ground.machine.log(f"E9 settings, {phase}: {result[f'settings {phase}']}")
		counts = result["events"]["counts"]
		ground.machine.log(f"E9 event counts during Alt+Tab: {counts}")
		shells = sorted(
			{
				key.split(" ")[0]
				for key in counts
				if key.split(" ")[0] in ("plasmashell", "kwin", "kwin_wayland")
			}
		)
		ground.machine.log(f"E9 the desktop's own programs that sent events: {shells}")

	with ground.subtest("Alt+Tab moves the focus between the two windows as AT-SPI reports it"):
		first = ground.activeWindow()
		assert first is not None and first["title"] in (GTK3, QT), first
		other = GTK3 if first["title"] == QT else QT
		for press, then in (
			(lambda: ground.altTab(), other),
			(lambda: keyboard.chord("KEY_LEFTALT", "KEY_TAB"), first["title"]),
			(lambda: ground.altTab(), other),
		):
			mark = ground.now()
			press()
			active = ground.waitFor(
				lambda then=then: (w := ground.activeWindow()) and w["title"] == then and w,
				f"{then} to be active",
			)
			# An upper bound only: each look at AT-SPI is a program started through the runner.
			ground.machine.log(
				f"Alt+Tab made {active['title']} active, seen {ground.now() - mark:.2f} s after"
			)
			typedTo(ground, keyboard, then)

	with ground.subtest(
		"linda-keyd admits a client of the autostarted runner and passes keys to the Qt window"
	):
		ground.activate(QT)
		ground.waitForHelper()
		mark = ground.mark()
		client = ground.keydClient("inPlasma")
		cgroup = ground.shell(f"cat /proc/{client.pid}/cgroup").strip()
		answer = client.connect()
		said = ground.waitForHelperLog("admitted process", mark)[1]
		ground.machine.log(f"E7 admission: the client's control group {cgroup}; the helper: {said}")
		assert answer["welcome"], answer
		assert said.endswith(f"of session {front}"), (said, front)
		client.capture(UHID, since=mark)
		ground.waitForDisplayDevice(VIRTUAL_KEYBOARD)
		mark = ground.mark()
		typedTo(ground, keyboard, QT, device=VIRTUAL_KEYBOARD)
		ground.settle(0.3)
		assert not ground.keys("evdev", mark, device=UHID), ground.keys("evdev", mark, device=UHID)
		client.stop()
		memory(ground, "at the end")

	with ground.subtest("whether kscreenlocker sets logind's LockedHint"):
		before = lockedHint(ground, front)
		ground.shell(f"loginctl lock-session {front}")
		locked = ground.waitFor(lambda: greeterRuns(ground), "the lock screen to show", LOCK_TIMEOUT)
		try:
			ground.waitFor(lambda: lockedHint(ground, front) == "yes", "LockedHint to be set", LOCK_TIMEOUT)
		except AssertionError:
			pass
		whileLocked = lockedHint(ground, front)
		ground.shell(f"loginctl unlock-session {front}")
		try:
			ground.waitFor(lambda: not greeterRuns(ground), "the lock screen to go", LOCK_TIMEOUT)
		except AssertionError:
			pass
		unlocked = not greeterRuns(ground)
		after = lockedHint(ground, front)
		ground.machine.log(
			f"E10 LockedHint before the lock {before}, with the lock screen up {whileLocked}, after "
			f"logind's unlock {after}; the lock screen showed: {locked}; it went at logind's unlock: "
			f"{unlocked}"
		)
		assert before == "no", before
