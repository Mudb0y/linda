"""Fixture window with the text widgets the text tests read, in one of four toolkits.

textapp.py gtk3|gtk4|qt5|qt6

Every widget has an accessible name, so a test finds it by name:
entry (single line), multi (multi line, wrapped), password, label (selectable read-only label),
rich (formatted text). The texts are in TEXTS; a test imports this module for them.
"""

import sys

MULTI = (
	"Zażółć gęślą jaźń\n"
	"\n"
	"é café \U0001f600 emoji\tTab\n"
	"To jest bardzo długa linia tekstu, która powinna zawinąć się w wąskim polu kilka razy.\n"
	"\n"
	"ostatnia bez końca"
)
TEXTS = {
	"entry": "Zażółć \U0001f600 éx\tkoniec",
	"multi": MULTI,
	"password": "tajne\U0001f600",
	"label": "Etykieta zaznaczalna ąę \U0001f600",
	# Plain text of the rich widget; RICH_RUNS gives the formatting by substring.
	"rich": "zwykły pogrubiony kursywa podkreślony duży czerwony koniec",
}
RICH_RUNS = (
	("pogrubiony", "bold"),
	("kursywa", "italic"),
	("podkreślony", "underline"),
	("duży", "big"),
	("czerwony", "red"),
)


def gtk3():
	import gi

	gi.require_version("Gtk", "3.0")
	from gi.repository import Gtk, Pango

	win = Gtk.Window(title="TextFixture")
	win.set_default_size(260, 400)
	box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
	win.add(box)

	def named(w, name):
		w.get_accessible().set_name(name)
		return w

	entry = named(Gtk.Entry(), "entry")
	entry.set_text(TEXTS["entry"])
	box.pack_start(entry, False, False, 0)
	tv = named(Gtk.TextView(), "multi")
	tv.set_wrap_mode(Gtk.WrapMode.WORD)
	tv.set_size_request(200, 160)
	tv.get_buffer().set_text(MULTI)
	box.pack_start(tv, False, False, 0)
	pw = named(Gtk.Entry(), "password")
	pw.set_visibility(False)
	# GTK 3 masks with U+25CF or U+2022, by the font; Qt's entry has the first, so this one the second.
	pw.set_invisible_char("\u2022")
	pw.set_text(TEXTS["password"])
	box.pack_start(pw, False, False, 0)
	lab = named(Gtk.Label(label=TEXTS["label"]), "label")
	lab.set_selectable(True)
	box.pack_start(lab, False, False, 0)
	rich = named(Gtk.TextView(), "rich")
	rich.set_wrap_mode(Gtk.WrapMode.WORD)
	buf = rich.get_buffer()
	buf.set_text(TEXTS["rich"])
	tags = {
		"bold": buf.create_tag("bold", weight=Pango.Weight.BOLD),
		"italic": buf.create_tag("italic", style=Pango.Style.ITALIC),
		"underline": buf.create_tag("underline", underline=Pango.Underline.SINGLE),
		"big": buf.create_tag("big", size_points=20),
		"red": buf.create_tag("red", foreground="#ff0000"),
	}
	for word, tag in RICH_RUNS:
		i = TEXTS["rich"].index(word)
		buf.apply_tag(tags[tag], buf.get_iter_at_offset(i), buf.get_iter_at_offset(i + len(word)))
	box.pack_start(rich, True, True, 0)
	box.pack_start(named(Gtk.Button(label="Przycisk OK"), "button"), False, False, 0)
	win.connect("destroy", Gtk.main_quit)
	win.show_all()
	win.present()
	entry.grab_focus()
	Gtk.main()


