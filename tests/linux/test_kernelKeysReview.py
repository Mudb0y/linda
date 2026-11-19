"""Faults found in review of the kernel key source, each shown against the stand-in for linda-keyd and,
where the X route is the reference, against the X route in the same harness. Each test fails while the
fault is there. Run under the rig:

	nix develop . -c tests/linux/rig python3 -m pytest tests/linux/test_kernelKeysReview.py
"""

from pathlib import Path

import pytest
from test_kernelKeys import runKernelChild

#: Reads the window's key events with the modifiers each came with.
STATEFUL_KEYS = r"""
def keysWithState():
	keys = []
	while app.pending_events():
		event = app.next_event()
		if event.type in (X.KeyPress, X.KeyRelease):
			keys.append([event.detail - 8, event.type == X.KeyPress, bool(event.state & X.ShiftMask)])
	return keys
"""

#: Shift goes down while the NVDA key is held, the NVDA key comes up, and a is typed with Shift still
#: held, as a fast typist does after an NVDA+Shift chord: what the window had, and what NVDA was told
#: a typed.
MODIFIER_UNDER_NVDA_CHILD = (
	STATEFUL_KEYS
	+ r"""
source = startSource(ROUTE)
keysWithState()
keyboard.down("Insert")
time.sleep(0.05)
keyboard.down("Shift_L")
time.sleep(0.05)
keyboard.up("Insert")
time.sleep(0.05)
keyboard.press("a")
time.sleep(0.05)
keyboard.up("Shift_L")
time.sleep(0.4)
result = {
	"kernel": source.isKernel,
	"window": [[evdev, shift] for evdev, down, shift in keysWithState() if down and evdev == 30],
	"nvda": [e["text"] for e in presses(ord("A"))],
}
source.stop()
keyboard.close()
"""
)


@pytest.mark.parametrize("route", ("x11", "auto"))
def test_modifierHeldUnderTheNVDAKeyReachesTheApplication(tmp_path: Path, route: str):
	value = runKernelChild(tmp_path, f"ROUTE = {route!r}\n" + MODIFIER_UNDER_NVDA_CHILD)
	assert value["kernel"] == (route == "auto")
	# NVDA says what the application typed: the X route lets the X server see Shift under its grab.
	assert value["nvda"] == ["A"], value
	assert value["window"] == [[30, True]], (
		f"on the {route} route the application typed a without Shift while NVDA was told A: {value}"
	)


#: The focus leaves the document's window for another application's and comes back, and h, browse
#: mode's next heading, is typed 50 ms after it is back, while NVDA's focus takes 150 ms to follow, as
#: Firefox's focus event takes: where h went.
BACK_TO_DOCUMENT_CHILD = r"""
other = root.create_window(0, 0, 100, 100, 0, app.screen().root_depth, event_mask=X.KeyPressMask)
other.change_property(pidAtom, Xatom.CARDINAL, 32, [2])
other.map()
app.sync()
source = startSource(ROUTE)
mode["now"] = "browse"
source.setCapture("browse", 1)
time.sleep(0.3)
other.set_input_focus(X.RevertToParent, X.CurrentTime)
app.sync()
time.sleep(0.2)
mode["now"] = "off"
source.setCapture("off")
time.sleep(0.3)
windowKeys()
delivered.clear()
window.set_input_focus(X.RevertToParent, X.CurrentTime)
app.sync()


def follow():
	time.sleep(0.15)
	mode["now"] = "browse"
	source.setCapture("browse", 1)


threading.Thread(target=follow, daemon=True).start()
time.sleep(0.05)
keyboard.press("h", hold=0.03)
time.sleep(0.8)
result = {
	"kernel": source.isKernel,
	"window": [key for key in windowKeys() if key[1]],
	"nvda": [[e["captured"], e["reached"]] for e in presses(ord("H"))],
}
source.stop()
keyboard.close()
"""


