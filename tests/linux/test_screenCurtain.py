"""Screen Curtain on X: NVDA's command and settings make every CRTC's gamma ramps zero, and the ramps
that were there come back when it is turned off, when Linda exits, when Linda is killed, and at the
next start should Linda and the curtain's guard both have been killed.

The ramps read back from the X server stand in for looking at the screen: a CRTC whose three ramps
are all zero sends black to its monitor whatever is drawn. Linda runs on an X server of the test's
own, started inside the rig: Xorg with the dummy driver, which has sixteen CRTCs with ramps of 256
entries through the same RandR code a graphics card's driver uses, and for the refusals Xephyr,
whose one CRTC has no ramps, and Xvfb without RandR. The rig's own Xvfb has ramps too, on one CRTC.

nix develop . -c tests/linux/rig python3 -m pytest -q tests/linux/test_screenCurtain.py
"""

import json
import os
import signal
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import harness
import pytest
from desktopSession import DesktopSession
from Xlib import X, display
from Xlib.ext import randr

XORG_CONFIG = harness.HERE / "fixtures" / "xorgDummy.conf"
#: Display numbers of the test's own servers, which the rig's Xvfb, :99, never has.
DUMMY_DISPLAY = ":71"
NO_GAMMA_DISPLAY = ":72"
NO_RANDR_DISPLAY = ":73"
#: Seconds an X server has to accept connections.
SERVER_TIMEOUT = 20.0
#: Seconds from starting Linda to its log saying it has initialised.
STARTUP_TIMEOUT = 60.0
#: Seconds within which a curtain lifted by the guard or set back after a rewrite must show; the
#: curtain checks every second.
RECOVERY_TIMEOUT = 3.0
TOGGLE = "Insert+control+Escape"
#: Bound in the test's gestures.ini, as NVDA binds reportScreenCurtainState to nothing.
REPORT = "Insert+shift+control+F9"
TEMPORARY = "Temporary Screen curtain, enabled until next restart"
ENABLED = "Screen curtain enabled"
DISABLED = "Screen curtain disabled"
COULD_NOT = "Could not enable screen curtain"
#: What the guard's command line holds, by which the test finds it among the rig's processes.
GUARD_MARK = b"gammaCurtainRamps"

Ramps = tuple[tuple[int, ...], tuple[int, ...], tuple[int, ...]]


def startServer(command: list[str], directory: Path, name: str, env: dict | None = None) -> subprocess.Popen:
	"""Start an X server that writes its display number to -displayfd once it accepts connections."""
	read, write = os.pipe()
	with open(directory / f"server{name.strip(':')}.log", "w") as log:
		server = subprocess.Popen(
			[*command, "-displayfd", str(write)],
			pass_fds=(write,),
			stdout=log,
			stderr=subprocess.STDOUT,
			env={**os.environ, **(env or {})},
		)
	os.close(write)
	os.set_blocking(read, False)
	deadline = time.monotonic() + SERVER_TIMEOUT
	written = b""
	while not written.endswith(b"\n"):
		try:
			written += os.read(read, 64)
		except BlockingIOError:
			pass
		if server.poll() is not None or time.monotonic() >= deadline:
			os.close(read)
			server.kill()
			server.wait()
			raise AssertionError(
				f"{command[0]} did not start on {name}, {harness.describeExit(server.poll())}\n"
				f"{(directory / f'server{name.strip(":")}.log').read_text()[-4000:]}",
			)
		time.sleep(0.05)
	os.close(read)
	assert f":{written.decode().strip()}" == name, written
	return server


def stopServer(server: subprocess.Popen) -> None:
	if server.poll() is None:
		server.terminate()
		try:
			server.wait(5)
		except subprocess.TimeoutExpired:
			server.kill()
			server.wait()


