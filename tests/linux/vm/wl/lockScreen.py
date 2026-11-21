"""The locked session on a Wayland desktop, with Linda started by the session's autostarted runner and
its keys taken by linda-keyd: Linda says the session is locked, reads the unlock prompt where the
shell is on the accessibility bus and says nothing more where it is not, speaks none of the password
typed there through the helper, and says the session is unlocked once it is. A page in browse mode
lies below the lock: its quick navigation keys are Linda's before the lock and again after it, and
while the session is locked they reach the prompt, which only accepts the password if each of its
letters did.

Experiment E10 is written down along the way: which process owns the desktop's screen saver name,
what its environment and logind say of its session, and what Linda's lock monitor made of it.

The password is MARKER, set for the user at the start. Package SH types it into the shells' other
password prompts with typePassword and passwordUnspoken, and holds the logs to it with
assertPasswordAbsent.
"""

import json
import re
import shlex
from typing import TYPE_CHECKING

from ground import GROUND, KERNEL_ROUTE, USER

if TYPE_CHECKING:
	from ground import FakeKeyboard, Ground, Mark

#: The password, which no layer may log or speak. Browse mode's quick navigation letters come first,
#: so that a capture left on for the page below the lock would keep them from the prompt.
MARKER = "hkfe7rq3wz"
UHID = "Ground uhid keyboard"
#: What Linda says as the lock begins and ends, sessionTracking's messages.
LOCKED = "Screen locked"
UNLOCKED = "Screen unlocked"
#: Seconds the desktop has to put its lock screen up or take it down, and Linda to say so.
LOCK_TIMEOUT = 30
#: The screen savers sessionLock.py listens to.
SAVER_NAMES = ("org.mate.ScreenSaver", "org.gnome.ScreenSaver", "org.freedesktop.ScreenSaver")
#: What Linda's log says as NVDA's focus leaves a document and as it is in one in browse mode, the
#: route's setCapture, which NVDA asks again at every change.
OFF_MODE = "Keys captured for off mode"
BROWSE_MODE = "Keys captured for browse mode"

PAGE = f"{GROUND}/lockPage.html"
PAGE_TITLE = "Ground page"
FIRST_HEADING = "First heading"
SECOND_HEADING = "Second heading"
THIRD_HEADING = "Third heading"
PROFILE = f"{GROUND}/firefox"
#: Every key the page has is written to Firefox's output.
PAGE_TEXT = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>{PAGE_TITLE}</title></head>
<body>
<h1>{FIRST_HEADING}</h1>
<p>Some text under the first heading.</p>
<h2>{SECOND_HEADING}</h2>
<p>Some text under the second.</p>
<h2>{THIRD_HEADING}</h2>
<p>The end.</p>
<script>
document.addEventListener("keydown", (event) => console.log("ground key <" + event.key + ">"), true);
</script>
</body></html>
"""
#: A fresh profile's preferences: no first-run pages, no updates, no network, the console on standard
#: output.
USER_JS = "\n".join(
	f"user_pref({json.dumps(name)}, {json.dumps(value)});"
	for name, value in {
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
		"devtools.console.stdout.content": True,
	}.items()
)


def keyName(character: str) -> str:
	return f"KEY_{character.upper()}"


def typePassword(ground: "Ground", keyboard: "FakeKeyboard", password: str = MARKER) -> None:
	"""Type password on a keyboard the helper holds, so that each key is Linda's to pass on."""
	keyboard.tap(*(keyName(c) for c in password))


def passwordUnspoken(ground: "Ground", since: "Mark", password: str = MARKER) -> list[str]:
	"""What Linda said after since, once it is known to hold none of password's characters, nor the
	password itself."""
	spoken = [text for _t, text in ground.speech(since)]
	characters = set(password.lower())
	leaked = [text for text in spoken if text.strip().lower() in characters or password in text.lower()]
	assert not leaked, f"Linda spoke {leaked} of the password; it said {spoken}"
	return spoken


def assertPasswordAbsent(ground: "Ground", password: str = MARKER) -> dict:
	"""Neither Linda's log nor the helper's journal holds password. The whole journal is only looked
	at, for the record: what the desktop's own programs log is not Linda's."""
	inLog = ground.lindaLog().count(password)
	inHelper = sum(message.count(password) for _t, message in ground.helperLog())
	journal = ground.shell(f"journalctl -b --no-pager -o cat | grep -c {shlex.quote(password)} || true")
	found = {"lindaLog": inLog, "helperJournal": inHelper, "wholeJournal": int(journal.strip() or 0)}
	ground.machine.log(f"the password's occurrences: {found}")
	assert inLog == 0 and inHelper == 0, found
	return found


def activeSession(ground: "Ground") -> str:
	return ground.shell("loginctl show-seat seat0 -P ActiveSession").strip()


def lockedHint(ground: "Ground", session: str) -> str:
	return ground.shell(f"loginctl show-session {session} -P LockedHint").strip()


