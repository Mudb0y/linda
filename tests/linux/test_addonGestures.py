"""Keys a global plugin binds without the NVDA key, as the Speech History add-on binds F12, Shift+F12
and Shift+F11: each runs the plugin's script and never reaches the program in front, a page or not, on
the X route and on the kernel route, as on Windows; a key bound nowhere reaches the program at once,
never captured; a binding the plugin adds while NVDA runs is captured from then on; and a page's own
keys stay grabbed on its window while the focus is away, so that the first key after Alt+Tab back to
it is still browse mode's. In a Wayland session the keys outside a document are left as they were,
reaching the program as well. Run under the rig:

	nix develop . -c tests/linux/rig python3 -m pytest tests/linux/test_addonGestures.py
	LINDA_RIG_COMPOSITOR=sway nix develop . -c tests/linux/rig python3 -m pytest tests/linux/test_addonGestures.py
"""

import json
import shutil
import tempfile
import time
from collections.abc import Iterator
from contextlib import ExitStack
from pathlib import Path

import desktopSession
import harness
import pytest
import test_browseKeys as browseKeys
import webSession
from desktopSession import PREFIX_MARK, DesktopSession
from keydStandIn import StandIn, virtualKeyboardName
from test_kernelKeys import runKernelChild
from test_keys import STARTUP_TIMEOUT, Keyboard, configText
from test_waylandKeys import waylandLinda
from test_waylandMenu import KeyProbe

#: Seconds NVDA has to say what a key asked for, and the probe to report a key.
STEP_TIMEOUT = 5.0
#: Seconds after a binding changes before the keyboard watcher has surely looked again.
SETTLE = 0.3

#: The test's add-on: F12, Shift+F12 and Shift+F11 bound as Speech History binds them, NVDA+Shift+F9 to
#: bind F9 while NVDA runs, and a record, for each press of the keys the tests press, of whether it came
#: to NVDA captured, a key the program gets only if NVDA passes it.
PLUGIN = """
import json
import os

import globalPluginHandler
import ui
import winInputHook
from linuxPlatform import inputHook
from scriptHandler import script

RECORD = os.environ["LINDA_ADDON_KEYS_RECORD"]
#: Escape, X, F5, F9, F11 and F12.
WATCHED = {0x1B, 0x58, 0x74, 0x78, 0x7A, 0x7B}


class GlobalPlugin(globalPluginHandler.GlobalPlugin):
	def __init__(self):
		super().__init__()
		keyDown = winInputHook.keyDownCallback

		def recorded(vkCode, scanCode, extended, injected):
			event = inputHook.deliveringKey()
			if vkCode in WATCHED and event is not None and not event.repeat:
				with open(RECORD, "a", encoding="utf-8") as record:
					record.write(json.dumps({"vk": vkCode, "captured": event.captured}) + "\\n")
			return keyDown(vkCode, scanCode, extended, injected)

		winInputHook.keyDownCallback = recorded

	@script(gesture="kb:f12")
	def script_newest(self, gesture):
		ui.message("history newest")

	@script(gesture="kb:shift+f12")
	def script_next(self, gesture):
		ui.message("history next")

	@script(gesture="kb:shift+f11")
	def script_previous(self, gesture):
		ui.message("history previous")

	@script(gesture="kb:NVDA+shift+f9")
	def script_bindLate(self, gesture):
		self.bindGesture("kb:f9", "late")
		ui.message("late binding made")

	def script_late(self, gesture):
		ui.message("late script")

"""

ESCAPE, X_KEY, F5, F9, F11, F12 = 0x1B, 0x58, 0x74, 0x78, 0x7A, 0x7B
#: F12's X key code.
F12_CODE = 96
#: (chord, what the plugin says) of each key it binds.
BOUND = (("F12", "history newest"), ("shift+F12", "history next"), ("shift+F11", "history previous"))


def hooks(directory: Path) -> Path:
	hooksDir = directory / "hooks"
	(hooksDir / "globalPlugins").mkdir(parents=True)
	(hooksDir / "synthDrivers").symlink_to(harness.HERE / "synthDrivers")
	(hooksDir / "globalPlugins" / "addonKeys.py").write_text(PLUGIN, encoding="utf-8")
	return hooksDir