@pytest.mark.parametrize("route", ("x11", "auto"))
def test_firstKeyBackInADocumentIsBrowseModes(tmp_path: Path, route: str):
	value = runKernelChild(tmp_path, f"ROUTE = {route!r}\n" + BACK_TO_DOCUMENT_CHILD)
	assert value["kernel"] == (route == "auto")
	assert value["window"] == [], f"on the {route} route h reached the page instead of browse mode: {value}"
	assert value["nvda"] == [[True, False]], value


#: As BACK_TO_DOCUMENT_CHILD, with h typed 10 ms after the focus is back while each thread of NVDA's
#: that reads the X server's focus events takes 80 ms over the focus coming back, as when NVDA's other
#: threads keep the interpreter: the key reaches NVDA before NVDA has read where the focus is.
SLOW_FOCUS_CHILD = r"""
other = root.create_window(0, 0, 100, 100, 0, app.screen().root_depth, event_mask=X.KeyPressMask)
other.change_property(pidAtom, Xatom.CARDINAL, 32, [2])
other.map()
app.sync()
source = startSource(ROUTE)
mode["now"] = "browse"
source.setCapture("browse", 1)
time.sleep(0.3)
other.set_input_focus(X.RevertToParent, X.CurrentTime)
app.sync()
time.sleep(0.2)
mode["now"] = "off"
source.setCapture("off")
time.sleep(0.3)


def slowFocus(handle):
	def handled(event):
		if event.type == X.FocusIn:
			time.sleep(0.08)
		return handle(event)

	return handled


source._x._handle = slowFocus(source._x._handle)
if source._kernel is not None:
	source._kernel._onXEvent = slowFocus(source._kernel._onXEvent)
windowKeys()
delivered.clear()
window.set_input_focus(X.RevertToParent, X.CurrentTime)
app.sync()


def follow():
	time.sleep(0.15)
	mode["now"] = "browse"
	source.setCapture("browse", 1)


threading.Thread(target=follow, daemon=True).start()
time.sleep(0.01)
keyboard.press("h", hold=0.03)
time.sleep(0.8)
result = {
	"kernel": source.isKernel,
	"window": [key for key in windowKeys() if key[1]],
	"nvda": [[e["captured"], e["reached"]] for e in presses(ord("H"))],
}
source.stop()
keyboard.close()
"""


@pytest.mark.parametrize("route", ("x11", "auto"))
def test_firstKeyBackInADocumentIsBrowseModesBeforeNVDAReadsTheFocus(tmp_path: Path, route: str):
	value = runKernelChild(tmp_path, f"ROUTE = {route!r}\n" + SLOW_FOCUS_CHILD)
	assert value["kernel"] == (route == "auto")
	assert value["window"] == [], (
		f"on the {route} route h reached the page as NVDA had yet to read that the focus was back: {value}"
	)
	assert value["nvda"] == [[True, False]], value


#: A second window of the document's own application takes the focus, as a file chooser opened with
#: Control+O from Firefox does, and h is typed in it before NVDA's focus has followed: where h went.
SAME_APPLICATION_WINDOW_CHILD = r"""
dialog = root.create_window(0, 0, 100, 100, 0, app.screen().root_depth, event_mask=X.KeyPressMask)
dialog.change_property(pidAtom, Xatom.CARDINAL, 32, [1])
dialog.map()
app.sync()
source = startSource(ROUTE)
mode["now"] = "browse"
source.setCapture("browse", 1)
time.sleep(0.3)
dialog.set_input_focus(X.RevertToParent, X.CurrentTime)
app.sync()
time.sleep(0.1)
windowKeys()
delivered.clear()
keyboard.press("h", hold=0.03)
time.sleep(0.5)
result = {
	"kernel": source.isKernel,
	"dialog": [key for key in windowKeys() if key[1]],
	"nvda": [[e["captured"], e["reached"]] for e in presses(ord("H"))],
}
source.stop()
keyboard.close()
"""


