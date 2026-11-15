"""The user's language and locale, read from the POSIX environment the way glibc reads it.

NVDA's languageHandler asks Windows these questions; on Linux this module answers them.
"""

import ctypes
import functools
import locale
import os
from collections.abc import Collection

from logHandler import log

#: glibc's number for the LC_MEASUREMENT category, which Python's locale module does not name.
_LC_MEASUREMENT = 11
#: nl_langinfo's item for LC_MEASUREMENT's measurement system: 1 is metric, 2 is US customary.
_NL_MEASUREMENT_MEASUREMENT = _LC_MEASUREMENT << 16
_IMPERIAL = 2

#: ICU's UDISPCTX_CAPITALIZATION_FOR_UI_LIST_OR_MENU and UDISPCTX_NO_SUBSTITUTE.
_ICU_DISPLAY_CONTEXTS = (0x103, 0x301)


def _nvdaName(posixName: str) -> str | None:
	"""A POSIX locale name such as "pt_BR.UTF-8@euro" as NVDA spells a language: "pt_BR".
	None for the C and POSIX locales, which name no language."""
	name = posixName.partition("@")[0].partition(".")[0]
	if name in ("", "C", "POSIX"):
		return None
	language, _, region = name.replace("-", "_").partition("_")
	return f"{language.lower()}_{region.upper()}" if region else language.lower()


def _messagesLocale() -> str:
	"""The environment's locale for messages, by glibc's precedence."""
	return next((value for name in ("LC_ALL", "LC_MESSAGES", "LANG") if (value := os.environ.get(name))), "")


def messagesLanguages() -> list[str]:
	"""The languages the user wants messages in, most wanted first, as NVDA spells them.

	glibc consults the LANGUAGE list only when the messages locale is not C, so neither does this.
	"""
	main = _nvdaName(_messagesLocale())
	if main is None:
		return []
	wanted = [_nvdaName(name) for name in os.environ.get("LANGUAGE", "").split(":")]
	wanted.append(main)
	return list(dict.fromkeys(name for name in wanted if name))


def uiLanguage(available: Collection[str]) -> str:
	"""The first wanted language NVDA has an interface translation for, trying each without its region
	too, or English when there is none."""
	for name in messagesLanguages():
		for candidate in (name, name.partition("_")[0]):
			if candidate in available:
				return candidate
	return "en"


def _utf8Locales(localeName: str) -> list[str]:
	language, _, region = localeName.partition("_")
	candidates = []
	environment = _messagesLocale()
	envName = _nvdaName(environment)
	if envName and envName.partition("_")[0] == language and (not region or envName == localeName):
		# The user's own locale for this language keeps the conventions of their region.
		base = environment.partition("@")[0].partition(".")[0]
		modifier = environment.partition("@")[2]
		candidates.append(f"{base}.UTF-8@{modifier}" if modifier else f"{base}.UTF-8")
	if region:
		candidates.append(f"{language}_{region}.UTF-8")
	else:
		# locale.normalize knows each language's usual region, but its codeset is often not UTF-8.
		usual = locale.normalize(language).partition("@")[0].partition(".")[0]
		if "_" in usual:
			candidates.append(f"{usual}.UTF-8")
	return list(dict.fromkeys(candidates))


def setLocale(localeName: str) -> str:
	"""Set every category of the C library's locale to a UTF-8 locale for an NVDA language such as
	"pl" or "pt_BR", or to C.UTF-8 when the system has none installed. Returns the locale set."""
	for candidate in _utf8Locales(localeName):
		try:
			result = locale.setlocale(locale.LC_ALL, candidate)
		except locale.Error:
			log.debug(f"locale {candidate} is not available")
			continue
		log.debug(f"set the C locale to {result}")
		return result
	# glibc has C.UTF-8 built in, so it is there even without a locale archive.
	result = locale.setlocale(locale.LC_ALL, "C.UTF-8")
	log.debug(f"no locale for {localeName} is available, set the C locale to {result}")
	return result


