"""Lets a launcher or test add drivers and plugins that exist only for testing.

NVDA finds synth drivers and global plugins by searching the __path__ of the synthDrivers and
globalPlugins packages. Add-ons and the scratchpad directory are the upstream ways in, but
--disable-addons, which is how tests run, turns both off, so this adds the directories named by an
environment variable directly. Nothing here acts unless that variable is set, and nothing acts in
secure mode or in a built copy, which must never run code that the environment names.

Call install() only on a test launch, after the command line has been parsed, so that --secure is
known.
"""

import importlib.abc
import importlib.machinery
import os
import sys
from collections.abc import Sequence
from importlib.machinery import ModuleSpec
from types import ModuleType

#: Names a directory that may hold synthDrivers/ and globalPlugins/ subdirectories.
environmentVariable = "BASEDREADER_TEST_HOOKS_DIR"

#: The packages whose search paths get the subdirectory of the same name.
packageNames = ("synthDrivers", "globalPlugins")


def _addDirectory(searchPath: list[str], root: str, packageName: str) -> str | None:
	directory = os.path.join(root, packageName)
	if os.path.isdir(directory) and directory not in searchPath:
		searchPath.append(directory)
		return directory
	return None


class _HookFinder(importlib.abc.MetaPathFinder):
	"""Gives the test directory to the packages each time they are imported.

	globalPluginHandler.reloadGlobalPlugins removes globalPlugins from sys.modules and imports it
	again, with a fresh search path, so adding to the search path once is not enough.
	"""

	def __init__(self, root: str) -> None:
		self.root = root

	def find_spec(
		self,
		fullname: str,
		path: Sequence[str] | None,
		target: ModuleType | None = None,
	) -> ModuleSpec | None:
		if fullname not in packageNames:
			return None
		spec = importlib.machinery.PathFinder.find_spec(fullname, path)
		if spec is not None and spec.submodule_search_locations is not None:
			_addDirectory(spec.submodule_search_locations, self.root, fullname)
		return spec


def install() -> list[str]:
	"""Add the test directories to the packages' search paths, and return the directories added.

	Safe to call more than once. The directories go at the end of each search path, so a test
	driver or plugin can never take the place of a built-in or an add-on of the same name.
	"""
	root = os.environ.get(environmentVariable)
	if not root:
		return []
	import globalVars
	import NVDAState

	if globalVars.appArgs.secure or not NVDAState.isRunningAsSource():
		return []
	if not os.path.isdir(root):
		# A test that set the variable expects its drivers; carrying on without them would make it
		# fail later and further from the cause.
		raise FileNotFoundError(f"{environmentVariable} names {root!r}, which is not a directory")
	root = os.path.abspath(root)
	import globalPlugins
	import synthDrivers

	added = []
	for package in (synthDrivers, globalPlugins):
		directory = _addDirectory(package.__path__, root, package.__name__)
		if directory:
			added.append(directory)
	if not any(isinstance(finder, _HookFinder) for finder in sys.meta_path):
		# Ahead of the path finder, which would otherwise answer first, and behind every finder
		# that replaces modules.
		position = sys.meta_path.index(importlib.machinery.PathFinder)
		sys.meta_path.insert(position, _HookFinder(root))
	return added
