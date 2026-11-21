"""What NVDA says on finding Orca with its service's name in a Wayland session: that both will speak,
once in a run, with the way to turn Orca off on the desktops whose switch is known, and nothing in an
X session. The session is forced to Wayland in the X rig, and Orca is a stand-in that owns the name,
or the one Orca 51 serves instead, under the program name orca.

nix develop . -c tests/linux/rig python3 -m pytest -q tests/linux/test_waylandOrcaNotice.py
"""

import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import desktopSession
import harness
import pytest
from desktopSession import DesktopSession

SESSION_CONFIG = harness.HERE / "fixtures" / "sessionConfig" / "nvda.ini"
#: Seconds from starting NVDA to its first words; a start under the rig takes about three.
STARTUP_BOUND = 60.0
#: Seconds for the name to pass between owners.
HANDOVER_BOUND = 5.0
#: Seconds to listen for speech that must not come.
SETTLE = 1.0

BUS_NAME = "org.gnome.Orca.Service"
OBJECT_PATH = "/org/gnome/Orca/Service"
#: Orca 51 and later serve their own name and path in place of the old ones, which they leave alone.
ORCA1_BUS_NAME = "org.gnome.Orca1.Service"
ORCA1_OBJECT_PATH = "/org/gnome/Orca1/Service"
GTK = ("gtkFixture", "Fixture window")
AT_START = ("Fixture window", "Press me button Does nothing Alt plus P")

NOTICE = "Orca is running too, so both will speak."
#: As the synthesizer is given them, with the plus signs spoken.
GNOME_NOTICE = f"{NOTICE} If Orca was turned on with Super plus Alt plus S, press it again to turn it off."
KDE_NOTICE = f"{NOTICE} If Orca was turned on with Meta plus Alt plus S, press it again to turn it off."
ANSWERING = f"Speak service: NVDA now answers {BUS_NAME}"
WAITING = f"Speak service: NVDA is waiting for the name {BUS_NAME}"
FOUND = "Speak service: Orca has "

#: Another owner of a name, given with its path: it waits as long as its first argument says, as Orca
#: does between connecting to the bus and asking for its name, asks for the name as Orca does, without
#: taking it, and prints the bus's reply.
OWNER = f"""\
#!{sys.executable}
import sys
import time

from gi.repository import Gio, GLib

delay, name, path = sys.argv[1:]
XML = f'''<node><interface name="{{name}}"><method name="PresentMessage">
<arg direction="in" type="s"/><arg direction="out" type="b"/></method></interface></node>'''


def onCall(connection, sender, path, interface, method, parameters, invocation):
	invocation.return_value(GLib.Variant("(b)", (True,)))


bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
bus.register_object(path, Gio.DBusNodeInfo.new_for_xml(XML).interfaces[0], onCall, None, None)
time.sleep(float(delay))
(reply,) = bus.call_sync(
	"org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "RequestName",
	GLib.Variant("(su)", (name, 1)), GLib.VariantType("(u)"), 0, -1, None,
).unpack()
print("reply", reply, flush=True)
GLib.MainLoop().run()
"""


def waylandOn(desktop: str) -> dict[str, str | None]:
	"""A Wayland session whose desktop is named desktop and no compositor's own socket variable."""
	return {
		"LINDA_SESSION_TYPE": "wayland",
		"XDG_SESSION_TYPE": "wayland",
		"XDG_CURRENT_DESKTOP": desktop,
		"GAMESCOPE_WAYLAND_DISPLAY": None,
		"HYPRLAND_INSTANCE_SIGNATURE": None,
		"NIRI_SOCKET": None,
		"SWAYSOCK": None,
	}


#: The owner's own session: lightdm's X11 MATE session, here with a desktop name that would choose
#: GNOME's wording if anything looked at it.
X_SESSION = {
	"LINDA_SESSION_TYPE": None,
	"XDG_SESSION_TYPE": "x11",
	"XDG_CURRENT_DESKTOP": "GNOME",
	"WAYLAND_DISPLAY": None,
	"GAMESCOPE_WAYLAND_DISPLAY": None,
}


@pytest.fixture
def desktop(tmp_path: Path) -> Iterator[DesktopSession]:
	with DesktopSession(tmp_path / "desktop") as session:
		yield session


@pytest.fixture
def owners() -> Iterator[list[subprocess.Popen]]:
	started: list[subprocess.Popen] = []
	yield started
	for owner in started:
		if owner.poll() is None:
			stopOwner(owner)


def start(desktop: DesktopSession, env: dict[str, str | None]) -> harness.Launch:
	app = desktop.startApp(desktop.fixture(GTK[0]), title=GTK[1])
	assert app.stdout.readline() == "ready\n"
	desktopSession.activate(GTK[1])
	return desktop.startNVDA(SESSION_CONFIG, env=env)


def startOwner(
	owners: list[subprocess.Popen],
	directory: Path,
	name: str,
	delay: float,
	busName: str = BUS_NAME,
	objectPath: str = OBJECT_PATH,
) -> subprocess.Popen:
	"""Start another owner of busName as the program name, and wait for the bus's reply to it."""
	program = directory / name
	program.write_text(OWNER, encoding="utf-8")
	program.chmod(0o755)
	owner = subprocess.Popen(
		[str(program), str(delay), busName, objectPath],
		stdout=subprocess.PIPE,
		stderr=subprocess.DEVNULL,
		text=True,
	)
	owners.append(owner)
	assert owner.stdout.readline().strip() == "reply 1"
	return owner


def stopOwner(owner: subprocess.Popen) -> None:
	owner.terminate()
	owner.wait(5)
	owner.stdout.close()


