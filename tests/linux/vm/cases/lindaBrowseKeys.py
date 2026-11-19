"""Browse mode's keys on the kernel route go where the X route's grabs send them, the X route, with the
helper stopped, being the reference for each:
- after Alt+Tab back into a document, a quick navigation key typed once Firefox has the focus on the
  document's window again, before Linda's focus is back in it, is Linda's and never reaches the page,
  which on the kernel route needs a helper that grants WAIT;
- one typed sooner, while marco has given the focus to Firefox's top-level window and Firefox has yet
  to move it to the document's window inside, a hundred milliseconds here, reaches the page on both
  routes, as no grab is on that window: a fault of both, expected to fail until it is mended;
- a key typed in a new window of the document's own application, which Linda's focus does not
  follow, reaches that window.
"""

import json
import re
import shlex
from typing import TYPE_CHECKING

from ground import GROUND, KERNEL_ROUTE, LOGS, USER, WINDOW_TITLE, X_ROUTE

if TYPE_CHECKING:
	from ground import FakeKeyboard, Ground, Mark

UHID = "Ground uhid keyboard"
PAGE = f"{GROUND}/page.html"
PAGE_TITLE = "Ground page"
SECOND_HEADING = "Second heading"
THIRD_HEADING = "Third heading"
PROFILE = f"{GROUND}/firefox"
DIALOG_TITLE = "Ground dialog"
#: Seconds from Alt's release to H when it is typed at once.
AT_ONCE = 0.03
#: Run in the session with the ground tools' Python, from TYPER_PATH: waits until the X focus is on the
#: window whose id is given, or in the one of the title given, then, when a process is given, makes that
#: window that process's, and types H on the keyboard whose socket is given.
TYPER = """
import json, socket, sys, time
from Xlib import Xatom, display
path, want, pid = sys.argv[1], sys.argv[2], int(sys.argv[3])
d = display.Display()
root = d.screen().root


def matching(window):
	if want.isdigit():
		return window if window.id == int(want) else None
	while window is not None and not isinstance(window, int) and window.id != root.id:
		if window.get_wm_name() == want:
			return window
		window = window.query_tree().parent
	return None


deadline = time.monotonic() + 10
found = None
while found is None and time.monotonic() < deadline:
	focus = d.get_input_focus().focus
	if not isinstance(focus, int):
		found = matching(focus)
	if found is None:
		time.sleep(0.002)
if found is None:
	sys.exit("the window never had the focus")
print(f"focus on {found.id:#x} at {time.monotonic():.3f}", file=sys.stderr)
if pid:
	found.change_property(d.intern_atom("_NET_WM_PID"), Xatom.CARDINAL, 32, [pid])
	d.sync()
with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
	client.connect(path)
	client.sendall(json.dumps([{"tap": "KEY_H", "hold": 0.03}]).encode() + bytes([10]))
	print(client.makefile().readline(), end="")
"""
TYPER_PATH = f"{GROUND}/typer.py"
#: Finds the Python of the ground tools' wrappers, wrapGAppsHook's included, and runs a script with it.
WITH_TOOLS_PYTHON = (
	'tool=$(readlink -f "$(command -v ground-keyboard)"); '
	'python=$(cat "$tool" "$(dirname "$tool")/.ground-keyboard-wrapped" 2>/dev/null | tr "\\000" "\\n" '
	"| grep -o '/nix/store/[a-z0-9]*-python3[^/]*/bin/python3[.0-9]*' | head -1); "
	'exec "$python" "$@"'
)


def startTyper(ground: "Ground", want: str, pid: str = "0") -> None:
	ground.start(
		"typer",
		["sh", "-c", WITH_TOOLS_PYTHON, "typer", TYPER_PATH, f"{GROUND}/keyboards/{UHID}.sock", want, pid],
	)


def awaitTyper(ground: "Ground") -> None:
	ground.waitFor(lambda: not ground.running("typer"), "H once the window has the focus")
	typed = ground.shell(f"cat {GROUND}/jobs/typer.out {GROUND}/jobs/typer.err")
	if '"written"' not in typed:
		# Not an AssertionError, which an expected failure would take for the fault it expects.
		raise RuntimeError(f"the typer failed: {typed}")


