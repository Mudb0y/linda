"""Browse mode in a browser whose own accessibility server cannot be reached, as in Steam's container
and in Flatpak: issue 6.

A program run by pressure-vessel or Flatpak has a runtime directory of its own, and the socket it
offers in answer to GetApplicationBusAddress is in there, where nothing outside can connect to it.
Under Flatpak its name on the accessibility bus also belongs to the proxy between it and the bus,
xdg-dbus-proxy, not to the process that holds the socket. Everything it offers over the bus still
works. Here a real browser is put in the same place with bubblewrap: its runtime directory is a
private one, which holds only the accessibility bus's socket, or its bus is proxied with the filter
Flatpak gives that proxy, or both. Browse mode must start, read the page, move by single letters and
list its elements, all over the bus, and the log must say once which way the program is read and why.

LINDA_RIG_WM=marco nix develop . -c tests/linux/rig python3 -m pytest -q tests/linux/test_sandboxedBrowser.py
"""

import os
import shlex
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import chromiumSession
import harness
import pytest
import webSession
from desktopSession import DesktopSession
from test_browseUsable import (
	EXIT_BOUND,
	FREEZE_MARKS,
	LARGE_FIRST_LINE_BOUND,
	LARGE_LAST_LINE,
	LARGE_NAME_BOUND,
	LARGE_TITLE,
	SETTLE,
	STARTUP_BOUND,
	STEP_BOUND,
	TIMED_SPEECH,
	step,
	stopReading,
	typeEchoed,
	waitForRecord,
)
from test_chromium import ADDRESS_BAR

#: What the engine logs when a program's own server could not be used and it reads it over the bus.
OVER_THE_BUS = "over the accessibility bus, which is slower, because its peer-to-peer server at"
#: What the engine logs when it reads a program over its own server.
PEER_TO_PEER = "over its own peer-to-peer connection because"
#: What NVDA says of a document the engine could not render.
NO_BROWSE_MODE = "No browse mode for this document"
#: Flatpak 1.18's filter on the accessibility bus (flatpak_run_add_a11y_dbus_args in
#: common/flatpak-run-dbus.c), which limits what the program may call on the bus, not what is called
#: on it.
FLATPAK_A11Y_FILTER = (
	"--filter",
	"--sloppy-names",
	"--broadcast=org.a11y.atspi.Registry=org.a11y.atspi.Registry.EventListenerRegistered@/org/a11y/atspi/registry",
	"--broadcast=org.a11y.atspi.Registry=org.a11y.atspi.Registry.EventListenerDeregistered@/org/a11y/atspi/registry",
	"--call=org.a11y.atspi.Registry=org.a11y.atspi.Socket.Embed@/org/a11y/atspi/accessible/root",
	"--call=org.a11y.atspi.Registry=org.a11y.atspi.Socket.Unembed@/org/a11y/atspi/accessible/root",
	"--call=org.a11y.atspi.Registry=org.a11y.atspi.Registry.GetRegisteredEvents@/org/a11y/atspi/registry",
	"--call=org.a11y.atspi.Registry=org.a11y.atspi.DeviceEventController.GetKeystrokeListeners@/org/a11y/atspi/registry/deviceeventcontroller",
	"--call=org.a11y.atspi.Registry=org.a11y.atspi.DeviceEventController.GetDeviceEventListeners@/org/a11y/atspi/registry/deviceeventcontroller",
	"--call=org.a11y.atspi.Registry=org.a11y.atspi.DeviceEventController.NotifyListenersSync@/org/a11y/atspi/registry/deviceeventcontroller",
	"--call=org.a11y.atspi.Registry=org.a11y.atspi.DeviceEventController.NotifyListenersAsync@/org/a11y/atspi/registry/deviceeventcontroller",
)
#: Seconds the proxy has to make its socket.
PROXY_BOUND = 10.0
#: Seconds after Return by which the whole large page is in the buffer: the engine walks all of
#: Chromium's in about 23 s over the bus, where peer to peer takes about 15 (test_chromium.py waits 25).
LARGE_COMPLETE_DELAY = 35.0


