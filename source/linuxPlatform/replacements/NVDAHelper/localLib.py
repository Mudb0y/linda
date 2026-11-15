"""Linux replacement for NVDAHelper.localLib.

Upstream binds nvdaHelperLocal.dll. This has the entry points that code staying on Linux names.
The beep generator and the three text boundary functions are implemented here, with upstream's
calling convention, so their callers work unchanged; the others raise until they have a Linux
implementation. Entry points that only Windows-only modules call (in-process helpers for Office,
SysListView32, IAccessible2 and UI Automation) are left out.

The text functions count in UTF-16 code units, as NVDA does everywhere: textLength, offsets and
results are code units of the text encoded as UTF-16, whatever form the text is passed in.
"""

import ctypes
import math
from array import array
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from ctypes import c_ulonglong, c_void_p
from typing import Any, NoReturn

from linuxPlatform import NoLinuxImplementation
from linuxPlatform.utf16 import Utf16Buffer
from winBindings import icu


def _notImplemented(name: str) -> Callable[..., NoReturn]:
	def entryPoint(*args: Any, **kwargs: Any) -> NoReturn:
		raise NoLinuxImplementation(f"NVDAHelper.localLib.{name}")

	entryPoint.__name__ = entryPoint.__qualname__ = name
	return entryPoint


VBufRemote_bufferHandle_t = c_void_p
VBufRemote_nodeHandle_t = c_ulonglong

VBuf_createBuffer = _notImplemented("VBuf_createBuffer")
VBuf_destroyBuffer = _notImplemented("VBuf_destroyBuffer")
VBuf_findNodeByAttributes = _notImplemented("VBuf_findNodeByAttributes")
VBuf_getControlFieldNodeWithIdentifier = _notImplemented("VBuf_getControlFieldNodeWithIdentifier")
VBuf_getFieldNodeOffsets = _notImplemented("VBuf_getFieldNodeOffsets")
VBuf_getIdentifierFromControlFieldNode = _notImplemented("VBuf_getIdentifierFromControlFieldNode")
VBuf_getLineOffsets = _notImplemented("VBuf_getLineOffsets")
VBuf_getSelectionOffsets = _notImplemented("VBuf_getSelectionOffsets")
VBuf_getTextInRange = _notImplemented("VBuf_getTextInRange")
VBuf_getTextLength = _notImplemented("VBuf_getTextLength")
VBuf_locateControlFieldNodeAtOffset = _notImplemented("VBuf_locateControlFieldNodeAtOffset")
VBuf_locateTextFieldNodeAtOffset = _notImplemented("VBuf_locateTextFieldNodeAtOffset")
VBuf_setSelectionOffsets = _notImplemented("VBuf_setSelectionOffsets")

displayModel_getCaretRect = _notImplemented("displayModel_getCaretRect")
displayModel_getFocusRect = _notImplemented("displayModel_getFocusRect")
displayModel_getWindowTextInRect = _notImplemented("displayModel_getWindowTextInRect")
displayModel_requestTextChangeNotificationsForWindow = _notImplemented(
	"displayModel_requestTextChangeNotificationsForWindow",
)

audioDucking_shouldDelay = _notImplemented("audioDucking_shouldDelay")
isScreenFullyBlack = _notImplemented("isScreenFullyBlack")
localListeningSocketExists = _notImplemented("localListeningSocketExists")
nvdaInProcUtils_dumpOnCrash = _notImplemented("nvdaInProcUtils_dumpOnCrash")
writeCrashDump = _notImplemented("writeCrashDump")


# The beep generator is nvdaHelper/local/beeps.cpp, statement for statement. hz arrives as a C float
# and sampleRate/hz is a float division there, so both are rounded to single precision here.
_BEEP_SAMPLE_RATE = 44100
_BEEP_AMPLITUDE = 14000


def _toFloat32(value: float) -> float:
	return ctypes.c_float(value).value


def _cRemainder(dividend: int, divisor: int) -> int:
	"""C's %, which takes the sign of the dividend."""
	return int(math.fmod(dividend, divisor))


def generateBeep(buf: Any, hz: float, length: int, left: int = 50, right: int = 50) -> int:
	"""Fill buf with a beep as 16-bit stereo samples at 44.1 kHz, and return its size in bytes.

	Called with None for buf, it only returns the size, which is what tones.beep allocates.
	"""
	# The conversions ctypes made from upstream's argtypes, which also refuse what they refused.
	hz = _toFloat32(hz)
	length, left, right = (ctypes.c_int(n).value for n in (length, left, right))
	cyclesRatio = _toFloat32(_BEEP_SAMPLE_RATE / hz)
	samplesPerCycle = int(cyclesRatio)
	totalSamples = int((length / 1000.0) / (1.0 / _BEEP_SAMPLE_RATE))
	totalSamples += samplesPerCycle - _cRemainder(totalSamples, samplesPerCycle)
	size = totalSamples * 4
	if buf is None:
		return size
	if isinstance(buf, ctypes.Array) and ctypes.sizeof(buf) < size:
		raise ValueError(f"generateBeep needs {size} bytes, the buffer has {ctypes.sizeof(buf)}")
	lpan = (left / 100.0) * _BEEP_AMPLITUDE
	rpan = (right / 100.0) * _BEEP_AMPLITUDE
	sinFreq = (2.0 * math.pi) / cyclesRatio
	samples = array("h", bytes(size))
	for sampleNum in range(totalSamples):
		sample = min(max(math.sin((sampleNum % _BEEP_SAMPLE_RATE) * sinFreq) * 2.0, -1.0), 1.0)
		samples[sampleNum * 2] = int(sample * lpan)
		samples[sampleNum * 2 + 1] = int(sample * rpan)
	ctypes.memmove(buf, samples.tobytes(), size)
	return size


