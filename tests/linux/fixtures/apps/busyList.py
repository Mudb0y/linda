"""Inside the rig only. A GTK 3 window holding a long list that can move its own cursor many times a
second while it is too busy to answer quickly, as Caja's views do with a large folder.

Prints "ready" once shown. Reads commands on stdin: "flood SECONDS" moves the list's cursor down a
row at a time for that long, MOVES_PER_STEP rows at once and then busy for BUSY_SECONDS, and prints
"flooded NAME" with the name of the row it stopped on.
"""

import sys
import time

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk

ROWS = 3000
#: Moves made at once, and how long the list keeps its main loop to itself after them: about sixty
#: moves a second, each call answered some 50 to 150 ms late, as Caja did with a large folder.
MOVES_PER_STEP = 4
BUSY_SECONDS = 0.04

tree: Gtk.TreeView


def build() -> Gtk.Window:
	global tree
	win = Gtk.Window(title="Busy list")
	win.set_default_size(300, 400)
	win.connect("destroy", Gtk.main_quit)
	store = Gtk.ListStore(str)
	for number in range(ROWS):
		store.append([f"Row {number}"])
	tree = Gtk.TreeView(model=store)
	tree.append_column(Gtk.TreeViewColumn("Name", Gtk.CellRendererText(), text=0))
	tree.set_headers_visible(False)
	scroller = Gtk.ScrolledWindow()
	scroller.add(tree)
	win.add(scroller)
	win.show_all()
	tree.set_cursor(Gtk.TreePath.new_first(), None, False)
	tree.grab_focus()
	return win


def flood(seconds: float) -> None:
	end = time.monotonic() + seconds

	def step() -> bool:
		for _ in range(MOVES_PER_STEP):
			path, _column = tree.get_cursor()
			row = (path.get_indices()[0] + 1 if path is not None else 0) % ROWS
			tree.set_cursor(Gtk.TreePath.new_from_indices([row]), None, False)
		time.sleep(BUSY_SECONDS)
		if time.monotonic() < end:
			return True
		print(f"flooded Row {row}", flush=True)
		return False

	GLib.timeout_add(1, step)


def onCommand(channel, _condition) -> bool:
	line = channel.readline().strip()
	if not line:
		Gtk.main_quit()
		return False
	command, _, argument = line.partition(" ")
	if command == "flood":
		flood(float(argument))
	return True


def main() -> None:
	win = build()

	def announce() -> bool:
		win.present()
		print("ready", flush=True)
		return False

	GLib.timeout_add(200, announce)
	channel = GLib.IOChannel.unix_new(sys.stdin.fileno())
	GLib.io_add_watch(channel, GLib.IO_IN | GLib.IO_HUP, onCommand)
	Gtk.main()


sys.exit(main())