class AddonSession:
	"""The key probe focused, and NVDA beside it with the test's add-on, its keys from the X display or,
	with a stand-in, from the kernel through it."""

	def __init__(self, desktop: DesktopSession, tmpPath: Path, keyboard, standIn: StandIn | None) -> None:
		self.desktop = desktop
		self.keyboard = keyboard
		self.record = tmpPath / "presses.jsonl"
		self.record.touch()
		config = tmpPath / "nvda.ini"
		config.write_text(configText("desktop", 6))
		self.probe = KeyProbe(tmpPath)
		env = {"LINDA_TEST_HOOKS_DIR": str(hooks(tmpPath)), "LINDA_ADDON_KEYS_RECORD": str(self.record)}
		if standIn is not None:
			env |= {"LINDA_TEST_KEYD_SOCKET": standIn.path, "LINDA_TEST_KEYD_VIRTUAL": virtualKeyboardName()}
		self.launch = desktop.startNVDA(config, env=env)
		desktop.expect("Key probe" + PREFIX_MARK, since=0, timeout=STARTUP_TIMEOUT)
		if standIn is not None:
			standIn.waitFor(lambda: standIn.capturing, 10)
			self.launch.waitForLog(
				"Keys come from the kernel through linda-keyd: linda-keyd admitted NVDA", 5
			)
		time.sleep(SETTLE)

	def press(self, chord: str, *said: str) -> list[str]:
		"""Press chord, wait for NVDA to say said, then press F5, bound nowhere, and wait for the probe to
		have it, and so every key before it: what the probe was given from chord to F5."""
		since = len(self.probe.lines)
		mark = self.desktop.mark()
		self.keyboard.press(chord)
		if said:
			self.desktop.expect(*said, since=mark, timeout=STEP_TIMEOUT)
		self.keyboard.press("F5")
		self.probe.waitFor(lambda line: line == "pressed F5", STEP_TIMEOUT, since)
		return [line for line in self.probe.lines[since:] if line.startswith("pressed ")]

	def presses(self, vk: int) -> list[bool]:
		"""Whether each press of vk came to NVDA captured, in order."""
		records = [json.loads(line) for line in self.record.read_text(encoding="utf-8").splitlines()]
		return [record["captured"] for record in records if record["vk"] == vk]

	def quit(self) -> None:
		self.launch.process.terminate()
		self.launch.waitForExit(20)
		self.launch.assertClean(logNames=("nvda.log",))

	def close(self) -> None:
		self.probe.stop()


@pytest.fixture
def desktop(tmp_path: Path) -> Iterator[DesktopSession]:
	with DesktopSession(tmp_path / "desktop") as session:
		yield session


@pytest.fixture(params=("x11", "kernel"))
def addon(request: pytest.FixtureRequest, desktop: DesktopSession, tmp_path: Path) -> Iterator[AddonSession]:
	"""The session on the X route, typed on the rig's own keyboard, or on the kernel route, typed on the
	stand-in for linda-keyd."""
	if request.param == "x11":
		keyboard = Keyboard()
		try:
			session = AddonSession(desktop, tmp_path, keyboard, None)
			try:
				yield session
			finally:
				session.close()
		finally:
			keyboard.close()
		return
	socketDir = Path(tempfile.mkdtemp(prefix="keyd", dir="/tmp"))
	try:
		with StandIn(socketDir / "socket") as standIn:
			session = AddonSession(desktop, tmp_path, standIn, standIn)
			try:
				yield session
			finally:
				session.close()
	finally:
		shutil.rmtree(socketDir, ignore_errors=True)


@pytest.mark.rigSession("x11")
def test_boundKeysRunTheScriptAndNeverReachTheProgram(addon: AddonSession):
	leaked = {}
	for chord, said in BOUND:
		given = addon.press(chord, said)
		if f"pressed {chord.split('+')[-1]}" in given:
			leaked[chord] = given
	assert not leaked
	assert addon.presses(F12) == [True, True]
	assert addon.presses(F11) == [True]
	addon.quit()


@pytest.mark.rigSession("x11")
def test_unboundKeysReachTheProgramUncaptured(addon: AddonSession):
	"""F5, X and Escape are bound nowhere here: each reaches the probe, and came to NVDA as a key the
	program already had, with no wait for NVDA's answer."""
	given = addon.press("x")
	assert "pressed x" in given
	given = addon.press("Escape")
	assert "pressed Escape" in given
	assert addon.presses(X_KEY) == [False]
	assert addon.presses(ESCAPE) == [False]
	assert addon.presses(F5) == [False, False]
	addon.quit()


@pytest.mark.rigSession("x11")
def test_bindingAddedWhileRunningIsCaptured(addon: AddonSession):
	given = addon.press("F9")
	assert "pressed F9" in given
	mark = addon.desktop.mark()
	addon.keyboard.press("Insert+shift+F9")
	addon.desktop.expect("late binding made", since=mark, timeout=STEP_TIMEOUT)
	time.sleep(SETTLE)
	given = addon.press("F9", "late script")
	assert "pressed F9" not in given
	# The F9 between is the NVDA key's chord's.
	assert addon.presses(F9) == [False, True, True]
	addon.quit()


