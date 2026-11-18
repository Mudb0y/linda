"""Remote Access between NVDA on the rig's display and a peer of the tests' own, through the relay
server NVDA hosts: keys the controlling computer sends are typed here and run NVDA's commands, what
NVDA says and plays goes back to it, and nothing is left held down once it has gone.

The rig has one display and one NVDA per display, so the other computer is tests/linux/remotePeer.py
speaking the protocol, as NVDA on Windows does. Run under the rig:

	nix develop . -c tests/linux/rig python3 -m pytest tests/linux/test_remoteAccess.py
"""

import signal
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import harness
import pytest
import remotePeer
from desktopSession import DesktopSession
from remotePeer import RemotePeer
from test_clipboard import readClipboard
from test_keys import ENTRY_TEXT, Keyboard, _DeviceFakeInput, configText, entryState, waitForEntry
from Xlib import display

#: Seconds NVDA has to start and speak the focus.
STARTUP_TIMEOUT = 60.0
PORT = 16839
CHANNEL = "rigchannel"
#: Windows virtual-key codes the peer sends, as NVDA's keyboard hook on Windows reports them.
VK_BACK = 0x08
VK_TAB = 0x09
VK_RETURN = 0x0D
VK_ESCAPE = 0x1B
VK_END = 0x23
VK_HOME = 0x24
VK_LEFT = 0x25
VK_RIGHT = 0x27
VK_INSERT = 0x2D
VK_DELETE = 0x2E
VK_NUMPAD1 = 0x61
VK_NUMPAD5 = 0x65
VK_DECIMAL = 0x6E
VK_F5 = 0x74
VK_F9 = 0x78
VK_LSHIFT = 0xA0
VK_LCONTROL = 0xA2
VK_LMENU = 0xA4
VK_NONE = 0xFF
#: Seconds between the events of a key, as a person types.
GAP = 0.03

PROBE_PLUGIN = """
import os
import time

import globalPluginHandler
import globalVars
import nvwave
import tones
import _remoteClient
from logHandler import log
from scriptHandler import script


class GlobalPlugin(globalPluginHandler.GlobalPlugin):
	def __init__(self):
		super().__init__()
		nvwave.decide_playWaveFile.register(self._logWave)

	def terminate(self):
		nvwave.decide_playWaveFile.unregister(self._logWave)

	def _logWave(self, fileName, **kwargs):
		log.info(f"remote probe: wave {fileName}")
		return True

	@script(gesture="kb:NVDA+shift+f9")
	def script_beep(self, gesture):
		tones.beep(880, 40)

	@script(gesture="kb:NVDA+shift+f10")
	def script_wave(self, gesture):
		nvwave.playWaveFile(os.path.join(globalVars.appDir, "waves", "exit.wav"))

	@script(gesture="kb:NVDA+shift+f11")
	def script_push(self, gesture):
		_remoteClient._remoteClient.pushClipboard()

	@script(gesture="kb:NVDA+shift+f12")
	def script_busy(self, gesture):
		log.info("remote probe: busy")
		time.sleep(1.5)
		log.info("remote probe: idle")
"""


def remoteConfig(directory: Path, mode: int) -> Path:
	"""A configuration directory for NVDA hosting the relay server and joining it, as follower with
	mode 0 and as leader with mode 1."""
	directory.mkdir(parents=True)
	(directory / "nvda.ini").write_text(
		configText("desktop", 6) + "[remote]\n\tenabled = True\n\t[[controlServer]]\n\t\tautoconnect = True\n"
		f"\t\tselfHosted = True\n\t\tconnectionMode = {mode}\n\t\tport = {PORT}\n\t\tkey = {CHANNEL}\n"
		"\t[[ui]]\n\t\tconfirmDisconnectAsFollower = False\n",
		encoding="utf-8",
	)
	return directory


def probeHooks(directory: Path) -> dict[str, str]:
	hooks = directory / "hooks"
	(hooks / "globalPlugins").mkdir(parents=True)
	(hooks / "synthDrivers").symlink_to(harness.HERE / "synthDrivers")
	(hooks / "globalPlugins" / "remoteProbe.py").write_text(PROBE_PLUGIN)
	return {"LINDA_TEST_HOOKS_DIR": str(hooks)}


