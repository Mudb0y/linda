"""Lets a launcher or test add drivers and plugins that exist only for testing, give NVDA a
stand-in for linda-keyd, and make the focus's news come late to the keys of a Wayland session.

NVDA finds synth drivers and global plugins by searching the __path__ of the synthDrivers and
globalPlugins packages. Add-ons and the scratchpad directory are the upstream ways in, but
--disable-addons, which is how tests run, turns both off, so this adds the directories named by an
environment variable directly. Nothing here acts unless that variable is set, nothing acts in secure
mode, and nothing acts unless NVDA runs from a checkout, with tests/linux beside source/: an
installed copy carries only source/, and must never run code that the environment names.

Call install() after the command line has been parsed, so that --secure is known.
"""

import importlib.abc
import importlib.machinery
import os
import sys
from collections.abc import Sequence
from importlib.machinery import ModuleSpec
from types import ModuleType

#: Names a directory that may hold synthDrivers/ and globalPlugins/ subdirectories.
ENVIRONMENT_VARIABLE = "LINDA_TEST_HOOKS_DIR"
#: Names the socket of a test's stand-in for linda-keyd, honoured as ENVIRONMENT_VARIABLE is.
KEYD_SOCKET_VARIABLE = "LINDA_TEST_KEYD_SOCKET"
#: Names the milliseconds the capture feed holds back window events, honoured as ENVIRONMENT_VARIABLE
#: is, and taken up to MAX_CAPTURE_DELAY.
CAPTURE_DELAY_VARIABLE = "LINDA_TEST_CAPTURE_DELAY_MS"
MAX_CAPTURE_DELAY = 10000

#: The packages whose search paths get the subdirectory of the same name.
packageNames = ("synthDrivers", "globalPlugins")

#: NVDA's source directory, the one that holds linuxPlatform.
_sourceDir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _isRunningFromCheckout() -> bool:
	return os.path.isdir(os.path.join(os.path.dirname(_sourceDir), "tests", "linux"))


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


def keydSocketPath() -> str | None:
	"""The socket of a test's stand-in for linda-keyd, named by KEYD_SOCKET_VARIABLE, which takes the
	place of the helper's own; None outside a test.

	Only where test drivers would be added, so that an installed copy never gives its keys to whatever
	the environment names.
	"""
	path = os.environ.get(KEYD_SOCKET_VARIABLE)
	if not path or not os.environ.get(ENVIRONMENT_VARIABLE):
		return None
	import globalVars

	if globalVars.appArgs.secure or not _isRunningFromCheckout():
		return None
	return path


def captureDelay() -> int:
	"""Milliseconds the capture feed of a Wayland session holds back each window event, named by
	CAPTURE_DELAY_VARIABLE, so that a test can make the focus's news come late; 0 outside a test, or
	when the value is not a whole number of milliseconds from 0 to MAX_CAPTURE_DELAY.

	Only where test drivers would be added, so that an installed copy never slows its own keys for
	whatever the environment says.
	"""
	value = os.environ.get(CAPTURE_DELAY_VARIABLE)
	if not value or not os.environ.get(ENVIRONMENT_VARIABLE):
		return 0
	import globalVars

	if globalVars.appArgs.secure or not _isRunningFromCheckout():
		return 0
	try:
		milliseconds = int(value)
	except ValueError:
		return 0
	return milliseconds if 0 <= milliseconds <= MAX_CAPTURE_DELAY else 0


def install() -> list[str]:
	"""Add the test directories to the packages' search paths, and return the directories added.

	Does nothing, and returns an empty list, when the variable is unset, in secure mode, or when NVDA
	does not run from a checkout. Safe to call more than once. The directories go at the end of each
	search path, so a test driver or plugin can never take the place of a built-in or an add-on of
	the same name.
	"""
	root = os.environ.get(ENVIRONMENT_VARIABLE)
	if not root:
		return []
	import globalVars

	if globalVars.appArgs.secure or not _isRunningFromCheckout():
		return []
	if not os.path.isdir(root):
		# A test that set the variable expects its drivers; carrying on without them would make it
		# fail later and further from the cause.
		raise FileNotFoundError(f"{ENVIRONMENT_VARIABLE} names {root!r}, which is not a directory")
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
