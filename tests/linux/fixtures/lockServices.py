"""Stand-ins for a screen saver on the session bus and for logind on a bus of the test's choosing.

	python3 lockServices.py saver NAME PATH
	python3 lockServices.py logind ADDRESS SESSION_PATH

Each prints "ready" once it owns its name, then carries out one command a line from standard input
and prints "done" once what it sent has left for the bus:

	active true|false   the screen saver's ActiveChanged, which its GetActive then answers
	lock | unlock       logind's Lock or Unlock signal on the session
	hint true|false     logind's LockedHint, with its PropertiesChanged signal

The screen saver also starts a child named mate-screensaver-dialog, and prints its process ID
first as "dialog PID", as mate-screensaver starts its unlock prompt.
"""

import os
import shutil
import subprocess
import sys
import threading

from gi.repository import Gio, GLib

LOGIND = "org.freedesktop.login1"
MANAGER = "org.freedesktop.login1.Manager"
SESSION = "org.freedesktop.login1.Session"

SAVER_XML = """
<node><interface name="{name}">
	<method name="GetActive"><arg direction="out" type="b"/></method>
	<signal name="ActiveChanged"><arg type="b"/></signal>
</interface></node>
"""
LOGIND_XML = f"""
<node>
	<interface name="{MANAGER}">
		<method name="GetSession"><arg direction="in" type="s"/><arg direction="out" type="o"/></method>
		<method name="GetSessionByPID"><arg direction="in" type="u"/><arg direction="out" type="o"/></method>
	</interface>
	<interface name="{SESSION}">
		<property name="LockedHint" type="b" access="read"/>
		<signal name="Lock"/>
		<signal name="Unlock"/>
	</interface>
</node>
"""

state = {"active": False, "hint": False}


def main() -> None:
	role = sys.argv[1]
	loop = GLib.MainLoop()
	if role == "saver":
		name, path = sys.argv[2], sys.argv[3]
		connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
		node = Gio.DBusNodeInfo.new_for_xml(SAVER_XML.format(name=name))

		def onCall(connection, sender, objectPath, interface, method, parameters, invocation):
			invocation.return_value(GLib.Variant("(b)", (state["active"],)))

		connection.register_object(path, node.interfaces[0], onCall, None, None)
		# bash runs whatever its first argument names it, and waits on its builtin read.
		dialog = subprocess.Popen(
			["mate-screensaver-dialog", "-c", "read -r line"],
			executable=shutil.which("bash"),
			stdin=subprocess.PIPE,
		)
		print(f"dialog {dialog.pid}", flush=True)
	else:
		address, sessionPath = sys.argv[2], sys.argv[3]
		name = LOGIND
		path = sessionPath
		connection = Gio.DBusConnection.new_for_address_sync(
			address,
			Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
			None,
			None,
		)
		node = Gio.DBusNodeInfo.new_for_xml(LOGIND_XML)

		def onManagerCall(connection, sender, objectPath, interface, method, parameters, invocation):
			invocation.return_value(GLib.Variant("(o)", (sessionPath,)))

		def onGetProperty(connection, sender, objectPath, interface, propertyName):
			return GLib.Variant("b", state["hint"])

		connection.register_object(
			"/org/freedesktop/login1", node.lookup_interface(MANAGER), onManagerCall, None, None
		)
		connection.register_object(sessionPath, node.lookup_interface(SESSION), None, onGetProperty, None)
	reply = connection.call_sync(
		"org.freedesktop.DBus",
		"/org/freedesktop/DBus",
		"org.freedesktop.DBus",
		"RequestName",
		GLib.Variant("(su)", (name, 0x4)),
		GLib.VariantType.new("(u)"),
		0,
		-1,
		None,
	).unpack()
	assert reply == (1,), reply
	threading.Thread(target=loop.run, daemon=True).start()
	print("ready", flush=True)
	for line in sys.stdin:
		command, *argument = line.split()
		value = argument == ["true"]
		if command == "active":
			state["active"] = value
			connection.emit_signal(None, path, name, "ActiveChanged", GLib.Variant("(b)", (value,)))
		elif command in ("lock", "unlock"):
			connection.emit_signal(None, path, SESSION, command.capitalize(), None)
		elif command == "hint":
			state["hint"] = value
			connection.emit_signal(
				None,
				path,
				"org.freedesktop.DBus.Properties",
				"PropertiesChanged",
				GLib.Variant("(sa{sv}as)", (SESSION, {"LockedHint": GLib.Variant("b", value)}, [])),
			)
		connection.flush_sync(None)
		print("done", flush=True)
	if role == "saver":
		dialog.kill()
		dialog.wait()
	os._exit(0)


if __name__ == "__main__":
	main()
