"""Browse mode in Firefox on GNOME, native on Wayland, with the real helper, in one boot: H is Linda's
and never reaches the page; experiment E19, where Tab, which Linda keeps in browse mode and sends on
as its script ends, must reach Firefox once and after every key typed before it; and the Alt+Tab race,
fifty times between Firefox and a GTK 3 window, with a letter typed 100 ms after Alt is let go, which
must land in exactly one place, the right one: Linda's quick navigation when the keyboard went to
Firefox, the window when it went there.

Linda runs with its non-command keys passed to the page in browse mode, so that a letter that is not
Linda's can be typed before the Tab, and with a global plugin of the case's own that records each
quick navigation as Linda runs it, as tests/linux/test_waylandTimings.py records them.
"""

import json
import re
import shlex
from typing import TYPE_CHECKING

from ground import (
	BASE_SETTINGS,
	GROUND,
	KERNEL_ROUTE,
	LINDA_DIRECTORY,
	TRANSCRIPT,
	USER,
	VIRTUAL_KEYBOARD,
	WINDOW_TITLE,
	Mark,
	keyEvents,
)
from wl import lindaAcceptance as acceptance
from wl.lindaAcceptance import APART, UHID, said

if TYPE_CHECKING:
	from ground import FakeKeyboard, Ground

KEYD = "linda-keyd.service"
NVDA_LOG = f"{LINDA_DIRECTORY}/nvda.log"
HOOKS = f"{GROUND}/hooks"
#: Where the plugin writes each quick navigation.
PROBE = f"{GROUND}/quickNav.jsonl"
PROBE_PLUGIN = """\
import json
import os
import time

import globalPluginHandler
from virtualBuffers import atspi as atspiBuffers

PATH = os.environ.get("LINDA_QUICK_NAV_PROBE")


class GlobalPlugin(globalPluginHandler.GlobalPlugin):
	def __init__(self):
		super().__init__()
		if not PATH:
			return
		quickNav = atspiBuffers.AtSpiBuffer._quickNavScript

		def recorded(buffer, gesture, itemType, direction, errorMessage, readUnit):
			with open(PATH, "a", encoding="utf-8") as out:
				out.write(json.dumps({"t": time.monotonic(), "document": buffer.rootNVDAObject.name}) + "\\n")
			return quickNav(buffer, gesture, itemType, direction, errorMessage, readUnit)

		atspiBuffers.AtSpiBuffer._quickNavScript = recorded
"""

PAGE = f"{GROUND}/page.html"
PAGE_TITLE = "Ground page"
PROFILE = f"{GROUND}/firefox"
LINKS = 14
HEADINGS = 200
#: Every key the page has is written to Firefox's output, and every element that takes the focus.
PAGE_TEXT = (
	f'<!doctype html><html lang="en"><head><meta charset="utf-8"><title>{PAGE_TITLE}</title></head><body>'
	f"<h1>{PAGE_TITLE}</h1>"
	+ "".join(f'<p><a href="#l{n}" id="l{n}">Link {n}</a></p>' for n in range(1, LINKS + 1))
	+ "".join(f"<h2>Heading {n}</h2><p>Text under heading {n}.</p>" for n in range(1, HEADINGS + 1))
	+ "<script>"
	'document.addEventListener("keydown", (e) => console.log("ground key <" + e.key + ">"), true);'
	'document.addEventListener("focusin", (e) => e.target.id && console.log("ground focus <" + e.target.id + ">"));'
	"</script></body></html>"
)
#: A fresh profile's preferences, as cases/lindaBrowseKeys.py gives Firefox: no first-run pages, no
#: updates, no network, the console on standard output.
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
	"devtools.console.stdout.content": True,
}
MODIFIERS = ("Alt", "AltGraph", "Control", "Shift", "Meta", "Super", "NumLock", "CapsLock")
BROWSE_MODE = "Keys captured for browse mode"

#: Typed fast, as cases/lindaTyping.py types: each key held 15 ms, 15 ms apart.
FAST = {"hold": 0.015, "gap": 0.015}
#: Letters no browse mode command has, which Linda passes to the page with trapping off, typed before
#: the Tab in each round of E19.
BEFORE_TAB = ("j", "y", "z")
E19_ROUNDS = 10

