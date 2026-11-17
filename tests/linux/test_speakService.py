"""The call scripts make to have Orca speak, answered by NVDA: PresentMessage on Orca's service on the
session bus, called from other processes as such a script calls it, with Polish, odd and very long
text, with another owner of the name there before NVDA, and with a program named orca that asks for
the name after NVDA has it, as Orca does when it starts.

nix develop . -c tests/linux/rig python3 -m pytest -q tests/linux/test_speakService.py
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
#: Seconds a call may take to be answered.
REPLY_BOUND = 1.0
#: Seconds for the name to pass between owners.
HANDOVER_BOUND = 5.0
#: Seconds to listen for speech that must not come.
SETTLE = 0.5

BUS_NAME = "org.gnome.Orca.Service"
OBJECT_PATH = "/org/gnome/Orca/Service"
INTERFACE = "org.gnome.Orca.Service"
GTK = ("gtkFixture", "Fixture window")
AT_START = ("Fixture window", "Press me button Does nothing Alt plus P")
POLISH = "Zażółć gęślą jaźń"
ANSWERING = f"Speak service: NVDA now answers {BUS_NAME}"

#: Calls PresentMessage with the text on standard input, as a script would, and prints the reply and
#: the seconds the call took.
CALLER = f"""
import sys
import time
from gi.repository import Gio, GLib

bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
text = sys.stdin.read()
started = time.monotonic()
reply = bus.call_sync(
	"{BUS_NAME}", "{OBJECT_PATH}", "{INTERFACE}", "PresentMessage",
	GLib.Variant("(s)", (text,)), GLib.VariantType("(b)"), 0, 10000, None,
)
print(reply.unpack()[0], time.monotonic() - started)
"""

#: Another owner of the name: it serves the object, waits as long as its argument says, as Orca does
#: between connecting to the bus and asking for its name, asks for the name as Orca does, without
#: taking it, prints the bus's reply, and then prints each message it is given.
OWNER = f"""\
#!{sys.executable}
import sys
import time

from gi.repository import Gio, GLib

XML = '''<node><interface name="{INTERFACE}"><method name="PresentMessage">
<arg direction="in" type="s"/><arg direction="out" type="b"/></method></interface></node>'''


def onCall(connection, sender, path, interface, method, parameters, invocation):
	print("heard", parameters.unpack()[0], flush=True)
	invocation.return_value(GLib.Variant("(b)", (True,)))


bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
bus.register_object("{OBJECT_PATH}", Gio.DBusNodeInfo.new_for_xml(XML).interfaces[0], onCall, None, None)
time.sleep(float(sys.argv[1]))
(reply,) = bus.call_sync(
	"org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "RequestName",
	GLib.Variant("(su)", ("{BUS_NAME}", 1)), GLib.VariantType("(u)"), 0, -1, None,
).unpack()
print("reply", reply, flush=True)
GLib.MainLoop().run()
"""


@pytest.fixture
def desktop(tmp_path: Path) -> Iterator[DesktopSession]:
	with DesktopSession(tmp_path / "desktop") as session:
		yield session


def start(desktop: DesktopSession, env: dict[str, str] | None = None) -> harness.Launch:
	app = desktop.startApp(desktop.fixture(GTK[0]), title=GTK[1])
	assert app.stdout.readline() == "ready\n"
	desktopSession.activate(GTK[1])
	nvda = desktop.startNVDA(SESSION_CONFIG, env=env)
	desktop.expect(*AT_START, since=0, timeout=STARTUP_BOUND)
	return nvda


def busSend(method: str, *arguments: str) -> subprocess.CompletedProcess:
	"""A call as a script makes it from the command line."""
	return subprocess.run(
		[
			"dbus-send",
			"--session",
			"--print-reply",
			f"--dest={BUS_NAME}",
			OBJECT_PATH,
			f"{INTERFACE}.{method}",
		]
		+ list(arguments),
		capture_output=True,
		text=True,
		timeout=10,
		check=False,
	)


def present(text: str) -> float:
	"""Call PresentMessage with text from another process; the seconds the call took."""
	reply = subprocess.run(
		[sys.executable, "-c", CALLER],
		input=text.encode("utf-8"),
		capture_output=True,
		timeout=20,
		check=True,
	)
	answer, seconds = reply.stdout.split()
	assert answer == b"True", reply
	return float(seconds)


def startOwner(directory: Path, name: str, delay: float) -> subprocess.Popen:
	"""Start another owner of the name as the program name, and wait for the bus's reply to it."""
	program = directory / name
	program.write_text(OWNER, encoding="utf-8")
	program.chmod(0o755)
	owner = subprocess.Popen(
		[str(program), str(delay)],
		stdout=subprocess.PIPE,
		stderr=subprocess.DEVNULL,
		text=True,
	)
	return owner


