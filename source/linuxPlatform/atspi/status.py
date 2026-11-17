"""Telling the desktop that a screen reader is running, so that programs expose what it reads.

The accessibility bus launcher keeps org.a11y.Status on the session bus. Chromium, and every Electron
program with it, reads IsEnabled once, as it starts: off, it never joins the accessibility bus, and
nothing short of a restart changes that. Firefox reads it at start and again whenever it changes. GTK
3, GTK 4 and Qt on X11 join the bus whatever it says. at-spi2-core 2.61 and later switch IsEnabled on
by themselves as soon as a client registers for events and off when the last one leaves, and no longer
have ScreenReaderEnabled; earlier ones keep both where a client sets them, and Qt reads either. So both
are switched on here, before NVDA registers for events, and each one this switched on is switched off
again when NVDA stops.
"""

from gi.repository import Gio, GLib
from logHandler import log

BUS_NAME = "org.a11y.Bus"
OBJECT_PATH = "/org/a11y/bus"
INTERFACE = "org.a11y.Status"
PROPERTIES_INTERFACE = "org.freedesktop.DBus.Properties"
#: The properties a program may read to decide whether a screen reader is there.
PROPERTIES = ("IsEnabled", "ScreenReaderEnabled")
#: The launcher answers at once; a session without one must not hold NVDA's start up.
TIMEOUT_MS = 1000

#: The properties claim found off and switched on, for release to switch off.
_switchedOn: list[str] = []


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
	"""Switch on each property the launcher has that is off, and remember which."""
	bus = _sessionBus()
	if bus is None:
		return
	for name in PROPERTIES:
		if name in _switchedOn or _get(bus, name) is not False:
			continue
		if _set(bus, name, True):
			_switchedOn.append(name)
			log.info(f"Accessibility status: switched {name} on")


def release() -> None:
	"""Switch off what claim switched on."""
	if not _switchedOn:
		return
	bus = _sessionBus()
	names = list(_switchedOn)
	_switchedOn.clear()
	if bus is None:
		return
	for name in names:
		if _set(bus, name, False):
			log.info(f"Accessibility status: switched {name} off again")
