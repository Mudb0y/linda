"""NVDA's add-on store is off until the user turns it on, and asks a store of the test's own.

nix develop . -c tests/linux/rig python3 -m pytest -q tests/linux/test_addonStore.py

Each test points the store at a small HTTP server in the test process, on the rig's loopback, which
serves a few add-ons in the store's own format and records every request. Off, the window that opens
from the Tools menu is called Add-ons and manages the installed add-ons, and the server hears
nothing; on, it is the NVDA Add-on Store.
"""

import json
import textwrap
import threading
import time
import zipfile
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import desktopSession
import harness
import pytest
from desktopSession import DesktopSession

SESSION_CONFIG = harness.HERE / "fixtures" / "sessionConfig" / "nvda.ini"
STARTUP_BOUND = 60.0
#: Seconds to leave for a request that may yet be made.
SETTLE = 3.0

GTK = ("gtkFixture", "Fixture window")
AT_START = ("Fixture window", "Press me button Does nothing Alt plus P")
WARNING = "Add-on Store Warning dialog Add-ons are created by the NVDA community…"
WARNING_CHECK_BOX = "Don't show this message again check box not checked Alt plus D"
STORE_NAME = "NVDA Add-on Store"
SWITCH_OFF = f"Enable the {STORE_NAME} check box not checked…"
SWITCH_ON = f"Enable the {STORE_NAME} check box checked…"
TURN_ON_WARNING = f"{STORE_NAME} dialog The {STORE_NAME} holds add-ons written for NVDA on Windows.…"

#: Brings up NVDA's configuration as core.main does, which NVDA's modules need before the gui can be
#: imported.
STARTUP = """
import config
from NVDAState import WritePaths

WritePaths.configDir = config.getUserDefaultConfigPath()
config.initConfigPath()
config.initialize()
import NVDAHelper
"""

#: STARTUP, and the add-on store's data manager as core.main brings it up, with the store at the
#: address the test substitutes for STORE_URL and its switch at ENABLED.
CORE_PRELUDE = (
	STARTUP
	+ """
config.conf["addonStore"]["baseServerURL"] = STORE_URL
config.conf["addonStore"]["automaticUpdates"] = "notify"
config.conf["addonStore"]["enabled"] = ENABLED
import addonHandler
from addonStore import dataManager

dataManager.initialize()
addonHandler.initialize()
# It takes the data manager as the module holds it when imported, as NVDA's gui does.
from gui.addonStoreGui.controls.messageDialogs import UpdatableAddonsDialog
manager = dataManager.addonDataManager
"""
)

STORE_ENTRY = {
	"addonId": "storeProbe",
	"displayName": "Store probe",
	"description": "An add-on the test's store lists",
	"publisher": "A test",
	"channel": "stable",
	"addonVersionName": "1.0.0",
	"addonVersionNumber": {"major": 1, "minor": 0, "patch": 0},
	"homepage": None,
	"changelog": None,
	"license": "GPL v2",
	"licenseURL": None,
	"sourceURL": "http://127.0.0.1/source",
	"URL": "http://127.0.0.1/storeProbe.nvda-addon",
	"sha256": "0" * 64,
	"minNVDAVersion": {"major": 2024, "minor": 1, "patch": 0},
	"lastTestedVersion": {"major": 2026, "minor": 3, "patch": 0},
	"legacy": False,
}


class StoreServer:
	"""The add-on store's server: its cache hash, and one add-on in every list."""

	def __init__(self) -> None:
		self.requests: list[str] = []
		requests = self.requests

		class Handler(BaseHTTPRequestHandler):
			def do_GET(self) -> None:
				requests.append(self.path)
				if self.path == "/cacheHash.json":
					body = json.dumps("testHash")
				elif self.path.endswith(".json"):
					body = json.dumps([STORE_ENTRY])
				else:
					self.send_error(404)
					return
				data = body.encode()
				self.send_response(200)
				self.send_header("Content-Type", "application/json")
				self.send_header("Content-Length", str(len(data)))
				self.end_headers()
				self.wfile.write(data)

			def log_message(self, format: str, *args) -> None:
				pass

		self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
		self.url = f"http://127.0.0.1:{self._server.server_address[1]}"
		self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
		self._thread.start()

	def close(self) -> None:
		self._server.shutdown()
		self._server.server_close()
		self._thread.join()