def ownerReply(owner: subprocess.Popen) -> str:
	return owner.stdout.readline().strip()


def stopOwner(owner: subprocess.Popen) -> None:
	owner.terminate()
	owner.wait(5)


def waitForLogCount(nvda: harness.Launch, text: str, count: int) -> None:
	deadline = time.monotonic() + HANDOVER_BOUND
	while nvda.text("nvda.log").count(text) < count:
		assert time.monotonic() < deadline, f"the log did not say {text!r} {count} times\n{nvda.describe()}"
		time.sleep(0.05)


def test_messagesAreSpokenAndTheCallsAnswered(desktop: DesktopSession):
	nvda = start(desktop)
	waitForLogCount(nvda, ANSWERING, 1)

	mark = desktop.mark()
	started = time.monotonic()
	reply = busSend("PresentMessage", f"string:{POLISH}")
	assert time.monotonic() - started < REPLY_BOUND
	assert reply.returncode == 0 and reply.stdout.splitlines()[-1].strip() == "boolean true", reply
	desktop.expect(POLISH, since=mark)

	# Odd characters and a book's worth of text are spoken, and neither stops NVDA.
	mark = desktop.mark()
	assert present("bell\x07 escape\x1b[0m right to left\N{RIGHT-TO-LEFT OVERRIDE} mark 😀 end") < REPLY_BOUND
	desktop.expect("bell…", since=mark)
	mark = desktop.mark()
	assert present("słowo " * 200_000) < REPLY_BOUND
	desktop.expect("słowo słowo…", since=mark)
	mark = desktop.mark()
	desktop.press("Insert+t")
	desktop.expect("Fixture window", since=mark)

	quit = busSend("Quit")
	assert quit.returncode != 0 and "org.freedesktop.DBus.Error.NotSupported" in quit.stderr, quit
	version = busSend("GetVersion")
	assert version.returncode == 0 and "string" in version.stdout, version
	mark = desktop.mark()
	desktop.press("Insert+Tab")
	desktop.expect("Press me button…", since=mark)

	desktop.close()
	assert nvda.process.returncode == 0, nvda.describe()
	nvda.assertClean(logNames=("nvda.log",))


def test_nameIsSharedWithOtherOwnersAndOrca(desktop: DesktopSession, tmp_path: Path):
	first = startOwner(tmp_path, "owner", 0)
	assert ownerReply(first) == "reply 1"
	nvda = start(desktop)

	# NVDA waits in the queue while another program has the name.
	mark = desktop.mark()
	present("first")
	assert ownerReply(first) == "heard first"
	time.sleep(SETTLE)
	assert "first" not in desktop.spokenSince(mark)
	stopOwner(first)
	waitForLogCount(nvda, ANSWERING, 1)
	mark = desktop.mark()
	present(POLISH)
	desktop.expect(POLISH, since=mark)

	# Orca, starting while NVDA has the name, is given it, and NVDA answers again once it has gone.
	orca = startOwner(tmp_path, "orca", 0.5)
	assert ownerReply(orca) == "reply 1", nvda.describe()
	mark = desktop.mark()
	present("second")
	assert ownerReply(orca) == "heard second"
	time.sleep(SETTLE)
	assert "second" not in desktop.spokenSince(mark)
	stopOwner(orca)
	waitForLogCount(nvda, ANSWERING, 2)
	mark = desktop.mark()
	present("third")
	desktop.expect("third", since=mark)

	desktop.close()
	assert nvda.process.returncode == 0, nvda.describe()
	nvda.assertClean(logNames=("nvda.log",))


def test_serviceCanBeTurnedOff(desktop: DesktopSession):
	nvda = start(desktop, env={"BASEDREADER_SPEAK_SERVICE": "off"})
	reply = busSend("PresentMessage", "string:nobody")
	assert reply.returncode != 0 and "ServiceUnknown" in reply.stderr, reply
	desktop.close()
	assert nvda.process.returncode == 0, nvda.describe()
	nvda.assertClean(logNames=("nvda.log",))
