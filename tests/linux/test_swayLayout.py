"""The layout sway gives linda-keyd's keyboard, read over sway's IPC: layout descriptions named again
through xkbcommon's registry, the group followed through sway's input events, the notice when that
keyboard's layout is not the user's keyboard's, and whether sway lists it and takes its events.

Most tests run the feed in a child against a stand-in for sway's IPC that lists what sway 1.12's
ipc_json_describe_input lists for devices libinput reads, which the rig cannot have. The tests marked
for sway run the feed against the rig's headless sway, with virtual keyboards of the test's own;
the one test for the X session runs those there. Run under the rig:

	nix develop . -c tests/linux/rig python3 -m pytest tests/linux/test_swayLayout.py
"""

import os
import re
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import harness
import pytest
import waylandKeyboard
from test_desktopLayout import standIn

#: Within which a change must reach onChange while the main thread is kept busy.
CHANGE_BOUND = 0.1
#: Switches each busy test makes.
SWITCHES = 5
DEBUG = 10
NOTICE = "The desktop gives linda-keyd's keyboard another layout than your keyboard."
US_NOTICE = "NVDA cannot find this desktop's keyboard layout, so it reads keys as US English."
LINDA = "19561:28260:Linda_virtual_keyboard"
#: The identifier sway gives every zwp_virtual_keyboard_v1, the only keyboards the rig's sway has.
RIG_KEYBOARD = "0:0:wlr_virtual_keyboard_v1"
#: sway's repeat for a keyboard its configuration says nothing of: 600 ms, then 25 a second.
SWAY_REPEAT = [True, 0.6, 0.04]
#: Every record the module may make.
ALLOWED_RECORDS = [
	re.compile(p)
	for p in (
		r"Keyboard layout from (sway|localed|environment): [\w-]+( [\w-]+)?",
		r"Keyboard layout: xkbcommon's registry has no layout sway calls .+, so localed's layout is taken",
		r"Keyboard layout: sway lists no keyboard with a layout, so localed's layout is taken",
		r"Keyboard layout: sway's IPC connection ended",
		r"Keyboard layout: sway did not list its inputs: \w+",
		r"Keyboard layout: \d+ sway sockets with a running sway, so none is taken",
	)
]

