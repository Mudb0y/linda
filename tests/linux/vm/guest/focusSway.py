"""The sway guest's half of groundFocus.py, and the sway guest's own program ground-sway.

sway says through its IPC socket which window has the focus and moves it when asked, so here the
focus is read and moved by sway itself, in the protocol swaymsg speaks (sway-ipc(7)), not by keys
and not through AT-SPI. A window is active when sway's tree has it focused, which is the compositor's
own answer: what AT-SPI says is Linda's view, which the cases test against this one.

	focusSway.py watch OUT

writes each of sway's window events to OUT as a JSON line, stamped with the guest's CLOCK_MONOTONIC
as the ground's recorders are, after a first record of kind "ready" once sway has taken the
subscription: {"kind": "window", "received", "change", "title", "pid", "protocol"}, where change is
sway's (new, focus, title, close and the rest) and protocol is xdg_shell or xwayland. It runs in the
session until sway goes.
"""

import json
import os
import socket
import struct
import sys
import time
from contextlib import closing
from typing import Any

GUEST = "/etc/ground/guest.json"
MAGIC = b"i3-ipc"
#: The magic, the payload's length and the message's type, in the machine's own byte order.
HEADER = struct.Struct("=6sII")
RUN_COMMAND = 0
SUBSCRIBE = 2
GET_OUTPUTS = 3
GET_TREE = 4
GET_VERSION = 7
GET_INPUTS = 100
#: sway sets the high bit of an event's type; window events are 3.
WINDOW_EVENT = 0x80000003
#: Seconds a request to sway may take before it is given up.
TIMEOUT = 5
#: Characters a title may have, so that it stands for itself in a criteria's regular expression.
PLAIN_TITLE = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 -_")


def _guest() -> dict:
	with open(GUEST) as f:
		return json.load(f)


def socketPath() -> str:
	"""sway's IPC socket: the one sway gave the session, or the guest's fixed one from outside it."""
	return os.environ.get("SWAYSOCK") or _guest()["swaysock"]


class Connection:
	def __init__(self, timeout: float | None = TIMEOUT) -> None:
		self._socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
		self._socket.settimeout(timeout)
		self._socket.connect(socketPath())

	def close(self) -> None:
		self._socket.close()

	def _read(self, size: int) -> bytes:
		data = b""
		while len(data) < size:
			chunk = self._socket.recv(size - len(data))
			if not chunk:
				raise ConnectionError("sway closed its IPC socket")
			data += chunk
		return data

	def receive(self) -> tuple[int, Any]:
		magic, length, type = HEADER.unpack(self._read(HEADER.size))
		if magic != MAGIC:
			raise ConnectionError(f"sway's IPC sent {magic!r} where its magic belongs")
		return type, json.loads(self._read(length))

	def call(self, type: int, payload: str = "") -> Any:
		data = payload.encode()
		self._socket.sendall(HEADER.pack(MAGIC, len(data), type) + data)
		answerType, answer = self.receive()
		if answerType != type:
			raise ConnectionError(f"sway answered request {type} with message {answerType:#x}")
		return answer


def call(type: int, payload: str = "") -> Any:
	with closing(Connection()) as connection:
		return connection.call(type, payload)


def _walk(node: dict) -> Any:
	yield node
	for key in ("nodes", "floating_nodes"):
		for child in node.get(key, []):
			yield from _walk(child)


def ready() -> bool:
	"""sway has an output and a workspace on it, so a window mapped now is shown and can be focused."""
	return any(output.get("active") and output.get("current_workspace") for output in call(GET_OUTPUTS))


def windows() -> list[dict]:
	"""Each of the programs' windows in sway's tree; sway has no surfaces of its own in this guest."""
	found = []
	for node in _walk(call(GET_TREE)):
		if node.get("type") not in ("con", "floating_con") or node.get("pid") is None:
			continue
		properties = node.get("window_properties") or {}
		found.append(
			{
				"application": node.get("app_id") or properties.get("class"),
				"pid": node["pid"],
				"title": node.get("name"),
				"active": bool(node.get("focused")),
				"showing": bool(node.get("visible")),
				"shell": False,
				"protocol": node.get("shell"),
			}
		)
	return found


def activate(title: str) -> bool:
	if not title or not set(title) <= PLAIN_TITLE:
		raise ValueError(f"{title!r} would not stand for itself in sway's criteria")
	answer = call(RUN_COMMAND, f'[title="^{title}$"] focus')
	failed = [result for result in answer if not result.get("success")]
	if failed:
		raise RuntimeError(f"sway did not focus {title!r}: {failed}")
	return True


def _xwaylandRunning() -> bool:
	for pid in os.listdir("/proc"):
		if not pid.isdigit():
			continue
		try:
			with open(f"/proc/{pid}/comm") as f:
				if f.read().strip() == "Xwayland":
					return True
		except OSError:
			continue
	return False


def facts() -> dict:
	version = call(GET_VERSION)
	guest = _guest()
	with open(guest["variant"]) as f:
		variant = f.read().strip()
	return {
		"version": version.get("human_readable"),
		"outputs": [
			{"name": output.get("name"), "active": output.get("active")} for output in call(GET_OUTPUTS)
		],
		"keyboards": [
			{
				"name": device.get("name"),
				"identifier": device.get("identifier"),
				"layout": device.get("xkb_active_layout_name"),
				"layouts": device.get("xkb_layout_names"),
			}
			for device in call(GET_INPUTS)
			if device.get("type") == "keyboard"
		],
		"xwaylandBuilt": guest["xwayland"],
		"xwaylandConfigured": variant,
		"xwaylandRunning": _xwaylandRunning(),
		"display": os.environ.get("DISPLAY"),
		"swaysock": socketPath(),
	}


def watch(path: str) -> None:
	connection = Connection(timeout=None)
	answer = connection.call(SUBSCRIBE, json.dumps(["window"]))
	if not answer.get("success"):
		raise ConnectionError(f"sway refused the subscription: {answer}")
	with open(path, "a", buffering=1) as out:
		out.write(json.dumps({"kind": "ready", "received": time.monotonic()}) + "\n")
		while True:
			type, event = connection.receive()
			received = time.monotonic()
			if type != WINDOW_EVENT:
				continue
			container = event.get("container") or {}
			record = {
				"kind": "window",
				"received": received,
				"change": event.get("change"),
				"title": container.get("name"),
				"pid": container.get("pid"),
				"protocol": container.get("shell"),
			}
			out.write(json.dumps(record) + "\n")


if __name__ == "__main__":
	if sys.argv[1:2] == ["watch"] and len(sys.argv) == 3:
		try:
			watch(sys.argv[2])
		except ConnectionError as e:
			sys.exit(str(e))
	else:
		sys.exit("usage: focusSway.py watch OUT")
