"""Tests for .nvda-addon FreeDesktop MIME association, CLI normalization, and controlSocket IPC."""

import os
import sys
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SOURCE_DIR = REPO_ROOT / "source"
if str(SOURCE_DIR) not in sys.path:
	sys.path.insert(0, str(SOURCE_DIR))


class TestAddonMimeAndDesktop(unittest.TestCase):
	def test_mimePackageXml(self):
		mimePath = REPO_ROOT / "linux" / "linda-mime.xml"
		self.assertTrue(mimePath.is_file(), f"MIME file not found at {mimePath}")
		tree = ET.parse(mimePath)
		root = tree.getroot()
		self.assertEqual(root.tag, "{http://www.freedesktop.org/standards/shared-mime-info}mime-info")
		mimeTypeElem = root.find("{http://www.freedesktop.org/standards/shared-mime-info}mime-type")
		self.assertIsNotNone(mimeTypeElem)
		self.assertEqual(mimeTypeElem.attrib.get("type"), "application/x-nvda-addon")
		subclassElem = mimeTypeElem.find("{http://www.freedesktop.org/standards/shared-mime-info}sub-class-of")
		self.assertIsNotNone(subclassElem)
		self.assertEqual(subclassElem.attrib.get("type"), "application/zip")
		globElem = mimeTypeElem.find("{http://www.freedesktop.org/standards/shared-mime-info}glob")
		self.assertIsNotNone(globElem)
		self.assertEqual(globElem.attrib.get("pattern"), "*.nvda-addon")

	def test_desktopEntry(self):
		desktopPath = REPO_ROOT / "linux" / "linda.desktop"
		self.assertTrue(desktopPath.is_file())
		content = desktopPath.read_text(encoding="utf-8")
		lines = content.splitlines()
		self.assertIn("Exec=linda %F", lines)
		self.assertIn("MimeType=application/x-nvda-addon;", lines)


class TestAddonArgExtraction(unittest.TestCase):
	@staticmethod
	def _extractAddons(
		appArgsInstallAddons: list[str] | None, unknownArgs: list[str]
	) -> tuple[list[str], list[str]]:
		installAddons = list(appArgsInstallAddons or [])
		remainingUnknown = []
		for arg in unknownArgs:
			if arg.lower().endswith(".nvda-addon"):
				installAddons.append(arg)
			else:
				remainingUnknown.append(arg)
		return installAddons, remainingUnknown

	def test_singlePositionalAddon(self):
		addons, remaining = self._extractAddons(None, ["/home/user/myaddon.nvda-addon"])
		self.assertEqual(addons, ["/home/user/myaddon.nvda-addon"])
		self.assertEqual(remaining, [])

	def test_alreadyOptionPrefixed(self):
		addons, remaining = self._extractAddons(["/home/user/myaddon.nvda-addon"], [])
		self.assertEqual(addons, ["/home/user/myaddon.nvda-addon"])
		self.assertEqual(remaining, [])

	def test_multipleAddonsMixed(self):
		addons, remaining = self._extractAddons(
			["flagged.nvda-addon"],
			["positional1.nvda-addon", "--other-unknown", "positional2.NVDA-ADDON"],
		)
		self.assertEqual(
			addons,
			["flagged.nvda-addon", "positional1.nvda-addon", "positional2.NVDA-ADDON"],
		)
		self.assertEqual(remaining, ["--other-unknown"])

	def test_noAddonsLeavesUnknownUntouched(self):
		addons, remaining = self._extractAddons(None, ["--some-flag", "some-value"])
		self.assertEqual(addons, [])
		self.assertEqual(remaining, ["--some-flag", "some-value"])


class TestArgsParsing(unittest.TestCase):
	def test_installAddonOption(self):
		with (
			patch.dict(
				"sys.modules",
				{
					"winUser": MagicMock(),
					"serial": MagicMock(),
					"serial.win32": MagicMock(),
					"linuxPlatform": MagicMock(),
					"linuxPlatform.desktopRegistration": MagicMock(),
				},
			),
			patch("sys.platform", "linux"),
		):
			# Reload or get fresh parser with sys.platform == 'linux'
			if "argsParsing" in sys.modules:
				del sys.modules["argsParsing"]
			import argsParsing

			parser = argsParsing._createNVDAArgParser()
			args, unknown = parser.parse_known_args(["--install-addon", "/path/to/addon.nvda-addon"])
			self.assertEqual(args.installAddons, ["/path/to/addon.nvda-addon"])
			self.assertEqual(unknown, [])

			argsMulti, unknownMulti = parser.parse_known_args(
				["--install-addon", "one.nvda-addon", "--install-addon", "two.nvda-addon"]
			)
			self.assertEqual(argsMulti.installAddons, ["one.nvda-addon", "two.nvda-addon"])
			self.assertEqual(unknownMulti, [])


