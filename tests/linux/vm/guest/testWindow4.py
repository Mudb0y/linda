"""The GTK 4 twin of testWindow.py: a window for Linda to read in the guest, which records every key
that reaches it.

	testWindow4.py OUT [TITLE]

The window has a text field named Name, which has the focus once the window is active. Every key
event that reaches the window is written to OUT by a key controller in the capture phase, before any
widget of it sees the key, in the form testWindow.py uses: "t" when GTK handed it over, "serverTime"
the event's own time, "window" the window's title, "code" and "key" the evdev code and its name, and
"keyval", GDK's name for the key in the layout the compositor applies. A first record of kind
"ready" says the window is active and its field focused.

GTK 4 hands a capture controller a key repeat as another press, as GDK 3 does.
"""

import json
import sys
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from evdev import ecodes
from gi.repository import Gdk, GLib, Gtk

#: GDK's key codes are evdev codes plus this, on Wayland as on X.
KEYCODE_OFFSET = 8


def keyName(code: int) -> str:
	name = ecodes.KEY.get(code, str(code))
	return name[0] if isinstance(name, list) else name


class Recorder:
	def __init__(self, path: str) -> None:
		self._file = open(path, "a", buffering=1)  # noqa: SIM115

	def __call__(self, kind: str, **fields: object) -> None:
		self._file.write(json.dumps({"kind": kind, "received": time.monotonic(), **fields}) + "\n")


def build(record: Recorder, title: str, loop: GLib.MainLoop) -> Gtk.Window:
	window = Gtk.Window(title=title)
	window.set_default_size(400, 200)
	window.connect("close-request", lambda _window: loop.quit() or False)

	def onKey(
		controller: Gtk.EventControllerKey, keyval: int, keycode: int, _state: object, value: int
	) -> bool:
		event = controller.get_current_event()
		code = keycode - KEYCODE_OFFSET
		record(
			"key",
			t=time.monotonic(),
			serverTime=event.get_time() / 1000 if event is not None else None,
			window=title,
			code=code,
			key=keyName(code),
			keyval=Gdk.keyval_name(keyval),
			value=value,
		)
		return False

	controller = Gtk.EventControllerKey()
	controller.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
	controller.connect("key-pressed", onKey, 1)
	controller.connect("key-released", onKey, 0)
	window.add_controller(controller)

	row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
	entry = Gtk.Entry(hexpand=True)
	label = Gtk.Label.new_with_mnemonic("_Name")
	label.set_mnemonic_widget(entry)
	row.append(label)
	row.append(entry)
	window.set_child(row)

	def ready() -> bool:
		record("ready")
		return False

	def onActive(*_args: object) -> None:
		if not window.is_active():
			return
		window.disconnect(handler)
		entry.grab_focus()
		GLib.idle_add(ready)

	handler = window.connect("notify::is-active", onActive)
	window.present()
	return window


def main() -> None:
	# GTK 4 names a program with no GApplication "Unnamed" on the accessibility bus; GTK 3 takes the
	# script's own name, which this gives the twin too.
	GLib.set_prgname("testWindow4.py")
	Gtk.init()
	record = Recorder(sys.argv[1])
	loop = GLib.MainLoop()
	build(record, sys.argv[2] if len(sys.argv) > 2 else "Ground window 4", loop)
	loop.run()


if __name__ == "__main__":
	main()
