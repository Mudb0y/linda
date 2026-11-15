"""The Linux side of NVDA's native helper: beeps, text boundaries, ICU and liblouis.

Text offsets are UTF-16 code units throughout, so every text case puts an astral character
somewhere it changes the count.

	nix develop . -c tests/linux/rig python3 -m pytest -p no:cacheprovider tests/linux/test_nativeHelper.py
"""

import math
import os
import struct
from itertools import pairwise
from pathlib import Path

import harness

ROOT = Path(__file__).resolve().parent.parent.parent

FAMILY = "\U0001f469\U0001f3fb‍\U0001f467\U0001f3fb‍\U0001f466\U0001f3fb"
"""Woman, girl and boy, each with a skin tone, joined by zero width joiners: 14 UTF-16 units."""


def utf16Length(text: str) -> int:
	return len(text.encode("utf_16_le", errors="surrogatepass")) // 2


def test_offsetConversionAcrossAstralCharacters(tmp_path: Path):
	text = "a\U0001f600b\U0001d11ec"
	done = harness.runChild(
		tmp_path,
		f"""
		import textUtils
		converter = textUtils.WideStringOffsetConverter({text!r})
		result = {{
			"length": converter.encodedStringLength,
			"toEncoded": [list(converter.strToEncodedOffsets(i, i + 1)) for i in range(len({text!r}))],
			"toStr": [list(converter.encodedToStrOffsets(i, i + 1)) for i in range(converter.encodedStringLength)],
			"wholeToEncoded": list(converter.strToEncodedOffsets(0, len({text!r}))),
			"wholeToStr": list(converter.encodedToStrOffsets(0, converter.encodedStringLength)),
		}}
		""",
	)
	done.assertClean()
	starts = [utf16Length(text[:i]) for i in range(len(text) + 1)]
	assert done.value["length"] == utf16Length(text) == 7
	assert done.value["toEncoded"] == [[starts[i], starts[i + 1]] for i in range(len(text))]
	# A code unit in either half of a surrogate pair belongs to the one character.
	owner = [i for i in range(len(text)) for _ in range(starts[i + 1] - starts[i])]
	assert done.value["toStr"] == [[c, c + 1] for c in owner]
	assert done.value["wholeToEncoded"] == [0, 7]
	assert done.value["wholeToStr"] == [0, len(text)]


def test_graphemeBoundaries(tmp_path: Path):
	cases = {
		"family": FAMILY,
		"flag": "\U0001f1f5\U0001f1f1",
		"keycap": "1️⃣",
		"warning": "⚠️",
		"combiningAcute": "é",
		"hebrewWithPoints": "בְּ",
		"plain": "x",
	}
	text = "".join(cases.values())
	done = harness.runChild(
		tmp_path,
		f"""
		import ctypes
		import NVDAHelper
		from NVDAHelper import localLib
		from textUtils import uniscribe
		text = {text!r}
		length = len(text.encode("utf_16_le")) // 2
		offsets = (ctypes.c_int * (length + 1))()
		count = ctypes.c_int()
		ok = localLib.calculateCharacterBoundaries(text, length, offsets, ctypes.byref(count))
		def characterAt(offset):
			bounds = ctypes.c_int(), ctypes.c_int()
			localLib.calculateCharacterOffsets(text, length, offset, *map(ctypes.byref, bounds))
			return [bound.value for bound in bounds]
		pastEnd = ctypes.c_int(), ctypes.c_int()
		localLib.calculateCharacterOffsets(text, length, length + 3, *map(ctypes.byref, pastEnd))
		result = {{
			"ok": ok,
			"boundaries": offsets[: count.value],
			"split": list(uniscribe.splitAtCharacterBoundaries(text)),
			"characters": [characterAt(offset) for offset in range(length)],
			"pastEnd": [v.value for v in pastEnd],
			"empty": localLib.calculateCharacterBoundaries("", 0, offsets, ctypes.byref(count)),
		}}
		""",
	)
	done.assertClean()
	expected = [0]
	for cluster in cases.values():
		expected.append(expected[-1] + utf16Length(cluster))
	assert done.value["ok"] is True
	assert done.value["boundaries"] == expected
	assert done.value["split"] == list(cases.values())
	length = utf16Length(text)
	# Every code unit, either half of a surrogate pair included, belongs to its whole cluster.
	assert done.value["characters"] == [
		[start, end] for start, end in pairwise(expected) for _ in range(end - start)
	]
	assert done.value["pastEnd"] == [length + 3, length + 4]
	assert done.value["empty"] is False