@pytest.fixture
def dummy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
	"""Xorg with the dummy driver, made the display of everything the test starts."""
	configDir = tmp_path / "xorg.conf.d"
	configDir.mkdir()
	server = startServer(
		[
			f"{os.environ['LINDA_XORG_BIN']}/Xorg",
			DUMMY_DISPLAY,
			"-config",
			str(XORG_CONFIG),
			# Not the host's /etc/X11/xorg.conf.d, which the sandbox can read.
			"-configdir",
			str(configDir),
			"-modulepath",
			os.environ["LINDA_XORG_MODULE_PATH"],
			"-logfile",
			str(tmp_path / "Xorg.log"),
			"-nolisten",
			"tcp",
			"-noreset",
			"-novtswitch",
			"-sharevts",
			"-keeptty",
		],
		tmp_path,
		DUMMY_DISPLAY,
	)
	# With no keyboard device the server's keymap has old XFree86 key codes, by which Insert is the
	# key evdev calls the numpad's slash; Linda reads evdev's, as on the rig's Xvfb and a desktop.
	subprocess.run(
		["setxkbmap", "-display", DUMMY_DISPLAY, "-rules", "evdev", "-model", "pc105", "-layout", "us"],
		check=True,
	)
	monkeypatch.setenv("DISPLAY", DUMMY_DISPLAY)
	try:
		yield DUMMY_DISPLAY
	finally:
		stopServer(server)


@pytest.fixture
def desktop(dummy: str, tmp_path: Path) -> Iterator[DesktopSession]:
	# A window manager gives the focus back to the settings dialog once the warning over it closes.
	with DesktopSession(tmp_path / "desktop") as session:
		yield session


def connect(name: str) -> display.Display:
	connection = display.Display(name)
	assert connection.has_extension("RANDR")
	return connection


def crtcs(connection: display.Display) -> list[int]:
	return list(randr.get_screen_resources_current(connection.screen().root).crtcs)


def readRamps(name: str) -> dict[int, Ramps]:
	"""Every CRTC's ramps, as the X server holds them."""
	connection = connect(name)
	try:
		ramps = {}
		for crtc in crtcs(connection):
			reply = randr.get_crtc_gamma(connection, crtc)
			ramps[crtc] = (tuple(reply.red), tuple(reply.green), tuple(reply.blue))
		return ramps
	finally:
		connection.close()


def setRamps(name: str, ramps: dict[int, Ramps]) -> None:
	"""Set ramps as a night light or a calibration program does."""
	connection = connect(name)
	try:
		for crtc, (red, green, blue) in ramps.items():
			randr.set_crtc_gamma(connection, crtc, len(red), red, green, blue)
		connection.sync()
	finally:
		connection.close()


def isBlack(ramps: Ramps) -> bool:
	return not any(any(channel) for channel in ramps)


def warm(size: int, strength: float) -> Ramps:
	"""Ramps that warm the picture as a night light does, different for each strength."""
	full = [round(65535 * index / (size - 1)) for index in range(size)]
	return (
		tuple(full),
		tuple(round(level * (1 - strength / 2)) for level in full),
		tuple(round(level * (1 - strength)) for level in full),
	)


def userRamps(name: str) -> dict[int, Ramps]:
	"""Give the first three CRTCs a night light's ramps of their own, leave the rest as the server
	started them, and return what every CRTC then has: what the curtain must put back."""
	before = readRamps(name)
	first = sorted(before)[:3]
	setRamps(name, {crtc: warm(len(before[crtc][0]), 0.2 + 0.2 * index) for index, crtc in enumerate(first)})
	ramps = readRamps(name)
	assert all(ramps[crtc] != before[crtc] for crtc in first)
	assert not any(isBlack(r) for r in ramps.values())
	return ramps


def waitFor(condition, timeout: float, what: str) -> float:
	"""Seconds until condition() is true, polled; fails after timeout."""
	started = time.monotonic()
	while not condition():
		assert time.monotonic() - started < timeout, f"{what} not within {timeout} s"
		time.sleep(0.05)
	return time.monotonic() - started


def guards() -> list[int]:
	"""Process IDs of the curtain's guards running in the rig."""
	found = []
	for entry in Path("/proc").iterdir():
		if not entry.name.isdigit():
			continue
		try:
			if GUARD_MARK in (entry / "cmdline").read_bytes():
				found.append(int(entry.name))
		except OSError:
			continue
	return found


def recordPath(name: str) -> Path:
	return Path(os.environ["XDG_RUNTIME_DIR"]) / "linda" / f"display{name}.curtain"


