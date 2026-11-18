"""The LINDA_ variables that say where the native pieces are, and the values an installation fixed for
them when it was built.

The Meson build writes linuxPlatform.installPaths into the tree it installs, with the libraries it
built and the eSpeak NG, ICU and openevv it was given. A checkout has no such module, so there only
the environment counts. A variable that is set wins over the installed value, an empty one included,
because the dev shell and the tests name their own builds and switch pieces off that way.
"""

import os

try:
	from linuxPlatform.installPaths import values as _installed
except ImportError:
	_installed = {}


def get(variable: str) -> str | None:
	"""The variable's value from the environment, or else what the installation fixed for it."""
	if variable in os.environ:
		return os.environ[variable]
	return _installed.get(variable)
