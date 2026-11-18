"""A GTK 3 window laid out as mate-screensaver-dialog 1.28 lays out its unlock prompt, and named as it.

A clock renamed every second, then a "Password:" label beside a password field it is not tied to, a
Caps Lock label below them and a message label below that, as AT-SPI shows the real prompt. Prints
"ready" once shown; each line on standard input becomes the message label's text, after which it
prints "done".
"""

import sys
import time

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk

window = Gtk.Window(title="Unlock prompt")
page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
clockBox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
clock = Gtk.Label(label=time.strftime("%H:%M:%S"))
clockBox.pack_start(clock, False, False, 0)
clockBox.pack_start(Gtk.Label(label="Wednesday, November 18"), False, False, 0)
page.pack_start(clockBox, False, False, 0)
auth = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
row.pack_start(Gtk.Label(label="Password:"), False, False, 0)
field = Gtk.Entry()
field.set_visibility(False)
row.pack_start(field, True, True, 0)
auth.pack_start(row, False, False, 0)
auth.pack_start(Gtk.Label(label=""), False, False, 0)
page.pack_start(auth, False, False, 0)
message = Gtk.Label(label="")
page.pack_start(message, False, False, 0)
window.add(page)
window.show_all()
field.grab_focus()


def tick():
	clock.set_text(time.strftime("%H:%M:%S"))
	return True


def onLine(channel, condition):
	line = sys.stdin.readline()
	if not line:
		Gtk.main_quit()
		return False
	message.set_text(line.rstrip("\n"))
	print("done", flush=True)
	return True


def ready():
	print("ready", flush=True)
	return False


GLib.timeout_add(1000, tick)
GLib.io_add_watch(GLib.IOChannel.unix_new(0), GLib.IO_IN | GLib.IO_HUP, onLine)
GLib.timeout_add(300, ready)
Gtk.main()
