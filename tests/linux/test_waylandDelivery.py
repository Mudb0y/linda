"""The delivery watch of a Wayland session: whether the keys Linda passes on reach the desktop. Proved
absent, by sway's listing of the virtual keyboard or by the lock echo, Linda lets go of the keyboard,
says so, and asks the helper again only once it is another or the user takes the keyboard back; proved
present, by a character typed or by sway's listing, it is remembered for the desktop, and the rescue
hint is not said at the next start. The lock echo proves nothing under a remapped Caps Lock, for a light
the desktop never answered, for two presses of one light inside the wait, or once a typed character has
matched. An X session builds no watch.

Most of it runs in a child with the session forced, a stand-in for linda-keyd putting keys out on a
sink whose lights a test can freeze, and for sway's listing a fake sway that answers its IPC; then
Linda itself in the X rig with the session forced, and under the rig's compositors. Run under the rig:

	nix develop . -c tests/linux/rig python3 -m pytest -q tests/linux/test_waylandDelivery.py

and through tests/linux/wayland-gate for the compositors.
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

import harness
import pytest
import test_waylandKeys as waylandKeys
from desktopSession import PREFIX_MARK, DesktopSession
from keydStandIn import StandIn
from test_keys import STARTUP_TIMEOUT, configText
from test_waylandLayoutCheck import WINDOW_TITLE, typeInto
from test_waylandRoute import HINT, LOST, OWNER_SESSION, runRouteChild

ABSENT = (
	"Keys NVDA passes on do not reach the desktop, so NVDA has let go of the keyboard. "
	"Start NVDA again once that is put right."
)
HAS_KEYBOARD_AGAIN = "NVDA has the keyboard again"
#: The identifier sway gives linda-keyd's keyboard, and the one it gives the rig's own.
VIRTUAL_IDENTIFIER = "19561:28260:Linda_virtual_keyboard"
RIG_IDENTIFIER = "0:0:wlr_virtual_keyboard_v1"
#: Typed into a program, and never to be found in a log or in the record.
MARKER = "kqzmxjwv"
KERNEL_ROUTE = "Keys come from the kernel through linda-keyd: linda-keyd admitted NVDA and holds the keyboard"
NOT_HINTED = "The rescue hint is not said: keys passed on have reached this desktop before"


def lindaSays(text: str) -> str:
	return text.replace("NVDA", "Linda")


def today() -> str:
	return time.strftime("%Y-%m-%d")


#: After test_waylandRoute's ROUTE_CHILD: a sink whose lights a test can freeze, as a desktop that no
#: longer reads the virtual keyboard would leave them, and the count of typed characters matched in the
#: test's hands.
DELIVERY_CHILD = r"""
from linuxPlatform.keys import deliveryWatch, keydClient


class FrozenSink(NullSink):
	frozen = None

	def lights(self):
		return self.frozen if self.frozen is not None else super().lights()


matched = [0]
deliveryWatch.layoutMatches = lambda: matched[0]
RECORD = os.environ.get("LINDA_TEST_DELIVERY_RECORD")


def answered():
	keyboard.press("Caps_Lock")
	wait(lambda: source._watch._answered & keydClient.LOCK_CAPS, 3, "the first Caps Lock was not answered")
	time.sleep(0.6)
	sink.frozen = sink.lights()


def startTrusted(**kwargs):
	# As though NVDA's layout came from the desktop's own settings, as GNOME's and KDE's does, under which
	# alone the lock echo is taken.
	source = startRoute(**kwargs)
	deliveryWatch.ECHO_LAYOUTS = (*deliveryWatch.ECHO_LAYOUTS, source.layout.source)
	return source