@functools.cache
def _libc() -> ctypes.CDLL:
	libc = ctypes.CDLL(None)
	libc.newlocale.restype = ctypes.c_void_p
	libc.newlocale.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_void_p)
	# The result is a pointer into locale data, not a string: the value's byte is followed directly by
	# unrelated data, so reading it as a C string runs past it.
	libc.nl_langinfo_l.restype = ctypes.c_void_p
	libc.nl_langinfo_l.argtypes = (ctypes.c_int, ctypes.c_void_p)
	libc.freelocale.restype = None
	libc.freelocale.argtypes = (ctypes.c_void_p,)
	return libc


def usesImperialMeasurements() -> bool:
	"""Whether the user's measurement locale uses US customary units, read from the environment
	rather than from the process's locale, which NVDA sets to its own interface language."""
	libc = _libc()
	measurementLocale = libc.newlocale(1 << _LC_MEASUREMENT, b"", None)
	if not measurementLocale:
		# The user's locale is not installed, so programs measure as the C locale does, in metric.
		return False
	try:
		value = libc.nl_langinfo_l(_NL_MEASUREMENT_MEASUREMENT, measurementLocale)
		return ctypes.string_at(value, 1)[0] == _IMPERIAL
	finally:
		libc.freelocale(measurementLocale)


class _Icu:
	"""The few ICU functions that name languages, from the library the dev shell or package names."""

	def __init__(self):
		libDir = os.environ.get("BASEDREADER_ICU_LIB_DIR")
		version = os.environ.get("BASEDREADER_ICU_VERSION_MAJOR")
		if not libDir or not version:
			raise RuntimeError("BASEDREADER_ICU_LIB_DIR and BASEDREADER_ICU_VERSION_MAJOR must name ICU")
		lib = ctypes.CDLL(os.path.join(libDir, f"libicuuc.so.{version}"))
		# ICU suffixes every exported name with its major version.
		self.openForContext = getattr(lib, f"uldn_openForContext_{version}")
		self.openForContext.restype = ctypes.c_void_p
		self.openForContext.argtypes = (
			ctypes.c_char_p,
			ctypes.POINTER(ctypes.c_int),
			ctypes.c_int32,
			ctypes.POINTER(ctypes.c_int),
		)
		self.localeDisplayName = getattr(lib, f"uldn_localeDisplayName_{version}")
		self.localeDisplayName.restype = ctypes.c_int32
		self.localeDisplayName.argtypes = (
			ctypes.c_void_p,
			ctypes.c_char_p,
			ctypes.POINTER(ctypes.c_uint16),
			ctypes.c_int32,
			ctypes.POINTER(ctypes.c_int),
		)


@functools.cache
def _icu() -> _Icu:
	return _Icu()


@functools.cache
def _displayNames(displayLocale: str) -> int:
	"""ICU's names of languages in displayLocale. Kept for the life of the process; there are few."""
	icu = _icu()
	contexts = (ctypes.c_int * len(_ICU_DISPLAY_CONTEXTS))(*_ICU_DISPLAY_CONTEXTS)
	status = ctypes.c_int(0)
	handle = icu.openForContext(displayLocale.encode(), contexts, len(contexts), ctypes.byref(status))
	if status.value > 0 or not handle:
		raise RuntimeError(f"ICU cannot name languages in {displayLocale}: error {status.value}")
	return handle


def _icuDisplayName(code: str, displayLocale: str) -> str | None:
	icu = _icu()
	handle = _displayNames(displayLocale)
	size = 128
	while True:
		buffer = (ctypes.c_uint16 * size)()
		status = ctypes.c_int(0)
		length = icu.localeDisplayName(handle, code.encode(), buffer, size, ctypes.byref(status))
		if length <= size or status.value <= 0:
			break
		size = length
	if status.value > 0:
		# Asked not to substitute, ICU fails rather than return the code for a language it cannot name.
		return None
	return bytes(buffer)[: 2 * length].decode("utf-16-le")


def languageDescription(code: str) -> str | None:
	"""The name of an NVDA language such as "pl" or "pt_BR", in the language of the user's desktop, as
	Windows names it in the user's display language. None for a language nothing can name."""
	# Translated at import, so imported only after a translation is installed.
	import localesData

	# NVDA's translators named these few, and chose the names deliberately.
	description = localesData.LANG_NAMES_TO_LOCALIZED_DESCS.get(code)
	if description:
		return description
	displayLocale = next(iter(messagesLanguages()), "en")
	return _icuDisplayName(code, displayLocale)
