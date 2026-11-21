"""The GNOME guest is a ground: GNOME Shell in safe mode signs alice in on Wayland, native GTK 3 and
GTK 4 windows record keys from a uhid keyboard in the Polish layout, Alt+Tab and new windows move the
focus as AT-SPI reports it, and linda-keyd admits a client the session's autostarted runner starts,
which sits outside the logind session, and passes its keys to the windows.

Along the way it writes down experiment E7 (logind's facts about the session and whether XDG_VTNR
reaches an autostarted program), the GNOME half of E9 (the shell probe), and E20 (how long the guest
takes to start and how much memory it uses).
"""

import json
from typing import TYPE_CHECKING

from ground import VIRTUAL_KEYBOARD
from wl import shellProbe

if TYPE_CHECKING:
	from ground import Ground

UHID = "Ground uhid keyboard"
GTK3 = "Ground window"
GTK4 = "Ground window 4"
NEW = "Ground window 3"
LOGS = {GTK3: "app", GTK4: "app4", NEW: "app"}
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
)


def memory(ground: "Ground", when: str) -> None:
	info = {
		line.split(":")[0]: int(line.split()[1])
		for line in ground.shell("cat /proc/meminfo").splitlines()
		if line.split(":")[0] in ("MemTotal", "MemAvailable")
	}
	used = (info["MemTotal"] - info["MemAvailable"]) / 1024
	ground.machine.log(f"E20 memory {when}: {used:.0f} MiB in use of {info['MemTotal'] / 1024:.0f} MiB")
	ground.machine.log(ground.shell("ps -eo rss,user,comm --sort=-rss | head -n 12"))


def typedTo(ground: "Ground", keyboard: object, title: str) -> None:
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
	ground.waitForKeys("evdev", expected, mark, device=UHID)
	app = ground.waitForKeys(LOGS[title], expected, mark)
	presses = [r for r in app if r["value"] == 1]
	assert [r["keyval"] for r in presses[:3]] == ["b", "ISO_Level3_Shift", "aogonek"], app
	assert {r["window"] for r in app} == {title}, app
	others = [log for name, log in LOGS.items() if log != LOGS[title]]
	for log in others:
		assert not ground.keys(log, mark), (log, ground.keys(log, mark))
	ground.machine.log(f"{title}: {[(r['key'], r['value'], r['keyval']) for r in app]}")


def run(ground: "Ground") -> None:
	ground.machine.log(f"E20 start-up, seconds from starting the guest: {ground.bootTimes}")

	with ground.subtest("the GNOME session is up, with the shell in safe mode"):
		facts = ground.focus("facts")
		ground.machine.log(f"the shell: {facts}")
		assert facts["unsafeMode"] is False, facts
		assert facts["mode"] == "user", facts
		assert not facts["overviewActive"], facts
		memory(ground, "once the session is ready")

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

	ground.openWindow(GTK3)
	ground.openWindow(GTK4, name="window4", gtk4=True)
	memory(ground, "with the two windows open")
	keyboard = ground.uhidKeyboard(UHID)

	with ground.subtest("a key from the uhid keyboard reaches the GTK 3 and GTK 4 windows in the pl layout"):
		for title in (GTK4, GTK3):
			ground.activate(title)
			typedTo(ground, keyboard, title)

	with ground.subtest("the shell probe lists the desktop and counts the events of an Alt+Tab"):
		result = shellProbe.probe(ground, lambda: ground.altTab())
		for phase in ("before", "claimed", "after"):
			names = [a.get("name") for a in result[f"applications {phase}"]["applications"]]
			ground.machine.log(f"E9 applications on the desktop, {phase}: {names}")
		ground.machine.log(f"E9 settings: {result['settings']}")
		ground.machine.log(f"E9 event counts during Alt+Tab: {result['events']['counts']}")

	with ground.subtest("Alt+Tab moves the focus between the two windows as AT-SPI reports it"):
		first = ground.activeWindow()
		assert first is not None and first["title"] in (GTK3, GTK4), first
		other = GTK3 if first["title"] == GTK4 else GTK4
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
			ground.machine.log(f"Alt+Tab made {active['title']} active within {ground.now() - mark:.2f} s")
			typedTo(ground, keyboard, then)

	with ground.subtest("a new window takes the focus"):
		ground.openWindow(NEW, name="window3")
		active = ground.waitFor(
			lambda: (w := ground.activeWindow()) and w["title"] == NEW and w, f"{NEW} to be active"
		)
		ground.machine.log(f"the new window is active: {active}")
		typedTo(ground, keyboard, NEW)

	with ground.subtest("linda-keyd admits a client of the autostarted runner and passes its keys on"):
		ground.waitForHelper()
		mark = ground.mark()
		client = ground.keydClient("inGnome")
		cgroup = ground.shell(f"cat /proc/{client.pid}/cgroup").strip()
		answer = client.connect()
		said = ground.waitForHelperLog("admitted process", mark)[1]
		ground.machine.log(f"E7 admission: the client's control group {cgroup}; the helper: {said}")
		assert answer["welcome"], answer
		assert said.endswith(f"of session {front}"), (said, front)
		client.capture(UHID, since=mark)
		ground.waitForDisplayDevice(VIRTUAL_KEYBOARD)
		mark = ground.mark()
		keyboard.tap("KEY_C")
		typed = [("KEY_C", 1), ("KEY_C", 0)]
		ground.waitForKeys("evdev", typed, mark, device=VIRTUAL_KEYBOARD)
		app = ground.waitForKeys("app", typed, mark)
		assert app[0]["keyval"] == "c" and app[0]["window"] == NEW, app
		ground.settle(0.3)
		assert not ground.keys("evdev", mark, device=UHID), ground.keys("evdev", mark, device=UHID)
		client.stop()
		memory(ground, "at the end")