#: Before each child's code: the module, a recording onChange, and a stand-in for sway's IPC.
PRELUDE = r"""
import json, os, socket, struct, tempfile, threading, time
from logHandler import log
log.setLevel(log.DEBUG)
from linuxPlatform.keys import swayLayout

changes = []
HEADER = struct.Struct("=6sII")
EVENT_INPUT = 0x80000015


def onChange(snapshot):
	changes.append((time.monotonic(), threading.current_thread().name, snapshot))


def plain(snapshot):
	repeat = None if snapshot.repeat is None else list(snapshot.repeat)
	return [
		list(snapshot.names), snapshot.group, repeat, snapshot.locksGuess, snapshot.source,
		snapshot.inputMethod, snapshot.notice,
	]


def busy(seconds):
	end = time.monotonic() + seconds
	count = 0
	while time.monotonic() < end:
		count += 1


def feedThreads():
	return [t.name for t in threading.enumerate() if t.name == "lindaKeyboardLayout"]


def keyboard(identifier, names, active=0, libinput=True, sendEvents="enabled", delay=600, rate=25):
	# As sway 1.12's ipc_json_describe_input describes a keyboard.
	vendor, product, name = identifier.split(":", 2)
	entry = {
		"identifier": identifier, "name": name.replace("_", " "), "type": "keyboard",
		"repeat_delay": delay, "repeat_rate": rate, "xkb_layout_names": names,
	}
	if names:
		entry["xkb_active_layout_index"], entry["xkb_active_layout_name"] = active, names[active]
	if libinput:
		entry["libinput"] = {"send_events": sendEvents}
		entry["vendor"], entry["product"] = int(vendor), int(product)
	return entry


LINDA = "19561:28260:Linda_virtual_keyboard"
AT = "1:1:AT_Translated_Set_2_keyboard"
POWER = "0:1:Power_Button"
PROGRAM = "0:0:wlr_virtual_keyboard_v1"
POINTER = {"identifier": "2:7:SynPS/2_Synaptics_TouchPad", "name": "SynPS/2 Synaptics TouchPad", "type": "pointer"}


def swayCommand(text):
	# What swaymsg sends.
	with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
		s.settimeout(5)
		s.connect(os.environ["SWAYSOCK"])
		data = text.encode()
		s.sendall(HEADER.pack(b"i3-ipc", len(data), 0) + data)
		reply = b""
		while len(reply) < HEADER.size:
			reply += s.recv(4096)
		_magic, size, _type = HEADER.unpack(reply[:HEADER.size])
		while len(reply) < HEADER.size + size:
			reply += s.recv(4096)
	results = json.loads(reply[HEADER.size:])
	assert all(r["success"] for r in results), results


class FakeSway:
	# sway's IPC as far as the feed uses it: SUBSCRIBE, GET_INPUTS and input events.

	def __init__(self, inputs):
		self.inputs = inputs
		self.listings = 0
		self.subscribers = []
		self.lock = threading.Lock()
		self.directory = tempfile.mkdtemp(prefix="sway", dir="/tmp")
		self.path = os.path.join(self.directory, "sway-ipc.sock")
		self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
		self.server.bind(self.path)
		self.server.listen()
		threading.Thread(target=self._accept, name="fakeSway", daemon=True).start()

	def _accept(self):
		while True:
			try:
				client, _address = self.server.accept()
			except OSError:
				return
			threading.Thread(target=self._serve, args=(client,), name="fakeSway", daemon=True).start()

	def _exactly(self, client, size):
		data = b""
		while len(data) < size:
			chunk = client.recv(size - len(data))
			if not chunk:
				return None
			data += chunk
		return data

	def _serve(self, client):
		try:
			while True:
				header = self._exactly(client, HEADER.size)
				if header is None:
					return
				_magic, size, kind = HEADER.unpack(header)
				if size:
					self._exactly(client, size)
				if kind == 2:
					with self.lock:
						self.subscribers.append(client)
					self.send(client, 2, {"success": True})
				elif kind == 100:
					with self.lock:
						self.listings += 1
						listing = json.loads(json.dumps(self.inputs))
					self.send(client, 100, listing)
		except OSError:
			pass

	def send(self, client, kind, value):
		data = json.dumps(value).encode()
		with self.lock:
			client.sendall(HEADER.pack(b"i3-ipc", len(data), kind) + data)

	def emit(self, change, entry):
		for client in list(self.subscribers):
			try:
				self.send(client, EVENT_INPUT, {"change": change, "input": entry})
			except OSError:
				# A feed that has stopped.
				self.subscribers.remove(client)

	def hangUp(self):
		for client in list(self.subscribers):
			client.shutdown(socket.SHUT_RDWR)

	def close(self):
		self.server.close()
		os.unlink(self.path)
		os.rmdir(self.directory)


def started(feed):
	snapshot = feed.start(onChange)
	feed.stop()
	return plain(snapshot)

"""


def snap(
	names: tuple[str, str, str, str, str],
	group: int = 0,
	repeat: list | None = SWAY_REPEAT,
	locks: int | None = None,
	source: str = "sway",
	inputMethod: bool = False,
	notice: str | None = None,
) -> list:
	return [list(names), group, repeat, locks, source, inputMethod, notice]


def names(layout: str, variant: str = "", model: str = "pc105") -> tuple[str, str, str, str, str]:
	return ("evdev", model, layout, variant, "")


def noSystemBus() -> dict[str, str | None]:
	return {
		"XKB_DEFAULT_LAYOUT": None,
		"XKB_DEFAULT_VARIANT": None,
		"XKB_DEFAULT_OPTIONS": None,
		"XKB_DEFAULT_MODEL": None,
		"XKB_DEFAULT_RULES": None,
		"SWAYSOCK": None,
	}


