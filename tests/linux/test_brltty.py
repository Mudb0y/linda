"""Braille through BRLTTY: NVDA's BRLTTY driver against a real BRLTTY, the one the brlapi module is
built from, driving a stand-in display.

The display is a CombiBraille as BRLTTY's cb driver knows it, spoken on a pseudo-terminal by this
module: it answers BRLTTY's identification as a 40-cell model, takes the cells BRLTTY writes and
presses keys, whose meaning comes from BRLTTY's own key table for it. Everything from BRLTTY's driver
inwards, the BrlAPI server, its local socket in /var/lib/BrlAPI, the client library and the Python
binding, is what a person's machine runs; only the serial line to a real display is not. The rig's
root is its own, so /var/lib/BrlAPI is made here, and BRLTTY asks for no key.

nix develop . -c tests/linux/rig python3 -m pytest tests/linux/test_brltty.py
"""

import os
import select
import signal
import subprocess
import threading
import time
import tty
from collections.abc import Iterator
from pathlib import Path

import harness
import louis
import pytest
from desktopSession import DesktopSession

SESSION_CONFIG = harness.HERE / "fixtures" / "sessionConfig" / "nvda.ini"
SOCKET_DIRECTORY = Path("/var/lib/BrlAPI")
SOCKET = SOCKET_DIRECTORY / "0"
#: The table NVDA's automatic choice takes for English.
TABLE = harness.SOURCE / "louis" / "tables" / "en-ueb-g1.ctb"
STARTUP_BOUND = 60.0
#: Seconds for BRLTTY to start, find the display and open its socket.
BRLTTY_BOUND = 15.0
#: Seconds for NVDA to take up a BRLTTY that has come: brlapiClient.POLL_INTERVAL and a connection,
#: with room.
RETRY_BOUND = 10.0
#: Seconds NVDA has to exit on SIGTERM.
EXIT_BOUND = 10.0
COLUMNS = 40

ESC = 0x1B
IDENTITY = ord("?")
WRITE = ord("B")
ROUTING = ord("C")
KEYS = ord("K")
#: A CombiBraille has five status cells before its text cells.
STATUS_CELLS = 5
#: The model whose identity is 1 has 40 text cells.
MODEL = 1
#: The bit a CombiBraille gives each dot, dot 1 first: BRLTTY's output table for it.
DOT_BITS = (0x01, 0x02, 0x04, 0x80, 0x40, 0x20, 0x08, 0x10)
#: Keys as BRLTTY's cb driver numbers them, and what its key table, Tables/Input/cb/all.ktb, binds.
THUMB1 = 1 << 8  # FWINLT
THUMB5 = 1 << 12  # FWINRT
#: The status keys come as routing keys 0 to 5; the fifth is bound to LEARN.
STATUS5 = 4


