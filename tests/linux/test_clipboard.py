"""The clipboard, shared with another program through X: NVDA+c reads what xclip put there, and what
NVDA copies with the review cursor or from the About dialog is what xclip reads. Every key is pressed
on the rig's display under marco.

nix develop . -c tests/linux/rig python3 -m pytest -q tests/linux/test_clipboard.py
"""

import signal
import subprocess
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
#: Seconds xclip has to read the clipboard, or to take it.
XCLIP_BOUND = 5.0
#: Seconds from NVDA+c to speech when the clipboard's owner does not answer: NVDA waits one.
HUNG_OWNER_BOUND = 2.0

GTK = ("gtkFixture", "Fixture window")
AT_START = ("Fixture window", "Press me button Does nothing Alt plus P")
POLISH = "Zażółć gęślą jaźń"


@pytest.fixture
def desktop(tmp_path: Path) -> Iterator[DesktopSession]:
	with DesktopSession(tmp_path / "desktop") as session:
		yield session


def readClipboard() -> str:
	"""The clipboard's text as another program reads it."""
	return subprocess.run(
		["xclip", "-selection", "clipboard", "-o", "-t", "UTF8_STRING"],
		capture_output=True,
		check=True,
		timeout=XCLIP_BOUND,
	).stdout.decode("utf-8")


def ownClipboard(text: str) -> subprocess.Popen:
	"""Have xclip own the clipboard with text until another program takes it, and wait until it does."""
	process = subprocess.Popen(
		["xclip", "-quiet", "-selection", "clipboard", "-t", "UTF8_STRING", "-i"],
		stdin=subprocess.PIPE,
		stdout=subprocess.DEVNULL,
		stderr=subprocess.DEVNULL,
	)
	process.stdin.write(text.encode("utf-8"))
	process.stdin.close()
	deadline = time.monotonic() + XCLIP_BOUND
	while True:
		try:
			if readClipboard() == text:
				return process
		except subprocess.CalledProcessError:
			# Until xclip has taken it, the clipboard may have no owner at all to read from.
			pass
		assert time.monotonic() < deadline, "xclip did not take the clipboard"
		time.sleep(0.05)


def step(desktop: DesktopSession, chord: str, *expected: str, timeout: float = 5) -> int:
	mark = desktop.mark()
	desktop.press(chord)
	desktop.expect(*expected, since=mark, timeout=timeout)
	return mark


def test_clipboardIsSharedWithOtherPrograms(desktop: DesktopSession):
	app = desktop.startApp(desktop.fixture(GTK[0]), title=GTK[1])
	assert app.stdout.readline() == "ready\n"
	desktopSession.activate(GTK[1])
	nvda = desktop.startNVDA(SESSION_CONFIG)
	desktop.expect(*AT_START, since=0, timeout=STARTUP_BOUND)

	xclip = ownClipboard(POLISH)
	try:
		step(desktop, "Insert+c", POLISH)
		# An owner that never answers costs NVDA a moment, and then it says there is nothing.
		xclip.send_signal(signal.SIGSTOP)
		mark = desktop.mark()
		pressed = time.monotonic()
		desktop.press("Insert+c", gap=0)
		desktop.expect("There is no text on the clipboard", since=mark)
		assert time.monotonic() - pressed < HUNG_OWNER_BOUND
		step(desktop, "Insert+t", "Fixture window")
	finally:
		xclip.send_signal(signal.SIGCONT)
		xclip.terminate()
		xclip.wait()

	# The review cursor's mark and copy, from the start of the Name field to its end.
	for chord, expected in (
		("Tab", "Enable sound check box…"),
		("Tab", "Size grouping"),
		("Tab", "Colour: combo box…"),
		("Tab", "Name: edit Alt plus N selected Alice"),
	):
		step(desktop, chord, expected)
	desktop.typeText("Źdźbło")
	time.sleep(0.5)
	step(desktop, "Home", "Ź")
	step(desktop, "Insert+F9", "Start marked")
	step(desktop, "End", "blank")
	desktop.press("Insert+F10", "Insert+F10", gap=0.05)
	desktop.expect("Copied to clipboard: Źdźbło", since=0)
	assert readClipboard() == "Źdźbło"
	step(desktop, "Insert+c", "Źdźbło")

	# The About dialog's Copy version number.
	step(desktop, "Insert+n", "Preferences sub Menu P 1 of 9")
	step(desktop, "h", "User Guide U 1 of …")
	step(desktop, "a", "About Linda dialog…")
	mark = step(desktop, "alt+c", "Copied to clipboard")
	version = readClipboard()
	assert version and " (" in version, version
	step(desktop, "Escape", "Fixture window", "Name: edit…")
	# NVDA's built-in speech dictionary puts spaces into words, as wherever a letter runs into a digit
	# in the version's commit hash, so what is read is compared without spaces.
	readMark = step(desktop, "Insert+c")
	deadline = time.monotonic() + 5
	while "".join(version.split()) not in ["".join(text.split()) for text in desktop.spokenSince(readMark)]:
		assert time.monotonic() < deadline, (version, desktop.spokenSince(readMark))
		time.sleep(0.05)
	assert "Unable to copy" not in desktop.spokenSince(mark)

	desktop.close()
	assert nvda.process.returncode == 0, nvda.describe()
	nvda.assertClean(logNames=("nvda.log",))
