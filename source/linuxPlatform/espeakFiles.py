"""Where eSpeak NG's library and its data are on Linux.

NVDA's Windows build puts espeak.dll and espeak-ng-data in synthDrivers, beside the driver. On Linux
both come from a build of the eSpeak NG commit NVDA pins, made without audio output, and the dev
shell or the launcher's wrapper names them in two environment variables. Neither is looked for
anywhere else: eSpeak given a data path that does not exist falls back without a word to
ESPEAK_DATA_PATH, ~/espeak-ng-data or the path compiled into it, and speaks with whatever it finds
there.
"""

import errno
import os

#: Names libespeak-ng.so.1.
libraryVariable = "LINDA_ESPEAK_LIB"
#: Names the espeak-ng-data directory.
dataDirVariable = "LINDA_ESPEAK_DATA_DIR"


def _named(variable: str) -> str:
	value = os.environ.get(variable)
	if not value:
		raise FileNotFoundError(errno.ENOENT, f"{variable} is not set, so eSpeak NG cannot be found")
	return value


def libraryPath() -> str:
	"""The path of the eSpeak NG library to load.

	:raises FileNotFoundError: The variable is unset, or names no file.
	"""
	path = _named(libraryVariable)
	if not os.path.isfile(path):
		raise FileNotFoundError(errno.ENOENT, f"{libraryVariable} names no file", path)
	return path


def dataDir() -> str:
	"""The espeak-ng-data directory, which espeak_Initialize accepts as readily as the parent NVDA
	passes on Windows.

	eSpeak itself checks only that the path it is given is a directory, so this checks for the
	variants directory, which the driver lists.

	:raises FileNotFoundError: The variable is unset, or names no eSpeak NG data.
	"""
	path = _named(dataDirVariable)
	if not os.path.isdir(os.path.join(path, "voices", "!v")):
		raise FileNotFoundError(errno.ENOENT, f"{dataDirVariable} names no eSpeak NG data", path)
	return path