#: The kernel key source's child, F12 bound: the focus leaves the document's window for another
#: application's, where F12 and x are typed, and comes back, and h, browse mode's next heading, is typed
#: 50 ms after it is back, while NVDA's focus takes 150 ms to follow. NVDA says off outside the document,
#: as inputHook does: which window had which key, what NVDA was told of each, and what the X source
#: grabbed while the focus was away.
BOUND_AWAY_CHILD = r"""
F12 = 0x7B


def keysByWindow():
	keys = []
	while app.pending_events():
		event = app.next_event()
		if event.type == X.KeyPress:
			keys.append(["other" if event.window.id == other.id else "document", event.detail - 8])
	return keys


def told(vk):
	return [[e["captured"], e["reached"]] for e in presses(vk)]


other = root.create_window(0, 0, 100, 100, 0, app.screen().root_depth, event_mask=X.KeyPressMask)
other.change_property(pidAtom, Xatom.CARDINAL, 32, [2])
other.map()
app.sync()
source = startSource(ROUTE)
source.setBoundKeys(frozenset({(frozenset(), F12, False)}))
mode["now"] = "browse"
source.setCapture("browse", 1)
time.sleep(0.3)
other.set_input_focus(X.RevertToParent, X.CurrentTime)
app.sync()
time.sleep(0.2)
mode["now"] = "off"
source.setCapture("off")
time.sleep(0.3)
keysByWindow()
delivered.clear()
keyboard.press("F12", hold=0.03)
keyboard.press("x", hold=0.03)
time.sleep(0.4)
away = keysByWindow()
boundGrabs = sorted(source._x._boundGrabbed)
window.set_input_focus(X.RevertToParent, X.CurrentTime)
app.sync()


def follow():
	time.sleep(0.15)
	mode["now"] = "browse"
	source.setCapture("browse", 1)


threading.Thread(target=follow, daemon=True).start()
time.sleep(0.05)
keyboard.press("h", hold=0.03)
time.sleep(0.8)
result = {
	"kernel": source.isKernel,
	"away": away,
	"back": keysByWindow(),
	"boundGrabs": boundGrabs,
	"f12": told(F12),
	"x": told(ord("X")),
	"h": told(ord("H")),
}
source.stop()
keyboard.close()
"""


@pytest.mark.rigSession("x11")
@pytest.mark.parametrize("route", ("x11", "auto"))
def test_boundKeyFollowsTheFocusAndThePageKeepsItsKeys(tmp_path: Path, route: str):
	"""With F12 bound, the page's keys stay grabbed on its window while the focus is away, so that the
	first key typed after Alt+Tab back is still browse mode's, as test_kernelKeysReview's
	test_firstKeyBackInADocumentIsBrowseModes has it with nothing bound; and F12 is NVDA's in the other
	window, where x is that window's alone, never waiting for NVDA."""
	value = runKernelChild(tmp_path, f"ROUTE = {route!r}\n" + BOUND_AWAY_CHILD)
	assert value["kernel"] == (route == "auto")
	assert value["away"] == [["other", 45]], f"on the {route} route the other window had {value['away']}"
	assert value["f12"] == [[True, False]], value
	assert value["x"] == [[False, True]], value
	assert value["boundGrabs"] == [F12_CODE], value
	assert value["back"] == [], f"on the {route} route h reached the page instead of browse mode: {value}"
	assert value["h"] == [[True, False]], value


#: Seconds Firefox and NVDA have to build the page's buffer.
BUFFER_BOUND = browseKeys.BUFFER_BOUND


