"""GTK 4 fixture: the same controls with GTK 4's current widgets.

Where GTK 4 deprecated a widget without a replacement (the combo box with an
entry, the status bar) the deprecated one is used, as applications still do.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import gi

gi.require_version("Gtk", "4.0")
from commands import TEXT, Commands, say
from gi.repository import Gio, GLib, GObject, Gtk

W = {}
loop = GLib.MainLoop()


def labelled(grid, row, labelText, widget, name):
	label = Gtk.Label.new_with_mnemonic("_" + labelText)
	label.set_mnemonic_widget(widget)
	label.set_xalign(0)
	grid.attach(label, 0, row, 1, 1)
	grid.attach(widget, 1, row, 1, 1)
	W[name] = widget


def textFactory(column):
	f = Gtk.SignalListItemFactory()

	def setup(_f, item):
		item.set_child(Gtk.Label(xalign=0))

	def bind(_f, item):
		item.get_child().set_label(item.get_item().get_property("string").split("\t")[column])

	f.connect("setup", setup)
	f.connect("bind", bind)
	return f


class Node(GObject.Object):
	def __init__(self, name, size, children):
		super().__init__()
		self.name = name
		self.size = size
		self.children = children


def treeColumns():
	roots = Gio.ListStore(item_type=Node)
	for name, size, kids in TEXT["tree"]:
		roots.append(Node(name, size, kids))

	def childModel(node):
		if not node.children:
			return None
		store = Gio.ListStore(item_type=Node)
		for cname, csize in node.children:
			store.append(Node(cname, csize, ()))
		return store

	model = Gtk.TreeListModel.new(roots, False, False, childModel)
	view = Gtk.ColumnView(model=Gtk.SingleSelection(model=model))
	for i, title in enumerate(TEXT["treeColumns"]):
		f = Gtk.SignalListItemFactory()

		def setup(_f, item, first=(i == 0)):
			label = Gtk.Label(xalign=0)
			if first:
				exp = Gtk.TreeExpander()
				exp.set_child(label)
				item.set_child(exp)
			else:
				item.set_child(label)

		def bind(_f, item, first=(i == 0)):
			row = item.get_item()
			node = row.get_item()
			if first:
				item.get_child().set_list_row(row)
				item.get_child().get_child().set_label(node.name)
			else:
				item.get_child().set_label(node.size)

		f.connect("setup", setup)
		f.connect("bind", bind)
		view.append_column(Gtk.ColumnViewColumn(title=title, factory=f))
	return view


def build():
	win = Gtk.Window(title="GTK4 fixture")
	win.set_default_size(1000, 950)
	win.connect("close-request", lambda _w: loop.quit() or False)
	outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
	win.set_child(outer)

	actions = Gio.SimpleActionGroup()
	for n in ("new", "open", "one", "two", "quit", "cut", "copy", "paste"):
		actions.add_action(Gio.SimpleAction.new(n, None))
	actions.add_action(Gio.SimpleAction.new_stateful("toolbar", None, GLib.Variant("b", True)))
	actions.lookup_action("toolbar").connect(
		"activate", lambda a, _p: a.set_state(GLib.Variant("b", not a.get_state().unpack()))
	)
	size = Gio.SimpleAction.new_stateful("size", GLib.VariantType("s"), GLib.Variant("s", "small"))
	size.connect("activate", lambda a, p: a.set_state(p))
	actions.add_action(size)
	win.insert_action_group("fx", actions)

	menu = Gio.Menu()
	fileMenu = Gio.Menu()
	fileMenu.append("New", "fx.new")
	fileMenu.append("Open", "fx.open")
	recent = Gio.Menu()
	recent.append("one.txt", "fx.one")
	recent.append("two.txt", "fx.two")
	fileMenu.append_submenu("Recent", recent)
	quitSection = Gio.Menu()
	quitSection.append("Quit", "fx.quit")
	fileMenu.append_section(None, quitSection)
	menu.append_submenu("_File", fileMenu)
	viewMenu = Gio.Menu()
	viewMenu.append("Show toolbar", "fx.toolbar")
	viewMenu.append("Small", "fx.size::small")
	viewMenu.append("Large", "fx.size::large")
	menu.append_submenu("_View", viewMenu)
	menubar = Gtk.PopoverMenuBar.new_from_model(menu)
	outer.append(menubar)

	toolbar = Gtk.Box(spacing=4)
	toolbar.add_css_class("toolbar")
	bold = Gtk.ToggleButton(label=TEXT["toolbarBold"])
	italic = Gtk.Button(label=TEXT["toolbarItalic"])
	toolbar.append(bold)
	toolbar.append(italic)
	W["toolbarBold"] = bold
	W["toolbarItalic"] = italic
	outer.append(toolbar)

	grid = Gtk.Grid(column_spacing=8, row_spacing=4)
	grid.set_margin_start(8)
	outer.append(grid)
	row = 0

	status = Gtk.Statusbar()
	status.push(0, TEXT["status"])
	W["status"] = status

	push = Gtk.Button(label=TEXT["push"])
	push.set_tooltip_text(TEXT["pushTooltip"])
	pushes = [0]

	def onPush(_b):
		pushes[0] += 1
		status.push(0, f"Pushed {pushes[0]} times")

	push.connect("clicked", onPush)
	toggle = Gtk.ToggleButton(label=TEXT["toggle"])
	check = Gtk.CheckButton(label=TEXT["check"])
	r1 = Gtk.CheckButton(label=TEXT["radio1"])
	r2 = Gtk.CheckButton(label=TEXT["radio2"], group=r1)
	r3 = Gtk.CheckButton(label=TEXT["radio3"], group=r1)
	r1.set_active(True)
	buttons = Gtk.Box(spacing=6)
	for name, w in (
		("push", push),
		("toggle", toggle),
		("check", check),
		("radio1", r1),
		("radio2", r2),
		("radio3", r3),
	):
		buttons.append(w)
		W[name] = w
	grid.attach(buttons, 0, row, 2, 1)
	row += 1

	entry = Gtk.Entry()
	entry.set_text(TEXT["entry"])
	labelled(grid, row, TEXT["entryLabel"], entry, "entry")
	row += 1
	password = Gtk.PasswordEntry()
	password.set_text(TEXT["password"])
	labelled(grid, row, TEXT["passwordLabel"], password, "password")
	row += 1

	view = Gtk.TextView()
	view.get_buffer().set_text(TEXT["multiline"])
	sw = Gtk.ScrolledWindow()
	sw.set_size_request(300, 70)
	sw.set_child(view)
	labelled(grid, row, TEXT["multilineLabel"], sw, "multilineScroll")
	W["multiline"] = view
	row += 1

	combo = Gtk.DropDown.new_from_strings(list(TEXT["comboItems"]))
	labelled(grid, row, TEXT["comboLabel"], combo, "combo")
	row += 1
	comboEntry = Gtk.ComboBoxText.new_with_entry()
	for t in TEXT["comboEntryItems"]:
		comboEntry.append_text(t)
	comboEntry.set_active(0)
	labelled(grid, row, TEXT["comboEntryLabel"], comboEntry, "comboEntryBox")
	W["comboEntry"] = comboEntry.get_child()
	row += 1

	spin = Gtk.SpinButton.new_with_range(0, 100, 1)
	spin.set_value(5)
	labelled(grid, row, TEXT["spinLabel"], spin, "spin")
	row += 1
	slider = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 1)
	slider.set_value(50)
	slider.set_size_request(200, -1)
	labelled(grid, row, TEXT["sliderLabel"], slider, "slider")
	row += 1
	progress = Gtk.ProgressBar()
	progress.set_fraction(0.3)
	progress.set_show_text(True)
	labelled(grid, row, TEXT["progressLabel"], progress, "progress")
	row += 1

	listModel = Gtk.StringList.new(list(TEXT["listItems"]))
	listView = Gtk.ListView(model=Gtk.SingleSelection(model=listModel), factory=textFactory(0))
	contextModel = Gio.Menu()
	for t in TEXT["contextItems"]:
		contextModel.append(t, "fx." + t.lower())
	contextMenu = Gtk.PopoverMenu.new_from_model(contextModel)
	contextMenu.set_parent(listView)
	keys = Gtk.ShortcutController()
	keys.add_shortcut(
		Gtk.Shortcut.new(
			Gtk.ShortcutTrigger.parse_string("<Shift>F10|Menu"),
			Gtk.CallbackAction.new(lambda *_a: contextMenu.popup() or True),
		)
	)
	listView.add_controller(keys)
	labelled(grid, row, TEXT["listLabel"], listView, "list")
	row += 1

	tree = treeColumns()
	labelled(grid, row, TEXT["treeLabel"], tree, "tree")
	row += 1

	tableModel = Gtk.StringList.new(["\t".join(r) for r in TEXT["tableRows"]])
	table = Gtk.ColumnView(model=Gtk.SingleSelection(model=tableModel))
	for i, title in enumerate(TEXT["tableColumns"]):
		table.append_column(Gtk.ColumnViewColumn(title=title, factory=textFactory(i)))
	labelled(grid, row, TEXT["tableLabel"], table, "table")
	row += 1

	notebook = Gtk.Notebook()
	for title, contents in zip(TEXT["tabs"], TEXT["tabContents"]):
		notebook.append_page(Gtk.Label(label=contents), Gtk.Label(label=title))
	grid.attach(notebook, 0, row, 2, 1)
	W["tabs"] = notebook
	row += 1

	dialogButton = Gtk.Button(label=TEXT["dialogButton"])
	dialogButton.connect("clicked", lambda _b: openDialog(win))
	messageButton = Gtk.Button(label=TEXT["messageButton"])
	messageButton.connect("clicked", lambda _b: openMessage(win))
	link = Gtk.LinkButton.new_with_label(TEXT["linkUri"], TEXT["link"])
	link.connect("activate-link", lambda _b: True)
	disabled = Gtk.Button(label=TEXT["disabled"])
	disabled.set_sensitive(False)
	blinker = Gtk.Button(label=TEXT["blinker"])
	more = Gtk.Box(spacing=6)
	for name, w in (
		("dialogButton", dialogButton),
		("messageButton", messageButton),
		("link", link),
		("disabled", disabled),
		("blinker", blinker),
	):
		more.append(w)
		W[name] = w
	grid.attach(more, 0, row, 2, 1)
	row += 1

	expander = Gtk.Expander(label=TEXT["expander"])
	expander.set_child(Gtk.CheckButton(label=TEXT["expanderChild"]))
	grid.attach(expander, 0, row, 2, 1)
	W["expander"] = expander
	row += 1

	outer.append(status)
	W["window"] = win
	return win


def openDialog(parent):
	d = Gtk.Window(title=TEXT["dialogTitle"], transient_for=parent, modal=True)
	box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
	entry = Gtk.Entry(activates_default=True)
	label = Gtk.Label.new_with_mnemonic("_" + TEXT["dialogEntryLabel"])
	label.set_mnemonic_widget(entry)
	buttons = Gtk.Box(spacing=6)
	cancel = Gtk.Button.new_with_mnemonic("_Cancel")
	ok = Gtk.Button.new_with_mnemonic("_OK")
	cancel.connect("clicked", lambda _b: d.destroy())
	ok.connect("clicked", lambda _b: d.destroy())
	buttons.append(cancel)
	buttons.append(ok)
	box.append(label)
	box.append(entry)
	box.append(buttons)
	d.set_child(box)
	d.set_default_widget(ok)
	d.present()


def openMessage(parent):
	m = Gtk.AlertDialog(
		message=TEXT["messageText"],
		detail=TEXT["messageDetail"],
		buttons=["OK"],
		default_button=0,
		cancel_button=0,
		modal=True,
	)
	m.choose(parent, None, lambda *_a: None)


timer = [None]


def blink():
	b = W["blinker"]
	b.set_visible(not b.get_visible())
	return True


def run(line):
	cmd, _, arg = line.partition(" ")
	if cmd == "focus":
		if not W[arg].grab_focus():
			raise RuntimeError("grab_focus returned False")
	elif cmd == "timer":
		if arg == "on" and timer[0] is None:
			timer[0] = GLib.timeout_add(1000, blink)
		elif arg == "off" and timer[0] is not None:
			GLib.source_remove(timer[0])
			timer[0] = None
			W["blinker"].set_visible(True)
	elif cmd == "quit":
		loop.quit()
	else:
		raise ValueError("unknown command")


def main():
	win = build()
	win.present()
	commands = Commands(run)
	GLib.timeout_add(50, commands.poll)
	GLib.idle_add(lambda: say("ready") and False)
	loop.run()


if __name__ == "__main__":
	main()
