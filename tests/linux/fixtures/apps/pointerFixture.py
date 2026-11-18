"""A GTK 3 window that says on its standard output what the mouse did to it.

Run inside the rig only. Its text field has the focus, and prints "text WHAT" whenever its text
changes; the pad below it prints "press N" and "release N" for each button and "scroll DIRECTION"
for each wheel notch; the button below that prints "clicked". It prints "ready" once it is shown.
"""

import sys

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk

DIRECTIONS = {
	Gdk.ScrollDirection.UP: "up",
	Gdk.ScrollDirection.DOWN: "down",
	Gdk.ScrollDirection.LEFT: "left",
	Gdk.ScrollDirection.RIGHT: "right",
}


def say(text):
	print(text, flush=True)


def build():
	win = Gtk.Window(title="Pointer window")
	win.set_default_size(400, 400)
	win.connect("destroy", Gtk.main_quit)
	box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
	box.set_border_width(8)
	win.add(box)

	entry = Gtk.Entry()
	entry.set_text("Alice")
	entry.connect("changed", lambda e: say(f"text {e.get_text()}"))
	box.pack_start(entry, False, False, 0)

	pad = Gtk.EventBox()
	pad.set_size_request(300, 120)
	pad.add_events(
		Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.BUTTON_RELEASE_MASK | Gdk.EventMask.SCROLL_MASK
	)
	pad.add(Gtk.Label(label="Nothing here"))
	pad.connect("button-press-event", lambda _w, e: say(f"press {e.button}") or True)
	pad.connect("button-release-event", lambda _w, e: say(f"release {e.button}") or True)

	def onScroll(_widget, event):
		direction = DIRECTIONS.get(event.direction)
		if direction is None:
			_ok, dx, dy = event.get_scroll_deltas()
			direction = ("down" if dy > 0 else "up") if dy else ("right" if dx > 0 else "left")
		say(f"scroll {direction}")
		return True

	pad.connect("scroll-event", onScroll)
	box.pack_start(pad, False, False, 0)

	button = Gtk.Button(label="Target")
	button.connect("clicked", lambda _b: say("clicked"))
	box.pack_start(button, False, False, 0)

	win.show_all()
	entry.grab_focus()
	return win


def main():
	win = build()

	def announce():
		win.present()
		say("ready")
		return False

	GLib.timeout_add(200, announce)
	Gtk.main()


if __name__ == "__main__":
	sys.exit(main())
