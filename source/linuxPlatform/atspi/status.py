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
"""

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


def release() -> None:
	"""Put back what claim switched on, where the launcher keeps it as a setting."""
	global _putBack
	if not _putBack:
		return
	_putBack = False
	bus = _sessionBus()
	if bus is not None and _set(bus, ENABLED, False):
		log.info(f"Accessibility status: switched {ENABLED} off again")