def writeConfig(directory: Path, **screenCurtain: bool) -> Path:
	"""A configuration directory with the transcript synth, Insert as the NVDA key, a key for
	reportScreenCurtainState, and the screenCurtain settings given."""
	directory.mkdir(parents=True)
	settings = {"enabled": False, "warnOnLoad": False, "playToggleSounds": False, **screenCurtain}
	lines = [
		"schemaVersion = 25",
		"[general]",
		"\tlanguage = en",
		"\tplayStartAndExitSounds = False",
		"\tshowWelcomeDialogAtStartup = False",
		"[speech]",
		"\tsynth = transcript",
		"[braille]",
		"\tdisplay = noBraille",
		"[keyboard]",
		"\tNVDAModifierKeys = 6",
		"[featureFlag]",
		"\tplayErrorSound = 2",
		"[addonStore]",
		"\tautomaticUpdates = disabled",
		"\tbaseServerURL = http://127.0.0.1:1",
		"[screenCurtain]",
		*(f"\t{key} = {value}" for key, value in settings.items()),
		"",
	]
	(directory / "nvda.ini").write_text("\n".join(lines), encoding="utf-8")
	(directory / "gestures.ini").write_text(
		"[globalCommands.GlobalCommands]\nreportScreenCurtainState = kb:NVDA+shift+control+f9\n",
		encoding="utf-8",
	)
	return directory


def startLinda(desktop: DesktopSession, tmp_path: Path, name: str = "config", **screenCurtain: bool):
	nvda = desktop.startNVDA(writeConfig(tmp_path / name, **screenCurtain))
	nvda.waitForLog("NVDA initialized", STARTUP_TIMEOUT)
	return nvda


def say(desktop: DesktopSession, chord: str, *expected: str, timeout: float = 10) -> None:
	mark = desktop.mark()
	desktop.press(chord)
	desktop.expect(*expected, since=mark, timeout=timeout)


def quitLinda(desktop: DesktopSession, nvda: harness.Launch) -> None:
	nvda.process.send_signal(signal.SIGTERM)
	assert nvda.waitForExit(30) == 0, nvda.describe()


def test_commandBlacksEveryCrtcAndPutsBackTheRampsThatWereThere(desktop: DesktopSession, tmp_path: Path):
	found = userRamps(DUMMY_DISPLAY)
	assert len(found) == 16
	nvda = startLinda(desktop, tmp_path)
	say(desktop, REPORT, DISABLED)

	say(desktop, TOGGLE, TEMPORARY)
	curtained = readRamps(DUMMY_DISPLAY)
	assert sorted(curtained) == sorted(found)
	assert all(isBlack(ramps) for ramps in curtained.values()), curtained
	assert len(guards()) == 1
	record = json.loads(recordPath(DUMMY_DISPLAY).read_text())
	assert record["display"] == DUMMY_DISPLAY
	assert {int(crtc): tuple(map(tuple, ramps)) for crtc, ramps in record["crtcs"].items()} == found
	say(desktop, REPORT, TEMPORARY)

	say(desktop, TOGGLE, DISABLED)
	assert readRamps(DUMMY_DISPLAY) == found
	waitFor(lambda: not guards(), RECOVERY_TIMEOUT, "the guard's exit")
	assert not recordPath(DUMMY_DISPLAY).exists()
	say(desktop, REPORT, DISABLED)

	# Pressed twice, as NVDA has it: on until turned off, and kept in the configuration.
	desktop.press(TOGGLE, gap=0.05)
	say(desktop, TOGGLE, ENABLED)
	assert all(isBlack(ramps) for ramps in readRamps(DUMMY_DISPLAY).values())
	say(desktop, REPORT, ENABLED)
	quitLinda(desktop, nvda)
	assert readRamps(DUMMY_DISPLAY) == found
	assert not guards()
	assert not recordPath(DUMMY_DISPLAY).exists()
	assert "enabled = True" in (nvda.directory / "config" / "nvda.ini").read_text()
	nvda.assertClean(logNames=("nvda.log",))


def test_aProgramReadingTheScreenStillGetsItsPixels(desktop: DesktopSession, tmp_path: Path):
	"""What content recognition reads, as a screenshot or screen sharing does: the ramps change what the
	monitors are sent, not what is drawn."""
	connection = connect(DUMMY_DISPLAY)
	try:
		screen = connection.screen()
		window = screen.root.create_window(
			0, 0, 100, 100, 0, screen.root_depth, background_pixel=screen.white_pixel, override_redirect=True
		)
		window.map()
		connection.sync()
		time.sleep(0.2)

		def pixel() -> bytes:
			return screen.root.get_image(50, 50, 1, 1, X.ZPixmap, 0xFFFFFFFF).data[:3]

		assert pixel() == b"\xff\xff\xff"
		nvda = startLinda(desktop, tmp_path)
		say(desktop, TOGGLE, TEMPORARY)
		assert all(isBlack(ramps) for ramps in readRamps(DUMMY_DISPLAY).values())
		assert pixel() == b"\xff\xff\xff"
		# Past the time in which a second press would make it lasting.
		time.sleep(1)
		say(desktop, TOGGLE, DISABLED)
		window.destroy()
		connection.sync()
	finally:
		connection.close()
	quitLinda(desktop, nvda)
	nvda.assertClean(logNames=("nvda.log",))


