# A part of NonVisual Desktop Access (NVDA)
# Copyright (C) 2026 NV Access Limited
# This file may be used under the terms of the GNU General Public License, version 2 or later, as modified by the NVDA license.
# For full terms and any additional permissions, see the NVDA license file: https://github.com/nvaccess/nvda/blob/master/copying.txt

"""Unit tests for on-demand formatting lookup in AtSpiBufferTextInfo."""

import sys
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import textInfos

if sys.platform == "linux":
	from virtualBuffers.atspi import AtSpiBufferTextInfo


@unittest.skipUnless(sys.platform == "linux", "AtSpiBufferTextInfo requires Linux AT-SPI")
class TestAtSpiBufferFormatting(unittest.TestCase):
	def _makeInfo(self, start: int = 0, end: int = 1) -> "AtSpiBufferTextInfo":
		info = AtSpiBufferTextInfo.__new__(AtSpiBufferTextInfo)
		info.obj = SimpleNamespace(
			VBufHandle=123,
			rootNVDAObject=SimpleNamespace(node=SimpleNamespace(key=("b", "/root"))),
			getNVDAObjectFromIdentifier=MagicMock(),
		)
		info._startOffset = start
		info._endOffset = end
		return info

	def test_fastPathSkipsAtspiWhenFormattingDisabled(self):
		info = self._makeInfo(0, 5)
		baseCommands = [textInfos.FieldCommand("controlStart", textInfos.ControlField()), "Hello"]
		with (
			patch.object(AtSpiBufferTextInfo.__bases__[0], "getTextWithFields", return_value=baseCommands),
			patch.object(info, "_getFormatFieldAtOffset") as getFmt,
		):
			self.assertEqual(info.getTextWithFields(None), baseCommands)
			self.assertEqual(
				info.getTextWithFields({"reportSpellingErrors2": 1, "reportFontName": False}),
				baseCommands,
			)
			getFmt.assert_not_called()

	def test_injectsFormatFieldWhenFormattingRequested(self):
		info = self._makeInfo(0, 1)
		ctrl = textInfos.ControlField()
		baseCommands = [
			textInfos.FieldCommand("controlStart", ctrl),
			"H",
			textInfos.FieldCommand("controlEnd", ctrl),
		]
		expectedField = textInfos.FormatField({"font-name": "DejaVu Sans", "font-size": "12pt", "bold": True})
		with (
			patch.object(AtSpiBufferTextInfo.__bases__[0], "getTextWithFields", return_value=baseCommands),
			patch.object(info, "_getFormatFieldAtOffset", return_value=expectedField) as getFmt,
		):
			result = info.getTextWithFields({"reportFontName": True, "reportFontSize": True})
			getFmt.assert_called_once_with(0)
			self.assertIsInstance(result[1], textInfos.FieldCommand)
			self.assertEqual(result[1].command, "formatChange")
			self.assertEqual(result[1].field["font-name"], "DejaVu Sans")
			self.assertEqual(result[2], "H")

	def test_atspiTextOffsetAccountsForInlineChildren(self):
		info = self._makeInfo(11, 12)
		xml = '<control role="2">Hello <control role="1">bold</control> </control>'
		with patch("virtualBuffers.atspi.NVDAHelper.localLib.VBuf_getTextInRange", return_value=xml):
			# "Hello " (6) + 1 (\ufffc for <b>) + " " (1) = 8
			self.assertEqual(info._atspiTextOffset(0, 11), 8)
			self.assertEqual(info._atspiTextOffset(5, 5), 0)

	def test_nodeGoneReturnsBaseCommandsCleanly(self):
		from linuxPlatform.atspi.client import NodeGone

		info = self._makeInfo(0, 1)
		baseCommands = ["H"]
		nodeObj = SimpleNamespace(
			atspiInterfaces={"Text"},
			node=SimpleNamespace(key=("b", "/p")),
			parent=None,
		)
		info.obj.getNVDAObjectFromIdentifier.return_value = nodeObj

		def fakeLocate(_h, _off, s, e, doc, ident, _node):
			s._obj.value = 0
			e._obj.value = 1
			doc._obj.value = 1
			ident._obj.value = 2

		mockSource = MagicMock()
		mockSource.attributeRun.side_effect = NodeGone("gone")
		with (
			patch.object(AtSpiBufferTextInfo.__bases__[0], "getTextWithFields", return_value=baseCommands),
			patch(
				"virtualBuffers.atspi.NVDAHelper.localLib.VBuf_locateControlFieldNodeAtOffset",
				side_effect=fakeLocate,
			),
			patch("virtualBuffers.atspi.toolkitOf", return_value="Gecko"),
			patch("virtualBuffers.atspi.unitFor", return_value="codepoint"),
			patch("virtualBuffers.atspi.TextSource", return_value=mockSource),
		):
			self.assertEqual(info.getTextWithFields({"reportFontName": True}), ["H"])
