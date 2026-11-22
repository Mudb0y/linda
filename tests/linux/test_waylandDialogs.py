"""NVDA's dialogs on a Wayland display: one left behind another window is brought back by being mapped
again, a modal one without its loop ending, and one the compositor does not give the keyboard says how
to reach it. On an X display Raise and SetFocus do what they did.

The set-up is test_waylandMenu.py's: Linda natively beside the GTK 4 key probe, a plugin of the test's
own that opens and raises NVDA's windows and records what it sees, and an AT-SPI listener.

tests/linux/wayland-gate SCRATCH runs this file under Mutter and sway, with and without Xwayland; in the
X rig only the X test runs:

nix develop . -c tests/linux/rig python3 -m pytest -q tests/linux/test_waylandDialogs.py
"""

import os
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import desktopSession
import harness
import pytest
import test_waylandMenu
from test_waylandMenu import ACTIVATION_BOUND, GTK, KEY_PROBE, MENU_TITLE, Linda, Lines, WaylandSession

SETTINGS = "Linda Settings"
QUESTION = "Probe question"
#: What Linda says of a window of its own the compositor did not give the keyboard, as the transcript has
#: it, with the plus sign spoken.
NOT_REACHED = "Linda's window opened but did not get the keyboard. Press Alt plus Tab to reach it."
#: Seconds after which the line about an unreached window has had every chance to be said again.
REPEAT_WINDOW = 3.0
#: GNOME's setting under which a new window is given the keyboard only if it belongs to the window that
#: has it, as Mutter's window_state_on_map reads it, in the keyfile LINDA_RIG_MUTTER_SETTINGS names.
STRICT_FOCUS = "[org/gnome/desktop/wm/preferences]\nfocus-new-windows='strict'\n"


@pytest.fixture
def session(tmp_path: Path) -> Iterator[WaylandSession]:
	opened = WaylandSession(tmp_path / "session")
	try:
		yield opened
	finally:
		opened.close()


def lindaActivated(session: WaylandSession, title: str, since: float, timeout: float = 5) -> dict:
	return session.listener.activation(
		lambda event: event["pid"] == session.lindaID and event["name"].startswith(title),
		since=since,
		timeout=timeout,
	)


#: A GTK 4 window of its own, which says when it is active; the key probe is a unique application, whose
#: second copy would only present the first.
COVER = """\
import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gio, Gtk


def activate(application):
	window = Gtk.ApplicationWindow(application=application, title="Cover")
	window.set_child(Gtk.Button(label="Covering"))
	window.connect("notify::is-active", lambda w, _p: w.is_active() and print("active", flush=True))
	window.present()


app = Gtk.Application(flags=Gio.ApplicationFlags.NON_UNIQUE)
app.connect("activate", activate)
app.run([])
"""


def coverWithAnotherWindow(session: WaylandSession, name: str) -> Lines:
	"""Open another GTK 4 window, which takes the keyboard as it maps, and wait until it has it.

	GTK's own word is waited for: GTK 4 does not always tell AT-SPI of its first activation."""
	cover = session.start(Lines([sys.executable, "-c", COVER], session.directory / f"{name}.stderr.txt"))
	cover.waitFor(lambda line: line == "active", 15)
	return cover


@pytest.mark.rigSession("wayland")
def test_dialogBehindAnotherWindowIsBroughtBack(session: WaylandSession) -> None:
	linda = session.linda
	session.probeHasFocus()
	opened = linda.command("settings")
	lindaActivated(session, SETTINGS, since=opened)
	cover = coverWithAnotherWindow(session, "cover")
	assert not linda.state(SETTINGS)["active"]

	# Raise, as the log viewer, the Python console and the find dialog are brought back.
	mark = len(linda.spoken())
	raised = linda.command("raise", SETTINGS)
	assert not linda.waitFor("raising", since=raised)["active"]
	activation = lindaActivated(session, SETTINGS, since=raised)
	assert activation["t"] - raised < ACTIVATION_BOUND, activation
	assert linda.waitFor("presentAgain", since=raised)["title"].startswith(SETTINGS)
	linda.waitForState(lambda state: state["active"], SETTINGS)
	linda.expect("Linda Settings: General (normal configuration) dialog", since=mark)
	activeBefore = len(cover.lines)
	session.press("Escape")
	cover.waitFor(lambda line: line == "active", 5, since=activeBefore)

	# Opened again while open, NVDA focuses the open one, which brings it back.
	opened = linda.command("settings")
	lindaActivated(session, SETTINGS, since=opened)
	coverWithAnotherWindow(session, "cover2")
	focused = linda.command("settings")
	lindaActivated(session, SETTINGS, since=focused)
	assert linda.waitFor("presentAgain", since=focused)["title"].startswith(SETTINGS)
	linda.waitForState(lambda state: state["active"], SETTINGS)
	time.sleep(ACTIVATION_BOUND)
	assert NOT_REACHED not in linda.spoken()


@pytest.mark.rigSession("wayland")
def test_modalMessageDialogIsBroughtBackWithItsLoopRunning(session: WaylandSession) -> None:
	linda = session.linda
	session.probeHasFocus()
	asked = linda.command("messageBox", QUESTION)
	lindaActivated(session, QUESTION, since=asked)
	coverWithAnotherWindow(session, "cover")
	assert not linda.state(QUESTION)["active"]

	# As NVDA brings back a modal message it waits on when a command is refused for it.
	mark = len(linda.spoken())
	focused = linda.command("focusBlocking")
	activation = lindaActivated(session, QUESTION, since=focused)
	assert activation["t"] - focused < ACTIVATION_BOUND, activation
	linda.waitForState(lambda state: state["active"], QUESTION)
	linda.expect(f"{QUESTION} dialog", since=mark)
	assert not linda.records("messageAnswered")

	# Raise does the same, and the dialog is still waiting for its answer, which it then takes.
	coverWithAnotherWindow(session, "cover2")
	raised = linda.command("raise", QUESTION)
	lindaActivated(session, QUESTION, since=raised)
	linda.waitForState(lambda state: state["active"], QUESTION)
	assert not linda.records("messageAnswered")
	answered = session.press("Return")
	answer = linda.waitFor("messageAnswered", since=answered)
	assert answer["ok"], answer
	assert NOT_REACHED not in linda.spoken()


