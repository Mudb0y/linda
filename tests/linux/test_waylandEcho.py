"""Typing echo in a GTK 4 password entry, its keys taken by the stand-in for linda-keyd: ten letters
typed give ten stars and nothing of the letters, at the stand-in's pace and at the twelve keys a
second the GNOME guest types at. A test plugin writes, for each insert into the protected focus that
reaches typedChars, only its offset, its length, whether its text was withheld and how long after its
key it came, so that what the toolkit sends is known when a count is wrong.

Under the rig's compositors Linda runs natively; in the X rig it is told the session is Wayland and
the stand-in puts the keys out on the X display:

	nix develop . -c tests/linux/rig python3 -m pytest -q tests/linux/test_waylandEcho.py
	nix develop . -c tests/linux/wayland-gate SCRATCH tests/linux/test_waylandEcho.py
"""

import json
import shutil
import sys
import tempfile
import textwrap
import time
from collections.abc import Iterator
from pathlib import Path

import desktopSession
import harness
import keydStandIn
import pytest
from desktopSession import DesktopSession
from keydStandIn import StandIn
from test_keys import STARTUP_TIMEOUT, configText

#: Typed into the password entry, and never to be spoken or logged.
MARKER = "qxzvbjwkpm"
TITLE = "Echo fixture"
#: typedChars.TYPING_WINDOW: an insert that comes later than this after the last key is not echoed.
TYPING_WINDOW = 0.5
#: Seconds for the last insert to come and its star to be spoken.
SETTLE = 2.0
#: (seconds a key is held, seconds after each press and each release): the stand-in's own pace, about
#: six keys a second, and the GNOME guests' typePassword, about twelve.
PACES = {"steady": (0.1, 0.03), "fast": (0.03, 0.025)}

#: A window with an entry and a GTK 4 password entry below it, which has the focus.
FIXTURE = textwrap.dedent(
	"""\
	import sys

	import gi

	gi.require_version("Gtk", "4.0")
	from gi.repository import Gtk

	app = Gtk.Application(application_id="org.linda.WaylandEcho")


	def activate(application):
		window = Gtk.ApplicationWindow(application=application, title=sys.argv[1])
		window.set_default_size(400, 200)
		box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
		entry = Gtk.Entry()
		entry.update_property([Gtk.AccessibleProperty.LABEL], ["Name"])
		password = Gtk.PasswordEntry()
		password.update_property([Gtk.AccessibleProperty.LABEL], ["Secret"])
		box.append(entry)
		box.append(password)
		window.set_child(box)
		window.present()
		password.grab_focus()


	app.connect("activate", activate)
	app.run([])
	"""
)

#: Writes to LINDA_ECHO_PROBE what typedChars is given for each insert into a protected focus, and each
#: echo it queues; nothing of the text.
PROBE_PLUGIN = textwrap.dedent(
	"""\
	import json
	import os
	import time

	import eventHandler
	import globalPluginHandler
	from linuxPlatform.atspi import typedChars

	OUT = open(os.environ["LINDA_ECHO_PROBE"], "a", buffering=1)


	class GlobalPlugin(globalPluginHandler.GlobalPlugin):
		def __init__(self):
			super().__init__()
			handle = typedChars.handleTextChange
			queue = typedChars._queueEcho

			def handled(obj, kind, offset, length, text):
				focus = eventHandler.lastQueuedFocusObject
				if kind == "insert" and getattr(focus, "isProtected", False):
					key = typedChars._lastKeyDown()
					now = time.monotonic()
					OUT.write(json.dumps({
						"insert": offset, "length": length, "withheld": text is None,
						"inside": getattr(obj, "node", None) != getattr(focus, "node", None),
						"afterKey": None if key is None else now - key.time,
					}) + "\\n")
				return handle(obj, kind, offset, length, text)

			def queued(interrupt, speak):
				OUT.write(json.dumps({"echo": True}) + "\\n")
				return queue(interrupt, speak)

			typedChars.handleTextChange = handled
			typedChars._queueEcho = queued
	"""
)


@pytest.fixture
def desktop(tmp_path: Path) -> Iterator[DesktopSession]:
	with DesktopSession(tmp_path / "desktop") as session:
		yield session


@pytest.fixture
def keyboard() -> Iterator[StandIn]:
	"""The stand-in, putting keys out where the session's programs have them."""
	directory = Path(tempfile.mkdtemp(prefix="keyd", dir="/tmp"))
	sink = keydStandIn.compositorSink() if desktopSession.compositor() is not None else None
	try:
		with StandIn(directory / "socket", sink=sink) as standIn:
			yield standIn
	finally:
		shutil.rmtree(directory, ignore_errors=True)