def waitForLogCount(nvda: harness.Launch, text: str, count: int) -> None:
	deadline = time.monotonic() + HANDOVER_BOUND
	while nvda.text("nvda.log").count(text) < count:
		assert time.monotonic() < deadline, f"the log did not say {text!r} {count} times\n{nvda.describe()}"
		time.sleep(0.05)


def noticesSpoken(desktop: DesktopSession) -> list[str]:
	return [text for text in desktop.spokenSince(0) if NOTICE in text]


def finish(desktop: DesktopSession, nvda: harness.Launch) -> None:
	desktop.close()
	assert nvda.process.returncode == 0, nvda.describe()
	nvda.assertClean(logNames=("nvda.log",))


def test_gnomeSaysItOnceThoughOrcaComesTwice(desktop: DesktopSession, owners, tmp_path: Path):
	orca = startOwner(owners, tmp_path, "orca", 0)
	nvda = start(desktop, waylandOn("GNOME"))
	# After the first focus, which would cut it off.
	desktop.expect(*AT_START, GNOME_NOTICE, since=0, timeout=STARTUP_BOUND)

	stopOwner(orca)
	waitForLogCount(nvda, ANSWERING, 1)
	startOwner(owners, tmp_path, "orca", 0.5)
	# NVDA has given the name up to the second Orca and asked again behind it.
	waitForLogCount(nvda, WAITING, 2)
	time.sleep(SETTLE)
	assert noticesSpoken(desktop) == [GNOME_NOTICE], nvda.describe()
	assert nvda.text("nvda.log").count(FOUND) == 1, nvda.describe()
	finish(desktop, nvda)


def test_kdeSaysItWhenOrcaStartsAfterNVDA(desktop: DesktopSession, owners, tmp_path: Path):
	nvda = start(desktop, waylandOn("KDE"))
	desktop.expect(*AT_START, since=0, timeout=STARTUP_BOUND)
	waitForLogCount(nvda, ANSWERING, 1)
	assert noticesSpoken(desktop) == []

	mark = desktop.mark()
	startOwner(owners, tmp_path, "orca", 0.5)
	desktop.expect(KDE_NOTICE, since=mark, timeout=HANDOVER_BOUND)
	time.sleep(SETTLE)
	assert noticesSpoken(desktop) == [KDE_NOTICE], nvda.describe()
	finish(desktop, nvda)


def test_elsewhereOnlyTheFirstSentenceAndOnlyForOrca(desktop: DesktopSession, owners, tmp_path: Path):
	other = startOwner(owners, tmp_path, "owner", 0)
	nvda = start(desktop, waylandOn("sway"))
	desktop.expect(*AT_START, since=0, timeout=STARTUP_BOUND)
	waitForLogCount(nvda, WAITING, 1)
	time.sleep(SETTLE)
	# A program that is not Orca has the name: nothing is said.
	assert noticesSpoken(desktop) == [], nvda.describe()

	stopOwner(other)
	waitForLogCount(nvda, ANSWERING, 1)
	mark = desktop.mark()
	startOwner(owners, tmp_path, "orca", 0.5)
	desktop.expect(NOTICE, since=mark, timeout=HANDOVER_BOUND)
	time.sleep(SETTLE)
	assert noticesSpoken(desktop) == [NOTICE], nvda.describe()
	finish(desktop, nvda)


def test_orca51BeforeNVDA(desktop: DesktopSession, owners, tmp_path: Path):
	startOwner(owners, tmp_path, "orca", 0, ORCA1_BUS_NAME, ORCA1_OBJECT_PATH)
	nvda = start(desktop, waylandOn("GNOME"))
	desktop.expect(*AT_START, GNOME_NOTICE, since=0, timeout=STARTUP_BOUND)
	time.sleep(SETTLE)
	assert noticesSpoken(desktop) == [GNOME_NOTICE], nvda.describe()
	assert nvda.text("nvda.log").count(f"{FOUND}{ORCA1_BUS_NAME}") == 1, nvda.describe()
	finish(desktop, nvda)


def test_orca51AfterNVDA(desktop: DesktopSession, owners, tmp_path: Path):
	nvda = start(desktop, waylandOn("KDE"))
	desktop.expect(*AT_START, since=0, timeout=STARTUP_BOUND)
	waitForLogCount(nvda, ANSWERING, 1)
	assert noticesSpoken(desktop) == []

	mark = desktop.mark()
	startOwner(owners, tmp_path, "orca", 0.5, ORCA1_BUS_NAME, ORCA1_OBJECT_PATH)
	desktop.expect(KDE_NOTICE, since=mark, timeout=HANDOVER_BOUND)
	time.sleep(SETTLE)
	assert noticesSpoken(desktop) == [KDE_NOTICE], nvda.describe()
	finish(desktop, nvda)


@pytest.mark.rigSession("x11")
def test_nothingIsSaidInAnXSession(desktop: DesktopSession, owners, tmp_path: Path):
	orca = startOwner(owners, tmp_path, "orca", 0)
	orca51 = startOwner(owners, tmp_path, "orca", 0, ORCA1_BUS_NAME, ORCA1_OBJECT_PATH)
	nvda = start(desktop, X_SESSION)
	desktop.expect(*AT_START, since=0, timeout=STARTUP_BOUND)
	waitForLogCount(nvda, WAITING, 1)

	stopOwner(orca)
	stopOwner(orca51)
	waitForLogCount(nvda, ANSWERING, 1)
	startOwner(owners, tmp_path, "orca", 0.5)
	startOwner(owners, tmp_path, "orca", 0, ORCA1_BUS_NAME, ORCA1_OBJECT_PATH)
	waitForLogCount(nvda, WAITING, 2)
	time.sleep(SETTLE)
	assert noticesSpoken(desktop) == [], nvda.describe()
	assert FOUND not in nvda.text("nvda.log"), nvda.describe()
	finish(desktop, nvda)
