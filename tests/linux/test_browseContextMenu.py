"""Firefox's context menu, opened from browse mode, takes the arrows (issue 35).

Firefox says its popup menu opens only with focus:, before any item is highlighted; until NVDA takes
that for the focus, browse mode keeps the arrows and the menu cannot be reached. The keys are typed on
the rig's keyboard: Firefox opens no menu for xdotool's Shift+F10. Run under the rig:

	nix develop . -c tests/linux/rig python3 -m pytest tests/linux/test_browseContextMenu.py
"""

from pathlib import Path

import pytest
from desktopSession import DesktopSession
from test_browseKeys import desktop, keyboard, quitNVDA, startPage, stepOn, waitForCapture  # noqa: F401
from test_keys import Keyboard


@pytest.mark.parametrize(("chord", "logged"), [("Menu", "ContextMenu"), ("shift+F10", "Shift+F10")])
def test_contextMenuTakesTheArrowsFromBrowseMode(
	desktop: DesktopSession,  # noqa: F811
	keyboard: Keyboard,  # noqa: F811
	tmp_path: Path,
	chord: str,
	logged: str,
):
	nvda, page = startPage(desktop, tmp_path)
	stepOn(keyboard, desktop, chord, "menu")
	waitForCapture(nvda, "off")
	# Back and Forward are unavailable on a page opened first, so the first item the arrow reaches is
	# Reload.
	stepOn(keyboard, desktop, "Down", "Reload 3 of 4")
	stepOn(keyboard, desktop, "Escape", "Keys page document")
	waitForCapture(nvda, "browse")
	assert page.log() == [logged]
	quitNVDA(desktop, nvda)