@pytest.fixture
def store() -> Iterator[StoreServer]:
	server = StoreServer()
	yield server
	server.close()


@pytest.fixture
def desktop(tmp_path: Path) -> Iterator[DesktopSession]:
	with DesktopSession(tmp_path / "desktop") as session:
		yield session


def runStarted(tmp_path: Path, body: str, prelude: str = STARTUP) -> harness.ChildResult:
	return harness.runChild(
		tmp_path,
		prelude + textwrap.dedent(body),
		prepare="appDir",
		env={
			"XDG_CONFIG_HOME": str(tmp_path / "xdgConfig"),
			"XDG_CACHE_HOME": str(tmp_path / "xdgCache"),
			"XDG_STATE_HOME": str(tmp_path / "xdgState"),
		},
	)


def runWithStore(tmp_path: Path, store: StoreServer, enabled: bool, body: str) -> harness.ChildResult:
	prelude = CORE_PRELUDE.replace("STORE_URL", repr(store.url)).replace("ENABLED", repr(enabled))
	return runStarted(tmp_path, body, prelude)


def test_offNothingAsksTheStore(tmp_path: Path, store: StoreServer):
	done = runWithStore(
		tmp_path,
		store,
		False,
		"""
		started = manager._initialiseAvailableAddonsThread.ident is not None
		UpdatableAddonsDialog._checkForUpdatableAddons()
		listed = [len(listing) for listing in manager.getLatestCompatibleAddons().values()]
		listed += [len(listing) for listing in manager.getLatestAddons().values()]
		dataManager.terminate()
		result = {"started": started, "listed": sum(listed)}
		""",
	)
	done.assertClean()
	assert done.value == {"started": False, "listed": 0}
	assert store.requests == []


def test_onTheStoreIsAskedAtStartAndDaily(tmp_path: Path, store: StoreServer):
	done = runWithStore(
		tmp_path,
		store,
		True,
		"""
		manager._initialiseAvailableAddonsThread.join(30)
		UpdatableAddonsDialog._checkForUpdatableAddons()
		listed = [len(listing) for listing in manager.getLatestCompatibleAddons().values()]
		dataManager.terminate()
		result = {"listed": sum(listed)}
		""",
	)
	done.assertClean()
	assert done.value == {"listed": 1}
	# At start, by the daily job, and by the listing: each asks for the cache hash.
	assert store.requests.count("/cacheHash.json") == 3, store.requests


def test_theStoreIsNamedNvdasInEveryLanguage(tmp_path: Path):
	done = harness.runChild(
		tmp_path,
		"""
		import languageHandler
		from linuxPlatform import nvdaAddonStore

		result = {}
		for language in ("en", "pl", "de", "fr"):
			languageHandler.setLanguage(language)
			result[language] = {
				"store": nvdaAddonStore.storeName(),
				"translated": pgettext("addonStore", "Add-on Store"),
				"renamed": _("Exit NVDA"),
			}
		""",
		prepare="appDir",
	)
	done.assertClean()
	for language, names in done.value.items():
		assert names["store"] == f"NVDA {names['translated']}", language
		assert "Linda" in names["renamed"] and "NVDA" not in names["renamed"], language
	assert done.value["en"]["store"] == "NVDA Add-on Store"


def sessionConfig(directory: Path, store: StoreServer, *, enabled: bool | None = None) -> Path:
	"""The session configuration with the store at store's address and automatic updates left at
	upstream's default, which has the daily job ask the store; enabled, when given, sets the switch."""
	lines = []
	for line in SESSION_CONFIG.read_text(encoding="utf-8").splitlines():
		stripped = line.strip()
		if stripped.startswith("automaticUpdates"):
			continue
		if stripped.startswith("baseServerURL"):
			line = f"\tbaseServerURL = {store.url}"
			if enabled is not None:
				line += f"\n\tenabled = {enabled}"
		lines.append(line)
	directory.mkdir(parents=True, exist_ok=True)
	path = directory / "nvda.ini"
	path.write_text("\n".join(lines) + "\n", encoding="utf-8")
	return path