@dataclass
class Session:
	desktop: DesktopSession
	nvda: harness.Launch
	browser: webSession.FirefoxProcess
	directory: Path


def _a11yBusSocket() -> Path:
	"""The path of the accessibility bus's socket, as its launcher gives it."""
	reply = subprocess.run(
		[
			"dbus-send",
			"--session",
			"--print-reply=literal",
			"--dest=org.a11y.Bus",
			"/org/a11y/bus",
			"org.a11y.Bus.GetAddress",
		],
		capture_output=True,
		text=True,
		check=True,
	)
	address = reply.stdout.strip()
	assert address.startswith("unix:path="), address
	return Path(address.removeprefix("unix:path=").split(",")[0])


def sandboxed(command: list[str], *, privateRuntime: bool, proxied: bool) -> list[str]:
	"""command run as a container runs it: with a runtime directory of its own that holds only the
	accessibility bus's socket, as pressure-vessel gives Steam's programs; with its accessibility bus
	reached through xdg-dbus-proxy and Flatpak's filter; or both, as Flatpak runs a program."""
	runtime = Path(os.environ["XDG_RUNTIME_DIR"])
	busSocket = _a11yBusSocket()
	wrapped = ["bwrap", "--die-with-parent", "--dev-bind", "/", "/"]
	if privateRuntime:
		assert busSocket.is_relative_to(runtime), busSocket
		wrapped += ["--tmpfs", str(runtime), "--bind", str(busSocket.parent), str(busSocket.parent)]
	if not proxied:
		return [*wrapped, "--", *command]
	# Under the rig's own /tmp, outside the private runtime directory, as Flatpak binds the proxy's
	# socket in from outside; a path under the test's directory is too long for a socket.
	proxySocket = Path(tempfile.mkdtemp(prefix="a11y-bus-proxy-")) / "bus"
	wrapped += ["--setenv", "AT_SPI_BUS_ADDRESS", f"unix:path={proxySocket}"]
	proxy = shlex.join(["xdg-dbus-proxy", f"unix:path={busSocket}", str(proxySocket), *FLATPAK_A11Y_FILTER])
	# One process group, so that the session's close stops the proxy with the browser, and the browser
	# is started only once the proxy listens, as Flatpak waits for it.
	script = (
		f"{proxy} & "
		f"for _ in $(seq {int(PROXY_BOUND * 20)}); do [ -S {shlex.quote(str(proxySocket))} ] && break; sleep 0.05; done; "
		f"exec {shlex.join([*wrapped, '--', *command])}"
	)
	return ["sh", "-c", script]


def startSandboxedChromium(desktop: DesktopSession, page: str, **where: bool) -> webSession.FirefoxProcess:
	"""Chromium on page, a name under webSession.PAGES, in a sandbox as sandboxed makes it."""
	profile = desktop.directory / f"chromium{len(desktop._apps)}"
	profile.mkdir()
	command = [
		"chromium",
		f"--user-data-dir={profile}",
		*chromiumSession.CHROMIUM_SWITCHES,
		webSession.pageURL(page),
	]
	process = chromiumSession._start(
		desktop,
		sandboxed(command, **where),
		window=f"{webSession._pageTitle(page)}{chromiumSession.CHROMIUM_TITLE_SUFFIX}",
		logName=f"{profile.name}.log",
	)
	process.profile = profile
	return process


def startSandboxedFirefox(desktop: DesktopSession, page: str, **where: bool) -> webSession.FirefoxProcess:
	"""Firefox on page, a name under webSession.PAGES, with a fresh profile, in a sandbox as sandboxed
	makes it."""
	profile = desktop.directory / f"firefox{len(desktop._apps)}"
	profile.mkdir()
	(profile / "user.js").write_text(webSession.USER_JS.read_text())
	command = [
		"env",
		"MOZ_CRASHREPORTER_DISABLE=1",
		"firefox",
		"--no-remote",
		"--new-instance",
		"--profile",
		str(profile),
		webSession.pageURL(page),
	]
	process = chromiumSession._start(
		desktop,
		sandboxed(command, **where),
		window=f"{webSession._pageTitle(page)} — Mozilla Firefox",
		logName=f"{profile.name}.log",
	)
	process.profile = profile
	return process


