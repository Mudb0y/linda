"""A GTK 3 window with a File menu whose focus reaches the bus through ATK's focus tracker, which GTK 3
called before it reported focus as a change of the focused state. The bridge sends what the tracker is
given as the legacy focus: event, and the object it was given has the focused state.

"only": the menu is what a program that bundles an older GTK 3 and bridge has, FastSM's portable build
among them: its items say they have the focus with focus: alone, never with object:state-changed:focused.
Today's GTK 3 reports a real menu item's focus as a state change of its own, so these items are labels
with the roles of a menu, highlighted by F10, Down and Up, and Escape gives the focus back to the button
with focus: too.
"both": a real menu, whose items GTK reports as it does today and also gives to the tracker, as a
program that sends both events for one object does.

Run inside the rig only. It prints "ready" once it is shown.
"""

import sys

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("Atk", "1.0")
from gi.repository import Atk, Gdk, GLib, Gtk

ITEMS = ("Open", "Save", "Quit")


def notify(widget: Gtk.Widget) -> bool:
	Atk.focus_tracker_notify(widget.get_accessible())
	return False


def buildOnly(box: Gtk.Box, button: Gtk.Button) -> None:
	menuBar = Gtk.Box()
	menuBar.get_accessible().set_role(Atk.Role.MENU_BAR)
	menu = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
	menu.get_accessible().set_role(Atk.Role.MENU)
	menu.get_accessible().set_name("File")
	items = []
	for name in ITEMS:
		item = Gtk.Label(label=name)
		item.get_accessible().set_role(Atk.Role.MENU_ITEM)
		menu.pack_start(item, False, False, 0)
		items.append(item)
	menuBar.pack_start(menu, False, False, 0)
	box.pack_start(menuBar, False, False, 0)
	highlighted = [None]

	def onKey(_window, event) -> bool:
		if event.keyval == Gdk.KEY_F10:
			highlighted[0] = 0
		elif event.keyval in (Gdk.KEY_Down, Gdk.KEY_Up) and highlighted[0] is not None:
			step = 1 if event.keyval == Gdk.KEY_Down else -1
			highlighted[0] = (highlighted[0] + step) % len(items)
		elif event.keyval == Gdk.KEY_Escape and highlighted[0] is not None:
			highlighted[0] = None
			notify(button)
			return True
		else:
			return False
		notify(items[highlighted[0]])
		return True

	box.get_toplevel().connect("key-press-event", onKey)


def buildBoth(box: Gtk.Box, button: Gtk.Button) -> None:
	menuBar = Gtk.MenuBar()
	fileItem = Gtk.MenuItem.new_with_mnemonic("_File")
	menu = Gtk.Menu()
	fileItem.set_submenu(menu)
	menuBar.append(fileItem)
	for name in ITEMS:
		menu.append(Gtk.MenuItem(label=name))
	for item in (fileItem, *menu.get_children()):
		# As GTK's own report of the focus, once the highlight has moved.
		item.connect("select", lambda item: GLib.idle_add(notify, item))
	menuBar.connect("deactivate", lambda _menuBar: GLib.idle_add(notify, button))
	box.pack_start(menuBar, False, False, 0)


def main() -> None:
	mode = sys.argv[1]
	win = Gtk.Window(title=f"Legacy focus {mode}")
	win.set_default_size(300, 200)
	win.connect("destroy", Gtk.main_quit)
	box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
	win.add(box)
	button = Gtk.Button(label="Press me")
	{"only": buildOnly, "both": buildBoth}[mode](box, button)
	box.pack_start(button, False, False, 0)
	win.show_all()
	button.grab_focus()

	def announce() -> bool:
		win.present()
		print("ready", flush=True)
		return False

	GLib.timeout_add(200, announce)
	Gtk.main()


if __name__ == "__main__":
	main()
