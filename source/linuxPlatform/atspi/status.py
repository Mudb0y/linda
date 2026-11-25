"""Telling the desktop that a screen reader is running, so that programs expose what it reads.

The accessibility bus launcher keeps org.a11y.Status on the session bus. Chromium, and every Electron
program with it, reads IsEnabled once, as it starts: off, it never joins the accessibility bus, and
nothing short of a restart changes that. Firefox reads it at start and again whenever it changes. GTK 3
and GTK 4 do not read it, and Qt on X11 joins the bus whenever the display names one.

at-spi2-core 2.61 and later switch IsEnabled on by themselves when a client registers for events and
off when the last such client leaves; there it is not a setting but a report, and they have no
ScreenReaderEnabled. Earlier launchers keep both as the desktop's settings, and on GNOME and MATE
ScreenReaderEnabled is the setting that starts Orca, so it is never touched here.

So IsEnabled is switched on, before NVDA registers for events, when it is off. When NVDA stops, a
launcher that reports it is left to switch it off once no screen reader remains, which writes none of
the desktop's settings, as switching it off here would; an earlier launcher gets back what was found.

A Chromium or Electron program started while IsEnabled was off, as one is between one screen reader
quitting and the next starting, cannot be read until it is restarted, and NVDA names it as it starts.
"""

import os

from gi.repository import Gio, GLib
from logHandler import log

BUS_NAME = "org.a11y.Bus"
OBJECT_PATH = "/org/a11y/bus"
INTERFACE = "org.a11y.Status"
PROPERTIES_INTERFACE = "org.freedesktop.DBus.Properties"
ENABLED = "IsEnabled"
#: Only launchers that keep the status as settings have it.
SCREEN_READER_ENABLED = "ScreenReaderEnabled"
#: The launcher answers at once; a session without one must not hold NVDA's start up.
TIMEOUT_MS = 1000
#: What the command line of Chromium's zygote holds, and no main process's.
ZYGOTE_SWITCH = b"--type=zygote"
#: What the command line of every process a Chromium main process starts for itself holds.
CHILD_SWITCH = b"--type="
#: Where the parent's process ID and the start time are among the fields _stat gives.
PARENT_FIELD = 1
STARTTIME_FIELD = 19
#: Milliseconds after NVDA's start before the programs it cannot read are named, so that what NVDA says
#: as it starts comes first.
REPORT_DELAY_MS = 3000

#: Whether claim switched IsEnabled on on a launcher that keeps it as a setting, for release to put back.
_putBack = False


def _sessionBus() -> Gio.DBusConnection | None:
	try:
		return Gio.bus_get_sync(Gio.BusType.SESSION, None)
	except GLib.Error as e:
		log.debugWarning(f"Accessibility status: no session bus: {e.message}")
		return None


def _get(bus: Gio.DBusConnection, name: str) -> bool | None:
	"""The property's value, or None when the launcher does not have it or does not answer."""
	try:
		reply = bus.call_sync(
			BUS_NAME,
			OBJECT_PATH,
			PROPERTIES_INTERFACE,
			"Get",
			GLib.Variant("(ss)", (INTERFACE, name)),
			GLib.VariantType("(v)"),
			Gio.DBusCallFlags.NONE,
			TIMEOUT_MS,
			None,
		)
	except GLib.Error as e:
		log.debug(f"Accessibility status: {name} cannot be read: {e.message}")
		return None
	value = reply.unpack()[0]
	return value if isinstance(value, bool) else None


def _set(bus: Gio.DBusConnection, name: str, value: bool) -> bool:
	try:
		bus.call_sync(
			BUS_NAME,
			OBJECT_PATH,
			PROPERTIES_INTERFACE,
			"Set",
			GLib.Variant("(ssv)", (INTERFACE, name, GLib.Variant("b", value))),
			None,
			Gio.DBusCallFlags.NONE,
			TIMEOUT_MS,
			None,
		)
	except GLib.Error as e:
		log.debugWarning(f"Accessibility status: {name} cannot be set: {e.message}")
		return False
	return True


