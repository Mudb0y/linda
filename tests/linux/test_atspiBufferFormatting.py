"""Unit and browser rig tests for AT-SPI Browse Mode formatting reporting (issue 52).

LINDA_RIG_WM=marco nix develop . -c tests/linux/rig python3 -m pytest -v tests/linux/test_atspiBufferFormatting.py
"""

import time
from pathlib import Path

import chromiumSession
import harness
import pytest
import webSession
from desktopSession import DesktopSession

STARTUP_BOUND = 60.0
LOAD_BOUND = 15.0
STEP_BOUND = 5.0
SETTLE = 0.5

UNIT_TESTS_CHILD = r"""
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import NVDAHelper  # noqa: F401
import textInfos
from linuxPlatform.atspi.client import NodeGone
from virtualBuffers.atspi import AtSpiBufferTextInfo


class TestAtSpiBufferFormatting(unittest.TestCase):
	def _makeInfo(self, start: int = 0, end: int = 1) -> AtSpiBufferTextInfo:
		info = AtSpiBufferTextInfo.__new__(AtSpiBufferTextInfo)
		info.obj = SimpleNamespace(
			VBufHandle=123,
			rootNVDAObject=SimpleNamespace(node=SimpleNamespace(key=("b", "/root"))),
			getNVDAObjectFromIdentifier=MagicMock(),
			getIdentifierFromNVDAObject=MagicMock(return_value=(1, 10)),
		)
		info._startOffset = start
		info._endOffset = end
		return info

	def test_fastPathSkipsAtspiWhenFormattingDisabled(self):
		info = self._makeInfo(0, 5)
		baseCommands = [textInfos.FieldCommand("controlStart", textInfos.ControlField()), "Hello"]
		with (
			patch.object(AtSpiBufferTextInfo.__bases__[0], "getTextWithFields", return_value=baseCommands),
			patch.object(info, "_splitFormattedText") as splitFmt,
		):
			self.assertEqual(info.getTextWithFields(None), baseCommands)
			self.assertEqual(
				info.getTextWithFields({"reportSpellingErrors2": 1, "reportFontName": False}),
				baseCommands,
			)
			splitFmt.assert_not_called()

	def test_singleCharacterInjectsFormatField(self):
		info = self._makeInfo(0, 1)
		ctrl = textInfos.ControlField({"controlIdentifier_docHandle": "1", "controlIdentifier_ID": "2"})
		baseCommands = [
			textInfos.FieldCommand("controlStart", ctrl),
			"H",
			textInfos.FieldCommand("controlEnd", ctrl),
		]
		nodeObj = SimpleNamespace(
			atspiInterfaces={"Text"},
			node=SimpleNamespace(key=("b", "/p")),
			parent=None,
		)
		info.obj.getNVDAObjectFromIdentifier.return_value = nodeObj
		mockSource = MagicMock()
		mockSource.attributeRun.return_value = (
			{"family-name": "DejaVu Sans", "size": "12", "weight": "bold"},
			0,
			1,
		)
		with (
			patch.object(AtSpiBufferTextInfo.__bases__[0], "getTextWithFields", return_value=baseCommands),
			patch.object(info, "_getOffsetsFromFieldIdentifier", return_value=(0, 1)),
			patch("virtualBuffers.atspi.toolkitOf", return_value="Gecko"),
			patch("virtualBuffers.atspi.unitFor", return_value="codepoints"),
			patch("virtualBuffers.atspi.TextSource", return_value=mockSource),
		):
			result = info.getTextWithFields({"reportFontName": True, "reportFontSize": True})
		self.assertEqual(len(result), 4)
		self.assertIsInstance(result[1], textInfos.FieldCommand)
		self.assertEqual(result[1].command, "formatChange")
		self.assertEqual(result[1].field["font-name"], "DejaVu Sans")
		self.assertTrue(result[1].field["bold"])
		self.assertEqual(result[2], "H")

	def test_multiRunSplitsLineAndPreservesLanguage(self):
		info = self._makeInfo(0, 13)
		ctrl = textInfos.ControlField({"controlIdentifier_docHandle": "1", "controlIdentifier_ID": "2"})
		baseCommands = [
			textInfos.FieldCommand("controlStart", ctrl),
			textInfos.FieldCommand("formatChange", textInfos.FormatField({"language": "pl"})),
			"Plain BoldEnd",
			textInfos.FieldCommand("controlEnd", ctrl),
		]
		nodeObj = SimpleNamespace(
			atspiInterfaces={"Text"},
			node=SimpleNamespace(key=("b", "/p")),
			parent=None,
		)
		info.obj.getNVDAObjectFromIdentifier.return_value = nodeObj
		mockSource = MagicMock()
		mockSource.attributeRun.side_effect = [
			({"weight": "400"}, 0, 6),
			({"weight": "700"}, 6, 13),
		]
		with (
			patch.object(AtSpiBufferTextInfo.__bases__[0], "getTextWithFields", return_value=baseCommands),
			patch.object(info, "_getOffsetsFromFieldIdentifier", return_value=(0, 13)),
			patch("virtualBuffers.atspi.toolkitOf", return_value="Gecko"),
			patch("virtualBuffers.atspi.unitFor", return_value="codepoints"),
			patch("virtualBuffers.atspi.TextSource", return_value=mockSource),
		):
			result = info.getTextWithFields({"fontAttributeReporting": 1})
		# controlStart, formatChange(plain+pl), "Plain ", formatChange(bold+pl), "BoldEnd", controlEnd
		self.assertEqual(len(result), 6)
		self.assertEqual(result[1].command, "formatChange")
		self.assertEqual(result[1].field["language"], "pl")
		self.assertFalse(result[1].field["bold"])
		self.assertEqual(result[2], "Plain ")
		self.assertEqual(result[3].command, "formatChange")
		self.assertEqual(result[3].field["language"], "pl")
		self.assertTrue(result[3].field["bold"])
		self.assertEqual(result[4], "BoldEnd")

	def test_atspiTextOffsetNestedControlsAndDirectChildren(self):
		info = self._makeInfo(30, 31)
		xml = (
			'<control controlIdentifier_docHandle="1" controlIdentifier_ID="1">'
			'<control controlIdentifier_docHandle="1" controlIdentifier_ID="2">'
			'<control controlIdentifier_docHandle="1" controlIdentifier_ID="3">'
			'Plain text '
			'<control controlIdentifier_docHandle="1" controlIdentifier_ID="4">'
			'a <control controlIdentifier_docHandle="1" controlIdentifier_ID="5">nested</control> link'
			'</control>'
			' after '
			'</control>'
			'</control>'
			'</control>'
		)
		with patch("virtualBuffers.atspi.NVDAHelper.localLib.VBuf_getTextInRange", return_value=xml):
			# "Plain text " (11) + 1 (\ufffc for link 4, ignoring grandchild 5) + " after " (7) = 19
			self.assertEqual(info._atspiTextOffset(10, 30, 1, 3), 19)
			self.assertEqual(info._atspiTextOffset(10, 10, 1, 3), 0)

	def test_walkedUpUsesAncestorIdentifierAndOffsets(self):
		info = self._makeInfo(5, 6)
		baseCommands = ["H"]
		ancestorObj = SimpleNamespace(
			atspiInterfaces={"Text"},
			node=SimpleNamespace(key=("b", "/p")),
			parent=None,
		)
		childObj = SimpleNamespace(
			atspiInterfaces=set(),
			node=SimpleNamespace(key=("b", "/span")),
			parent=ancestorObj,
		)
		info.obj.getNVDAObjectFromIdentifier.return_value = childObj
		info.obj.getIdentifierFromNVDAObject.return_value = (1, 10)
		xml = (
			'<control controlIdentifier_docHandle="1" controlIdentifier_ID="1">'
			'<control controlIdentifier_docHandle="1" controlIdentifier_ID="10">Hello</control>'
			'</control>'
		)

		def fakeLocate(_h, _off, s, e, doc, ident, _node):
			s._obj.value = 5
			e._obj.value = 6
			doc._obj.value = 1
			ident._obj.value = 20

		mockSource = MagicMock()
		mockSource.attributeRun.return_value = ({"weight": "700"}, 5, 6)
		with (
			patch.object(AtSpiBufferTextInfo.__bases__[0], "getTextWithFields", return_value=baseCommands),
			patch(
				"virtualBuffers.atspi.NVDAHelper.localLib.VBuf_locateControlFieldNodeAtOffset",
				side_effect=fakeLocate,
			),
			patch.object(info, "_getOffsetsFromFieldIdentifier", return_value=(0, 10)) as getOffsets,
			patch("virtualBuffers.atspi.NVDAHelper.localLib.VBuf_getTextInRange", return_value=xml),
			patch("virtualBuffers.atspi.toolkitOf", return_value="Gecko"),
			patch("virtualBuffers.atspi.unitFor", return_value="codepoints"),
			patch("virtualBuffers.atspi.TextSource", return_value=mockSource),
		):
			result = info.getTextWithFields({"fontAttributeReporting": 1})
		getOffsets.assert_called_once_with(1, 10)
		mockSource.attributeRun.assert_called_once_with(5)
		self.assertTrue(result[0].field["bold"])
		self.assertEqual(result[1], "H")

	def test_nodeGoneReturnsBaseCommandsCleanly(self):
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
			patch("virtualBuffers.atspi.unitFor", return_value="codepoints"),
			patch("virtualBuffers.atspi.TextSource", return_value=mockSource),
		):
			self.assertEqual(info.getTextWithFields({"reportFontName": True}), ["H"])


suite = unittest.defaultTestLoader.loadTestsFromTestCase(TestAtSpiBufferFormatting)
runner = unittest.TextTestRunner(verbosity=2)
res = runner.run(suite)
if not res.wasSuccessful():
	raise SystemExit(1)
result = res.testsRun
"""


