"""Whether a desktop's shell is on the accessibility bus, and what it says there, found with no Linda
and no screen reader setting: the GNOME half of experiment E9, and the Plasma half in its guest.

The file has two halves. In the guest, as ground-shell-probe (tools.nix installs it), it is a plain
AT-SPI client run in the session:

	shellProbe.py OUT SECONDS

It writes one JSON object a line to OUT: "settings", the desktop's screen reader and toolkit
accessibility settings and the bus's IsEnabled and ScreenReaderEnabled as found; "applications",
phase "before", each application on the AT-SPI desktop with its process, toolkit and top-level
children; then it claims IsEnabled, as a screen reader does, and after a pause lists them again, phase
"claimed"; "listening", once it has registered for window, focus, state, children and property
events; after SECONDS, "events", the count of each event type from each application and the first
of them in order, by role and accessible name only; the applications again, phase "after"; and
"done". No text event is asked for, so nothing typed is recorded.

On the host, probe runs it in the guest around a key sequence a case gives, and answers the records
by kind.
"""

import json
import sys
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
	from ground import Ground

#: Seconds after IsEnabled is claimed before the desktop is listed again, for toolkits that load
#: their bridge only then.
CLAIM_SETTLE = 3.0
#: Events listed in order, after which they are only counted.
FIRST_EVENTS = 300
EVENTS = (
	"window:",
	"focus:",
	"object:state-changed:",
	"object:children-changed:",
	"object:property-change:",
	"object:active-descendant-changed",
	"object:selection-changed",
)
OUTPUT = "/tmp/ground/shellProbe.jsonl"


def probe(ground: "Ground", keys: Callable[[], None], seconds: float = 8.0) -> dict[str, object]:
	"""Run the probe in the guest's session, call keys once it listens, and answer its records by kind,
	the applications by phase."""
	ground.shell(f"rm -f {OUTPUT}")
	ground.start("shell-probe", ["ground-shell-probe", OUTPUT, str(seconds)])

	def kinds() -> dict[str, object]:
		found: dict[str, object] = {}
		for record in ground.readJsonLines(OUTPUT):
			key = record["kind"] if record["kind"] != "applications" else f"applications {record['phase']}"
			found[key] = record
		return found

	ground.waitFor(lambda: "listening" in kinds(), "the shell probe to listen", 60)
	keys()
	ground.waitFor(lambda: "done" in kinds(), "the shell probe to finish", seconds + 60)
	result = kinds()
	ground.machine.log("the shell probe found:\n" + json.dumps(result, indent=1))
	return result


def _guest(path: str, seconds: float) -> None:
	import gi

	gi.require_version("Atspi", "2.0")
	from gi.repository import Atspi, Gio, GLib

	out = open(path, "a", buffering=1)  # noqa: SIM115

	def write(kind: str, **fields: object) -> None:
		out.write(json.dumps({"kind": kind, "t": time.monotonic(), **fields}) + "\n")

	bus = Gio.bus_get_sync(Gio.BusType.SESSION)

	def status(name: str) -> object:
		try:
			answer = bus.call_sync(
				"org.a11y.Bus",
				"/org/a11y/bus",
				"org.freedesktop.DBus.Properties",
				"Get",
				GLib.Variant("(ss)", ("org.a11y.Status", name)),
				None,
				0,
				5000,
				None,
			)
			return answer.unpack()[0]
		except GLib.Error as e:
			return f"error: {e.message}"

	def setting(schema: str, key: str) -> object:
		source = Gio.SettingsSchemaSource.get_default()
		if source is None or source.lookup(schema, True) is None:
			return None
		return Gio.Settings.new(schema).get_boolean(key)

	write(
		"settings",
		screenReaderEnabled=setting("org.gnome.desktop.a11y.applications", "screen-reader-enabled"),
		toolkitAccessibility=setting("org.gnome.desktop.interface", "toolkit-accessibility"),
		isEnabled=status("IsEnabled"),
		screenReaderStatus=status("ScreenReaderEnabled"),
	)

	Atspi.init()
	Atspi.set_timeout(2000, 5000)

	def applications(phase: str) -> None:
		desktop = Atspi.get_desktop(0)
		found = []
		for index in range(desktop.get_child_count()):
			try:
				application = desktop.get_child_at_index(index)
				if application is None:
					continue
				entry: dict[str, object] = {
					"name": application.get_name(),
					"pid": application.get_process_id(),
					"toolkit": application.get_toolkit_name(),
				}
				children = []
				for child in range(min(application.get_child_count(), 20)):
					accessible = application.get_child_at_index(child)
					if accessible is not None:
						children.append({"role": accessible.get_role_name(), "name": accessible.get_name()})
				entry["children"] = children
			except GLib.Error as e:
				entry = {"index": index, "error": e.message}
			found.append(entry)
		write("applications", phase=phase, applications=found)

	applications("before")
	bus.call_sync(
		"org.a11y.Bus",
		"/org/a11y/bus",
		"org.freedesktop.DBus.Properties",
		"Set",
		GLib.Variant("(ssv)", ("org.a11y.Status", "IsEnabled", GLib.Variant("b", True))),
		None,
		0,
		5000,
		None,
	)
	time.sleep(CLAIM_SETTLE)
	applications("claimed")

	counts: dict[str, int] = {}
	first: list[dict] = []
	names: dict[int, str] = {}

	def applicationOf(source: Atspi.Accessible) -> str:
		try:
			application = source.get_application()
			key = application.get_process_id()
			if key not in names:
				names[key] = application.get_name()
			return names[key]
		except GLib.Error:
			return "?"

	def onEvent(event: Atspi.Event) -> None:
		application = applicationOf(event.source)
		key = f"{application} {event.type}"
		counts[key] = counts.get(key, 0) + 1
		if len(first) < FIRST_EVENTS:
			try:
				role, name = event.source.get_role_name(), event.source.get_name()
			except GLib.Error:
				role, name = "?", "?"
			first.append(
				{
					"t": time.monotonic(),
					"type": event.type,
					"application": application,
					"role": role,
					"name": name,
					"detail1": event.detail1,
				}
			)

	listener = Atspi.EventListener.new(onEvent)
	for event in EVENTS:
		listener.register(event)
	write("listening")

	def end() -> bool:
		Atspi.event_quit()
		return False

	GLib.timeout_add(int(seconds * 1000), end)
	Atspi.event_main()
	write("events", counts=counts, first=first)
	applications("after")
	write("done")


if __name__ == "__main__":
	if len(sys.argv) != 3:
		sys.exit("usage: shellProbe.py OUT SECONDS")
	_guest(sys.argv[1], float(sys.argv[2]))
