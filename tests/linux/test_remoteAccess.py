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
from test_clipboard import ownClipboard, readClipboard
from test_keys import ENTRY_TEXT, configText, entryState, waitForEntry
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

import api
import globalPluginHandler
import globalVars
import nvwave
import tones
import _remoteClient
from scriptHandler import script


class GlobalPlugin(globalPluginHandler.GlobalPlugin):
	@script(gesture="kb:NVDA+shift+f9")
	def script_beep(self, gesture):
		tones.beep(880, 40)

	@script(gesture="kb:NVDA+shift+f10")
	def script_wave(self, gesture):
		nvwave.playWaveFile(os.path.join(globalVars.appDir, "waves", "exit.wav"))

	@script(gesture="kb:NVDA+shift+f11")
	def script_push(self, gesture):
		_remoteClient._remoteClient.pushClipboard()
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
		deadline = time.monotonic() + 5
		while True:
			try:
				if readClipboard() == "from the leader":
					break
			except subprocess.CalledProcessError:
				# Nothing owns the clipboard yet.
				pass
			assert time.monotonic() < deadline, "the leader's text did not reach the clipboard"
			time.sleep(0.05)
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
