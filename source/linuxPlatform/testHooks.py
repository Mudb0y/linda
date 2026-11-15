"""Lets a launcher or test add drivers and plugins that exist only for testing.

NVDA finds synth drivers and global plugins by searching the __path__ of the synthDrivers and
globalPlugins packages. Add-ons and the scratchpad directory are the upstream ways in, but both are
turned off by --disable-addons and --secure, which is how tests run, so this adds the directories
named by an environment variable directly. Nothing here acts unless that variable is set.
"""

import os
from types import ModuleType

#: Names a directory that may hold synthDrivers/ and globalPlugins/ subdirectories.
environmentVariable = "BASEDREADER_TEST_HOOKS_DIR"


def _packages() -> tuple[ModuleType, ...]:
	import globalPlugins
	import synthDrivers

	return (synthDrivers, globalPlugins)


def install() -> list[str]:
	"""Add the test directories to the packages' search paths, and return the directories added.

	Safe to call more than once. The directories go at the end of each search path, so a test
	driver or plugin can never take the place of a built-in or an add-on of the same name.
	"""
	root = os.environ.get(environmentVariable)
	if not root:
		return []
	if not os.path.isdir(root):
		# A test that set the variable expects its drivers; carrying on without them would make it
		# fail later and further from the cause.
		raise FileNotFoundError(f"{environmentVariable} names {root!r}, which is not a directory")
	added = []
	for package in _packages():
		directory = os.path.join(os.path.abspath(root), package.__name__)
		if os.path.isdir(directory) and directory not in package.__path__:
			package.__path__.append(directory)
			added.append(directory)
	return added