class TestControlSocketAddonIpc(unittest.TestCase):
	def test_connectionHasRequestDetection(self):
		from linuxPlatform import controlSocket

		conn = controlSocket._Connection.__new__(controlSocket._Connection)
		conn._socket = MagicMock()
		conn._socket.recv.return_value = b""
		conn._received = b"take-keyboard\n"
		self.assertTrue(conn.hasRequest())

		conn._received = b"install-addon /tmp/test.nvda-addon\n"
		self.assertTrue(conn.hasRequest())

		conn._received = b"install-addon /path/with spaces/addon.nvda-addon\n"
		self.assertTrue(conn.hasRequest())

		# Unrecognized or partial commands
		conn._received = b"junk\n"
		self.assertFalse(conn.hasRequest())
		conn._received = b"install-addon partial"
		self.assertFalse(conn.hasRequest())

	def test_answerDispatchesAddonInstall(self):
		from linuxPlatform import controlSocket

		conn = controlSocket._Connection.__new__(controlSocket._Connection)
		conn._socket = MagicMock()
		conn._socket.recv.return_value = b""
		addonPath = "/tmp/sample.nvda-addon"
		conn._received = f"install-addon {addonPath}\n".encode()

		mockWx = MagicMock()
		mockSecurity = MagicMock()
		mockSecurity.isLockScreenModeActive.return_value = False
		mockSecurity.isRunningOnSecureDesktop.return_value = False

		with (
			patch.dict(
				"sys.modules",
				{
					"logHandler": MagicMock(),
					"wx": mockWx,
					"utils": MagicMock(),
					"utils.security": mockSecurity,
				},
			),
			patch("os.path.isfile", return_value=True),
		):
			conn._answer()
			conn._socket.send.assert_called_once_with(controlSocket.REPLY_OK)
			mockWx.CallAfter.assert_called_once_with(
				controlSocket.queueAddonInstall, os.path.abspath(addonPath)
			)

	def test_answerRejectsNonexistentFile(self):
		from linuxPlatform import controlSocket

		conn = controlSocket._Connection.__new__(controlSocket._Connection)
		conn._socket = MagicMock()
		conn._socket.recv.return_value = b""
		addonPath = "/nonexistent/addon.nvda-addon"
		conn._received = f"install-addon {addonPath}\n".encode()

		mockSecurity = MagicMock()
		mockSecurity.isLockScreenModeActive.return_value = False
		mockSecurity.isRunningOnSecureDesktop.return_value = False

		with (
			patch.dict(
				"sys.modules",
				{
					"logHandler": MagicMock(),
					"utils": MagicMock(),
					"utils.security": mockSecurity,
				},
			),
			patch("os.path.isfile", return_value=False),
		):
			conn._answer()
			conn._socket.send.assert_called_once_with(controlSocket.FAILED_REPLY)

	def test_answerRejectsUnderLockScreen(self):
		from linuxPlatform import controlSocket

		conn = controlSocket._Connection.__new__(controlSocket._Connection)
		conn._socket = MagicMock()
		conn._socket.recv.return_value = b""
		addonPath = "/tmp/sample.nvda-addon"
		conn._received = b"install-addon /tmp/sample.nvda-addon\n"

		mockSecurity = MagicMock()
		mockSecurity.isLockScreenModeActive.return_value = True
		mockSecurity.isRunningOnSecureDesktop.return_value = False

		with (
			patch.dict(
				"sys.modules",
				{
					"logHandler": MagicMock(),
					"utils": MagicMock(),
					"utils.security": mockSecurity,
				},
			),
			patch("os.path.isfile", return_value=True),
		):
			conn._answer()
			conn._socket.send.assert_called_once_with(controlSocket.FAILED_REPLY)

	def test_queueAddonInstallSequential(self):
		from linuxPlatform import controlSocket

		controlSocket._pendingAddons.clear()
		controlSocket._installingAddon = False

		calls = []
		mockWx = MagicMock()
		mockAddonGui = MagicMock()

		def fakeHandleInstall(path):
			calls.append(path)
			# While the first install is running, simulate another queueAddonInstall call
			if path == "/tmp/1.nvda-addon":
				controlSocket.queueAddonInstall("/tmp/2.nvda-addon")

		mockAddonGui.handleRemoteAddonInstall.side_effect = fakeHandleInstall

		with patch.dict(
			"sys.modules",
			{
				"wx": mockWx,
				"gui": MagicMock(addonGui=mockAddonGui),
				"gui.addonGui": mockAddonGui,
			},
		):
			controlSocket.queueAddonInstall("/tmp/1.nvda-addon")
			self.assertEqual(calls, ["/tmp/1.nvda-addon"])
			# The second add-on should be queued in _pendingAddons and wx.CallAfter should be requested
			self.assertEqual(controlSocket._pendingAddons, ["/tmp/2.nvda-addon"])
			mockWx.CallAfter.assert_called_with(controlSocket._processNextAddon)

			# Process the next add-on
			controlSocket._processNextAddon()
			self.assertEqual(calls, ["/tmp/1.nvda-addon", "/tmp/2.nvda-addon"])
			self.assertEqual(controlSocket._pendingAddons, [])

	def test_askAcceptsAcceptedReplies(self):
		import socket
		from linuxPlatform import controlSocket

		for reply in (controlSocket.REPLY_TAKEN, controlSocket.REPLY_OK):
			mockSocket = MagicMock()
			mockSocket.recv.side_effect = [reply, b""]
			with (
				patch.object(socket, "AF_UNIX", 1, create=True),
				patch.object(socket, "SOCK_CLOEXEC", 0, create=True),
				patch("socket.socket", return_value=mockSocket),
				patch("time.monotonic", side_effect=[0.0, 0.1, 0.2, 0.3]),
			):
				outcome = controlSocket.ask(["/dummy.socket"])
				self.assertEqual(outcome, controlSocket.ANSWERED)


if __name__ == "__main__":
	unittest.main()
