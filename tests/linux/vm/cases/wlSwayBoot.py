"""The sway guest is a ground: lightdm signs alice in to a sway session that logind counts as Wayland,
native GTK 3 and GTK 4 windows record keys from a uhid keyboard in the Polish layout, sway's IPC moves
the focus and times it, and linda-keyd admits a client started from sway's exec and passes its keys
on through its virtual keyboard, which sway gives the Polish layout too.

All of it runs twice in one boot: first in the session as the guest starts it, with Xwayland, which
an X client of the test's own then starts; then in the session started again with Xwayland disabled,
where nothing has DISPLAY.

Along the way it writes down experiment E7 (logind's facts about the session, and what a program
started from sway's exec knows of it) and E20 (how long the guest takes to start and how much memory
it uses).
"""

import json
import shlex
import time
from typing import TYPE_CHECKING

from ground import GROUND, LOGS, VIRTUAL_KEYBOARD, Mark

if TYPE_CHECKING:
	from ground import FakeKeyboard, Ground

UHID = "Ground uhid keyboard"
GTK3 = "Ground window"
GTK4 = "Ground window 4"
#: A GTK 3 test window made an X client, which starts Xwayland in the variant that has it.
XWINDOW = "Ground X window"
#: The record each window's keys go to.
WINDOW_LOGS = {GTK3: "app", GTK4: "app4", XWINDOW: "app"}
SESSION_PROPERTIES = ("Type", "Class", "VTNr", "Seat", "Remote", "Service", "Desktop", "State", "Scope")
SESSION_VARIABLES = (
	"XDG_VTNR",
	"XDG_SESSION_ID",
	"XDG_SESSION_TYPE",
	"XDG_SESSION_CLASS",
	"XDG_SEAT",
	"XDG_CURRENT_DESKTOP",
	"WAYLAND_DISPLAY",
	"DISPLAY",
	"SWAYSOCK",
)
#: Seconds the old session may take to end once lightdm is stopped.
SESSION_END_TIMEOUT = 60


def memory(ground: "Ground", when: str) -> None:
	info = {
		line.split(":")[0]: int(line.split()[1])
		for line in ground.shell("cat /proc/meminfo").splitlines()
		if line.split(":")[0] in ("MemTotal", "MemAvailable")
	}
	used = (info["MemTotal"] - info["MemAvailable"]) / 1024
	ground.machine.log(f"E20 memory {when}: {used:.0f} MiB in use of {info['MemTotal'] / 1024:.0f} MiB")
	ground.machine.log(ground.shell("ps -eo rss,user,comm --sort=-rss | head -n 12"))


def frontSession(ground: "Ground") -> str:
	return ground.shell("loginctl show-seat seat0 -P ActiveSession").strip()


def sessionFacts(ground: "Ground", variant: str) -> tuple[str, dict]:
	"""E7: logind's session in front and what the runner sway's exec started knows of it."""
	front = frontSession(ground)
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
		f"E7 {variant} "
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
	assert session["State"] == "active", session
	assert display == front, (display, front)
	# What sway's exec starts is the session's own, as a program a user's sway configuration starts.
	assert cgroup.endswith(f"/{session['Scope']}"), (cgroup, session)
	return front, variables


def typedTo(ground: "Ground", keyboard: "FakeKeyboard", title: str, through: str) -> None:
	"""B, then AltGr with A, which the Polish layout makes a with ogonek and the US one a plain a,
	seen by the kernel on the keyboard named through and by the window titled title alone."""
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
	ground.waitForKeys("evdev", expected, mark, device=through)
	log = WINDOW_LOGS[title]
	app = ground.waitForKeys(log, expected, mark)
	presses = [r for r in app if r["value"] == 1]
	assert [r["keyval"] for r in presses[:3]] == ["b", "ISO_Level3_Shift", "aogonek"], app
	assert {r["window"] for r in app} == {title}, app
	for other in {"app", "app4"} - {log}:
		assert not ground.keys(other, mark), (other, ground.keys(other, mark))
	ground.machine.log(f"{title} through {through}: {[(r['key'], r['value'], r['keyval']) for r in app]}")


def watchWindows(ground: "Ground", name: str) -> str:
	"""Start focusSway.py's watcher of sway's window events in the session; answers its file."""
	path = f"{GROUND}/{name}.jsonl"
	ground.start(name, ["ground-sway", "watch", path])
	ground.waitFor(
		lambda: any(r["kind"] == "ready" for r in ground.readJsonLines(path)), "the window watcher"
	)
	return path


def openXWindow(ground: "Ground") -> None:
	"""The GTK 3 test window as an X client, which makes sway start Xwayland."""
	mark = Mark(0, ground.now())
	ground.start("xwindow", ["ground-window", LOGS["app"], XWINDOW], env={"GDK_BACKEND": "x11"})
	ground.waitFor(
		lambda: any(r["kind"] == "ready" and r["received"] >= mark.t for r in ground.records("app")),
		"the X test window",
		60,
	)