class StandInDisplay:
	"""A CombiBraille on a pseudo-terminal, reached by BRLTTY through a link that plug and unplug make
	and remove, as a display's device node comes and goes."""

	def __init__(self, link: Path) -> None:
		self.link = link
		self._cells: list[list[int]] = []
		self._changed = threading.Condition()
		self._master: int | None = None
		self._slave: int | None = None
		self._thread: threading.Thread | None = None
		self._stop = False

	def plug(self) -> None:
		with self._changed:
			self._cells.clear()
		self._master, self._slave = os.openpty()
		# Raw before BRLTTY opens it, so that nothing it is sent echoes back.
		tty.setraw(self._slave)
		temporary = self.link.with_name(self.link.name + ".new")
		temporary.unlink(missing_ok=True)
		temporary.symlink_to(os.ttyname(self._slave))
		temporary.replace(self.link)
		self._stop = False
		self._thread = threading.Thread(target=self._run, daemon=True)
		self._thread.start()

	def unplug(self) -> None:
		self._stop = True
		self._thread.join()
		os.close(self._master)
		os.close(self._slave)
		self.link.unlink()
		self._master = None

	def press(self, keys: int) -> None:
		os.write(self._master, bytes([ESC, KEYS, keys & 0xFF, keys >> 8]))

	def route(self, cell: int) -> None:
		os.write(self._master, bytes([ESC, ROUTING, cell + 6]))

	def status(self, key: int) -> None:
		os.write(self._master, bytes([ESC, ROUTING, key]))

	@property
	def writes(self) -> int:
		with self._changed:
			return len(self._cells)

	def cells(self) -> list[int]:
		"""The dots of each cell as the display shows them now, dot 1 the lowest bit."""
		with self._changed:
			return list(self._cells[-1]) if self._cells else [0] * COLUMNS

	def waitFor(self, expected: list[int], timeout: float) -> None:
		"""Wait until the display's cells start with expected."""
		deadline = time.monotonic() + timeout
		with self._changed:
			while not (self._cells and self._cells[-1][: len(expected)] == expected):
				remaining = deadline - time.monotonic()
				if remaining <= 0:
					shown = self._cells[-1] if self._cells else None
					raise AssertionError(f"the display shows {shown}, not {expected}")
				self._changed.wait(remaining)

	def _run(self) -> None:
		buffer = b""
		while not self._stop:
			ready, _, _ = select.select([self._master], [], [], 0.05)
			if ready:
				try:
					buffer += os.read(self._master, 4096)
				except OSError:
					return
			while len(buffer) >= 2:
				if buffer[0] != ESC:
					buffer = buffer[1:]
				elif buffer[1] == IDENTITY:
					buffer = buffer[2:]
					os.write(self._master, bytes([ESC, IDENTITY, MODEL]))
				elif buffer[1] == WRITE:
					written, index = [], 2
					# A byte that is ESC comes twice.
					while len(written) < STATUS_CELLS + COLUMNS and index < len(buffer):
						if buffer[index] == ESC:
							if index + 1 >= len(buffer):
								break
							index += 1
						written.append(buffer[index])
						index += 1
					if len(written) < STATUS_CELLS + COLUMNS:
						break
					buffer = buffer[index:]
					cells = [
						sum(1 << dot for dot, bit in enumerate(DOT_BITS) if byte & bit)
						for byte in written[STATUS_CELLS:]
					]
					with self._changed:
						self._cells.append(cells)
						self._changed.notify_all()
				else:
					buffer = buffer[1:]


class Brltty:
	"""BRLTTY as a process of the test's own, with the display at device, its log in directory."""

	def __init__(self, directory: Path, device: Path) -> None:
		self.directory = directory
		self.device = device
		self.process: subprocess.Popen | None = None
		self._starts = 0

	def start(self, waitForDisplay: bool = True) -> None:
		state = self.directory / "state"
		state.mkdir(parents=True, exist_ok=True)
		self._starts += 1
		with open(self.directory / f"brltty{self._starts}.log", "w") as log:
			self.process = subprocess.Popen(
				[
					os.environ["LINDA_BRLTTY"],
					"--no-daemon",
					"--standard-error",
					"--log-level=info,brlkeys,server",
					"--configuration-file=/dev/null",
					f"--preferences-file={state / 'brltty.prefs'}",
					"--braille-driver=cb",
					f"--braille-device=serial:{self.device}",
					"--screen-driver=no",
					"--speech-driver=no",
					"--api-parameters=auth=none",
					f"--updatable-directory={state}",
					f"--writable-directory={state}",
				],
				stdout=log,
				stderr=subprocess.STDOUT,
			)
		deadline = time.monotonic() + BRLTTY_BOUND
		while not SOCKET.is_socket() or (waitForDisplay and "Braille Device:" not in self.log()):
			assert self.process.poll() is None and time.monotonic() < deadline, (
				f"BRLTTY did not start, {harness.describeExit(self.process.poll())}\n{self.log()[-3000:]}"
			)
			time.sleep(0.05)

	def log(self) -> str:
		return (self.directory / f"brltty{self._starts}.log").read_text(errors="replace")

	def stop(self) -> None:
		if self.process is None:
			return
		if self.process.poll() is None:
			self.process.send_signal(signal.SIGCONT)
			self.process.terminate()
			try:
				self.process.wait(5)
			except subprocess.TimeoutExpired:
				self.process.kill()
				self.process.wait()
		self.process = None
		# BRLTTY removes its socket as it exits; one that is killed leaves it.
		SOCKET.unlink(missing_ok=True)

	def pause(self) -> None:
		self.process.send_signal(signal.SIGSTOP)

	def resume(self) -> None:
		self.process.send_signal(signal.SIGCONT)


