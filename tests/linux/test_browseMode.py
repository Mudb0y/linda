"""Browse mode in Firefox: NVDA reads a web page through its own virtual buffer, built by the engine's
renderer into NVDA's buffer storage, with every key pressed on the rig's display.

BASEDREADER_VBUF_LIB=ROOT/engine/target/vbuf/libnvdaVbuf.so \
BASEDREADER_VBUF_ENGINE=ROOT/engine/target/release/libbre_vbuf.so \
BASEDREADER_RIG_WM=marco nix develop . -c tests/linux/rig python3 -m pytest -q tests/linux/test_browseMode.py

The two libraries are built with:

	nix develop . -c bash engine/vbuf/build.sh nvdaHelper engine/target/vbuf/libnvdaVbuf.so
	cd engine && nix develop .. -c cargo build --release --offline -p basedreader-vbufengine
"""

import json
import os
import shutil
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import desktopSession
import harness
import pytest
from desktopSession import DesktopSession

WEB_PAGES = harness.HERE / "fixtures" / "web"
SESSION_CONFIG = harness.HERE / "fixtures" / "sessionConfig" / "nvda.ini"
FIREFOX_PREFS = harness.HERE / "bench" / "user.js"
LIBRARY_VARIABLES = ("BASEDREADER_VBUF_LIB", "BASEDREADER_VBUF_ENGINE")
#: Seconds for Firefox to show its window; a first start with a fresh profile takes several.
FIREFOX_BOUND = 60.0
#: Seconds from NVDA's start to the buffer being built.
BUFFER_BOUND = 60.0
#: Seconds for anything a key starts to settle before the next key.
SETTLE = 1.0


def requireBrowseLibraries() -> None:
	missing = [name for name in LIBRARY_VARIABLES if not os.path.isfile(os.environ.get(name, ""))]
	if missing:
		pytest.skip(f"{' and '.join(missing)} must name the built browse-mode libraries")


@pytest.fixture
def desktop(tmp_path: Path) -> Iterator[DesktopSession]:
	with DesktopSession(tmp_path / "desktop") as session:
		yield session


def browseConfig(directory: Path) -> Path:
	"""The session configuration, without say all on load, so that nothing reads while keys are
	pressed; mode changes are spoken, not played."""
	config = directory / "nvda.ini"
	config.write_text(
		SESSION_CONFIG.read_text()
		+ "[virtualBuffers]\n\tautoSayAllOnPageLoad = False\n\tpassThroughAudioIndication = False\n",
	)
	return config


def firefoxWindow(title: str, process: subprocess.Popen, timeout: float = FIREFOX_BOUND) -> str:
	"""The name of Firefox's top-level window for the page title, once it is on the accessibility
	desktop."""
	deadline = time.monotonic() + timeout
	while True:
		names = desktopSession.topLevelNames()
		for name in names:
			if name.startswith(title):
				return name
		if process.poll() is not None or time.monotonic() >= deadline:
			raise AssertionError(
				f"no Firefox window for {title!r} within {timeout} s, "
				f"{harness.describeExit(process.poll())}; windows: {names}",
			)
		time.sleep(0.2)


def startFirefox(directory: Path, page: Path) -> subprocess.Popen:
	"""Firefox on page with a fresh profile, its output in directory."""
	profile = directory / "profile"
	profile.mkdir(parents=True)
	shutil.copyfile(FIREFOX_PREFS, profile / "user.js")
	with open(directory / "firefox.log", "w") as output:
		return subprocess.Popen(
			["firefox", "--no-remote", "--new-instance", "--profile", str(profile), page.as_uri()],
			stdout=output,
			stderr=subprocess.STDOUT,
			env={**os.environ, "MOZ_CRASHREPORTER_DISABLE": "1"},
		)


def stopFirefox(process: subprocess.Popen) -> None:
	if process.poll() is None:
		process.terminate()
		try:
			process.wait(10)
		except subprocess.TimeoutExpired:
			process.kill()
			process.wait()


def expect(desktop: DesktopSession, *texts: str, since: int, timeout: float = 5) -> None:
	"""desktop.expect, read again when it met a transcript line the synth was still writing."""
	for _ in range(50):
		try:
			return desktop.expect(*texts, since=since, timeout=timeout)
		except json.JSONDecodeError:
			time.sleep(0.01)
	return desktop.expect(*texts, since=since, timeout=timeout)


def step(desktop: DesktopSession, chord: str, *expected: str) -> None:
	mark = desktop.mark()
	desktop.press(chord)
	expect(desktop, *expected, since=mark)
	time.sleep(SETTLE)


def test_smoke(desktop: DesktopSession, tmp_path: Path):
	requireBrowseLibraries()
	firefox = startFirefox(tmp_path / "firefox", WEB_PAGES / "smoke.html")
	try:
		window = firefoxWindow("Basedreader browse demo", firefox)
		desktopSession.activate(window, timeout=30)
		nvda = desktop.startNVDA(browseConfig(tmp_path))
		nvda.waitForLog("Buffer rendered", BUFFER_BOUND)
		expect(desktop, "Basedreader browse demo document", since=0, timeout=BUFFER_BOUND)
		time.sleep(SETTLE)

		step(desktop, "Down", "list bullet link Contact")
		step(desktop, "Down", "main landmark heading level 1 Welcome to the demo")
		step(desktop, "h", "Second section heading level 2")

		step(desktop, "Insert+q", "Exit NVDA…")
		desktop.press("Return")
		nvda.waitForExit(10)
	finally:
		stopFirefox(firefox)
	assert nvda.process.returncode == 0, nvda.describe()
	nvda.assertClean(logNames=("nvda.log",))
