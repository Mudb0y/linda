"""linda --register-desktop and --unregister-desktop: the desktop's own choice of screen reader.

MATE keeps its choice of screen reader in org.mate.applications-at-visual: exec names it and startup
says whether mate-session starts it at sign-in. mate-session reads exec as the name of a desktop
entry, as its default, orca, is, and starts that entry's program in the entry's phase; a path names no
entry, and starts nothing. MATE's own settings for its preferred applications write exec the same way,
as the program of the entry chosen.

Registering names Linda's application entry in exec, so that MATE knows Linda as its screen reader,
and has Linda start at sign-in through Linda's own autostart entry, the one the "start after I sign
in" setting writes, which starts it in the Initialization phase, before the rest of the desktop, and
works on other desktops too. startup is left off, so that mate-session never starts a second copy from
the application entry. Unregistering puts exec and startup back to the schema's defaults, Orca and off,
and turns Linda's autostart entry off. Only these two options, run by hand, write those keys: the
desktop's choice of screen reader is the user's, and a copy of Linda that wrote it as it started would
take it from Orca for good. GNOME starts Orca by name, so there is nothing to register there.
"""

import argparse
import sys
from typing import NoReturn

from linuxPlatform import branding, launcher, xdg

REGISTER_OPTION = "--register-desktop"
UNREGISTER_OPTION = "--unregister-desktop"
OPTIONS = (REGISTER_OPTION, UNREGISTER_OPTION)
SCHEMA = "org.mate.applications-at-visual"
DESKTOP = "MATE"
#: The name of Linda's application entry, linda.desktop, as mate-session finds it among the
#: applications, and the program it starts.
ENTRY_NAME = launcher.LAUNCHER_NAME


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
		help=f"Make {branding.NAME} {DESKTOP}'s screen reader, and start it when you sign in",
	)
	group.add_argument(
		UNREGISTER_OPTION,
		action="store_true",
		help=f"Put {DESKTOP}'s choice of screen reader back to its default, and stop {branding.NAME}"
		" starting when you sign in",
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


def _entryText() -> str | None:
	try:
		with open(xdg.autostartEntryPath(xdg.autostartEntryName), encoding="utf-8") as f:
			return f.read()
	except OSError:
		return None


def _writeEntry(command: str, enable: bool) -> None:
	try:
		xdg.writeAutostartEntry(xdg.autostartEntryName, branding.NAME, command, enable)
	except OSError as e:
		raise _Unchanged(
			f"{xdg.autostartEntryPath(xdg.autostartEntryName)} could not be written: {e}"
		) from None


def _sentence(parts: list[str]) -> str:
	return " ".join(f"{part[0].upper()}{part[1:]}." for part in parts)


def register() -> str:
	Gio, settings = _settings()
	try:
		command = launcher.lastingCommand()
	except launcher.NoLastingCommand as e:
		raise _Unchanged(str(e)) from None
	formerExec = settings.get_string("exec")
	formerStartup = settings.get_boolean("startup")
	wasStarting = xdg.isAutostartEnabled(xdg.autostartEntryName)
	formerEntry = _entryText()
	# First, as it is what can fail, and MATE's keys are then left as they were.
	_writeEntry(command, True)
	entryChanged = _entryText() != formerEntry
	settings.set_string("exec", ENTRY_NAME)
	settings.set_boolean("startup", False)
	Gio.Settings.sync()
	changed = []
	if formerExec != ENTRY_NAME:
		changed.append(f"{DESKTOP}'s screen reader is now {ENTRY_NAME} in place of {formerExec}")
	if formerStartup:
		changed.append(
			f"{DESKTOP} no longer starts its screen reader itself when you sign in, so that only one copy of"
			f" {branding.NAME} starts"
		)
	entry = xdg.autostartEntryPath(xdg.autostartEntryName)
	if not wasStarting:
		changed.append(
			f"{branding.NAME} now starts when you sign in, before the rest of the desktop, from {entry}"
		)
	elif entryChanged:
		changed.append(f"{entry} now starts {command} when you sign in, before the rest of the desktop")
	if not changed:
		raise _Unchanged(
			f"{DESKTOP}'s screen reader is already {ENTRY_NAME}, and {branding.NAME} already starts when you"
			f" sign in, from {entry}"
		)
	return _sentence(changed)


def unregister() -> str:
	Gio, settings = _settings()
	formerExec = settings.get_string("exec")
	formerStartup = settings.get_boolean("startup")
	wasStarting = xdg.isAutostartEnabled(xdg.autostartEntryName)
	if wasStarting:
		# A disabled entry names no command that has to start anything, so a copy without a lasting
		# command can write it too.
		_writeEntry(launcher.LAUNCHER_NAME, False)
	settings.reset("exec")
	settings.reset("startup")
	Gio.Settings.sync()
	default = settings.get_string("exec")
	changed = []
	if formerExec != default:
		changed.append(
			f"{DESKTOP}'s screen reader is back to its default, {default}, in place of {formerExec}"
		)
	if formerStartup != settings.get_boolean("startup"):
		changed.append(f"{DESKTOP} no longer starts its screen reader when you sign in")
	if wasStarting:
		changed.append(
			f"{branding.NAME} no longer starts when you sign in, as"
			f" {xdg.autostartEntryPath(xdg.autostartEntryName)} is turned off"
		)
	if not changed:
		raise _Unchanged(
			f"{DESKTOP}'s screen reader is already its default, {default}, and {branding.NAME} does not start"
			" when you sign in"
		)
	return _sentence(changed)


def main(option: str) -> NoReturn:
	"""Carry out option, one of OPTIONS, say what it changed, and exit: with status 0 when it changed
	anything and 1 when it did not."""
	try:
		print(register() if option == REGISTER_OPTION else unregister(), flush=True)
	except _Unchanged as e:
		print(f"Nothing was changed, because {e}.", flush=True)
		sys.exit(1)
	sys.exit(0)
