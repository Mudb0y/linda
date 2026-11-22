"""Chromium's own menus: a page's context menu, the three-dot menu and one of its submenus, each item
spoken as the arrows reach it (issue 12), and browse mode's keys working again once Escape has closed
the context menu.

LINDA_RIG_WM=marco nix develop . -c tests/linux/rig python3 -m pytest -q tests/linux/test_chromiumMenus.py

Chromium draws these menus in its own toolkit, which marks the item the arrows reach selected and
focused and sends only the selection. It highlights no item as a menu opens; the first Down Arrow
highlights the first.
"""

from collections.abc import Iterator

import chromiumSession
import pytest
import webSession
from desktopSession import DesktopSession
from test_browseUsable import SETTLE, STARTUP_BOUND, TIMED_SPEECH, step, stopReading
from test_chromium import Session


@pytest.fixture(scope="module")
def session(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Session]:
	webSession.requireBrowseLibraries()
	directory = tmp_path_factory.mktemp("chromiumMenus")
	with DesktopSession(directory / "desktop") as desktop:
		chromiumSession.clearAccessibilityStatus()
		nvda = desktop.startNVDA(webSession.BROWSE_CONFIG, env=TIMED_SPEECH)
		nvda.waitForLog("NVDA initialized", STARTUP_BOUND)
		browser = chromiumSession.startChromium(desktop, "usable.html")
		session = Session(desktop, nvda, browser, directory)
		desktop.expect("heading level 1 Welcome", since=0, timeout=STARTUP_BOUND)
		stopReading(session)
		yield session


@pytest.fixture(autouse=True)
def recoverAfterAFailure(request: pytest.FixtureRequest, session: Session) -> Iterator[None]:
	failed = request.session.testsfailed
	yield
	if request.session.testsfailed > failed and session.nvda.process.poll() is None:
		session.desktop.press("Escape", "Escape", "Escape", gap=SETTLE)


def test_contextMenu(session: Session):
	step(session, "Menu", "frame")
	step(session, "Down", "Back Alt plus Left Arrow…")
	step(session, "Down", "Forward Alt plus Right Arrow…")
	step(session, "Down", "Reload Ctrl plus R…")
	step(session, "Up", "Forward Alt plus Right Arrow…")
	step(session, "Escape", "Usable page document")
	step(session, "ctrl+Home", "heading level 1 Welcome")
	step(session, "h", "News heading level 2")


def test_threeDotMenuAndASubmenu(session: Session):
	step(session, "alt+f", "frame")
	step(session, "Down", "New tab Ctrl plus T…")
	step(session, "Down", "New window Ctrl plus N…")
	step(session, "Down", "New Incognito window Ctrl plus Shift plus N…")
	step(session, "Down", "Your Chromium…")
	step(session, "Right", "Customize your Chromium…")
	step(session, "Down", "Close this profile…")
	step(session, "Up", "Customize your Chromium…")
	step(session, "Escape", "Your Chromium…")
	step(session, "Down", "Passwords and autofill…")
	# The item the menu is taken down from was said as unavailable.
	mark = step(session, "Escape", "Usable page document")
	assert "unavailable" not in session.desktop.spokenSince(mark)
