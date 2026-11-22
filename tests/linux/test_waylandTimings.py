"""The Alt+Tab race on the Wayland route, timed against sway's window subscription, and experiment E4's
window event timings.

A letter typed just after Alt+Tab must go to exactly one place, the right one: to Linda, as a browse
mode key of the page, when the keyboard went to Firefox, and to the GTK 4 key probe when it went
there. Linda's capture feed hears the switch from AT-SPI only some milliseconds after sway made it, and
the kernel source holds such a letter until the feed has heard, or for capture's grace at the most.
Here Alt+Tab is sway's focus next, bound for the test, pressed on the stand-in for linda-keyd as a
person presses it on a keyboard the helper holds, and the letter follows Alt's release by 100 ms, by
100 ms with every window event the feed hears held back 200 ms through the test hook, and by 30 ms. A
second window of the same program is a case of its own, counted rather than held to: the capture
follows it as on X. Experiment E4 times the window events of GTK 3, GTK 4, Qt 6, Firefox and Chromium
against the key on a private connection, with Linda idle and with it reading.

Under rig sway, with and without Xwayland, through tests/linux/wayland-gate SCRATCH, or:

	LINDA_RIG_COMPOSITOR=sway nix develop . -c tests/linux/rig python3 -m pytest -q -s tests/linux/test_waylandTimings.py
"""

import ast
import contextlib
import json
import math
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import chromiumSession
import desktopSession
import harness
import pytest
import test_browseKeys as browseKeys
import waylandKeyboard
import webSession
from keydStandIn import VERDICT_WAIT, code
from test_waylandKeys import MARKER, WaylandLinda, socketDir, waylandLinda  # noqa: F401
from test_waylandMenu import KEY_PROBE, Listener

pytestmark = pytest.mark.rigSession("sway")

#: Seconds each key of Alt+Tab and the letter is held, and between them.
HOLD = 0.03
#: Trials in each race, half of them to each window.
TRIALS = 100
#: The letter typed after Alt+Tab: a browse mode key on the keys page, and typed in the probe.
LETTER = "h"
#: Seconds a trial waits for Linda to follow the switch, and then for anything more to happen.
FOLLOW_BOUND = 5.0
TRIAL_SETTLE = 0.3
#: Experiment E4 set the bound on letters misplaced at 30 ms: the share of switches whose first window
#: event the feed would hear after FOCUS_MOVE_GRACE, at most one in a hundred, the 99th percentile.
MISPLACED_AT_30_MS = 1
#: The window events held back, in milliseconds, for the race that holds them.
HELD_BACK_MS = 200
#: The keys page in Firefox, and the window of the usable page a second Firefox window shows.
KEYS_WINDOW = "Keys page — Mozilla Firefox"
USABLE_WINDOW = "Usable page — Mozilla Firefox"
PROBE_WINDOW = "Key probe"
#: Switches between windows timed for experiment E4, in each of its two runs.
E4_SWITCHES = 60
#: Seconds the reading runs before Alt+Tab in E4's second run, and the speech that makes it last.
READING_BEFORE = 0.3
TIMED_READING = {"LINDA_TRANSCRIPT_MODE": "timed", "LINDA_TRANSCRIPT_MS_PER_CHAR": "10"}
#: Switches between two windows of one program.
SAME_PROGRAM_TRIALS = 20
#: Where a letter of the race belongs, by the window Alt+Tab went to, and where each one bound for
#: Firefox goes while the race's known fault stands.
TO_PROBE = ["probe"]
TO_FIREFOX = ["linda in Keys page"]
BOTH_PLACES = ["page", "linda in Keys page"]

