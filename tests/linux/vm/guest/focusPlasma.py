"""The Plasma guest's half of groundFocus.py, and the native Qt 6 window this guest alone has.

As groundFocus.py's module for kind plasma, it reads the focus from AT-SPI's active state, as Linda
reads it, and moves it with kdotool, which loads a script into KWin over D-Bus; when kdotool fails it
loads a script of its own the same way, through org.kde.KWin's /Scripting. Whether the desktop is
ready, and what it says of itself, it asks of KWin and plasmashell on the session bus.

As a program, run by ground-window-qt (guestPlasma.nix), under a Python that has PyQt6 and not GTK:

	focusPlasma.py window OUT [TITLE]

it shows a Qt 6 window on the compositor, never on Xwayland, with a text field named Name, which has
the focus once the window is active, and records every key that reaches the window to OUT in the
form testWindow.py uses: "t", "window" the title, "code" and "key" the evdev code and its name,
"keyval" the keysym's name in the layout the compositor applies, as xkbcommon names it, and "value",
1 a press, 0 a release and 2 a repeat. A record of kind "shown" says the window is mapped, and one of
kind "ready" that it is active and its field focused.
"""

import json
import os
import subprocess
import sys
import tempfile
import time

#: The desktop's own programs on the AT-SPI desktop, whose surfaces are no program's window.
SHELL_APPLICATIONS = ("plasmashell", "kwin", "kwin_wayland")
#: What must own a name on the session bus once Plasma has started, and what owns one only while its
#: splash screen is up.
STARTED_NAMES = ("org.kde.KWin", "org.kde.plasmashell", "org.kde.keyboard")
SPLASH_NAME = "org.kde.KSplash"
KWIN = "org.kde.KWin"
KEYBOARD = "org.kde.keyboard"
#: Milliseconds an AT-SPI call may take, and one made while a program starts, before it is given up.
ATSPI_TIMEOUT = 2000
ATSPI_STARTUP_TIMEOUT = 5000
DBUS_TIMEOUT = 5000
#: Seconds kdotool, or a version query, may take.
COMMAND_TIMEOUT = 10
#: Seconds a script of our own is left loaded in KWin before it is unloaded.
SCRIPT_RUN = 0.5
#: Qt's key codes on Wayland are evdev codes plus this, as X's are.
KEYCODE_OFFSET = 8


def _bus() -> object:
	from gi.repository import Gio

	return Gio.bus_get_sync(Gio.BusType.SESSION)


def _call(
	name: str, path: str, interface: str, method: str, parameters: object = None, reply: str | None = None
) -> tuple:
	from gi.repository import GLib

	answer = _bus().call_sync(
		name,
		path,
		interface,
		method,
		parameters,
		GLib.VariantType(reply) if reply else None,
		0,
		DBUS_TIMEOUT,
		None,
	)
	return answer.unpack() if answer is not None else ()


