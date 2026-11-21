"""The desktop tests read applications on: the window manager, the fixture applications and the
browsers on the accessibility desktop, keys typed into them on the rig's display or on a stand-in for
linda-keyd, the keyboard given to one window and another, and NVDA started beside them.

On the X display, and in each of the rig's Wayland sessions, where every application must be the
compositor's own client and none may reach the compositor's Xwayland:

nix develop . -c tests/linux/rig python3 -m pytest tests/linux/test_desktopSession.py
LINDA_RIG_COMPOSITOR=sway LINDA_RIG_XWAYLAND=1 nix develop . -c tests/linux/rig python3 -m pytest \
	tests/linux/test_desktopSession.py
"""

import os
import subprocess
import tempfile
import time
from collections.abc import Iterable, Iterator
from pathlib import Path

import chromiumSession
import desktopSession
import harness
import keydStandIn
import pytest
import waylandKeyboard
import webSession
from desktopSession import DesktopSession
from keydStandIn import StandIn

BOOT_CONFIG = harness.HERE / "fixtures" / "bootConfig" / "nvda.ini"
#: Seconds from starting NVDA to its first words; a start under the rig takes about three.
STARTUP_BOUND = 60.0
#: Seconds a key pressed has to reach the application or light the lock.
KEY_BOUND = 2.0
#: Seconds the window manager has to move the keyboard to the window asked for.
ACTIVATE_BOUND = 10.0
#: The Electron fixture's window.
CHAT_TITLE = "Chat fixture"
#: Processes the session may not have started in the rig's Wayland sessions.
X_WINDOW_MANAGERS = ("marco", "Xvfb")

wayland = pytest.mark.rigSession("wayland")


@pytest.fixture(params=[False, True], ids=["display", "standIn"])
def standIn(request: pytest.FixtureRequest) -> Iterator[StandIn | None]:
	"""None, for keys pressed on the rig's display, or a stand-in with no client, which puts every key
	out at once on the display's keyboard."""
	if not request.param:
		yield None
		return
	sink = None if desktopSession.compositor() is None else keydStandIn.compositorSink()
	# The pytest directory's path is too long for a socket's.
	with tempfile.TemporaryDirectory() as directory, StandIn(Path(directory) / "keyd", sink=sink) as keyboard:
		yield keyboard


@pytest.fixture
def desktop(tmp_path: Path, standIn: StandIn | None) -> Iterator[DesktopSession]:
	with DesktopSession(tmp_path / "desktop", standIn=standIn) as session:
		yield session


def _processNames() -> set[str]:
	names = set()
	for entry in Path("/proc").iterdir():
		try:
			names.add((entry / "comm").read_text(encoding="utf-8").strip())
		except OSError:
			pass
	return names


def _xClientProcesses() -> set[int]:
	"""The process IDs of the X display's clients, this process's own query among them."""
	from Xlib import display
	from Xlib.ext import res

	connection = display.Display()
	try:
		ids = connection.res_query_client_ids([{"client": 0, "mask": res.LocalClientPIDMask}])
		return {value.value[0] for value in ids.ids if value.spec.mask == res.LocalClientPIDMask}
	finally:
		connection.close()


def _assertWaylandClients(processes: Iterable[int]) -> None:
	"""In a Wayland session, that none of processes is a client of the compositor's Xwayland."""
	if desktopSession.compositor() is None:
		return
	assert harness.rigSession() != "x11" and "Xvfb" not in _processNames()
	if not harness.rigHasXwayland():
		# The application is on the desktop with no X display to be on.
		assert "DISPLAY" not in os.environ
		return
	clients = _xClientProcesses()
	# The query's own connection: an empty answer would prove nothing.
	assert os.getpid() in clients, clients
	assert not set(processes) & clients, f"X clients {sorted(set(processes) & clients)}"


def _waitFor(condition, bound: float, what: str) -> None:
	deadline = time.monotonic() + bound
	while not condition():
		assert time.monotonic() < deadline, f"{what} within {bound} s"
		time.sleep(0.05)


@pytest.mark.rigSession("x11")
@pytest.mark.parametrize("standIn", [False], ids=["display"], indirect=True)
def test_windowManagerIsMarco(desktop: DesktopSession):
	shown = subprocess.run(["wmctrl", "-m"], capture_output=True, text=True, check=True).stdout
	# It names itself "Metacity (Marco)", after the window manager it was forked from.
	assert shown.startswith("Name: ") and "Marco" in shown.splitlines()[0], shown


@wayland
@pytest.mark.parametrize("standIn", [False], ids=["display"], indirect=True)
def test_compositorIsTheWindowManager(desktop: DesktopSession):
	assert desktopSession.compositor() == harness.rigSession() in ("mutter", "sway")
	assert desktopSession.windowManagerIsRunning()
	assert desktop._windowManager is None
	names = _processNames()
	assert not names & set(X_WINDOW_MANAGERS), names
	# Nix's wrapper makes Mutter .mutter-wrapped.
	assert any(harness.rigSession() in name for name in names), names


