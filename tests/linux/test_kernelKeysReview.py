"""Faults found in review of the kernel key source, each shown against the stand-in for linda-keyd and,
where the X route is the reference, against the X route in the same harness. Each test fails while the
fault is there. Run under the rig:

	nix develop . -c tests/linux/rig python3 -m pytest tests/linux/test_kernelKeysReview.py
"""

from pathlib import Path

import pytest
from test_kernelKeys import LET_GO_MESSAGE, runKernelChild

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


#: Shift held on a keyboard the helper leaves alone, here another program's XTEST, as on a remapper's
#: keyboard or one that is also a pointer, and a typed on the keyboard the helper holds: what the window
#: had, and what NVDA was told.
SHIFT_ELSEWHERE_CHILD = (
	STATEFUL_KEYS
	+ r"""
from Xlib.ext import xtest
source = startSource(ROUTE)
shift = app.keysym_to_keycode(XK.XK_Shift_L)
xtest.fake_input(app, X.KeyPress, shift)
app.sync()
time.sleep(0.2)
keysWithState()
delivered.clear()
keyboard.press("a", hold=0.03)
time.sleep(0.3)
xtest.fake_input(app, X.KeyRelease, shift)
app.sync()
time.sleep(0.2)
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
def test_modifierHeldOnAKeyboardTheHelperLeavesAloneCountsForItsKeys(tmp_path: Path, route: str):
	value = runKernelChild(tmp_path, f"ROUTE = {route!r}\n" + SHIFT_ELSEWHERE_CHILD)
	assert value["kernel"] == (route == "auto")
	assert value["window"] == [[30, True]], value
	assert value["nvda"] == ["A"], (
		f"on the {route} route NVDA was told a while the application typed A: {value}"
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


#: The focus leaves the document's window for another application's and comes back, and h is typed
#: 10 ms after, which the helper holds for NVDA's focus to follow, 250 ms later; Insert goes down 20 ms
#: after h, behind it in line, and h comes up. The stand-in's bound is 100 ms, and WELCOME gives
#: whatever the test says.
LINE_HELD_BY_A_DOCUMENT = r"""
other = root.create_window(0, 0, 100, 100, 0, app.screen().root_depth, event_mask=X.KeyPressMask)
other.change_property(pidAtom, Xatom.CARDINAL, 32, [2])
other.map()
app.sync()
source = startSource()
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
start = len(keyboard.events)
window.set_input_focus(X.RevertToParent, X.CurrentTime)
app.sync()


def follow():
	time.sleep(0.25)
	mode["now"] = "browse"
	source.setCapture("browse", 1)


threading.Thread(target=follow, daemon=True).start()
time.sleep(0.01)
keyboard.down("h")
time.sleep(0.02)
keyboard.down("Insert")
time.sleep(0.03)
keyboard.up("h")


def noted(kind, seq=None):
	return [rest for _t, what, *rest in keyboard.events[start:] if what == kind and (seq is None or rest[0] == seq)]


def pressSeq(evdev):
	return next(seq for seq, code, value, _flags in noted("key") if code == evdev and value == 1)
"""


#: Insert is answered after its own bound has passed while h holds the line: WELCOME says 300 ms, so
#: that NVDA asks the helper to wait 120 ms after the press and answers once h is decided.
VERDICT_AFTER_BOUND_CHILD = (
	LINE_HELD_BY_A_DOCUMENT
	+ r"""
time.sleep(0.03)
keyboard.up("Insert")
time.sleep(0.8)
insert = pressSeq(110)
result = {
	"kernel": source.isKernel,
	"window": [key for key in windowKeys() if key[1]],
	"nvda": [[e["vk"], e["captured"], e["reached"]] for e in presses() if e["vk"] in (ord("H"), 0x2D)],
	"waited": [delay for seq, delay in keyboard.waits if seq == insert],
	"verdicts": [[keep, delay] for seq, keep, delay in keyboard.verdicts if seq == insert],
	"expired": noted("expired", insert),
}
source.stop()
keyboard.close()
"""
)


def test_verdictAfterItsBoundCountsWhileAPressBeforeHoldsTheLine(tmp_path: Path):
	value = runKernelChild(tmp_path, VERDICT_AFTER_BOUND_CHILD, standIn={"announcedVerdictTimeout": 0.3})
	assert value["kernel"]
	# What the test is about: both came after Insert's own bound, while it was still in line.
	assert value["waited"] and value["waited"][0] > 0.1, value
	assert value["verdicts"] and value["verdicts"][0][0] and value["verdicts"][0][1] > 0.1, value
	assert value["expired"] == [], value
	assert value["window"] == [], f"a key NVDA kept after its bound reached the application: {value}"
	assert value["nvda"] == [[ord("H"), True, False], [0x2D, True, False]], value


#: Insert is not answered before h is decided, after Insert's own bound: WELCOME says 900 ms, so that
#: NVDA would ask the helper to wait only 360 ms after the press, and NVDA takes Insert only once the
#: helper has said EXPIRED, which it does as it takes Insert out of line behind h. t is typed with
#: Insert still down, then Insert comes up.
LATE_EXPIRED_CHILD = (
	LINE_HELD_BY_A_DOCUMENT
	+ r"""


def keepOnceExpired(event):
	if event.vkCode == 0x2D:
		deadline = time.monotonic() + 1
		while not noted("expired") and time.monotonic() < deadline:
			time.sleep(0.005)
	return True


keep = keepOnceExpired
deadline = time.monotonic() + 2
while not noted("expired") and time.monotonic() < deadline:
	time.sleep(0.005)
time.sleep(0.1)
keyboard.press("t", hold=0.03)
time.sleep(0.1)
keyboard.up("Insert")
time.sleep(0.5)
insert = pressSeq(110)
result = {
	"kernel": source.isKernel,
	"window": [key for key in windowKeys() if key[1]],
	"expired": noted("expired"),
	"insert": insert,
	"late": noted("late verdict", insert),
	"t": [[e["captured"], e["reached"], e["nvda"]] for e in presses(ord("T"))],
	"h": [[e["captured"], e["reached"]] for e in presses(ord("H"))],
}
source.stop()
keyboard.close()
"""
)


def test_pressExpiredLaterThanItsBoundIsTheApplications(tmp_path: Path):
	value = runKernelChild(tmp_path, LATE_EXPIRED_CHILD, standIn={"announcedVerdictTimeout": 0.9})
	assert value["kernel"]
	assert value["expired"] == [[value["insert"]]], value
	assert value["h"] == [[True, False]], value
	# The helper passed Insert on, and NVDA sent nothing for it after; t, typed with it still down, is
	# browse mode's own and no chord with an NVDA key the application has.
	assert value["window"] == [[110, True]], f"{value}"
	assert value["late"] == [], value
	assert value["t"] == [[True, False, False]], value


#: Shift and x go down and out, then Insert, which NVDA takes 200 ms over; x and Shift come up
#: meanwhile: when the X display had each, beside the verdict on Insert.
RELEASE_OVERTAKES_CHILD = r"""
source = startSource()
start = len(keyboard.events)


def slowInsert(event):
	if event.vkCode == 0x2D:
		time.sleep(0.2)
	return True


keep = slowInsert
keyboard.down("Shift_L")
time.sleep(0.05)
keyboard.down("x")
time.sleep(0.05)
keyboard.down("Insert")
time.sleep(0.03)
keyboard.up("x")
time.sleep(0.01)
keyboard.up("Shift_L")
time.sleep(0.4)
keyboard.up("Insert")
time.sleep(0.3)
events = keyboard.events[start:]
insert = next(rest[0] for _t, what, *rest in events if what == "key" and rest[1] == 110 and rest[2] == 1)
verdict = next(t for t, what, *rest in events if what == "verdict" and rest[0] == insert)
out = {f"{rest[0]} {'down' if rest[1] else 'up'}": t < verdict for t, what, *rest in events if what == "x"}
order = [[e["vk"], e["up"]] for e in delivered if e["vk"] in (ord("X"), 0x2D)]
result = {"kernel": source.isKernel, "beforeInsertsVerdict": out, "nvda": order, "window": windowKeys()}
source.stop()
keyboard.close()
"""


def test_releaseOvertakesAPressWaitingForItsVerdictUnlessAModifier(tmp_path: Path):
	value = runKernelChild(tmp_path, RELEASE_OVERTAKES_CHILD)
	assert value["kernel"]
	assert value["beforeInsertsVerdict"] == {
		"42 down": True,
		"45 down": True,
		"45 up": True,
		"42 up": False,
	}, value
	# NVDA has the keys in the order they were typed, whatever order the X display had them in.
	assert value["nvda"] == [[ord("X"), False], [0x2D, False], [ord("X"), True], [0x2D, True]], value
	assert value["window"] == [[42, True], [45, True], [45, False], [42, False]], value


#: A document in a window inside a top-level one, as GTK's programs and Firefox have it: the focus
#: leaves for another application's window and comes back, from outside, to the top-level window, as
#: the window manager gives it, and h is typed 30 ms after, before the program moves the focus to the
#: document's window inside; then, NVDA's focus back in the document, the focus goes from the
#: document's window to the top-level window, as to some other part of the program, and h is typed
#: there: where each h went.
TOP_LEVEL_CHILD = r"""
other = root.create_window(0, 0, 100, 100, 0, app.screen().root_depth, event_mask=X.KeyPressMask)
other.change_property(pidAtom, Xatom.CARDINAL, 32, [2])
other.map()
top = root.create_window(0, 0, 300, 300, 0, app.screen().root_depth, event_mask=X.KeyPressMask)
top.change_property(pidAtom, Xatom.CARDINAL, 32, [1])
wmState = app.intern_atom("WM_STATE")
top.change_property(wmState, wmState, 32, [1, 0])
top.map()
page = top.create_window(-1, -1, 1, 1, 0, app.screen().root_depth, event_mask=X.KeyPressMask)
page.map()
app.sync()
time.sleep(0.2)
page.set_input_focus(X.RevertToParent, X.CurrentTime)
app.sync()
time.sleep(0.2)
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
top.set_input_focus(X.RevertToParent, X.CurrentTime)
app.sync()


def follow():
	time.sleep(0.15)
	mode["now"] = "browse"
	source.setCapture("browse", 1)


threading.Thread(target=follow, daemon=True).start()
time.sleep(0.03)
keyboard.press("h", hold=0.03)
time.sleep(0.05)
page.set_input_focus(X.RevertToParent, X.CurrentTime)
app.sync()
time.sleep(0.6)
back = {"window": [key for key in windowKeys() if key[1]], "nvda": [[e["captured"], e["reached"]] for e in presses(ord("H"))]}
delivered.clear()
keyboard.press("h", hold=0.03)
time.sleep(0.4)
inside = {"window": [key for key in windowKeys() if key[1]], "nvda": [[e["captured"], e["reached"]] for e in presses(ord("H"))]}
delivered.clear()
top.set_input_focus(X.RevertToParent, X.CurrentTime)
app.sync()
time.sleep(0.2)
keyboard.press("h", hold=0.03)
time.sleep(0.4)
result = {
	"kernel": source.isKernel,
	"back": back,
	"inside": inside,
	"left": {"window": [key for key in windowKeys() if key[1]], "nvda": [[e["captured"], e["reached"]] for e in presses(ord("H"))]},
}
source.stop()
keyboard.close()
"""


@pytest.mark.parametrize("route", ("x11", "auto"))
def test_keyTypedAsTheFocusReachesTheDocumentsTopLevelWindowIsBrowseModes(tmp_path: Path, route: str):
	value = runKernelChild(tmp_path, f"ROUTE = {route!r}\n" + TOP_LEVEL_CHILD)
	assert value["kernel"] == (route == "auto")
	assert value["back"] == {"window": [], "nvda": [[True, False]]}, (
		f"on the {route} route h typed as the focus came back to the top-level window reached it: {value}"
	)
	assert value["inside"] == {"window": [], "nvda": [[True, False]]}, value
	assert value["left"]["window"] == [[35, True]], (
		f"on the {route} route h was taken from the top-level window the focus went to from the document's "
		f"own: {value}"
	)


#: The document's window inside a top-level one, as in TOP_LEVEL_CHILD; the focus leaves for another
#: application's window, then goes to the root window or to PointerRoot with the pointer over the
#: top-level window, where by the X protocol a grab on the window under the pointer takes keys, and NVDA's
#: focus stays in the document. h is typed, its XInput event never coming, so that the X source has it
#: only from the grab: where it went.
POINTER_ROOT_CHILD = r"""
other = root.create_window(0, 0, 100, 100, 0, app.screen().root_depth, event_mask=X.KeyPressMask)
other.change_property(pidAtom, Xatom.CARDINAL, 32, [2])
other.map()
top = root.create_window(0, 0, 300, 300, 0, app.screen().root_depth, event_mask=X.KeyPressMask)
top.change_property(pidAtom, Xatom.CARDINAL, 32, [1])
wmState = app.intern_atom("WM_STATE")
top.change_property(wmState, wmState, 32, [1, 0])
top.map()
page = top.create_window(-1, -1, 1, 1, 0, app.screen().root_depth, event_mask=X.KeyPressMask)
page.map()
app.sync()
time.sleep(0.2)
page.set_input_focus(X.RevertToParent, X.CurrentTime)
app.sync()
time.sleep(0.2)
source = startSource(ROUTE)
mode["now"] = "browse"
source.setCapture("browse", 1)
time.sleep(0.3)
other.set_input_focus(X.RevertToParent, X.CurrentTime)
app.sync()
time.sleep(0.3)
onSlave = source._x._onSlave


def losingH(data, isUp):
	if data.detail != 43 or isUp:
		onSlave(data, isUp)


source._x._onSlave = losingH
root.warp_pointer(250, 250)
if FOCUS == "root":
	root.set_input_focus(X.RevertToPointerRoot, X.CurrentTime)
else:
	app.set_input_focus(X.PointerRoot, X.RevertToPointerRoot, X.CurrentTime)
app.sync()
time.sleep(0.3)
windowKeys()
delivered.clear()
keyboard.press("h", hold=0.03)
time.sleep(0.5)
result = {
	"kernel": source.isKernel,
	"window": [key for key in windowKeys() if key[1]],
	"nvda": [[e["captured"], e["reached"]] for e in presses(ord("H"))],
}
source.stop()
# The pointer and the focus as the X server starts with them, for the tests after this one.
screen = app.screen()
root.warp_pointer(screen.width_in_pixels // 2, screen.height_in_pixels // 2)
app.set_input_focus(X.PointerRoot, X.RevertToPointerRoot, X.CurrentTime)
app.sync()
keyboard.close()
"""


@pytest.mark.parametrize("focus", ("root", "pointerRoot"))
@pytest.mark.parametrize("route", ("x11", "auto"))
def test_keyTypedWithTheFocusOnTheRootOverTheDocumentsWindowReachesIt(tmp_path: Path, route: str, focus: str):
	value = runKernelChild(tmp_path, f"ROUTE = {route!r}\nFOCUS = {focus!r}\n" + POINTER_ROOT_CHILD)
	assert value["kernel"] == (route == "auto")
	assert value["window"] == [[35, True]], (
		f"on the {route} route h was kept from the window under the pointer, the focus on {focus}: {value}"
	)
	assert value["nvda"] == [[False, True]], value


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


#: Input help ends while the X thread is slow to let go of the keyboard it grabbed for it, and b is
#: typed at once: what the window had.
HOLD_ENDS_CHILD = r"""
source = startSource()
source.holdKeyboard(True)
time.sleep(0.2)
hold = source._x._hold


def slowHold(wanted):
	if not wanted:
		time.sleep(0.05)
	hold(wanted)


source._x._hold = slowHold
windowKeys()
source.holdKeyboard(False)
keyboard.press("b", hold=0.03)
time.sleep(0.3)
result = {"kernel": source.isKernel, "window": windowKeys()}
source.stop()
keyboard.close()
"""


def test_keyTypedAsInputHelpEndsReachesTheApplicationWhole(tmp_path: Path):
	value = runKernelChild(tmp_path, HOLD_ENDS_CHILD)
	assert value["kernel"]
	assert value["window"] == [[48, True], [48, False]], (
		f"the press went to the X source's grab of the keyboard, still there: {value}"
	)


#: Every kind of key, pressed and released with what decides its virtual-key code changing between the
#: two where it can: plain, modifiers, AltGr in and out of a hold, numpad keys across Num Lock, the
#: locks, the NVDA key by each of its keys, keys typed by NVDA, and keys of a mode that gives them back.
#: Whether each release NVDA had is its press's, and what NVDA is left believing is down.
EVERY_KEY_CHILD = r"""
import subprocess
told = []


def deliver(event):
	told.append([event.vkCode, event.scanCode, event.extended, event.isUp, event.repeat])
	if event.isUp or not event.captured:
		return True
	return not keep(event)


xkb = app.query_extension("XKEYBOARD").major_opcode
x11._XkbUseExtension(display=app.display, opcode=xkb, wantedMajor=1, wantedMinor=0)


def locks():
	locked = x11._XkbGetState(display=app.display, opcode=xkb, deviceSpec=x11.XKB_CORE_KEYBOARD).lockedMods
	return locked & (X.LockMask | x11.NUM_LOCK_MASK), app.get_keyboard_control().led_mask & 4


locksBefore = locks()
subprocess.run(["setxkbmap", "pl"], check=True)
time.sleep(0.3)
source = startSource(ROUTE, consumed=x11.EXTENDED_INSERT | x11.CAPS_LOCK | x11.NUMPAD_INSERT)
keyboard.press("a", "Shift_L+a", "Control_L+a")
keyboard.down(97)
keyboard.press("a")
keyboard.up(97)
keyboard.press("ISO_Level3_Shift+e")
keyboard.down("ISO_Level3_Shift")
source.holdKeyboard(True)
time.sleep(0.2)
keyboard.press("e")
keyboard.up("ISO_Level3_Shift")
time.sleep(0.1)
keyboard.down("ISO_Level3_Shift")
source.holdKeyboard(False)
time.sleep(0.2)
keyboard.up("ISO_Level3_Shift")
keyboard.press("KP_Up")
keyboard.down("KP_Up")
keyboard.press("Num_Lock")
keyboard.up("KP_Up")
keyboard.press("KP_Up")
keyboard.down("KP_Up")
keyboard.press("Num_Lock")
keyboard.up("KP_Up")
keyboard.down("KP_Insert")
keyboard.press("Num_Lock")
keyboard.up("KP_Insert")
keyboard.press("Num_Lock")
keyboard.press("Insert+t", "Caps_Lock+t", "KP_Insert+t")
keyboard.press("Caps_Lock")
time.sleep(0.15)
keyboard.press("Caps_Lock")
keyboard.press("Scroll_Lock", "Scroll_Lock")
source.typeKey(ord("B"), False, False)
source.typeKey(ord("B"), False, True)
source.typeKey(0xA0, False, False)
source.typeKey(ord("C"), False, False)
source.typeKey(ord("C"), False, True)
source.typeKey(0xA0, False, True)
mode["now"] = "browse"
source.setCapture("browse", 1)
time.sleep(0.3)
mode["now"] = "off"
keyboard.press("h", "Shift_L+h", "Down")
time.sleep(0.5)
source.setCapture("off")
subprocess.run(["setxkbmap", "us"], check=True)
down = {}
unmatched = []
for vk, scan, extended, up, repeat in told:
	if repeat:
		continue
	if up:
		if vk in down:
			if down.pop(vk) != (scan, extended):
				unmatched.append([vk, scan, extended])
		else:
			unmatched.append([vk, scan, extended])
	else:
		down[vk] = (scan, extended)
result = {"kernel": source.isKernel, "stillDown": sorted(down), "unmatched": unmatched, "count": len(told)}
source.stop()
# The locks as they were, whatever the keys typed did: a lock key NVDA kept toggled nothing, so that
# counting the presses does not tell, and the tests after this one share the display.
result["locksLeft"] = locks()
x11._XkbLatchLockState(
	display=app.display,
	opcode=xkb,
	deviceSpec=x11.XKB_CORE_KEYBOARD,
	affectModLocks=X.LockMask | x11.NUM_LOCK_MASK,
	modLocks=locksBefore[0],
	lockGroup=False,
	groupLock=0,
	affectModLatches=0,
	modLatches=0,
	latchGroup=False,
	groupLatch=0,
)
if result["locksLeft"][1] != locksBefore[1]:
	from Xlib.ext import xtest
	code = app.keysym_to_keycode(XK.XK_Scroll_Lock)
	xtest.fake_input(app, X.KeyPress, code)
	xtest.fake_input(app, X.KeyRelease, code)
app.sync()
time.sleep(0.2)
result["locksPutBack"] = locks() == locksBefore
keyboard.close()
"""


@pytest.mark.parametrize("route", ("x11", "auto"))
def test_everyReleaseNVDAHasIsItsPresss(tmp_path: Path, route: str):
	value = runKernelChild(tmp_path, f"ROUTE = {route!r}\n" + EVERY_KEY_CHILD)
	assert value["kernel"] == (route == "auto")
	assert value["count"] > 60, value
	assert value["stillDown"] == [], f"on the {route} route NVDA is left believing keys are down: {value}"
	assert value["unmatched"] == [], (
		f"on the {route} route releases came as other keys than their presses: {value}"
	)
	assert value["locksPutBack"], value


#: As TOP_LEVEL_CHILD, with another program holding h on the top-level window; the focus comes back
#: to the top-level window from outside, and h and j are typed there: how long after each was typed
#: NVDA had it.
REFUSED_ON_TOP_CHILD = r"""
other = root.create_window(0, 0, 100, 100, 0, app.screen().root_depth, event_mask=X.KeyPressMask)
other.change_property(pidAtom, Xatom.CARDINAL, 32, [2])
other.map()
top = root.create_window(0, 0, 300, 300, 0, app.screen().root_depth, event_mask=X.KeyPressMask)
top.change_property(pidAtom, Xatom.CARDINAL, 32, [1])
wmState = app.intern_atom("WM_STATE")
top.change_property(wmState, wmState, 32, [1, 0])
top.map()
page = top.create_window(-1, -1, 1, 1, 0, app.screen().root_depth, event_mask=X.KeyPressMask)
page.map()
app.sync()
holder = display.Display()
holder.create_resource_object("window", top.id).grab_key(
	holder.keysym_to_keycode(XK.XK_h), X.AnyModifier, False, X.GrabModeAsync, X.GrabModeAsync
)
holder.sync()
time.sleep(0.2)
page.set_input_focus(X.RevertToParent, X.CurrentTime)
app.sync()
time.sleep(0.2)
source = startSource("x11")
mode["now"] = "browse"
source.setCapture("browse", 1)
time.sleep(0.3)
other.set_input_focus(X.RevertToParent, X.CurrentTime)
app.sync()
time.sleep(0.3)
top.set_input_focus(X.RevertToParent, X.CurrentTime)
app.sync()
time.sleep(0.3)
delivered.clear()
start = len(keyboard.events)
keyboard.press("h", "j", hold=0.03, gap=0.01)
time.sleep(0.8)
typed = {rest[0]: t for t, what, *rest in keyboard.events[start:] if what == "x" and rest[1]}
late = {}
for e in presses():
	evdev = {ord("H"): 35, ord("J"): 36}.get(e["vk"])
	if evdev in typed:
		late[chr(e["vk"])] = round(e["t"] - typed[evdev], 3)
result = {"late": late}
source.stop()
keyboard.close()
holder.close()
"""


def test_keyAnotherProgramHoldsOnTheTopLevelWindowIsNotWaitedFor(tmp_path: Path):
	value = runKernelChild(tmp_path, REFUSED_ON_TOP_CHILD)
	assert set(value["late"]) == {"H", "J"}, value
	assert max(value["late"].values()) < 0.15, (
		f"a key another program holds on the top-level window was taken for one Linda's grab holds, and NVDA "
		f"had it, and the key after it, a quarter of a second late: {value}"
	)


#: As TOP_LEVEL_CHILD, on the X route: with the focus on the top-level window itself, back from
#: another program's, the NVDA key is held and z, which NVDA passes on, typed with it; the focus goes to
#: the other program's window and comes back to the top-level window, and h is typed 30 ms after:
#: where h went.
CHORD_ON_TOP_CHILD = r"""
other = root.create_window(0, 0, 100, 100, 0, app.screen().root_depth, event_mask=X.KeyPressMask)
other.change_property(pidAtom, Xatom.CARDINAL, 32, [2])
other.map()
top = root.create_window(0, 0, 300, 300, 0, app.screen().root_depth, event_mask=X.KeyPressMask)
top.change_property(pidAtom, Xatom.CARDINAL, 32, [1])
wmState = app.intern_atom("WM_STATE")
top.change_property(wmState, wmState, 32, [1, 0])
top.map()
page = top.create_window(-1, -1, 1, 1, 0, app.screen().root_depth, event_mask=X.KeyPressMask)
page.map()
app.sync()
time.sleep(0.2)
page.set_input_focus(X.RevertToParent, X.CurrentTime)
app.sync()
time.sleep(0.2)
source = startSource("x11")
mode["now"] = "browse"
source.setCapture("browse", 1)
time.sleep(0.3)


def away():
	other.set_input_focus(X.RevertToParent, X.CurrentTime)
	app.sync()
	time.sleep(0.2)
	mode["now"] = "off"
	source.setCapture("off")
	time.sleep(0.3)


def back():
	top.set_input_focus(X.RevertToParent, X.CurrentTime)
	app.sync()


away()
back()
time.sleep(0.2)
mode["now"] = "browse"
source.setCapture("browse", 1)
time.sleep(0.3)
keep = lambda event: event.vkCode != ord("Z")
keyboard.down("Insert")
time.sleep(0.05)
keyboard.press("z", hold=0.03)
time.sleep(0.05)
keyboard.up("Insert")
time.sleep(0.3)
away()
windowKeys()
delivered.clear()
back()


def follow():
	time.sleep(0.15)
	mode["now"] = "browse"
	source.setCapture("browse", 1)


threading.Thread(target=follow, daemon=True).start()
time.sleep(0.03)
keyboard.press("h", hold=0.03)
time.sleep(0.6)
result = {
	"window": [key for key in windowKeys() if key[1]],
	"nvda": [[e["captured"], e["reached"]] for e in presses(ord("H"))],
}
source.stop()
keyboard.close()
"""


def test_documentsKeysAreTakenOnTheTopLevelWindowAfterAChordThere(tmp_path: Path):
	value = runKernelChild(tmp_path, CHORD_ON_TOP_CHILD)
	assert value["window"] == [], (
		f"an NVDA key's chord on the top-level window let go of the document's keys there for good, and h "
		f"typed as the focus came back reached it: {value}"
	)
	assert value["nvda"] == [[True, False]], value


#: The helper says it let go for the emergency chord just as NVDA is stopping the kernel source, as
#: when NVDA gives the helper up at that moment: whether NVDA lets go until the user asks, and says so.
EMERGENCY_WHILE_STOPPING_CHILD = r"""
from linuxPlatform.keys import keydClient
source = startSource()
kernel = source._kernel


def stoppingAsTheChordComes():
	kernel._stopping = True
	kernel._onReleased(keydClient.RELEASED_EMERGENCY)


kernel._post(stoppingAsTheChordComes)
time.sleep(3)
result = {"kernel": source.isKernel, "starts": keyboard.starts, "said": list(said)}
source.stop()
keyboard.close()
"""


def test_emergencyChordAsTheKernelSourceStopsIsHonoured(tmp_path: Path):
	value = runKernelChild(tmp_path, EMERGENCY_WHILE_STOPPING_CHILD)
	assert not value["kernel"], value
	assert value["starts"] == 1, f"NVDA asked the helper to capture again after the emergency chord: {value}"
	assert value["said"] == [LET_GO_MESSAGE], value


#: The layout's thread is held up past the two seconds stop waits for it: whether its wake-up pipes
#: were closed under it, so that the numbers go to the next connection made, which it would write into.
STOP_UNDER_A_LIVE_THREAD_CHILD = r"""
import os, socket, stat
source = startSource()
kernel = source._kernel
watch = kernel._watchCapture
stuck = threading.Event()


def slowWatch():
	if not stuck.is_set():
		stuck.set()
		time.sleep(3)
	watch()


kernel._watchCapture = slowWatch
kernel._wakeLayout()
stuck.wait(2)
pipes = (kernel._wakeRead, kernel._wakeWrite, kernel._layoutWakeRead, kernel._layoutWakeWrite)
source.stop()


def isPipe(fd):
	try:
		return stat.S_ISFIFO(os.fstat(fd).st_mode)
	except OSError:
		return False


open_ = [isPipe(fd) for fd in pipes]
reused = []
for _ in range(4):
	a, b = socket.socketpair()
	reused += [fd for fd in (a.fileno(), b.fileno()) if fd in pipes]
result = {"open": open_, "reused": reused}
keyboard.close()
"""


def test_stopLeavesTheWakePipesOfAThreadStillRunning(tmp_path: Path):
	value = runKernelChild(tmp_path, STOP_UNDER_A_LIVE_THREAD_CHILD)
	assert value["reused"] == [] and value["open"] == [True] * 4, (
		f"the kernel source closed the pipes a thread of its still running writes its wake-ups into: {value}"
	)


#: The helper lets go as it cannot tell which session is in front, and captures again by itself once
#: it can: whether NVDA stays with that helper and says nothing, and has the keys typed meanwhile and
#: after.
SESSION_UNKNOWN_CHILD = r"""
source = startSource()
kernel = source._kernel
keyboard.letGo(5)
time.sleep(0.5)
delivered.clear()
keyboard.press("a")
time.sleep(0.3)
meanwhile = [e["text"] for e in presses()]
with keyboard._lock:
	keyboard._capture()
time.sleep(2.5)
delivered.clear()
keyboard.press("b")
time.sleep(0.3)
result = {
	"same": source._kernel is kernel and source.isKernel,
	"starts": keyboard.starts,
	"said": list(said),
	"meanwhile": meanwhile,
	"after": [e["text"] for e in presses()],
	"window": [key for key in windowKeys() if key[1]],
}
source.stop()
keyboard.close()
"""


def test_helperUnsureOfTheSessionInFrontIsWaitedFor(tmp_path: Path):
	value = runKernelChild(tmp_path, SESSION_UNKNOWN_CHILD)
	assert value["same"] and value["starts"] == 1 and value["said"] == [], (
		f"the helper let go as it could not tell which session is in front, which it takes back by itself: "
		f"NVDA gave it up instead: {value}"
	)
	assert value["meanwhile"] == ["a"] and value["after"] == ["b"], value
	assert value["window"] == [[30, True], [48, True]], value


#: The helper lets go as this session is no longer the one in front; Caps Lock is turned on meanwhile,
#: here through XTEST, and the user asks NVDA to take the keyboard back: whether NVDA reports the lock
#: as the X display has it, and what it says.
BACKGROUND_CHILD = r"""
from Xlib.ext import xtest
source = startSource()
keyboard.letGo(3)
time.sleep(0.3)
before = source.keyState(0x14) & 1
caps = app.keysym_to_keycode(XK.XK_Caps_Lock)
xtest.fake_input(app, X.KeyPress, caps)
xtest.fake_input(app, X.KeyRelease, caps)
app.sync()
time.sleep(0.3)
after = source.keyState(0x14) & 1
source.takeBack()
time.sleep(0.3)
xtest.fake_input(app, X.KeyPress, caps)
xtest.fake_input(app, X.KeyRelease, caps)
app.sync()
time.sleep(0.2)
result = {"before": before, "after": after, "said": list(said), "kernel": source.isKernel}
source.stop()
keyboard.close()
"""


def test_whileTheHelperHoldsNothingLocksAreTheXDisplaysAndTakingBackSaysSo(tmp_path: Path):
	value = runKernelChild(tmp_path, BACKGROUND_CHILD)
	assert (value["before"], value["after"]) == (0, 1), (
		f"with the helper holding nothing, NVDA reported Caps Lock from the helper's last word: {value}"
	)
	assert value["said"] == ["linda-keyd holds no keyboard now, and takes it for NVDA as soon as it may"], (
		value
	)


#: Insert, the NVDA key, is pressed and released, and its verdict never reaches the helper, which passes
#: it on 2.5 s later, as after a freeze it forgives, where its bound runs afresh from the thaw; a key is
#: typed meanwhile: whether NVDA still knows the press when the helper's EXPIRED comes.
LATE_EXPIRED_AFTER_RELEASE_CHILD = r"""
source = startSource()
kernel = source._kernel
send = kernel._send


def losingVerdicts(message):
	if message[0] == 0x04:
		return False
	return send(message)


kernel._send = losingVerdicts
keyboard.down("Insert")
time.sleep(0.05)
press = kernel._presses.get(110 + 8)
keyboard.up("Insert")
time.sleep(1.5)
keyboard.press("a")
deadline = time.monotonic() + 3
while not any(what == "expired" for _t, what, *rest in keyboard.events) and time.monotonic() < deadline:
	time.sleep(0.02)
time.sleep(0.3)
kernel._send = send
result = {
	"expired": [rest for _t, what, *rest in keyboard.events if what == "expired"],
	"known": press is not None and press.expired and bool(press.passed),
	"kept": len(kernel._released),
}
source.stop()
keyboard.close()
"""


def test_expiredLongAfterItsPressWasReleasedIsKnown(tmp_path: Path):
	value = runKernelChild(
		tmp_path,
		LATE_EXPIRED_AFTER_RELEASE_CHILD,
		standIn={"verdictTimeout": 2.5, "announcedVerdictTimeout": 0.1, "features": 0, "unansweredSpan": 10},
	)
	assert value["expired"], value
	assert value["known"], f"NVDA had forgotten the press the helper said it passed on: {value}"


#: Twenty keys typed, some NVDA's: how many released presses the kernel source still keeps for a late
#: EXPIRED a second after, by when the helper has echoed heartbeats sent after every answer.
NOTHING_KEPT_CHILD = r"""
source = startSource()
kernel = source._kernel
keyboard.type("abcdefghij")
keyboard.press("Insert+t", "Insert+z", "Shift_L+a", "b", "c")
time.sleep(1.2)
result = {"kept": len(kernel._released), "echoes": len(keyboard.heartbeats)}
source.stop()
keyboard.close()
"""


def test_releasedPressesAreForgottenOnceTheHelperCanSayNothingOfThem(tmp_path: Path):
	value = runKernelChild(tmp_path, NOTHING_KEPT_CHILD)
	assert value["kept"] == 0, value


#: On the X route, twenty thousand changes of a root property, as a desktop makes over weeks: what the
#: guard has left unread on its sockets a second later; then another twenty thousand, and the X source
#: falls silent, as a stopped NVDA does: how long until the guard has freed the keyboard and closed the
#: source's connection, which the source sees as it connects again.
GUARD_UNREAD_CHILD = r"""
import subprocess
source = startSource("x11")
x = source._x
guard = x._guard.pid
filler = app.intern_atom("_LINDA_TEST_FILLER")


def changeRootProperty(times):
	for i in range(times):
		root.change_property(filler, Xatom.CARDINAL, 32, [i])
		if i % 500 == 0:
			app.flush()
	app.sync()


def unread(pid):
	sockets = subprocess.run(["ss", "-xpn"], capture_output=True, text=True, check=True).stdout
	return sorted(int(line.split()[2]) for line in sockets.splitlines() if f"pid={pid}," in line)


changeRootProperty(20000)
time.sleep(1.0)
idle = unread(guard)
changeRootProperty(20000)
connection = x._display
x._beat = lambda now: None
silent = time.monotonic()
deadline = silent + 10
while x._display is connection and time.monotonic() < deadline:
	time.sleep(0.01)
result = {"idle": idle, "freedAfter": time.monotonic() - silent, "again": x._display is not connection}
root.delete_property(filler)
app.sync()
source.stop()
keyboard.close()
"""


def test_guardReadsWhatTheXServerSendsItAndFreesTheKeyboardInTime(tmp_path: Path):
	value = runKernelChild(tmp_path, GUARD_UNREAD_CHILD)
	assert value["idle"] and max(value["idle"]) == 0, (
		f"the guard left on its sockets what the X server sent it: {value}"
	)
	# Its silence of two seconds after the last heartbeat, half a second apart, and its wait for the X
	# server of up to one.
	assert value["again"] and value["freedAfter"] < 3.5, value
