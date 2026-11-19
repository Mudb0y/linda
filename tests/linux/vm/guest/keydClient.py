"""A client of linda-keyd for the tests, written from the helper's protocol as
engine/crates/keyd/src/protocol.rs gives it, which a test script drives one step at a time: it can
answer each key at once, late or never, stop its heartbeats, send rubbish, or die.

	keydClient.py serve NAME
	keydClient.py call NAME JSON

serve takes requests on DIRECTORY/NAME.sock and records, one JSON object a line in DIRECTORY/NAME.jsonl,
every message it gets from the helper and every one it sends but heartbeats, each with "t", the
guest's CLOCK_MONOTONIC when it was read or sent, "dir", "in" or "out", and "type", the message's
name in protocol.rs, with its fields; reasons and statuses by their names there. A KEY record has
"key", the evdev name of its code, too.

A request is one JSON object, answered by one:
- {"op": "connect", "hello": true, "version": 1, "features": 0}: connects and, unless hello is false,
  sends HELLO; answers once WELCOME or GOODBYE has come, or after ten seconds, with what came.
- {"op": "start"}, {"op": "stop"}: START and STOP.
- {"op": "verdicts", "default": RULE, "keys": {KEY: RULE}}: how to answer a press that asks for a
  verdict, for KEY by its evdev name or any other. A RULE is "pass", "keep", "never", or
  {"after": SECONDS, "keep": BOOL} for an answer that late; with "wait": true it answers WAIT at
  once first (connect with FEATURE_WAIT in "features"), and without "after" nothing more. At first
  every press is passed at once.
- {"op": "heartbeats", "on": BOOL}: sends a HEARTBEAT every interval WELCOME gave, or stops.
- {"op": "send", "hex": HEX}: sends those bytes as one message, rubbish or not.
- {"op": "close"}: closes the connection.
- {"op": "die"}: ends the process at once, without closing anything itself.
- {"op": "state"}: answers whether it is connected, the WELCOME and the GOODBYE it had, and how many
  heartbeats were echoed.
"""

import json
import os
import socket
import struct
import sys
import threading
import time

from evdev import ecodes

DIRECTORY = "/tmp/ground/keyd"
SOCKET_PATH = "/run/linda-keyd/socket"
MAX_MESSAGE = 256

HELLO, START, STOP, VERDICT, HEARTBEAT = 0x01, 0x02, 0x03, 0x04, 0x05
VERDICT_WAIT = 2
FEATURE_WAIT = 2
WELCOME, GOODBYE, KEY, EXPIRED, RELEASED, DEVICE, LOCKS, HEARTBEAT_ECHO = range(0x81, 0x89)
KEY_ASK, KEY_SYNTHETIC, KEY_TYPED = 0x01, 0x02, 0x04
RELEASED_REASONS = {
	1: "STOPPED",
	2: "EMERGENCY",
	3: "BACKGROUND",
	4: "VIRTUAL_KEYBOARD",
	5: "SESSION_UNKNOWN",
}
GOODBYE_REASONS = {
	1: "VERSION",
	2: "NOT_IN_FRONT",
	3: "NOT_AUTHORIZED",
	4: "BUSY",
	5: "NO_ANSWER",
	6: "SILENT",
	7: "MALFORMED",
	8: "UNANSWERED",
	9: "REPLACED",
	10: "OVERFLOW",
	11: "TOO_MANY",
}
DEVICE_STATUSES = {1: "HELD", 2: "WAITING", 3: "BUSY", 4: "GONE", 5: "IGNORED"}
IGNORED_REASONS = {0: None, 1: "POINTER", 2: "SWITCHES", 3: "BUTTONS", 4: "VIRTUAL", 5: "OTHER_SEAT"}


def keyName(code: int) -> str:
	name = ecodes.KEY.get(code, str(code))
	return name[0] if isinstance(name, list) else name


