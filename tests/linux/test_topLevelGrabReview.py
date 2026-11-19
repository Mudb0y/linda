"""A fault found in review of the X source's grabs on a document's top-level window: the test fails
while the fault is there. Run under the rig with browse mode's libraries and marco:

LINDA_VBUF_LIB=ROOT/engine/target/vbuf/libnvdaVbuf.so \
LINDA_VBUF_ENGINE=ROOT/engine/target/release/libbre_vbuf.so \
LINDA_RIG_WM=marco nix develop . -c tests/linux/rig python3 -m pytest -q tests/linux/test_topLevelGrabReview.py
"""

import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import desktopSession
import pytest
import webSession
from desktopSession import PREFIX_MARK, DesktopSession
from test_browseKeys import SETTLE, quitNVDA, startPage, step, waitForCapture
from Xlib import X, XK, display
from Xlib.ext import xtest


@pytest.fixture
def desktop(tmp_path: Path) -> Iterator[DesktopSession]:
	with DesktopSession(tmp_path / "desktop") as session:
		yield session


def test_keyTakenOnTheTopLevelWindowLeavesThePagesFocusAlone(desktop: DesktopSession, tmp_path: Path):
	"""Alt+Tab from the page to another program and back, and h typed the moment marco has given the
	focus to Firefox's top-level window, before Firefox moves it to its own window inside, as a quick
	navigation key typed at once is. Linda takes h there with a grab on the top-level window, which
	has the focus; the X server tells that window it lost the focus to the grab and got it back, and
	GTK, which follows grabs for its top-level windows, tells Firefox and the page the same. With no
	key typed the page sees its window get the focus once."""
	webSession.requireBrowseLibraries()
	desktop.startApp(desktop.fixture("gtkFixture"), title="Fixture window")
	desktopSession.activate("Fixture window")
	nvda, page = startPage(desktop, tmp_path)
	step(desktop, "control+Home", "heading level 1 Keys page")
	probe = display.Display()
	top = probe.get_input_focus().focus.query_tree().parent
	hKey = probe.keysym_to_keycode(XK.XK_h)
	found = {}
	for how in ("no key", "h at once"):
		step(desktop, "alt+Tab", "Fixture window" + PREFIX_MARK)
		waitForCapture(nvda, "off")
		time.sleep(SETTLE)
		mark = desktop.mark()
		before = len(page.focusLog())
		switcher = subprocess.Popen(["xdotool", "key", "alt+Tab"])
		typedOnTop = None
		if how == "h at once":
			deadline = time.monotonic() + 3
			while getattr(probe.get_input_focus().focus, "id", 0) != top.id:
				assert time.monotonic() < deadline, "the focus never reached Firefox's top-level window"
			xtest.fake_input(probe, X.KeyPress, hKey)
			xtest.fake_input(probe, X.KeyRelease, hKey)
			probe.sync()
			typedOnTop = getattr(probe.get_input_focus().focus, "id", 0) == top.id
		switcher.wait()
		time.sleep(SETTLE * 4)
		found[how] = {
			"typedWhileTheTopLevelWindowHadTheFocus": typedOnTop,
			"pageFocus": page.focusLog()[before:],
			"pageKeys": page.log(),
			"said": desktop.spokenSince(mark),
		}
		step(desktop, "control+Home", "heading level 1 Keys page")
	probe.close()
	quitNVDA(desktop, nvda)
	assert found["no key"]["pageFocus"] == ["window-focus"], found
	atOnce = found["h at once"]
	assert atOnce["typedWhileTheTopLevelWindowHadTheFocus"], f"h came after Firefox moved the focus: {found}"
	assert atOnce["pageKeys"] == [] and "Field and link heading level 2" in atOnce["said"], found
	assert atOnce["pageFocus"] == ["window-focus"], (
		"h typed while marco had given the focus to Firefox's top-level window was Linda's, but the grab "
		f"that took it made the page's window lose the focus and get it back: {atOnce['pageFocus']}; {found}"
	)