def moduleRecords(result) -> list[str]:
	return [
		e["message"]
		for e in result.events
		if e["kind"] == "log" and (e.get("codepath") or "").startswith("linuxPlatform.keys.swayLayout")
	]


def runFeed(tmp_path: Path, code: str, env: dict[str, str | None] | None = None, **clean) -> object:
	result = harness.runChild(
		tmp_path, PRELUDE + code, env={**noSystemBus(), **(env or {})}, recordLevel=DEBUG, timeout=120
	)
	result.assertClean(**clean)
	for message in moduleRecords(result):
		assert any(p.fullmatch(message) for p in ALLOWED_RECORDS), f"an unexpected record: {message!r}"
	return result


def test_descriptionsNameTheirLayouts(tmp_path: Path):
	"""What sway reports is xkb_keymap_layout_get_name of the keymap it compiled, the name the symbols
	file gives its group; each must be found again in the registry, which is experiment E21's second
	half, run over every layout and variant the registry lists."""
	result = runFeed(
		tmp_path,
		r"""
from xkbcommon import xkb
from xkbcommon._ffi import ffi, lib

known = swayLayout.registry()
context = xkb.Context()
nowhere, elsewhere, compiled = [], [], 0
import xml.etree.ElementTree as ElementTree
entries = []
for directory in swayLayout._includePaths():
	main = os.path.join(directory, "rules", "evdev.xml")
	if os.path.isfile(main):
		for path in (main, os.path.join(directory, "rules", "evdev.extras.xml")):
			for layout in ElementTree.parse(path).getroot().iterfind("layoutList/layout"):
				name = layout.findtext("configItem/name")
				entries.append((name, ""))
				entries.extend((name, v.findtext("configItem/name")) for v in layout.iterfind("variantList/variant"))
		break
for layout, variant in entries:
	try:
		keymap = context.keymap_new_from_names(layout=layout, variant=variant)
	except xkb.XKBKeymapCreationFailure:
		continue
	compiled += 1
	# The binding decodes names as ASCII, which some are not.
	description = ffi.string(lib.xkb_keymap_layout_get_name(keymap._keymap, 0)).decode()
	found = known.get(description)
	if found is None:
		nowhere.append([layout, variant, description])
	elif found != (layout, variant):
		elsewhere.append([layout, variant])
result = {
	"named": {d: list(known.get(d) or ()) for d in (
		"Polish", "English (US)", "Polish (programmer Dvorak)", "German (no dead keys)", "Klingon",
	)},
	"entries": len(entries),
	"compiled": compiled,
	"nowhere": nowhere,
	"elsewhere": elsewhere,
}
""",
		ignoring=("stderr",),
	)
	value = result.value
	assert value["named"] == {
		"Polish": ["pl", ""],
		"English (US)": ["us", ""],
		"Polish (programmer Dvorak)": ["pl", "dvp"],
		"German (no dead keys)": ["de", "nodeadkeys"],
		"Klingon": [],
	}
	assert value["entries"] > 700 and value["compiled"] >= value["entries"] - 1
	assert value["nowhere"] == []
	# Sun's layouts in the extras call their first group English (US); two variants share a
	# description with another; Mara is listed under two layouts.
	odd = [e for e in value["elsewhere"] if not e[1].startswith("sun_type")]
	assert sorted(odd) == [["hr", "glagyz"], ["in", "mara"], ["ru", "ruintl_en"]], value["elsewhere"]