#: A global plugin that records, as JSON lines in the file LINDA_FEED_PROBE names, with the clock every
#: process shares: every window event the Wayland route's capture feed follows, when it follows it, and
#: each chord the kernel source says may move the focus; and each browse mode quick navigation, with
#: the document it moved in.
FEED_PROBE = """\
import json
import os
import threading
import time

import globalPluginHandler
import wx
from linuxPlatform import inputHook
from virtualBuffers import atspi as atspiBuffers

PATH = os.environ.get("LINDA_FEED_PROBE")
_lock = threading.Lock()


def record(kind, **fields):
	line = json.dumps({"kind": kind, "t": time.monotonic(), **fields})
	with _lock, open(PATH, "a", encoding="utf-8") as out:
		out.write(line + "\\n")


def wrap(feed, name, describe):
	original = getattr(feed, name)

	def wrapped(*args):
		record(name, **describe(*args))
		return original(*args)

	setattr(feed, name, wrapped)


def window(key, now):
	return {"window": list(key), "now": now}


class GlobalPlugin(globalPluginHandler.GlobalPlugin):
	def __init__(self):
		super().__init__()
		if not PATH:
			return
		quickNav = atspiBuffers.AtSpiBuffer._quickNavScript

		def recorded(buffer, gesture, itemType, direction, errorMessage, readUnit):
			record("quickNav", document=buffer.rootNVDAObject.name, itemType=itemType)
			return quickNav(buffer, gesture, itemType, direction, errorMessage, readUnit)

		atspiBuffers.AtSpiBuffer._quickNavScript = recorded
		self.timer = wx.Timer()
		self.timer.Bind(wx.EVT_TIMER, self.poll)
		self.timer.Start(50)

	def poll(self, evt):
		feed = getattr(getattr(inputHook._source, "_environment", None), "feed", None)
		if feed is None:
			return
		self.timer.Stop()
		wrap(feed, "_activated", window)
		wrap(feed, "_deactivated", window)
		wrap(feed, "_windowGone", window)
		wrap(feed, "_applicationGone", lambda busName, now: {"window": [busName, ""], "now": now})
		wrap(feed, "noteFocusMayMove", lambda now: {"now": now})
		record("ready")
"""


def focusMoveGrace() -> float:
	"""The capture feed's FOCUS_MOVE_GRACE, read from its source, which this process does not import."""
	tree = ast.parse((harness.SOURCE / "linuxPlatform" / "keys" / "atspiCapture.py").read_text())
	for node in tree.body:
		if isinstance(node, ast.Assign) and [t.id for t in node.targets if isinstance(t, ast.Name)] == [
			"FOCUS_MOVE_GRACE"
		]:
			return ast.literal_eval(node.value)
	raise AssertionError("atspiCapture.py has no FOCUS_MOVE_GRACE")


def percentile(values: list[float], share: float) -> float:
	"""The value share of values are at or below, by the nearest rank."""
	ordered = sorted(values)
	return ordered[max(0, math.ceil(share * len(ordered)) - 1)]


def summary(values: list[float]) -> str:
	if not values:
		return "none"
	ms = [value * 1000 for value in values]
	return (
		f"{len(ms)}: median {percentile(ms, 0.5):.0f} ms, 99th percentile {percentile(ms, 0.99):.0f} ms, "
		f"most {max(ms):.0f} ms"
	)


def sessionName() -> str:
	return harness.rigSession() + ("-xwayland" if harness.rigHasXwayland() else "")


class SwayFocus:
	"""sway's window subscription, each focus change stamped as it is read."""

	def __init__(self, directory: Path) -> None:
		with open(directory / "swayFocus.stderr.txt", "w") as errors:
			self.process = subprocess.Popen(
				["swaymsg", "-t", "subscribe", "-m", '["window"]'],
				stdout=subprocess.PIPE,
				stderr=errors,
				text=True,
			)
		#: (when it was read, the window's name, its process) of each focus change.
		self.events: list[tuple[float, str, int]] = []
		threading.Thread(target=self._read, daemon=True).start()

	def _read(self) -> None:
		for line in self.process.stdout:
			read = time.monotonic()
			try:
				event = json.loads(line)
			except json.JSONDecodeError:
				continue
			if event.get("change") == "focus":
				container = event.get("container") or {}
				self.events.append((read, container.get("name") or "", container.get("pid") or 0))

	def after(self, since: float) -> tuple[float, str, int] | None:
		return next((event for event in self.events if event[0] >= since), None)

	def stop(self) -> None:
		self.process.terminate()
		self.process.wait(5)
		self.process.stdout.close()


@contextlib.contextmanager
def altTabBound() -> Iterator[None]:
	"""Alt+Tab as sway's focus next, for as long as the test needs it: the rig's sway outlives the test."""
	subprocess.run(["swaymsg", "bindsym Mod1+Tab focus next"], check=True, capture_output=True, timeout=5)
	try:
		yield
	finally:
		subprocess.run(["swaymsg", "unbindsym Mod1+Tab"], check=False, capture_output=True, timeout=5)