"""


def runDelivery(tmp_path: Path, code: str, *, env: dict | None = None, timeout: float = 120) -> dict:
	return runRouteChild(tmp_path, DELIVERY_CHILD + code, standIn=None, env=env, timeout=timeout)


# The lock echo.

LOCK_ECHO_CHILD = r"""
sink = FrozenSink(layout="pl")
standIn()
source = startTrusted(consumed=x11.EXTENDED_INSERT)
answered()
keyboard.press("Caps_Lock")
time.sleep(0.7)
first = {"kernel": source.isKernel, "said": list(said), "unanswered": source._watch._unanswered}
second = time.monotonic()
keyboard.press("Caps_Lock")
wait(lambda: not source.isKernel, 3, "the route did not let go")
letGo = time.monotonic() - second
wait(lambda: not keyboard.capturing, 3, "the stand-in still captures")
asked = len(admissions())
before = len(sink.sent)
keyboard.press("a")
time.sleep(0.3)
afterLetGo = [list(e) for e in sink.sent[before:]]
time.sleep(10)
held = {"asked": len(admissions()) - asked, "kernel": source.isKernel, "blocked": source._blocked is not None}
saidBefore = list(said)
source.takeBack()
wait(lambda: source.isKernel, 5, "takeBack did not take the keyboard")
time.sleep(0.3)
result = {
	"first": first,
	"letGo": letGo,
	"afterLetGo": afterLetGo,
	"held": held,
	"said": saidBefore,
	"afterTakeBack": said[len(saidBefore):],
	"blocked": source._blocked,
	"lines": [line for line in logged.lines if "do not reach the desktop" in line or "given up" in line],
}
source.stop()
"""


@pytest.mark.rigSession("x11")
def test_twoUnansweredLockKeysLetGoAndStayLetGo(tmp_path: Path):
	"""Caps Lock answered once, then two that change no light: Linda lets go within the wait of the
	second, says so once, and keys reach the desktop without it; no HELLO for ten seconds, until the
	user takes the keyboard back."""
	value = runDelivery(tmp_path, LOCK_ECHO_CHILD)
	assert value["first"] == {"kernel": True, "said": [HINT], "unanswered": 1}
	assert value["letGo"] <= 0.5 + 0.5, value["letGo"]
	assert value["said"] == [HINT, ABSENT]
	a = 30
	assert value["afterLetGo"] == [[a, True, False], [a, False, False]]
	assert value["held"] == {"asked": 0, "kernel": False, "blocked": True}
	assert value["afterTakeBack"] == [HAS_KEYBOARD_AGAIN]
	assert value["blocked"] is None
	why = "lock keys passed on changed no light, which the desktop has answered before"
	again = "it is asked again once it starts again or the user asks"
	assert value["lines"] == [
		f"Keys NVDA passes on do not reach the desktop: {why}",
		f"NVDA has no keys in this Wayland session: linda-keyd was given up: {why}; {again}",
	]


UNREAD_OPTIONS_CHILD = r"""
sink = FrozenSink(layout="pl")
standIn()
source = startRoute(consumed=x11.EXTENDED_INSERT)
layout = source.layout.source
answered()
for _ in range(4):
	keyboard.press("Caps_Lock")
	time.sleep(0.7)
result = {
	"layout": layout,
	"trusted": layout in getattr(deliveryWatch, "ECHO_LAYOUTS", ()),
	"kernel": source.isKernel,
	"said": said,
	"unanswered": source._watch._unanswered,
	"lines": [line for line in logged.lines if "proves nothing" in line or "do not reach" in line],
}
source.stop()
"""


@pytest.mark.rigSession("x11")
def test_aLayoutWithoutTheDesktopsOptionsTakesNoEcho(tmp_path: Path):
	"""NVDA's layout from the XKB_DEFAULT variables, which cannot hold an option set in the compositor's
	own configuration: Caps Lock answered once, then four that change no light, and Linda keeps the
	keyboard, as it must where such an option makes Caps Lock an Escape."""
	value = runDelivery(tmp_path, UNREAD_OPTIONS_CHILD)
	assert value["layout"] in ("environment", "localed"), value
	assert not value["trusted"]
	assert value["kernel"]
	assert value["said"] == [HINT]
	assert value["unanswered"] == 4
	assert not [line for line in value["lines"] if "do not reach" in line], value["lines"]
	assert value["lines"][0].startswith(f"Key delivery: NVDA's layout comes from {value['layout']},")


TYPED_MATCH_CHILD = r"""
sink = FrozenSink(layout="pl")
standIn()
source = startTrusted(consumed=x11.EXTENDED_INSERT)
answered()
keyboard.press("Caps_Lock")
time.sleep(0.7)
matched[0] += 1
for _ in range(3):
	keyboard.press("Caps_Lock")
	time.sleep(0.7)
result = {
	"kernel": source.isKernel,
	"said": said,
	"state": source._watch.state,
	"unanswered": source._watch._unanswered,
	"since": sum("a character typed has matched since" in line for line in logged.lines),
}
source.stop()
"""


@pytest.mark.rigSession("x11")
def test_aCharacterTypedSinceTheLastAnswerKeepsTheKeyboard(tmp_path: Path):
	value = runDelivery(tmp_path, TYPED_MATCH_CHILD)
	assert value["kernel"]
	assert value["said"] == [HINT]
	# The match is a proof of presence of its own.
	assert value["state"] == "present"
	assert value["unanswered"] == 4
	assert value["since"] == 3


UNANSWERED_LIGHT_CHILD = r"""
sink = FrozenSink(layout="pl")
standIn()
source = startTrusted(consumed=x11.EXTENDED_INSERT)
answered()
# Num Lock's light never answered: twenty of them prove nothing.
for _ in range(20):
	keyboard.press("Num_Lock")
	time.sleep(0.6)
