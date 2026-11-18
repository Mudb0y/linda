"""Linux replacement for winBindings.icu: ICU's break iterators from the dev shell's ICU.

Upstream binds the ICU that Windows ships. On Linux the library is the one LINDA_ICU_LIB_DIR
names, or the installation fixed (linuxPlatform.installSettings), never one found through the loader's
search path, and a Linux ICU build suffixes every C symbol with its major version,
LINDA_ICU_VERSION_MAJOR. The names here are upstream's, plus the
character break type and the two iteration functions NVDAHelper.localLib needs. Text goes in as
UTF-16 code units through linuxPlatform.utf16, because c_wchar_p is UTF-32 on Linux.

The ``ubrk_*`` functions are defined only when :data:`ICU_AVAILABLE` is True, as upstream's are.
Reaching for one that is not defined, or for any other name this module lacks, raises
linuxPlatform.NoLinuxAttribute naming it, which is both NoLinuxImplementation and AttributeError.
"""

import functools
import os
from collections.abc import Callable
from ctypes import CDLL, POINTER, c_char_p, c_int32, c_void_p
from enum import IntEnum
from typing import Any, NoReturn

from linuxPlatform import installSettings, missingAttribute
from linuxPlatform.utf16 import Utf16Pointer

LIB_DIR_VARIABLE = "LINDA_ICU_LIB_DIR"
VERSION_MAJOR_VARIABLE = "LINDA_ICU_VERSION_MAJOR"


def _load() -> tuple[CDLL | None, str]:
	libDir = installSettings.get(LIB_DIR_VARIABLE)
	major = installSettings.get(VERSION_MAJOR_VARIABLE)
	if not libDir or not major:
		return None, ""
	try:
		library = CDLL(os.path.join(libDir, f"libicuuc.so.{major}"))
	except OSError:
		return None, ""
	# A library of another major version loads but lacks these names, and is then not usable.
	if not hasattr(library, f"ubrk_open_{major}"):
		return None, ""
	return library, major


dll, _versionMajor = _load()

ICU_AVAILABLE: bool = dll is not None
"""True if an ICU library was successfully loaded."""

UBRK_DONE: int = -1
"""Returned by iterator functions when there are no more boundaries."""

UErrorCode = c_int32
"""Signed 32-bit integer error code. U_ZERO_ERROR = 0; positive values indicate errors."""


class UBRK(IntEnum):
	"""The possible types of text boundaries (UBreakIteratorType)."""

	CHARACTER = 0
	"""Extended grapheme cluster breaks."""

	WORD = 1
	"""Word breaks."""

	SENTENCE = 3
	"""Sentence breaks."""


def U_FAILURE(code: int) -> bool:
	"""Return True if the given UErrorCode indicates an error."""
	return code > 0


def __getattr__(name: str) -> NoReturn:
	missingAttribute(
		__name__,
		name,
		None if ICU_AVAILABLE else f"{__name__}.{name}: ICU did not load from {LIB_DIR_VARIABLE}",
	)


def _bind(library: CDLL, name: str, restype: type | None, argtypes: tuple[type, ...]) -> Callable[..., Any]:
	function = library[f"{name}_{_versionMajor}"]
	function.restype = restype
	function.argtypes = argtypes
	return function


def bindFunction(name: str, restype: type | None, *argtypes: type) -> Callable[..., Any]:
	"""A function of the loaded libicuuc by its C name without the version suffix, typed as given.

	Other Linux code that needs ICU binds through this, so that ICU is found one way only. Call it
	only when :data:`ICU_AVAILABLE` is True.

	:raises AttributeError: If the library has no such function.
	"""
	return _bind(dll, name, restype, argtypes)


@functools.cache
def _formattingLibrary() -> CDLL:
	return CDLL(os.path.join(installSettings.get(LIB_DIR_VARIABLE), f"libicui18n.so.{_versionMajor}"))


def bindFormattingFunction(name: str, restype: type | None, *argtypes: type) -> Callable[..., Any]:
	"""As bindFunction, for a function of libicui18n, where ICU keeps what writes dates and numbers.
	That library is loaded from beside libicuuc when the first function is asked for.

	:raises OSError: If the library does not load.
	:raises AttributeError: If the library has no such function.
	"""
	return _bind(_formattingLibrary(), name, restype, argtypes)


if ICU_AVAILABLE:
	ubrk_open = bindFunction(
		"ubrk_open",
		c_void_p,
		c_int32,  # kind: UBreakIteratorType
		c_char_p,  # locale: UTF-8 locale ID or NULL
		Utf16Pointer,  # text: UTF-16 text to analyze
		c_int32,  # textLength: code units, or -1 for NUL-terminated
		POINTER(UErrorCode),  # status: in/out error code
	)
	"""Create a new break iterator; it must be freed with ubrk_close."""

	ubrk_close = bindFunction("ubrk_close", None, c_void_p)
	"""Free a break iterator created by ubrk_open."""

	ubrk_preceding = bindFunction("ubrk_preceding", c_int32, c_void_p, c_int32)
	"""Return the largest boundary position strictly less than offset, and move there."""

	ubrk_following = bindFunction("ubrk_following", c_int32, c_void_p, c_int32)
	"""Return the smallest boundary position strictly greater than offset, and move there.

	Returns UBRK_DONE if past the end.
	"""

	ubrk_first = bindFunction("ubrk_first", c_int32, c_void_p)
	"""Move to the first boundary, which is always 0, and return it."""

	ubrk_next = bindFunction("ubrk_next", c_int32, c_void_p)
	"""Move to the next boundary and return it, or UBRK_DONE after the last."""
