"""The import hook that puts Linux files in place of named modules.

A directory placed ahead of source/ on sys.path cannot do this job: a submodule such as
winAPI.sessionTracking is looked up through its parent package's __path__, not through sys.path.
So a finder on sys.meta_path answers for exactly the dotted names listed below, and for nothing
else. Each name's file sits at the same dotted path under replacements/ or standIns/.
"""

import importlib.abc
import importlib.util
import os
import sys

#: Upstream modules that are Windows to the core but imported by code that stays.
#: The upstream file is never loaded; the file at the same path under replacements/ is.
replacedModules: tuple[str, ...] = (
	"IAccessibleHandler",
	"NVDAHelper",
	"NVDAHelper.localLib",
	"UIAHandler",
	"easeOfAccess",
	"wasapi",
	"watchdog",
	"winAPI.sessionTracking",
)

#: Windows-only third-party packages and the comInterfaces modules NVDA's build generates from
#: Windows type libraries. Neither the standard library nor any dependency in the dev shell
#: probes for these names.
standInModules: tuple[str, ...] = (
	"comInterfaces.Accessibility",
	"comInterfaces.IAccessible2Lib",
	"comInterfaces.SpeechLib",
	"comInterfaces.UIAutomationClient",
	"comInterfaces.tom",
	"comtypes",
	"comtypes.automation",
	"comtypes.client",
	"comtypes.client.dynamic",
	"comtypes.client.lazybind",
	"comtypes.gen",
	"comtypes.hresult",
	"comtypes.persist",
	"pycaw",
	"pycaw.api",
	"pycaw.api.audiopolicy",
	"pycaw.callbacks",
	"pycaw.constants",
	"pycaw.utils",
	"winsound",
)

#: The standard library changes behaviour when these import (mimetypes, subprocess, shutil,
#: urllib, platform, getpass), so nothing may ever make them importable on Linux.
neverProvided: frozenset[str] = frozenset({"winreg", "msvcrt", "_winapi"})

_here = os.path.dirname(os.path.abspath(__file__))


def _locate(baseDir: str, fullname: str) -> tuple[str, bool]:
	"""The file for a dotted name under baseDir, and whether it is a package."""
	parts = fullname.split(".")
	packageInit = os.path.join(baseDir, *parts, "__init__.py")
	if os.path.isfile(packageInit):
		return packageInit, True
	moduleFile = os.path.join(baseDir, *parts[:-1], parts[-1] + ".py")
	if os.path.isfile(moduleFile):
		return moduleFile, False
	raise FileNotFoundError(f"no file for {fullname} under {baseDir}")


def _buildTable() -> dict[str, tuple[str, bool]]:
	table = {}
	for baseName, names in (("replacements", replacedModules), ("standIns", standInModules)):
		baseDir = os.path.join(_here, baseName)
		for fullname in names:
			if fullname.split(".")[0] in neverProvided:
				raise RuntimeError(f"{fullname} must never be importable on Linux")
			if fullname in table:
				raise RuntimeError(f"{fullname} is both replaced and stood in")
			table[fullname] = _locate(baseDir, fullname)
	return table


class PlatformFinder(importlib.abc.MetaPathFinder):
	def __init__(self):
		self._table = _buildTable()

	def find_spec(self, fullname, path, target=None):
		entry = self._table.get(fullname)
		if entry is None:
			return None
		location, isPackage = entry
		# A package's submodules come from this table or not at all, so an unlisted
		# submodule of a replaced package fails to import instead of loading upstream's.
		return importlib.util.spec_from_file_location(
			fullname,
			location,
			submodule_search_locations=[] if isPackage else None,
		)


def install() -> None:
	if any(isinstance(f, PlatformFinder) for f in sys.meta_path):
		return
	for name in neverProvided:
		if name in sys.modules:
			raise RuntimeError(f"{name} is already imported, which Linux must never allow")
	sys.meta_path.insert(0, PlatformFinder())