numLock = {"kernel": source.isKernel, "unanswered": source._watch._unanswered}
# Two of Caps Lock inside the wait may cancel: only the second counts.
keyboard.press("Caps_Lock", gap=0)
time.sleep(0.15)
keyboard.press("Caps_Lock")
time.sleep(0.8)
result = {
	"numLock": numLock,
	"twice": {"kernel": source.isKernel, "unanswered": source._watch._unanswered},
	"said": said,
}
source.stop()
"""


@pytest.mark.rigSession("x11")
def test_aLightNeverAnsweredOrTwoPressesInsideTheWaitProveNothing(tmp_path: Path):
	value = runDelivery(tmp_path, UNANSWERED_LIGHT_CHILD)
	assert value["numLock"] == {"kernel": True, "unanswered": 0}
	assert value["twice"] == {"kernel": True, "unanswered": 1}
	assert value["said"] == [HINT]


REMAPPED_CHILD = r"""
sink = FrozenSink(layout="pl", options=OPTIONS)
sink.frozen = 0
standIn()
source = startTrusted(consumed=x11.EXTENDED_INSERT)
environment = source._environment
noted = []
passedLock = environment.onPassedLock
environment.onPassedLock = lambda light, now: noted.append(light) or passedLock(light, now)
# As though the desktop had answered Caps Lock's light before, which leaves the remap alone to hold.
source._watch._answered = keydClient.LOCK_CAPS
before = len(sink.passed())
for _ in range(20):
	keyboard.press("Caps_Lock")
	time.sleep(0.3)
time.sleep(0.8)
result = {
	"noted": noted,
	"kernel": source.isKernel,
	"said": said,
	"capsPassed": sum(1 for evdev, down in sink.passed()[before:] if evdev == code("Caps_Lock") and down),
	"options": environment.layout.names[4],
}
source.stop()
"""


@pytest.mark.rigSession("x11")
@pytest.mark.parametrize("options", ["caps:escape", "ctrl:nocaps"])
def test_twentyRemappedCapsLocksProveNothing(tmp_path: Path, options: str):
	"""Under an option that makes Caps Lock another key, it changes no light in the layout, so no press of
	it is a lock key at all, though it is passed on twenty times and the light was answered before."""
	value = runDelivery(
		tmp_path, f"OPTIONS = {options!r}\n" + REMAPPED_CHILD, env={"XKB_DEFAULT_OPTIONS": options}
	)
	assert value["options"] == options
	assert value["capsPassed"] == 20
	assert value["noted"] == []
	assert value["kernel"]
	assert value["said"] == [HINT]


# What is remembered, and the hint.

RECORD_CHILD = r"""
standIn()
source = startRoute()
time.sleep(0.5)
first = list(said)
matched[0] += 1
wait(lambda: os.path.exists(RECORD), 5, "no proof was remembered")
recorded = open(RECORD).read()
mode = os.stat(RECORD).st_mode & 0o777
source.stop()
runs = {"first": first}
for name, text in (("remembered", None), ("another desktop's", "sway 2026-01-01\n"), ("unreadable", "\xff")):
	if text is not None:
		with open(RECORD, "w", encoding="latin-1") as file:
			file.write(text)
	del said[:]
	source = startRoute()
	time.sleep(0.5)
	runs[name] = list(said)
	source.stop()
layoutBefore = keys.createSource("auto", said.append).layout
source = startRoute()
layout = {"before": layoutBefore, "now": list(source.layout.names), "same": source.layout is source._environment.layout}
source.stop()
result = {
	"runs": runs,
	"recorded": recorded,
	"mode": mode,
	"layout": layout,
	"notHinted": sum(NOT_HINTED in line for line in logged.lines),
}
"""


@pytest.mark.rigSession("x11")
def test_aProofIsRememberedAndTheHintIsThenNotSaid(tmp_path: Path):
	record = tmp_path / "keyDelivery"
	value = runDelivery(
		tmp_path,
		f"NOT_HINTED = {NOT_HINTED!r}\n" + RECORD_CHILD,
		env={"LINDA_TEST_DELIVERY_RECORD": str(record), "XDG_CURRENT_DESKTOP": "river"},
	)
	runs = value["runs"]
	assert runs["first"] == [HINT]
	assert runs["remembered"] == []
	assert runs["another desktop's"] == [HINT]
	assert runs["unreadable"] == [HINT]
	# The desktop's name and the day, and nothing of any key.
	assert value["recorded"] == f"river {today()}\n"
	assert value["mode"] == 0o600
	assert value["notHinted"] == 1
	assert value["layout"] == {"before": None, "now": ["evdev", "pc105", "pl", "", ""], "same": True}


VIRTUAL_CHILD = r"""
class Source:
	isHolding = True


absent = []
recordPath = tempfile.mkdtemp() + "/keyDelivery"


def watching():
	watch = deliveryWatch.DeliveryWatch(
		"sway", lambda source, why: absent.append(why), record=deliveryWatch.Record(recordPath), matches=lambda: 0
	)
	watch.start()
	watch.held(Source())
	return watch