def claim() -> None:
	"""Switch IsEnabled on when it is off, and remember whether it must be put back."""
	global _putBack
	bus = _sessionBus()
	if bus is None or _get(bus, ENABLED) is not False:
		return
	if not _set(bus, ENABLED, True):
		return
	_putBack = _get(bus, SCREEN_READER_ENABLED) is not None
	log.info(f"Accessibility status: switched {ENABLED} on")


def _stat(processID: int | str) -> list[bytes]:
	"""The fields of /proc/PID/stat after the command name, from the state on."""
	with open(f"/proc/{processID}/stat", "rb") as f:
		return f.read().rsplit(b")", 1)[1].split()


def _commandLine(processID: int | str) -> bytes:
	with open(f"/proc/{processID}/cmdline", "rb") as f:
		return f.read(4096)


def _isSandbox(processID: str) -> bool:
	"""Whether the process is Flatpak's bubblewrap, which holds the command line of a sandboxed zygote it
	starts for an app such as Discord: neither it nor the process that started it, the app's own
	bubblewrap or flatpak-portal, is a Chromium program."""
	from linuxPlatform import processes

	path = processes.findExecutablePath(int(processID))
	return path is not None and os.path.basename(path) in processes.SANDBOX_PROGRAMS


def _chromiumMainProcesses() -> list[int]:
	"""The main processes of Chromium and of every Electron program that started before NVDA: those
	that started a zygote, the helper Chromium forks its pages from, whose command line says so. One
	that started after NVDA read the status on, and may still be joining the bus."""
	try:
		nvdaStarted = int(_stat("self")[STARTTIME_FIELD])
	except (OSError, ValueError, IndexError):
		return []
	mains = []
	for entry in os.scandir("/proc"):
		if not entry.name.isdecimal():
			continue
		try:
			if ZYGOTE_SWITCH not in _commandLine(entry.name) or _isSandbox(entry.name):
				continue
			parent = int(_stat(entry.name)[PARENT_FIELD])
			if parent <= 1 or parent in mains or int(_stat(parent)[STARTTIME_FIELD]) >= nvdaStarted:
				continue
			# A sandboxed zygote forks once more to be the first process of its own namespace, so the
			# zygote that serves pages is the child of another zygote, not of the main process.
			if CHILD_SWITCH in _commandLine(parent):
				continue
		except (OSError, ValueError, IndexError):
			continue
		mains.append(parent)
	return mains


def unreadablePrograms() -> list[str]:
	"""The names of Chromium and Electron programs running without joining the accessibility bus, as
	they do when they started while IsEnabled was off."""
	from appModuleHandler import getAppNameFromProcessID

	from . import handler

	mains = _chromiumMainProcesses()
	if not mains:
		return []
	joined = {handler.processIDOf(app) for app in handler.applications()}
	names = []
	for processID in mains:
		if processID in joined:
			continue
		name = getAppNameFromProcessID(processID)
		if name and name not in names:
			names.append(name)
	return names


def reportUnreadable() -> None:
	"""Say which Chromium and Electron programs cannot be read until they are restarted, once NVDA has
	said that it is running."""

	def report() -> None:
		import ui

		names = unreadablePrograms()
		if names:
			log.info(f"Accessibility status: started before it was on: {', '.join(names)}")
			ui.message(
				# Translators: Said as NVDA starts, of programs such as Chromium or Discord that were started
				# while accessibility was off and cannot be read until they are started again.
				ngettext(
					"{programs} was started before accessibility was on and cannot be read until it is restarted",
					"{programs} were started before accessibility was on and cannot be read until they are restarted",
					len(names),
				).format(programs=", ".join(names)),
			)

	import core

	core.callLater(REPORT_DELAY_MS, report)


def release() -> None:
	"""Put back what claim switched on, where the launcher keeps it as a setting."""
	global _putBack
	if not _putBack:
		return
	_putBack = False
	bus = _sessionBus()
	if bus is not None and _set(bus, ENABLED, False):
		log.info(f"Accessibility status: switched {ENABLED} off again")
