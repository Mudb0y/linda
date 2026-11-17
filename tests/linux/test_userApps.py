"""What programs made for screen reader users need, walked with the keyboard on fixtures that copy their
patterns: a conferencing client in Qt laid out as TeamTalk is, a document reader in GTK 3 that announces
through Orca's remote control as Paperback does, and that remote control as FastSM and Prism use it.

nix develop . -c tests/linux/rig python3 -m pytest -q tests/linux/test_userApps.py

The transcript holds what the synthesizer is given after symbols are spoken as words, so "Alt+C" is
"Alt plus C".
"""

import subprocess
from collections.abc import Iterator
from pathlib import Path

import desktopSession
import harness
import pytest
from desktopSession import DesktopSession

SESSION_CONFIG = harness.HERE / "fixtures" / "sessionConfig" / "nvda.ini"
#: Seconds from starting NVDA to its first words; a start under the rig takes about three.
STARTUP_BOUND = 60.0


@pytest.fixture
def desktop(tmp_path: Path) -> Iterator[DesktopSession]:
	with DesktopSession(tmp_path / "desktop") as session:
		yield session


def startWith(desktop: DesktopSession, fixture: str, title: str, *args: str, expected: tuple[str, ...]):
	"""Start a fixture, give it the keyboard, start NVDA and wait for what it says first."""
	app = desktop.startApp(desktop.fixture(fixture, *args), title=title)
	assert app.stdout.readline() == "ready\n"
	desktopSession.activate(title)
	nvda = desktop.startNVDA(SESSION_CONFIG)
	desktop.expect(*expected, since=0, timeout=STARTUP_BOUND)
	return app, nvda


def step(desktop: DesktopSession, chord: str, *expected: str, timeout: float = 5) -> int:
	"""Press chord and wait for expected, in order; return the mark taken before the key."""
	mark = desktop.mark()
	desktop.press(chord)
	desktop.expect(*expected, since=mark, timeout=timeout)
	return mark


def callOrca(method: str, *args: str, path: str = "/org/gnome/Orca/Service") -> str:
	"""Call a method of Orca's remote control on the rig's session bus, as gdbus prints the answer."""
	interface = "org.gnome.Orca.Service" if path == "/org/gnome/Orca/Service" else "org.gnome.Orca.Module"
	return subprocess.run(
		[
			"gdbus",
			"call",
			"--session",
			"--dest",
			"org.gnome.Orca.Service",
			"--object-path",
			path,
			"--method",
			f"{interface}.{method}",
			*args,
		],
		capture_output=True,
		text=True,
		check=True,
		timeout=10,
	).stdout.strip()


def assertQuitCleanly(desktop: DesktopSession, nvda: harness.Launch) -> None:
	desktop.close()
	assert nvda.process.returncode == 0, nvda.describe()
	nvda.assertClean(logNames=("nvda.log",))


def test_documentReader(desktop: DesktopSession):
	_app, nvda = startWith(
		desktop,
		"gtkFixture",
		"Reader window",
		"reader",
		expected=("Reader window", "edit read only multi line Chapter one"),
	)
	# The document is a read-only field, read by line, character and word as any other.
	step(desktop, "Down", "The first paragraph of chapter one.")
	step(desktop, "Right", "h")
	step(desktop, "control+Left", "The…")
	# The reader moves to the next heading itself and announces it through Orca's remote control.
	step(desktop, "h", "Section A Heading level 2")
	step(desktop, "Insert+Up", "Section A")
	step(desktop, "h", "Chapter two Heading level 1")
	step(desktop, "h", "No next heading.")
	# A list whose first column holds check boxes, with the cursor in the name column: the row once.
	mark = step(desktop, "Tab", "a dot txt; Size: 6 bytes…")
	assert not any("a dot txt; a dot txt" in text for text in desktop.spokenSince(mark))
	# GTK takes the first arrow after the list gains the focus to place its cursor, without moving.
	mark = desktop.mark()
	desktop.press("Down", "Down")
	desktop.expect("b dot png; Size: 2 KB 2 of 2", since=mark)
	step(desktop, "Left", "b dot png; Size: 2 KB 2 of 2")
	assertQuitCleanly(desktop, nvda)


def test_orcaRemoteControl(desktop: DesktopSession):
	_app, nvda = startWith(
		desktop,
		"gtkFixture",
		"Reader window",
		"reader",
		expected=("Reader window", "edit read only multi line Chapter one"),
	)
	# What FastSM and the Prism library behind TeamTalk ask before they speak through it.
	assert callOrca("ListModules") == "(['SpeechAndVerbosityManager'],)"
	module = "/org/gnome/Orca/Service/SpeechAndVerbosityManager"
	assert "'InterruptSpeech'" in callOrca("ListCommands", path=module)
	mark = desktop.mark()
	assert callOrca("PresentMessage", "Three new posts") == "(true,)"
	desktop.expect("Three new posts", since=mark)
	assert callOrca("ExecuteCommand", "InterruptSpeech", "false", path=module) == "(true,)"
	assert callOrca("ExecuteCommand", "NoSuchCommand", "false", path=module) == "(false,)"
	step(desktop, "Insert+t", "Reader window")
	assertQuitCleanly(desktop, nvda)


def test_conferencingClient(desktop: DesktopSession):
	# Qt marks the current tab focused while the channel tree has the keyboard; the tree is what has it.
	_app, nvda = startWith(
		desktop,
		"qtFixture",
		"Qt chat window",
		"chat",
		expected=("Qt chat window", "Channel list tree view", "Lobby; Users: 2 collapsed"),
	)
	assert not any("Chat tab" in text for text in desktop.spokenSince(0))
	# A Qt tree's rows are tree items read with their columns, not cells numbered by row.
	step(desktop, "Down", "Music; Users: 0")
	step(desktop, "Up", "Lobby; Users: 2 collapsed")
	# Qt opens and closes a branch without an event, and replaces the item as it opens one.
	step(desktop, "Right", "Lobby; Users: 2 expanded")
	step(desktop, "Down", "Alice")
	step(desktop, "Left", "Lobby; Users: 2 expanded")
	step(desktop, "Left", "collapsed")
	# A control in a group box is named by its own label; the box is said as its group.
	step(desktop, "shift+Tab", "User Settings grouping", "Gender: combo box Neutral collapsed")
	step(desktop, "shift+Tab", "Send button Alt plus S")
	step(desktop, "shift+Tab", "Message edit blank")
	step(desktop, "shift+Tab", "History edit read only multi line Alice joined the channel.")
	# The menu opens on its first item that can be chosen, and closing it gives the focus back.
	step(desktop, "alt+c", "Channels Alt plus C 1 of 2", "Channel Information I 2 of 3")
	step(desktop, "Down", "Leave Channel L 3 of 3")
	step(desktop, "Escape", "Channels Alt plus C 1 of 2")
	mark = step(desktop, "Escape", "History edit read only multi line…")
	assert not any("tab" in text for text in desktop.spokenSince(mark))
	assertQuitCleanly(desktop, nvda)
