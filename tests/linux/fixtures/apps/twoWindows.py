"""Inside the rig only. One GTK 3 application with two top-level windows and a dialog.

Prints "ready" once shown. Reads commands on stdin: "first" and "second" present that window,
"dialog" opens a modal message dialog over the first window, "close" destroys the dialog.
"""

import sys

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk

windows = {}
dialog = None


def build():
	first = Gtk.Window(title="First window")
	box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
	box.pack_start(Gtk.Button.new_with_mnemonic("_Alpha"), False, False, 0)
	box.pack_start(Gtk.Button.new_with_mnemonic("_Beta"), False, False, 0)
	first.add(box)
	first.set_default_size(300, 200)
	first.move(0, 0)
	second = Gtk.Window(title="Second window")
	second.add(Gtk.CheckButton.new_with_mnemonic("_Gamma"))
	second.set_default_size(300, 200)
	second.move(400, 0)
	for w in (first, second):
		w.connect("destroy", Gtk.main_quit)
		w.show_all()
	windows["first"], windows["second"] = first, second


def onCommand(channel, condition):
	global dialog
	line = channel.readline().strip()
	if not line:
		Gtk.main_quit()
		return False
	if line in windows:
		windows[line].present()
	elif line == "dialog":
		dialog = Gtk.MessageDialog(
			transient_for=windows["first"],
			modal=True,
			message_type=Gtk.MessageType.QUESTION,
			buttons=Gtk.ButtonsType.OK_CANCEL,
			text="Save changes?",
		)
		dialog.format_secondary_text("Your edits will be lost otherwise.")
		dialog.set_title("Unsaved document")
		dialog.show_all()
		dialog.present()
	elif line == "close" and dialog is not None:
		dialog.destroy()
		dialog = None
	print("done " + line, flush=True)
	return True


def main():
	build()

	def announce():
		windows["first"].present()
		print("ready", flush=True)
		return False

	GLib.timeout_add(300, announce)
	channel = GLib.IOChannel.unix_new(sys.stdin.fileno())
	GLib.io_add_watch(channel, GLib.IO_IN | GLib.IO_HUP, onCommand)
	Gtk.main()


sys.exit(main())