def lindaEnv(keyboard: StandIn, probe: Path, hooks: Path) -> dict[str, str | None]:
	env: dict[str, str | None] = {
		"LINDA_TEST_HOOKS_DIR": str(hooks),
		"LINDA_TEST_KEYD_SOCKET": keyboard.path,
		"LINDA_ECHO_PROBE": str(probe),
	}
	if desktopSession.compositor() is None:
		return {
			**env,
			"LINDA_SESSION_TYPE": "wayland",
			"XDG_CURRENT_DESKTOP": None,
			"XKB_DEFAULT_LAYOUT": "us",
		}
	return {**env, "XKB_DEFAULT_LAYOUT": "pl"}


def probeRecords(probe: Path, since: int) -> list[dict]:
	return [json.loads(line) for line in probe.read_text().splitlines()[since:]]


def typeIntoPassword(desktop: DesktopSession, keyboard: StandIn, tmp_path: Path) -> dict:
	"""Start the fixture and Linda, type MARKER into the password entry at each pace, and answer for
	each what was spoken and what the probe wrote."""
	fixture = tmp_path / "echoFixture.py"
	fixture.write_text(FIXTURE)
	hooks = tmp_path / "hooks"
	(hooks / "globalPlugins").mkdir(parents=True)
	(hooks / "synthDrivers").symlink_to(harness.HERE / "synthDrivers")
	(hooks / "globalPlugins" / "echoProbe.py").write_text(PROBE_PLUGIN)
	probe = tmp_path / "probe.jsonl"
	probe.touch()
	config = tmp_path / "nvda.ini"
	config.write_text(configText("desktop", 7))
	desktop.startApp([sys.executable, str(fixture), TITLE], title=TITLE)
	if desktopSession.compositor() is None:
		desktopSession.activate(TITLE)
	nvda = desktop.startNVDA(config, env=lindaEnv(keyboard, probe, hooks))
	runs = {}
	try:
		desktop.expect("Secret…", since=0, timeout=STARTUP_TIMEOUT)
		keyboard.waitFor(lambda: keyboard.capturing, 10)
		nvda.waitForLog("Keys come from the kernel through linda-keyd", 10)
		time.sleep(1)
		for pace, (hold, gap) in PACES.items():
			mark = desktop.mark()
			given = len(probe.read_text().splitlines())
			started = time.monotonic()
			keyboard.press(*MARKER, hold=hold, gap=gap)
			typed = time.monotonic() - started
			time.sleep(SETTLE)
			runs[pace] = {
				"spoken": desktop.spokenSince(mark),
				"probe": probeRecords(probe, given),
				"seconds a key": typed / len(MARKER),
			}
	finally:
		nvda.process.terminate()
		nvda.waitForExit(20)
	nvda.assertClean(logNames=("nvda.log",))
	harness.assertMarkerAbsent(MARKER, {"nvda.log": nvda.text("nvda.log")})
	return runs


def assertTenStars(runs: dict) -> None:
	for index, (pace, run) in enumerate(runs.items()):
		inserts = [r for r in run["probe"] if "insert" in r]
		echoes = [r for r in run["probe"] if "echo" in r]
		facts = json.dumps(run)
		# Each pace types after what the paces before it typed.
		start = index * len(MARKER)
		assert [r["insert"] for r in inserts] == list(range(start, start + len(MARKER))), facts
		assert all(r["length"] == 1 and r["withheld"] and not r["inside"] for r in inserts), facts
		assert all(r["afterKey"] is not None and r["afterKey"] < TYPING_WINDOW for r in inserts), facts
		assert len(echoes) == len(MARKER), facts
		assert [text.strip() for text in run["spoken"]] == ["star"] * len(MARKER), (pace, facts)


@pytest.mark.rigSession("wayland")
def test_tenLettersInAGtk4PasswordEntryAreTenStars(
	desktop: DesktopSession, keyboard: StandIn, tmp_path: Path
):
	"""Under Mutter and sway the entry inserts each letter alone, withheld, just after its key, and
	each is one star."""
	assertTenStars(typeIntoPassword(desktop, keyboard, tmp_path))


@pytest.mark.rigSession("x11")
def test_tenLettersInAGtk4PasswordEntryAreTenStarsOnTheXDisplay(
	desktop: DesktopSession, keyboard: StandIn, tmp_path: Path
):
	"""Linda told the session is Wayland, the stand-in putting the keys out on the X display: the same."""
	assertTenStars(typeIntoPassword(desktop, keyboard, tmp_path))
