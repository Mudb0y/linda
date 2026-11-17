"""ui.browseableMessage, which NVDA and its add-ons use for longer messages, on the rig's display
under marco: the message opens, is read in browse mode, runs its actions and closes, as a WebKitGTK
document where wx has WebKitGTK and as text in a read-only field where it has not.

LINDA_VBUF_ENGINE=ROOT/engine/target/release/libbre_vbuf.so \
nix develop . -c tests/linux/rig python3 -m pytest -q tests/linux/test_browseableMessage.py
"""

import json
import time
from collections.abc import Iterator
from pathlib import Path

import desktopSession
import harness
import pytest
import webSession
from desktopSession import DesktopSession

#: Seconds from starting NVDA to its first words.
STARTUP_BOUND = 60.0
#: Seconds from the key to the message being read; a WebKitGTK web process takes a second to start.
OPEN_BOUND = 20.0
#: Seconds for anything a key starts to settle before the next key.
SETTLE = 0.5

GTK = ("gtkFixture", "Fixture window")
AT_START = ("Fixture window", "Press me button Does nothing Alt plus P")
HOOK_KEY = "Insert+control+shift+F9"
TITLE = "Probe message"

HTML = (
	"<h1>Message heading</h1>"
	"<p>The first paragraph.</p>"
	"<h2>Second heading</h2>"
	'<p>Press <a href="nvda-action://hello">say hello</a> to run it.</p>'
	"<p>The last paragraph.</p>"
)

#: Shows the message the command file beside it describes, as JSON: browseableMessage's arguments,
#: with sanitize false to keep the HTML as it is, as the Speech History add-on does. The
#: nvda-action://hello link says "Hello action ran".
PLUGIN = """
import json
import os

import globalPluginHandler
import ui
from gui.message import HtmlMessageDialog

COMMAND = os.path.join(os.path.dirname(__file__), "messageProbe.json")


class GlobalPlugin(globalPluginHandler.GlobalPlugin):
	def script_showMessage(self, gesture):
		with open(COMMAND) as f:
			arguments = json.load(f)
		if not arguments.pop("sanitize", True):
			arguments["sanitizeHtmlFunc"] = lambda html: html
		ui.browseableMessage(**arguments)
		HtmlMessageDialog._instances[-1].registerAction("hello", lambda: ui.message("Hello action ran"))

	__gestures = {"kb:NVDA+control+shift+f9": "showMessage"}
"""


@pytest.fixture
def desktop(tmp_path: Path) -> Iterator[DesktopSession]:
	with DesktopSession(tmp_path / "desktop") as session:
		yield session


def start(desktop: DesktopSession, env: dict[str, str] | None = None) -> harness.Launch:
	webSession.requireBrowseLibraries()
	hooks = desktop.directory / "messageHooks"
	(hooks / "globalPlugins").mkdir(parents=True)
	(hooks / "synthDrivers").symlink_to(harness.HERE / "synthDrivers")
	(hooks / "globalPlugins" / "messageProbe.py").write_text(PLUGIN, encoding="utf-8")
	app = desktop.startApp(desktop.fixture(GTK[0]), title=GTK[1])
	assert app.stdout.readline() == "ready\n"
	desktopSession.activate(GTK[1])
	nvda = desktop.startNVDA(
		webSession.BROWSE_CONFIG, env={"LINDA_TEST_HOOKS_DIR": str(hooks), **(env or {})}
	)
	desktop.expect(*AT_START, since=0, timeout=STARTUP_BOUND)
	return nvda


def show(desktop: DesktopSession, *expected: str, **arguments) -> None:
	path = desktop.directory / "messageHooks" / "globalPlugins" / "messageProbe.json"
	path.write_text(json.dumps({"title": TITLE, **arguments}), encoding="utf-8")
	step(desktop, HOOK_KEY, *expected, timeout=OPEN_BOUND)


def step(desktop: DesktopSession, chord: str, *expected: str, timeout: float = 5) -> int:
	mark = desktop.mark()
	desktop.press(chord)
	try:
		desktop.expect(*expected, since=mark, timeout=timeout)
	except AssertionError as e:
		raise AssertionError(f"{e}\nspoken since {chord}: {desktop.spokenSince(mark)}") from None
	time.sleep(SETTLE)
	return mark


def closed(desktop: DesktopSession) -> None:
	deadline = time.monotonic() + 5
	while TITLE in desktopSession.topLevelNames():
		assert time.monotonic() < deadline, desktopSession.topLevelNames()
		time.sleep(0.1)


def quit(desktop: DesktopSession, nvda: harness.Launch) -> None:
	step(desktop, "Insert+q", "Exit Linda…")
	desktop.press("Return")
	nvda.waitForExit(10)
	assert nvda.process.returncode == 0, nvda.describe()
	nvda.assertClean(logNames=("nvda.log",))


def test_htmlMessageIsReadRunsItsActionAndCloses(desktop: DesktopSession):
	nvda = start(desktop)
	show(
		desktop,
		f"{TITLE} dialog",
		"edit read only Message heading",
		message=HTML,
		isHtml=True,
		sanitize=False,
	)
	step(desktop, "Down", "The first paragraph.")
	step(desktop, "Down", "Second heading")
	step(desktop, "Down", "Press say hello to run it.")
	step(desktop, "Down", "The last paragraph.")
	step(desktop, "control+Home", "Message heading")
	# The link the field cannot follow is a button after it.
	step(desktop, "Tab", "say hello button")
	step(desktop, "Return", "Hello action ran")
	step(desktop, "Escape", *AT_START)
	closed(desktop)

	# A second message, after the first has gone, in plain text with a close button.
	show(
		desktop,
		f"{TITLE} dialog",
		"edit read only First line of plain text.",
		message="First line of plain text.\n<b>Not markup</b> & more.",
		closeButton=True,
	)
	# The transcript has symbols as words.
	step(desktop, "Down", "less b greater Not markup less  slash b greater   and  more.")
	step(desktop, "Tab", "Close button Alt plus C")
	step(desktop, "Return", *AT_START)
	closed(desktop)
	quit(desktop, nvda)


def test_textOfHtml(monkeypatch: pytest.MonkeyPatch):
	monkeypatch.syspath_prepend(str(harness.ROOT / "source"))
	from linuxPlatform import htmlMessage

	text, actions = htmlMessage.textOf(
		"<html><head><title>T</title><style>p {}</style><script>x()</script></head><body>"
		"<h1>Head</h1><p>One   <b>bold</b>\nword.</p><ul><li>A</li><li>B</li></ul>"
		"<pre>  kept\n  as is</pre><table><tr><td>1</td><td>2</td></tr></table>"
		'<p><a href="nvda-action://close">Close it</a> <a href="https://example.org">web</a></p>'
		"</body></html>",
	)
	assert text == "Head\nOne bold word.\n• A\n• B\n  kept\n  as is\n1\t2\nClose it web"
	assert actions == [("Close it", "close")]
