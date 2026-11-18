"""A second keyboard, made inside the guest, driven by the test script.

The framework's own key sending presses the guest's emulated keyboard, one chord at a time, with the
monitor's timing. This keyboard is for what that cannot do: a second keyboard beside the first, one
that vanishes with keys held, and keys at exact intervals, since a whole sequence runs here in the
guest and not over the test framework's shell.

	fakeKeyboard.py serve NAME [uinput|uhid]
	fakeKeyboard.py call NAME JSON

A uinput keyboard is made through /dev/uinput, as a remapper makes its own: the kernel gives it no
parent device, and the key capture helper never takes it. A uhid keyboard is a HID boot keyboard made
through /dev/uhid, as BlueZ makes a Bluetooth keyboard: the kernel's HID layer reads its reports, so
it is a keyboard with a parent like any plugged in, which the helper takes. It keeps up to six keys
and the eight modifiers down at once, and records what the kernel sets its lights to as records with
"leds", bit n the kernel's LED code n.

serve makes the keyboard, writes its event node to DIRECTORY/NAME.ready, and takes requests on
DIRECTORY/NAME.sock. A request is a list of steps, run in order: {"press": KEY}, {"release": KEY},
{"tap": KEY, "hold": SECONDS}, {"chord": [KEY, ...], "hold": SECONDS}, which presses the keys in
order and releases them in reverse, {"sleep": SECONDS}, and {"vanish": true}, which destroys the
keyboard at once, with whatever it holds down, and ends serve. KEY is an evdev name such as KEY_T.
The answer lists every event written, with the monotonic time it was written at, and each one is
also appended to DIRECTORY/NAME.jsonl.

It runs as root: /dev/uinput and /dev/uhid are root's alone.
"""

import glob
import json
import os
import socket
import struct
import sys
import threading
import time

import evdev
from evdev import ecodes

DIRECTORY = "/tmp/ground/keyboards"
#: What udev's input_id needs to call it a keyboard: every key code below BTN_MISC.
KEYS = [code for code in range(1, ecodes.BTN_MISC) if code in ecodes.KEY]
LEDS = [ecodes.LED_NUML, ecodes.LED_CAPSL, ecodes.LED_SCROLLL]
#: An ordinary USB keyboard, not a virtual one, so that a helper that tells the two apart takes it.
BUSTYPE = ecodes.BUS_USB
VENDOR = 0x1D50
PRODUCT = 0x6122

DEFAULT_HOLD = 0.03

# uhid's events, from linux/uhid.h.
UHID_DESTROY = 1
UHID_OUTPUT = 6
UHID_GET_REPORT = 9
UHID_GET_REPORT_REPLY = 10
UHID_CREATE2 = 11
UHID_INPUT2 = 12
UHID_SET_REPORT = 13
UHID_SET_REPORT_REPLY = 14
#: More than struct uhid_event, which a read must have room for.
UHID_READ_SIZE = 8192

#: The boot keyboard of the HID specification's appendix B.1: the modifiers as eight bits, a byte
#: kept, six key usages from 0 to 101, and five lights in an output report.
BOOT_KEYBOARD = bytes.fromhex(
	"05010906a101050719e029e71500250175019508810295017508810195057501"
	"050819012905910295017503910195067508150025650507190029658100c0"
)
#: The kernel's hid_keyboard table from drivers/hid/hid-input.c, usages 0 to 101: the key code of
#: each usage.
HID_KEYBOARD = [
	*(0, 0, 0, 0, 30, 48, 46, 32, 18, 33, 34, 35, 23, 36, 37, 38),
	*(50, 49, 24, 25, 16, 19, 31, 20, 22, 47, 17, 45, 21, 44, 2, 3),
	*(4, 5, 6, 7, 8, 9, 10, 11, 28, 1, 14, 15, 57, 12, 13, 26),
	*(27, 43, 43, 39, 40, 41, 51, 52, 53, 58, 59, 60, 61, 62, 63, 64),
	*(65, 66, 67, 68, 87, 88, 99, 70, 119, 110, 102, 104, 111, 107, 109, 106),
	*(105, 108, 103, 69, 98, 55, 74, 78, 96, 79, 80, 81, 75, 76, 77, 71),
	*(72, 73, 82, 83, 86, 127),
]
#: The modifier bits, from usage 0xE0 on.
HID_MODIFIERS = [
	ecodes.KEY_LEFTCTRL,
	ecodes.KEY_LEFTSHIFT,
	ecodes.KEY_LEFTALT,
	ecodes.KEY_LEFTMETA,
	ecodes.KEY_RIGHTCTRL,
	ecodes.KEY_RIGHTSHIFT,
	ecodes.KEY_RIGHTALT,
	ecodes.KEY_RIGHTMETA,
]
USAGES = {code: usage for usage, code in reversed(list(enumerate(HID_KEYBOARD))) if code}