#: Trials in the race, half to each window, and the letter's delay after Alt's release.
TRIALS = 50
DELAY = 0.1
#: Seconds each key of Alt+Tab is held, and between them, as a person presses it.
HOLD = 0.03
#: Seconds a trial waits for the window switched to to be active, and then for anything more.
FOLLOW_BOUND = 5.0
TRIAL_SETTLE = 1.5
TO_WINDOW = ["window"]
TO_FIREFOX = ["linda"]
#: Why every letter landed in the wrong place on GNOME in the case's first run, 50 of 50, 47 of the looks
#: ending unknown. Packages CF and KL mend the feed and the look in wave 4; this turns into a failure
#: to fail once they do.
RACE_FAULT = (
	"on GNOME the window events of an Alt+Tab come after the capture feed's grace, so a letter typed "
	"100 ms after Alt's release is passed once its look ends unknown: bound for Firefox it reaches the "
	"page and not Linda, and bound for the window it reaches the window while Linda, its focus still "
	"in Firefox, runs quick navigation there too"
)


def startLinda(ground: "Ground", settings: dict[str, dict[str, str]]) -> None:
	"""Ground.startLinda, with the case's hooks beside the transcript synth."""
	merged = {section: dict(values) for section, values in BASE_SETTINGS.items()}
	for section, values in settings.items():
		merged.setdefault(section, {}).update(values)
	lines = ["schemaVersion = 25"]
	for section, values in merged.items():
		lines.append(f"[{section}]")
		lines += [f"\t{key} = {value}" for key, value in values.items()]
	config = f"{LINDA_DIRECTORY}/config"
	ground.machine.succeed(
		f"install -d -o {USER} {LINDA_DIRECTORY} {config} {HOOKS}/globalPlugins",
		f"ln -sfn {ground.linda}/share/linda/tests/linux/testHooks/synthDrivers {HOOKS}/synthDrivers",
		f"echo {shlex.quote(PROBE_PLUGIN)} > {HOOKS}/globalPlugins/quickNavProbe.py",
		f"echo {shlex.quote(chr(10).join(lines))} > {config}/nvda.ini",
		f"touch {PROBE}",
		f"chown -R {USER} {config} {HOOKS} {PROBE}",
	)
	ground.start(
		"linda",
		[f"{ground.linda}/bin/linda", "-c", config, "-f", NVDA_LOG, "--debug-logging"],
		env={
			"LINDA_TEST_HOOKS_DIR": HOOKS,
			"LINDA_TRANSCRIPT_FILE": TRANSCRIPT,
			"LINDA_TRANSCRIPT_MODE": "immediate",
			"LINDA_QUICK_NAV_PROBE": PROBE,
		},
	)
	ground.lindaStarted = True
	ground.expect(WINDOW_TITLE, since=Mark(0, 0.0), timeout=120)


def firefoxOutput(ground: "Ground") -> str:
	return ground.shell(f"cat {GROUND}/jobs/firefox.out 2>/dev/null || true")


def pageKeys(ground: "Ground") -> list[str]:
	"""The keys the page had, but the modifiers."""
	return [k for k in re.findall(r"ground key <(.*?)>", firefoxOutput(ground)) if k not in MODIFIERS]


def pageFocus(ground: "Ground") -> list[str]:
	return re.findall(r"ground focus <(.*?)>", firefoxOutput(ground))


def quickNavs(ground: "Ground", since: float = 0) -> list[dict]:
	return [r for r in ground.readJsonLines(PROBE) if r["t"] >= since]


def spokenSince(ground: "Ground", mark: "Mark", text: str) -> bool:
	return any(text in spoken for _t, spoken in ground.speech(mark))


def startFirefox(ground: "Ground") -> str:
	prefs = "\n".join(f"user_pref({json.dumps(k)}, {json.dumps(v)});" for k, v in FIREFOX_PREFERENCES.items())
	ground.machine.succeed(
		f"echo {shlex.quote(PAGE_TEXT)} > {PAGE}",
		f"install -d -o {USER} {PROFILE}",
		f"echo {shlex.quote(prefs)} > {PROFILE}/user.js",
		f"chown -R {USER} {PAGE} {PROFILE}",
	)
	mark = ground.mark()
	browse = len(ground.lindaLogRecords(BROWSE_MODE))
	ground.start(
		"firefox",
		["firefox", "--new-instance", "--profile", PROFILE, f"file://{PAGE}"],
		env={"MOZ_ENABLE_WAYLAND": "1", "MOZ_CRASHREPORTER_DISABLE": "1"},
	)
	title = ground.waitFor(
		lambda: (t := acceptance.activeTitle(ground)) and t.startswith(PAGE_TITLE) and t,
		"Firefox's window to be active",
		180,
	)
	ground.waitFor(lambda: spokenSince(ground, mark, PAGE_TITLE), "Linda to read Firefox's page", 120)
	ground.waitForLindaLog(BROWSE_MODE, count=browse + 1, timeout=60)
	ground.settle(2)
	return title


