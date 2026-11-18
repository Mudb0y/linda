"""linda --register-desktop and --unregister-desktop: the desktop's own choice of screen reader.

MATE starts the screen reader that org.mate.applications-at-visual names in exec when its startup is
true, and its accessibility settings start the same one. Registering points that at Linda and turns
the start at login on; unregistering puts both back to the schema's defaults, Orca and off. Only these
two options, run by hand, write those keys: the desktop's choice of screen reader is the user's, and
a copy of Linda that wrote it as it started would take it from Orca for good. GNOME starts Orca by
name, so there is nothing to register there.
"""

import argparse
import shlex
import sys
from typing import NoReturn

from linuxPlatform import branding, launcher, xdg

REGISTER_OPTION = "--register-desktop"
UNREGISTER_OPTION = "--unregister-desktop"
OPTIONS = (REGISTER_OPTION, UNREGISTER_OPTION)
SCHEMA = "org.mate.applications-at-visual"
DESKTOP = "MATE"


class _Unchanged(Exception):
	"""Nothing was changed, for the reason the message gives as a clause."""


def addOptions(parser: argparse.ArgumentParser) -> None:
	"""List the two options in the help, though the launcher acts on them before parsing."""
	group = parser.add_argument_group(
		"desktop registration",
		"Each takes no other argument, changes the setting, says what it changed and exits without"
		f" starting {branding.NAME}.",
	)
	group.add_argument(
		REGISTER_OPTION,
		action="store_true",
		help=f"Make {branding.NAME} the screen reader that {DESKTOP} starts when you sign in",
	)
	group.add_argument(
		UNREGISTER_OPTION,
		action="store_true",
		help=f"Put {DESKTOP}'s choice of screen reader and whether it starts one back to their defaults",
	)


def _settings():
	if DESKTOP not in xdg.currentDesktops():
		running = ", ".join(xdg.currentDesktops()) or "not named in XDG_CURRENT_DESKTOP"
		raise _Unchanged(
			f"the running desktop is {running}, not {DESKTOP}, the only one whose choice of screen reader"
			f" {branding.NAME} knows how to set",
		)
	import gi

	gi.require_version("Gio", "2.0")
	from gi.repository import Gio

	source = Gio.SettingsSchemaSource.get_default()
	if source is None or source.lookup(SCHEMA, True) is None:
		raise _Unchanged(
			f"{DESKTOP}'s setting for its screen reader, the GSettings schema {SCHEMA}, is not installed"
		)
	return Gio, Gio.Settings.new(SCHEMA)


def _startsAtLogin(startup: bool) -> str:
	return "starts it when you sign in" if startup else "does not start it when you sign in"


def register() -> str:
	Gio, settings = _settings()
	try:
		command = launcher.lastingCommand()
	except launcher.NoLastingCommand as e:
		raise _Unchanged(str(e)) from None
	# MATE splits it as a shell would.
	command = shlex.quote(command)
	former = settings.get_string("exec")
	if former == command and settings.get_boolean("startup"):
		raise _Unchanged(f"{DESKTOP}'s screen reader is already {command}, which it starts when you sign in")
	settings.set_string("exec", command)
	settings.set_boolean("startup", True)
	Gio.Settings.sync()
	return (
		f"{DESKTOP}'s screen reader is now {command} in place of {former}, and {DESKTOP} starts it when you"
		" sign in."
	)


def unregister() -> str:
	Gio, settings = _settings()
	settings.reset("exec")
	settings.reset("startup")
	Gio.Settings.sync()
	return (
		f"{DESKTOP}'s screen reader is back to its default, {settings.get_string('exec')}, and {DESKTOP}"
		f" {_startsAtLogin(settings.get_boolean('startup'))}."
	)


def main(option: str) -> NoReturn:
	"""Carry out option, one of OPTIONS, say what it changed, and exit: with status 0 when it changed
	the setting and 1 when it did not."""
	try:
		print(register() if option == REGISTER_OPTION else unregister(), flush=True)
	except _Unchanged as e:
		print(f"Nothing was changed, because {e}.", flush=True)
		sys.exit(1)
	sys.exit(0)