@pytest.mark.parametrize("standIn", [False], ids=["display"], indirect=True)
@pytest.mark.parametrize(
	("name", "args", "title"),
	[
		("gtkFixture", (), "Fixture window"),
		("qtFixture", (), "Qt fixture window"),
		("wxfixture", (), "wx fixture"),
		("gtk4", (), "GTK4 fixture"),
	],
)
def test_fixtureAppearsOnTheAccessibilityDesktop(
	desktop: DesktopSession,
	name: str,
	args: tuple[str, ...],
	title: str,
):
	app = desktop.startApp(desktop.fixture(name, *args), title=title, timeout=5)
	assert app.poll() is None
	_assertWaylandClients([app.pid])


@pytest.mark.parametrize("standIn", [False], ids=["display"], indirect=True)
@pytest.mark.parametrize("browser", ["firefox", "chromium", "electron"])
def test_browserWindowAppearsOnTheAccessibilityDesktop(desktop: DesktopSession, browser: str):
	"""Each browser starts on a page, its window named on the accessibility desktop and given the
	keyboard, as a Wayland client under a compositor."""
	if browser == "firefox":
		process = webSession.startFirefox(desktop, "basic.html")
		assert process.window.startswith("Basic page")
		if desktopSession.compositor() is not None:
			environment = Path(f"/proc/{process.pid}/environ").read_bytes().split(b"\0")
			assert b"MOZ_ENABLE_WAYLAND=1" in environment
			assert not any(entry.startswith(b"DISPLAY=") for entry in environment)
	else:
		# Chromium joins the accessibility bus only if the status was on when it started, and the rig's
		# own setting may have been switched off by an earlier test in the run.
		if chromiumSession.accessibilityStatus() is not True:
			chromiumSession._status("Set", "string:IsEnabled", "variant:boolean:true")
		assert chromiumSession.accessibilityStatus() is True
		if browser == "chromium":
			process = chromiumSession.startChromium(desktop, "basic.html")
			assert process.window == "Basic page" + chromiumSession.CHROMIUM_TITLE_SUFFIX
		else:
			process = chromiumSession.startElectron(desktop, "electronChat", window=CHAT_TITLE)
		# Chromium writes its whole command line over its first argument.
		command = Path(f"/proc/{process.pid}/cmdline").read_bytes()
		assert (b"--ozone-platform=wayland" in command) is (desktopSession.compositor() is not None)
	assert desktopSession.activeTopLevelNames() == [process.window]
	_assertWaylandClients(process.liveProcesses())


def _entryText(windowTitle: str) -> str:
	"""The text of the first editable text field in the window, read through AT-SPI."""
	Atspi = desktopSession._atspi()
	desktop = Atspi.get_desktop(0)
	for appIndex in range(desktop.get_child_count()):
		app = desktop.get_child_at_index(appIndex)
		for index in range(app.get_child_count() if app else 0):
			window = app.get_child_at_index(index)
			if window is not None and window.get_name() == windowTitle:
				found = _firstEntry(window)
				assert found is not None, f"{windowTitle!r} has no editable text field"
				return Atspi.Text.get_text(found, 0, -1)
	raise AssertionError(f"no window {windowTitle!r}")


def _firstEntry(node):
	if node.get_role_name() == "text" and node.get_state_set().contains(
		desktopSession._atspi().StateType.EDITABLE
	):
		return node
	for index in range(node.get_child_count()):
		child = node.get_child_at_index(index)
		if child is not None and (found := _firstEntry(child)) is not None:
			return found
	return None


def _typeIntoTheGtkEntry(desktop: DesktopSession, text: str) -> str:
	"""What the GTK fixture's entry holds after text was typed over its first text."""
	app = desktop.startApp(desktop.fixture("gtkFixture"), title="Fixture window")
	# It is on the accessibility desktop before it has presented itself and has the keyboard.
	assert app.stdout.readline() == "ready\n"
	desktopSession.activate("Fixture window", timeout=ACTIVATE_BOUND)
	# The mnemonic of the entry's label, which focuses the entry and selects all of "Alice".
	desktop.press("alt+n")
	desktop.typeText(text)
	found = ""
	for _ in range(40):
		found = _entryText("Fixture window")
		if found == text:
			break
		time.sleep(0.05)
	return found


def _putOut(standIn: StandIn) -> list[tuple[int, bool]]:
	"""(code, down) of each key the stand-in put out."""
	return [(event[2], event[3]) for event in standIn.events if event[1] == "x"]