def test_wordOffsets(tmp_path: Path):
	english = "The quick brown fox."
	polish = "\U0001f600 Zażółć gęślą jaźń, źdźbło."
	done = harness.runChild(
		tmp_path,
		f"""
		import ctypes
		import NVDAHelper
		from NVDAHelper import localLib
		import textUtils
		from textUtils._wordSeg.wordSegmenter import WordSegmenter
		from textUtils.segFlag import WordSegFlag
		from winBindings import icu

		def helper(text, offset):
			start, end = ctypes.c_int(), ctypes.c_int()
			length = len(text.encode("utf_16_le")) // 2
			if not localLib.calculateWordOffsets(text, length, offset, ctypes.byref(start), ctypes.byref(end)):
				return None
			return [start.value, end.value]

		def segmenter(text, offset, flag):
			return list(WordSegmenter(text, textUtils.WCHAR_ENCODING, flag).getSegmentForOffset(offset))

		result = {{
			"icuAvailable": icu.ICU_AVAILABLE,
			"english": [helper({english!r}, o) for o in (0, 5, 9, 17, 19)],
			"polish": [helper({polish!r}, o) for o in (0, 1, 3, 11, 14, 17, 20, 23, 26)],
			"auto": segmenter({polish!r}, 14, WordSegFlag.AUTO),
			"uniscribe": segmenter({polish!r}, 14, WordSegFlag.UNISCRIBE),
			"negative": helper({english!r}, -1),
			"pastEnd": helper({english!r}, 40),
		}}
		""",
	)
	done.assertClean()
	assert done.value["icuAvailable"] is True
	assert done.value["english"] == [[0, 4], [4, 10], [4, 10], [16, 19], [19, 20]]
	# The emoji takes two code units, so each word starts one unit later than its index in the str.
	units = [utf16Length(polish[:i]) for i in range(len(polish) + 1)]
	assert [units[polish.index(word)] for word in ("Zażółć", "gęślą", "jaźń", "źdźbło")] == [3, 10, 16, 22]
	assert done.value["polish"] == [
		[0, 3],
		[0, 3],
		[3, 10],
		[10, 16],
		[10, 16],
		[16, 20],
		[20, 22],
		[22, 28],
		[22, 28],
	]
	assert done.value["auto"] == done.value["uniscribe"] == [10, 16]
	assert done.value["negative"] is None
	assert done.value["pastEnd"] == [40, 41]


def test_icuLoadsFromTheDevShellAndTakesUtf16(tmp_path: Path):
	done = harness.runChild(
		tmp_path,
		"""
		import ctypes
		import os
		from winBindings import icu
		status = icu.UErrorCode(0)
		try:
			icu.ubrk_open(icu.UBRK.WORD, b"", ctypes.create_unicode_buffer("ab"), 2, ctypes.byref(status))
			refused = None
		except ctypes.ArgumentError as e:
			refused = str(e)
		result = {
			"available": icu.ICU_AVAILABLE,
			"path": icu.dll._name,
			"expected": os.path.join(
				os.environ["BASEDREADER_ICU_LIB_DIR"],
				"libicuuc.so." + os.environ["BASEDREADER_ICU_VERSION_MAJOR"],
			),
			"wideRefused": refused,
		}
		""",
	)
	done.assertClean()
	assert done.value["available"] is True
	assert done.value["path"] == done.value["expected"]
	assert done.value["wideRefused"] is not None