def browseH(ground: "Ground", keyboard: "FakeKeyboard") -> None:
	with ground.subtest("browse mode's H is Linda's in native Firefox, and the page never has it"):
		before = len(pageKeys(ground))
		mark = ground.mark()
		keyboard.chord("KEY_LEFTCTRL", "KEY_HOME")
		ground.waitFor(lambda: spokenSince(ground, mark, PAGE_TITLE), "the top of the page")
		mark = ground.mark()
		keyboard.tap("KEY_H")
		ground.waitFor(lambda: spokenSince(ground, mark, "Heading 1"), "the first heading after the top")
		ground.settle(0.5)
		assert pageKeys(ground)[before:] == [], pageKeys(ground)[before:]


def e19(ground: "Ground", keyboard: "FakeKeyboard") -> dict:
	"""Experiment E19: from each link, the letters BEFORE_TAB and Tab typed fast. Linda passes the letters
	and keeps Tab, whose script finds the caret on the focused link and sends it on with TYPE_INJECT;
	Firefox must have the letters and then one Tab, which moves its focus to the next link."""
	figures: dict = {"rounds": []}
	with ground.subtest(
		"E19: Tab sent on by browse mode reaches Firefox once, after the keys typed before it"
	):
		mark = ground.mark()
		keyboard.chord("KEY_LEFTCTRL", "KEY_HOME")
		ground.waitFor(lambda: spokenSince(ground, mark, PAGE_TITLE), "the top of the page")
		ground.settle(APART)
		keys = len(pageKeys(ground))
		focus = len(pageFocus(ground))
		mark = ground.mark()
		keyboard.tap("KEY_TAB")
		ground.waitFor(lambda: pageFocus(ground)[focus:] == ["l1"], "Linda's Tab to focus the first link")
		ground.settle(APART)
		# NVDA moves the focus to the first link itself, the caret being above it, and sends nothing.
		assert pageKeys(ground)[keys:] == [], pageKeys(ground)[keys:]
		for link in range(1, E19_ROUNDS + 1):
			keys = len(pageKeys(ground))
			focus = len(pageFocus(ground))
			mark = ground.mark()
			steps: list[dict] = []
			for key in (*(f"KEY_{c.upper()}" for c in BEFORE_TAB), "KEY_TAB"):
				steps += [{"tap": key, "hold": FAST["hold"]}, {"sleep": FAST["gap"]}]
			written = keyboard.run(*steps)
			wanted = [*BEFORE_TAB, "Tab"]
			ground.waitFor(
				lambda keys=keys: len(pageKeys(ground)) >= keys + len(BEFORE_TAB) + 1,
				"the page to have the keys",
			)
			ground.waitFor(
				lambda focus=focus, link=link: pageFocus(ground)[focus:] == [f"l{link + 1}"],
				f"Firefox's focus to move to link {link + 1}",
			)
			ground.settle(APART)
			got = pageKeys(ground)[keys:]
			moved = pageFocus(ground)[focus:]
			virtual = keyEvents(ground.keys("evdev", mark, device=VIRTUAL_KEYBOARD))
			tabDown = acceptance.written(written, "KEY_TAB", 1)
			tabOut = next(
				(
					r["t"]
					for r in ground.keys("evdev", mark, device=VIRTUAL_KEYBOARD)
					if r["key"] == "KEY_TAB"
				),
				None,
			)
			figures["rounds"].append(
				{
					"page": got,
					"focus": moved,
					"virtual": virtual,
					"tabOut": None if tabOut is None else round(tabOut - tabDown, 4),
				}
			)
			assert got == wanted, (link, got, virtual)
			assert moved == [f"l{link + 1}"], (link, moved)
			assert virtual == [
				(k, v) for k in (r["key"] for r in written if r["value"] == 1) for v in (1, 0)
			], (
				link,
				virtual,
			)
		outcomes = [r.splitlines()[-1] for _t, r in ground.lindaLogRecords("linda-keyd's typed key")]
		figures["typedOutcomes"] = outcomes
		ground.machine.log(f"E19 figures {json.dumps(figures)}")
		assert not outcomes, outcomes
	return figures


def h(ground: "Ground") -> dict:
	"""Where an H could have gone: the test window, the page, and Linda's quick navigation."""
	return {
		"window": len(
			[r for r in ground.keys("app", Mark(0, 0.0)) if r["key"] == "KEY_H" and r["value"] == 1]
		),
		"page": pageKeys(ground).count("h"),
		"linda": len(quickNavs(ground)),
	}


