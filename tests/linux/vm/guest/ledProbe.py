"""What a desktop does with a new keyboard's lock lights, and what it allows a plain process that asks
for Orca's key-monitor name: the guest halves of experiments E6 and E12, which the cases named for
the probes run (cases/wlGnomeProbes.py and its siblings for sway, Plasma and the X guest). Each case's
module installs this file as ground-led-probe.

	ledProbe.py probe [--preset none|complement|BITS] [--reader REGEX] [--seconds S]
	ledProbe.py experiment [--reader REGEX] [--seconds S]
	ledProbe.py monitor SOCKET OUT
	ledProbe.py call SOCKET JSON

probe makes a standalone uinput keyboard, "Ground light probe", made as linda-keyd makes its virtual
keyboard (a virtual bus, every key, the helper's five lights, autorepeat), and none of the helper's
or the ground's other keyboards. As soon as it exists it presets its lights, as the helper's light
probe would: to nothing with none, to the opposite of the reference keyboard's with complement, or to
BITS, bit n the kernel's LED code n (Num Lock 1, Caps Lock 2, Scroll Lock 4, Compose 8, Kana 16).
Then for S seconds it records every event the kernel hands it, which for a uinput device is what a
reader of its node wrote to it, the kernel passing a light on only when it changes; the echo of its
own preset, which the kernel hands back as it does a reader's write; and when each process that held
a keyboard open as it started first holds its node open, with its pid, comm and uid. A process whose
comm REGEX finds is the desktop's reader: the compositor, or the X server. It prints one JSON object,
every time on the guest's CLOCK_MONOTONIC and also in seconds from the moment the keyboard was made.
The reference keyboard is QEMU's virtio keyboard, which the desktop lights as it lights every
keyboard of the seat.

experiment runs E6's four probes in a row and prints them as one JSON object: with every light off,
first with no preset and then with the complement, and then with Num Lock on in the desktop, the same
two. Num Lock is pressed on a uinput keyboard of its own, "Ground light presser", made first, and is
pressed again before a probe whenever the reference keyboard has lost it, and at the end to leave the
desktop as it was.

monitor runs in the session as its user: a plain process, no screen reader, which on request asks for
org.gnome.Orca.KeyboardMonitor and calls the compositor's org.freedesktop.a11y.KeyboardMonitor. It
records to OUT, one JSON object a line, each KeyEvent the compositor sends it, with the evdev code and
its name, and each NameAcquired and NameLost of the name; it takes requests on SOCKET, one JSON object
a line, answered with one. call sends one and prints the answer. The requests, by "op":
- "state": its unique name, the name's owner and queue, each with its process, and the manager's.
- "own" with "flags" (default 1, replacement allowed) and "release": RequestName and ReleaseName.
- "watch", "unwatch", "grabKeyboard", "ungrabKeyboard": the method of that name.
- "grabs" with "modifiers", a list of keysyms, and "keystrokes", a list of [keysym, mask]:
  SetKeyGrabs.
- "stranger" with "method" (default WatchKeyboard): the method called on a connection of its own,
  which owns no name.
A call the compositor refuses is answered with the D-Bus error's name and message.
"""

import argparse
import json
import os
import re
import select
import socket
import sys
import threading
import time

import evdev
from evdev import ecodes

PROBE = "Ground light probe"
PRESSER = "Ground light presser"
REFERENCE = "QEMU Virtio Keyboard"
#: What udev's input_id needs to call it a keyboard: every key code below BTN_MISC.
KEYS = [code for code in range(1, ecodes.BTN_MISC) if code in ecodes.KEY]
#: The lights linda-keyd's virtual keyboard has, LEDS in keys.rs.
LEDS = [ecodes.LED_NUML, ecodes.LED_CAPSL, ecodes.LED_SCROLLL, ecodes.LED_COMPOSE, ecodes.LED_KANA]
ALL_LIGHTS = sum(1 << led for led in LEDS)
NUM = 1 << ecodes.LED_NUML
#: BUS_VIRTUAL, as the helper's own; vendor and product are the ground's, so that no configuration
#: for the helper's identifier applies to it.
BUSTYPE = 0x06
VENDOR = 0x4772
PRODUCT = 0x4C50
#: Seconds between two looks for a reader that has opened the probe.
WATCH_INTERVAL = 0.002
#: Seconds the presser is given to be opened and settle, and the reference to show a lock pressed.
PRESSER_SETTLE = 1.0
LOCK_TIMEOUT = 5.0

NAME = "org.gnome.Orca.KeyboardMonitor"
MANAGER = "org.freedesktop.a11y.Manager"
MANAGER_PATH = "/org/freedesktop/a11y/Manager"
MONITOR = "org.freedesktop.a11y.KeyboardMonitor"
DBUS_TIMEOUT = 5000
#: X key codes are evdev codes plus this; the KeyEvent's keycode is an X key code.
KEYCODE_OFFSET = 8