def _environment(ground: "Ground", processID: int, names: tuple[str, ...]) -> dict[str, str | None]:
	text = ground.shell(f"tr '\\000' '\\n' < /proc/{processID}/environ 2>/dev/null || true")
	values = dict(line.split("=", 1) for line in text.splitlines() if "=" in line)
	return {name: values.get(name) for name in names}


def _logindSessionOf(ground: "Ground", processID: int) -> str:
	_status, output = ground.machine.execute(
		"busctl call org.freedesktop.login1 /org/freedesktop/login1 org.freedesktop.login1.Manager "
		f"GetSessionByPID u {processID} 2>&1"
	)
	return output.strip()


def sessionFacts(ground: "Ground") -> dict:
	"""For E10: who owns each screen saver name, what its environment and logind say of its session,
	and the same of Linda's process."""
	variables = ("XDG_SESSION_ID", "DISPLAY", "XDG_SESSION_TYPE", "WAYLAND_DISPLAY")
	savers = {}
	for name in SAVER_NAMES:
		answer = ground.run(
			[
				"busctl",
				"--user",
				"call",
				"org.freedesktop.DBus",
				"/org/freedesktop/DBus",
				"org.freedesktop.DBus",
				"GetConnectionUnixProcessID",
				"s",
				name,
			]
		)
		if answer.returncode != 0:
			continue
		processID = int(answer.stdout.split()[1])
		savers[name] = {
			"pid": processID,
			"program": ground.shell(f"cat /proc/{processID}/comm").strip(),
			"environment": _environment(ground, processID, variables),
			"cgroup": ground.shell(f"cat /proc/{processID}/cgroup").strip(),
			"logind": _logindSessionOf(ground, processID),
		}
	linda = ground.session({"op": "status", "name": "linda"})["pid"]
	return {
		"activeSession": activeSession(ground),
		"userDisplay": ground.shell(f"loginctl show-user {USER} -P Display").strip(),
		"savers": savers,
		"linda": {
			"pid": linda,
			"environment": _environment(ground, linda, variables),
			"cgroup": ground.shell(f"cat /proc/{linda}/cgroup").strip(),
			"logind": _logindSessionOf(ground, linda),
		},
	}


def pageKeys(ground: "Ground") -> list[str]:
	"""The keys the page had, but the modifiers."""
	text = ground.shell(f"cat {GROUND}/jobs/firefox.out 2>/dev/null || true")
	return [key for key in re.findall(r"ground key <(.*?)>", text) if key not in ("Alt", "Control", "Shift")]


def spokenSince(ground: "Ground", since: "Mark", text: str) -> bool:
	return any(text in spoken for _t, spoken in ground.speech(since))


def openPage(ground: "Ground") -> None:
	ground.machine.succeed(
		f"echo {shlex.quote(PAGE_TEXT)} > {PAGE}",
		f"install -d -o {USER} {PROFILE}",
		f"echo {shlex.quote(USER_JS)} > {PROFILE}/user.js",
		f"chown -R {USER} {PAGE} {PROFILE}",
	)
	mark = ground.mark()
	ground.start(
		"firefox",
		["firefox", "--no-remote", "--profile", PROFILE, f"file://{PAGE}"],
		env={"MOZ_CRASHREPORTER_DISABLE": "1"},
	)
	ground.waitFor(lambda: spokenSince(ground, mark, PAGE_TITLE), "Firefox's page", 120)
	ground.waitForLindaLog(BROWSE_MODE, timeout=30)
	ground.settle(2)


def headingByH(ground: "Ground", keyboard: "FakeKeyboard", heading: str) -> None:
	"""H is Linda's: it moves to heading, and the page never has it."""
	before = len(pageKeys(ground))
	mark = ground.mark()
	keyboard.tap("KEY_H")
	ground.waitFor(lambda: spokenSince(ground, mark, heading), f"Linda to move to {heading}")
	ground.settle(0.5)
	assert pageKeys(ground)[before:] == [], pageKeys(ground)[before:]


def loggedSpeech(ground: "Ground") -> list[str]:
	"""What NVDA's speech logged, at IO level, as it was asked to speak: unlike the transcript, also
	what was cancelled before it reached the synth."""
	return [record.split("\n", 1)[-1] for _t, record in ground.lindaLogRecords("Speaking [")]


def waitForField(ground: "Ground", since: "Mark") -> None:
	try:
		ground.waitFor(
			lambda: spokenSince(ground, since, "protected"), "Linda to read the password field", LOCK_TIMEOUT
		)
	except AssertionError as e:
		raise AssertionError(
			f"{e}; it spoke {ground.speech(since)}; its speech logged {loggedSpeech(ground)[-8:]}"
		) from None


