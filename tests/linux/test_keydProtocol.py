"""The two Python clients of linda-keyd held to engine/crates/keyd/src/protocol.rs, which is the whole
of the protocol: every message's struct format and number, and every constant both sides name, as
that file gives them, and HELLO as Linda sends it today. Run under the rig:

	nix develop . -c tests/linux/rig python3 -m pytest tests/linux/test_keydProtocol.py
"""

import importlib.util
import inspect
import re
import shutil
import socket
import struct
import sys
import tempfile
import threading
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PROTOCOL = ROOT / "engine" / "crates" / "keyd" / "src" / "protocol.rs"
CLIENT = ROOT / "source" / "linuxPlatform" / "keys" / "keydClient.py"
GUEST_CLIENT = ROOT / "tests" / "linux" / "vm" / "guest" / "keydClient.py"

#: HELLO for version 1 asking for FEATURE_WAIT alone, as Linda has sent it since the feature came.
TODAYS_HELLO = bytes([0x01, 0x01, 0x00, 0x02, 0x00, 0x00, 0x00])

#: The names both protocol.rs and the product client give a number.
SHARED_NAMES = (
	"PROTOCOL_VERSION",
	"MAX_MESSAGE",
	"HELLO",
	"START",
	"STOP",
	"VERDICT",
	"HEARTBEAT",
	"TYPE",
	"LATE_PASS",
	"WELCOME",
	"GOODBYE",
	"KEY",
	"EXPIRED",
	"RELEASED",
	"DEVICE",
	"LOCKS",
	"HEARTBEAT_ECHO",
	"TYPED",
	"VIRTUAL",
	"FEATURE_TYPING",
	"FEATURE_WAIT",
	"FEATURE_LATE_PASS",
	"FEATURE_VIRTUAL_SEEN",
	"VERDICT_WAIT",
	"KEY_ASK",
	"KEY_SYNTHETIC",
	"KEY_TYPED",
	"KEY_RELEASE",
	"KEY_PRESS",
	"KEY_REPEAT",
	"LOCK_NUM",
	"LOCK_CAPS",
	"LOCK_SCROLL",
	"TYPE_DECIDE",
	"TYPE_INJECT",
	"TYPED_OUT",
	"TYPED_KEPT",
	"TYPED_REFUSED_STATE",
	"TYPED_REFUSED_KEY",
	"TYPED_REFUSED_FULL",
	"TYPED_NOT_DOWN",
	"TYPED_LOST",
	"VIRTUAL_SEEN",
	"VIRTUAL_UNSEEN",
	"VIRTUAL_OLD",
)


def rustConstants() -> dict[str, int]:
	found = {}
	pattern = r"^pub const (\w+): (?:u8|u16|u32|usize) = (0x[0-9A-Fa-f]+|\d+)(?: << (\d+))?;$"
	for name, value, shift in re.findall(pattern, PROTOCOL.read_text(), re.MULTILINE):
		found[name] = int(value, 0) << int(shift or 0)
	return found


def documentedLayouts() -> dict[str, str]:
	"""Each message's struct format, from the first line of its constant's documentation."""
	layouts = {}
	docs: list[str] = []
	for line in PROTOCOL.read_text().splitlines():
		if line.startswith("///"):
			docs.append(line)
			continue
		match = re.match(r"pub const (\w+): u8 = 0x[0-9A-Fa-f]+;", line)
		if match and docs:
			layout = re.match(r"/// `(<\w+)`", docs[0])
			if layout:
				layouts[match.group(1)] = layout.group(1)
		docs = []
	return layouts