def keyName(code: int) -> str:
	name = ecodes.KEY.get(code, str(code))
	return name[0] if isinstance(name, list) else name


def device(name: str) -> evdev.InputDevice:
	for path in evdev.list_devices():
		found = evdev.InputDevice(path)
		if found.name == name:
			return found
		found.close()
	raise LookupError(f"no input device is named {name!r}")


def lights(name: str) -> int:
	found = device(name)
	try:
		return sum(1 << led for led in found.leds())
	finally:
		found.close()


def holding(pid: int, prefix: str) -> bool:
	try:
		fds = os.listdir(f"/proc/{pid}/fd")
	except OSError:
		return False
	for fd in fds:
		try:
			if os.readlink(f"/proc/{pid}/fd/{fd}").startswith(prefix):
				return True
		except OSError:
			continue
	return False


def readers() -> list[int]:
	"""Every process but this one that holds a keyboard open, which the desktop's reader is among."""
	own = os.getpid()
	return [
		int(entry)
		for entry in os.listdir("/proc")
		if entry.isdigit() and int(entry) != own and holding(int(entry), "/dev/input/event")
	]


def described(pid: int) -> dict:
	try:
		with open(f"/proc/{pid}/comm") as f:
			comm = f.read().strip()
		uid = os.stat(f"/proc/{pid}").st_uid
	except OSError:
		comm, uid = None, None
	return {"pid": pid, "comm": comm, "uid": uid}


def keyboard(name: str) -> evdev.UInput:
	return evdev.UInput(
		{ecodes.EV_KEY: KEYS, ecodes.EV_LED: LEDS, ecodes.EV_REP: []},
		name=name,
		bustype=BUSTYPE,
		vendor=VENDOR,
		product=PRODUCT,
		version=1,
		phys=f"ground/{name}/input0",
	)


def setLights(device: evdev.UInput, bits: int) -> list[dict]:
	written = []
	for led in LEDS:
		value = int(bool(bits & 1 << led))
		device.write(ecodes.EV_LED, led, value)
		written.append({"code": led, "value": value})
	device.syn()
	return written


def drain(device: evdev.UInput) -> list[dict]:
	"""Every event the kernel has handed the device, with its stamp and when it was read."""
	events = []
	while select.select([device.fd], [], [], 0)[0]:
		event = device.read_one()
		if event is None:
			break
		events.append(
			{
				"t": event.timestamp(),
				"read": time.monotonic(),
				"type": event.type,
				"code": event.code,
				"value": event.value,
			}
		)
	return events


def waitOpened(node: str, seconds: float) -> dict | None:
	deadline = time.monotonic() + seconds
	candidates = readers()
	while time.monotonic() < deadline:
		for pid in candidates:
			if holding(pid, node):
				return {**described(pid), "t": time.monotonic()}
		time.sleep(WATCH_INTERVAL)
	return None


def probe(preset: str, reader: str | None, seconds: float) -> dict:
	referenceBefore = lights(REFERENCE)
	bits = {"none": None, "complement": ~referenceBefore & ALL_LIGHTS}.get(preset)
	if bits is None and preset != "none":
		bits = int(preset)
	candidates = readers()
	device = keyboard(PROBE)
	created = time.monotonic()
	node = device.device.path
	# The keyboard's own reading of its node, made to find it, is no reader's.
	device.device.close()
	# What the kernel's own handlers, the console's among them, wrote as the device was registered.
	atCreate = drain(device)
	presetAt = None
	written: list[dict] = []
	echo: list[dict] = []
	if bits is not None:
		presetAt = time.monotonic()
		written = setLights(device, bits)
		echo = drain(device)
	events: list[dict] = []
	opened: dict[int, float] = {}
	deadline = created + seconds
	while time.monotonic() < deadline:
		select.select([device.fd], [], [], WATCH_INTERVAL)
		events += drain(device)
		for pid in candidates:
			if pid not in opened and holding(pid, node):
				opened[pid] = time.monotonic()
	events += drain(device)
	final = lights(PROBE)
	referenceAfter = lights(REFERENCE)
	device.close()

	def since(t: float | None) -> float | None:
		return None if t is None else round(t - created, 4)

	openers = sorted(
		({**described(pid), "t": t, "after": since(t)} for pid, t in opened.items()), key=lambda o: o["t"]
	)
	for event in [*atCreate, *echo, *events]:
		event["after"] = since(event["t"])
		event["readAfter"] = since(event["read"])
	desktop = next(
		(o for o in openers if reader is not None and o["comm"] and re.search(reader, o["comm"])), None
	)
	leds = [e for e in events if e["type"] == ecodes.EV_LED]
	byDesktop = [e for e in leds if desktop is not None and e["t"] >= desktop["t"]]
	changed = 0
	for e in byDesktop:
		was = (bits or 0) & 1 << e["code"]
		if bool(e["value"]) != bool(was):
			changed |= 1 << e["code"]
	return {
		"preset": preset,
		"presetBits": bits,
		"referenceBefore": referenceBefore,
		"referenceAfter": referenceAfter,
		"node": node,
		"created": created,
		"atCreate": atCreate,
		"presetAfter": since(presetAt),
		"written": written,
		"echo": echo,
		# A new device has every light off, so only the lights the preset turns on are changes.
		"echoed": bits is not None
		and sorted((e["code"], e["value"]) for e in echo if e["type"] == ecodes.EV_LED)
		== sorted((w["code"], w["value"]) for w in written if w["value"]),
		"openers": openers,
		"desktop": desktop,
		"events": events,
		"ledsBeforeDesktop": [e for e in leds if desktop is None or e["t"] < desktop["t"]],
		"ledsByDesktop": byDesktop,
		"desktopChangedBits": changed,
		"final": final,
	}