class FeedProbe:
	"""What FEED_PROBE recorded."""

	def __init__(self, path: Path) -> None:
		self.path = path
		path.touch()

	def records(self, kind: str | None = None, since: float = 0) -> list[dict]:
		text = self.path.read_text(encoding="utf-8")
		records = [json.loads(line) for line in text.splitlines() if line.strip()]
		return [r for r in records if (kind is None or r["kind"] == kind) and r["t"] >= since]

	def windowEvents(self, since: float) -> list[dict]:
		kinds = ("_activated", "_deactivated", "_windowGone", "_applicationGone")
		return [r for r in self.records(since=since) if r["kind"] in kinds]

	def waitFor(self, kind: str, timeout: float) -> None:
		deadline = time.monotonic() + timeout
		while not self.records(kind):
			assert time.monotonic() < deadline, f"the feed probe recorded no {kind}"
			time.sleep(0.05)


class Race:
	"""Linda reading the keys page in Firefox beside the key probe, under sway, with Alt+Tab bound."""

	def __init__(self, session: WaylandLinda, directory: Path, *, holdMs: int = 0) -> None:
		self.session = session
		self.feed = FeedProbe(directory / "feed.jsonl")
		env = {"LINDA_FEED_PROBE": str(self.feed.path)}
		if holdMs:
			env["LINDA_TEST_CAPTURE_DELAY_MS"] = str(holdMs)
		self.nvda = session.start(
			browseKeys.browseConfig(directory),
			plugins={"browseKeysProbe": browseKeys.PROBE_PLUGIN, "feedProbe": FEED_PROBE},
			env=env,
		)
		self.feed.waitFor("ready", FOLLOW_BOUND)
		webSession.startFirefox(session.desktop, "keys.html")
		self.nvda.waitForLog("Buffer rendered", browseKeys.BUFFER_BOUND)
		browseKeys.waitForCapture(self.nvda, "browse", timeout=browseKeys.BUFFER_BOUND)
		time.sleep(browseKeys.SETTLE)
		self.page = browseKeys.KeysPage()
		self.sway = SwayFocus(directory)

	def altTab(self) -> tuple[float, float]:
		"""Alt+Tab on the stand-in; when Tab went down and when Alt came up."""
		keyboard = self.session.keyboard
		keyboard.down("alt")
		time.sleep(HOLD)
		tab = time.monotonic()
		keyboard.down("Tab")
		time.sleep(HOLD)
		keyboard.up("Tab")
		time.sleep(HOLD)
		released = time.monotonic()
		keyboard.up("alt")
		return tab, released

	def trial(self, delay: float, toProbe: bool, places: Callable[[], dict]) -> dict:
		"""One Alt+Tab and the letter delay seconds after Alt's release, once Linda has followed."""
		session = self.session
		before = places()
		tab, released = self.altTab()
		time.sleep(max(0.0, released + delay - time.monotonic()))
		pressed = time.monotonic()
		session.keyboard.down(LETTER)
		time.sleep(HOLD)
		session.keyboard.up(LETTER)
		wanted = "off" if toProbe else "browse"
		deadline = time.monotonic() + FOLLOW_BOUND
		while browseKeys.captureMode(self.nvda) != wanted and time.monotonic() < deadline:
			time.sleep(0.05)
		time.sleep(TRIAL_SETTLE)
		after = places()
		got = [place for place in after if after[place] != before[place]]
		quick = self.feed.records("quickNav", since=pressed)
		if quick:
			got.append(f"linda in {quick[0]['document']}")
		swayed = self.sway.after(tab)
		heard = self.feed.windowEvents(tab)
		return {
			"got": got,
			"sway": swayed[0] - tab if swayed else None,
			"heard": heard[0]["now"] - released if heard else None,
			"pressedAfterHeard": pressed - heard[0]["now"] if heard else None,
			"answers": session.answers(code(LETTER), pressed),
		}

	def run(self, delay: float, trials: int = TRIALS) -> list[dict]:
		"""Alternate between the probe and the page: a letter to the probe is typed there, one to the page
		is Linda's, in the keys page's document."""
		probe = self.session.probe

		def places() -> dict:
			return {
				"probe": sum(line == f"pressed {LETTER}" for line in probe.lines),
				"page": len(self.page.log()),
			}

		results = []
		for number in range(trials):
			toProbe = number % 2 == 0
			result = self.trial(delay, toProbe, places)
			result["right"] = TO_PROBE if toProbe else TO_FIREFOX
			results.append(result)
		return results

	def close(self) -> None:
		self.sway.stop()


