"""A second keyboard, made inside the guest through the guest's own uinput, driven by the test script.

The framework's own key sending presses the guest's emulated keyboard, one chord at a time, with the
monitor's timing. This keyboard is for what that cannot do: a second keyboard beside the first, one
that vanishes with keys held, and keys at exact intervals, since a whole sequence runs here in the
guest and not over the test framework's shell.

	fakeKeyboard.py serve NAME
	fakeKeyboard.py call NAME JSON

serve makes the keyboard, writes its event node to DIRECTORY/NAME.ready, and takes requests on
DIRECTORY/NAME.sock. A request is a list of steps, run in order: {"press": KEY}, {"release": KEY},
{"tap": KEY, "hold": SECONDS}, {"chord": [KEY, ...], "hold": SECONDS}, which presses the keys in
order and releases them in reverse, {"sleep": SECONDS}, and {"vanish": true}, which destroys the
keyboard at once, with whatever it holds down, and ends serve. KEY is an evdev name such as KEY_T.
The answer lists every event written, with the monotonic time it was written at, and each one is
also appended to DIRECTORY/NAME.jsonl.

It runs as root: /dev/uinput is root's alone unless the guest gives it a group.
"""

import json
import os
import socket
import sys
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


class Keyboard:
	def __init__(self, name: str) -> None:
		self.name = name
		self.device = evdev.UInput(
			{ecodes.EV_KEY: KEYS, ecodes.EV_LED: LEDS, ecodes.EV_REP: []},
			name=name,
			bustype=BUSTYPE,
			vendor=VENDOR,
			product=PRODUCT,
			version=1,
			phys=f"ground/{name}/input0",
		)
		self.log = open(os.path.join(DIRECTORY, f"{name}.jsonl"), "a", buffering=1)  # noqa: SIM115

	def write(self, code: int, value: int, written: list[dict]) -> None:
		self.device.write(ecodes.EV_KEY, code, value)
		self.device.syn()
		record = {"t": time.monotonic(), "code": code, "key": keyName(code), "value": value}
		self.log.write(json.dumps(record) + "\n")
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
				self.device.close()
				self.log.write(json.dumps({"t": time.monotonic(), "vanished": True}) + "\n")
				return written, True
			else:
				raise ValueError(f"unknown step {step!r}")
		return written, False


def keyName(code: int) -> str:
	name = ecodes.KEY.get(code, str(code))
	return name[0] if isinstance(name, list) else name


def serve(name: str) -> None:
	os.makedirs(DIRECTORY, exist_ok=True)
	path = os.path.join(DIRECTORY, f"{name}.sock")
	if os.path.exists(path):
		os.unlink(path)
	keyboard = Keyboard(name)
	server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
	server.bind(path)
	server.listen(1)
	with open(os.path.join(DIRECTORY, f"{name}.ready"), "w") as ready:
		ready.write(keyboard.device.device.path + "\n")
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
	if sys.argv[1:2] == ["serve"] and len(sys.argv) == 3:
		serve(sys.argv[2])
	elif sys.argv[1:2] == ["call"] and len(sys.argv) == 4:
		sys.exit(call(sys.argv[2], sys.argv[3]))
	else:
		sys.exit("usage: fakeKeyboard.py serve NAME | call NAME JSON")
