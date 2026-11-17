"""Chromium and Electron for browse-mode tests: started plainly, as a person starts them, with a fresh
profile, and their documents found on the accessibility desktop; and the accessibility status on the
session bus that decides whether they join that desktop at all.

Inside the rig only. A program runs in a process group of its own, led by the process this starts;
the desktop session's close stops it, and waits for or kills the rest of the group through it.

Chromium, and Electron with it, read org.a11y.Status IsEnabled once, at their start, and join the
accessibility bus only when it is on; so do the variables GNOME_ACCESSIBILITY and QT_ACCESSIBILITY,
which the rig sets for every program, and which a desktop does not. The programs here start without
them, so that what decides is the status NVDA sets, as on a desktop.
"""

import os
import subprocess
import tempfile
import time
from pathlib import Path

import desktopSession
import harness
import webSession
from desktopSession import DesktopSession

#: Electron programs under the fixtures, by directory.
ELECTRON_APPS = harness.HERE / "fixtures" / "apps"
#: The variables that would switch accessibility on whatever the status says.
FORCING_VARIABLES = ("GNOME_ACCESSIBILITY", "QT_ACCESSIBILITY", "QT_LINUX_ACCESSIBILITY_ALWAYS_ON")
#: How every title of Chromium's window ends.
CHROMIUM_TITLE_SUFFIX = " - Chromium"
#: Seconds from starting a program to its window on the accessibility desktop; a first start under
#: the rig takes a few.
WINDOW_TIMEOUT = 60.0
#: Seconds the window then has to take the keyboard.
ACTIVATE_TIMEOUT = 30.0
#: What a sandboxed run with no graphics processor and no network needs, and what keeps a fresh
#: profile from asking first-run questions, signing in, syncing, or fetching anything.
CHROMIUM_SWITCHES = (
	"--no-first-run",
	"--no-default-browser-check",
	"--disable-search-engine-choice-screen",
	"--disable-gpu",
	"--password-store=basic",
	"--disable-sync",
	"--disable-background-networking",
	"--disable-component-update",
	"--disable-default-apps",
	"--disable-breakpad",
	"--no-pings",
	"--disable-features=Translate,MediaRouter,OptimizationHints",
	"--lang=en-US",
)
ELECTRON_SWITCHES = ("--disable-gpu",)


def plainEnvironment() -> dict[str, str]:
	"""This process's environment without what would switch accessibility on by itself."""
	return {name: value for name, value in os.environ.items() if name not in FORCING_VARIABLES}


def _status(method: str, *arguments: str) -> subprocess.CompletedProcess:
	return subprocess.run(
		[
			"dbus-send",
			"--session",
			"--print-reply",
			"--dest=org.a11y.Bus",
			"/org/a11y/bus",
			f"org.freedesktop.DBus.Properties.{method}",
			"string:org.a11y.Status",
			*arguments,
		],
		capture_output=True,
		text=True,
		check=False,
	)


def accessibilityStatus(name: str = "IsEnabled") -> bool | None:
	"""The status property name, or None when the launcher does not have it."""
	reply = _status("Get", f"string:{name}")
	if reply.returncode != 0:
		return None
	return reply.stdout.split()[-1] == "true"


def clearAccessibilityStatus() -> None:
	"""Switch IsEnabled, and ScreenReaderEnabled where the launcher has it, off, as a desktop has them
	before any screen reader starts."""
	for name in ("IsEnabled", "ScreenReaderEnabled"):
		if accessibilityStatus(name) is not None:
			_status("Set", f"string:{name}", "variant:boolean:false")
	assert accessibilityStatus() is False, "the accessibility status could not be switched off"


def _start(
	desktop: DesktopSession,
	command: list[str],
	*,
	window: str,
	logName: str,
) -> webSession.FirefoxProcess:
	"""Start command in a process group of its own and wait until it has a window named window on the
	accessibility desktop, then give that window the keyboard."""
	logPath = desktop.directory / logName
	with open(logPath, "w") as log:
		process = webSession.FirefoxProcess(
			command,
			stdin=subprocess.DEVNULL,
			stdout=log,
			stderr=subprocess.STDOUT,
			env=plainEnvironment(),
		)
	desktop._apps.append(process)
	started = time.monotonic()
	names: list[str] = []
	while True:
		app = webSession._application(process)
		names = webSession._windowNames(app) if app is not None else []
		if window in names:
			process.shown = time.monotonic()
			break
		if process.firefoxHasExited() or time.monotonic() - started >= WINDOW_TIMEOUT:
			raise AssertionError(
				f"{command[0]} showed no window {window!r} within {WINDOW_TIMEOUT} s, "
				f"{harness.describeExit(process.returncode)}; its windows: {names}\n"
				f"{logPath.read_text(errors='replace')[-3000:]}",
			)
		time.sleep(0.1)
	desktopSession.activate(window, timeout=ACTIVATE_TIMEOUT)
	process.window = window
	return process


def startChromium(desktop: DesktopSession, page: str) -> webSession.FirefoxProcess:
	"""Start Chromium on page, a name under webSession.PAGES or an address, with a fresh profile, and
	wait until its window, named after the page's title, is on the accessibility desktop and has the
	keyboard."""
	profile = Path(tempfile.mkdtemp(prefix="chromium", dir=desktop.directory))
	title = webSession._pageTitle(page)
	process = _start(
		desktop,
		["chromium", f"--user-data-dir={profile}", *CHROMIUM_SWITCHES, webSession.pageURL(page)],
		window=f"{title}{CHROMIUM_TITLE_SUFFIX}",
		logName=f"{profile.name}.log",
	)
	process.profile = profile
	return process


def startElectron(desktop: DesktopSession, app: str, *, window: str) -> webSession.FirefoxProcess:
	"""Start the Electron program in the fixtures directory app, and wait until its window named window
	is on the accessibility desktop and has the keyboard."""
	directory = ELECTRON_APPS / app
	assert (directory / "package.json").is_file(), f"no Electron program {app} in {ELECTRON_APPS}"
	profile = Path(tempfile.mkdtemp(prefix="electron", dir=desktop.directory))
	process = _start(
		desktop,
		["electron", *ELECTRON_SWITCHES, f"--user-data-dir={profile}", str(directory)],
		window=window,
		logName=f"{profile.name}.log",
	)
	process.profile = profile
	return process