def _session(directory: Path, start, **where: bool) -> Iterator[Session]:
	webSession.requireBrowseLibraries()
	with DesktopSession(directory / "desktop") as desktop:
		chromiumSession.clearAccessibilityStatus()
		nvda = desktop.startNVDA(webSession.BROWSE_CONFIG, env=TIMED_SPEECH)
		nvda.waitForLog("NVDA initialized", STARTUP_BOUND)
		browser = start(desktop, "usable.html", **where)
		yield Session(desktop, nvda, browser, directory)


@pytest.fixture(scope="class")
def steamLike(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Session]:
	yield from _session(
		tmp_path_factory.mktemp("steamLike"),
		startSandboxedChromium,
		privateRuntime=True,
		proxied=False,
	)


def saying(session: Session, chord: str, fragment: str) -> None:
	"""Press chord and wait for something said that holds fragment."""
	desktop = session.desktop
	mark = desktop.mark()
	desktop.press(chord)
	deadline = time.monotonic() + STEP_BOUND
	while not any(fragment in text for text in desktop.spokenSince(mark)):
		assert time.monotonic() < deadline, (
			f"NVDA did not say {fragment!r}; it spoke {desktop.spokenSince(mark)}"
		)
		time.sleep(0.05)
	time.sleep(SETTLE)


def assertReadOverTheBus(session: Session, reason: str) -> None:
	"""The log says once that the browser is read over the bus, because of reason, and never that its
	document could not be rendered."""
	log = session.nvda.text("nvda.log")
	lines = [line for line in log.splitlines() if "Browse mode reads" in line]
	assert len(lines) == 1, lines
	assert OVER_THE_BUS in lines[0], lines[0]
	assert reason in lines[0], lines[0]
	assert "The engine could not render the document" not in log
	assert NO_BROWSE_MODE not in session.nvda.spokenTexts()


def readsThePage(session: Session) -> None:
	"""The page is read as it loads, and its headings and links are found by their letters."""
	session.desktop.expect(
		"heading level 1 Welcome",
		"This page tests browse mode.",
		since=0,
		timeout=STARTUP_BOUND,
	)
	stopReading(session)
	step(session, "ctrl+Home", "heading level 1 Welcome")
	step(session, "h", "News heading level 2")
	step(session, "h", "Contact heading level 2")
	step(session, "shift+h", "News heading level 2")
	step(session, "ctrl+Home", "heading level 1 Welcome")
	step(session, "k", "the news link")
	step(session, "k", "the second page link")


