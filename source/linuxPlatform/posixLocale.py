"""The user's language and locale, read from the POSIX environment the way glibc reads it.

NVDA's languageHandler asks Windows these questions; on Linux this module answers them.
"""

import ctypes
import functools
import locale
import os
from collections.abc import Callable, Collection

from logHandler import log

from linuxPlatform import NoLinuxImplementation, utf16

#: glibc's number for the LC_MEASUREMENT category, which Python's locale module does not name.
_LC_MEASUREMENT = 11
#: nl_langinfo's item for LC_MEASUREMENT's measurement system: 1 is metric, 2 is US customary.
_NL_MEASUREMENT_MEASUREMENT = _LC_MEASUREMENT << 16
_IMPERIAL = 2

#: ICU's UDISPCTX_CAPITALIZATION_FOR_UI_LIST_OR_MENU and UDISPCTX_NO_SUBSTITUTE.
_ICU_DISPLAY_CONTEXTS = (0x103, 0x301)
#: ICU's ULOC_FULLNAME_CAPACITY, the longest locale ID it writes, with room for the NUL.
_ICU_LOCALE_ID_CAPACITY = 157


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


def _translatedAs(name: str, available: Collection[str]) -> str | None:
	"""How NVDA would name a wanted language so that setLanguage finds a translation for it, or None.

	setLanguage tries a name and then its language alone, so either having a translation keeps the name
	as it is, as Windows reports "pl_PL" for Polish. A language given alone, as LANGUAGE lists usually
	give them, may have a translation only for a region, which setLanguage would not find.
	"""
	language, _, region = name.partition("_")
	if name in available or language in available:
		return name
	if region:
		return None
	# Python's copy of glibc's locale aliases knows each language's usual region, and aliases such as
	# "polish". Failing that, the regional translations are taken in a fixed order.
	usual = _nvdaName(locale.normalize(name))
	if usual and usual != name and (usual in available or usual.partition("_")[0] in available):
		return usual
	return next((code for code in sorted(available) if code.startswith(f"{language}_")), None)


def uiLanguage(available: Collection[str]) -> str:
	"""The language the user wants NVDA's interface in, as Windows reports its display language.

	Windows has one display language and LANGUAGE may list several, so the first listed that NVDA has a
	translation for is the one. When none has, the first wanted language is reported all the same, as
	Windows reports a display language NVDA has no translation for: setLanguage falls back from it as
	it does there, and other callers, such as word segmentation, want the user's real language.
	"""
	wanted = messagesLanguages()
	for name in wanted:
		if translated := _translatedAs(name, available):
			return translated
	return wanted[0] if wanted else "en"


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
	"""The ICU functions that name languages and regions, from the library winBindings.icu loaded."""

	def __init__(self, bind: Callable[..., Callable[..., int]]) -> None:
		""":param bind: winBindings.icu.bindFunction, which knows the suffix ICU puts on every name."""
		status = ctypes.POINTER(ctypes.c_int)
		self.openForContext = bind(
			"uldn_openForContext",
			ctypes.c_void_p,
			ctypes.c_char_p,
			ctypes.POINTER(ctypes.c_int),
			ctypes.c_int32,
			status,
		)
		# Each writes a UTF-16 name for a code: (names, code, buffer, capacity, status) -> length.
		nameArgs = (ctypes.c_void_p, ctypes.c_char_p, utf16.Utf16Pointer, ctypes.c_int32, status)
		self.localeDisplayName = bind("uldn_localeDisplayName", ctypes.c_int32, *nameArgs)
		self.languageDisplayName = bind("uldn_languageDisplayName", ctypes.c_int32, *nameArgs)
		self.regionDisplayName = bind("uldn_regionDisplayName", ctypes.c_int32, *nameArgs)
		# Each writes a locale ID or part of one: (locale, buffer, capacity, status) -> length.
		idArgs = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int32, status)
		self.addLikelySubtags = bind("uloc_addLikelySubtags", ctypes.c_int32, *idArgs)
		self.getCountry = bind("uloc_getCountry", ctypes.c_int32, *idArgs)