def trial(ground: "Ground", keyboard: "FakeKeyboard", firefox: str) -> dict:
	before = h(ground)
	was = acceptance.activeTitle(ground)
	written = keyboard.run(
		{"press": "KEY_LEFTALT"},
		{"sleep": HOLD},
		{"tap": "KEY_TAB", "hold": HOLD},
		{"sleep": HOLD},
		{"release": "KEY_LEFTALT"},
		{"sleep": DELAY},
		{"tap": "KEY_H", "hold": HOLD},
	)
	released = acceptance.written(written, "KEY_LEFTALT", 0)
	pressed = acceptance.written(written, "KEY_H", 1)
	now = ground.waitFor(
		lambda: (t := acceptance.activeTitle(ground)) and t != was and t,
		"the switch to the other window",
		FOLLOW_BOUND,
	)
	ground.settle(TRIAL_SETTLE)
	after = h(ground)
	got = [place for place in after if after[place] != before[place]]
	assert now in (WINDOW_TITLE, firefox), now
	return {
		"to": "window" if now == WINDOW_TITLE else "firefox",
		"got": got,
		"right": TO_WINDOW if now == WINDOW_TITLE else TO_FIREFOX,
		"letterAfterRelease": round(pressed - released, 3),
	}


def race(ground: "Ground", keyboard: "FakeKeyboard", firefox: str) -> dict:
	with ground.subtest(
		f"the Alt+Tab race: H {DELAY * 1000:.0f} ms after Alt's release lands right, {TRIALS} times"
	):
		results = [trial(ground, keyboard, firefox) for _ in range(TRIALS)]
		wrong = [r for r in results if r["got"] != r["right"]]
		figures = {
			"trials": len(results),
			"toFirefox": sum(r["to"] == "firefox" for r in results),
			"toWindow": sum(r["to"] == "window" for r in results),
			"wrong": len(wrong),
			"wrongToFirefox": [r["got"] for r in wrong if r["to"] == "firefox"],
			"wrongToWindow": [r["got"] for r in wrong if r["to"] == "window"],
			"unknownLooks": len(ground.lindaLogRecords("was not found out for a key")),
		}
		ground.machine.log(f"AT race figures {json.dumps(figures)}")
		ground.machine.log(f"AT race trials {json.dumps(results)}")
		assert figures["toFirefox"] and figures["toWindow"], figures
		if RACE_FAULT:
			with ground.expectedFailure(RACE_FAULT):
				assert not wrong, f"{len(wrong)} of {len(results)} letters landed wrong: {wrong}"
		else:
			assert not wrong, f"{len(wrong)} of {len(results)} letters landed wrong: {wrong}"
	return figures


def nothingLogged(ground: "Ground", keyboard: "FakeKeyboard") -> None:
	with ground.subtest("what was typed is in neither Linda's log nor the helper's journal"):
		ground.activate(WINDOW_TITLE)
		ground.settle(APART)
		acceptance.typedMarker(ground, keyboard, NVDA_LOG)
		journal = ground.shell(f"journalctl -b -o cat --no-pager -u {KEYD} || true")
		assert not acceptance.holdingMarker({"journal": journal}), "the helper's journal holds what was typed"
		log = ground.shell(f"cat {NVDA_LOG}")
		lines = acceptance.linesHoldingMarker(log)
		ground.machine.log(f"nvda.log: {len(lines)} lines hold the marker whole, the first: {lines[:1]}")
		acceptance.assertLogClean(ground, {"nvda.log": log})


def run(ground: "Ground") -> None:
	ground.openWindow()
	ground.waitForHelper()
	keyboard = ground.uhidKeyboard(UHID)
	startLinda(
		ground,
		{"virtualBuffers": {"autoSayAllOnPageLoad": "False", "trapNonCommandGestures": "False"}},
	)
	ground.waitForLindaLog(KERNEL_ROUTE, timeout=30)
	firefox = startFirefox(ground)
	ground.machine.log(f"Firefox's window: {firefox!r}; Linda said {said(ground, Mark(0, 0.0))[-5:]}")
	browseH(ground, keyboard)
	figures = {"E19": e19(ground, keyboard)}
	figures["race"] = race(ground, keyboard, firefox)
	ground.machine.log("AT gnome browse figures " + json.dumps(figures, sort_keys=True))
	nothingLogged(ground, keyboard)