def keyName(code: int) -> str:
	name = ecodes.KEY.get(code, str(code))
	return name[0] if isinstance(name, list) else name


class Keyboard:
	def __init__(self, name: str) -> None:
		self.name = name
		self.log = open(os.path.join(DIRECTORY, f"{name}.jsonl"), "a", buffering=1)  # noqa: SIM115
		self.logLock = threading.Lock()

	def record(self, record: dict) -> None:
		with self.logLock:
			self.log.write(json.dumps(record) + "\n")

	def node(self) -> str:
		raise NotImplementedError

	def send(self, code: int, value: int) -> None:
		raise NotImplementedError

	def destroy(self) -> None:
		raise NotImplementedError

	def write(self, code: int, value: int, written: list[dict]) -> None:
		# The moment it is handed to the kernel, which stamps it during the write.
		record = {"t": time.monotonic(), "code": code, "key": keyName(code), "value": value}
		self.send(code, value)
		self.record(record)
		written.append(record)

	def run(self, steps: list[dict]) -> tuple[list[dict], bool]:
		written: list[dict] = []
		for step in steps:
			if "press" in step:
				self.write(ecodes.ecodes[step["press"]], 1, written)
			elif "release" in step:
				self.write(ecodes.ecodes[step["release"]], 0, written)
			elif "tap" in step:
				code = ecodes.ecodes[step["tap"]]
				self.write(code, 1, written)
				time.sleep(step.get("hold", DEFAULT_HOLD))
				self.write(code, 0, written)
			elif "chord" in step:
				codes = [ecodes.ecodes[key] for key in step["chord"]]
				for code in codes:
					self.write(code, 1, written)
					time.sleep(0.01)
				time.sleep(step.get("hold", DEFAULT_HOLD))
				for code in reversed(codes):
					self.write(code, 0, written)
					time.sleep(0.01)
			elif "sleep" in step:
				time.sleep(step["sleep"])
			elif step.get("vanish"):
				self.destroy()
				self.record({"t": time.monotonic(), "vanished": True})
				return written, True
			else:
				raise ValueError(f"unknown step {step!r}")
		return written, False


class UinputKeyboard(Keyboard):
	def __init__(self, name: str) -> None:
		super().__init__(name)
		self.device = evdev.UInput(
			{ecodes.EV_KEY: KEYS, ecodes.EV_LED: LEDS, ecodes.EV_REP: []},
			name=name,
			bustype=BUSTYPE,
			vendor=VENDOR,
			product=PRODUCT,
			version=1,
			phys=f"ground/{name}/input0",
		)

	def node(self) -> str:
		return self.device.device.path

	def send(self, code: int, value: int) -> None:
		self.device.write(ecodes.EV_KEY, code, value)
		self.device.syn()

	def destroy(self) -> None:
		self.device.close()