def variant(ground: "Ground", keyboard: "FakeKeyboard", xwayland: bool) -> None:
	name = "with Xwayland" if xwayland else "without Xwayland"
	tag = "X" if xwayland else "W"

	with ground.subtest(f"{name}: the sway session is up"):
		facts = ground.focus("facts")
		ground.machine.log(f"sway {name}: {facts}")
		assert facts["xwaylandConfigured"] == ("xwayland enable" if xwayland else "xwayland disable"), facts
		assert (facts["display"] is not None) == xwayland, facts
		# On demand: nothing has asked for it yet.
		assert not facts["xwaylandRunning"], facts
		memory(ground, f"{name}, once the session is ready")

	with ground.subtest(f"{name}: logind's facts about the session, and the runner's environment"):
		front, variables = sessionFacts(ground, name)
		assert (variables["DISPLAY"] is not None) == xwayland, variables

	ground.openWindow(GTK3)
	ground.openWindow(GTK4, name="window4", gtk4=True)
	titles = [GTK3, GTK4]
	if xwayland:
		openXWindow(ground)
		titles.append(XWINDOW)
	windows = {w["title"]: w for w in ground.focus("windows")}
	ground.machine.log(f"sway's windows {name}: {windows}")
	assert windows[GTK3]["protocol"] == windows[GTK4]["protocol"] == "xdg_shell", windows
	if xwayland:
		assert windows[XWINDOW]["protocol"] == "xwayland", windows
	assert ground.focus("facts")["xwaylandRunning"] == xwayland
	memory(ground, f"{name}, with the windows open")
	ground.waitForDisplayDevice(UHID)

	with ground.subtest(f"{name}: a key from the uhid keyboard reaches each window in the pl layout"):
		for title in titles:
			ground.activate(title)
			typedTo(ground, keyboard, title, UHID)

	with ground.subtest(f"{name}: sway's window events time a focus change"):
		path = watchWindows(ground, f"sway-windows-{tag}")
		for title in (GTK3, GTK4):
			mark = ground.now()
			ground.activate(title)
			event = ground.waitFor(
				lambda title=title, mark=mark: next(
					(
						r
						for r in ground.readJsonLines(path)
						if r["kind"] == "window"
						and r["change"] == "focus"
						and r["title"] == title
						and r["received"] >= mark
					),
					None,
				),
				f"sway's focus event for {title}",
			)
			# An upper bound only: the request goes through a program the runner starts.
			ground.machine.log(f"sway focused {title} {event['received'] - mark:.3f} s after it was asked")
			typedTo(ground, keyboard, title, UHID)

	with ground.subtest(f"{name}: linda-keyd admits a client started from sway's exec and passes its keys"):
		ground.waitForHelper()
		mark = ground.mark()
		client = ground.keydClient(f"inSway{tag}")
		cgroup = ground.shell(f"cat /proc/{client.pid}/cgroup").strip()
		answer = client.connect()
		said = ground.waitForHelperLog("admitted process", mark)[1]
		ground.machine.log(f"E7 {name} admission: the client's control group {cgroup}; the helper: {said}")
		assert answer["welcome"], answer
		assert said.endswith(f"of session {front}"), (said, front)
		client.capture(UHID, since=mark)
		ground.waitForDisplayDevice(VIRTUAL_KEYBOARD)
		keyboards = {k["name"]: k for k in ground.focus("facts")["keyboards"]}
		ground.machine.log(f"sway's keyboards {name}: {keyboards}")
		assert keyboards[VIRTUAL_KEYBOARD]["layout"] == "Polish", keyboards
		held = ground.mark()
		for title in titles:
			ground.activate(title)
			typedTo(ground, keyboard, title, VIRTUAL_KEYBOARD)
		ground.settle(0.3)
		assert not ground.keys("evdev", held, device=UHID), ground.keys("evdev", held, device=UHID)
		client.stop()


def startAgain(ground: "Ground", xwayland: bool) -> None:
	"""Write the variant sway reads as it starts, end the session and have lightdm sign alice in again."""
	front = frontSession(ground)
	line = "xwayland enable" if xwayland else "xwayland disable"
	ground.shell(f"echo {shlex.quote(line)} > {shlex.quote(ground.guest['variant'])}")
	ground.shell("systemctl stop display-manager.service")
	# Everything the session ran goes with it, the runner and what it started among them.
	ground.shell(f"loginctl terminate-session {front} || true")
	ground.waitFor(
		lambda: ground.machine.execute(f"loginctl show-session {front}")[0] != 0,
		f"session {front} to end",
		SESSION_END_TIMEOUT,
	)
	# So that the waits below see the new session's and sway uses the fixed name again.
	stale = [ground.guest["socket"], ground.guest["swaysock"], f"{GROUND}/session.sock"]
	ground.shell("rm -f " + " ".join(shlex.quote(path) for path in stale))
	started = time.monotonic()
	ground.shell("systemctl start display-manager.service")
	ground._bootWayland(started)
	assert frontSession(ground) != front, front


def run(ground: "Ground") -> None:
	ground.machine.log(f"E20 start-up with Xwayland, seconds from starting the guest: {ground.bootTimes}")
	keyboard = ground.uhidKeyboard(UHID)
	variant(ground, keyboard, xwayland=True)

	with ground.subtest("the session starts again without Xwayland"):
		startAgain(ground, xwayland=False)
		ground.machine.log(
			f"E20 start-up without Xwayland, seconds from starting lightdm again: {ground.bootTimes}"
		)
	variant(ground, keyboard, xwayland=False)
	memory(ground, "at the end")
