"""A GTK 3 window holding one of each ordinary control, read by the tests.

Run inside the rig only. It takes X input focus itself (there is no window manager in the rig) and
prints "ready" once it is shown.
"""

import os
import sys

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk


def labelled(box, text, widget):
	row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
	label = Gtk.Label.new_with_mnemonic(text)
	label.set_mnemonic_widget(widget)
	row.pack_start(label, False, False, 0)
	row.pack_start(widget, True, True, 0)
	box.pack_start(row, False, False, 0)
	return label


def build():
	win = Gtk.Window(title="Fixture window")
	win.set_default_size(500, 600)
	win.connect("destroy", Gtk.main_quit)
	outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
	win.add(outer)

	accel = Gtk.AccelGroup()
	win.add_accel_group(accel)
	menubar = Gtk.MenuBar()
	fileItem = Gtk.MenuItem.new_with_mnemonic("_File")
	fileMenu = Gtk.Menu()
	fileItem.set_submenu(fileMenu)
	openItem = Gtk.MenuItem.new_with_mnemonic("_Open")
	key, mods = Gtk.accelerator_parse("<Control>o")
	openItem.add_accelerator("activate", accel, key, mods, Gtk.AccelFlags.VISIBLE)
	fileMenu.append(openItem)
	recentItem = Gtk.MenuItem.new_with_mnemonic("_Recent")
	recentMenu = Gtk.Menu()
	recentMenu.append(Gtk.MenuItem(label="one.txt"))
	for n in range(int(os.environ.get("LINDA_RECENTSIZE", "0"))):
		recentMenu.append(Gtk.MenuItem(label=f"file{n}.txt"))
	recentItem.set_submenu(recentMenu)
	fileMenu.append(recentItem)
	wrapItem = Gtk.CheckMenuItem.new_with_mnemonic("_Wrap lines")
	fileMenu.append(wrapItem)
	fileMenu.append(Gtk.SeparatorMenuItem())
	fileMenu.append(Gtk.MenuItem.new_with_mnemonic("_Quit"))
	menubar.append(fileItem)
	helpItem = Gtk.MenuItem.new_with_mnemonic("_Help")
	helpMenu = Gtk.Menu()
	helpMenu.append(Gtk.MenuItem(label="About"))
	helpItem.set_submenu(helpMenu)
	menubar.append(helpItem)
	outer.pack_start(menubar, False, False, 0)

	box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
	box.set_border_width(8)
	outer.pack_start(box, True, True, 0)

	button = Gtk.Button.new_with_mnemonic("_Press me")
	button.set_tooltip_text("Does nothing")
	box.pack_start(button, False, False, 0)

	check = Gtk.CheckButton.new_with_mnemonic("_Enable sound")
	box.pack_start(check, False, False, 0)

	frame = Gtk.Frame(label="Size")
	radios = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
	small = Gtk.RadioButton.new_with_label(None, "Small")
	medium = Gtk.RadioButton.new_with_label_from_widget(small, "Medium")
	large = Gtk.RadioButton.new_with_label_from_widget(small, "Large")
	medium.set_active(True)
	for r in (small, medium, large):
		radios.pack_start(r, False, False, 0)
	frame.add(radios)
	box.pack_start(frame, False, False, 0)

	combo = Gtk.ComboBoxText()
	for c in ("Red", "Green", "Blue"):
		combo.append_text(c)
	combo.set_active(1)
	labelled(box, "_Colour:", combo)

	entry = Gtk.Entry()
	entry.set_text("Alice")
	labelled(box, "_Name:", entry)

	scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 1)
	scale.set_value(40)
	labelled(box, "_Volume:", scale)

	spin = Gtk.SpinButton.new_with_range(0, 10, 1)
	spin.set_value(3)
	labelled(box, "C_ount:", spin)

	store = Gtk.ListStore(str)
	for fruit in ["Apples", "Pears", "Plums"] + [
		f"Item {n}" for n in range(int(os.environ.get("LINDA_LISTSIZE", "0")))
	]:
		store.append([fruit])
	tree = Gtk.TreeView(model=store)
	tree.append_column(Gtk.TreeViewColumn("Fruit", Gtk.CellRendererText(), text=0))
	tree.set_headers_visible(False)
	tree.get_selection().select_path(Gtk.TreePath.new_first())
	labelled(box, "_Fruit:", tree)

	toggle = Gtk.ToggleButton.new_with_mnemonic("_Bold")
	box.pack_start(toggle, False, False, 0)

	link = Gtk.LinkButton.new_with_label("https://example.invalid/", "Example link")
	box.pack_start(link, False, False, 0)

	search = Gtk.Entry()
	search.set_placeholder_text("Search…")
	box.pack_start(search, False, False, 0)

	notebook = Gtk.Notebook()
	notebook.append_page(Gtk.Label(label="First page"), Gtk.Label(label="General"))
	notebook.append_page(Gtk.Label(label="Second page"), Gtk.Label(label="Advanced"))
	box.pack_start(notebook, False, False, 0)

	status = Gtk.Statusbar()
	status.push(status.get_context_id("main"), "Ready")
	outer.pack_end(status, False, False, 0)

	win.show_all()
	button.grab_focus()
	return win