def told(watch, status):
	watch.virtual(status)
	time.sleep(0.3)
	return watch.state


steps = {}
watch = watching()
steps["unseen, never seen"] = (told(watch, keydClient.VIRTUAL_UNSEEN), list(absent))
steps["seen"] = (told(watch, keydClient.VIRTUAL_SEEN), open(recordPath).read())
watch.stop()
watch = watching()
steps["old"] = (told(watch, keydClient.VIRTUAL_OLD), list(absent))
steps["unseen, seen before"] = (told(watch, keydClient.VIRTUAL_UNSEEN), list(absent))
watch.stop()
result = steps
"""


@pytest.mark.rigSession("x11")
def test_theLightProbesWord(tmp_path: Path):
	"""virtual(status), which nothing calls yet: SEEN is a proof, remembered as the probe's; UNSEEN is
	absence only on a desktop the probe saw before; OLD says nothing."""
	value = runDelivery(tmp_path, VIRTUAL_CHILD)
	assert value["unseen, never seen"] == ["unknown", []]
	assert value["seen"] == ["present", f"sway {today()} probed\n"]
	assert value["old"] == ["unknown", []]
	unseen = "the desktop did not write the lights of linda-keyd's keyboard, as it did before"
	assert value["unseen, seen before"] == ["absent", [unseen]]


# sway's listing, from a fake sway.

FAKE_SWAY = r"""
import json, socket, struct

HEADER = struct.Struct("=6sII")
IDENTIFIER = "19561:28260:Linda_virtual_keyboard"


def virtualEntry(events):
	return {
		"identifier": IDENTIFIER,
		"name": "Linda virtual keyboard",
		"vendor": 19561,
		"product": 28260,
		"type": "keyboard",
		"xkb_layout_names": ["Polish"],
		"xkb_active_layout_index": 0,
		"libinput": {"send_events": events},
	}


class FakeSway:
	def __init__(self, path, entry):
		self.entry = entry
		self.lock = threading.Lock()
		self.subscribers = []
		self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
		self.server.bind(path)
		self.server.listen(8)
		threading.Thread(target=self.accept, daemon=True).start()

	def inputs(self):
		own = {
			"identifier": "1:1:AT_Translated_Set_2_keyboard",
			"name": "AT Translated Set 2 keyboard",
			"vendor": 1,
			"product": 1,
			"type": "keyboard",
			"xkb_layout_names": ["Polish"],
			"xkb_active_layout_index": 0,
			"libinput": {"send_events": "enabled"},
		}
		return [e for e in (self.entry, own) if e is not None]

	def accept(self):
		while True:
			try:
				connection, _address = self.server.accept()
			except OSError:
				return
			threading.Thread(target=self.serve, args=(connection,), daemon=True).start()

	def exactly(self, connection, size):
		data = b""
		while len(data) < size:
			chunk = connection.recv(size - len(data))
			if not chunk:
				raise EOFError
			data += chunk
		return data

	def serve(self, connection):
		try:
			while True:
				_magic, size, kind = HEADER.unpack(self.exactly(connection, HEADER.size))
				self.exactly(connection, size)
				if kind == 2:
					with self.lock:
						self.subscribers.append(connection)
					self.send(connection, kind, {"success": True})
				elif kind == 100:
					self.send(connection, kind, self.inputs())
				else:
					self.send(connection, kind, {})
		except (OSError, EOFError):
			pass

	def send(self, connection, kind, payload):
		data = json.dumps(payload).encode()
		with self.lock:
			connection.sendall(HEADER.pack(b"i3-ipc", len(data), kind) + data)

	def change(self, entry, change):
		self.entry = entry
		event = {"change": change, "input": entry or {"identifier": IDENTIFIER, "type": "keyboard"}}
		for connection in list(self.subscribers):
			try:
				self.send(connection, 0x80000015, event)
			except OSError:
				pass


os.environ["LINDA_TEST_DELIVERY_IDENTIFIER"] = IDENTIFIER
swayPath = socketDir + "/sway"
fake = FakeSway(swayPath, None if START == "missing" else virtualEntry(START))
os.environ["SWAYSOCK"] = swayPath


def taken():
	source.takeBack()
	wait(lambda: source.isKernel, 5, "takeBack did not take the keyboard")
	return time.monotonic()


def letGo(since):
	wait(lambda: not source.isKernel, 3, "the route did not let go")
	return round(time.monotonic() - since, 3)