@contextlib.contextmanager
def race(tmp_path: Path, socketDir: Path, *, holdMs: int = 0) -> Iterator[Race]:  # noqa: F811
	webSession.requireBrowseLibraries()
	with waylandLinda(tmp_path, socketDir) as session, altTabBound():
		found = Race(session, tmp_path, holdMs=holdMs)
		try:
			yield found
		finally:
			found.close()


def report(name: str, results: list[dict]) -> None:
	misplaced = [r for r in results if r["got"] != r["right"]]
	print(
		f"{sessionName()}: {name}: {len(misplaced)} of {len(results)} misplaced; sway's focus after Tab "
		f"{summary([r['sway'] for r in results if r['sway'] is not None])}; the feed's first window event "
		f"after Alt's release {summary([r['heard'] for r in results if r['heard'] is not None])}"
	)
	for result in misplaced:
		print(f"{sessionName()}: {name}: misplaced {result}")


def finish(found: Race) -> None:
	session = found.session
	session.step("Insert+space", "Focus mode")
	browseKeys.waitForCapture(found.nvda, "focus")
	session.stepHolding("Tab", "Field edit")
	session.desktop.typeText(MARKER)
	found.page.waitForField(MARKER)
	session.quit()


class Misplaced(AssertionError):
	"""The races' known fault and nothing else: letters bound for Firefox that went to the page and to
	Linda both."""


def misplaced(results: list[dict]) -> list[dict]:
	return [r for r in results if r["got"] != r["right"]]


def blame(results: list[dict], wrong: list[dict]) -> None:
	"""Raise Misplaced when every wrong trial has the known fault's shape; fail plainly when any has
	another, which RACE_FAULT must not swallow."""
	if not wrong:
		return
	other = [r for r in wrong if r["right"] != TO_FIREFOX or r["got"] != BOTH_PLACES]
	assert not other, f"{len(other)} of {len(results)} letters misplaced, not by the known fault: {other}"
	raise Misplaced(
		f"{len(wrong)} of {len(results)} letters bound for Firefox went to the page and Linda both"
	)


def landedRight(results: list[dict]) -> None:
	blame(results, misplaced(results))


def waitedForActivation(results: list[dict]) -> None:
	blame(results, misplaced(results))
	# A letter bound for Firefox came before the feed heard of the switch, and waited for it. One bound
	# for the probe may pass at once: NVDA's own focus reaches the probe first and turns capture off.
	toFirefox = [r for r in results if r["right"] == TO_FIREFOX]
	assert toFirefox
	assert all(r["pressedAfterHeard"] is not None and r["pressedAfterHeard"] < 0 for r in toFirefox)
	assert all(r["answers"] and r["answers"][0]["given"][:1] == [VERDICT_WAIT] for r in toFirefox)


def wentToOnePlace(results: list[dict]) -> None:
	wrong = misplaced(results)
	if any(len(r["got"]) != 1 for r in results) or len(wrong) > MISPLACED_AT_30_MS:
		blame(results, wrong)


#: Why the races fail on main as wave 3 builds them, in results/AR-altTab-race.txt of the Wayland work:
#: sway moves the keyboard as Tab goes down, and the window events come before Alt is let go, whose
#: release opens a wait for window events that never come, so that every look ends unknown at the
#: grace and the letter is passed, while NVDA acts on it too; and with the feed's events held back,
#: NVDA's setCapture, ahead of the feed, leaves it with no anchor to look for. Only Misplaced, raised
#: once Linda's quit checks have passed, is the known fault: a failure anywhere else must fail. strict,
#: so that the fix turns these into failures until the marks go.
RACE_FAULT = pytest.mark.xfail(
	reason="the Wayland route sends letters typed just after Alt+Tab under sway to the page and NVDA both",
	raises=Misplaced,
	strict=True,
)


@RACE_FAULT
def test_aLetterAHundredMillisecondsAfterAltTabLandsRight(tmp_path: Path, socketDir: Path):  # noqa: F811
	with race(tmp_path, socketDir) as found:
		results = found.run(0.1)
		report("100 ms", results)
		finish(found)
	landedRight(results)