def pressNumLock(presser: evdev.UInput) -> None:
	for value in (1, 0):
		presser.write(ecodes.EV_KEY, ecodes.KEY_NUMLOCK, value)
		presser.syn()
		time.sleep(0.03)


def waitLights(want: int, mask: int) -> bool:
	deadline = time.monotonic() + LOCK_TIMEOUT
	while time.monotonic() < deadline:
		if lights(REFERENCE) & mask == want:
			return True
		time.sleep(0.02)
	return False


def ensureNumLock(presser: evdev.UInput, on: bool) -> list[str]:
	"""Press Num Lock until the reference keyboard shows it as asked; answers what was done."""
	done = []
	for _attempt in range(2):
		if bool(lights(REFERENCE) & NUM) == on:
			return done
		pressNumLock(presser)
		done.append("pressed")
		if waitLights(NUM if on else 0, NUM):
			return done
	done.append("gave up")
	return done


def experiment(reader: str | None, seconds: float) -> dict:
	presser = keyboard(PRESSER)
	presserNode = presser.device.path
	presser.device.close()
	presserOpened = waitOpened(presserNode, 10)
	time.sleep(PRESSER_SETTLE)
	runs = []
	for numLock in (False, True):
		for preset in ("none", "complement"):
			pressed = ensureNumLock(presser, numLock)
			result = probe(preset, reader, seconds)
			result.update(numLock=numLock, numLockPresses=pressed)
			runs.append(result)
			# A desktop that writes lights at the end of a keyboard's life, or late, has done so by now.
			time.sleep(0.3)
	restored = ensureNumLock(presser, False)
	presser.close()
	return {"presserOpened": presserOpened, "runs": runs, "restored": restored}


# The plain process that asks for Orca's key-monitor name.