def start(desktop: DesktopSession, config: Path) -> harness.Launch:
	app = desktop.startApp(desktop.fixture(GTK[0]), title=GTK[1])
	assert app.stdout.readline() == "ready\n"
	desktopSession.activate(GTK[1])
	nvda = desktop.startNVDA(config)
	desktop.expect(*AT_START, since=0, timeout=STARTUP_BOUND)
	return nvda


def step(desktop: DesktopSession, chord: str, *expected: str, timeout: float = 5) -> int:
	"""Press chord and wait for expected, in order; return the mark taken before the key."""
	mark = desktop.mark()
	desktop.press(chord)
	desktop.expect(*expected, since=mark, timeout=timeout)
	return mark


def readUntil(desktop: DesktopSession, chord: str, start: str, presses: int = 15) -> None:
	"""Press chord until NVDA says something that starts with start."""
	for _ in range(presses):
		mark = desktop.mark()
		desktop.press(chord)
		time.sleep(0.5)
		if any(text.startswith(start) for text in desktop.spokenSince(mark)):
			return
	raise AssertionError(f"never heard {start!r}; last heard {desktop.spokenSince(0)[-10:]}")


def openToolsMenu(desktop: DesktopSession) -> None:
	step(desktop, "Insert+n", "Preferences sub Menu P 1 of 9")
	step(desktop, "t", "View log L 1 of 6")


def openStoreCategory(desktop: DesktopSession) -> None:
	"""Open the settings dialog and arrow down its categories to the store's."""
	step(desktop, "Insert+n", "Preferences sub Menu P 1 of 9")
	step(desktop, "Return", "Settings... S 1 of 4")
	step(desktop, "Return", "Categories: list…")
	for _ in range(30):
		mark = step(desktop, "Down", "…")
		if any(text.startswith(STORE_NAME) for text in desktop.spokenSince(mark)):
			return
	raise AssertionError(f"no category {STORE_NAME}")


def savedConfig(nvda: harness.Launch) -> str:
	return (nvda.directory / "config" / "nvda.ini").read_text(encoding="utf-8")


def assertExitedCleanly(nvda: harness.Launch) -> None:
	assert nvda.process.returncode == 0, nvda.describe()
	nvda.assertClean(logNames=("nvda.log",))


def test_offTheStoreIsAskedNothingAndTheWindowManagesInstalledAddOns(
	desktop: DesktopSession, store: StoreServer, tmp_path: Path
):
	config = sessionConfig(tmp_path / "config", store).parent
	makeAddOn(config / "addons" / "installedProbe", "installedProbe", summary="Installed probe")
	bundle = tmp_path / "bundled.nvda-addon"
	makeAddOn(tmp_path / "bundled", "bundled")
	with zipfile.ZipFile(bundle, "w") as archive:
		for path in (tmp_path / "bundled").rglob("*"):
			archive.write(path, path.relative_to(tmp_path / "bundled"))
	nvda = start(desktop, config)
	openToolsMenu(desktop)
	# NVDA's own warning about add-ons, which is not about the store.
	step(desktop, "a", WARNING, WARNING_CHECK_BOX)
	step(desktop, "Return", "Add-ons - Installed add-ons (All) dialog…")
	# The details of the installed add-on the list selects, which was running before Linda read
	# platforms and so stays enabled.
	desktop.press("Home")
	step(desktop, "Tab", "Installed probe panel", "Installed probe edit read only Does nothing")
	readUntil(desktop, "Tab", "Other Details:")
	readUntil(desktop, "Down", "Platforms: Made for Windows")
	# Installing from a file stays, and asks first about an add-on made for Windows.
	step(desktop, "alt+x", "Choose Add-on Package File dialog…")
	desktop.press("control+l")
	desktop.typeText(str(bundle))
	step(
		desktop,
		"Return",
		"Install add-on dialog Warning: bundled 1.0 does not say that it supports Linux. It was written"
		" for NVDA on Windows and may not work.…",
		timeout=10,
	)
	step(desktop, "alt+n", "Add-ons - Installed add-ons (All) dialog…")
	step(desktop, "Escape", *AT_START)
	time.sleep(SETTLE)
	assert store.requests == []
	desktop.close()
	assertExitedCleanly(nvda)
	assert store.requests == []


