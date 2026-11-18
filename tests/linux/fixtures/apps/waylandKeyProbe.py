"""A GTK 4 window, "Key probe", with an entry 40 pixels in and 60 down, that reports on standard output
its windowing backend, when it becomes active, every key it is given and its text."""

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, Gtk

app = Gtk.Application(application_id="org.linda.WaylandKeyProbe")


def activate(application):
	window = Gtk.ApplicationWindow(application=application, title="Key probe")
	window.set_default_size(400, 300)
	entry = Gtk.Entry()
	entry.set_margin_start(40)
	entry.set_margin_top(60)
	entry.set_halign(Gtk.Align.START)
	entry.set_valign(Gtk.Align.START)
	window.set_child(entry)
	keys = Gtk.EventControllerKey()
	keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
	keys.connect(
		"key-pressed", lambda _c, keyval, _code, _state: print("pressed", Gdk.keyval_name(keyval), flush=True)
	)
	keys.connect(
		"key-released",
		lambda _c, keyval, _code, _state: print("released", Gdk.keyval_name(keyval), flush=True),
	)
	window.add_controller(keys)
	entry.connect("changed", lambda e: print("text", e.get_text(), flush=True))
	window.connect("notify::is-active", lambda w, _p: w.is_active() and print("active", flush=True))
	print("backend", type(window.get_display()).__name__, flush=True)
	window.present()


app.connect("activate", activate)
app.run([])