@RACE_FAULT
def test_aLetterWaitsForActivationHeldBack(tmp_path: Path, socketDir: Path):  # noqa: F811
	with race(tmp_path, socketDir, holdMs=HELD_BACK_MS) as found:
		results = found.run(0.1)
		report(f"100 ms, window events held back {HELD_BACK_MS} ms", results)
		finish(found)
	waitedForActivation(results)


@RACE_FAULT
def test_aLetterThirtyMillisecondsAfterAltTabGoesToOnePlace(tmp_path: Path, socketDir: Path):  # noqa: F811
	with race(tmp_path, socketDir) as found:
		results = found.run(0.03)
		report("30 ms", results)
		finish(found)
	wentToOnePlace(results)


def trialOf(right: list[str], got: list[str], given: list[int], pressedAfterHeard: float = -0.04) -> dict:
	"""A trial as Race.run gives it, for the checks' own tests."""
	return {
		"got": got,
		"right": right,
		"sway": 0.005,
		"heard": 0.14,
		"pressedAfterHeard": pressedAfterHeard,
		"answers": [{"seq": 1, "t": 0.0, "given": given, "expired": False, "waited": []}],
	}


def raised(check: Callable[[list[dict]], None], results: list[dict]) -> type[BaseException] | None:
	try:
		check(results)
	except AssertionError as error:
		return type(error)
	return None


def test_theRaceChecksCallOnlyTheKnownFaultMisplaced():
	assert RACE_FAULT.mark.kwargs["raises"] is Misplaced
	toProbe = trialOf(TO_PROBE, TO_PROBE, [VERDICT_WAIT, 0])
	toFirefox = trialOf(TO_FIREFOX, TO_FIREFOX, [VERDICT_WAIT, 1])
	both = trialOf(TO_FIREFOX, BOTH_PLACES, [VERDICT_WAIT, 0])
	pageAlone = trialOf(TO_FIREFOX, ["page"], [VERDICT_WAIT, 0])
	probeInFirefox = trialOf(TO_PROBE, TO_FIREFOX, [VERDICT_WAIT, 1])
	for check in (landedRight, waitedForActivation, wentToOnePlace):
		assert raised(check, [toProbe, toFirefox] * 50) is None, check
		assert raised(check, [toProbe, both] * 50) is Misplaced, check
		assert raised(check, [toProbe, both] * 49 + [toProbe, pageAlone]) is AssertionError, check
		assert raised(check, [probeInFirefox, both] * 50) is AssertionError, check
	# At 30 ms a letter bound for the wrong window may slip through, so long as it goes to one place.
	assert (
		raised(wentToOnePlace, [toProbe, toFirefox] * 49 + [toProbe, trialOf(TO_FIREFOX, TO_PROBE, [0])])
		is None
	)
	assert raised(wentToOnePlace, [toProbe, toFirefox] * 49 + [toProbe, both]) is Misplaced


def test_theHeldBackCheckAsksAWaitOnlyOfLettersBoundForFirefox():
	passedToProbe = trialOf(TO_PROBE, TO_PROBE, [0], pressedAfterHeard=0.01)
	waitedInFirefox = trialOf(TO_FIREFOX, TO_FIREFOX, [VERDICT_WAIT, 1])
	passedInFirefox = trialOf(TO_FIREFOX, TO_FIREFOX, [1])
	lateInFirefox = trialOf(TO_FIREFOX, TO_FIREFOX, [VERDICT_WAIT, 1], pressedAfterHeard=0.01)
	assert raised(waitedForActivation, [passedToProbe, waitedInFirefox] * 50) is None
	for wrong in (passedInFirefox, lateInFirefox):
		results = [passedToProbe, waitedInFirefox] * 49 + [passedToProbe, wrong]
		assert raised(waitedForActivation, results) is AssertionError, wrong