def keysDown() -> list[int]:
	"""The key codes the rig's display has down."""
	connection = display.Display()
	try:
		keymap = connection.query_keymap()
	finally:
		connection.close()
	return [index * 8 + bit for index, byte in enumerate(keymap) for bit in range(8) if byte & (1 << bit)]


def waitForNoKeysDown(timeout: float = 3) -> None:
	deadline = time.monotonic() + timeout
	while down := keysDown():
		assert time.monotonic() < deadline, f"key codes {down} are still down on the display"
		time.sleep(0.05)


class Controlled:
	"""The GTK fixture with its text field focused, NVDA beside it as Remote Access's follower, and the
	peer joined as the leader controlling it."""

	def __init__(self, desktop: DesktopSession, tmpPath: Path) -> None:
		self.desktop = desktop
		desktop.startApp(desktop.fixture("gtkFixture"), title="Fixture window")
		desktop.startNVDA(remoteConfig(tmpPath / "config", 0), env=probeHooks(tmpPath))
		self.nvda = desktop.nvda
		desktop.expect("Fixture window", since=0, timeout=STARTUP_TIMEOUT)
		mark = desktop.mark()
		desktop.press("Tab", "Tab", "Tab", "Tab")
		desktop.expect("Name: edit\N{HORIZONTAL ELLIPSIS}", since=mark)
		assert entryState() == (ENTRY_TEXT, len(ENTRY_TEXT))
		self.nvda.waitForLog("Control connector connected", STARTUP_TIMEOUT)
		self.peer = RemotePeer(PORT, CHANNEL, remotePeer.LEADER)
		self.nvda.waitForLog("Client connected: {'id': 2", 5)

	def tap(self, vk: int, extended: bool = False, mods: tuple[int, ...] = ()) -> None:
		"""Press and release a key with modifiers held, as the leader's keyboard sends them."""
		for mod in mods:
			self.peer.key(mod, True, mod in (VK_INSERT,))
			time.sleep(GAP)
		self.peer.key(vk, True, extended)
		time.sleep(GAP)
		self.peer.key(vk, False, extended)
		time.sleep(GAP)
		for mod in reversed(mods):
			self.peer.key(mod, False, mod in (VK_INSERT,))
			time.sleep(GAP)

	def type(self, text: str) -> None:
		for char in text:
			self.tap(ord(char.upper()), mods=(VK_LSHIFT,) if char.isupper() else ())

	def close(self) -> harness.Launch:
		self.peer.close()
		launch = self.nvda
		launch.process.send_signal(signal.SIGTERM)
		launch.waitForExit(20)
		return launch


@pytest.fixture
def desktop(tmp_path: Path) -> Iterator[DesktopSession]:
	with DesktopSession(tmp_path / "desktop") as session:
		yield session


#: The X key code of the right Alt key, AltGr on a Polish layout. Its keysym also names a key no
#: keyboard has, which the X server would look up instead.
RIGHT_ALT_KEYCODE = 108


class AltGrKeyboard(Keyboard):
	"""The rig's keyboard, on which "AltGr" is the right Alt key."""

	def _send(self, key: str, down: bool) -> None:
		if key != "AltGr":
			super()._send(key, down)
			return
		_DeviceFakeInput(
			display=self._display.display,
			opcode=self._xtest,
			eventType=self._firstEvent + (1 if down else 2),
			detail=RIGHT_ALT_KEYCODE,
			time=0,
			root=0,
			x=0,
			y=0,
			deviceid=self._device,
		)
		self._display.sync()


@pytest.fixture
def keyboard() -> Iterator[Keyboard]:
	board = AltGrKeyboard()
	yield board
	board.close()