@pytest.mark.parametrize("route", ("x11", "auto"))
def test_keyTypedInANewWindowOfTheDocumentsApplicationReachesIt(tmp_path: Path, route: str):
	value = runKernelChild(tmp_path, f"ROUTE = {route!r}\n" + SAME_APPLICATION_WINDOW_CHILD)
	assert value["kernel"] == (route == "auto")
	assert value["dialog"] == [[35, True]], (
		f"on the {route} route h was kept from the window that has the focus: {value}"
	)


#: A browser is closed and started again, and its new window gets the number its old one had, as the X
#: server gives a new client the slot of the last one to go: h typed in the new browser's document.
RESTARTED_APPLICATION_CHILD = r"""
source = startSource()


def browser(pid):
	client = display.Display()
	page = client.screen().root.create_window(0, 0, 100, 100, 0, client.screen().root_depth, event_mask=X.KeyPressMask)
	page.change_property(client.intern_atom("_NET_WM_PID"), Xatom.CARDINAL, 32, [pid])
	page.map()
	client.sync()
	time.sleep(0.2)
	page.set_input_focus(X.RevertToParent, X.CurrentTime)
	client.sync()
	time.sleep(0.2)
	return client, page


def typed(client):
	keys = []
	while client.pending_events():
		event = client.next_event()
		if event.type == X.KeyPress:
			keys.append(event.detail - 8)
	return keys


old, oldPage = browser(5)
mode["now"] = "browse"
source.setCapture("browse", 5)
time.sleep(0.2)
keyboard.press("h", hold=0.03)
time.sleep(0.3)
first = [[e["captured"], e["reached"]] for e in presses(ord("H"))]
oldId = oldPage.id
old.close()
time.sleep(0.3)
delivered.clear()
new, newPage = browser(6)
source.setCapture("browse", 6)
time.sleep(0.2)
keyboard.press("h", hold=0.03)
time.sleep(0.3)
result = {
	"kernel": source.isKernel,
	"sameNumber": newPage.id == oldId,
	"first": first,
	"second": [[e["captured"], e["reached"]] for e in presses(ord("H"))],
	"page": typed(new),
}
source.stop()
keyboard.close()
"""


def test_browseKeysAreCapturedInARestartedApplication(tmp_path: Path):
	value = runKernelChild(tmp_path, RESTARTED_APPLICATION_CHILD)
	assert value["kernel"]
	assert value["first"] == [[True, False]], value
	if not value["sameNumber"]:
		pytest.skip(f"the X server gave the new window another number: {value}")
	assert value["second"] == [[True, False]] and value["page"] == [], (
		f"the new browser's window has its old one's number, and its process is remembered by number, so "
		f"browse mode's h reached the page: {value}"
	)


#: Insert pressed twice quickly, which NVDA passes on the second time, with NVDA taking 60 ms over each
#: key, as it does while its other threads keep the interpreter busy: whether the application had the
#: second press.
DOUBLE_PRESS_CHILD = r"""
last = {"t": 0.0}


def twiceSlowly(event):
	time.sleep(0.06)
	now = time.monotonic()
	passed = event.vkCode == 0x2D and now - last["t"] < 0.6
	last["t"] = 0.0 if passed else (now if event.vkCode == 0x2D else 0.0)
	return not passed


keep = twiceSlowly
source = startSource(ROUTE)
windowKeys()
keyboard.press("Insert", hold=0.03)
time.sleep(0.15)
keyboard.press("Insert", hold=0.03)
time.sleep(0.5)
result = {"kernel": source.isKernel, "window": [key for key in windowKeys() if key[1]]}
source.stop()
keyboard.close()
"""


@pytest.mark.parametrize("route", ("x11", "auto"))
def test_nvdaKeyPressedTwiceReachesTheApplicationWhenNVDAIsSlow(tmp_path: Path, route: str):
	value = runKernelChild(tmp_path, f"ROUTE = {route!r}\n" + DOUBLE_PRESS_CHILD)
	assert value["kernel"] == (route == "auto")
	assert value["window"] == [[110, True]], (
		f"on the {route} route Insert pressed twice, answered in 60 ms, never reached the application: {value}"
	)