def test_perIdentifierConfigurationGivesTheNotice(tmp_path: Path):
	result = runFeed(
		tmp_path,
		r"""
cases = {
	# input 1:1:AT_Translated_Set_2_keyboard xkb_layout pl, and nothing for other keyboards.
	"perIdentifier": [keyboard(LINDA, ["English (US)"]), POINTER, keyboard(AT, ["Polish"]), keyboard(POWER, ["English (US)"])],
	# The same, with linda-keyd's keyboard named as well: the firmware's buttons stay on US.
	"bothNamed": [keyboard(LINDA, ["Polish"]), keyboard(AT, ["Polish"]), keyboard(POWER, ["English (US)"])],
	# input type:keyboard xkb_layout pl,us, then a switch to us.
	"byType": [
		keyboard(LINDA, ["Polish", "English (US)"], 1, delay=250, rate=40),
		keyboard(AT, ["Polish", "English (US)"], 1),
	],
	# Two keyboards of the user's with layouts of their own: one is linda-keyd's.
	"twoKeyboards": [keyboard(LINDA, ["English (US)"]), keyboard(AT, ["Polish"]), keyboard("1133:49970:USB_Keyboard", ["English (US)"])],
	# A program's virtual keyboard is not one linda-keyd holds.
	"programKeyboard": [keyboard(LINDA, ["English (US)"]), keyboard(PROGRAM, ["Polish"], libinput=False), keyboard(AT, ["English (US)"])],
	"variant": [keyboard(LINDA, ["Polish (programmer Dvorak)", "German (no dead keys)"]), keyboard(AT, ["Polish"])],
	"noRepeat": [keyboard(LINDA, ["Polish"], rate=0)],
}
result = {}
for name, inputs in cases.items():
	sway = FakeSway(inputs)
	result[name] = started(swayLayout.SwayLayoutFeed(sway.path))
	sway.close()
result["threads"] = feedThreads()
""",
	)
	assert result.value == {
		"perIdentifier": snap(names("us"), notice=NOTICE),
		"bothNamed": snap(names("pl")),
		"byType": snap(names("pl,us", ","), group=1, repeat=[True, 0.25, 0.025]),
		"twoKeyboards": snap(names("us")),
		"programKeyboard": snap(names("us")),
		"variant": snap(names("pl,de", "dvp,nodeadkeys"), notice=NOTICE),
		"noRepeat": snap(names("pl"), repeat=[False, 0.6, 0.04]),
		"threads": [],
	}


def test_modelAndOptionsAreTheEnvironments(tmp_path: Path):
	result = runFeed(
		tmp_path,
		r"""
sway = FakeSway([keyboard(LINDA, ["Polish"])])
result = started(swayLayout.SwayLayoutFeed(sway.path))
""",
		env={"XKB_DEFAULT_MODEL": "pc104", "XKB_DEFAULT_OPTIONS": "caps:none", "XKB_DEFAULT_LAYOUT": "de"},
	)
	assert result.value == snap(("evdev", "pc104", "pl", "", "caps:none"))


def test_firstKeyboardUntilTheVirtualOneIsListed(tmp_path: Path):
	result = runFeed(
		tmp_path,
		r"""
result = {}
sway = FakeSway([keyboard(POWER, ["English (US)"]), keyboard(AT, ["Polish"])])
feed = swayLayout.SwayLayoutFeed(sway.path)
result["first"] = plain(feed.start(onChange))
virtual = keyboard(LINDA, ["English (US)"])
sway.inputs = [virtual] + sway.inputs
sway.emit("added", virtual)
time.sleep(0.3)
sway.inputs = sway.inputs[1:]
sway.emit("removed", virtual)
time.sleep(0.3)
feed.stop()
result["changes"] = [[name, plain(s)] for _t, name, s in changes]
sway.close()
sway = FakeSway([keyboard(PROGRAM, ["German (no dead keys)"], libinput=False)])
result["onlyAProgram"] = started(swayLayout.SwayLayoutFeed(sway.path))
sway.close()
sway = FakeSway([keyboard(PROGRAM, [], libinput=False), POINTER])
result["noLayout"] = started(swayLayout.SwayLayoutFeed(sway.path))
sway.close()
""",
		env={"XKB_DEFAULT_LAYOUT": "cz"},
	)
	value = result.value
	assert value["first"] == snap(names("pl"))
	assert value["changes"] == [
		["lindaKeyboardLayout", snap(names("us"), notice=NOTICE)],
		["lindaKeyboardLayout", snap(names("pl"))],
	]
	assert value["onlyAProgram"] == snap(names("de", "nodeadkeys"))
	# With no system bus in the rig, localed's layout is the environment's.
	assert value["noLayout"] == snap(names("cz"), repeat=None, source="environment")
	assert (
		"Keyboard layout: sway lists no keyboard with a layout, so localed's layout is taken"
		in moduleRecords(result)
	)


