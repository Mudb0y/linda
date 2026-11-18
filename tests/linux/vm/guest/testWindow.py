"""A GTK 3 window for Linda to read in the guest, which records every key that reaches it.

	testWindow.py OUT [TITLE]

The window has a menu bar (File with Open and Quit, Help with About) and a text field named Name,
which has the focus once the window is shown. Every key event GDK hands the program, for any of its
windows and the menus' among them, is written to OUT before GTK sees it, in the form keyRecorder.py
uses, with "t" when GDK handed it over, "serverTime" the X server's time of the event, "window" the title of the window it came to or
"menu" for a menu's, and "keyval", GDK's name for the key. Records of kind "menu" say a menu was
shown or hidden, and a first record of kind "ready" says the window is shown and its field focused.
"""

import json
import sys
import time

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from evdev import ecodes
from gi.repository import Gdk, GLib, Gtk

#: X key codes are evdev codes plus this.
X_KEYCODE_OFFSET = 8


def keyName(code: int) -> str:
	name = ecodes.KEY.get(code, str(code))
	return name[0] if isinstance(name, list) else name


class Recorder:
	def __init__(self, path: str) -> None:
		self._file = open(path, "a", buffering=1)  # noqa: SIM115

	def __call__(self, kind: str, **fields: object) -> None:
		self._file.write(json.dumps({"kind": kind, "received": time.monotonic(), **fields}) + "\n")

	def onEvent(self, event: Gdk.Event, _data: object = None) -> None:
		if event.type in (Gdk.EventType.KEY_PRESS, Gdk.EventType.KEY_RELEASE):
			code = event.hardware_keycode - X_KEYCODE_OFFSET
			widget = Gtk.get_event_widget(event)
			toplevel = widget.get_toplevel() if widget else None
			if not isinstance(toplevel, Gtk.Window):
				window = "none"
			elif toplevel.get_window_type() == Gtk.WindowType.POPUP:
				# A menu is a popup window of its own, which has no title.
				window = "menu"
			else:
				window = toplevel.get_title()
			self(
				"key",
				t=time.monotonic(),
				serverTime=event.time / 1000,
				window=window,
				code=code,
				key=keyName(code),
				keyval=Gdk.keyval_name(event.keyval),
				value=1 if event.type == Gdk.EventType.KEY_PRESS else 0,
			)
		Gtk.main_do_event(event)


def build(record: Recorder, title: str) -> Gtk.Window:
	window = Gtk.Window(title=title)
	window.set_default_size(400, 200)
	window.connect("destroy", Gtk.main_quit)
	box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
	window.add(box)

	menuBar = Gtk.MenuBar()
	for menuName, items in (("_File", ("_Open", "_Quit")), ("_Help", ("_About",))):
		item = Gtk.MenuItem.new_with_mnemonic(menuName)
		menu = Gtk.Menu()
		for itemName in items:
			menu.append(Gtk.MenuItem.new_with_mnemonic(itemName))
		menu.connect("show", lambda _menu, name=menuName: record("menu", change="shown", menu=name))
		menu.connect("hide", lambda _menu, name=menuName: record("menu", change="hidden", menu=name))
		item.set_submenu(menu)
		menuBar.append(item)
	box.pack_start(menuBar, False, False, 0)

	row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
	entry = Gtk.Entry()
	label = Gtk.Label.new_with_mnemonic("_Name")
	label.set_mnemonic_widget(entry)
	row.pack_start(label, False, False, 0)
	row.pack_start(entry, True, True, 0)
	box.pack_start(row, False, False, 0)

	def ready() -> bool:
		record("ready")
		return False

	def onFocusIn(*_args: object) -> bool:
		entry.grab_focus()
		GLib.idle_add(ready)
		window.disconnect(handler)
		return False

	handler = window.connect("focus-in-event", onFocusIn)
	window.show_all()
	window.present()
	return window


def main() -> None:
	record = Recorder(sys.argv[1])
	Gdk.Event.handler_set(record.onEvent, None)
	build(record, sys.argv[2] if len(sys.argv) > 2 else "Ground window")
	Gtk.main()


if __name__ == "__main__":
	main()