#: What the kernel source logs when the helper grants WAIT.
WAITS = "linda-keyd holds a press longer when asked"
#: What the X source logs as NVDA's focus leaves a document.
OFF_MODE = "Keys captured for off mode"
#: Every key the page has is written to Firefox's output.
PAGE_TEXT = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>{PAGE_TITLE}</title></head>
<body>
<h1>First heading</h1>
<p>Some text under the first heading.</p>
<h2>{SECOND_HEADING}</h2>
<p>Some text under the second.</p>
<h2>{THIRD_HEADING}</h2>
<p>The end.</p>
<script>
document.addEventListener("keydown", (event) => console.log("ground key <" + event.key + ">"), true);
</script>
</body></html>
"""
#: A fresh profile's preferences: no first-run pages, no updates, no network, the console on standard
#: output.
USER_JS = "\n".join(
	f"user_pref({json.dumps(name)}, {json.dumps(value)});"
	for name, value in {
		"browser.shell.checkDefaultBrowser": False,
		"browser.startup.homepage_override.mstone": "ignore",
		"browser.aboutwelcome.enabled": False,
		"trailhead.firstrun.didSeeAboutWelcome": True,
		"datareporting.policy.dataSubmissionEnabled": False,
		"toolkit.telemetry.reportingpolicy.firstRun": False,
		"app.update.auto": False,
		"app.normandy.enabled": False,
		"network.dns.disabled": True,
		"browser.sessionstore.resume_from_crash": False,
		"devtools.console.stdout.content": True,
	}.items()
)


def pageKeys(ground: "Ground") -> list[str]:
	"""The keys the page had, but the modifiers, which reach it alone on either route."""
	text = ground.shell(f"cat {GROUND}/jobs/firefox.out 2>/dev/null || true")
	return [key for key in re.findall(r"ground key <(.*?)>", text) if key not in ("Alt", "Control", "Shift")]


def spokenSince(ground: "Ground", mark: "Mark", text: str) -> bool:
	return any(text in spoken for _t, spoken in ground.speech(mark))


def windowId(ground: "Ground", title: str) -> str:
	found = ground.run(["xdotool", "search", "--name", title])
	assert found.returncode == 0, found
	return found.stdout.split()[0]


def toFirstHeading(ground: "Ground", keyboard: "FakeKeyboard") -> None:
	mark = ground.mark()
	keyboard.chord("KEY_LEFTCTRL", "KEY_HOME")
	ground.waitFor(lambda: spokenSince(ground, mark, "First heading"), "the first heading")
	mark = ground.mark()
	keyboard.tap("KEY_H")
	ground.waitFor(lambda: spokenSince(ground, mark, SECOND_HEADING), "the second heading")
	ground.settle(0.5)


def backIntoDocument(ground: "Ground", keyboard: "FakeKeyboard", route: str, *, atOnce: bool) -> None:
	"""From the second heading, Alt+Tab to the test window and back, and H: at once, or once Firefox has
	the focus on the document's window again. Linda moves to the third heading and the page has no H."""
	documentWindow = ground.run(["xdotool", "getwindowfocus", "-f"]).stdout.strip()
	mark = ground.mark()
	left = len(ground.lindaLogRecords(OFF_MODE))
	keyboard.chord("KEY_LEFTALT", "KEY_TAB")
	ground.expect(WINDOW_TITLE, since=mark)
	ground.waitFor(lambda: len(ground.lindaLogRecords(OFF_MODE)) > left, "Linda to leave the document")
	ground.settle(0.8)
	mark = ground.mark()
	before = len(pageKeys(ground))
	steps = [
		{"press": "KEY_LEFTALT"},
		{"tap": "KEY_TAB", "hold": 0.03},
		{"sleep": 0.03},
		{"release": "KEY_LEFTALT"},
	]
	if atOnce:
		keyboard.run(*steps, {"sleep": AT_ONCE}, {"tap": "KEY_H", "hold": 0.03})
	else:
		# Waiting already as Alt+Tab is typed, so that H follows the focus within milliseconds, well
		# before Linda's focus does.
		startTyper(ground, documentWindow)
		ground.settle(0.5)
		keyboard.run(*steps)
		awaitTyper(ground)
	ground.settle(1.5)
	how = "at once" if atOnce else "once Firefox has the focus on the document"
	ground.machine.log(
		f"{route} route, H {how} after Alt+Tab back: Linda said {ground.speech(mark)}, the page had "
		f"{pageKeys(ground)[before:]}"
	)
	assert pageKeys(ground)[before:] == [], (route, how, pageKeys(ground)[before:], ground.speech(mark))
	assert spokenSince(ground, mark, THIRD_HEADING), (route, how, ground.speech(mark))