def run(
	ground: "Ground", *, promptIsRead: bool, raisePrompt: list[str], promptFault: str | None = None
) -> dict:
	"""The whole case on one desktop. promptIsRead says whether the shell probe found the desktop's
	lock screen on the accessibility bus; raisePrompt is the keys that bring up its password field
	once the lock screen shows, typed through the helper; promptFault names a fault, where there is
	one today, that keeps Linda from reading the field all the same. Answers the figures for the log."""
	figures: dict = {}
	ground.shell(f"echo {USER}:{MARKER} | chpasswd")
	ground.openWindow()
	ground.waitForHelper()
	keyboard = ground.uhidKeyboard(UHID)
	# Read from the top by the keys alone.
	ground.startLinda({"virtualBuffers": {"autoSayAllOnPageLoad": "False"}})
	ground.waitForLindaLog(KERNEL_ROUTE, timeout=30)
	session = activeSession(ground)

	with ground.subtest("browse mode's H is Linda's before the lock"):
		openPage(ground)
		mark = ground.mark()
		keyboard.chord("KEY_LEFTCTRL", "KEY_HOME")
		ground.waitFor(lambda: spokenSince(ground, mark, FIRST_HEADING), "the first heading")
		headingByH(ground, keyboard, SECOND_HEADING)

	with ground.subtest("E10: whose screen saver Linda's lock monitor takes for this session's"):
		facts = sessionFacts(ground)
		facts["monitor"] = [record for _t, record in ground.lindaLogRecords("Session lock:")]
		figures["E10 before the lock"] = facts
		ground.machine.log("E10 " + json.dumps(facts))
		assert lockedHint(ground, session) == "no", lockedHint(ground, session)

	offBefore = len(ground.lindaLogRecords(OFF_MODE))
	browseBefore = len(ground.lindaLogRecords(BROWSE_MODE))
	keysBefore = len(pageKeys(ground))
	with ground.subtest("Linda says the session is locked"):
		lockMark = ground.mark()
		ground.shell(f"loginctl lock-session {session}")
		ground.waitFor(lambda: lockedHint(ground, session) == "yes", "LockedHint", LOCK_TIMEOUT)
		ground.waitForSpeech(LOCKED, lockMark, LOCK_TIMEOUT)
		decision = [record for _t, record in ground.lindaLogRecords("Session lock: locked")]
		figures["E10 decision"] = decision
		ground.machine.log(f"E10 the monitor: {decision}")

	with ground.subtest(
		"the unlock prompt is read where the shell is on the bus, and Linda is silent where not"
	):
		if raisePrompt:
			keyboard.tap(*raisePrompt)
		if promptIsRead:
			if promptFault is None:
				waitForField(ground, lockMark)
			else:
				with ground.expectedFailure(promptFault):
					waitForField(ground, lockMark)
		else:
			ground.settle(5)
			assert [text for _t, text in ground.speech(lockMark)] == [LOCKED], ground.speech(lockMark)
		ground.settle(1)
		figures["the prompt"] = [text for _t, text in ground.speech(lockMark)]
		ground.machine.log(f"the unlock prompt, as Linda read it: {figures['the prompt']}")

	with ground.subtest("typing the password through the helper speaks none of its characters"):
		mark = ground.mark()
		logged = len(loggedSpeech(ground))
		typePassword(ground, keyboard)
		ground.settle(2)
		figures["while typing"] = passwordUnspoken(ground, mark)
		figures["logged while typing"] = loggedSpeech(ground)[logged:]
		ground.machine.log(
			f"Linda's speech logged while the password was typed: {figures['logged while typing']}"
		)
		quoted = {f"'{c}'" for c in MARKER}
		leaked = [s for s in figures["logged while typing"] if any(q in s.lower() for q in quoted)]
		assert not leaked, leaked
		ground.machine.log(f"Linda said while the password was typed: {figures['while typing']}")
		if not promptIsRead:
			assert figures["while typing"] == [], figures["while typing"]

	with ground.subtest("browse capture is dormant while locked: the page has none of the keys"):
		assert len(ground.lindaLogRecords(OFF_MODE)) > offBefore, ground.lindaLogTail()
		assert pageKeys(ground)[keysBefore:] == [], pageKeys(ground)[keysBefore:]
		heard = [text for _t, text in ground.speech(lockMark)]
		below = [
			t
			for t in heard
			if any(h in t for h in (PAGE_TITLE, FIRST_HEADING, SECOND_HEADING, THIRD_HEADING))
		]
		assert not below, heard

	with ground.subtest("Enter unlocks, and Linda says so"):
		mark = ground.mark()
		keyboard.tap("KEY_ENTER")
		ground.waitFor(lambda: lockedHint(ground, session) == "no", "LockedHint to clear", LOCK_TIMEOUT)
		ground.waitForSpeech(UNLOCKED, mark, LOCK_TIMEOUT)
		ground.settle(1)
		figures["on unlocking"] = [text for _t, text in ground.speech(mark)]
		ground.machine.log(f"Linda said as the session was unlocked: {figures['on unlocking']}")
		assert pageKeys(ground)[keysBefore:] == [], pageKeys(ground)[keysBefore:]

	with ground.subtest("browse capture returns after the lock"):
		ground.waitForLindaLog(BROWSE_MODE, count=browseBefore + 1, timeout=30)
		ground.settle(1)
		headingByH(ground, keyboard, THIRD_HEADING)

	with ground.subtest("the password is absent from Linda's log and the helper's journal"):
		figures["the password's occurrences"] = assertPasswordAbsent(ground)
		figures["E10 lines"] = [record for _t, record in ground.lindaLogRecords("Session lock:")]
	return figures