def test_icuAbsentIsReportedTruthfully(tmp_path: Path):
	done = harness.runChild(
		tmp_path,
		"""
		import ctypes
		import NVDAHelper
		from NVDAHelper import localLib
		from linuxPlatform import NoLinuxImplementation
		from winBindings import icu
		from textUtils._wordSeg import wordSegmenter
		try:
			localLib.calculateWordOffsets("ab", 2, 0, ctypes.byref(ctypes.c_int()), ctypes.byref(ctypes.c_int()))
			raised = None
		except NoLinuxImplementation as e:
			raised = e.what
		result = {
			"available": icu.ICU_AVAILABLE,
			"segmenterSees": wordSegmenter.ICU_AVAILABLE,
			"dll": icu.dll,
			"bound": hasattr(icu, "ubrk_open"),
			"raised": raised,
		}
		""",
		env={"BASEDREADER_ICU_LIB_DIR": "/nonexistent"},
	)
	assert done.error is None, done.error
	assert done.value == {
		"available": False,
		"segmenterSees": False,
		"dll": None,
		"bound": False,
		"raised": "NVDAHelper.localLib.calculateWordOffsets",
	}
	assert done.noLinuxImplementation == ["NVDAHelper.localLib.calculateWordOffsets"]
	assert not done.logErrors


def toFloat32(value: float) -> float:
	return struct.unpack("<f", struct.pack("<f", value))[0]


def expectedBeepSamples(hz: float, length: int, left: int, right: int, count: int) -> tuple[int, list]:
	"""nvdaHelper/local/beeps.cpp transcribed: the size in bytes and the first count frames."""
	hz = toFloat32(hz)
	perCycle = toFloat32(44100 / hz)
	samplesPerCycle = int(perCycle)
	totalSamples = int((length / 1000.0) / (1.0 / 44100))
	totalSamples += samplesPerCycle - totalSamples % samplesPerCycle
	lpan = (left / 100.0) * 14000
	rpan = (right / 100.0) * 14000
	sinFreq = (2.0 * math.pi) / perCycle
	frames = []
	for n in range(count):
		sample = min(max(math.sin((n % 44100) * sinFreq) * 2.0, -1.0), 1.0)
		frames.append([int(sample * lpan), int(sample * rpan)])
	return totalSamples * 4, frames