class UhidKeyboard(Keyboard):
	def __init__(self, name: str) -> None:
		super().__init__(name)
		self.phys = f"ground/{name}/hid0"
		self.fd = os.open("/dev/uhid", os.O_RDWR | os.O_CLOEXEC)
		self.modifiers = 0
		self.down: list[int] = []
		request = struct.pack(
			"<I128s64s64sHHIIII",
			UHID_CREATE2,
			name.encode(),
			self.phys.encode(),
			b"",
			len(BOOT_KEYBOARD),
			BUSTYPE,
			VENDOR,
			PRODUCT,
			1,
			0,
		)
		os.write(self.fd, request + BOOT_KEYBOARD)
		threading.Thread(target=self.serveKernel, daemon=True).start()
		self.eventNode = self.findNode()

	def findNode(self) -> str:
		deadline = time.monotonic() + 10
		while time.monotonic() < deadline:
			for phys in glob.glob("/sys/class/input/input*/phys"):
				with open(phys) as f:
					if f.read().strip() != self.phys:
						continue
				events = glob.glob(os.path.join(os.path.dirname(phys), "event*"))
				if events:
					return "/dev/input/" + os.path.basename(events[0])
			time.sleep(0.02)
		raise RuntimeError(f"no event node appeared for {self.name}")

	def serveKernel(self) -> None:
		"""Answers the kernel's questions, which would otherwise wait out a timeout, and records what
		it sets the lights to."""
		while True:
			try:
				event = os.read(self.fd, UHID_READ_SIZE)
			except OSError:
				return
			kind = struct.unpack_from("<I", event)[0]
			if kind == UHID_OUTPUT:
				data = event[4 : 4 + 4096]
				size = struct.unpack_from("<H", event, 4 + 4096)[0]
				if size:
					self.record({"t": time.monotonic(), "leds": data[0]})
			elif kind == UHID_GET_REPORT:
				request = struct.unpack_from("<I", event, 4)[0]
				os.write(self.fd, struct.pack("<IIHH", UHID_GET_REPORT_REPLY, request, 5, 0))
			elif kind == UHID_SET_REPORT:
				request = struct.unpack_from("<I", event, 4)[0]
				os.write(self.fd, struct.pack("<IIH", UHID_SET_REPORT_REPLY, request, 0))

	def node(self) -> str:
		return self.eventNode

	def send(self, code: int, value: int) -> None:
		if code in HID_MODIFIERS:
			bit = 1 << HID_MODIFIERS.index(code)
			self.modifiers = self.modifiers | bit if value else self.modifiers & ~bit
		elif code not in USAGES:
			raise ValueError(f"a HID boot keyboard has no {keyName(code)}")
		elif value and code not in self.down:
			if len(self.down) == 6:
				raise ValueError("a HID boot keyboard holds six keys at most")
			self.down.append(code)
		elif not value and code in self.down:
			self.down.remove(code)
		usages = [USAGES[c] for c in self.down] + [0] * (6 - len(self.down))
		report = bytes([self.modifiers, 0, *usages])
		os.write(self.fd, struct.pack("<IH", UHID_INPUT2, len(report)) + report)

	def destroy(self) -> None:
		os.write(self.fd, struct.pack("<I", UHID_DESTROY))
		os.close(self.fd)


def make(name: str, kind: str) -> Keyboard:
	os.makedirs(DIRECTORY, exist_ok=True)
	return (UhidKeyboard if kind == "uhid" else UinputKeyboard)(name)


def serve(keyboard: Keyboard) -> None:
	"""Takes requests for the keyboard until it vanishes."""
	path = os.path.join(DIRECTORY, f"{keyboard.name}.sock")
	if os.path.exists(path):
		os.unlink(path)
	server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
	server.bind(path)
	server.listen(1)
	with open(os.path.join(DIRECTORY, f"{keyboard.name}.ready"), "w") as ready:
		ready.write(keyboard.node() + "\n")
	while True:
		connection, _address = server.accept()
		with connection, connection.makefile("rwb") as stream:
			try:
				written, vanished = keyboard.run(json.loads(stream.readline()))
				answer: dict = {"written": written}
			except Exception as e:  # noqa: BLE001
				written, vanished = [], False
				answer = {"error": repr(e)}
			stream.write(json.dumps(answer).encode() + b"\n")
			stream.flush()
		if vanished:
			server.close()
			os.unlink(path)
			return


def call(name: str, text: str) -> int:
	with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
		client.connect(os.path.join(DIRECTORY, f"{name}.sock"))
		with client.makefile("rwb") as stream:
			stream.write(json.dumps(json.loads(text)).encode() + b"\n")
			stream.flush()
			answer = stream.readline().decode()
	print(answer, end="")
	return 1 if "error" in json.loads(answer) else 0


if __name__ == "__main__":
	if sys.argv[1:2] == ["serve"] and len(sys.argv) in (3, 4) and sys.argv[3:] in ([], ["uinput"], ["uhid"]):
		serve(make(sys.argv[2], (sys.argv[3:] or ["uinput"])[0]))
	elif sys.argv[1:2] == ["call"] and len(sys.argv) == 4:
		sys.exit(call(sys.argv[2], sys.argv[3]))
	else:
		sys.exit("usage: fakeKeyboard.py serve NAME [uinput|uhid] | call NAME JSON")