def test_atspiBufferFormattingUnitTests(tmp_path: Path):
	done = harness.runChild(tmp_path, UNIT_TESTS_CHILD, timeout=60)
	done.assertClean()
	assert done.value == 6


def _step(desktop: DesktopSession, chord: str, *expected: str, timeout: float = STEP_BOUND) -> int:
	mark = desktop.mark()
	desktop.press(chord)
	desktop.expect(*expected, since=mark, timeout=timeout)
	time.sleep(SETTLE)
	return mark


def _moveToWord(desktop: DesktopSession, target: str) -> None:
	for _ in range(10):
		mark = desktop.mark()
		desktop.press("control+Right")
		desktop.expect("…", since=mark, timeout=STEP_BOUND)
		time.sleep(0.2)
		if any(target in text for text in desktop.spokenSince(mark)):
			return
	raise AssertionError(f"control+Right never reached {target!r}")


def _reportFormatting(desktop: DesktopSession) -> str:
	mark = desktop.mark()
	desktop.press("Insert+f")
	desktop.expect("…", since=mark, timeout=STEP_BOUND)
	time.sleep(SETTLE)
	return " ".join(desktop.spokenSince(mark))


def _assertFormattingInBrowser(desktop: DesktopSession) -> None:
	_step(desktop, "Control_L+Home", "heading level 1 Formatting")
	_step(desktop, "Down", "…Plain text…")

	plainReport = _reportFormatting(desktop)
	assert "bold" not in plainReport.lower(), plainReport
	assert "italic" not in plainReport.lower(), plainReport

	_moveToWord(desktop, "span")
	spanReport = _reportFormatting(desktop)
	assert "bold" in spanReport.lower(), spanReport
	assert "red" in spanReport.lower(), spanReport

	_moveToWord(desktop, "italic")
	italicReport = _reportFormatting(desktop)
	assert "italic" in italicReport.lower(), italicReport
	assert "bold" not in italicReport.lower(), italicReport

	_step(desktop, "Down", "Before…")
	_moveToWord(desktop, "bold")
	boldAfterLinkReport = _reportFormatting(desktop)
	assert "bold" in boldAfterLinkReport.lower(), boldAfterLinkReport