def test_beepBuffer(tmp_path: Path):
	cases = [(440, 50, 50, 50), (1000.3, 30, 20, 85), (441, 1000, 100, 0)]
	done = harness.runChild(
		tmp_path,
		f"""
		import ctypes
		import struct
		import NVDAHelper
		from NVDAHelper.localLib import generateBeep
		result = []
		for hz, length, left, right in {cases!r}:
			size = generateBeep(None, hz, length, left, right)
			buf = ctypes.create_string_buffer(size)
			written = generateBeep(buf, hz, length, left, right)
			result.append({{
				"size": size,
				"written": written,
				"samples": list(struct.unpack("<%dh" % (size // 2), buf.raw)),
			}})
		""",
	)
	done.assertClean()
	for (hz, length, left, right), got in zip(cases, done.value):
		samplesPerCycle = int(toFloat32(44100 / toFloat32(hz)))
		size, firstCycle = expectedBeepSamples(hz, length, left, right, samplesPerCycle)
		assert got["size"] == got["written"] == size
		# 16-bit signed stereo frames, a whole number of cycles.
		assert size % 4 == 0 and (size // 4) % samplesPerCycle == 0
		frames = [got["samples"][i : i + 2] for i in range(0, 2 * samplesPerCycle, 2)]
		assert frames == firstCycle
	# 441 Hz is exactly 100 samples a cycle and 1000 ms exactly 441 cycles, and the C++ then adds a
	# whole extra cycle.
	assert done.value[2]["size"] == (44100 + 100) * 4
	assert max(done.value[2]["samples"][0::2]) == 14000 and max(done.value[2]["samples"][1::2]) == 0


def test_brailleTranslationThroughLouisHelper(tmp_path: Path):
	done = harness.runChild(
		tmp_path,
		"""
		import ctypes
		import logging
		import os
		import brailleTables
		import config
		import louis
		import louisHelper
		from logHandler import log

		messages = []

		class Collector(logging.Handler):
			def emit(self, record):
				if "liblouis at internal log level" in str(getattr(record, "codepath", "")):
					messages.append(record.getMessage())

		log.addHandler(Collector(level=logging.DEBUG))
		log.setLevel(logging.DEBUG)
		config.conf["debugLog"]["louis"] = True
		louisHelper.initialize()
		louis.setLogLevel(louis.LOG_ALL)
		try:
			english, *_ = louisHelper.translate(["en-ueb-g2.ctb"], "the quick brown fox")
			swedish, *_ = louisHelper.translate(["Se-Se.ctb"], "Hej 1")
			result = {
				"tablesDir": brailleTables.TABLES_DIR,
				"resolved": list(louisHelper._resolveTableInner(["Se-Se.ctb"])),
				"english": louisHelper.backTranslate(["en-ueb-g2.ctb"], english),
				"englishCells": len(english),
				"swedish": louisHelper.backTranslate(["Se-Se.ctb"], swedish),
				"swedishCells": len(swedish),
				"cdecl": [
					type(callback)._flags_ & ctypes._FUNCFLAG_CDECL == ctypes._FUNCFLAG_CDECL
					for callback in (louisHelper.louis_log, louisHelper._resolveTable)
				],
				"logged": any("translat" in m.lower() for m in messages),
			}
		finally:
			louisHelper.terminate()
		""",
	)
	done.assertClean()
	tablesDir = os.environ["BASEDREADER_LOUIS_TABLES_DIR"]
	assert done.value["tablesDir"] == tablesDir
	assert done.value["resolved"] == [os.path.join(tablesDir, "Se-Se.ctb")]
	assert done.value["english"] == "the quick brown fox"
	# Grade 2 contracts "the" and "quick" to one cell or two.
	assert done.value["englishCells"] < len("the quick brown fox")
	assert done.value["swedish"] == "Hej 1"
	assert done.value["swedishCells"] == len("Hej 1")
	assert done.value["cdecl"] == [True, True]
	assert done.value["logged"] is True


def test_brailleTablesFallBackToTheBuiltDirectory(tmp_path: Path):
	builtTables = ROOT / "source" / "louis" / "tables"
	assert builtTables.is_dir(), "run tools/build-artifacts first"
	done = harness.runChild(
		tmp_path,
		"""
		import brailleTables
		import louisHelper
		louisHelper.initialize()
		try:
			cells, *_ = louisHelper.translate(["No-No-g1.ctb"], "hei")
		finally:
			louisHelper.terminate()
		result = {"tablesDir": brailleTables.TABLES_DIR, "cells": len(cells)}
		""",
		env={"BASEDREADER_LOUIS_TABLES_DIR": ""},
	)
	done.assertClean()
	assert done.value == {"tablesDir": str(builtTables), "cells": 3}


def test_virtualBufferAndDisplayModelStillRaise(tmp_path: Path):
	done = harness.runChild(
		tmp_path,
		"""
		import NVDAHelper
		from NVDAHelper import localLib
		from linuxPlatform import NoLinuxImplementation
		result = []
		for name in ("VBuf_getTextLength", "VBuf_createBuffer", "displayModel_getWindowTextInRect"):
			try:
				getattr(localLib, name)(None)
			except NoLinuxImplementation:
				result.append(name)
		""",
	)
	assert done.error is None, done.error
	expected = ["VBuf_getTextLength", "VBuf_createBuffer", "displayModel_getWindowTextInRect"]
	assert done.value == expected
	assert done.noLinuxImplementation == [f"NVDAHelper.localLib.{name}" for name in expected]
