"""Experiment E13: a browser whose accessibility bus is reached through xdg-dbus-proxy, as Flatpak runs
one, on the Wayland route. Its name on the bus is the proxy's connection, so the process the bus records
for it is the proxy's, not the browser's. The capture feed and NVDA's own objects both take a program's
process from the bus's record, so they must name the same one, and browse mode's letters must then be
Linda's in the page.

Under Mutter and sway, each with and without Xwayland, through tests/linux/wayland-gate SCRATCH, or one
of them:

	LINDA_RIG_COMPOSITOR=sway nix develop . -c tests/linux/rig python3 -m pytest -q -s tests/linux/test_waylandProxyApp.py
"""

import json
import os
import time
from collections.abc import Iterator
from pathlib import Path

import chromiumSession
import harness
import pytest
import test_browseKeys as browseKeys
import webSession
from keydStandIn import code
from test_sandboxedBrowser import sandboxed
from test_waylandBrowse import USABLE_WALK, lettersKept, processNamed, sessionName, waitForTheWholePage
from test_waylandKeys import MARKER, WaylandLinda, socketDir, waylandLinda  # noqa: F401

pytestmark = pytest.mark.rigSession("wayland")

#: Seconds for Linda's focus and the feed's foreground to agree that the page has the keyboard.
AGREE_BOUND = 10.0

#: Records, every 200 ms as JSON lines in the file LINDA_IDENTITY_PROBE names, the process NVDA's focus
#: and its application hold and the one the capture feed has for its foreground, with the capture
#: state the kernel source reads.
IDENTITY_PROBE = """\
import json
import os
import time

import api
import globalPluginHandler
import wx
from linuxPlatform import inputHook

PATH = os.environ.get("LINDA_IDENTITY_PROBE")


class GlobalPlugin(globalPluginHandler.GlobalPlugin):
	def __init__(self):
		super().__init__()
		if not PATH:
			return
		self.timer = wx.Timer()
		self.timer.Bind(wx.EVT_TIMER, self.poll)
		self.timer.Start(200)

	def poll(self, evt):
		feed = getattr(getattr(inputHook._source, "_environment", None), "feed", None)
		if feed is None:
			return
		focus = api.getFocusObject()
		foreground = feed.foreground
		state = feed.captureState
		line = {
			"t": time.monotonic(),
			"focus": focus.processID if focus is not None else None,
			"appModule": focus.appModule.processID if focus is not None and focus.appModule else None,
			"feed": foreground[2] if foreground is not None else None,
			"mode": state.mode,
			"windowProcess": state.windowProcess,
			"processID": state.processID,
			"focused": state.focused,
		}
		with open(PATH, "a", encoding="utf-8") as out:
			out.write(json.dumps(line) + "\\n")
"""


def startProxiedBrowser(session: WaylandLinda, program: str) -> webSession.FirefoxProcess:
	"""The browser on the usable page as a Wayland client, its accessibility bus reached only through
	xdg-dbus-proxy with Flatpak's filter."""
	desktop = session.desktop
	profile = desktop.directory / f"{program}{len(desktop._apps)}"
	profile.mkdir()
	title = webSession._pageTitle("usable.html")
	if program == "chromium":
		command = [
			"chromium",
			f"--user-data-dir={profile}",
			*chromiumSession.CHROMIUM_SWITCHES,
			*chromiumSession.WAYLAND_SWITCHES,
			webSession.pageURL("usable.html"),
		]
		window = f"{title}{chromiumSession.CHROMIUM_TITLE_SUFFIX}"
	else:
		(profile / "user.js").write_text(webSession.USER_JS.read_text())
		command = [
			"env",
			"-u",
			"DISPLAY",
			"-u",
			"XAUTHORITY",
			"MOZ_ENABLE_WAYLAND=1",
			"MOZ_CRASHREPORTER_DISABLE=1",
			"firefox",
			"--no-remote",
			"--new-instance",
			"--profile",
			str(profile),
			webSession.pageURL("usable.html"),
		]
		window = f"{title} — Mozilla Firefox"
	# Its own runtime directory too, as Flatpak gives, would hide the compositor's socket in it.
	return chromiumSession._start(
		desktop,
		sandboxed(command, privateRuntime=False, proxied=True),
		window=window,
		logName=f"{profile.name}.log",
	)


def identities(path: Path) -> list[dict]:
	if not path.exists():
		return []
	return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


@pytest.fixture
def session(tmp_path: Path, socketDir: Path) -> Iterator[WaylandLinda]:  # noqa: F811
	webSession.requireBrowseLibraries()
	with waylandLinda(tmp_path, socketDir, probe=False) as found:
		yield found


@pytest.mark.parametrize("program", ["chromium", "firefox"])
def test_aProxiedBrowsersProcessIsTheSameToTheFeedAndToNVDA(
	session: WaylandLinda, tmp_path: Path, program: str
):
	probe = tmp_path / "identity.jsonl"
	chromiumSession.clearAccessibilityStatus()
	nvda = session.start(
		browseKeys.browseConfig(tmp_path),
		plugins={"browseKeysProbe": browseKeys.PROBE_PLUGIN, "identityProbe": IDENTITY_PROBE},
		env={"LINDA_IDENTITY_PROBE": str(probe)},
	)
	startProxiedBrowser(session, program)
	nvda.waitForLog("Buffer rendered", browseKeys.BUFFER_BOUND)
	browseKeys.waitForCapture(nvda, "browse", timeout=browseKeys.BUFFER_BOUND)
	deadline = time.monotonic() + AGREE_BOUND
	while not (agreed := [r for r in identities(probe) if r["mode"] == "browse" and r["focused"]]):
		assert time.monotonic() < deadline, identities(probe)[-5:]
		time.sleep(0.2)
	record = agreed[-1]
	proxies = processNamed("xdg-dbus-proxy")
	print(f"{sessionName()}: E13, {program}: {record}; xdg-dbus-proxy processes {proxies}")
	assert record["feed"] == record["focus"] == record["appModule"], record
	assert record["windowProcess"] == record["processID"] == record["feed"], record
	# Both have the proxy's process, which the bus records for the browser's connection.
	assert record["feed"] in proxies, (record, proxies)
	waitForTheWholePage(session, USABLE_WALK[0])
	lettersKept(session, USABLE_WALK)
	lettersKept(session, (USABLE_WALK[0], ("e", "Your name edit")))
	session.step("Return", "Focus mode")
	browseKeys.waitForCapture(nvda, "focus")
	passed = session.passedMark()
	session.desktop.typeText(MARKER)
	time.sleep(browseKeys.SETTLE)
	assert session.pressesPassed(passed) == [code(letter) for letter in MARKER]
	session.step("Escape", "Browse mode")
	session.quit()
	assert os.path.getsize(probe) > 0
	assert harness.nvdaLogErrorRecords(nvda.text("nvda.log")) == []