def dots(text: str) -> list[int]:
	"""The cells NVDA writes for text with its automatic English table."""
	braille = louis.translate([str(TABLE)], text, mode=louis.dotsIO | louis.ucBrl)[0]
	return [ord(character) & 0xFF for character in braille]


def sessionConfig(directory: Path, display: str) -> Path:
	text = SESSION_CONFIG.read_text(encoding="utf-8").replace("display = noBraille", f"display = {display}")
	assert f"display = {display}" in text
	directory.mkdir(parents=True, exist_ok=True)
	path = directory / "nvda.ini"
	path.write_text(text, encoding="utf-8")
	return path


@pytest.fixture
def socketDirectory() -> Iterator[Path]:
	harness.requireRig()
	SOCKET_DIRECTORY.mkdir(parents=True, exist_ok=True)
	SOCKET_DIRECTORY.chmod(0o1777)
	SOCKET.unlink(missing_ok=True)
	yield SOCKET_DIRECTORY
	SOCKET.unlink(missing_ok=True)


@pytest.fixture
def display(tmp_path: Path) -> Iterator[StandInDisplay]:
	standIn = StandInDisplay(tmp_path / "combibraille")
	standIn.plug()
	yield standIn
	if standIn._master is not None:
		standIn.unplug()


@pytest.fixture
def brltty(tmp_path: Path, socketDirectory: Path, display: StandInDisplay) -> Iterator[Brltty]:
	daemon = Brltty(tmp_path / "brltty", display.link)
	yield daemon
	daemon.stop()


@pytest.fixture
def desktop(tmp_path: Path) -> Iterator[DesktopSession]:
	with DesktopSession(tmp_path / "desktop") as session:
		yield session


def startNVDA(desktop: DesktopSession, tmp_path: Path, display: str = "auto") -> harness.Launch:
	nvda = desktop.startNVDA(sessionConfig(tmp_path / "config", display))
	nvda.waitForLog("NVDA initialized", STARTUP_BOUND)
	return nvda


def waitForBrltty(nvda: harness.Launch, timeout: float, times: int = 1) -> None:
	loaded = "Loaded braille display driver 'brltty', current display has 40 cells."
	deadline = time.monotonic() + timeout
	while nvda.text("nvda.log").count(loaded) < times:
		assert nvda.process.poll() is None and time.monotonic() < deadline, (
			f"NVDA did not take up BRLTTY within {timeout} s\n{nvda.describe()}"
		)
		time.sleep(0.05)


def windowDots(nvda: harness.Launch) -> list[int]:
	"""The cells NVDA last wrote, from its log at debug level: each cell's dot numbers, or - for none."""
	lines = [line for line in nvda.text("nvda.log").splitlines() if line.startswith("Braille window dots: ")]
	assert lines, nvda.describe()
	return [
		0 if cell == "-" else sum(1 << (int(dot) - 1) for dot in cell)
		for cell in lines[-1].removeprefix("Braille window dots: ").split(" ")
	]


def waitUntilShown(nvda: harness.Launch, display: StandInDisplay, timeout: float = 10) -> None:
	"""Wait until the display shows what NVDA wrote last. While BRLTTY shows a message of its own, as it
	does when it starts and when it finds the display again, it keeps the display's keys for itself."""
	deadline = time.monotonic() + timeout
	while True:
		try:
			display.waitFor(windowDots(nvda), timeout=0.2)
			return
		except AssertionError:
			if time.monotonic() >= deadline:
				raise


def toggleInputHelp(desktop: DesktopSession, display: StandInDisplay, on: bool) -> None:
	mark = desktop.mark()
	display.status(STATUS5)
	if on:
		desktop.expect("input help on. Press learn again to turn it off.", since=mark)
		display.waitFor(dots("input help on."), timeout=5)
	else:
		desktop.expect("input help off", since=mark)
		display.waitFor(dots("input help off"), timeout=5)


def assertExitedCleanly(desktop: DesktopSession, nvda: harness.Launch) -> None:
	started = time.monotonic()
	desktop.close()
	assert time.monotonic() - started < EXIT_BOUND, nvda.describe()
	assert nvda.process.returncode == 0, nvda.describe()
	nvda.assertClean(logNames=("nvda.log",))


