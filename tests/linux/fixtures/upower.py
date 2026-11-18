"""A stand-in for UPower on whatever bus DBUS_SESSION_BUS_ADDRESS names: its daemon object, with
OnBattery, and its display device, with what NVDA reads of a battery.

Run inside the rig only, which has a session bus and no system bus; NVDA is pointed at it through
DBUS_SYSTEM_BUS_ADDRESS. It prints "ready" once it has the name. Each line of its standard input is
"NAME VALUE ...": OnBattery takes 0 or 1, IsPresent 0 or 1, Percentage a number, State, TimeToEmpty
and TimeToFull whole numbers. The properties a line names change together, after which PropertiesChanged
is sent for each object that changed, the device first, as UPower does. It prints "ok" for each line.
"""

import sys

from gi.repository import Gio, GLib

BUS_NAME = "org.freedesktop.UPower"
DAEMON_PATH = "/org/freedesktop/UPower"
DEVICE_PATH = "/org/freedesktop/UPower/devices/DisplayDevice"
DEVICE_INTERFACE = "org.freedesktop.UPower.Device"

INTROSPECTION = f"""
<node>
	<interface name="{BUS_NAME}">
		<property name="OnBattery" type="b" access="read"/>
		<property name="DaemonVersion" type="s" access="read"/>
	</interface>
	<interface name="{DEVICE_INTERFACE}">
		<property name="IsPresent" type="b" access="read"/>
		<property name="Type" type="u" access="read"/>
		<property name="Percentage" type="d" access="read"/>
		<property name="State" type="u" access="read"/>
		<property name="TimeToEmpty" type="x" access="read"/>
		<property name="TimeToFull" type="x" access="read"/>
	</interface>
</node>
"""

#: (object path, interface, D-Bus type) of each property.
PROPERTIES = {
	"OnBattery": (DAEMON_PATH, BUS_NAME, "b"),
	"DaemonVersion": (DAEMON_PATH, BUS_NAME, "s"),
	"IsPresent": (DEVICE_PATH, DEVICE_INTERFACE, "b"),
	"Type": (DEVICE_PATH, DEVICE_INTERFACE, "u"),
	"Percentage": (DEVICE_PATH, DEVICE_INTERFACE, "d"),
	"State": (DEVICE_PATH, DEVICE_INTERFACE, "u"),
	"TimeToEmpty": (DEVICE_PATH, DEVICE_INTERFACE, "x"),
	"TimeToFull": (DEVICE_PATH, DEVICE_INTERFACE, "x"),
}
values = {
	"OnBattery": False,
	"DaemonVersion": "1.90.0",
	"IsPresent": False,
	"Type": 0,
	"Percentage": 0.0,
	"State": 0,
	"TimeToEmpty": 0,
	"TimeToFull": 0,
}


def convert(name, text):
	kind = PROPERTIES[name][2]
	if kind == "b":
		return text not in ("0", "false", "False")
	if kind == "d":
		return float(text)
	return int(text)


def main():
	connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
	nodeInfo = Gio.DBusNodeInfo.new_for_xml(INTROSPECTION)

	def getProperty(_connection, _sender, path, interface, name):
		return GLib.Variant(PROPERTIES[name][2], values[name])

	for path, interface in ((DAEMON_PATH, BUS_NAME), (DEVICE_PATH, DEVICE_INTERFACE)):
		connection.register_object(path, nodeInfo.lookup_interface(interface), None, getProperty, None)
	loop = GLib.MainLoop()

	def acquired(*_args):
		print("ready", flush=True)

	Gio.bus_own_name_on_connection(connection, BUS_NAME, Gio.BusNameOwnerFlags.NONE, acquired, None)

	def onLine(channel, _condition):
		line = channel.readline()
		if not line:
			loop.quit()
			return False
		words = line.split()
		changed = {}
		for name, text in zip(words[::2], words[1::2]):
			values[name] = convert(name, text)
			changed.setdefault((PROPERTIES[name][0], PROPERTIES[name][1]), {})[name] = values[name]
		for (path, interface), names in sorted(changed.items(), key=lambda item: item[0][0] != DEVICE_PATH):
			connection.emit_signal(
				None,
				path,
				"org.freedesktop.DBus.Properties",
				"PropertiesChanged",
				GLib.Variant(
					"(sa{sv}as)",
					(interface, {n: GLib.Variant(PROPERTIES[n][2], v) for n, v in names.items()}, []),
				),
			)
		connection.flush_sync(None)
		print("ok", flush=True)
		return True

	channel = GLib.IOChannel.unix_new(sys.stdin.fileno())
	GLib.io_add_watch(channel, GLib.PRIORITY_DEFAULT, GLib.IOCondition.IN | GLib.IOCondition.HUP, onLine)
	loop.run()


if __name__ == "__main__":
	sys.exit(main())