def test_controlledComputerTypesTheLeadersKeys(desktop: DesktopSession, tmp_path: Path):
	controlled = Controlled(desktop, tmp_path)
	try:
		controlled.type("Hi")
		waitForEntry("Hi")
		# Extended keys: Left and Delete take the i, Home goes before the H.
		controlled.tap(VK_LEFT, extended=True)
		controlled.tap(VK_DELETE, extended=True)
		controlled.tap(VK_HOME, extended=True)
		controlled.type("O")
		waitForEntry("OH")
		controlled.tap(VK_END, extended=True)
		controlled.tap(VK_BACK)
		waitForEntry("O")
		# Control+A selects the whole field, which the next key replaces.
		controlled.tap(ord("A"), mods=(VK_LCONTROL,))
		controlled.type("z")
		waitForEntry("z")
		# Numpad digits with Num Lock on at the leader, though the rig's display has it off.
		controlled.tap(VK_NUMPAD5)
		controlled.tap(VK_NUMPAD1)
		controlled.tap(VK_DECIMAL)
		waitForEntry("z51.")
		assert entryState() == ("z51.", 4)
		mark = controlled.peer.mark()
		desktopMark = desktop.mark()
		# The NVDA key with t is NVDA's title command, here, as on Windows: neither key reaches the field.
		controlled.tap(ord("T"), mods=(VK_INSERT,))
		desktop.expect("Fixture window", since=desktopMark)
		controlled.peer.waitFor(
			lambda m: m["type"] == "speak" and "Fixture window" in remotePeer.spokenText(m), since=mark
		)
		# Numpad Insert, as the leader sends it with Num Lock off, is the NVDA key too. Pressed again at
		# once, the command would spell the title instead.
		time.sleep(0.6)
		desktopMark = desktop.mark()
		controlled.peer.key(VK_INSERT, True, False)
		time.sleep(GAP)
		controlled.tap(ord("T"))
		controlled.peer.key(VK_INSERT, False, False)
		desktop.expect("Fixture window", since=desktopMark)
		# What the leader sends after a chord it ended with VK_NONE does nothing here.
		controlled.peer.key(VK_NONE, True)
		controlled.peer.key(VK_NONE, False)
		for key, extended in ((VK_ESCAPE, False), (VK_F5, False), (VK_RETURN, False), (VK_RIGHT, True)):
			controlled.tap(key, extended=extended)
		controlled.tap(VK_TAB, mods=(VK_LMENU,))
		time.sleep(0.5)
		assert entryState()[0] == "z51."
		waitForNoKeysDown()
	finally:
		launch = controlled.close()
	launch.assertClean(logNames=("nvda.log",))
	log = launch.text("nvda.log")
	for gesture in ("NVDA+t", "escape", "f5", "enter", "rightArrow", "alt+tab", "control+a"):
		assert f"Input: kb(desktop):{gesture}\n" in log, gesture
	assert "NoLinuxImplementation" not in log


def test_controlledComputerSendsWhatItSaysAndPlays(desktop: DesktopSession, tmp_path: Path):
	controlled = Controlled(desktop, tmp_path)
	peer = controlled.peer
	try:
		mark = peer.mark()
		controlled.tap(VK_F9, mods=(VK_INSERT, VK_LSHIFT))
		tone = peer.waitFor(lambda m: m["type"] == "tone", since=mark)
		assert (tone["hz"], tone["length"]) == (880, 40)
		controlled.tap(VK_F9 + 1, mods=(VK_INSERT, VK_LSHIFT))
		wave = peer.waitFor(lambda m: m["type"] == "wave", since=mark)
		assert wave["fileName"].endswith("exit.wav")
		# Control+alt+delete is Windows' own, and Linux has none to trigger.
		desktopMark = desktop.mark()
		peer.send("send_SAS")
		desktop.expect("Unable to trigger control plus alt plus delete", since=desktopMark)
		peer.waitFor(
			lambda m: m["type"] == "speak" and "Unable to trigger control" in remotePeer.spokenText(m),
			since=mark,
		)
		# The clipboard both ways.
		peer.send("set_clipboard_text", text="from the leader")
		waitForClipboard("from the leader")
		owner = ownClipboard("from this computer")
		try:
			mark = peer.mark()
			controlled.tap(VK_F9 + 2, mods=(VK_INSERT, VK_LSHIFT))
			pushed = peer.waitFor(lambda m: m["type"] == "set_clipboard_text", since=mark)
			assert pushed["text"] == "from this computer"
		finally:
			owner.terminate()
			owner.wait()
	finally:
		launch = controlled.close()
	launch.assertClean(logNames=("nvda.log",))