def test_typedTextReachesTheGtkEntry(desktop: DesktopSession, standIn: StandIn | None):
	assert _typeIntoTheGtkEntry(desktop, "Bob") == "Bob"
	if standIn is not None:
		shift, b, o, n, alt = 42, 48, 24, 49, 56
		assert _putOut(standIn) == [
			(alt, True),
			(n, True),
			(n, False),
			(alt, False),
			(shift, True),
			(b, True),
			(b, False),
			(shift, False),
			(o, True),
			(o, False),
			(b, True),
			(b, False),
		]


def _recordStandInPresses(standIn: StandIn | None, monkeypatch: pytest.MonkeyPatch) -> list[str]:
	"""The chords the session hands to StandIn.press from now on, each still pressed."""
	chords: list[str] = []
	if standIn is not None:
		press = standIn.press

		def recorded(*given: str, **options) -> None:
			chords.extend(given)
			press(*given, **options)

		monkeypatch.setattr(standIn, "press", recorded)
	return chords


def test_chordByTheDriversNameGoesThroughTheStandIn(
	desktop: DesktopSession, standIn: StandIn | None, monkeypatch: pytest.MonkeyPatch
):
	"""xdotool's "ctrl", which the stand-in names too, selects all of the entry's text."""
	assert _typeIntoTheGtkEntry(desktop, "Bob") == "Bob"
	chords = _recordStandInPresses(standIn, monkeypatch)
	desktop.press("ctrl+a")
	desktop.typeText("Al")
	_waitFor(
		lambda: _entryText("Fixture window") == "Al", KEY_BOUND, "Ctrl+A did not select the entry's text"
	)
	if standIn is not None:
		assert chords == ["ctrl+a"]
		control, a = 29, 30
		# After Alt+N's four and Bob's eight.
		assert _putOut(standIn)[12:16] == [(control, True), (a, True), (a, False), (control, False)]


def test_chordWithANameTheStandInLacks(
	desktop: DesktopSession, standIn: StandIn | None, monkeypatch: pytest.MonkeyPatch
):
	"""xdotool's "question", a key symbol the stand-in has no key by, is pressed key by key."""
	assert _typeIntoTheGtkEntry(desktop, "Bob") == "Bob"
	chords = _recordStandInPresses(standIn, monkeypatch)
	desktop.press("question")
	_waitFor(lambda: _entryText("Fixture window") == "Bob?", KEY_BOUND, "question did not type ?")
	if standIn is not None:
		assert chords == []
		shift, slash = 42, 53
		assert _putOut(standIn)[12:16] == [(shift, True), (slash, True), (slash, False), (shift, False)]


@wayland
def test_typedTextIsTypedInThePlLayout(desktop: DesktopSession, standIn: StandIn | None):
	"""Capitals, AltGr's letters and the punctuation on Shift, as the compositor's pl keymap gives them."""
	text = "Źdźbło: ą, Ę? (x_y) 2+2*$"
	assert _typeIntoTheGtkEntry(desktop, text) == text
	assert not desktopSession.capsLockIsOn()
	if standIn is not None:
		expected = []
		for character in text:
			codes = desktopSession.characterCodes(character)
			expected += [(code, True) for code in codes] + [(code, False) for code in reversed(codes)]
		# After Alt+N's four.
		assert _putOut(standIn)[4:] == expected


@pytest.mark.parametrize("standIn", [False], ids=["display"], indirect=True)
def test_keyNamesAndCharactersFindTheirKeys(desktop: DesktopSession):
	assert desktopSession.chordCodes("alt+Tab") == [56, 15]
	assert desktopSession.chordCodes("Insert+t") == [110, 20]
	assert desktopSession.chordCodes("KP_Up") == [72]
	# Names the rig's keyboard has no code for, by the key symbol, with what selects its level.
	assert desktopSession.chordCodes("question") == [42, 53]
	assert desktopSession.chordCodes("shift+question") == [42, 53]
	assert desktopSession.chordCodes("ctrl+aogonek") == [29, 100, 30]
	assert desktopSession.characterCodes("a") == (30,)
	assert desktopSession.characterCodes("A") == (42, 30)
	assert desktopSession.characterCodes("ą") == (100, 30)
	assert desktopSession.characterCodes("Ł") == (42, 100, 38)
	assert desktopSession.characterCodes(" ") == (57,)
	assert desktopSession.characterCodes("\n") == (28,)
	# On the main block, as xdotool types them, though the keypad's plus and asterisk, which NVDA takes
	# as commands, and keys such as KEY_DOLLAR need no Shift.
	assert desktopSession.characterCodes("+") == (42, 13)
	assert desktopSession.characterCodes("*") == (42, 9)
	assert desktopSession.characterCodes("$") == (42, 5)
	assert desktopSession.characterCodes("#") == (42, 4)
	assert desktopSession.characterCodes("(") == (42, 10)
	for character in "0123456789+-*/.=,<>()$#\r\n":
		assert max(desktopSession.characterCodes(character)) <= 57, character
	with pytest.raises(ValueError):
		desktopSession.characterCodes("\N{SNOWMAN}")
	with pytest.raises(ValueError):
		desktopSession.chordCodes("NoSuchKey")