@pytest.mark.rigSession("x11")
@pytest.mark.parametrize("route", ("x11", "kernel"))
def test_pageKeepsItsKeysWithABoundKey(desktop: DesktopSession, tmp_path: Path, route: str):
	"""test_browseKeys.test_altTabLeavesThePageItsKeys with the add-on's F12 bound, on the X route and on
	the kernel route: the first key typed after Alt+Tab back to the page is browse mode's and never
	reaches the page; F12 runs the add-on's script in the other program and in the page, in browse mode
	and in focus mode, and the page never has it."""
	webSession.requireBrowseLibraries()
	with ExitStack() as stack:
		record = tmp_path / "presses.jsonl"
		record.touch()
		env = {"LINDA_TEST_HOOKS_DIR": str(hooks(tmp_path)), "LINDA_ADDON_KEYS_RECORD": str(record)}
		if route == "kernel":
			socketDir = Path(tempfile.mkdtemp(prefix="keyd", dir="/tmp"))
			stack.callback(shutil.rmtree, socketDir, ignore_errors=True)
			standIn = stack.enter_context(StandIn(socketDir / "socket"))
			board = standIn
			env |= {"LINDA_TEST_KEYD_SOCKET": standIn.path, "LINDA_TEST_KEYD_VIRTUAL": virtualKeyboardName()}
		else:
			board = Keyboard()
			stack.callback(board.close)
		desktop.startApp(desktop.fixture("gtkFixture"), title="Fixture window")
		desktopSession.activate("Fixture window")
		# To the text field, which holds "Alice".
		desktop.press("Tab", "Tab", "Tab", "Tab")
		webSession.startFirefox(desktop, "keys.html")
		nvda = desktop.startNVDA(browseKeys.browseConfig(tmp_path), env=env)
		nvda.waitForLog("Buffer rendered", BUFFER_BOUND)
		browseKeys.waitForCapture(nvda, "browse", timeout=BUFFER_BOUND)
		if route == "kernel":
			standIn.waitFor(lambda: standIn.capturing, 10)
			nvda.waitForLog("Keys come from the kernel through linda-keyd: linda-keyd admitted NVDA", 5)
		time.sleep(browseKeys.SETTLE)
		page = browseKeys.KeysPage()
		assert page.log() == []

		def step(chord: str, *expected: str) -> None:
			mark = desktop.mark()
			board.press(chord)
			browseKeys.expect(desktop, *expected, since=mark)
			time.sleep(browseKeys.SETTLE)

		def waitForLeftPage() -> None:
			# By what is captured, neither of the page's modes, whatever NVDA calls the mode outside.
			deadline = time.monotonic() + 5
			while browseKeys.captureMode(nvda) in ("browse", "focus"):
				assert time.monotonic() < deadline, f"NVDA's focus did not leave the page:\n{nvda.describe()}"
				time.sleep(0.05)

		def waitForField(letter: str) -> None:
			deadline = time.monotonic() + 3
			while letter not in (text := browseKeys.gtkEntryText()):
				assert time.monotonic() < deadline, f"{letter} did not reach the fixture's field: {text!r}"
				time.sleep(0.05)

		step("control+Home", "heading level 1 Keys page")
		step("F12", "history newest")
		step("alt+Tab", "Fixture window" + PREFIX_MARK)
		waitForLeftPage()
		step("F12", "history newest")
		board.press("x")
		waitForField("x")

		step("alt+Tab", "Keys page" + PREFIX_MARK)
		browseKeys.waitForCapture(nvda, "browse")
		step("control+Home", "heading level 1 Keys page")
		step("h", "Field and link heading level 2")
		assert page.log() == []

		# The first key after Alt+Tab is the new window's, though NVDA's focus follows the window only once
		# its application has told it: typing in the fixture, and NVDA's on the page.
		mark = desktop.mark()
		board.press("alt+Tab")
		board.press("y")
		browseKeys.expect(desktop, "Fixture window" + PREFIX_MARK, since=mark)
		waitForLeftPage()
		waitForField("y")
		time.sleep(browseKeys.SETTLE)
		mark = desktop.mark()
		board.press("alt+Tab")
		board.press("h")
		browseKeys.expect(desktop, "no next heading", since=mark)
		time.sleep(browseKeys.SETTLE)
		assert page.log() == [], f"on the {route} route the page had {page.log()}"

		# In focus mode, where the page has every other key.
		step("Tab", "Field edit" + PREFIX_MARK, "Focus mode")
		browseKeys.waitForCapture(nvda, "focus")
		step("F12", "history newest")
		board.press("a")
		page.waitForLog(["a"])
		step("Escape", "Browse mode")
		assert page.log() == ["a"]
		records = [json.loads(line) for line in record.read_text(encoding="utf-8").splitlines()]
		# In the page in browse mode, in the fixture, and in the page in focus mode: each NVDA's alone.
		assert [r["captured"] for r in records if r["vk"] == F12] == [True, True, True]
		step("Insert+q", "Exit Linda" + PREFIX_MARK)
		board.press("Return")
		nvda.waitForExit(10)
		nvda.assertClean(logNames=("nvda.log",))


@pytest.mark.rigSession("wayland")
def test_waylandLeavesKeysOutsideADocumentAsBefore(tmp_path: Path):
	"""On the Wayland route what is captured follows the capture feed's document, and outside one the
	add-on's key is left as it was: its script runs, and the program has the key too."""
	socketDir = Path(tempfile.mkdtemp(prefix="keyd", dir="/tmp"))
	try:
		with waylandLinda(tmp_path, socketDir) as linda:
			record = tmp_path / "presses.jsonl"
			record.touch()
			linda.start(plugins={"addonKeys": PLUGIN}, env={"LINDA_ADDON_KEYS_RECORD": str(record)})
			since = linda.probeLines()
			mark = linda.desktop.mark()
			linda.desktop.press("F12")
			linda.desktop.expect("history newest", since=mark, timeout=STEP_TIMEOUT)
			linda.probe.waitFor(lambda line: line == "pressed F12", STEP_TIMEOUT, since)
			linda.typeMarker()
			linda.quit()
	finally:
		shutil.rmtree(socketDir, ignore_errors=True)