#: The document the reader window shows, and the lines of it that are headings, with their levels.
READER_TEXT = (
	"Chapter one\nThe first paragraph of chapter one.\nSection A\nText in section A.\n"
	"Chapter two\nThe second chapter begins here."
)
READER_HEADINGS = {0: 1, 2: 2, 4: 1}


def presentMessage(text):
	"""Ask the screen reader to speak text through Orca's remote control, as Paperback does."""
	import subprocess

	subprocess.Popen(
		[
			"gdbus",
			"call",
			"--session",
			"--dest",
			"org.gnome.Orca.Service",
			"--object-path",
			"/org/gnome/Orca/Service",
			"--method",
			"org.gnome.Orca.Service.PresentMessage",
			text,
		],
		stdout=subprocess.DEVNULL,
	)


def buildReader():
	"""A document reader's window: the document is text the keyboard reads but cannot change, and h moves
	to the next heading and announces it. Below it, a list of files with a column of check boxes first,
	whose cursor is in the name column."""
	win = Gtk.Window(title="Reader window")
	win.set_default_size(500, 400)
	win.connect("destroy", Gtk.main_quit)
	box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
	scroller = Gtk.ScrolledWindow()
	scroller.set_size_request(-1, 200)
	view = Gtk.TextView()
	view.set_editable(False)
	view.set_cursor_visible(True)
	buffer = view.get_buffer()
	buffer.set_text(READER_TEXT)
	buffer.place_cursor(buffer.get_start_iter())
	scroller.add(view)
	box.pack_start(scroller, True, True, 0)
	store = Gtk.ListStore(bool, str, str)
	for row in ((True, "a.txt", "6 bytes"), (False, "b.png", "2 KB")):
		store.append(list(row))
	files = Gtk.TreeView(model=store)
	files.append_column(Gtk.TreeViewColumn("Use", Gtk.CellRendererToggle(), active=0))
	nameColumn = Gtk.TreeViewColumn("Name", Gtk.CellRendererText(), text=1)
	files.append_column(nameColumn)
	files.append_column(Gtk.TreeViewColumn("Size", Gtk.CellRendererText(), text=2))
	files.set_cursor(Gtk.TreePath.new_first(), nameColumn, False)
	box.pack_start(files, False, False, 0)
	win.add(box)

	def onKey(_view, event):
		if event.keyval not in (Gdk.KEY_h, Gdk.KEY_H):
			return False
		line = buffer.get_iter_at_mark(buffer.get_insert()).get_line()
		following = [n for n in sorted(READER_HEADINGS) if n > line]
		if not following:
			presentMessage("No next heading.")
			return True
		target = following[0]
		buffer.place_cursor(buffer.get_iter_at_line(target))
		lineText = READER_TEXT.split("\n")[target]
		presentMessage(f"{lineText} Heading level {READER_HEADINGS[target]}")
		return True

	view.connect("key-press-event", onKey)
	win.show_all()
	view.grab_focus()
	return win


def main():
	win = buildReader() if sys.argv[1:] == ["reader"] else build()

	def announce():
		# Without a window manager GDK gives itself the X input focus here.
		win.present()
		print("ready", flush=True)
		return False

	GLib.timeout_add(200, announce)
	Gtk.main()


if __name__ == "__main__":
	sys.exit(main())