def load(path: Path, name: str, monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
	spec = importlib.util.spec_from_file_location(name, path)
	module = importlib.util.module_from_spec(spec)
	monkeypatch.setitem(sys.modules, name, module)
	spec.loader.exec_module(module)
	return module


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
	return load(CLIENT, "keydClientUnderTest", monkeypatch)


@pytest.fixture
def guest(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
	# The guest's own python-evdev is not in the rig; the client needs only its names of key codes.
	evdev = types.ModuleType("evdev")
	evdev.ecodes = types.SimpleNamespace(KEY={30: "KEY_A"}, ecodes={"KEY_A": 30})
	monkeypatch.setitem(sys.modules, "evdev", evdev)
	return load(GUEST_CLIENT, "guestKeydClientUnderTest", monkeypatch)


def test_everyMessageHasItsDocumentedLayout(client: types.ModuleType):
	layouts = documentedLayouts()
	structs = {
		"HELLO": client._HELLO,
		"VERDICT": client._VERDICT,
		"HEARTBEAT": client._HEARTBEAT,
		"TYPE": client._TYPE,
		"LATE_PASS": client._LATE_PASS,
		"WELCOME": client._WELCOME,
		"GOODBYE": client._GOODBYE,
		"KEY": client._KEY,
		"EXPIRED": client._EXPIRED,
		"RELEASED": client._RELEASED,
		"DEVICE": client._DEVICE,
		"LOCKS": client._LOCKS,
		"HEARTBEAT_ECHO": client._HEARTBEAT,
		"TYPED": client._TYPED,
		"VIRTUAL": client._VIRTUAL,
	}
	assert set(layouts) == set(structs) | {"START", "STOP"}, layouts
	for name, layout in structs.items():
		assert layout.format == layouts[name], name
		assert layout.size == struct.calcsize(layouts[name]), name
	assert (struct.calcsize(layouts["TYPE"]), struct.calcsize(layouts["LATE_PASS"])) == (9, 17)
	assert (struct.calcsize(layouts["TYPED"]), struct.calcsize(layouts["VIRTUAL"])) == (14, 2)
	built = {
		"HELLO": client.hello(),
		"START": client.start(),
		"STOP": client.stop(),
		"VERDICT": client.wait(1),
		"HEARTBEAT": client.heartbeat(1),
		"TYPE": client.typeKey(1, 30, True, client.TYPE_DECIDE),
		"LATE_PASS": client.latePass(1, 2),
	}
	for name, message in built.items():
		assert len(message) == struct.calcsize(layouts[name]), name
		assert message[0] == getattr(client, name), name


def test_bothClientsNumberThingsAsTheHelperDoes(client: types.ModuleType, guest: types.ModuleType):
	rust = rustConstants()
	for name in SHARED_NAMES:
		assert getattr(client, name) == rust[name], name
		if hasattr(guest, name):
			assert getattr(guest, name) == rust[name], name
	for name in ("TYPE", "LATE_PASS", "TYPED", "VIRTUAL", "FEATURE_TYPING", "FEATURE_LATE_PASS"):
		assert hasattr(guest, name), name
	assert rust["TYPED_WAITING_LIMIT"] == 64
	assert (rust["TYPED_PER_SECOND"], rust["TYPED_BURST"]) == (100, 200)
	assert guest.TYPE_MODES == {"decide": rust["TYPE_DECIDE"], "inject": rust["TYPE_INJECT"]}
	outcomes = {name: value for name, value in rust.items() if name.startswith("TYPED_") and value < 8}
	assert {f"TYPED_{name}": value for value, name in guest.TYPED_OUTCOMES.items()} == outcomes
	statuses = {name: value for name, value in rust.items() if name.startswith("VIRTUAL_") and value < 4}
	assert {f"VIRTUAL_{name}": value for value, name in guest.VIRTUAL_STATUSES.items()} == statuses
	assert set(client.TYPED_NAMES) == set(outcomes.values())
	# The helper grants FEATURE_WAIT alone.
	assert re.search(r"^pub const FEATURES_KNOWN: u32 = FEATURE_WAIT;$", PROTOCOL.read_text(), re.MULTILINE)


def test_theNewMessagesAreBuiltAndReadByBothClients(client: types.ModuleType, guest: types.ModuleType):
	typed = bytes([0x06, 7, 0, 0, 0, 30, 0, 1, 1])
	assert client.typeKey(7, 30, True, client.TYPE_INJECT) == typed
	assert guest.typeKey(7, 30, True, 1) == typed
	assert client.typeKey(-1, 30, False, client.TYPE_DECIDE) == bytes([0x06, 255, 255, 255, 255, 30, 0, 0, 0])
	late = bytes([0x07, 1, 0, 0, 0, 0, 0, 0, 0, 2, 0, 0, 0, 0, 0, 0, 0])
	assert client.latePass(1, 2) == guest.latePass(1, 2) == late

	answer = struct.pack("<BIQB", 0x89, 7, 12, 6)
	assert client.decode(answer) == client.Typed(tag=7, seq=12, outcome=client.TYPED_NOT_DOWN)
	assert guest.decode(answer) == {"type": "TYPED", "tag": 7, "seq": 12, "outcome": "NOT_DOWN"}
	assert client.decode(bytes([0x8A, 1])) == client.Virtual(client.VIRTUAL_SEEN)
	assert guest.decode(bytes([0x8A, 3])) == {"type": "VIRTUAL", "status": "OLD"}
	for broken in (answer[:-1], bytes([0x8A]), bytes([0x8A, 1, 0])):
		with pytest.raises(client.Malformed):
			client.decode(broken)
	# A type of a later feature is still ignored.
	assert client.decode(bytes([0x8B, 1])) is None
	key = struct.pack("<BQIHBB", 0x83, 1, 0xFFFFFFFF, 30, 1, 0x05)
	assert guest.decode(key)["typed"] and guest.decode(key)["ask"]


class _Helper:
	"""A listener on a SOCK_SEQPACKET socket that records the first message of one connection and
	answers it with WELCOME granting what it is told to."""

	def __init__(self, path: Path, grants: int) -> None:
		self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
		self.listener.bind(str(path))
		self.listener.listen(1)
		self.grants = grants
		self.hello: bytes | None = None
		self.thread = threading.Thread(target=self.serve, daemon=True)
		self.thread.start()

	def serve(self) -> None:
		connection, _address = self.listener.accept()
		with connection:
			self.hello = connection.recv(256)
			connection.send(struct.pack("<BHIHHHH", 0x81, 1, self.grants, 100, 500, 2000, 1000))
			connection.recv(256)

	def close(self) -> None:
		self.listener.close()


@pytest.mark.parametrize(
	("features", "sent"),
	[
		(None, TODAYS_HELLO),
		(15, bytes([0x01, 0x01, 0x00, 0x0F, 0x00, 0x00, 0x00])),
		(0, bytes([0x01, 0x01, 0x00, 0x00, 0x00, 0x00, 0x00])),
	],
)
def test_connectSendsTodaysHelloUnlessAskedForOtherFeatures(
	client: types.ModuleType,
	features: int | None,
	sent: bytes,
):
	assert inspect.signature(client.connect).parameters["features"].default == client.FEATURE_WAIT
	# A Unix socket's path is limited to 107 bytes, which a basetemp under someone's scratch folder can
	# pass on its own.
	socketDir = Path(tempfile.mkdtemp(prefix="keyd", dir="/tmp"))
	path = socketDir / "socket"
	try:
		helper = _Helper(path, client.FEATURE_WAIT)
		try:
			if features is None:
				sock, welcome = client.connect(str(path), 5)
			else:
				sock, welcome = client.connect(str(path), 5, features)
			sock.close()
			helper.thread.join(5)
		finally:
			helper.close()
	finally:
		shutil.rmtree(socketDir, ignore_errors=True)
	assert helper.hello == sent
	assert welcome.features == client.FEATURE_WAIT