#: As DOUBLE_PRESS_CHILD on the kernel route, with what the helper was asked and whether it went on
#: serving NVDA.
DOUBLE_PRESS_ASKED_CHILD = DOUBLE_PRESS_CHILD.replace(
	'result = {"kernel": source.isKernel,',
	'result = {"waits": len(keyboard.waits), "malformed": [e for e in keyboard.events if e[1] == "malformed"], '
	'"granted": keyboard.granted, "kernel": source.isKernel,',
)


@pytest.mark.parametrize("features", (2, 0))
def test_slowNVDAKeyIsHeldLongerOnlyByAHelperThatCanWait(tmp_path: Path, features: int):
	value = runKernelChild(
		tmp_path, "ROUTE = 'auto'\n" + DOUBLE_PRESS_ASKED_CHILD, standIn={"features": features}
	)
	assert value["kernel"] and value["granted"] == features and not value["malformed"], value
	if features:
		assert value["waits"] >= 1 and value["window"] == [[110, True]], value
	else:
		# As before WAIT: the NVDA key is kept at 40 ms without NVDA, and the helper is asked nothing
		# it does not know.
		assert value["waits"] == 0 and value["window"] == [], value


#: The helper says goodbye, and Insert is pressed 50 ms later, as the user goes on typing: whether NVDA
#: had it and whether it reached the application.
FALLBACK_GAP_CHILD = r"""
source = startSource()
kernelBefore = source.isKernel
windowKeys()
delivered.clear()
gone = time.monotonic()
keyboard.goodbye(8, "unanswered")
time.sleep(0.05)
keyboard.press("Insert", hold=0.03)
xAt = None
deadline = time.monotonic() + 3
while time.monotonic() < deadline:
	if not source.isKernel and xAt is None:
		xAt = time.monotonic() - gone
	time.sleep(0.005)
result = {
	"kernelBefore": kernelBefore,
	"xAfter": xAt,
	"window": [key for key in windowKeys() if key[1]],
	"nvda": [e["vk"] for e in presses()],
}
source.stop()
keyboard.close()
"""


def test_noGapWithoutAKeySourceWhenTheHelperGoes(tmp_path: Path):
	value = runKernelChild(tmp_path, FALLBACK_GAP_CHILD)
	assert value["kernelBefore"]
	assert value["window"] == [] and value["nvda"] == [0x2D], (
		f"Insert typed 50 ms after the helper went reached the application, unheard; the X source took "
		f"over {value['xAfter']:.3f} s after the goodbye: {value}"
	)


#: Insert is pressed and released within the helper's bound, which here is shorter than the one it
#: announced, and NVDA takes longer than that bound over it: the helper passes it on after its release
#: has come, and says so.
EXPIRED_AFTER_RELEASE_CHILD = r"""
def slowly(event):
	time.sleep(0.08)
	return True


keep = slowly
source = startSource()
windowKeys()
delivered.clear()
keyboard.down("Insert")
time.sleep(0.01)
keyboard.up("Insert")
time.sleep(0.5)
result = {
	"kernel": source.isKernel,
	"expired": len([e for e in keyboard.events if e[1] == "expired"]),
	"releases": [e["reached"] for e in delivered if e["up"] and e["vk"] == 0x2D],
	"window": windowKeys(),
}
source.stop()
keyboard.close()
"""


def test_keyPassedOnByTheHelperAfterItsReleaseReachedTheApplication(tmp_path: Path):
	value = runKernelChild(
		tmp_path,
		EXPIRED_AFTER_RELEASE_CHILD,
		standIn={"verdictTimeout": 0.03, "announcedVerdictTimeout": 0.1},
	)
	assert value["kernel"] and value["expired"] == 1, value
	assert value["window"] == [[110, True], [110, False]], value
	assert value["releases"] == [True], f"NVDA was told Insert never reached the application: {value}"
