"""A stand-in for org.freedesktop.ScreenSaver's inhibitors on the session bus, as KDE and Xfce serve
them.

	python3 idleServices.py EVENTS

Prints "ready" once it owns the name, and appends a line to EVENTS for each Inhibit, "inhibit SENDER
COOKIE", each UnInhibit, "uninhibit SENDER COOKIE", and each sender whose connection closes while it
holds an inhibitor, "vanished SENDER", which the real services drop that sender's inhibitors for.
"""

import sys

from gi.repository import Gio, GLib

NAME = "org.freedesktop.ScreenSaver"
PATH = "/org/freedesktop/ScreenSaver"
XML = f"""
<node><interface name="{NAME}">
	<method name="Inhibit">
		<arg direction="in" type="s"/><arg direction="in" type="s"/><arg direction="out" type="u"/>
	</method>
	<method name="UnInhibit"><arg direction="in" type="u"/></method>
</interface></node>
"""


def main() -> None:
	eventsPath = sys.argv[1]
	loop = GLib.MainLoop()
	connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
	held: dict[int, str] = {}
	cookies = iter(range(1, 1_000_000))

	def record(line: str) -> None:
		with open(eventsPath, "a", encoding="utf-8") as f:
			f.write(line + "\n")

	def onCall(connection, sender, path, interface, method, parameters, invocation) -> None:
		if method == "Inhibit":
			cookie = next(cookies)
			held[cookie] = sender
			record(f"inhibit {sender} {cookie}")
			invocation.return_value(GLib.Variant("(u)", (cookie,)))
		else:
			(cookie,) = parameters.unpack()
			held.pop(cookie, None)
			record(f"uninhibit {sender} {cookie}")
			invocation.return_value(None)

	def onOwnerChanged(connection, sender, path, interface, signal, parameters) -> None:
		name, _old, new = parameters.unpack()
		if new or name not in held.values():
			return
		for cookie in [c for c, s in held.items() if s == name]:
			del held[cookie]
		record(f"vanished {name}")

	info = Gio.DBusNodeInfo.new_for_xml(XML)
	connection.register_object(PATH, info.interfaces[0], onCall, None, None)
	connection.signal_subscribe(
		"org.freedesktop.DBus",
		"org.freedesktop.DBus",
		"NameOwnerChanged",
		"/org/freedesktop/DBus",
		None,
		Gio.DBusSignalFlags.NONE,
		onOwnerChanged,
	)

	def onAcquired(*args) -> None:
		print("ready", flush=True)

	def onLost(*args) -> None:
		print(f"could not own {NAME}", flush=True)
		loop.quit()

	Gio.bus_own_name_on_connection(connection, NAME, Gio.BusNameOwnerFlags.NONE, onAcquired, onLost)
	loop.run()


if __name__ == "__main__":
	main()
