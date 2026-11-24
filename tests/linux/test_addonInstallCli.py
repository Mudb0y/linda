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


class TestCliNormalization(unittest.TestCase):
	@staticmethod
	def _normalize(argv: list[str]) -> list[str]:
		normalized = [argv[0]]
		for i, arg in enumerate(argv[1:], start=1):
			if arg.lower().endswith(".nvda-addon") and argv[i - 1] != "--install-addon":
				normalized.extend(["--install-addon", arg])
			else:
				normalized.append(arg)
		return normalized

	def test_singlePositionalAddon(self):
		argv = ["linda", "/home/user/myaddon.nvda-addon"]
		self.assertEqual(self._normalize(argv), ["linda", "--install-addon", "/home/user/myaddon.nvda-addon"])

	def test_alreadyOptionPrefixed(self):
		argv = ["linda", "--install-addon", "/home/user/myaddon.nvda-addon"]
		self.assertEqual(self._normalize(argv), argv)

	def test_multipleAddons(self):
		argv = ["linda", "a.nvda-addon", "b.NVDA-ADDON"]
		self.assertEqual(
			self._normalize(argv),
			["linda", "--install-addon", "a.nvda-addon", "--install-addon", "b.NVDA-ADDON"],
		)

	def test_mixedArguments(self):
		argv = ["linda", "--debug-logging", "-c", "/etc/linda", "pack.nvda-addon"]
		self.assertEqual(
			self._normalize(argv),
			["linda", "--debug-logging", "-c", "/etc/linda", "--install-addon", "pack.nvda-addon"],
		)

	def test_normalLaunchWithoutAddon(self):
		argv = ["linda"]
		self.assertEqual(self._normalize(argv), ["linda"])
		argvWithFlags = ["linda", "-r"]
		self.assertEqual(self._normalize(argvWithFlags), ["linda", "-r"])


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
		mockGui = MagicMock()
		mockSecurity = MagicMock()
		mockSecurity.isLockScreenModeActive.return_value = False
		mockSecurity.isRunningOnSecureDesktop.return_value = False

		with patch.dict(
			"sys.modules",
			{
				"logHandler": MagicMock(),
				"wx": mockWx,
				"gui": mockGui,
				"utils": MagicMock(),
				"utils.security": mockSecurity,
			},
		):
			conn._answer()
			conn._socket.send.assert_called_once_with(controlSocket.REPLY)
			mockWx.CallAfter.assert_called_once_with(mockGui.addonGui.handleRemoteAddonInstall, addonPath)

	def test_answerRejectsUnderLockScreen(self):
		from linuxPlatform import controlSocket

		conn = controlSocket._Connection.__new__(controlSocket._Connection)
		conn._socket = MagicMock()
		conn._socket.recv.return_value = b""
		conn._received = b"install-addon /tmp/sample.nvda-addon\n"

		mockSecurity = MagicMock()
		mockSecurity.isLockScreenModeActive.return_value = True
		mockSecurity.isRunningOnSecureDesktop.return_value = False

		with patch.dict(
			"sys.modules",
			{
				"logHandler": MagicMock(),
				"utils": MagicMock(),
				"utils.security": mockSecurity,
			},
		):
			conn._answer()
			conn._socket.send.assert_called_once_with(controlSocket.FAILED_REPLY)


if __name__ == "__main__":
	unittest.main()
