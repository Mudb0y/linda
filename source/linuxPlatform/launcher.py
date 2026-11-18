"""The command that starts this copy of Linda, and will still start it after an update.

Every layout that installs Linda puts its launcher at PREFIX/bin/linda and the program at
PREFIX/share/linda/source, the Nix package as much as a Meson install, so the launcher is found from
the program's own directory. A desktop entry or a desktop setting that starts Linda must name a command
that outlives this copy: a Nix store path goes away when the store is collected after an update, while
the profile's bin directory or /usr/bin keeps naming whichever copy is installed.
"""

import os
import shutil

#: The launcher's name in PREFIX/bin.
LAUNCHER_NAME = "linda"
_NIX_STORE = "/nix/store/"


class NoLastingCommand(Exception):
	"""This copy has no command that would still start it after an update.

	The message is a clause saying why, fit to follow "because" in a sentence for the user.
	"""


def appDir() -> str:
	"""The program's directory, PREFIX/share/linda/source when installed."""
	return os.path.dirname(os.path.dirname(os.path.realpath(__file__)))


def ownLauncher() -> str:
	"""Where this copy's launcher is when it is installed, PREFIX/bin/linda."""
	prefix = os.path.dirname(os.path.dirname(os.path.dirname(appDir())))
	return os.path.join(prefix, "bin", LAUNCHER_NAME)


def lastingCommand() -> str:
	"""The absolute path of a launcher that starts this copy and still will after an update.

	That is the linda on PATH when it is this copy's launcher, as from a Nix profile,
	/run/current-system/sw/bin or /usr/bin; otherwise this copy's launcher itself, unless that is in
	the Nix store, where nothing keeps it once another copy is installed.

	:raise NoLastingCommand: When there is no such command.
	"""
	launcher = ownLauncher()
	if not (os.path.isfile(launcher) and os.access(launcher, os.X_OK)):
		raise NoLastingCommand(
			"this copy runs from a source tree rather than from an installed launcher,"
			" so there is no command that would start it again",
		)
	onPath = shutil.which(LAUNCHER_NAME)
	if onPath is not None and os.path.realpath(onPath) == os.path.realpath(launcher):
		return os.path.abspath(onPath)
	if not launcher.startswith(_NIX_STORE):
		return launcher
	raise NoLastingCommand(
		"this copy was started straight from the Nix store, as nix run starts it,"
		" and its path there stops working once it is updated;"
		" install it with nix profile install first",
	)
