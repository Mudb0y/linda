"""The GNOME guest's half of groundFocus.py: GNOME Shell in safe mode gives a test no way to focus a
window by its title, so the focus is moved with real keys, Alt+Tab, and by new windows, which mutter
focuses when nothing says otherwise, and it is read here from AT-SPI's active state, as Linda reads it.

The shell's own D-Bus interface is used only for what it allows any caller in safe mode: its version,
its mode, whether the Activities overview is shown, and hiding it, which the shell shows once it has
started and which would take every key typed.
"""

import gi

gi.require_version("Atspi", "2.0")
from gi.repository import Atspi, Gio, GLib

SHELL = "org.gnome.Shell"
SHELL_PATH = "/org/gnome/Shell"
#: The shell's own application on the AT-SPI desktop, whose "Main stage" is active while the Alt+Tab
#: switcher is up.
SHELL_APPLICATION = "gnome-shell"
#: Milliseconds an AT-SPI call may take, and one made while a program starts, before it is given up.
ATSPI_TIMEOUT = 2000
ATSPI_STARTUP_TIMEOUT = 5000
DBUS_TIMEOUT = 5000


def _shell(
	method: str, parameters: GLib.Variant, interface: str = "org.freedesktop.DBus.Properties"
) -> tuple:
	bus = Gio.bus_get_sync(Gio.BusType.SESSION)
	answer = bus.call_sync(SHELL, SHELL_PATH, interface, method, parameters, None, 0, DBUS_TIMEOUT, None)
	return answer.unpack()


def _property(name: str) -> object:
	return _shell("Get", GLib.Variant("(ss)", (SHELL, name)))[0]


def ready() -> bool:
	if _property("OverviewActive"):
		_shell("Set", GLib.Variant("(ssv)", (SHELL, "OverviewActive", GLib.Variant("b", False))))
		return False
	return True


def facts() -> dict:
	evaluated = _shell("Eval", GLib.Variant("(s)", ("1",)), SHELL)
	return {
		"shellVersion": _property("ShellVersion"),
		"mode": _property("Mode"),
		"overviewActive": _property("OverviewActive"),
		# The shell answers Eval only in unsafe mode.
		"unsafeMode": bool(evaluated[0]),
	}


def windows() -> list[dict]:
	Atspi.init()
	Atspi.set_timeout(ATSPI_TIMEOUT, ATSPI_STARTUP_TIMEOUT)
	desktop = Atspi.get_desktop(0)
	found = []
	for index in range(desktop.get_child_count()):
		try:
			application = desktop.get_child_at_index(index)
			if application is None:
				continue
			name = application.get_name()
			pid = application.get_process_id()
			count = application.get_child_count()
		except GLib.Error:
			continue
		for child in range(count):
			try:
				window = application.get_child_at_index(child)
				if window is None:
					continue
				states = window.get_state_set()
				found.append(
					{
						"application": name,
						"pid": pid,
						"title": window.get_name(),
						"role": window.get_role_name(),
						"active": states.contains(Atspi.StateType.ACTIVE),
						"showing": states.contains(Atspi.StateType.SHOWING),
						"shell": name == SHELL_APPLICATION,
					}
				)
			except GLib.Error:
				continue
	return found
