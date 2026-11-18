"""Linda's name where NVDA would say its own.

Linda is NVDA brought to Linux under a name of its own. NVDA's messages stay as upstream wrote them,
since every message edited here would lose its translations and conflict at each upstream merge.
The translation languageHandler installs gives Linda instead wherever a message has the whole word
NVDA, in every language, except in a message that speaks of NV Access, which is about the upstream
project and its NVDA, and in one that names something of NVDA's own rather than the program running,
which UPSTREAM_NAMED_MESSAGES lists as the code gives it.
"""

import gettext
import re

import buildVersion

#: Linda's short name, which buildVersion gives on Linux.
NAME = buildVersion.name
LONG_NAME = "Linda Is Non-visual Desktop Access"
#: Where a problem with Linda is reported, which NV Access do not support.
ISSUES_URL = "https://github.com/Mudb0y/linda/issues"
UPSTREAM_NAME = "NVDA"
#: A message holding any of these speaks of NV Access, its site or its repositories.
_UPSTREAM_MARKS = ("NV Access", "nvaccess")
#: Messages, as the code gives them, that name something of NVDA's own, which a translation of them
#: keeps calling NVDA: the add-on store, whose add-ons are NVDA's (linuxPlatform.nvdaAddonStore).
UPSTREAM_NAMED_MESSAGES = frozenset({"NVDA {addonStore}"})
# ASCII bounds rather than \b, which would not end the word before a letter of a script that writes
# no spaces, as in 使用NVDA时.
_UPSTREAM_WORD = re.compile(r"(?<![A-Za-z0-9_])NVDA(?![A-Za-z0-9_])")
# Only in English, where "an NVDA modifier key" would otherwise become "an Linda modifier key". In
# German an is a preposition.
_ENGLISH_UPSTREAM_WORD = re.compile(
	r"(?:(?<![A-Za-z0-9_])(?P<article>[Aa])n )?(?<![A-Za-z0-9_])NVDA(?![A-Za-z0-9_])"
)


def _englishName(match: re.Match[str]) -> str:
	return f"{match['article']} {NAME}" if match["article"] else NAME


def inEnglish(text: str) -> str:
	"""English text that NVDA writes of itself without a translation, naming Linda."""
	return _ENGLISH_UPSTREAM_WORD.sub(_englishName, text)


class RenamingTranslations(gettext.NullTranslations):
	"""A translation that names Linda wherever the one it wraps names NVDA."""

	def __init__(self, upstream: gettext.NullTranslations) -> None:
		super().__init__()
		self.upstream = upstream
		# Messages are looked up on every object spoken; each one with the name is renamed once.
		self._renamed: dict[str, str] = {}
		self._renamedEnglish: dict[str, str] = {}

	def _rename(self, message: str, *untranslated: str) -> str:
		"""
		:param untranslated: The messages as the code gives them, one of which a lookup that found no
			translation returns as it is, in English.
		"""
		if UPSTREAM_NAME not in message or not UPSTREAM_NAMED_MESSAGES.isdisjoint(untranslated):
			return message
		isEnglish = any(message is source for source in untranslated)
		cache = self._renamedEnglish if isEnglish else self._renamed
		renamed = cache.get(message)
		if renamed is None:
			if any(mark in message for mark in _UPSTREAM_MARKS):
				renamed = message
			elif isEnglish:
				renamed = inEnglish(message)
			else:
				renamed = _UPSTREAM_WORD.sub(NAME, message)
			cache[message] = renamed
		return renamed

	def gettext(self, message: str) -> str:
		return self._rename(self.upstream.gettext(message), message)

	def ngettext(self, msgid1: str, msgid2: str, n: int) -> str:
		return self._rename(self.upstream.ngettext(msgid1, msgid2, n), msgid1, msgid2)

	def pgettext(self, context: str, message: str) -> str:
		return self._rename(self.upstream.pgettext(context, message), message)

	def npgettext(self, context: str, msgid1: str, msgid2: str, n: int) -> str:
		return self._rename(self.upstream.npgettext(context, msgid1, msgid2, n), msgid1, msgid2)

	def info(self) -> dict[str, str]:
		return self.upstream.info()

	def charset(self) -> str | None:
		return self.upstream.charset()


def upstreamGettext(message: str) -> str:
	"""A message as the interface language translates it, with NVDA left as it is."""
	import languageHandler

	installed = languageHandler.installedTranslation() if languageHandler.installedTranslation else None
	if isinstance(installed, RenamingTranslations):
		return installed.upstream.gettext(message)
	return _(message)


#: What NVDA's About message says, put the way that is true of Linda. English only, as no
#: translation of NVDA's has it.
_ABOUT = """\
{longName} ({name})
Version: {version} ({version_detailed})

{name} is NVDA, the NonVisual Desktop Access screen reader, brought to Linux. It is based on NVDA \
{version} by NV Access and the NVDA contributors, and is not affiliated with or endorsed by NV Access.
NVDA: {url}
{copyright}

{name}, like NVDA, is covered by the GNU General Public License (Version 2 or later).
You are free to share or change this software in any way you like as long as it is accompanied by \
the license and you make all source code available to anyone who wants it.
This applies to both original and modified copies of this software, plus any derivative works.
For further details, you can view the license from the Help menu.
It can also be viewed online at: https://www.gnu.org/licenses/old-licenses/gpl-2.0.html and \
https://www.gnu.org/licenses/gpl-3.0.en.html"""


def aboutMessage(version: str, version_detailed: str, url: str, copyright: str) -> str:
	return _ABOUT.format(
		longName=LONG_NAME,
		name=NAME,
		version=version,
		version_detailed=version_detailed,
		url=url,
		copyright=copyright,
	)
