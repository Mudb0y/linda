"""Say all keeping the session awake where org.freedesktop.ScreenSaver is all the session has, as on
KDE and Xfce, and the warning where nothing is. tests/linux/test_mateSession.py shows the same with
mate-session's own inhibitors.

NVDA reads a long document in Pluma with the transcript synth taking time over each character, so
that say all lasts. A stand-in for the screen saver records what Linda asks of it.

	nix develop . -c tests/linux/rig python3 -m pytest -q tests/linux/test_idleInhibitor.py
"""

import subprocess
import sys
import time
from pathlib import Path

import harness
from desktopSession import DesktopSession
from test_mateDesktop import SESSION_CONFIG, desktop, makeFolder, startPluma  # noqa: F401

SERVICE = harness.HERE / "fixtures" / "idleServices.py"
#: The transcript synth takes 10 ms a character, so that say all reads for minutes.
TIMED_SPEECH = {"LINDA_TRANSCRIPT_MODE": "timed", "LINDA_TRANSCRIPT_MS_PER_CHAR": "10"}
NOT_HELD = "NVDA cannot keep this Linux session from going idle"
STARTUP_BOUND = 60.0
BOUND = 10.0


def longDocument() -> Path:
	document = makeFolder(Path.home() / "idle") / "long.txt"
	document.write_text("".join(f"Line {number} of a document read aloud.\n" for number in range(400)))
	return document


def waitForEvents(path: Path, count: int, kind: str) -> list[list[str]]:
	"""Wait until the stand-in has recorded count events of kind; return every event it recorded."""
	deadline = time.monotonic() + BOUND
	while True:
		events = [line.split() for line in path.read_text().splitlines()] if path.exists() else []
		if sum(1 for e in events if e[0] == kind) >= count:
			return events
		assert time.monotonic() < deadline, f"no {count} {kind} within {BOUND} s: {events}"
		time.sleep(0.05)


def test_sayAllHoldsTheScreenSaverUntilItStopsOrLindaIsKilled(desktop: DesktopSession, tmp_path: Path):  # noqa: F811
	startPluma(desktop, longDocument())
	nvda = desktop.startNVDA(SESSION_CONFIG, env=TIMED_SPEECH)
	desktop.expect("edit multi line Line 0 of a document read aloud.", since=0, timeout=STARTUP_BOUND)

	# Nothing on the bus to hold the session: warned once, and say all reads regardless.
	mark = desktop.mark()
	desktop.press("Insert+Down")
	nvda.waitForLog(NOT_HELD, BOUND)
	desktop.expect("Line 1 of a document read aloud.", since=mark, timeout=BOUND)
	desktop.press("Control_L")

	events = tmp_path / "events"
	service = subprocess.Popen([sys.executable, str(SERVICE), str(events)], stdout=subprocess.PIPE, text=True)
	try:
		assert service.stdout.readline().strip() == "ready"
		desktop.press("Insert+Down")
		[(_kind, sender, cookie)] = waitForEvents(events, 1, "inhibit")
		desktop.press("Control_L")
		recorded = waitForEvents(events, 1, "uninhibit")
		assert recorded[1] == ["uninhibit", sender, cookie], recorded
		# Each say all holds one, and a killed Linda's goes with its connection.
		desktop.press("Insert+Down")
		recorded = waitForEvents(events, 2, "inhibit")
		nvda.process.kill()
		nvda.process.wait(BOUND)
		recorded = waitForEvents(events, 1, "vanished")
		assert recorded[2:] == [["inhibit", sender, recorded[2][2]], ["vanished", sender]], recorded
		assert nvda.text("nvda.log").count(NOT_HELD) == 1
	finally:
		service.terminate()
		service.wait(BOUND)