"""

SWAY_CHILD = r"""
standIn()
source = startRoute(waitForKernel=False)
wait(lambda: source.isKernel, 10, "the route did not take the helper's keys")
held = time.monotonic()
steps = {"compositor": displayServer.compositor()}
if START == "enabled":
	wait(lambda: source._watch.state == "present", 3, "no proof of presence")
	steps["present"] = round(time.monotonic() - held, 3)
	steps["recorded"] = open(RECORD).read()
	disabled = time.monotonic()
	fake.change(virtualEntry("disabled"), "libinput_config")
	steps["disabled"] = letGo(disabled)
	steps["saidThen"] = list(said)
	asked = len(admissions())
	time.sleep(3)
	steps["askedWhileBlocked"] = len(admissions()) - asked
	# Taken back while sway still takes no events from it: let go again once sway is asked.
	steps["stillDisabled"] = letGo(taken())
	fake.change(virtualEntry("enabled"), "libinput_config")
	taken()
	time.sleep(2)
	steps["enabledAgain"] = {"kernel": source.isKernel, "state": source._watch.state}
else:
	steps["missing"] = letGo(held)
	steps["saidThen"] = list(said)
result = steps
source.stop()
"""


@pytest.mark.rigSession("x11")
@pytest.mark.parametrize("start", ["enabled", "missing"])
def test_swaysListing(tmp_path: Path, start: str):
	"""sway lists linda-keyd's keyboard with its events on: a proof, remembered. Its events switched off,
	Linda lets go at once, and again a second after the user takes the keyboard back while they are
	still off; switched on again, taken back, it holds. sway not listing it a second after the helper
	holds: Linda lets go."""
	record = tmp_path / "keyDelivery"
	value = runDelivery(
		tmp_path,
		f"START = {start!r}\n" + FAKE_SWAY + SWAY_CHILD,
		env={"LINDA_TEST_DELIVERY_RECORD": str(record)},
	)
	assert value["compositor"] == "sway"
	if start == "enabled":
		assert 0.7 <= value["present"] <= 1.5, value
		assert value["recorded"] == f"sway {today()}\n"
		assert value["disabled"] <= 0.5, value
		assert value["saidThen"] == [HINT, ABSENT]
		assert value["askedWhileBlocked"] == 0
		assert 0.7 <= value["stillDisabled"] <= 1.6, value
		assert value["enabledAgain"] == {"kernel": True, "state": "present"}
	else:
		assert 0.7 <= value["missing"] <= 1.6, value
		assert value["saidThen"] == [HINT, ABSENT]
		assert not record.exists()


BACKGROUND_CHILD = r"""
from keydStandIn import RELEASED_BACKGROUND

PRESENT_LINE = "reach the desktop: sway lists linda-keyd's keyboard with its events on"


def proofs():
	return sum(PRESENT_LINE in line for line in logged.lines)


def holdAgain():
	# The helper captures again by itself once the session is in front again.
	with keyboard._lock:
		keyboard._capture()
	keyboard._wake()
	wait(lambda: source._kernel is kernel and kernel.isHolding, 3, "the helper did not hold again")
	return time.monotonic()


def background(removedFirst):
	# sway, its session paused, takes its devices away, before the helper's word that it let go or after.
	if removedFirst:
		fake.change(None, "removed")
		time.sleep(0.05)
	keyboard.letGo(RELEASED_BACKGROUND)
	wait(lambda: not kernel.isHolding, 3, "the helper's let-go did not come")
	if not removedFirst:
		fake.change(None, "removed")
	time.sleep(1.5)
	return {"kernel": source._kernel is kernel, "blocked": source._blocked is not None}


standIn()
source = startRoute(waitForKernel=False)
wait(lambda: source.isKernel, 10, "the route did not take the helper's keys")
wait(lambda: proofs() == 1, 3, "no proof of presence")
kernel = source._kernel
steps = {}
for removedFirst in (False, True):
	away = background(removedFirst)
	fake.change(virtualEntry("enabled"), "added")
	time.sleep(0.2)
	before = proofs()
	holdAgain()
	wait(lambda: proofs() > before, 3, "no proof of presence after the return")
	steps["removed first" if removedFirst else "let go first"] = {
		"away": away,
		"back": {"kernel": source._kernel is kernel, "isKernel": source.isKernel},
	}
steps["said"] = list(said)
# Back in front while sway lists no keyboard of the helper's yet, nor a second later.
background(False)
steps["stillMissing"] = letGo(holdAgain())
steps["saidThen"] = said[len(steps["said"]):]
steps["admissions"] = len(admissions())
result = steps
source.stop()
"""


@pytest.mark.rigSession("x11")
def test_aConsoleSwitchIsNoWordOnDelivery(tmp_path: Path):
	"""The helper lets go as the session goes to the background, and sway takes every device away:
	whichever comes first, Linda keeps the helper, says nothing, and once the helper holds again sway
	is asked afresh, which proves presence; back in front with sway listing no keyboard of the
	helper's a second later, Linda lets go."""
	value = runDelivery(
		tmp_path,
		"START = 'enabled'\n" + FAKE_SWAY + BACKGROUND_CHILD,
		env={"LINDA_TEST_DELIVERY_RECORD": str(tmp_path / "keyDelivery")},
	)
	for order in ("let go first", "removed first"):
		assert value[order] == {
			"away": {"kernel": True, "blocked": False},
			"back": {"kernel": True, "isKernel": True},
		}, value
	assert value["said"] == [HINT]
	assert 0.7 <= value["stillMissing"] <= 1.6, value
	assert value["saidThen"] == [ABSENT]
	assert value["admissions"] == 1


