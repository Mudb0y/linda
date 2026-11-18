"""Adds synthesizer drivers that are built outside this tree to the search path NVDA finds drivers on.

NVDA loads a synthesizer driver by importing it from the synthDrivers package, so a driver is available
when its directory is on synthDrivers.__path__. openevv's driver is kept in openevv's own source and
built into a directory of its own, laid out as its add-on is on Windows: the driver's three modules
beside openevv_engine/, which holds the engine library. An environment variable names that directory,
or an installation built with it names it (linuxPlatform.installSettings). Unlike testHooks, this is how
a built copy finds the driver too, so it acts in every kind of run.
"""

import os

from linuxPlatform import installSettings

#: Names the directory holding openevv.py, the modules it imports and openevv_engine/libeci.so.1.
openevvDriverDirVariable = "LINDA_OPENEVV_DRIVER_DIR"


def install() -> list[str]:
	"""Add the openevv driver's directory to synthDrivers.__path__, and return the directories added.

	The directory goes at the end, so that NVDA's own drivers and an add-on's driver of the same name
	come first, and it goes only once however often this is called and however it is spelled: through a
	symbolic link, or with ``.`` in it, it is still the directory already on the path. A variable that is
	unset, or names a directory without openevv.py in it, adds nothing.
	"""
	directory = installSettings.get(openevvDriverDirVariable)
	if not directory or not os.path.isfile(os.path.join(directory, "openevv.py")):
		return []
	directory = os.path.realpath(directory)
	import synthDrivers

	if any(os.path.realpath(entry) == directory for entry in synthDrivers.__path__):
		return []
	synthDrivers.__path__.append(directory)
	return [directory]