def decode(message: bytes) -> dict:
	"""A helper's message as a record, by protocol.rs's layouts."""
	kind = message[0]
	if kind == WELCOME:
		_, version, features, verdict, interval, timeout, span = struct.unpack("<BHIHHHH", message)
		return {
			"type": "WELCOME",
			"version": version,
			"features": features,
			"verdictMs": verdict,
			"heartbeatIntervalMs": interval,
			"heartbeatTimeoutMs": timeout,
			"unansweredSpanMs": span,
		}
	if kind == GOODBYE:
		_, reason, version = struct.unpack_from("<BBH", message)
		return {
			"type": "GOODBYE",
			"reason": GOODBYE_REASONS.get(reason, reason),
			"version": version,
			"text": message[4:].decode(),
		}
	if kind == KEY:
		_, seq, device, code, value, flags = struct.unpack("<BQIHBB", message)
		return {
			"type": "KEY",
			"seq": seq,
			"device": device,
			"code": code,
			"key": keyName(code),
			"value": value,
			"ask": bool(flags & KEY_ASK),
			"synthetic": bool(flags & KEY_SYNTHETIC),
			"flags": flags,
		}
	if kind == EXPIRED:
		return {"type": "EXPIRED", "seq": struct.unpack("<BQ", message)[1]}
	if kind == RELEASED:
		reason = struct.unpack("<BB", message)[1]
		return {"type": "RELEASED", "reason": RELEASED_REASONS.get(reason, reason)}
	if kind == DEVICE:
		_, device, status, detail = struct.unpack_from("<BIBB", message)
		return {
			"type": "DEVICE",
			"device": device,
			"status": DEVICE_STATUSES.get(status, status),
			"ignored": IGNORED_REASONS.get(detail, detail),
			"name": message[7:].decode(),
		}
	if kind == LOCKS:
		return {"type": "LOCKS", "leds": struct.unpack("<BH", message)[1]}
	if kind == HEARTBEAT_ECHO:
		return {"type": "HEARTBEAT_ECHO", "counter": struct.unpack("<BI", message)[1]}
	return {"type": "UNKNOWN", "hex": message.hex()}