BROKEN_VIRTUAL_CHILD = r"""
from keydStandIn import RELEASED_VIRTUAL

standIn()
source = startRoute(waitForKernel=False)
wait(lambda: source.isKernel, 10, "the route did not take the helper's keys")
wait(lambda: source._watch.state == "present", 3, "no proof of presence")
# The helper breaks its virtual keyboard: sway reports it gone, then the helper says it let go.
fake.change(None, "removed")
keyboard.letGo(RELEASED_VIRTUAL)
time.sleep(0.5)
# Its next one, made as it is asked again, is taken up by sway.
fake.change(virtualEntry("enabled"), "added")
wait(lambda: len(admissions()) == 2 and source.isKernel, 6, "the route did not take the helper again")
time.sleep(1.5)
result = {"blocked": source._blocked is not None, "isKernel": source.isKernel, "said": list(said)}
source.stop()
"""


@pytest.mark.rigSession("x11")
def test_aBrokenVirtualKeyboardIsALoss(tmp_path: Path):
	"""sway reports the virtual keyboard gone just as the helper breaks it and lets go: a loss, which
	Linda says and recovers from by asking again, not a proof that keys go nowhere."""
	value = runDelivery(
		tmp_path,
		"START = 'enabled'\n" + FAKE_SWAY + BROKEN_VIRTUAL_CHILD,
		env={"LINDA_TEST_DELIVERY_RECORD": str(tmp_path / "keyDelivery")},
	)
	assert value == {"blocked": False, "isKernel": True, "said": [HINT, LOST]}


LATE_WORD_CHILD = r"""
from keydStandIn import RELEASED_BACKGROUND, RELEASED_VIRTUAL

standIn()
source = startRoute()
kernel = source._kernel
steps = {}
# The watch's word comes just as the helper lets go, for a console switch, then as it breaks its
# virtual keyboard.
keyboard.letGo(RELEASED_BACKGROUND)
wait(lambda: not kernel.isHolding, 3, "the helper's let-go did not come")
source._onAbsent(kernel, "a test's word")
time.sleep(0.5)
steps["background"] = {"kernel": source._kernel is kernel, "blocked": source._blocked is not None}
with keyboard._lock:
	keyboard._capture()
keyboard._wake()
wait(lambda: kernel.isHolding, 3, "the helper did not hold again")
keyboard.letGo(RELEASED_VIRTUAL)
wait(lambda: not kernel.isHolding, 3, "the helper's let-go did not come")
source._onAbsent(kernel, "a test's word")
wait(lambda: len(admissions()) == 2 and source.isKernel, 6, "the route did not take the helper again")
steps["virtual"] = {"blocked": source._blocked is not None, "isKernel": source.isKernel}
steps["said"] = list(said)
steps["givenUp"] = sum("a test's word" in line for line in logged.lines if "given up" in line)
result = steps
source.stop()
"""


@pytest.mark.rigSession("x11")
def test_theWatchsWordAsTheHelperLetsGoIsSetAside(tmp_path: Path):
	"""Proved absent for a kernel source the helper no longer holds for, or that is lost already, Linda
	neither blocks the helper nor says the keys go nowhere: a loss is asked again as any is."""
	value = runRouteChild(tmp_path, LATE_WORD_CHILD, standIn=None)
	assert value["background"] == {"kernel": True, "blocked": False}
	assert value["virtual"] == {"blocked": False, "isKernel": True}
	assert value["said"] == [HINT, LOST]
	assert value["givenUp"] == 0


# An X session.

X11_CHILD = r"""
import os, sys
from linuxPlatform import displayServer
import linuxPlatform.keys as keys

os.environ["DISPLAY"] = RIG_DISPLAY
source = keys.createSource("auto", lambda message: None)
source.start(lambda event: True)
time.sleep(0.5)
source.stop()
result = {
	"type": type(source).__name__,
	"watch": hasattr(source, "_watch"),
	"modules": sorted(name for name in sys.modules if name.endswith(("deliveryWatch", "swayLayout"))),
}
"""


@pytest.mark.rigSession("x11")
def test_anX11SessionBuildsNoWatch(tmp_path: Path):
	done = harness.runChild(
		tmp_path,
		f"import time\nRIG_DISPLAY = {os.environ['DISPLAY']!r}\n" + X11_CHILD,
		env=OWNER_SESSION,
		timeout=60,
	)
	assert done.error is None, f"{done.error}\n{done.stderr[-6000:]}"
	assert done.value == {"type": "RoutedKeySource", "watch": False, "modules": []}


