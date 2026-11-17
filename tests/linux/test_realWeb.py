"""Browse mode on a few real websites, for the gross things only: the page is read from the top soon
after it opens, quick navigation finds a heading, the log has no error and NVDA exits cleanly.

LINDA_RIG_NET=1 nix develop . -c tests/linux/rig python3 -m pytest -q tests/linux/test_realWeb.py

Each needs the internet, which the rig gives only when its caller asks; without it they skip as
expected. A site is loaded once a run, with a fresh profile, and nothing is typed into it.
"""

import time
from pathlib import Path

import network
import pytest
import webSession
from desktopSession import DesktopSession

#: The sites: long articles, plain search results, documentation with landmarks, and a page of
#: widget patterns.
SITES = {
	"englishWikipedia": "https://en.wikipedia.org/wiki/Screen_reader",
	"widgetPatterns": "https://www.w3.org/WAI/ARIA/apg/patterns/",
	"plainSearchResults": "https://html.duckduckgo.com/html/?q=screen+reader",
	"developerDocumentation": "https://developer.mozilla.org/en-US/docs/Web/Accessibility/ARIA",
}
#: Seconds from NVDA's start to its being ready, and from the window appearing to the page being read.
STARTUP_BOUND = 60.0
READ_BOUND = 15.0
#: Records the reading of the page must have given by then, the page's name among them.
READ_RECORDS = 3
#: Seconds a key has to be answered.
KEY_BOUND = 5.0
SETTLE = 1.0


@pytest.mark.parametrize("site", SITES.values(), ids=SITES.keys())
def test_siteIsReadAndNavigated(tmp_path: Path, site: str):
	network.requireNetwork()
	webSession.requireBrowseLibraries()
	with DesktopSession(tmp_path / "desktop") as desktop:
		nvda = desktop.startNVDA(webSession.BROWSE_CONFIG)
		nvda.waitForLog("NVDA initialized", STARTUP_BOUND)
		firefox = webSession.startFirefox(desktop, site, prefs={"network.dns.disabled": False})
		nvda.waitForLog("Buffer rendered", READ_BOUND)
		deadline = firefox.shown + READ_BOUND
		while len(desktop.spokenSince(0)) < READ_RECORDS + 2 and time.monotonic() < deadline:
			time.sleep(0.1)
		spoken = desktop.spokenSince(0)
		assert len(spoken) >= READ_RECORDS + 2, f"the page was not read within {READ_BOUND} s: {spoken}"
		desktop.press("Control_L")
		time.sleep(SETTLE)
		mark = desktop.mark()
		desktop.press("control+Home")
		time.sleep(SETTLE)
		desktop.press("h")
		deadline = time.monotonic() + KEY_BOUND
		while (
			not any("heading" in text for text in desktop.spokenSince(mark)) and time.monotonic() < deadline
		):
			time.sleep(0.1)
		heard = desktop.spokenSince(mark)
		assert any("heading level" in text for text in heard), f"h found no heading: {heard}"
		mark = desktop.mark()
		desktop.press("Insert+q")
		desktop.expect("Exit NVDA…", since=mark, timeout=KEY_BOUND)
		time.sleep(SETTLE)
		desktop.press("Return")
		nvda.waitForExit(10)
		assert nvda.process.returncode == 0, nvda.describe()
		nvda.assertClean(logNames=("nvda.log",))