@pytest.fixture
def formattingConfig(tmp_path: Path) -> Path:
	config = tmp_path / "nvda.ini"
	config.write_text(
		webSession.BROWSE_CONFIG.read_text()
		+ "\n[documentFormatting]\n\tfontAttributeReporting = 1\n\treportColor = True\n",
	)
	return config


def test_firefoxFormattingReporting(tmp_path: Path):
	webSession.requireBrowseLibraries()
	with DesktopSession(tmp_path / "desktop") as desktop:
		nvda = desktop.startNVDA(webSession.BROWSE_CONFIG)
		nvda.waitForLog("NVDA initialized", STARTUP_BOUND)
		mark = desktop.mark()
		webSession.startFirefox(desktop, "featureFormatting.html")
		desktop.expect("heading level 1 Formatting", since=mark, timeout=LOAD_BOUND)
		desktop.press("Control_L")
		time.sleep(SETTLE)
		_assertFormattingInBrowser(desktop)


def test_chromiumFormattingReporting(tmp_path: Path):
	webSession.requireBrowseLibraries()
	with DesktopSession(tmp_path / "desktop") as desktop:
		chromiumSession.clearAccessibilityStatus()
		nvda = desktop.startNVDA(webSession.BROWSE_CONFIG)
		nvda.waitForLog("NVDA initialized", STARTUP_BOUND)
		mark = desktop.mark()
		chromiumSession.startChromium(desktop, "featureFormatting.html")
		desktop.expect("heading level 1 Formatting", since=mark, timeout=LOAD_BOUND)
		desktop.press("Control_L")
		time.sleep(SETTLE)
		_assertFormattingInBrowser(desktop)


def test_firefoxLineReadingWithFormattingSettingsEnabled(tmp_path: Path, formattingConfig: Path):
	webSession.requireBrowseLibraries()
	with DesktopSession(tmp_path / "desktop") as desktop:
		nvda = desktop.startNVDA(formattingConfig)
		nvda.waitForLog("NVDA initialized", STARTUP_BOUND)
		mark = desktop.mark()
		webSession.startFirefox(desktop, "featureFormatting.html")
		desktop.expect("heading level 1 Formatting", since=mark, timeout=LOAD_BOUND)
		desktop.press("Control_L")
		time.sleep(SETTLE)

		_step(desktop, "Control_L+Home", "heading level 1 Formatting")
		lineMark = desktop.mark()
		desktop.press("Down")
		desktop.expect("…Plain text…", since=lineMark, timeout=STEP_BOUND)
		time.sleep(SETTLE)
		spoken = " ".join(desktop.spokenSince(lineMark)).lower()
		assert "red" in spoken, spoken
		assert "bold" in spoken, spoken
		assert "italic" in spoken, spoken