def _utf16Units(text: Any, textLength: int) -> tuple[Utf16Buffer, int] | None:
	"""The text as UTF-16 and the number of its code units to analyse, or None for no text.

	Callers pass a str, a Utf16Buffer, or a c_wchar buffer or pointer, which on Linux holds UTF-32
	and is converted. Upstream's C reads textLength units whatever the buffer holds; this never
	reads past the end.
	"""
	if text is None:
		return None
	if not isinstance(text, Utf16Buffer):
		if isinstance(text, (ctypes.Array, ctypes.c_wchar_p)):
			text = text.value
		if text is None:
			return None
		text = Utf16Buffer(text)
	return text, max(0, min(textLength, len(text) - 1))


def _store(out: Any, value: int) -> None:
	"""Write through an out parameter as the C function would: a byref(), a pointer or a c_int."""
	target = getattr(out, "_obj", None)
	if target is None:
		target = out.contents if isinstance(out, ctypes._Pointer) else out
	target.value = value


def _requireIcu(name: str) -> None:
	if not icu.ICU_AVAILABLE:
		raise NoLinuxImplementation(
			f"NVDAHelper.localLib.{name}",
			f"NVDAHelper.localLib.{name} needs ICU, which did not load from {icu.LIB_DIR_VARIABLE}",
		)


@contextmanager
def _characterBreaks(text: Utf16Buffer, textLength: int) -> Iterator[int]:
	status = icu.UErrorCode(0)
	iterator = icu.ubrk_open(icu.UBRK.CHARACTER, b"", text, textLength, ctypes.byref(status))
	if icu.U_FAILURE(status.value) or not iterator:
		raise RuntimeError(f"ubrk_open failed with status {status.value}")
	try:
		yield iterator
	finally:
		icu.ubrk_close(iterator)


def calculateCharacterBoundaries(text: Any, textLength: int, offsets: Any, offsetsCount: Any) -> bool:
	"""Store the start of every grapheme cluster, then textLength, in offsets, and their number in
	offsetsCount. Returns False for no text, as upstream does."""
	_requireIcu("calculateCharacterBoundaries")
	units = _utf16Units(text, textLength)
	if offsets is None or units is None or units[1] <= 0:
		return False
	text, textLength = units
	count = 0
	with _characterBreaks(text, textLength) as iterator:
		boundary = icu.ubrk_first(iterator)
		while boundary != icu.UBRK_DONE:
			offsets[count] = boundary
			count += 1
			boundary = icu.ubrk_next(iterator)
	_store(offsetsCount, count)
	return True


def calculateCharacterOffsets(
	text: Any, textLength: int, offset: int, startOffset: Any, endOffset: Any
) -> bool:
	"""Store the bounds of the grapheme cluster containing offset. Past the end of the text, the unit
	is the one code unit at offset, as upstream's is."""
	_requireIcu("calculateCharacterOffsets")
	units = _utf16Units(text, textLength)
	if offset < 0 or units is None:
		return False
	text, textLength = units
	if offset >= textLength:
		_store(startOffset, offset)
		_store(endOffset, offset + 1)
		return True
	with _characterBreaks(text, textLength) as iterator:
		end = icu.ubrk_following(iterator, offset)
		# Searching back from offset + 1 instead would go wrong when that is inside a surrogate pair,
		# which ICU first moves back to the start of the pair.
		start = icu.ubrk_preceding(iterator, textLength if end == icu.UBRK_DONE else end)
	_store(startOffset, 0 if start == icu.UBRK_DONE else start)
	_store(endOffset, textLength if end == icu.UBRK_DONE else end)
	return True


def calculateWordOffsets(text: Any, textLength: int, offset: int, startOffset: Any, endOffset: Any) -> bool:
	"""Store the bounds of the word containing offset, trailing whitespace included.

	Upstream's Uniscribe rules have no Linux counterpart, so this is ICU's word segmentation as
	textUtils.icu applies it, which upstream already made to keep Uniscribe's whitespace handling.
	Both of NVDA's word segmenters then agree on Linux.
	"""
	_requireIcu("calculateWordOffsets")
	# Imported on first use: NVDA's import graph resolves only in upstream's order, and upstream's
	# NVDAHelper does not import textUtils.
	from textUtils import icu as textUtilsIcu

	units = _utf16Units(text, textLength)
	if offset < 0 or units is None:
		return False
	text, textLength = units
	bounds = textUtilsIcu.calculateWordOffsets(text[:textLength], offset)
	if bounds is None:
		return False
	_store(startOffset, bounds[0])
	_store(endOffset, bounds[1])
	return True