def test_aSecondWindowOfTheSameProgramIsCounted(tmp_path: Path, socketDir: Path):  # noqa: F811
	"""Two Firefox windows of one process, the keys page and the usable page, and Alt+Tab between them
	with the letter 100 ms after: the capture follows the window as on X, so NVDA's focus decides in
	which document the letter moves. Counted, not held to: the count is for experiment E4's record."""
	webSession.requireBrowseLibraries()
	with waylandLinda(tmp_path, socketDir, probe=False) as session, altTabBound():
		feed = FeedProbe(tmp_path / "feed.jsonl")
		nvda = session.start(
			browseKeys.browseConfig(tmp_path),
			plugins={"browseKeysProbe": browseKeys.PROBE_PLUGIN, "feedProbe": FEED_PROBE},
			env={"LINDA_FEED_PROBE": str(feed.path)},
		)
		feed.waitFor("ready", FOLLOW_BOUND)
		webSession.startFirefox(session.desktop, "keys.html")
		nvda.waitForLog("Buffer rendered", browseKeys.BUFFER_BOUND)
		browseKeys.waitForCapture(nvda, "browse", timeout=browseKeys.BUFFER_BOUND)
		time.sleep(browseKeys.SETTLE)
		rendered = nvda.text("nvda.log").count("Buffer rendered")
		session.desktop.press("control+n", gap=browseKeys.SETTLE * 4)
		session.desktop.typeText(webSession.pageURL("usable.html"))
		session.desktop.press("Return")
		deadline = time.monotonic() + browseKeys.BUFFER_BOUND
		while nvda.text("nvda.log").count("Buffer rendered") == rendered:
			assert time.monotonic() < deadline, "the second window's page was never rendered"
			time.sleep(0.1)
		browseKeys.waitForCapture(nvda, "browse", timeout=browseKeys.BUFFER_BOUND)
		time.sleep(browseKeys.SETTLE)
		documents = {KEYS_WINDOW: "Keys page", USABLE_WINDOW: "Usable page"}
		sway = SwayFocus(tmp_path)
		counts: dict[str, int] = {}
		try:
			for _number in range(SAME_PROGRAM_TRIALS):
				keyboard = session.keyboard
				keyboard.down("alt")
				time.sleep(HOLD)
				tab = time.monotonic()
				keyboard.down("Tab")
				time.sleep(HOLD)
				keyboard.up("Tab")
				time.sleep(HOLD)
				released = time.monotonic()
				keyboard.up("alt")
				time.sleep(max(0.0, released + 0.1 - time.monotonic()))
				pressed = time.monotonic()
				keyboard.down(LETTER)
				time.sleep(HOLD)
				keyboard.up(LETTER)
				time.sleep(TRIAL_SETTLE * 3)
				swayed = sway.after(tab)
				wanted = documents.get(swayed[1]) if swayed else None
				moved = [record["document"] for record in feed.records("quickNav", since=pressed)]
				if not moved:
					outcome = "not moved"
				elif moved[0] == wanted:
					outcome = "moved in the window switched to"
				else:
					outcome = "moved in the window left"
				counts[outcome] = counts.get(outcome, 0) + 1
		finally:
			sway.stop()
		print(
			f"{sessionName()}: a second window of the same program, {SAME_PROGRAM_TRIALS} switches: {counts}"
		)
		assert sum(counts.values()) == SAME_PROGRAM_TRIALS
		session.quit()


class Window:
	"""A window E4 switches through: its name, what made it, and its program's process."""

	def __init__(self, name: str, toolkit: str, process: int) -> None:
		self.name = name
		self.toolkit = toolkit
		self.process = process