def _names() -> set[str]:
	return set(_call("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "ListNames")[0])


def ready() -> bool:
	names = _names()
	return all(name in names for name in STARTED_NAMES) and SPLASH_NAME not in names


def _version(argv: list[str]) -> str:
	try:
		answer = subprocess.run(argv, capture_output=True, text=True, timeout=COMMAND_TIMEOUT, check=False)
	except (OSError, subprocess.TimeoutExpired) as e:
		return f"error: {e}"
	return (answer.stdout or answer.stderr).strip()


def _kdotool(*args: str) -> subprocess.CompletedProcess:
	return subprocess.run(
		["kdotool", *args], capture_output=True, text=True, timeout=COMMAND_TIMEOUT, check=False
	)


def facts() -> dict:
	from gi.repository import GLib

	try:
		layouts = _call(KEYBOARD, "/Layouts", "org.kde.KeyboardLayouts", "getLayoutsList", reply="(a(sss))")[
			0
		]
		layout = _call(KEYBOARD, "/Layouts", "org.kde.KeyboardLayouts", "getLayout", reply="(u)")[0]
	except GLib.Error as e:
		layouts, layout = f"error: {e.message}", None
	try:
		owner = _call(
			"org.freedesktop.DBus",
			"/org/freedesktop/DBus",
			"org.freedesktop.DBus",
			"GetNameOwner",
			GLib.Variant("(s)", (KEYBOARD,)),
		)[0]
	except GLib.Error as e:
		owner = f"error: {e.message}"
	active = _kdotool("getactivewindow", "getwindowname")
	return {
		"kwin": _version(["kwin_wayland", "--version"]),
		"plasmashell": _version(["plasmashell", "--version"]),
		"layouts": [list(entry) for entry in layouts] if isinstance(layouts, list) else layouts,
		"layout": layout,
		"keyboardOwner": owner,
		"names": sorted(name for name in _names() if name.startswith("org.kde.")),
		"kdotoolActive": {"returncode": active.returncode, "stdout": active.stdout.strip()},
	}


def windows() -> list[dict]:
	import gi

	gi.require_version("Atspi", "2.0")
	from gi.repository import Atspi, GLib

	Atspi.init()
	Atspi.set_timeout(ATSPI_TIMEOUT, ATSPI_STARTUP_TIMEOUT)
	desktop = Atspi.get_desktop(0)
	found = []
	for index in range(desktop.get_child_count()):
		try:
			application = desktop.get_child_at_index(index)
			if application is None:
				continue
			name = application.get_name()
			pid = application.get_process_id()
			count = application.get_child_count()
		except GLib.Error:
			continue
		for child in range(count):
			try:
				window = application.get_child_at_index(child)
				if window is None:
					continue
				states = window.get_state_set()
				found.append(
					{
						"application": name,
						"pid": pid,
						"title": window.get_name(),
						"role": window.get_role_name(),
						"active": states.contains(Atspi.StateType.ACTIVE),
						"showing": states.contains(Atspi.StateType.SHOWING),
						"shell": name in SHELL_APPLICATIONS,
					}
				)
			except GLib.Error:
				continue
	return found


def _script(title: str) -> bool:
	"""Activate the window captioned title with a KWin script loaded over D-Bus, as kdotool does."""
	from gi.repository import GLib

	source = (
		"for (const window of workspace.windowList()) {\n"
		f"\tif (window.caption === {json.dumps(title)}) {{\n"
		"\t\tworkspace.activeWindow = window;\n"
		"\t\tbreak;\n"
		"\t}\n"
		"}\n"
	)
	plugin = f"ground-activate-{os.getpid()}"
	with tempfile.NamedTemporaryFile("w", suffix=".js", dir="/tmp/ground", delete=False) as f:
		f.write(source)
	try:
		number = _call(
			KWIN, "/Scripting", "org.kde.kwin.Scripting", "loadScript", GLib.Variant("(ss)", (f.name, plugin))
		)[0]
		if number < 0:
			return False
		_call(KWIN, f"/Scripting/Script{number}", "org.kde.kwin.Script", "run")
		time.sleep(SCRIPT_RUN)
		_call(KWIN, "/Scripting", "org.kde.kwin.Scripting", "unloadScript", GLib.Variant("(s)", (plugin,)))
		return True
	except GLib.Error as e:
		print(f"focusPlasma: KWin's script failed: {e.message}", file=sys.stderr)
		return False
	finally:
		os.unlink(f.name)


def activate(title: str) -> bool:
	# kdotool matches a pattern; the ground's titles have nothing in them a pattern would read.
	found = _kdotool("search", "--name", f"^{title}$")
	ids = found.stdout.split()
	if found.returncode == 0 and ids and _kdotool("windowactivate", ids[0]).returncode == 0:
		return True
	print(f"focusPlasma: kdotool could not activate the window: {found.stderr.strip()}", file=sys.stderr)
	return _script(title)


def window(path: str, title: str) -> None:
	import ctypes

	from evdev import ecodes
	from PyQt6.QtCore import QEvent, QObject, QTimer
	from PyQt6.QtGui import QKeyEvent
	from PyQt6.QtWidgets import QApplication, QHBoxLayout, QLabel, QLineEdit, QWidget

	xkbcommon = ctypes.CDLL(os.environ["GROUND_XKBCOMMON"])
	out = open(path, "a", buffering=1)  # noqa: SIM115

	def record(kind: str, **fields: object) -> None:
		out.write(json.dumps({"kind": kind, "received": time.monotonic(), **fields}) + "\n")

	def keysymName(keysym: int) -> str | None:
		buffer = ctypes.create_string_buffer(64)
		length = xkbcommon.xkb_keysym_get_name(ctypes.c_uint32(keysym), buffer, ctypes.c_size_t(64))
		return buffer.value.decode() if length > 0 else None

	def keyName(code: int) -> str:
		name = ecodes.KEY.get(code, str(code))
		return name[0] if isinstance(name, list) else name

	class Recorder(QObject):
		"""Sees each key once, as it comes to the window, before any widget of it does."""

		def eventFilter(self, watched: QObject, event: QEvent) -> bool:
			# A ShortcutOverride is a QKeyEvent too, sent ahead of each press and each release.
			if (
				event.type() not in (QEvent.Type.KeyPress, QEvent.Type.KeyRelease)
				or not watched.isWindowType()
			):
				return False
			# PyQt aborts the program on an exception that leaves a Qt virtual.
			try:
				assert isinstance(event, QKeyEvent), event
				press = event.type() == QEvent.Type.KeyPress
				if event.isAutoRepeat() and not press:
					return False
				code = event.nativeScanCode() - KEYCODE_OFFSET
				record(
					"key",
					t=time.monotonic(),
					window=title,
					code=code,
					key=keyName(code),
					keyval=keysymName(event.nativeVirtualKey()),
					value=(2 if event.isAutoRepeat() else 1) if press else 0,
				)
			except Exception as e:  # noqa: BLE001
				record("error", error=repr(e))
			return False

	application = QApplication([sys.argv[0]])
	# The name AT-SPI gives the program, which would otherwise be this file's.
	application.setApplicationName("testWindowQt")
	recorder = Recorder()
	application.installEventFilter(recorder)

	frame = QWidget()
	frame.setWindowTitle(title)
	frame.resize(400, 200)
	row = QHBoxLayout(frame)
	label = QLabel("&Name")
	entry = QLineEdit()
	label.setBuddy(entry)
	row.addWidget(label)
	row.addWidget(entry)

	said = False

	def onFocus(*_args: object) -> None:
		nonlocal said
		if said or not frame.isActiveWindow():
			return
		if not entry.hasFocus():
			entry.setFocus()
			return
		said = True
		record("ready")

	application.focusChanged.connect(onFocus)
	application.applicationStateChanged.connect(onFocus)
	frame.show()
	entry.setFocus()
	QTimer.singleShot(0, lambda: record("shown", platform=application.platformName()))
	QTimer.singleShot(0, onFocus)
	application.exec()


if __name__ == "__main__":
	if len(sys.argv) in (3, 4) and sys.argv[1] == "window":
		window(sys.argv[2], sys.argv[3] if len(sys.argv) == 4 else "Ground Qt window")
	else:
		sys.exit("usage: focusPlasma.py window OUT [TITLE]")