def test_leaderGoingAwayLeavesNoKeyDown(desktop: DesktopSession, tmp_path: Path):
	controlled = Controlled(desktop, tmp_path)
	controlled.peer.key(VK_LSHIFT, True)
	controlled.peer.key(VK_LCONTROL, True)
	deadline = time.monotonic() + 3
	while len(keysDown()) < 2:
		assert time.monotonic() < deadline, "the leader's modifiers did not go down"
		time.sleep(0.05)
	controlled.peer.close()
	waitForNoKeysDown()
	# The field has a plain letter typed on the display as it was, with no modifier of the leader's.
	subprocess.run(["xdotool", "key", "q"], check=True)
	waitForEntry("q")
	# Held again by a leader that has come back, and NVDA exiting with it still held.
	controlled.peer = RemotePeer(PORT, CHANNEL, remotePeer.LEADER)
	controlled.nvda.waitForLog("Client connected: {'id': 3", 5)
	controlled.peer.key(VK_LSHIFT, True)
	deadline = time.monotonic() + 3
	while not keysDown():
		assert time.monotonic() < deadline, "the leader's Shift did not go down"
		time.sleep(0.05)
	launch = controlled.nvda
	launch.process.send_signal(signal.SIGTERM)
	assert launch.waitForExit(20) == 0, launch.describe()
	waitForNoKeysDown()
	controlled.peer.close()
	launch.assertClean(logNames=("nvda.log",))


def test_keyPressedAsTheLeaderLeavesIsNotLeftDown(desktop: DesktopSession, tmp_path: Path):
	controlled = Controlled(desktop, tmp_path)
	# With NVDA's main thread busy, the leader's last key and its leaving wait there together.
	subprocess.run(["xdotool", "key", "Insert+shift+F12"], check=True)
	controlled.nvda.waitForLog("remote probe: busy", 5)
	controlled.peer.key(VK_LCONTROL, True)
	controlled.peer.close()
	controlled.nvda.waitForLog("remote probe: idle", 5)
	controlled.nvda.waitForLog("Leader client disconnected", 5)
	time.sleep(1)
	assert not keysDown(), "a key the leader pressed before leaving is held down on the display"
	subprocess.run(["xdotool", "key", "q"], check=True)
	waitForEntry("q")
	launch = controlled.nvda
	launch.process.send_signal(signal.SIGTERM)
	assert launch.waitForExit(20) == 0, launch.describe()
	launch.assertClean(logNames=("nvda.log",))


def startControlling(desktop: DesktopSession, tmpPath: Path) -> RemotePeer:
	"""The GTK fixture with its text field focused, NVDA beside it as Remote Access's leader, and the
	peer joined as the follower it controls."""
	desktop.startApp(desktop.fixture("gtkFixture"), title="Fixture window")
	desktop.startNVDA(remoteConfig(tmpPath / "config", 1), env=probeHooks(tmpPath))
	desktop.expect("Fixture window", since=0, timeout=STARTUP_TIMEOUT)
	mark = desktop.mark()
	desktop.press("Tab", "Tab", "Tab", "Tab")
	desktop.expect("Name: edit\N{HORIZONTAL ELLIPSIS}", since=mark)
	desktop.nvda.waitForLog("Successfully connected as leader", STARTUP_TIMEOUT)
	peer = RemotePeer(PORT, CHANNEL, remotePeer.FOLLOWER)
	desktop.nvda.waitForLog("Client connected: {'id': 2", 5)
	return peer


def keyMessages(peer: RemotePeer, mark: int) -> list[tuple[int, bool, bool, int | None]]:
	"""(vk, extended, pressed, scan code) of each key message the peer had after mark."""
	return [(m["vk_code"], m["extended"], m["pressed"], m.get("scan_code")) for m in peer.since(mark, "key")]


def tapped(vk: int, extended: bool, scan: int) -> list[tuple[int, bool, bool, int]]:
	return [(vk, extended, True, scan), (vk, extended, False, scan)]


def waitForClipboard(text: str) -> None:
	deadline = time.monotonic() + 5
	while True:
		try:
			if readClipboard() == text:
				return
		except subprocess.CalledProcessError:
			# Nothing owns the clipboard yet.
			pass
		assert time.monotonic() < deadline, f"the clipboard does not hold {text!r}"
		time.sleep(0.05)