def test_switchWarnsAndOnTheStoreIsAsked(desktop: DesktopSession, store: StoreServer, tmp_path: Path):
	nvda = start(desktop, sessionConfig(tmp_path / "config", store))
	openStoreCategory(desktop)
	step(desktop, "Tab", SWITCH_OFF)
	# Answering no leaves the store off, and the controls that need it out of reach.
	step(desktop, "space", TURN_ON_WARNING)
	step(desktop, "alt+n", SWITCH_OFF)
	mark = desktop.mark()
	desktop.press("Tab")
	time.sleep(1)
	assert not [text for text in desktop.spokenSince(mark) if text.startswith("Automatic updates")]
	desktop.press("shift+Tab")
	desktop.expect(SWITCH_OFF, since=mark)
	step(desktop, "space", TURN_ON_WARNING)
	step(desktop, "alt+y", SWITCH_ON)
	step(desktop, "Tab", "Automatic updates: combo box Notify…")
	step(desktop, "Return", *AT_START)
	assert store.requests == []

	openToolsMenu(desktop)
	step(desktop, "n", WARNING, WARNING_CHECK_BOX)
	# Its first add-on is chosen and its details filled while the window is still being shown, which
	# crashed GTK's text view while the details were frozen.
	step(desktop, "Return", f"{STORE_NAME} - …")
	deadline = time.monotonic() + 10
	while "/cacheHash.json" not in store.requests:
		assert time.monotonic() < deadline, store.requests
		time.sleep(0.1)
	step(desktop, "Escape", *AT_START)
	desktop.close()
	assertExitedCleanly(nvda)
	assert "enabled = True" in savedConfig(nvda)


BOOT_CONFIG = harness.HERE / "fixtures" / "bootConfig" / "nvda.ini"
BOOT_SENTENCE = "the boot probe has started"


def makeAddOn(directory: Path, name: str, platforms: str | None = None, summary: str | None = None) -> None:
	"""An add-on whose global plugin, once loaded, leaves a file named ran-NAME in the configuration
	directory above it."""
	directory.mkdir(parents=True)
	manifest = (
		f"name = {name}\nsummary = {summary or name}\ndescription = Does nothing\nauthor = A test\nversion = 1.0\n"
		"minimumNVDAVersion = 2024.1\nlastTestedNVDAVersion = 2026.3\n"
	)
	if platforms is not None:
		manifest += f"platforms = {platforms}\n"
	(directory / "manifest.ini").write_text(manifest, encoding="utf-8")
	(directory / "globalPlugins").mkdir()
	(directory / "globalPlugins" / f"{name}.py").write_text(
		"import os\n"
		"import globalPluginHandler\n\n"
		f"open(os.path.join(os.path.dirname(__file__), '..', '..', '..', 'ran-{name}'), 'w').close()\n\n\n"
		"class GlobalPlugin(globalPluginHandler.GlobalPlugin):\n"
		"\tpass\n",
		encoding="utf-8",
	)


def bootOnce(launches, name: str, configDir: Path) -> set[str]:
	"""Start NVDA with configDir until the boot probe has spoken, and return the add-ons that ran."""
	for ran in configDir.glob("ran-*"):
		ran.unlink()
	directory = launches.directory / name
	nvda = launches(
		name,
		"-c",
		str(configDir),
		"-f",
		str(directory / "nvda.log"),
		"--debug-logging",
		env={"LINDA_BOOT_PROBE": "exit"},
	)
	nvda.waitForSpeech(BOOT_SENTENCE, STARTUP_BOUND)
	assert nvda.waitForExit(20) == 0, nvda.describe()
	nvda.assertClean(logNames=("nvda.log",))
	return {ran.name.removeprefix("ran-") for ran in configDir.glob("ran-*")}