def test_groupChangesReachOnChangeWhileTheMainThreadIsBusy(tmp_path: Path):
	result = runFeed(
		tmp_path,
		rf"""
virtual = keyboard(LINDA, ["Polish", "English (US)"])
other = keyboard(AT, ["Polish", "English (US)"])
sway = FakeSway([virtual, other])
feed = swayLayout.SwayLayoutFeed(sway.path)
first = plain(feed.start(onChange))
asked = []
for turn in range({SWITCHES}):
	virtual = keyboard(LINDA, ["Polish", "English (US)"], 1 - turn % 2)
	sway.inputs = [virtual, other]
	asked.append(time.monotonic())
	sway.emit("xkb_layout", virtual)
	busy(0.3)
listings = sway.listings
# Another keyboard's group, and a change of the virtual one's send-events mode, change nothing.
sway.emit("xkb_layout", keyboard(AT, ["Polish", "English (US)"], 1))
sway.emit("libinput_config", virtual)
time.sleep(0.3)
feed.stop()
sway.emit("xkb_layout", keyboard(LINDA, ["Polish", "English (US)"], 0))
time.sleep(0.3)
result = {{
	"first": first,
	"changes": [[t, name, plain(s)] for t, name, s in changes],
	"asked": asked,
	"listings": [listings, sway.listings],
	"threads": feedThreads(),
}}
""",
	)
	value = result.value
	assert value["first"] == snap(names("pl,us", ","))
	expected = [snap(names("pl,us", ","), group=1 - turn % 2) for turn in range(SWITCHES)]
	assert [c[1:] for c in value["changes"]] == [["lindaKeyboardLayout", s] for s in expected]
	delays = [c[0] - t for c, t in zip(value["changes"], value["asked"])]
	print("seconds from each change to onChange:", " ".join(f"{d:.4f}" for d in delays))
	assert max(delays) < CHANGE_BOUND, delays
	# A group's event is applied as it came; only the libinput_config event asked for a listing.
	assert value["listings"] == [1, 2]
	assert value["threads"] == []


def test_unknownDescriptionTakesLocaled(tmp_path: Path):
	code = r"""
result = {}
for name, inputs in {
	"unknown": [keyboard(LINDA, ["Klingon"])],
	"oneOfTwo": [keyboard(LINDA, ["Polish", "Klingon"], 1)],
}.items():
	sway = FakeSway(inputs)
	result[name] = started(swayLayout.SwayLayoutFeed(sway.path))
	sway.close()
sway = FakeSway([keyboard(LINDA, ["Klingon"]), keyboard(AT, ["Polish"])])
result["withNotice"] = started(swayLayout.SwayLayoutFeed(sway.path))
sway.close()
"""
	systemBus = {"DBUS_SYSTEM_BUS_ADDRESS": os.environ["DBUS_SESSION_BUS_ADDRESS"]}
	with standIn("localed", "de,us", "pc104", "nodeadkeys,", ""):
		result = runFeed(tmp_path, code, env=systemBus)
	localed = ("evdev", "pc104", "de,us", "nodeadkeys,", "")
	assert result.value == {
		"unknown": snap(localed, source="localed"),
		"oneOfTwo": snap(localed, group=1, source="localed"),
		"withNotice": snap(localed, source="localed", notice=NOTICE),
	}
	records = moduleRecords(result)
	assert (
		"Keyboard layout: xkbcommon's registry has no layout sway calls Klingon, so localed's layout is taken"
		in records
	)
	noBus = runFeed(tmp_path, code)
	assert noBus.value["unknown"] == snap(names("us"), source="environment", notice=US_NOTICE)


