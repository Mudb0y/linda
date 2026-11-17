"""Firefox in the rig for browse-mode tests: it starts on a page with a fresh profile, its document
appears on the accessibility desktop, keys typed into the keys page are logged where a test can read
them, and nothing of it is left once the session closes.

BASEDREADER_RIG_WM=marco nix develop . -c tests/linux/rig python3 -m pytest tests/linux/test_webSession.py
"""

import time
from collections.abc import Iterator
from pathlib import Path

import pytest
import webSession
from desktopSession import DesktopSession

#: Seconds a document has to appear on the accessibility desktop once Firefox has started.
DOCUMENT_BOUND = 30.0
#: Seconds a typed key has to reach the page's log.
KEY_LOG_BOUND = 5.0


@pytest.fixture
def desktop(tmp_path: Path) -> Iterator[DesktopSession]:
	with DesktopSession(tmp_path / "desktop") as session:
		yield session


def _firefoxProcesses() -> list[str]:
	"""Every running process in the rig whose program is Firefox's, each as its ID and command line."""
	found = []
	for entry in Path("/proc").iterdir():
		if not entry.name.isdecimal():
			continue
		try:
			program = (entry / "exe").readlink().name
			state = (entry / "stat").read_text().rsplit(")", 1)[1].split()[0]
			command = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
		except OSError:
			continue
		if "firefox" in program.lower() and state not in "ZX":
			found.append(f"{entry.name}: {command}")
	return found


def test_documentAppearsWithAFreshProfile(desktop: DesktopSession):
	started = time.monotonic()
	firefox = webSession.startFirefox(desktop, "basic.html")
	webSession.waitForDocument("Basic page", timeout=DOCUMENT_BOUND - (time.monotonic() - started))
	assert firefox.profile.parent == desktop.directory
	assert (firefox.profile / "user.js").read_text() == webSession.USER_JS.read_text()
	assert firefox.window.startswith("Basic page")


def test_keyTypedIntoTheKeysPageIsLogged(desktop: DesktopSession):
	webSession.startFirefox(desktop, "keys.html")
	webSession.waitForDocument("Keys page")
	assert webSession.pageKeyLog() == []
	desktop.press("x", "shift+Tab")
	deadline = time.monotonic() + KEY_LOG_BOUND
	while (log := webSession.pageKeyLog()) != ["x", "Shift+Tab"]:
		assert time.monotonic() < deadline, f"the page logged {log}"
		time.sleep(0.1)


def test_noFirefoxProcessOutlivesTheSession(tmp_path: Path):
	with DesktopSession(tmp_path / "desktop") as desktop:
		firefox = webSession.startFirefox(desktop, "usable.html", prefs={"browser.tabs.warnOnClose": False})
		webSession.waitForDocument("Usable page")
		started = firefox.liveProcesses()
		# One for the browser and at least one for the page's content.
		assert len(started) >= 2, _firefoxProcesses()
	assert firefox.liveProcesses() == []
	assert _firefoxProcesses() == []