class TestSteamLike:
	"""Chromium with a runtime directory of its own, as pressure-vessel runs Steam's interface, which
	is what issue 6 reported: its own server's socket is not there outside."""

	@pytest.fixture
	def session(self, steamLike: Session) -> Session:
		return steamLike

	def test_browseModeReadsThePage(self, session: Session):
		readsThePage(session)
		assertReadOverTheBus(session, "No such file or directory")

	def test_elementsList(self, session: Session):
		step(session, "ctrl+Home", "heading level 1 Welcome")
		saying(session, "Insert+F7", "Elements List dialog")
		saying(session, "alt+h", "Headings radio button")
		saying(session, "Tab", "Welcome")
		saying(session, "Down", "News")
		saying(session, "alt+m", "News heading level 2")
		step(session, "Down", "Nothing new today.")

	def test_aChangeToThePageIsFollowed(self, session: Session):
		step(session, "ctrl+Home", "heading level 1 Welcome")
		step(session, "e", "form Your name edit…")
		step(session, "Return", "Focus mode")
		typeEchoed(session, "Bob")
		step(session, "Escape", "Browse mode")
		step(session, "Tab", "Add item button")
		session.desktop.press("Return")
		time.sleep(SETTLE)
		step(session, "Down", "out of form list…")
		step(session, "Down", "bullet  Added Bob")

	def test_largePageOverTheBus(self, session: Session):
		"""The large page of the benchmark is read from its top within test_chromium.py's bounds over the
		bus too, without NVDA freezing, and its end is there once the rest has come."""
		page = session.directory / "large.html"
		subprocess.run(
			[sys.executable, str(harness.HERE / "bench" / "makePage.py"), "large", str(page)],
			check=True,
			capture_output=True,
		)
		mark = session.desktop.mark()
		session.desktop.press("ctrl+l")
		waitForRecord(
			session,
			lambda spoken: ADDRESS_BAR in spoken,
			since=mark,
			timeout=STEP_BOUND,
			what="the address bar",
		)
		time.sleep(SETTLE)
		session.desktop.typeText(page.as_uri())
		time.sleep(SETTLE)
		mark = session.desktop.mark()
		session.desktop.press("Return", gap=0)
		returned = time.monotonic()
		name = waitForRecord(
			session,
			lambda spoken: spoken.startswith(LARGE_TITLE),
			since=mark,
			timeout=STEP_BOUND,
			what="the page's name",
		)
		assert name["t"] - returned <= LARGE_NAME_BOUND, (
			f"the page's name came {name['t'] - returned:.2f} s after Return"
		)
		firstLine = waitForRecord(
			session,
			lambda spoken: "Section 0" in spoken,
			since=mark,
			timeout=STEP_BOUND,
			what="the page's first line",
		)
		assert firstLine["t"] - returned <= LARGE_FIRST_LINE_BOUND, (
			f"the page's first line came {firstLine['t'] - returned:.2f} s after Return"
		)
		time.sleep(max(0.0, returned + LARGE_COMPLETE_DELAY - time.monotonic()))
		session.desktop.press("Control_L")
		mark = session.desktop.mark()
		session.desktop.press("ctrl+End", "Up")
		waitForRecord(
			session,
			lambda spoken: LARGE_LAST_LINE in spoken,
			since=mark,
			timeout=STEP_BOUND,
			what="the page's last line",
		)
		log = session.nvda.text("nvda.log")
		assert not [mark for mark in FREEZE_MARKS if mark in log], "the watchdog found NVDA frozen"

	def test_exitIsClean(self, session: Session):
		step(session, "Insert+q", "Exit Linda…")
		session.desktop.press("Return")
		session.nvda.waitForExit(EXIT_BOUND)
		assert session.nvda.process.returncode == 0, session.nvda.describe()
		session.nvda.assertClean(logNames=("nvda.log",))
		assertReadOverTheBus(session, "No such file or directory")


@pytest.mark.parametrize(
	("privateRuntime", "reason"),
	[
		# Flatpak: the socket is in a runtime directory of its own, and the name is the proxy's.
		(True, "No such file or directory"),
		# Only the bus proxied: the socket is there, and held by another process than the name's.
		(False, "is served by process"),
	],
	ids=["flatpakLike", "proxiedBusOnly"],
)
def test_chromiumBehindFlatpaksProxy(tmp_path: Path, privateRuntime: bool, reason: str):
	for session in _session(tmp_path, startSandboxedChromium, privateRuntime=privateRuntime, proxied=True):
		readsThePage(session)
		assertReadOverTheBus(session, reason)


def test_firefoxWithARuntimeDirectoryOfItsOwn(tmp_path: Path):
	for session in _session(tmp_path, startSandboxedFirefox, privateRuntime=True, proxied=False):
		readsThePage(session)
		assertReadOverTheBus(session, "No such file or directory")


def test_aBrowserOutsideAnySandboxIsReadPeerToPeer(tmp_path: Path):
	for session in _session(tmp_path, chromiumSession.startChromium):
		readsThePage(session)
		log = session.nvda.text("nvda.log")
		lines = [line for line in log.splitlines() if "Browse mode reads" in line]
		assert len(lines) == 1 and PEER_TO_PEER in lines[0], lines