def test_virtualKeyboardState(tmp_path: Path):
	result = runFeed(
		tmp_path,
		r"""
result = {}
for name, inputs in {
	"enabled": [keyboard(AT, ["Polish"]), keyboard(LINDA, ["Polish"])],
	"disabled": [keyboard(LINDA, ["Polish"], sendEvents="disabled")],
	"missing": [keyboard(AT, ["Polish"]), POINTER],
	"noLibinput": [keyboard(LINDA, ["Polish"], libinput=False)],
	"unknownMode": [keyboard(LINDA, ["Polish"], sendEvents="unknown")],
}.items():
	sway = FakeSway(inputs)
	state = swayLayout.virtualKeyboard(sway.path)
	result[name] = None if state is None else [state.listed, state.eventsEnabled]
	sway.close()
result["noSocket"] = swayLayout.virtualKeyboard("/tmp/no-sway-here.sock")
result["noSway"] = swayLayout.virtualKeyboard()
sway = FakeSway([keyboard(LINDA, ["Polish"])])
os.environ["SWAYSOCK"] = sway.path
result["fromSwaysock"] = list(swayLayout.virtualKeyboard())
sway.close()
""",
	)
	assert result.value == {
		"enabled": [True, True],
		"disabled": [True, False],
		"missing": [False, None],
		"noLibinput": [True, None],
		"unknownMode": [True, None],
		"noSocket": None,
		"noSway": None,
		"fromSwaysock": [True, True],
	}
	assert "Keyboard layout: sway did not list its inputs: FileNotFoundError" in moduleRecords(result)


def test_createOnlyInASwaySession(tmp_path: Path):
	result = runFeed(
		tmp_path,
		r"""
import shutil
from linuxPlatform.keys import desktopLayout

# Short enough for a socket's path, and with no sway's socket in it yet.
runtime = os.environ["XDG_RUNTIME_DIR"] = tempfile.mkdtemp(prefix="run", dir="/tmp")

def kind(desktop):
	return type(swayLayout.create(desktop)).__name__

result = {"noSway": {d: kind(d) for d in ("GNOME", "KDE", "sway", "", None)}}
sway = FakeSway([keyboard(LINDA, ["Polish"])])
os.environ["SWAYSOCK"] = sway.path
result["swaysock"] = {d: kind(d) for d in ("GNOME", "sway", None)}
result["desktopLayout"] = started(desktopLayout.create("sway"))
os.environ["SWAYSOCK"] = "/tmp/no-sway-here.sock"
result["swaysockGone"] = kind("sway")
del os.environ["SWAYSOCK"]
# Where sway puts its socket for a session that does not export SWAYSOCK.
os.symlink(sway.path, os.path.join(runtime, f"sway-ipc.{os.getuid()}.{os.getpid()}.sock"))
result["found"] = [kind("sway"), kind("GNOME"), started(swayLayout.create("sway"))]
os.symlink(sway.path, os.path.join(runtime, f"sway-ipc.{os.getuid()}.1.sock"))
result["two"] = kind("sway")
os.unlink(os.path.join(runtime, f"sway-ipc.{os.getuid()}.{os.getpid()}.sock"))
result["otherAlive"] = kind("sway")
os.symlink(sway.path, os.path.join(runtime, f"sway-ipc.{os.getuid()}.999999999.sock"))
result["deadOneIgnored"] = kind("sway")
sway.close()
shutil.rmtree(runtime)
""",
	)
	value = result.value
	assert value["noSway"] == dict.fromkeys(("GNOME", "KDE", "sway", "", "null"), "NoneType")
	assert value["swaysock"] == dict.fromkeys(("GNOME", "sway", "null"), "SwayLayoutFeed")
	assert value["desktopLayout"] == snap(names("pl"))
	assert value["swaysockGone"] == "NoneType"
	assert value["found"] == ["SwayLayoutFeed", "NoneType", snap(names("pl"))]
	assert value["two"] == "NoneType"
	# Process 1 is the sandbox's own, alive, so its socket alone is left.
	assert value["otherAlive"] == "SwayLayoutFeed"
	assert value["deadOneIgnored"] == "SwayLayoutFeed"
	assert "Keyboard layout: 2 sway sockets with a running sway, so none is taken" in moduleRecords(result)