def monitor(path: str, out: str) -> None:
	import gi

	gi.require_version("Gio", "2.0")
	from gi.repository import Gio, GLib

	bus = Gio.bus_get_sync(Gio.BusType.SESSION)
	log = open(out, "a", buffering=1)  # noqa: SIM115
	logLock = threading.Lock()

	def record(**fields: object) -> None:
		with logLock:
			log.write(json.dumps({"t": time.monotonic(), **fields}) + "\n")

	def onKey(_bus, _sender, _path, _interface, _signal, parameters) -> None:
		released, state, keysym, unichar, keycode = parameters.unpack()
		code = keycode - KEYCODE_OFFSET
		record(
			kind="key",
			released=released,
			state=state,
			keysym=keysym,
			unichar=unichar,
			keycode=keycode,
			code=code,
			key=keyName(code),
			value=0 if released else 1,
		)

	def onName(_bus, _sender, _path, _interface, signal, parameters) -> None:
		record(kind="name", signal=signal, name=parameters.unpack()[0])

	bus.signal_subscribe(None, MONITOR, "KeyEvent", MANAGER_PATH, None, Gio.DBusSignalFlags.NONE, onKey)
	for signal in ("NameAcquired", "NameLost"):
		bus.signal_subscribe(
			"org.freedesktop.DBus",
			"org.freedesktop.DBus",
			signal,
			"/org/freedesktop/DBus",
			NAME,
			Gio.DBusSignalFlags.NONE,
			onName,
		)

	def call(connection, name: str, path_: str, interface: str, method: str, parameters=None) -> dict:
		try:
			answer = connection.call_sync(
				name, path_, interface, method, parameters, None, Gio.DBusCallFlags.NONE, DBUS_TIMEOUT, None
			)
		except GLib.Error as e:
			return {"error": Gio.DBusError.get_remote_error(e) or e.domain, "message": e.message}
		return {"answer": answer.unpack() if answer is not None else None}

	def daemon(method: str, *arguments: object, signature: str = "(s)") -> dict:
		return call(
			bus,
			"org.freedesktop.DBus",
			"/org/freedesktop/DBus",
			"org.freedesktop.DBus",
			method,
			GLib.Variant(signature, arguments) if arguments else None,
		)

	def process(name: str) -> int | None:
		answer = daemon("GetConnectionUnixProcessID", name)
		return answer.get("answer", [None])[0]

	def owner(name: str) -> dict:
		answer = daemon("GetNameOwner", name)
		if "error" in answer:
			return {"owner": None, "error": answer["error"]}
		unique = answer["answer"][0]
		return {"owner": unique, "pid": process(unique)}

	def handle(request: dict) -> dict:
		op = request["op"]
		if op == "state":
			queue = daemon("ListQueuedOwners", NAME).get("answer", [[]])[0]
			return {
				"unique": bus.get_unique_name(),
				"pid": os.getpid(),
				"name": owner(NAME),
				"queue": [{"owner": q, "pid": process(q)} for q in queue],
				"manager": owner(MANAGER),
			}
		if op == "own":
			return daemon("RequestName", NAME, request.get("flags", 1), signature="(su)")
		if op == "release":
			return daemon("ReleaseName", NAME)
		if op in ("watch", "unwatch", "grabKeyboard", "ungrabKeyboard"):
			method = {
				"watch": "WatchKeyboard",
				"unwatch": "UnwatchKeyboard",
				"grabKeyboard": "GrabKeyboard",
				"ungrabKeyboard": "UngrabKeyboard",
			}[op]
			return call(bus, MANAGER, MANAGER_PATH, MONITOR, method)
		if op == "grabs":
			parameters = GLib.Variant(
				"(aua(uu))",
				(request.get("modifiers", []), [tuple(k) for k in request.get("keystrokes", [])]),
			)
			return call(bus, MANAGER, MANAGER_PATH, MONITOR, "SetKeyGrabs", parameters)
		if op == "stranger":
			address = Gio.dbus_address_get_for_bus_sync(Gio.BusType.SESSION, None)
			stranger = Gio.DBusConnection.new_for_address_sync(
				address,
				Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
				| Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
				None,
				None,
			)
			try:
				answer = call(
					stranger, MANAGER, MANAGER_PATH, MONITOR, request.get("method", "WatchKeyboard")
				)
				answer["unique"] = stranger.get_unique_name()
				return answer
			finally:
				stranger.close_sync(None)
		raise ValueError(f"unknown op {op!r}")

	def serve() -> None:
		if os.path.exists(path):
			os.unlink(path)
		server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
		server.bind(path)
		server.listen(1)
		record(kind="ready", unique=bus.get_unique_name())
		while True:
			connection, _address = server.accept()
			with connection, connection.makefile("rwb") as stream:
				try:
					answer = handle(json.loads(stream.readline()))
				except Exception as e:  # noqa: BLE001
					answer = {"failed": repr(e)}
				stream.write(json.dumps(answer).encode() + b"\n")
				stream.flush()

	threading.Thread(target=serve, daemon=True).start()
	GLib.MainLoop().run()


def callMonitor(path: str, text: str) -> int:
	with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
		client.connect(path)
		with client.makefile("rwb") as stream:
			stream.write(json.dumps(json.loads(text)).encode() + b"\n")
			stream.flush()
			answer = stream.readline().decode()
	print(answer, end="")
	return 1 if "failed" in json.loads(answer) else 0


def main() -> int:
	parser = argparse.ArgumentParser(prog="ledProbe.py")
	commands = parser.add_subparsers(dest="command", required=True)
	for name in ("probe", "experiment"):
		command = commands.add_parser(name)
		command.add_argument("--reader")
		command.add_argument("--seconds", type=float, default=3.0)
		if name == "probe":
			command.add_argument("--preset", default="none")
	command = commands.add_parser("monitor")
	command.add_argument("socket")
	command.add_argument("out")
	command = commands.add_parser("call")
	command.add_argument("socket")
	command.add_argument("json")
	arguments = parser.parse_args()
	if arguments.command == "probe":
		print(json.dumps(probe(arguments.preset, arguments.reader, arguments.seconds)))
	elif arguments.command == "experiment":
		print(json.dumps(experiment(arguments.reader, arguments.seconds)))
	elif arguments.command == "monitor":
		monitor(arguments.socket, arguments.out)
	else:
		return callMonitor(arguments.socket, arguments.json)
	return 0


if __name__ == "__main__":
	sys.exit(main())