@pytest.mark.parametrize("standIn", [False], ids=["display"], indirect=True)
def test_aKeyThatFailsLetsGoOfTheModifiersHeld(desktop: DesktopSession, monkeypatch: pytest.MonkeyPatch):
	sent: list[tuple[int, bool]] = []

	def key(code: int, pressed: bool) -> None:
		if code == 9 and pressed:
			raise RuntimeError("the injector answered an error")
		sent.append((code, pressed))

	monkeypatch.setattr(waylandKeyboard, "key", key)
	with pytest.raises(RuntimeError):
		desktop._pressCodes([42, 100, 9])
	assert sent == [(42, True), (100, True), (100, False), (42, False)]


@pytest.mark.rigSession("sway")
@pytest.mark.parametrize("standIn", [False], ids=["display"], indirect=True)
@pytest.mark.parametrize("answer", ["exit 1", "exec sleep 30"], ids=["fails", "hangs"])
def test_activateIsBoundedWhenSwayDoesNotAnswer(
	desktop: DesktopSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, answer: str
):
	fake = tmp_path / "bin" / "swaymsg"
	fake.parent.mkdir()
	fake.write_text(f"#!/bin/sh\n{answer}\n")
	fake.chmod(0o755)
	monkeypatch.setenv("PATH", f"{fake.parent}:{os.environ['PATH']}")
	monkeypatch.setattr(desktopSession, "SWAYMSG_TIMEOUT", 0.5)
	started = time.monotonic()
	with pytest.raises(AssertionError, match="did not become active"):
		desktopSession.activate("No such window", timeout=1)
	assert time.monotonic() - started < 5


@pytest.mark.parametrize("standIn", [False], ids=["display"], indirect=True)
def test_activateGivesEachWindowTheKeyboard(desktop: DesktopSession):
	"""Under Mutter, which has no way to name a window, the oldest of three needs the most switching."""
	titles = ["Fixture window", "GTK4 fixture", "wx fixture"]
	for name, title in zip(("gtkFixture", "gtk4", "wxfixture"), titles, strict=True):
		desktop.startApp(desktop.fixture(name), title=title)
	for title in [*titles, titles[1], titles[0]]:
		desktopSession.activate(title, timeout=ACTIVATE_BOUND)
		assert desktopSession.activeTopLevelNames() == [title]


def test_capsLockIsFollowed(desktop: DesktopSession, standIn: StandIn | None):
	assert not desktopSession.capsLockIsOn()
	try:
		desktop.press("Caps_Lock")
		_waitFor(desktopSession.capsLockIsOn, KEY_BOUND, "Caps Lock did not light")
		desktop.press("Caps_Lock")
		_waitFor(lambda: not desktopSession.capsLockIsOn(), KEY_BOUND, "Caps Lock did not go out")
	finally:
		if desktopSession.capsLockIsOn():
			desktop.press("Caps_Lock")
	if standIn is not None:
		assert _putOut(standIn) == [(58, True), (58, False)] * 2


@pytest.mark.parametrize("standIn", [False], ids=["display"], indirect=True)
def test_nvdaSpeaksTheFixturesFirstControl(desktop: DesktopSession, tmp_path: Path):
	desktop.startApp(desktop.fixture("gtkFixture"), title="Fixture window")
	config = tmp_path / "nvda.ini"
	text = BOOT_CONFIG.read_text(encoding="utf-8")
	# The welcome dialog would take the focus from the fixture.
	config.write_text(
		text.replace("[general]\n", "[general]\n\tshowWelcomeDialogAtStartup = False\n", 1),
		encoding="utf-8",
	)
	nvda = desktop.startNVDA(config)
	# The transcript holds what the synthesizer is given, after symbols are spoken as words.
	desktop.expect(
		"Fixture window", "Press me button Does nothing Alt plus P", since=0, timeout=STARTUP_BOUND
	)
	desktop.close()
	assert nvda.process.returncode == 0, nvda.describe()
	# Under a compositor NVDA's own start is not yet clean: the mouse handler asks for the X pointer, and
	# GTK warns about a window of NVDA's. Those are the Wayland switch-offs' and NVDA's own windows'
	# to mend; what this proves there is that the driver puts a native window in front of NVDA to read.
	# The checks those two do not touch still hold there.
	if desktopSession.compositor() is None:
		nvda.assertClean(logNames=("nvda.log",))
	else:
		nvda.assertClean(ignoring=("noLinuxImplementation", "logErrors", "stderr"))