def test_automaticTakesUpBrlttyAndItsKeysRunNvdaCommands(
	desktop: DesktopSession, brltty: Brltty, display: StandInDisplay, tmp_path: Path
):
	brltty.start()
	nvda = startNVDA(desktop, tmp_path)
	waitForBrltty(nvda, STARTUP_BOUND)
	waitUntilShown(nvda, display)

	toggleInputHelp(desktop, display, on=True)
	# Routing keys reach NVDA as BRLTTY's ROUTE command with the cell, which input help counts from 1.
	mark = desktop.mark()
	display.route(3)
	desktop.expect(
		"route 4", "Routes the cursor to or activates the object under this braille cell", since=mark
	)
	# Scrolling is exempt from input help, so that its message can be read: the display shows what
	# follows "route 4" and two tabs, word-wrapped at 40 cells.
	display.press(THUMB5)
	display.waitFor(dots("activates the object under this braille"), timeout=5)
	toggleInputHelp(desktop, display, on=False)

	assert "Lost the connection" not in nvda.text("nvda.log")
	assertExitedCleanly(desktop, nvda)


def brlttyRecords(nvda: harness.Launch) -> list[str]:
	"""The records of NVDA's log at WARNING or above, and those that name BRLTTY's driver."""
	log = nvda.text("nvda.log")
	return [
		match[0]
		for match in harness.NVDA_LOG_RECORD_HEADER.finditer(log)
		if match["level"] in ("WARNING", "ERROR", "CRITICAL") or "brltty" in match[0]
	]


def test_automaticWithoutBrlttyIsQuietAndTakesItUpOnceItRuns(
	desktop: DesktopSession, brltty: Brltty, display: StandInDisplay, tmp_path: Path
):
	# As on most machines: no BRLTTY, and no directory for its sockets.
	SOCKET_DIRECTORY.rmdir()
	nvda = startNVDA(desktop, tmp_path)
	mark = desktop.mark()
	desktop.press("Insert+t")
	desktop.expect("…", since=mark)
	assert "Loaded braille display driver 'brltty'" not in nvda.text("nvda.log")
	assert not [record for record in brlttyRecords(nvda) if "brltty" in record.lower()], nvda.text("nvda.log")

	SOCKET_DIRECTORY.mkdir()
	SOCKET_DIRECTORY.chmod(0o1777)
	brltty.start()
	waitForBrltty(nvda, RETRY_BOUND)
	waitUntilShown(nvda, display)
	toggleInputHelp(desktop, display, on=True)
	toggleInputHelp(desktop, display, on=False)
	assertExitedCleanly(desktop, nvda)


@pytest.mark.parametrize("chosen", ["auto", "brltty"])
def test_brlttyRestartedIsTakenUpAgain(
	desktop: DesktopSession, brltty: Brltty, display: StandInDisplay, tmp_path: Path, chosen: str
):
	brltty.start()
	nvda = startNVDA(desktop, tmp_path, chosen)
	waitForBrltty(nvda, STARTUP_BOUND)
	waitUntilShown(nvda, display)

	brltty.stop()
	nvda.waitForLog("Lost the connection to BRLTTY", 5)
	# NVDA goes on speaking, and says nothing more of BRLTTY while it is away.
	time.sleep(2)
	mark = desktop.mark()
	desktop.press("Insert+t")
	desktop.expect("…", since=mark)

	brltty.start()
	waitForBrltty(nvda, RETRY_BOUND, times=2)
	waitUntilShown(nvda, display)
	toggleInputHelp(desktop, display, on=True)
	toggleInputHelp(desktop, display, on=False)
	warnings = [record for record in brlttyRecords(nvda) if not record.startswith(("INFO", "DEBUG"))]
	assert [record for record in warnings if "brltty" in record] == [
		next(record for record in warnings if "_connectionLost" in record)
	], nvda.text("nvda.log")
	assertExitedCleanly(desktop, nvda)
	assert f"display = {chosen}" in (nvda.directory / "config" / "nvda.ini").read_text(encoding="utf-8")