def test_windowEventTimingsOnAPrivateConnection(tmp_path: Path, socketDir: Path):  # noqa: F811
	"""Experiment E4: how long after the key that switches windows each toolkit says, on a connection of
	the test's own, that its window lost and gained the keyboard, with Linda idle and with it reading.
	The first word of a switch, whichever window gives it, is what the feed waits for, and it must come
	within FOCUS_MOVE_GRACE at the 99th percentile."""
	webSession.requireBrowseLibraries()
	with waylandLinda(tmp_path, socketDir) as session, altTabBound():
		desktop = session.desktop
		nvda = session.start(browseKeys.browseConfig(tmp_path), env=TIMED_READING)
		listener = Listener(tmp_path)
		try:
			windows = [Window(PROBE_WINDOW, "GTK 4", session.probe.process.pid)]
			gtk3 = desktop.startApp(desktop.fixture("gtkFixture"), title="Fixture window")
			windows.append(Window("Fixture window", "GTK 3", gtk3.pid))
			qt = desktop.startApp(desktop.fixture("qtFixture"), title="Qt fixture window")
			windows.append(Window("Qt fixture window", "Qt 6", qt.pid))
			firefox = webSession.startFirefox(desktop, "realSayAll.html")
			windows.append(Window(firefox.window, "Firefox", firefox.pid))
			chromium = chromiumSession.startChromium(desktop, "usable.html")
			windows.append(Window(chromium.window, "Chromium", chromium.pid))
			# A second window of the probe's own process: started again, GTK has the program running already
			# open the new window, and the second start ends.
			subprocess.run([sys.executable, str(KEY_PROBE)], check=True, capture_output=True, timeout=10)
			deadline = time.monotonic() + FOLLOW_BOUND
			while desktopSession.topLevelNames().count(PROBE_WINDOW) < 2:
				assert time.monotonic() < deadline, "the probe opened no second window"
				time.sleep(0.05)
			sway = SwayFocus(tmp_path)
			try:
				idle = switches(session, listener, sway, windows, reading=False)
				reading = switches(session, listener, sway, windows, reading=True)
			finally:
				sway.stop()
		finally:
			listener.stop()
		for name, timed in (("Linda idle", idle), ("Linda reading", reading)):
			for toolkit in sorted({t["toolkit"] for t in timed}):
				mine = [t for t in timed if t["toolkit"] == toolkit]
				print(
					f"{sessionName()}: E4, {name}, to {toolkit}: activate {summary([t['activate'] for t in mine if t['activate'] is not None])}, "
					f"deactivate of the window left {summary([t['deactivate'] for t in mine if t['deactivate'] is not None])}, "
					f"no activate {sum(t['activate'] is None for t in mine)}, no deactivate "
					f"{sum(t['deactivate'] is None for t in mine)}, of {len(mine)}"
				)
			same = [t for t in timed if t["sameProgram"]]
			print(
				f"{sessionName()}: E4, {name}, between two windows of one program: {len(same)} switches, events "
				f"per switch {[t['events'] for t in same]}"
			)
			first = [t["first"] for t in timed if t["first"] is not None]
			print(
				f"{sessionName()}: E4, {name}, the first word of each switch after Tab: {summary(first)}; after "
				f"sway's focus event: {summary([t['afterSway'] for t in timed if t['afterSway'] is not None])}"
			)
			assert len(first) == len(timed), [t for t in timed if t["first"] is None]
			assert percentile(first, 0.99) < focusMoveGrace()
		harness.assertMarkerAbsent(MARKER, {"nvda.log": nvda.text("nvda.log")})


def switches(
	session: WaylandLinda, listener: Listener, sway: SwayFocus, windows: list[Window], *, reading: bool
) -> list[dict]:
	"""E4_SWITCHES switches by the injector's Alt+Tab, straight to sway, each timed from Tab going down;
	reading, with Linda's say all started on the stand-in just before."""
	byName = {window.name: window for window in windows}
	timed = []
	left = sway.events[-1] if sway.events else None
	for _number in range(E4_SWITCHES):
		if reading:
			session.desktop.press("Insert+Down", gap=READING_BEFORE)
		tab = time.monotonic()
		waylandKeyboard.press("alt+Tab", gap=0, hold=HOLD)
		deadline = time.monotonic() + FOLLOW_BOUND
		while (swayed := sway.after(tab)) is None and time.monotonic() < deadline:
			time.sleep(0.01)
		assert swayed is not None, "sway moved no focus"
		time.sleep(TRIAL_SETTLE * 2)
		target = byName.get(swayed[1])
		events = [e for e in listener.events() if e["t"] >= tab]
		activate = next((e for e in events if e["type"] == "window:activate" and e["pid"] == swayed[2]), None)
		leftPid = left[2] if left is not None else None
		deactivate = next(
			(e for e in events if e["type"] == "window:deactivate" and leftPid and e["pid"] == leftPid), None
		)
		heard = [e["t"] for e in (activate, deactivate) if e is not None]
		timed.append(
			{
				"toolkit": target.toolkit if target is not None else swayed[1],
				"activate": activate["t"] - tab if activate else None,
				"deactivate": deactivate["t"] - tab if deactivate else None,
				"first": min(heard) - tab if heard else None,
				"afterSway": min(heard) - swayed[0] if heard else None,
				"sameProgram": leftPid == swayed[2],
				"events": [e["type"].split(":")[1] for e in events],
			}
		)
		left = swayed
	return timed