def ownClipboard(text: str) -> subprocess.Popen:
	"""Have xclip own the clipboard with text, from a moment when nothing may own it."""
	process = subprocess.Popen(
		["xclip", "-quiet", "-selection", "clipboard", "-t", "UTF8_STRING", "-i"],
		stdin=subprocess.PIPE,
		stdout=subprocess.DEVNULL,
		stderr=subprocess.DEVNULL,
	)
	process.stdin.write(text.encode("utf-8"))
	process.stdin.close()
	waitForClipboard(text)
	return process


def test_controllingComputerSendsTheKeysTypedHere(
	desktop: DesktopSession, keyboard: Keyboard, tmp_path: Path
):
	peer = startControlling(desktop, tmp_path)
	try:
		mark = desktop.mark()
		keyboard.press("Insert+alt+Tab")
		desktop.expect("Controlling remote computer", since=mark)
		# The keyboard is taken for the follower within the key watcher's turn.
		time.sleep(0.3)
		keyMark = peer.mark()
		mark = desktop.mark()
		keyboard.press("shift+a", "b", "Delete", "control+Left", "F5", "KP_Enter", "Insert+t")
		expected = [
			(VK_LSHIFT, False, True, 0x2A),
			*tapped(ord("A"), False, 0x1E),
			(VK_LSHIFT, False, False, 0x2A),
			*tapped(ord("B"), False, 0x30),
			*tapped(VK_DELETE, True, 0x53),
			(VK_LCONTROL, False, True, 0x1D),
			*tapped(VK_LEFT, True, 0x4B),
			(VK_LCONTROL, False, False, 0x1D),
			*tapped(VK_F5, False, 0x3F),
			*tapped(VK_RETURN, True, 0x1C),
			(VK_INSERT, True, True, 0x52),
			*tapped(ord("T"), False, 0x14),
			(VK_INSERT, True, False, 0x52),
		]
		deadline = time.monotonic() + 5
		while len(keyMessages(peer, keyMark)) < len(expected):
			assert time.monotonic() < deadline, keyMessages(peer, keyMark)
			time.sleep(0.05)
		assert keyMessages(peer, keyMark) == expected
		time.sleep(0.3)
		# None of them acted here: the field is as it was, and NVDA+t said nothing.
		assert entryState()[0] == ENTRY_TEXT
		assert "Fixture window" not in desktop.spokenSince(mark)
		# What the follower says and plays is heard here.
		peer.send("speak", sequence=["Hello from the follower"], priority=0)
		desktop.expect("Hello from the follower", since=mark)
		peer.send("tone", hz=440, length=50, left=50, right=50)
		desktop.nvda.waitForLog("Beep at pitch 440, for 50 ms", 5)
		# A Windows follower names its own copy's sound, which is played from this one's.
		peer.send("wave", fileName="C:\\Program Files (x86)\\NVDA\\waves\\exit.wav", asynchronous=True)
		desktop.nvda.waitForLog("remote probe: wave /", 5)
		# Switched back, the keys act here again, and the follower is told the modifiers are up.
		mark = desktop.mark()
		keyMark = peer.mark()
		keyboard.press("Insert+alt+Tab")
		desktop.expect("Controlling local computer", since=mark)
		time.sleep(0.3)
		keyboard.press("x")
		waitForEntry("x")
		sent = keyMessages(peer, keyMark)
		assert (VK_INSERT, True, False, None) in sent
		assert (VK_LMENU, False, False, None) in sent
		assert not any(vk == ord("X") for vk, *_rest in sent)
		# The clipboard both ways.
		owner = ownClipboard("from the leader")
		try:
			clipMark = peer.mark()
			keyboard.press("Insert+shift+F11")
			pushed = peer.waitFor(lambda m: m["type"] == "set_clipboard_text", since=clipMark)
			assert pushed["text"] == "from the leader"
		finally:
			owner.terminate()
			owner.wait()
		peer.send("set_clipboard_text", text="from the follower")
		waitForClipboard("from the follower")
		# Exiting while the keys go to the follower leaves the keyboard to the applications.
		mark = desktop.mark()
		keyboard.press("Insert+alt+Tab")
		desktop.expect("Controlling remote computer", since=mark)
		time.sleep(0.3)
		launch = desktop.nvda
		launch.process.send_signal(signal.SIGTERM)
		assert launch.waitForExit(20) == 0, launch.describe()
		keyboard.press("y")
		waitForEntry("xy")
		waitForNoKeysDown()
	finally:
		peer.close()
	launch.assertClean(logNames=("nvda.log",))