def notFocusedIsSaidOnce(session: WaylandSession) -> None:
	"""Linda's menu, which the compositor does not give the keyboard, has Linda say once how to reach it,
	and stays open for the person to."""
	linda = session.linda
	given = linda.command("menu")
	popup = linda.waitFor("popupMenu", since=given)
	deadline = time.monotonic() + 5
	while NOT_REACHED not in linda.spoken():
		assert time.monotonic() < deadline, (
			f"the line was not said: {linda.spoken()}\n{linda.launch.describe()}"
		)
		time.sleep(0.05)
	time.sleep(REPEAT_WINDOW)
	assert linda.spoken().count(NOT_REACHED) == 1, linda.spoken()
	assert not [
		event
		for event in session.listener.events()
		if event["type"] == "window:activate" and event["pid"] == session.lindaID
	]
	# The menu is still open, waiting for the person to reach it.
	assert linda.state()["menuOpen"]
	linda.command("closeMenu", MENU_TITLE)
	linda.waitFor("popupReturned", since=popup["t"])


@pytest.mark.rigSession("sway")
def test_windowTheCompositorDoesNotFocusSaysHowToReachIt(session: WaylandSession) -> None:
	"""sway's no_focus for Linda's windows stands for GNOME's focus-new-windows set to strict, which
	test_windowStrictMutterDoesNotFocusSaysHowToReachIt sets itself."""
	session.probeHasFocus()
	subprocess.run(["swaymsg", f"no_focus [pid={session.lindaID}]"], check=True, capture_output=True)
	notFocusedIsSaidOnce(session)


class _ActivatedKeyProbe(Lines):
	"""The GTK 4 key probe given the keyboard by the compositor's own switch, as a person would under
	strict focus, where no new window is given it."""

	def __init__(self, directory: Path, name: str = "probe") -> None:
		super().__init__([sys.executable, str(KEY_PROBE)], directory / f"{name}.stderr.txt")
		desktopSession.activate("Key probe", timeout=15)
		self.waitFor(lambda line: line == "active", 15)


def _mutterIsStrict() -> bool:
	settings = os.environ.get("LINDA_RIG_MUTTER_SETTINGS")
	return bool(settings) and Path(settings).read_text(encoding="utf-8") == STRICT_FOCUS


@pytest.mark.rigSession("mutter")
def test_windowStrictMutterDoesNotFocusSaysHowToReachIt(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
	"""Under Mutter with focus-new-windows strict, as GNOME users may set it. The gate's Mutter has the
	rig's empty settings, so this runs itself again in a Mutter of its own that starts with strict focus,
	with Xwayland or without as the gate's has it."""
	if not _mutterIsStrict():
		settings = tmp_path / "strict.keyfile"
		settings.write_text(STRICT_FOCUS, encoding="utf-8")
		env = {
			name: value
			for name, value in os.environ.items()
			if not name.startswith(("LINDA_RIG_SESSION", "LINDA_RIG_WM"))
		}
		env.update(LINDA_RIG_COMPOSITOR="mutter", LINDA_RIG_MUTTER_SETTINGS=str(settings))
		ran = subprocess.run(
			[
				str(harness.HERE / "rig"),
				"--bind",
				str(tmp_path),
				sys.executable,
				"-m",
				"pytest",
				"-q",
				"-p",
				"no:cacheprovider",
				f"--basetemp={tmp_path / 'pt'}",
				f"{__file__}::{request.node.name}",
			],
			env=env,
			capture_output=True,
			text=True,
			timeout=300,
			check=False,
		)
		assert ran.returncode == 0, ran.stdout[-6000:] + ran.stderr[-3000:]
		assert "1 passed" in ran.stdout, ran.stdout[-2000:]
		return
	monkeypatch.setattr(test_waylandMenu, "KeyProbe", _ActivatedKeyProbe)
	session = WaylandSession(tmp_path / "session")
	try:
		session.probeHasFocus()
		notFocusedIsSaidOnce(session)
	finally:
		session.close()


@pytest.mark.rigSession("x11")
def test_onXRaiseAndSetFocusDoWhatTheyDid(tmp_path: Path) -> None:
	with desktopSession.DesktopSession(tmp_path / "desktop") as desktop:
		app = desktop.startApp(desktop.fixture(GTK[0]), title=GTK[1])
		assert app.stdout.readline() == "ready\n"
		desktopSession.activate(GTK[1])
		linda = Linda(tmp_path / "linda")
		try:
			assert linda.ready["displayKind"] == "x11"
			assert not linda.ready["keptGtk"], linda.ready
			opened = linda.command("settings")
			linda.waitForState(lambda state: state["active"], SETTINGS)
			desktopSession.activate(GTK[1])
			linda.waitForState(lambda state: state["shown"] and not state["active"], SETTINGS)
			raised = linda.command("raise", SETTINGS)
			assert linda.waitFor("upstreamRaise", since=raised)["title"].startswith(SETTINGS)
			linda.waitForState(lambda state: state["active"], SETTINGS)
			linda.command("settings")
			time.sleep(ACTIVATION_BOUND)
			assert not linda.records("presentAgain", since=opened)
			assert NOT_REACHED not in linda.spoken()
		finally:
			linda.stop()