# Linda itself, told the session is Wayland while the rig gives it the X display.


@pytest.fixture
def desktop(tmp_path: Path) -> Iterator[DesktopSession]:
	with DesktopSession(tmp_path / "desktop") as session:
		yield session


@pytest.fixture
def standIn(socketDir: Path) -> Iterator[StandIn]:
	"""The stand-in for linda-keyd, putting the keys Linda passes on out on the X display."""
	with StandIn(socketDir / "socket") as keyboard:
		yield keyboard


@pytest.fixture
def socketDir() -> Iterator[Path]:
	# Under the rig's own /tmp: a path under pytest's directory is too long for a socket.
	directory = Path(tempfile.mkdtemp(prefix="keyd", dir="/tmp"))
	try:
		yield directory
	finally:
		shutil.rmtree(directory, ignore_errors=True)


def startForcedLinda(
	desktop: DesktopSession, keyboard: StandIn, tmp_path: Path, record: Path
) -> harness.Launch:
	hooks = tmp_path / f"hooks{len(desktop._launches)}"
	(hooks / "globalPlugins").mkdir(parents=True)
	(hooks / "synthDrivers").symlink_to(harness.HERE / "synthDrivers")
	config = tmp_path / "nvda.ini"
	config.write_text(configText("desktop", 7))
	nvda = desktop.startNVDA(
		config,
		env={
			"LINDA_TEST_HOOKS_DIR": str(hooks),
			"LINDA_TEST_KEYD_SOCKET": keyboard.path,
			"LINDA_TEST_DELIVERY_RECORD": str(record),
			"LINDA_SESSION_TYPE": "wayland",
			"XDG_CURRENT_DESKTOP": "river",
			"XKB_DEFAULT_LAYOUT": "us",
		},
	)
	nvda.waitForLog(KERNEL_ROUTE, STARTUP_TIMEOUT)
	return nvda


def quit(nvda: harness.Launch) -> str:
	nvda.process.terminate()
	nvda.waitForExit(20)
	nvda.assertClean(logNames=("nvda.log",))
	return nvda.text("nvda.log")


@pytest.mark.rigSession("x11")
def test_lindaRemembersATypedProof(desktop: DesktopSession, standIn: StandIn, tmp_path: Path):
	"""Characters typed into GTK 3's entry match Linda's layout, which proves the keys reach the desktop:
	the first start said the hint, and the next, with the proof remembered, does not."""
	record = tmp_path / "keyDelivery"
	desktop.startApp(desktop.fixture("textapp", "gtk3"), title=WINDOW_TITLE)
	nvda = startForcedLinda(desktop, standIn, tmp_path, record)
	desktop.expect(lindaSays(HINT), since=0, timeout=10)
	typeInto(standIn, "entry", MARKER)
	deadline = time.monotonic() + 5
	while not record.exists():
		assert time.monotonic() < deadline, nvda.text("nvda.log")[-4000:]
		time.sleep(0.1)
	first = quit(nvda)
	assert "Keys NVDA passes on reach the desktop: a character typed matched NVDA's layout" in first
	nvda = startForcedLinda(desktop, standIn, tmp_path, record)
	time.sleep(2)
	spoken = desktop.spokenSince(0)
	second = quit(nvda)
	assert lindaSays(HINT) not in spoken, spoken
	assert NOT_HINTED in second
	assert record.read_text() == f"river {today()}\n"
	# The second copy reads the entry, which holds the marker, aloud as it starts: only the first typed it.
	harness.assertMarkerAbsent(MARKER, {"nvda.log": first})


# Under the rig's compositors.


def startOn(linda: "waylandKeys.WaylandLinda", env: dict[str, str]) -> harness.Launch:
	"""Start Linda on the stand-in as WaylandLinda.start does, without waiting for the hint."""
	hooks = linda.directory / f"hooks{len(linda.desktop._launches)}"
	(hooks / "globalPlugins").mkdir(parents=True)
	(hooks / "synthDrivers").symlink_to(harness.HERE / "synthDrivers")
	nvda = linda.desktop.startNVDA(
		waylandKeys.SESSION_CONFIG,
		env={
			"LINDA_TEST_HOOKS_DIR": str(hooks),
			"LINDA_TEST_KEYD_SOCKET": linda.keyboard.path,
			"XKB_DEFAULT_LAYOUT": "pl",
			**env,
		},
	)
	linda.nvda = nvda
	linda.desktop.expect("Key probe" + PREFIX_MARK, since=0, timeout=waylandKeys.STARTUP_BOUND)
	nvda.waitForLog(KERNEL_ROUTE, waylandKeys.STEP_BOUND)
	return nvda