@functools.cache
def _icu() -> _Icu | None:
	"""ICU, or None when it is not available, which is said once.

	Language names are a convenience; without them the language list shows bare codes, as
	getLanguageDescription's callers expect for a language nothing can name.
	"""
	# Imported here, as winBindings.icu loads ICU when it is first imported.
	from winBindings import icu

	if not icu.ICU_AVAILABLE:
		log.warning(
			f"ICU is not available, so languages are not named; {icu.LIB_DIR_VARIABLE}"
			f" and {icu.VERSION_MAJOR_VARIABLE} name the library",
		)
		return None
	try:
		return _Icu(icu.bindFunction)
	except AttributeError:
		log.warning("ICU cannot name languages, so languages are not named", exc_info=True)
		return None


@functools.cache
def _displayNames(icu: _Icu, displayLocale: str) -> int | None:
	"""ICU's names of languages in displayLocale, or None when ICU has none, which is said once.
	Kept for the life of the process; there are few."""
	contexts = (ctypes.c_int * len(_ICU_DISPLAY_CONTEXTS))(*_ICU_DISPLAY_CONTEXTS)
	status = ctypes.c_int(0)
	handle = icu.openForContext(displayLocale.encode(), contexts, len(contexts), ctypes.byref(status))
	if status.value > 0 or not handle:
		log.warning(f"ICU cannot name languages in {displayLocale}: error {status.value}")
		return None
	return handle


def _icuName(nameFunction: str, code: str, displayLocale: str) -> str | None:
	"""What ICU's uldn function nameFunction calls code in displayLocale, or None when it has no name."""
	icu = _icu()
	if icu is None:
		return None
	handle = _displayNames(icu, displayLocale)
	if handle is None:
		return None
	function = getattr(icu, nameFunction)
	size = 128
	while True:
		buffer = (ctypes.c_uint16 * size)()
		status = ctypes.c_int(0)
		length = function(handle, code.encode(), buffer, size, ctypes.byref(status))
		if length <= size or status.value <= 0:
			break
		size = length
	if status.value > 0 or length <= 0:
		# Asked not to substitute, ICU fails rather than return the code for a name it does not know.
		return None
	return memoryview(buffer)[:length].tobytes().decode(utf16.ENCODING)


def _icuLocaleId(function: Callable[..., int], localeId: str) -> str:
	"""What one of ICU's uloc functions makes of localeId, or "" when it cannot."""
	buffer = ctypes.create_string_buffer(_ICU_LOCALE_ID_CAPACITY)
	status = ctypes.c_int(0)
	function(localeId.encode(), buffer, len(buffer), ctypes.byref(status))
	return "" if status.value > 0 else buffer.value.decode()


def englishLanguageName(code: str) -> str | None:
	"""The English name of the language of an NVDA language such as "pt_BR" or "pt-BR": "Portuguese".
	None for a language ICU does not know."""
	name = _nvdaName(code)
	if name is None:
		return None
	return _icuName("languageDisplayName", name.partition("_")[0], "en")


def englishCountryName(code: str) -> str | None:
	"""The English name of the country of an NVDA language such as "pt_BR": "Brazil". A language
	given alone, such as "de", is taken in the country it is most likely to be used in, as Windows
	takes it. None when ICU knows of no such country."""
	name = _nvdaName(code)
	icu = _icu()
	if name is None or icu is None:
		return None
	region = _icuLocaleId(icu.getCountry, _icuLocaleId(icu.addLikelySubtags, name))
	if not region:
		return None
	return _icuName("regionDisplayName", region, "en")


def ansiCodePage(code: str) -> str:
	"""Windows' ANSI code page for an NVDA language. Linux has none: text is UTF-8 throughout, and
	the code page is wanted only to name a Windows C runtime locale, which nothing does on Linux."""
	raise NoLinuxImplementation("languageHandler.ansiCodePageFromNVDALocale")


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
	return _icuName("localeDisplayName", code, displayLocale)