def test_rampsAnotherProgramSetsAreSetBackWithinASecond(desktop: DesktopSession, tmp_path: Path):
	found = userRamps(DUMMY_DISPLAY)
	nvda = startLinda(desktop, tmp_path)
	say(desktop, TOGGLE, TEMPORARY)
	first = min(found)
	size = len(found[first][0])
	for strength in (0.5, 0.7):
		setRamps(DUMMY_DISPLAY, {first: warm(size, strength)})
		assert not isBlack(readRamps(DUMMY_DISPLAY)[first])
		seconds = waitFor(
			lambda: isBlack(readRamps(DUMMY_DISPLAY)[first]), RECOVERY_TIMEOUT, "the ramps set back to black"
		)
		assert seconds < 1.5, seconds
	assert all(isBlack(ramps) for ramps in readRamps(DUMMY_DISPLAY).values())
	say(desktop, REPORT, TEMPORARY)
	say(desktop, TOGGLE, DISABLED)
	# The ramps that were there when the curtain went on, not those the other program set.
	assert readRamps(DUMMY_DISPLAY) == found
	quitLinda(desktop, nvda)
	log = (nvda.directory / "nvda.log").read_text()
	assert log.count("Another program set the gamma ramps") == 1, log
	nvda.assertClean(logNames=("nvda.log",))


def test_aCrtcTurnedOnUnderTheCurtainIsBlack(desktop: DesktopSession, tmp_path: Path):
	found = userRamps(DUMMY_DISPLAY)
	nvda = startLinda(desktop, tmp_path)
	say(desktop, TOGGLE, TEMPORARY)
	connection = connect(DUMMY_DISPLAY)
	try:
		resources = randr.get_screen_resources_current(connection.screen().root)
		crtc = resources.crtcs[0]
		info = randr.get_crtc_info(connection, crtc, resources.config_timestamp)
		assert info.mode
		# Off and on again, as a mode set or a monitor plugged in does it.
		for mode, outputs in ((0, []), (info.mode, info.outputs)):
			resources = randr.get_screen_resources_current(connection.screen().root)
			randr.set_crtc_config(
				connection, crtc, resources.config_timestamp, 0, 0, mode, randr.Rotate_0, outputs
			)
		connection.sync()
		assert isBlack(readRamps(DUMMY_DISPLAY)[crtc])
	finally:
		connection.close()
	time.sleep(1.5)
	assert all(isBlack(ramps) for ramps in readRamps(DUMMY_DISPLAY).values())
	say(desktop, TOGGLE, DISABLED)
	assert readRamps(DUMMY_DISPLAY) == found
	quitLinda(desktop, nvda)
	nvda.assertClean(logNames=("nvda.log",))


def test_killingLindaLiftsTheCurtainThroughItsGuard(desktop: DesktopSession, tmp_path: Path):
	found = userRamps(DUMMY_DISPLAY)
	nvda = startLinda(desktop, tmp_path)
	say(desktop, TOGGLE, TEMPORARY)
	assert all(isBlack(ramps) for ramps in readRamps(DUMMY_DISPLAY).values())
	assert len(guards()) == 1
	nvda.process.kill()
	nvda.process.wait()
	waitFor(lambda: readRamps(DUMMY_DISPLAY) == found, RECOVERY_TIMEOUT, "the ramps put back by the guard")
	waitFor(lambda: not guards(), RECOVERY_TIMEOUT, "the guard's exit")
	assert not recordPath(DUMMY_DISPLAY).exists()


def test_aGuardThatDiesIsReplacedAndTheCurtainStays(desktop: DesktopSession, tmp_path: Path):
	found = userRamps(DUMMY_DISPLAY)
	nvda = startLinda(desktop, tmp_path)
	say(desktop, TOGGLE, TEMPORARY)
	[first] = guards()
	os.kill(first, signal.SIGKILL)
	waitFor(lambda: guards() and guards() != [first], RECOVERY_TIMEOUT, "another guard")
	assert len(guards()) == 1
	assert all(isBlack(ramps) for ramps in readRamps(DUMMY_DISPLAY).values())
	say(desktop, REPORT, TEMPORARY)
	nvda.process.kill()
	nvda.process.wait()
	waitFor(
		lambda: readRamps(DUMMY_DISPLAY) == found, RECOVERY_TIMEOUT, "the ramps put back by the new guard"
	)
	waitFor(lambda: not guards(), RECOVERY_TIMEOUT, "the guard's exit")