def takeKeyboard() -> tuple[int, str]:
	"""linda --take-keyboard, run in the rig's session as a user would."""
	done = subprocess.run(
		[sys.executable, "-I", "-B", str(harness.LAUNCHER), "--take-keyboard"],
		capture_output=True,
		check=False,
		text=True,
		timeout=60,
		stdin=subprocess.DEVNULL,
	)
	return done.returncode, done.stdout


def admissions(keyboard: StandIn) -> int:
	return sum(1 for event in keyboard.events if event[1] == "admitted")


@pytest.mark.rigSession("sway")
def test_rigSwayNotListingTheKeyboardLetsGo(tmp_path: Path, socketDir: Path):
	"""The rig's sway lists no keyboard of linda-keyd's, as one that never took it up would not: Linda
	lets go a second after the stand-in holds, says so, and keys reach the probe without it; no HELLO for
	ten seconds; linda --take-keyboard takes it, and it lets go again."""
	with waylandKeys.waylandLinda(tmp_path, socketDir) as linda:
		keyboard = linda.keyboard
		nvda = startOn(linda, {"LINDA_TEST_DELIVERY_IDENTIFIER": VIRTUAL_IDENTIFIER})
		keyboard.waitFor(lambda: keyboard.capturing, waylandKeys.STEP_BOUND)
		held = time.monotonic()
		keyboard.waitFor(lambda: not keyboard.capturing, 3)
		letGo = time.monotonic() - held
		linda.desktop.expect(lindaSays(ABSENT), since=0, timeout=3)
		asked = admissions(keyboard)
		since = linda.probeLines()
		linda.desktop.typeText("a")
		linda.probe.waitFor(lambda line: line.startswith("text ") and line.endswith("a"), 5, since)
		time.sleep(10)
		assert admissions(keyboard) == asked
		status, output = takeKeyboard()
		assert status == 0, output
		keyboard.waitFor(lambda: keyboard.capturing, waylandKeys.STEP_BOUND)
		retaken = time.monotonic()
		keyboard.waitFor(lambda: not keyboard.capturing, 3)
		again = time.monotonic() - retaken
		time.sleep(1)
		spoken = linda.desktop.spokenSince(0)
		log = quit(nvda)
	assert letGo <= 1.6, letGo
	assert again <= 1.6, again
	assert spoken.count(lindaSays(ABSENT)) == 2, spoken
	assert spoken.count(lindaSays(HAS_KEYBOARD_AGAIN)) == 1, spoken
	assert (
		log.count("Keys NVDA passes on do not reach the desktop: sway does not list linda-keyd's keyboard")
		== 2
	)


@pytest.mark.rigSession("sway")
def test_rigSwayListingWithoutEventsSaysNothing(tmp_path: Path, socketDir: Path):
	"""The rig's own keyboard, which sway lists with no libinput entry, so with nothing of its events:
	neither proof, and Linda keeps the keyboard."""
	with waylandKeys.waylandLinda(tmp_path, socketDir) as linda:
		keyboard = linda.keyboard
		nvda = startOn(linda, {"LINDA_TEST_DELIVERY_IDENTIFIER": RIG_IDENTIFIER})
		keyboard.waitFor(lambda: keyboard.capturing, waylandKeys.STEP_BOUND)
		time.sleep(3)
		capturing = keyboard.capturing
		log = quit(nvda)
	assert capturing
	assert "do not reach the desktop" not in log
	assert "reach the desktop: sway" not in log


@pytest.mark.rigSession("wayland")
def test_aTypedCharacterProvesDeliveryUnderTheCompositor(tmp_path: Path, socketDir: Path):
	"""The marker typed into the key probe through Linda matches its layout: a proof, remembered for the
	desktop; the next start says no hint."""
	record = tmp_path / "keyDelivery"
	name = "sway" if harness.rigSession() == "sway" else "river"
	env = {"LINDA_TEST_DELIVERY_RECORD": str(record), "XDG_CURRENT_DESKTOP": "river"}
	with waylandKeys.waylandLinda(tmp_path, socketDir) as linda:
		nvda = startOn(linda, env)
		linda.desktop.expect(lindaSays(HINT), since=0, timeout=waylandKeys.STEP_BOUND)
		linda.typeMarker()
		deadline = time.monotonic() + 5
		while not record.exists():
			assert time.monotonic() < deadline, nvda.text("nvda.log")[-4000:]
			time.sleep(0.1)
		first = quit(nvda)
		nvda = startOn(linda, env)
		time.sleep(2)
		spoken = linda.desktop.spokenSince(0)
		second = quit(nvda)
	assert lindaSays(HINT) not in spoken, spoken
	assert NOT_HINTED in second
	assert record.read_text() == f"{name} {today()}\n"
	assert re.search(r"Keys NVDA passes on reach the desktop: a character typed matched", first)
	# As in the X rig, only the first typed it; the second may read the probe's text aloud.
	harness.assertMarkerAbsent(MARKER, {"nvda.log": first})