def test_addOnsInstalledBeforePlatformsStayAndNewOnesWait(launches, tmp_path: Path):
	configDir = tmp_path / "config"
	configDir.mkdir()
	(configDir / "nvda.ini").write_text(BOOT_CONFIG.read_text(encoding="utf-8"), encoding="utf-8")
	addons = configDir / "addons"
	# Installed and running before Linda read the platforms an add-on supports.
	makeAddOn(addons / "inUse", "inUse")
	makeAddOn(addons / "turnedOff", "turnedOff")
	stateFile = configDir / "addonsState.json"
	stateFile.write_text(
		json.dumps({"disabledAddons": ["turnedOff"], "backCompatToAPIVersion": [2026, 1, 0]}),
		encoding="utf-8",
	)

	assert bootOnce(launches, "upgrade", configDir) == {"inUse"}
	state = json.loads(stateFile.read_text(encoding="utf-8"))
	assert state["platformsRecorded"] is True
	# Names are kept in lower case.
	assert state["overrideCompatibility"] == ["inuse"]
	assert state["blocked"] == ["turnedoff"]

	# Installed afterwards, as an install from a file or the store leaves them for the next start.
	makeAddOn(addons / "newForWindows.pendingInstall", "newForWindows")
	makeAddOn(addons / "forLinux.pendingInstall", "forLinux", platforms="Windows, LINUX, haiku")
	state["pendingInstallsSet"] = ["newForWindows", "forLinux"]
	stateFile.write_text(json.dumps(state), encoding="utf-8")

	assert bootOnce(launches, "installed", configDir) == {"inUse", "forLinux"}
	state = json.loads(stateFile.read_text(encoding="utf-8"))
	assert sorted(state["blocked"]) == ["newforwindows", "turnedoff"]
	assert state["overrideCompatibility"] == ["inuse"]
	assert state["pendingInstallsSet"] == []


def test_platformsAreReadFromTheManifestAndDecideCompatibility(tmp_path: Path):
	addons = tmp_path / "addons"
	for name, platforms in (
		("unsaid", None),
		("empty", ""),
		("linuxOnly", "linux"),
		("both", "Windows, LINUX, haiku, linux"),
		("elsewhere", "haiku"),
	):
		makeAddOn(addons / name, name, platforms)
	done = runStarted(
		tmp_path,
		f"""
		import addonHandler
		import languageHandler
		from addonHandler import addonVersionCheck
		from addonStore.models.addon import _createGUIModelFromManifest
		from gui.addonStoreGui.controls.details import _platformsText

		languageHandler.setLanguage("en")
		result = {{}}
		for name in ("unsaid", "empty", "linuxOnly", "both", "elsewhere"):
			addon = addonHandler.Addon({str(addons)!r} + "/" + name)
			model = _createGUIModelFromManifest(addon)
			result[name] = {{
				"manifest": list(addon.manifest.platforms),
				"addon": list(addon.platforms),
				"model": list(model.platforms),
				"supported": addon.supportsCurrentPlatform,
				"onWindows": addonVersionCheck.supportsPlatform(addon, addonVersionCheck.WINDOWS),
				"compatible": addon.isCompatible,
				"modelCompatible": model.isCompatible,
				"canOverride": addon.canOverrideCompatibility,
				"reason": addon.getIncompatibleReason(),
				"details": _platformsText(model),
			}}
		""",
	)
	done.assertClean()
	found = done.value
	assert found["unsaid"]["manifest"] == found["empty"]["manifest"] == ["windows"]
	assert found["linuxOnly"]["manifest"] == ["linux"]
	assert found["both"]["manifest"] == ["windows", "linux", "haiku"]
	for name, addon in found.items():
		assert addon["addon"] == addon["model"] == addon["manifest"], name
		supported = name in ("linuxOnly", "both")
		assert addon["supported"] is addon["compatible"] is addon["modelCompatible"] is supported, name
		assert addon["canOverride"] is not supported, name
		assert addon["onWindows"] is (name in ("unsaid", "empty", "both")), name
		if supported:
			assert addon["reason"] is None, name
			assert addon["details"] == "Supports Linux", name
	assert found["unsaid"]["details"] == "Made for Windows"
	assert found["elsewhere"]["details"] == "Made for haiku"
	assert found["unsaid"]["reason"] == (
		"This add-on does not say that it supports Linux. It was written for NVDA on Windows and may not"
		" work. You can enable it at your own risk. If Linda misbehaves or does not start, start it with"
		" linda --disable-addons and remove this add-on."
	)