def test_aCurtainLeftByKilledLindaAndGuardIsLiftedAtTheNextStart(desktop: DesktopSession, tmp_path: Path):
	found = userRamps(DUMMY_DISPLAY)
	nvda = startLinda(desktop, tmp_path)
	say(desktop, TOGGLE, TEMPORARY)
	[guard] = guards()
	os.kill(guard, signal.SIGKILL)
	nvda.process.kill()
	nvda.process.wait()
	waitFor(lambda: not guards(), RECOVERY_TIMEOUT, "the guard's end")
	time.sleep(0.5)
	# Nothing left running to lift it: the screen stays black until Linda starts again.
	assert all(isBlack(ramps) for ramps in readRamps(DUMMY_DISPLAY).values())
	assert recordPath(DUMMY_DISPLAY).exists()

	again = startLinda(desktop, tmp_path, "again")
	assert readRamps(DUMMY_DISPLAY) == found
	assert not recordPath(DUMMY_DISPLAY).exists()
	assert f"Lifted a Screen Curtain left on {DUMMY_DISPLAY}" in (again.directory / "nvda.log").read_text()
	say(desktop, REPORT, DISABLED)
	quitLinda(desktop, again)
	again.assertClean(logNames=("nvda.log",))


def test_startWithTheCurtainOnInTheSettings(desktop: DesktopSession, tmp_path: Path):
	found = userRamps(DUMMY_DISPLAY)
	nvda = startLinda(desktop, tmp_path, enabled=True, playToggleSounds=True)
	assert all(isBlack(ramps) for ramps in readRamps(DUMMY_DISPLAY).values())
	assert len(guards()) == 1
	say(desktop, REPORT, ENABLED)
	quitLinda(desktop, nvda)
	assert readRamps(DUMMY_DISPLAY) == found
	assert not guards()
	# Still on in the settings, for the next start, as NVDA keeps it.
	assert "enabled = True" in (nvda.directory / "config" / "nvda.ini").read_text()
	nvda.assertClean(logNames=("nvda.log",))


def test_warningComesFirstAndNoLeavesTheScreenAsItWas(desktop: DesktopSession, tmp_path: Path):
	found = userRamps(DUMMY_DISPLAY)
	nvda = startLinda(desktop, tmp_path, warnOnLoad=True)
	say(desktop, TOGGLE, "Warning dialog Enabling Screen Curtain will make the screen of your computer…")
	# NVDA puts the focus on No, so that Enter declines; Linda does not hear it land there, and says
	# where it is when asked.
	say(desktop, "Insert+Tab", "No button Alt plus N")
	desktop.press("Return")
	time.sleep(1)
	assert readRamps(DUMMY_DISPLAY) == found
	say(desktop, REPORT, DISABLED)

	say(desktop, TOGGLE, "Warning dialog…")
	say(desktop, "alt+y", TEMPORARY)
	assert all(isBlack(ramps) for ramps in readRamps(DUMMY_DISPLAY).values())
	say(desktop, TOGGLE, DISABLED)
	assert readRamps(DUMMY_DISPLAY) == found
	quitLinda(desktop, nvda)
	nvda.assertClean(logNames=("nvda.log",))