def test_feedEnds(tmp_path: Path):
	result = runFeed(
		tmp_path,
		r"""
result = {}
sway = FakeSway([keyboard(LINDA, ["Polish", "English (US)"])])
feed = swayLayout.SwayLayoutFeed(sway.path)
feed.start(onChange)
sway.hangUp()
time.sleep(0.3)
result["afterHangUp"] = feedThreads()
feed.stop()
sway.close()
sway = FakeSway([keyboard(LINDA, ["Polish", "English (US)"])])
feed = swayLayout.SwayLayoutFeed(sway.path)
feed.start(onChange)
feed.stop()
sway.emit("xkb_layout", keyboard(LINDA, ["Polish", "English (US)"], 1))
time.sleep(0.3)
result["afterStop"] = feedThreads()
result["changes"] = len(changes)
try:
	feed.start(onChange)
except RuntimeError as e:
	result["again"] = str(e)
sway.close()
# A feed that never started stops quietly, as desktopLayout's fallback asks of one.
swayLayout.SwayLayoutFeed(sway.path).stop()
try:
	swayLayout.SwayLayoutFeed(sway.path).start(onChange)
except OSError as e:
	result["noSway"] = type(e).__name__
""",
	)
	assert result.value == {
		"afterHangUp": [],
		"afterStop": [],
		"changes": 0,
		"again": "the layout feed has started already",
		"noSway": "FileNotFoundError",
	}
	assert "Keyboard layout: sway's IPC connection ended" in moduleRecords(result)


@contextmanager
def rigKeyboard(layout: str, variant: str = "") -> Iterator[None]:
	"""A virtual keyboard of the test's own in the rig's sway, with this keymap; sway lists it first,
	before the injector's."""
	from pywayland.protocol.wayland import WlSeat
	from xkbcommon import xkb

	protocol = waylandKeyboard.loadProtocol(
		os.environ["LINDA_VIRTUAL_KEYBOARD_PROTOCOL"], "virtual_keyboard_unstable_v1"
	)
	display, registry, found = waylandKeyboard.connectWayland()
	try:
		manager = protocol.ZwpVirtualKeyboardManagerV1
		seatName, seatVersion = found["wl_seat"]
		seat = registry.bind(seatName, WlSeat, min(seatVersion, 7))
		keyboard = registry.bind(found[manager.name][0], manager, 1).create_virtual_keyboard(seat)
		text = xkb.Context().keymap_new_from_names(layout=layout, variant=variant).get_as_string()
		fd = waylandKeyboard._keymapFile(text)
		keyboard.keymap(waylandKeyboard.WL_KEYMAP_FORMAT_XKB_V1, fd, len(text.encode()) + 1)
		os.close(fd)
		display.roundtrip()
		yield
		keyboard.destroy()
		display.roundtrip()
	finally:
		display.disconnect()


RIG_FEED = rf"""
feed = swayLayout.SwayLayoutFeed(os.environ["SWAYSOCK"], identifier={RIG_KEYBOARD!r})
"""


@pytest.mark.rigSession("sway")
@pytest.mark.parametrize(
	("layout", "variant", "expected"),
	[
		("pl,us", "", names("pl,us", ",")),
		("pl,de", "dvp,nodeadkeys", names("pl,de", "dvp,nodeadkeys")),
		("us", "", names("us")),
	],
)
def test_swayNamesItsLayouts(tmp_path: Path, layout: str, variant: str, expected: tuple):
	with rigKeyboard(layout, variant):
		result = runFeed(
			tmp_path,
			RIG_FEED
			+ r"""
result = started(feed)
""",
			env={"SWAYSOCK": os.environ["SWAYSOCK"]},
		)
	assert result.value == snap(expected)