class Client:
	def __init__(self, name: str) -> None:
		self.log = open(os.path.join(DIRECTORY, f"{name}.jsonl"), "a", buffering=1)  # noqa: SIM115
		self.lock = threading.Lock()
		self.sock: socket.socket | None = None
		self.welcome: dict | None = None
		self.goodbye: dict | None = None
		self.answered = threading.Event()
		self.defaultRule: object = "pass"
		self.rules: dict[str, object] = {}
		self.heartbeating = True
		self.counter = 0
		self.echoes = 0
		threading.Thread(target=self.heartbeat, daemon=True).start()

	def record(self, direction: str, record: dict) -> None:
		with self.lock:
			self.log.write(json.dumps({"t": time.monotonic(), "dir": direction, **record}) + "\n")

	def send(self, message: bytes, record: dict | None = None) -> None:
		sock = self.sock
		if sock is None:
			return
		try:
			sock.send(message)
		except OSError as e:
			self.record("out", {"type": "ERROR", "error": repr(e)})
			return
		if record is not None:
			self.record("out", record)

	def connect(self, hello: bool, version: int, features: int) -> dict:
		self.welcome = self.goodbye = None
		self.answered.clear()
		sock = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
		sock.connect(SOCKET_PATH)
		self.sock = sock
		threading.Thread(target=self.read, args=(sock,), daemon=True).start()
		# The helper turns a caller of the wrong user away as it connects. A HELLO that reaches it
		# after that, unread as it closes, makes the kernel answer the next read here with
		# ECONNRESET instead of the GOODBYE waiting for it; so a moment is left for that GOODBYE.
		self.answered.wait(0.1)
		if hello and self.goodbye is None:
			self.send(struct.pack("<BHI", HELLO, version, features), {"type": "HELLO", "version": version})
		self.answered.wait(10)
		return {"welcome": self.welcome, "goodbye": self.goodbye}

	def read(self, sock: socket.socket) -> None:
		while True:
			try:
				message = sock.recv(MAX_MESSAGE)
			except OSError as e:
				self.record("in", {"type": "CLOSED", "error": repr(e)})
				break
			if not message:
				self.record("in", {"type": "CLOSED"})
				break
			record = {"t": time.monotonic(), **decode(message)}
			if record["type"] == "HEARTBEAT_ECHO":
				self.echoes += 1
				continue
			self.record("in", record)
			if record["type"] == "WELCOME":
				self.welcome = record
				self.answered.set()
			elif record["type"] == "GOODBYE":
				self.goodbye = record
				self.answered.set()
			elif record["type"] == "KEY" and record["ask"]:
				self.decide(record)
		if self.sock is sock:
			self.sock = None
		self.answered.set()

	def decide(self, key: dict) -> None:
		rule = self.rules.get(key["key"], self.defaultRule)
		if rule == "never":
			return
		if isinstance(rule, dict):
			if rule.get("wait"):
				self.send(
					struct.pack("<BQB", VERDICT, key["seq"], VERDICT_WAIT),
					{"type": "WAIT", "seq": key["seq"]},
				)
				if "after" not in rule:
					return
			keep = bool(rule.get("keep", False))
			timer = threading.Timer(rule["after"], self.verdict, (key["seq"], keep))
			timer.daemon = True
			timer.start()
			return
		self.verdict(key["seq"], rule == "keep")

	def verdict(self, seq: int, keep: bool) -> None:
		self.send(struct.pack("<BQB", VERDICT, seq, int(keep)), {"type": "VERDICT", "seq": seq, "keep": keep})

	def heartbeat(self) -> None:
		while True:
			interval = (self.welcome or {}).get("heartbeatIntervalMs", 500) / 1000
			time.sleep(interval)
			if self.heartbeating and self.welcome is not None and self.sock is not None:
				self.counter += 1
				self.send(struct.pack("<BI", HEARTBEAT, self.counter))

	def handle(self, request: dict) -> dict:
		op = request.get("op")
		if op == "connect":
			return self.connect(
				request.get("hello", True), request.get("version", 1), request.get("features", 0)
			)
		if op == "start":
			self.send(bytes([START]), {"type": "START"})
		elif op == "stop":
			self.send(bytes([STOP]), {"type": "STOP"})
		elif op == "verdicts":
			self.defaultRule = request.get("default", "pass")
			self.rules = request.get("keys", {})
		elif op == "heartbeats":
			self.heartbeating = request["on"]
		elif op == "send":
			self.send(bytes.fromhex(request["hex"]), {"type": "RAW", "hex": request["hex"]})
		elif op == "close":
			# Taken once: the shutdown ends the reading thread's recv, and that thread then clears
			# self.sock, which may come before the close here.
			sock, self.sock = self.sock, None
			if sock is not None:
				# The reading thread's recv keeps the socket open past close(); shutdown ends it.
				sock.shutdown(socket.SHUT_RDWR)
				sock.close()
		elif op == "die":
			os._exit(9)
		elif op != "state":
			return {"error": f"unknown op {op!r}"}
		return {
			"connected": self.sock is not None,
			"welcome": self.welcome,
			"goodbye": self.goodbye,
			"echoes": self.echoes,
			"pid": os.getpid(),
		}


def serve(name: str) -> None:
	os.makedirs(DIRECTORY, exist_ok=True)
	path = os.path.join(DIRECTORY, f"{name}.sock")
	if os.path.exists(path):
		os.unlink(path)
	client = Client(name)
	server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
	# Bound under a temporary name and renamed, so that the socket appearing means it accepts, and
	# open to root, which drives it, whoever this runs as.
	temporary = f"{path}.{os.getpid()}"
	server.bind(temporary)
	os.chmod(temporary, 0o666)
	server.listen(4)
	os.rename(temporary, path)
	while True:
		connection, _address = server.accept()
		with connection, connection.makefile("rwb") as stream:
			try:
				answer = client.handle(json.loads(stream.readline()))
			except Exception as e:  # noqa: BLE001
				answer = {"error": repr(e)}
			stream.write(json.dumps(answer).encode() + b"\n")
			stream.flush()


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
		sys.exit("usage: keydClient.py serve NAME | call NAME JSON")
