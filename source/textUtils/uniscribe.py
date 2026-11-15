# A part of NonVisual Desktop Access (NVDA)
# This file is covered by the GNU General Public License.
# See the file COPYING for more details.
# Copyright (C) 2024-2025 NV Access Limited, Leonard de Ruijter

"""Wrapper functions for NVDAHelper uniscribe functions."""

import ctypes
import sys
from collections.abc import Generator

# The boundaries come back in UTF-16 code units, and the buffer is sliced with them; a c_wchar
# buffer counts code units only on Windows.
if sys.platform == "win32":
	_createUtf16Buffer = ctypes.create_unicode_buffer
else:
	from linuxPlatform.utf16 import Utf16Buffer as _createUtf16Buffer


def splitAtCharacterBoundaries(text: str) -> Generator[str]:
	"""
	Splits a given string into real visible characters (or glyphs), thereby respecting character boundaries.
	Contrary to just iterating over a string, this respects surrogate pairs, decomposite characters, etc.
	"""
	import NVDAHelper  # Import late to avoid circular import.

	if not NVDAHelper.localLib:
		raise RuntimeError("NVDAHelper not initialized")
	if not text:
		return
	buffer = _createUtf16Buffer(text)
	textLength = len(buffer) - 1  # Length without terminating NULL character
	offsetsCount = ctypes.c_int()
	offsets = (ctypes.c_int * (textLength + 1))()
	if not NVDAHelper.localLib.calculateCharacterBoundaries(
		buffer,
		textLength,
		offsets,
		ctypes.byref(offsetsCount),
	):
		raise RuntimeError("NVDAHelper calculateCharacterBoundaries failed")
	# Get the end offsets of the characters we need.
	calculatedOffsets = offsets[1 : offsetsCount.value]
	start = 0
	for end in calculatedOffsets:
		yield buffer[start:end]
		start = end