VK_RMENU = 0xA5
#: The scan code Windows' hook gives the left Control it reports with AltGr.
ALTGR_CONTROL_SCAN = 0x21D


def test_controlledComputerTypesTheLeadersAltGrLetters(desktop: DesktopSession, tmp_path: Path):
	subprocess.run(["setxkbmap", "pl"], check=True)
	try:
		controlled = Controlled(desktop, tmp_path)
		try:
			# AltGr+a on a Windows leader with a Polish layout, as its keyboard hook reports it.
			peer = controlled.peer
			for vk, extended, pressed, scan in (
				(VK_LCONTROL, False, True, ALTGR_CONTROL_SCAN),
				(VK_RMENU, True, True, 0x38),
				(ord("A"), False, True, 0x1E),
				(ord("A"), False, False, 0x1E),
				(VK_LCONTROL, False, False, ALTGR_CONTROL_SCAN),
				(VK_RMENU, True, False, 0x38),
			):
				peer.key(vk, pressed, extended, scan)
				time.sleep(GAP)
			# The next letter comes without AltGr.
			controlled.tap(ord("L"))
			waitForEntry("ąl")
			waitForNoKeysDown()
		finally:
			launch = controlled.close()
		launch.assertClean(logNames=("nvda.log",))
	finally:
		subprocess.run(["setxkbmap", "us"], check=True)


def test_controllingComputerSendsAltGrAsWindowsReportsIt(
	desktop: DesktopSession, keyboard: Keyboard, tmp_path: Path
):
	subprocess.run(["setxkbmap", "pl"], check=True)
	try:
		peer = startControlling(desktop, tmp_path)
		try:
			mark = desktop.mark()
			keyboard.press("Insert+alt+Tab")
			desktop.expect("Controlling remote computer", since=mark)
			time.sleep(0.3)
			keyMark = peer.mark()
			keyboard.press("AltGr+a")
			expected = [
				(VK_LCONTROL, False, True, ALTGR_CONTROL_SCAN),
				(VK_RMENU, True, True, 0x38),
				*tapped(ord("A"), False, 0x1E),
				(VK_LCONTROL, False, False, ALTGR_CONTROL_SCAN),
				(VK_RMENU, True, False, 0x38),
			]
			deadline = time.monotonic() + 5
			while len(keyMessages(peer, keyMark)) < len(expected):
				assert time.monotonic() < deadline, keyMessages(peer, keyMark)
				time.sleep(0.05)
			assert keyMessages(peer, keyMark) == expected
			assert entryState()[0] == ENTRY_TEXT
			launch = desktop.nvda
			launch.process.send_signal(signal.SIGTERM)
			assert launch.waitForExit(20) == 0, launch.describe()
		finally:
			peer.close()
		launch.assertClean(logNames=("nvda.log",))
	finally:
		subprocess.run(["setxkbmap", "us"], check=True)


def test_controlledComputerTypesKeysThatArriveTogether(desktop: DesktopSession, tmp_path: Path):
	# A slow network hands over several of the leader's key messages at once.
	controlled = Controlled(desktop, tmp_path)
	peer = controlled.peer
	try:
		events = [(VK_LSHIFT, True), (ord("H"), True), (ord("H"), False), (VK_LSHIFT, False)]
		for char in "ello":
			events += [(ord(char.upper()), True), (ord(char.upper()), False)]
		for vk, pressed in events:
			peer.key(vk, pressed)
		waitForEntry("Hello")
		desktopMark = desktop.mark()
		for vk, pressed, extended in (
			(VK_INSERT, True, True),
			(ord("T"), True, False),
			(ord("T"), False, False),
			(VK_INSERT, False, True),
		):
			peer.key(vk, pressed, extended)
		desktop.expect("Fixture window", since=desktopMark)
		time.sleep(0.3)
		assert entryState()[0] == "Hello"
		waitForNoKeysDown()
	finally:
		launch = controlled.close()
	launch.assertClean(logNames=("nvda.log",))