def gtk4():
	import gi

	gi.require_version("Gtk", "4.0")
	from gi.repository import Gtk, Pango

	app = Gtk.Application(application_id="org.example.TextFixture4")

	def named(w, name):
		w.update_property([Gtk.AccessibleProperty.LABEL], [name])
		return w

	def activate(app):
		win = Gtk.ApplicationWindow(application=app, title="TextFixture")
		win.set_default_size(260, 400)
		box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
		win.set_child(box)
		entry = named(Gtk.Entry(), "entry")
		entry.set_text(TEXTS["entry"])
		box.append(entry)
		tv = named(Gtk.TextView(), "multi")
		tv.set_wrap_mode(Gtk.WrapMode.WORD)
		tv.set_size_request(200, 160)
		tv.get_buffer().set_text(MULTI)
		box.append(tv)
		pw = named(Gtk.PasswordEntry(), "password")
		pw.set_text(TEXTS["password"])
		box.append(pw)
		lab = named(Gtk.Label(label=TEXTS["label"]), "label")
		lab.set_selectable(True)
		box.append(lab)
		rich = named(Gtk.TextView(), "rich")
		rich.set_wrap_mode(Gtk.WrapMode.WORD)
		buf = rich.get_buffer()
		buf.set_text(TEXTS["rich"])
		tags = {
			"bold": buf.create_tag("bold", weight=Pango.Weight.BOLD),
			"italic": buf.create_tag("italic", style=Pango.Style.ITALIC),
			"underline": buf.create_tag("underline", underline=Pango.Underline.SINGLE),
			"big": buf.create_tag("big", size_points=20),
			"red": buf.create_tag("red", foreground="#ff0000"),
		}
		for word, tag in RICH_RUNS:
			i = TEXTS["rich"].index(word)
			buf.apply_tag(tags[tag], buf.get_iter_at_offset(i), buf.get_iter_at_offset(i + len(word)))
		rich.set_vexpand(True)
		box.append(rich)
		box.append(named(Gtk.Button(label="Przycisk OK"), "button"))
		win.present()
		entry.grab_focus()

	app.connect("activate", activate)
	app.run([])


def qt(version):
	if version == 5:
		from PyQt5 import QtWidgets
		from PyQt5.QtCore import Qt
	else:
		from PyQt6 import QtWidgets
		from PyQt6.QtCore import Qt
	app = QtWidgets.QApplication(["textapp"])
	win = QtWidgets.QWidget()
	win.setWindowTitle("TextFixture")
	win.resize(260, 400)
	lay = QtWidgets.QVBoxLayout(win)

	def named(w, name):
		w.setAccessibleName(name)
		lay.addWidget(w)
		return w

	entry = named(QtWidgets.QLineEdit(TEXTS["entry"]), "entry")
	multi = named(QtWidgets.QPlainTextEdit(), "multi")
	multi.setPlainText(MULTI)
	multi.setFixedSize(200, 160)
	pw = named(QtWidgets.QLineEdit(TEXTS["password"]), "password")
	echo = QtWidgets.QLineEdit.EchoMode.Password if version == 6 else QtWidgets.QLineEdit.Password
	pw.setEchoMode(echo)
	lab = named(QtWidgets.QLabel(TEXTS["label"]), "label")
	flags = (
		Qt.TextInteractionFlag.TextSelectableByMouse | Qt.TextInteractionFlag.TextSelectableByKeyboard
		if version == 6
		else Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard
	)
	lab.setTextInteractionFlags(flags)
	rich = named(QtWidgets.QTextEdit(), "rich")
	html = TEXTS["rich"]
	wrap = {
		"bold": "<b>{}</b>",
		"italic": "<i>{}</i>",
		"underline": "<u>{}</u>",
		"big": '<span style="font-size:20pt">{}</span>',
		"red": '<span style="color:#ff0000">{}</span>',
	}
	for word, tag in RICH_RUNS:
		html = html.replace(word, wrap[tag].format(word), 1)
	rich.setHtml(html)
	named(QtWidgets.QPushButton("Przycisk OK"), "button")
	win.show()
	win.activateWindow()
	entry.setFocus()
	sys.exit(app.exec() if version == 6 else app.exec_())


if __name__ == "__main__":
	kind = sys.argv[1]
	if kind == "gtk3":
		gtk3()
	elif kind == "gtk4":
		gtk4()
	elif kind in ("qt5", "qt6"):
		qt(int(kind[2]))
	else:
		sys.exit(f"unknown toolkit {kind}")
