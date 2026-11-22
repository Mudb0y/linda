"""Stand-ins for a screen saver and for a name's owner on the session bus, and for logind on a bus of
the test's choosing.

	python3 lockServices.py saver NAME PATH [silent] [undumpable]
	python3 lockServices.py owner NAME
	python3 lockServices.py logind ADDRESS SESSION_PATH [SETTING...]

Each prints "ready" once it owns its name, then carries out one command a line from standard input
and prints "done" once what it sent has left for the bus:

	active true|false   the screen saver's ActiveChanged, which its GetActive then answers
	dialog              the screen saver starts a child named mate-screensaver-dialog, as
	                    mate-screensaver starts its unlock prompt, and prints "dialog PID" first
	nodialog            that child ends
	lock | unlock       logind's Lock or Unlock signal on the session
	hint true|false     logind's LockedHint, with its PropertiesChanged signal
	set SETTING         logind answers from now on as SETTING says
	calls               logind prints "calls JSON" first: each method it was asked, with how often,
	                    and under "pids" every process GetSessionByPID was asked of

A silent screen saver never answers GetActive. An undumpable one makes itself so before it connects,
as KWin's capabilities make kwin_wayland, so that no process of its user may read its environment.
An owner only owns its name, as GNOME Shell owns org.gnome.Shell.ScreenShield.

logind answers GetSession with SESSION_PATH, and by default GetSessionByPID with SESSION_PATH for
every process and GetUser for this process's user. A SETTING changes one answer:

	PID=PATH|none|denied
	                    GetSessionByPID for that process: the session, NoSessionForPID, or
	                    AccessDenied, which is no answer about the process at all
	others=ANSWER       the same for every process not named
	display=PATH        the user's Display session, "/" for none; SESSION_PATH by default
	user=false          GetUser answers NoSuchUser
"""

import ctypes
import json
import os
import shutil
import subprocess
import sys
import threading

from gi.repository import Gio, GLib

LOGIND = "org.freedesktop.login1"
MANAGER = "org.freedesktop.login1.Manager"
SESSION = "org.freedesktop.login1.Session"
USER = "org.freedesktop.login1.User"
_PR_SET_DUMPABLE = 4

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
		<method name="GetUser"><arg direction="in" type="u"/><arg direction="out" type="o"/></method>
	</interface>
	<interface name="{SESSION}">
		<property name="LockedHint" type="b" access="read"/>
		<signal name="Lock"/>
		<signal name="Unlock"/>
	</interface>
	<interface name="{USER}">
		<property name="Display" type="(so)" access="read"/>
	</interface>
</node>
"""

state = {"active": False, "hint": False}
dialogs = []
unanswered = []
#: How logind answers, as its settings say.
answers = {"pids": {}, "others": None, "display": None, "user": True}
calls: dict[str, object] = {"pids": []}


def applySetting(setting: str) -> None:
	key, _, value = setting.partition("=")
	if key == "user":
		answers["user"] = value != "false"
	elif key == "display":
		answers["display"] = value
	elif key == "others":
		answers["others"] = value
	else:
		answers["pids"][int(key)] = value


def sessionIdOf(path: str) -> str:
	# logind's paths escape a session ID's leading digit as _ and its two hex digits.
	name = path.rsplit("/", 1)[-1]
	return chr(int(name[1:3], 16)) + name[3:] if name.startswith("_") else name


def main() -> None:
	role = sys.argv[1]
	loop = GLib.MainLoop()
	if role == "saver":
		name, path = sys.argv[2], sys.argv[3]
		options = sys.argv[4:]
		silent = "silent" in options
		if "undumpable" in options:
			libc = ctypes.CDLL(None, use_errno=True)
			assert libc.prctl(_PR_SET_DUMPABLE, 0, 0, 0, 0) == 0, os.strerror(ctypes.get_errno())
		connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
		node = Gio.DBusNodeInfo.new_for_xml(SAVER_XML.format(name=name))

		def onCall(connection, sender, objectPath, interface, method, parameters, invocation):
			if silent:
				unanswered.append(invocation)
				return
			invocation.return_value(GLib.Variant("(b)", (state["active"],)))

		connection.register_object(path, node.interfaces[0], onCall, None, None)
	elif role == "owner":
		name = sys.argv[2]
		path = "/"
		connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
	else:
		address, sessionPath = sys.argv[2], sys.argv[3]
		for setting in sys.argv[4:]:
			applySetting(setting)
		name = LOGIND
		path = sessionPath
		userPath = f"/org/freedesktop/login1/user/_{os.getuid()}"
		connection = Gio.DBusConnection.new_for_address_sync(
			address,
			Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
			None,
			None,
		)
		node = Gio.DBusNodeInfo.new_for_xml(LOGIND_XML)

		def onManagerCall(connection, sender, objectPath, interface, method, parameters, invocation):
			calls[method] = calls.get(method, 0) + 1
			if method == "GetSessionByPID":
				(processID,) = parameters.unpack()
				calls["pids"].append(processID)
				answer = answers["pids"].get(processID, answers["others"] or sessionPath)
				if answer == "none":
					invocation.return_dbus_error(
						f"{LOGIND}.NoSessionForPID", f"PID {processID} does not belong to any known session"
					)
					return
				if answer == "denied":
					invocation.return_dbus_error("org.freedesktop.DBus.Error.AccessDenied", "Access denied")
					return
				invocation.return_value(GLib.Variant("(o)", (answer,)))
			elif method == "GetUser":
				(uid,) = parameters.unpack()
				if not answers["user"] or uid != os.getuid():
					invocation.return_dbus_error(f"{LOGIND}.NoSuchUser", f"User ID {uid} is not logged in")
					return
				invocation.return_value(GLib.Variant("(o)", (userPath,)))
			else:
				invocation.return_value(GLib.Variant("(o)", (sessionPath,)))

		def onGetProperty(connection, sender, objectPath, interface, propertyName):
			return GLib.Variant("b", state["hint"])

		def onGetUserProperty(connection, sender, objectPath, interface, propertyName):
			calls["Display"] = calls.get("Display", 0) + 1
			display = answers["display"] or sessionPath
			return GLib.Variant("(so)", ("" if display == "/" else sessionIdOf(display), display))

		connection.register_object(
			"/org/freedesktop/login1", node.lookup_interface(MANAGER), onManagerCall, None, None
		)
		connection.register_object(sessionPath, node.lookup_interface(SESSION), None, onGetProperty, None)
		connection.register_object(userPath, node.lookup_interface(USER), None, onGetUserProperty, None)
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
		if command == "dialog":
			# bash runs whatever its first argument names it, and waits on its builtin read.
			dialogs.append(
				subprocess.Popen(
					["mate-screensaver-dialog", "-c", "read -r line"],
					executable=shutil.which("bash"),
					stdin=subprocess.PIPE,
				),
			)
			print(f"dialog {dialogs[-1].pid}", flush=True)
		elif command == "nodialog":
			for dialog in dialogs:
				dialog.kill()
				dialog.wait()
			dialogs.clear()
		elif command == "active":
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
		elif command == "set":
			for setting in argument:
				applySetting(setting)
		elif command == "calls":
			print(f"calls {json.dumps(calls)}", flush=True)
		connection.flush_sync(None)
		print("done", flush=True)
	for dialog in dialogs:
		dialog.kill()
		dialog.wait()
	os._exit(0)


if __name__ == "__main__":
	main()