@pytest.mark.rigSession("sway")
def test_swaySwitchReachesOnChangeWhileTheMainThreadIsBusy(tmp_path: Path):
	with rigKeyboard("pl,us"):
		result = runFeed(
			tmp_path,
			RIG_FEED
			+ rf"""
import subprocess
first = plain(feed.start(onChange))
asked = []
for turn in range({SWITCHES}):
	asked.append(time.monotonic())
	swayCommand(f"input {RIG_KEYBOARD} xkb_switch_layout {{1 - turn % 2}}")
	busy(0.3)
# And through swaymsg itself, timed from before its process starts.
for index in (0, 1):
	asked.append(time.monotonic())
	process = subprocess.Popen(["swaymsg", "input", {RIG_KEYBOARD!r}, "xkb_switch_layout", str(index)], stdout=subprocess.DEVNULL)
	busy(0.3)
	assert process.wait(5) == 0
feed.stop()
result = {{
	"first": first,
	"changes": [[t, name, plain(s)] for t, name, s in changes],
	"asked": asked,
	"threads": feedThreads(),
}}
""",
			env={"SWAYSOCK": os.environ["SWAYSOCK"]},
		)
	value = result.value
	assert value["first"] == snap(names("pl,us", ","))
	groups = [1 - turn % 2 for turn in range(SWITCHES)] + [0, 1]
	assert [c[1:] for c in value["changes"]] == [
		["lindaKeyboardLayout", snap(names("pl,us", ","), group=g)] for g in groups
	]
	delays = [c[0] - t for c, t in zip(value["changes"], value["asked"])]
	print("seconds from each change to onChange:", " ".join(f"{d:.4f}" for d in delays))
	assert max(delays) < CHANGE_BOUND, delays
	assert value["threads"] == []


@pytest.mark.rigSession("sway")
@pytest.mark.parametrize(("layout", "notice"), [("us", NOTICE), ("pl", None)])
def test_swayNoticeWhenTheVirtualKeyboardIsNotTheUsers(tmp_path: Path, layout: str, notice: str | None):
	"""Every keyboard in the rig's sway is a program's, of one identifier, and sway gives each the
	keymap its program uploads; so the injector's pl keyboard stands for the one linda-keyd holds, and
	the test's keyboard for linda-keyd's, on the keymap a configuration would have given it."""
	with rigKeyboard(layout):
		result = runFeed(
			tmp_path,
			rf"""
feed = swayLayout.SwayLayoutFeed(os.environ["SWAYSOCK"], identifier={RIG_KEYBOARD!r}, isHeld=lambda entry: True)
result = started(feed)
""",
			env={"SWAYSOCK": os.environ["SWAYSOCK"]},
		)
	assert result.value == snap(names(layout), notice=notice)


@pytest.mark.rigSession("sway")
def test_swayListsTheVirtualKeyboard(tmp_path: Path):
	result = runFeed(
		tmp_path,
		rf"""
result = [list(swayLayout.virtualKeyboard()), list(swayLayout.virtualKeyboard(identifier={RIG_KEYBOARD!r}))]
""",
		env={"SWAYSOCK": os.environ["SWAYSOCK"]},
	)
	# A program's virtual keyboard is no device of libinput's, so sway says nothing of its events.
	assert result.value == [[False, None], [True, None]]


@pytest.mark.rigSession("x11")
def test_underSway(tmp_path: Path) -> None:
	env = {
		name: value
		for name, value in os.environ.items()
		if not name.startswith(("LINDA_RIG_SESSION", "LINDA_RIG_WM", "LINDA_RIG_XWAYLAND"))
	}
	env.update(LINDA_RIG_COMPOSITOR="sway")
	ran = subprocess.run(
		[
			str(harness.HERE / "rig"),
			"--bind",
			str(tmp_path),
			sys.executable,
			"-m",
			"pytest",
			"-q",
			"-p",
			"no:cacheprovider",
			f"--basetemp={tmp_path / 'pt'}",
			"-m",
			"rigSession",
			__file__,
		],
		env=env,
		capture_output=True,
		text=True,
		timeout=300,
		check=False,
	)
	assert ran.returncode == 0, ran.stdout[-6000:] + ran.stderr[-3000:]
	assert " passed" in ran.stdout and " failed" not in ran.stdout, ran.stdout[-3000:]