def test_theSettingsCheckBoxMakesTheScreenBlackAfterTheWarning(desktop: DesktopSession, tmp_path: Path):
	found = userRamps(DUMMY_DISPLAY)
	nvda = startLinda(desktop, tmp_path, warnOnLoad=True)
	say(desktop, "Insert+n", "Preferences sub Menu P 1 of 9")
	say(desktop, "Return", "Settings... S 1 of 4")
	say(desktop, "Return", "Linda Settings: General (normal configuration) dialog…", "General 1 of 15")
	for category in ("Speech", "Braille", "Audio", "Privacy and Security"):
		say(desktop, "Down", f"{category}…")
	say(desktop, "Tab", "Screen Curtain grouping…")
	desktop.expect("Make screen black (immediate effect) check box not checked", since=0)
	say(desktop, "space", "Warning dialog Enabling Screen Curtain will make the screen of your computer…")
	say(desktop, "alt+y", "Make screen black (immediate effect) check box checked")
	assert all(isBlack(ramps) for ramps in readRamps(DUMMY_DISPLAY).values())
	say(desktop, "space", "not checked")
	assert readRamps(DUMMY_DISPLAY) == found
	say(desktop, "space", "Warning dialog…")
	say(desktop, "alt+y", "Make screen black (immediate effect) check box checked")
	# OK keeps it on, and in the settings.
	desktop.press("Return")
	time.sleep(1)
	say(desktop, REPORT, ENABLED)
	assert all(isBlack(ramps) for ramps in readRamps(DUMMY_DISPLAY).values())
	quitLinda(desktop, nvda)
	assert readRamps(DUMMY_DISPLAY) == found
	assert "enabled = True" in (nvda.directory / "config" / "nvda.ini").read_text()
	nvda.assertClean(logNames=("nvda.log",))


@pytest.fixture
def noGamma(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
	"""Xephyr in a window on the rig's Xvfb: RandR with one CRTC whose gamma ramp size is 0."""
	server = startServer(
		[
			f"{os.environ['LINDA_XORG_BIN']}/Xephyr",
			NO_GAMMA_DISPLAY,
			"-screen",
			"1024x768",
			"-nolisten",
			"tcp",
		],
		tmp_path,
		NO_GAMMA_DISPLAY,
	)
	try:
		connection = connect(NO_GAMMA_DISPLAY)
		try:
			resources = randr.get_screen_resources(connection.screen().root)
			assert [randr.get_crtc_gamma_size(connection, crtc).size for crtc in resources.crtcs] == [0]
		finally:
			connection.close()
		monkeypatch.setenv("DISPLAY", NO_GAMMA_DISPLAY)
		yield NO_GAMMA_DISPLAY
	finally:
		stopServer(server)


def test_aServerWithoutGammaRampsRefuses(noGamma: str, tmp_path: Path):
	with DesktopSession(tmp_path / "desktop", windowManager=False) as desktop:
		nvda = startLinda(desktop, tmp_path)
		say(desktop, TOGGLE, COULD_NOT)
		say(desktop, REPORT, DISABLED)
		assert not guards()
		assert not recordPath(noGamma).exists()
		quitLinda(desktop, nvda)
	log = (nvda.directory / "nvda.log").read_text()
	assert "the gamma ramp size of every CRTC is 0" in log, log
	nvda.assertClean(ignoring=("logErrors",), logNames=("nvda.log",))
	assert len(harness.nvdaLogErrorRecords(log)) == 1, log


def test_aServerWithoutGammaRampsSaysSoAtAStartWithTheCurtainOn(noGamma: str, tmp_path: Path):
	with DesktopSession(tmp_path / "desktop", windowManager=False) as desktop:
		nvda = startLinda(desktop, tmp_path, enabled=True)
		desktop.expect(
			"Could not enable screen curtain dialog There was a problem enabling Screen Curtain. The contents "
			"of your screen are still visible…",
			"OK button Alt plus O",
			since=0,
			timeout=20,
		)
		desktop.press("Return")
		say(desktop, REPORT, DISABLED)
		quitLinda(desktop, nvda)
	assert "the gamma ramp size of every CRTC is 0" in (nvda.directory / "nvda.log").read_text()
	nvda.assertClean(ignoring=("logErrors",), logNames=("nvda.log",))


def test_withoutRandrOrWithoutXTheCurtainRefusesAndSaysWhy(tmp_path: Path):
	server = startServer(
		["Xvfb", NO_RANDR_DISPLAY, "-extension", "RANDR", "-nolisten", "tcp"], tmp_path, NO_RANDR_DISPLAY
	)
	try:
		done = harness.runChild(
			tmp_path,
			f"""
			import os
			from linuxPlatform import gammaCurtain

			result = []
			for name in ({NO_RANDR_DISPLAY!r}, None):
				if name:
					os.environ["DISPLAY"] = name
				else:
					del os.environ["DISPLAY"]
				try:
					gammaCurtain.enable()
					result.append("enabled")
				except gammaCurtain.CurtainUnavailable as e:
					result.append(str(e))
				result.append(gammaCurtain.isActive())
			""",
		)
	finally:
		stopServer(server)
	done.assertClean()
	assert done.value == [
		"the X server has no RandR extension, through which the screen is made black",
		False,
		"there is no X display, and the screen is made black through X",
		False,
	]