def test_unpluggedDisplayComesBackWithoutNvdaNoticing(
	desktop: DesktopSession, brltty: Brltty, display: StandInDisplay, tmp_path: Path
):
	brltty.start()
	nvda = startNVDA(desktop, tmp_path)
	waitForBrltty(nvda, STARTUP_BOUND)
	waitUntilShown(nvda, display)

	# BRLTTY keeps its clients while it looks for the display again, and shows what NVDA last wrote
	# once it has found it and its own message has gone.
	display.unplug()
	time.sleep(1)
	display.plug()
	waitUntilShown(nvda, display, timeout=BRLTTY_BOUND)
	toggleInputHelp(desktop, display, on=True)
	toggleInputHelp(desktop, display, on=False)
	assert "Lost the connection" not in nvda.text("nvda.log")
	assertExitedCleanly(desktop, nvda)


def test_brlttyWithoutADisplayIsTakenUpOnceItHasOne(
	desktop: DesktopSession, brltty: Brltty, display: StandInDisplay, tmp_path: Path
):
	display.unplug()
	brltty.start(waitForDisplay=False)
	nvda = startNVDA(desktop, tmp_path)
	# Its server answers, but with no cells NVDA would write to nothing.
	time.sleep(3)
	assert "Loaded braille display driver 'brltty'" not in nvda.text("nvda.log")

	display.plug()
	waitForBrltty(nvda, BRLTTY_BOUND + RETRY_BOUND)
	waitUntilShown(nvda, display)
	toggleInputHelp(desktop, display, on=True)
	toggleInputHelp(desktop, display, on=False)
	assertExitedCleanly(desktop, nvda)


def test_stoppedBrlttyHoldsUpNothing(
	desktop: DesktopSession, brltty: Brltty, display: StandInDisplay, tmp_path: Path
):
	brltty.start()
	nvda = startNVDA(desktop, tmp_path)
	waitForBrltty(nvda, STARTUP_BOUND)
	waitUntilShown(nvda, display)

	# A stopped BRLTTY reads nothing, and its socket is full after about a hundred writes; every
	# title read is a braille message, written once as it comes and once as it goes.
	brltty.pause()
	for _ in range(80):
		desktop.press("Insert+t", gap=0.05)
	mark = desktop.mark()
	desktop.press("Insert+t")
	desktop.expect("…", since=mark, timeout=2)
	assertExitedCleanly(desktop, nvda)
	# The exit gave up on the writes still waiting for BRLTTY.
	assert "BRLTTY did not answer as its connection was closed" in nvda.text("nvda.log")
	brltty.resume()


def test_stoppedBrlttyAtStartIsWaitedForOnlyBriefly(
	desktop: DesktopSession, brltty: Brltty, display: StandInDisplay, tmp_path: Path
):
	brltty.start()
	brltty.pause()
	nvda = startNVDA(desktop, tmp_path)
	mark = desktop.mark()
	desktop.press("Insert+t")
	desktop.expect("…", since=mark)
	assert "Loaded braille display driver 'brltty'" not in nvda.text("nvda.log")

	brltty.resume()
	waitForBrltty(nvda, RETRY_BOUND)
	waitUntilShown(nvda, display)
	toggleInputHelp(desktop, display, on=True)
	toggleInputHelp(desktop, display, on=False)
	assertExitedCleanly(desktop, nvda)


def test_brlttyIsChosenInItsDialogAndKeptForTheNextStart(
	desktop: DesktopSession, brltty: Brltty, display: StandInDisplay, tmp_path: Path
):
	brltty.start()
	nvda = startNVDA(desktop, tmp_path)
	waitForBrltty(nvda, STARTUP_BOUND)
	mark = desktop.mark()
	desktop.press("Insert+control+a")
	desktop.expect(
		"Select Braille Display dialog",
		"Braille display: combo box Automatic (brltty) collapsed Alt plus D",
		since=mark,
	)
	mark = desktop.mark()
	desktop.press("Down")
	desktop.expect("brltty", since=mark)
	desktop.press("Return")
	waitForBrltty(nvda, 10, times=2)
	assertExitedCleanly(desktop, nvda)
	saved = (nvda.directory / "config" / "nvda.ini").read_text(encoding="utf-8")
	assert "display = brltty" in saved, saved

	again = desktop.startNVDA(nvda.directory / "config")
	waitForBrltty(again, STARTUP_BOUND)
	waitUntilShown(again, display)
	toggleInputHelp(desktop, display, on=True)
	toggleInputHelp(desktop, display, on=False)
	assertExitedCleanly(desktop, again)