def keyInNewWindow(ground: "Ground", keyboard: "FakeKeyboard", route: str) -> None:
	"""A window of the browser's process, which Linda's focus does not follow, takes the focus: H typed
	in it reaches it."""
	ground.settle(0.8)
	firefoxWindow = windowId(ground, PAGE_TITLE)
	pid = ground.run(["xdotool", "getwindowpid", firefoxWindow]).stdout.strip()
	mark = ground.mark()
	before = len(pageKeys(ground))
	# H as soon as the window has the focus, made the browser's, before anything of Linda's could follow.
	startTyper(ground, DIALOG_TITLE, pid)
	ground.settle(0.5)
	# Without the accessibility bus, as a window Linda cannot follow into.
	ground.start("dialog", ["ground-window", LOGS["app"], DIALOG_TITLE], env={"NO_AT_BRIDGE": "1"})
	awaitTyper(ground)
	ground.settle(0.8)
	typed = [r for r in ground.keys("app", mark) if r["window"] == DIALOG_TITLE and r["value"] == 1]
	ground.machine.log(f"{route} route, H in the browser's new window: it had {[r['key'] for r in typed]}")
	assert [r["key"] for r in typed] == ["KEY_H"], (route, ground.keys("app", mark))
	assert pageKeys(ground)[before:] == [], (route, pageKeys(ground)[before:])
	ground.session({"op": "signal", "name": "dialog", "signal": "SIGTERM"})
	ground.waitFor(lambda: not ground.running("dialog"), "the dialog to close")
	activated = ground.run(["xdotool", "windowactivate", "--sync", firefoxWindow])
	assert activated.returncode == 0, activated
	ground.settle(1)


def everything(ground: "Ground", keyboard: "FakeKeyboard", route: str) -> None:
	with ground.subtest(f"browse mode's H is Linda's, {route} route"):
		before = len(pageKeys(ground))
		toFirstHeading(ground, keyboard)
		assert pageKeys(ground)[before:] == [], pageKeys(ground)

	with ground.subtest(
		f"H after Alt+Tab back into the document once Firefox has it is Linda's, {route} route"
	):
		if route == "X" or ground.lindaLogRecords(WAITS):
			backIntoDocument(ground, keyboard, route, atOnce=False)
		else:
			with ground.expectedFailure("this linda-keyd cannot hold a key for Linda's focus to come back"):
				backIntoDocument(ground, keyboard, route, atOnce=False)

	with ground.subtest(f"H at once after Alt+Tab back into the document, {route} route"):
		toFirstHeading(ground, keyboard)
		with ground.expectedFailure(
			"before Firefox moves the focus from its top-level window to the document's, no grab takes a key"
		):
			backIntoDocument(ground, keyboard, route, atOnce=True)

	with ground.subtest(f"H in a new window of the browser reaches it, {route} route"):
		keyInNewWindow(ground, keyboard, route)


def run(ground: "Ground") -> None:
	ground.openWindow()
	ground.waitForHelper()
	keyboard = ground.uhidKeyboard(UHID)
	# For the session to type on it too.
	ground.machine.succeed(f"chmod 666 {shlex.quote(f'{GROUND}/keyboards/{UHID}.sock')}")
	# Read from the top by the keys alone.
	ground.startLinda({"virtualBuffers": {"autoSayAllOnPageLoad": "False"}})
	ground.waitForLindaLog(KERNEL_ROUTE, timeout=30)
	ground.machine.succeed(
		f"echo {shlex.quote(PAGE_TEXT)} > {PAGE}",
		f"install -d -o {USER} {PROFILE}",
		f"echo {shlex.quote(USER_JS)} > {PROFILE}/user.js",
		f"echo {shlex.quote(TYPER)} > {TYPER_PATH}",
		f"chown -R {USER} {PAGE} {PROFILE}",
	)
	mark = ground.mark()
	ground.start(
		"firefox",
		["firefox", "--no-remote", "--profile", PROFILE, f"file://{PAGE}"],
		env={"MOZ_CRASHREPORTER_DISABLE": "1"},
	)
	ground.waitFor(lambda: spokenSince(ground, mark, PAGE_TITLE), "Firefox's page", 90)
	ground.waitForLindaLog("Keys captured for browse mode", timeout=30)
	ground.settle(2)

	everything(ground, keyboard, "kernel")
	ground.shell("systemctl stop linda-keyd.service")
	ground.waitForLindaLog(X_ROUTE, count=2, timeout=10)
	ground.settle(1)
	everything(ground, keyboard, "X")
