"""A second Remote Access client for tests, speaking NVDA Remote's protocol to a relay server: JSON
messages, one to a line, over TLS. It stands in for the other computer, since the rig has one display
and one NVDA per display; NVDA hosts the relay server itself, and nothing here reaches beyond the
rig's own loopback.
"""

import json
import socket
import ssl
import threading
import time
from collections.abc import Callable
from typing import Any

#: connection_type as the protocol names the two ends: the controlling computer and the controlled.
LEADER = "master"
FOLLOWER = "slave"
PROTOCOL_VERSION = 2


class RemotePeer:
	"""A client joined to a channel of a relay server on this machine, recording every message."""

	def __init__(self, port: int, channel: str, connectionType: str, timeout: float = 10) -> None:
		context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
		# NVDA's own server has a certificate it made itself, as a self-hosted server does.
		context.check_hostname = False
		context.verify_mode = ssl.CERT_NONE
		# TLS 1.2, as NVDA's client connects: NVDA's relay server dropped this client at once over 1.3.
		context.maximum_version = ssl.TLSVersion.TLSv1_2
		deadline = time.monotonic() + timeout
		while True:
			try:
				raw = socket.create_connection(("127.0.0.1", port), timeout=timeout)
				break
			except ConnectionRefusedError:
				assert time.monotonic() < deadline, f"nothing listens on port {port}"
				time.sleep(0.1)
		self._socket = context.wrap_socket(raw, server_hostname="localhost")
		self._socket.settimeout(None)
		self._sendLock = threading.Lock()
		self._condition = threading.Condition()
		self.messages: list[dict[str, Any]] = []
		self._closed = False
		self._reader = threading.Thread(target=self._read, name="remotePeer", daemon=True)
		self._reader.start()
		self.send("protocol_version", version=PROTOCOL_VERSION)
		self.send("join", channel=channel, connection_type=connectionType)
		self.joined = self.waitFor(lambda m: m["type"] == "channel_joined", timeout)

	def _read(self) -> None:
		pending = b""
		try:
			while chunk := self._socket.recv(65536):
				*lines, pending = (pending + chunk).split(b"\n")
				with self._condition:
					self.messages.extend(json.loads(line) for line in lines if line.strip())
					self._condition.notify_all()
		except OSError:
			pass
		with self._condition:
			self._closed = True
			self._condition.notify_all()

	def send(self, type: str, **payload: Any) -> None:
		with self._sendLock:
			self._socket.sendall(json.dumps({**payload, "type": type}).encode("utf-8") + b"\n")

	def key(self, vk: int, pressed: bool, extended: bool = False, scan: int | None = None) -> None:
		"""A key event as NVDA's leader sends one from its keyboard hook."""
		self.send("key", vk_code=vk, extended=extended, pressed=pressed, scan_code=scan)

	def mark(self) -> int:
		with self._condition:
			return len(self.messages)

	def since(self, mark: int, type: str | None = None) -> list[dict[str, Any]]:
		with self._condition:
			return [m for m in self.messages[mark:] if type is None or m["type"] == type]

	def waitFor(
		self,
		predicate: Callable[[dict[str, Any]], bool],
		timeout: float = 5,
		since: int = 0,
	) -> dict[str, Any]:
		"""The first message after since that predicate accepts, waited for."""
		deadline = time.monotonic() + timeout
		with self._condition:
			while True:
				for message in self.messages[since:]:
					if predicate(message):
						return message
				left = deadline - time.monotonic()
				if left <= 0 or self._closed:
					types = [m["type"] for m in self.messages[since:]]
					raise AssertionError(f"no such message within {timeout} s; came: {types}")
				self._condition.wait(left)

	def close(self) -> None:
		try:
			self._socket.shutdown(socket.SHUT_RDWR)
		except OSError:
			pass
		self._socket.close()
		self._reader.join(5)


def spokenText(message: dict[str, Any]) -> str:
	"""The text of a speak message, its commands left out."""
	return " ".join(item for item in message.get("sequence", []) if isinstance(item, str))
