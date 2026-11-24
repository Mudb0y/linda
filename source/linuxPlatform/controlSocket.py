"""Asking the running copy to take the keyboard back or install an add-on from outside it.

After the emergency chord NVDA has no keys, so none of its own commands can be pressed to ask. The
running copy listens on a Unix socket beside each of its locks and answers from the main loop, so a
copy whose main loop is stuck never answers, and the caller says so instead of reporting success.
Add-ons opened from file managers are also dispatched through this socket to the running copy.
Nothing is ever signalled: a copy from before this socket existed would end on any signal it does
not catch.

The socket is in the lock's directory, which only this user may enter, and a caller of any other
user is turned away all the same.
"""

import os
import socket
import struct
import sys
import time
from typing import NoReturn

#: What a caller sends, and what the running copy answers once it has acted.
REQUEST = b"take-keyboard\n"
REPLY_TAKEN = b"taken\n"
REPLY_OK = b"ok\n"
#: For backwards compatibility with take-keyboard callers
REPLY = REPLY_TAKEN
#: What the running copy answers when a request failed or was rejected.
FAILED_REPLY = b"failed\n"
ACCEPTED_REPLIES = (REPLY_TAKEN, REPLY_OK)
#: Seconds the caller waits for the answer, from its first attempt to connect.
ANSWER_TIMEOUT = 2.0
#: Seconds a connection that has not sent the whole request is kept.
_REQUEST_TIMEOUT = 5.0
#: Connections served at once; one more is closed as it is accepted.
_MAX_CONNECTIONS = 8
_BACKLOG = 4
#: struct ucred, as SO_PEERCRED gives it: process, user and group.
_UCRED = struct.Struct("iII")

#: The locks of this copy whose sockets start() opens.
_lockPaths: list[str] = []
_listeners: list["_Listener"] = []
_connections: set["_Connection"] = set()
_pendingAddons: list[str] = []
_installingAddon = False


def queueAddonInstall(path: str) -> None:
	"""Queue an add-on installation sequentially to prevent modal dialog collisions."""
	_pendingAddons.append(path)
	_processNextAddon()


def _processNextAddon() -> None:
	global _installingAddon
	if _installingAddon or not _pendingAddons:
		return
	_installingAddon = True
	path = _pendingAddons.pop(0)
	try:
		from gui import addonGui

		addonGui.handleRemoteAddonInstall(path)
	except Exception:
		from logHandler import log

		log.exception("Error handling add-on installation for %s", path)
	finally:
		_installingAddon = False
		if _pendingAddons:
			import wx

			wx.CallAfter(_processNextAddon)


def socketPath(lockPath: str) -> str:
	"""The control socket beside the lock file at lockPath."""
	return f"{lockPath.removesuffix('.lock')}.socket"


def prepare(lockPaths: list[str]) -> None:
	"""Name the locks this copy holds, beside which start() opens the sockets."""
	_lockPaths[:] = lockPaths


def start() -> None:
	"""Listen beside each lock prepare() named, from the main loop; for core.postNvdaStartup, once
	NVDA has a keyboard to take back."""
	from logHandler import log

	for lockPath in _lockPaths:
		path = socketPath(lockPath)
		try:
			_listeners.append(_Listener(path))
		# Without it NVDA runs as it did before there was a socket, and the caller says that it cannot ask.
		except OSError as e:
			log.warning(f"NVDA cannot be asked from outside to take the keyboard back through {path}: {e}")


def stop() -> None:
	"""Stop listening, and remove the sockets, while this copy still holds the locks they are beside."""
	while _connections:
		_connections.pop().close()
	while _listeners:
		_listeners.pop().close()


def _isOwnUser(connection: socket.socket) -> bool:
	credentials = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, _UCRED.size)
	_processID, userID, _groupID = _UCRED.unpack(credentials)
	return userID == os.getuid()


class _Listener:
	def __init__(self, path: str) -> None:
		from gi.repository import GLib, GLibUnix

		self.path = path
		self._socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM | socket.SOCK_CLOEXEC)
		try:
			self._socket.setblocking(False)
			# Left by a copy that was killed. This copy holds the lock beside it, so no other has it open.
			try:
				os.unlink(path)
			except FileNotFoundError:
				pass
			self._socket.bind(path)
			self._socket.listen(_BACKLOG)
		except OSError:
			self._socket.close()
			raise
		self._watch = GLibUnix.fd_add_full(
			GLib.PRIORITY_DEFAULT,
			self._socket.fileno(),
			GLib.IOCondition.IN,
			self._onConnection,
		)

	def _onConnection(self, fd: int, condition: int) -> bool:
		from gi.repository import GLib

		self.acceptOne()
		return GLib.SOURCE_CONTINUE

	def acceptOne(self) -> bool:
		"""Take the next connection waiting, if there is one; whether there was."""
		from logHandler import log

		try:
			connection, _address = self._socket.accept()
		except BlockingIOError:
			return False
		except OSError:
			log.debugWarning("Accepting a connection to the control socket", exc_info=True)
			return False
		try:
			connection.setblocking(False)
			isAllowed = len(_connections) < _MAX_CONNECTIONS and _isOwnUser(connection)
		except OSError:
			isAllowed = False
		if isAllowed:
			_connections.add(_Connection(connection))
		else:
			connection.close()
		return True

	def close(self) -> None:
		from gi.repository import GLib

		GLib.source_remove(self._watch)
		self._socket.close()
		try:
			os.unlink(self.path)
		except OSError:
			pass


class _Connection:
	def __init__(self, connection: socket.socket) -> None:
		from gi.repository import GLib, GLibUnix

		self._socket = connection
		self._received = b""
		self._watch: int | None = GLibUnix.fd_add_full(
			GLib.PRIORITY_DEFAULT,
			connection.fileno(),
			GLib.IOCondition.IN | GLib.IOCondition.HUP | GLib.IOCondition.ERR,
			self._onReadable,
		)
		self._timer: int | None = GLib.timeout_add(int(_REQUEST_TIMEOUT * 1000), self._onTimeout)

	def _onReadable(self, fd: int, condition: int) -> bool:
		from gi.repository import GLib

		try:
			data = self._socket.recv(4096)
		except BlockingIOError:
			return GLib.SOURCE_CONTINUE
		except OSError:
			data = b""
		self._received += data
		if data and b"\n" not in self._received and len(self._received) < 4096:
			return GLib.SOURCE_CONTINUE
		if self.hasRequest():
			self._answer()
		self._watch = None
		self.close()
		return GLib.SOURCE_REMOVE

	def _onTimeout(self) -> bool:
		from gi.repository import GLib

		self._timer = None
		self.close()
		return GLib.SOURCE_REMOVE

	def hasRequest(self) -> bool:
		"""Whether the whole request has come, reading what is there without waiting for more."""
		try:
			self._received += self._socket.recv(4096)
		except OSError:
			pass
		return self._received == REQUEST or (
			self._received.startswith(b"install-addon ") and self._received.endswith(b"\n")
		)

	def _answer(self) -> None:
		from logHandler import log

		if self._received.startswith(b"install-addon "):
			path = self._received.removeprefix(b"install-addon ").rstrip(b"\r\n").decode("utf-8", errors="replace")
			path = os.path.abspath(path)
			from utils.security import isLockScreenModeActive, isRunningOnSecureDesktop

			if isRunningOnSecureDesktop() or isLockScreenModeActive() or not os.path.isfile(path):
				log.debugWarning(f"Add-on installation rejected for {path}")
				reply = FAILED_REPLY
			else:
				import wx

				log.info("Requesting installation of add-on from %s", path)
				wx.CallAfter(queueAddonInstall, path)
				reply = REPLY_OK
			try:
				self._socket.send(reply)
			except OSError:
				pass
			return
		from logHandler import log

		from . import inputHook

		# Requests that queued up behind this one while this copy could not answer, as a caller run again
		# and again against a stopped copy leaves them: one taking back answers them all, and is said once.
		for listener in _listeners:
			while listener.acceptOne():
				pass
		queued = [
			connection
			for connection in list(_connections)
			if connection is not self and connection.hasRequest()
		]
		log.info("Asked from outside NVDA, through the control socket, to take the keyboard back")
		if queued:
			log.debug(f"{len(queued)} more requests that queued up are answered by the same")
		try:
			inputHook.takeKeyboardBack()
		except Exception:
			log.exception("Taking the keyboard back when asked from outside NVDA")
			reply = FAILED_REPLY
		else:
			reply = REPLY
		for connection in (self, *queued):
			try:
				connection._socket.send(reply)
			# The caller stopped waiting, as it does after ANSWER_TIMEOUT.
			except OSError:
				log.debug("The caller asking to take the keyboard back had gone before the answer")
		for connection in queued:
			connection.close()

	def close(self) -> None:
		from gi.repository import GLib

		for source in (self._watch, self._timer):
			if source is not None:
				GLib.source_remove(source)
		self._watch = self._timer = None
		self._socket.close()
		_connections.discard(self)


# The caller's half, which runs in a process of its own and needs only the standard library.

ANSWERED, FAILED, NO_ANSWER, UNREACHABLE = "answered", "failed", "noAnswer", "unreachable"
#: takeKeyboard's outcome when nothing was reachable because no copy holds a lock either.
NOT_RUNNING = "notRunning"
#: takeKeyboard's outcome when the caller's environment names no display to find a copy by.
NO_DISPLAY = "noDisplay"
_SAID = {
	ANSWERED: "The running copy of NVDA was asked to take the keyboard back, and says how it went.",
	FAILED: (
		"The running copy of NVDA could not take the keyboard back, and its log says why. "
		"Start NVDA again instead."
	),
	NO_ANSWER: "The running copy of NVDA does not answer. Start NVDA again instead, which replaces it.",
	UNREACHABLE: "The running copy of NVDA cannot be asked to take the keyboard back. Start NVDA again instead.",
	NOT_RUNNING: "NVDA is not running. Start it instead.",
	NO_DISPLAY: (
		"This terminal names no display, so the running copy of NVDA cannot be found. "
		"Run this in a terminal of the graphical session instead."
	),
}


def ask(paths: list[str], timeout: float = ANSWER_TIMEOUT, request: bytes = REQUEST) -> str:
	"""Ask the copy listening at the first of paths that takes a connection to carry out request.

	:return: ANSWERED or FAILED as it answered; NO_ANSWER when a copy took the connection but did not
		answer within timeout; UNREACHABLE when no path took a connection.
	"""
	deadline = time.monotonic() + timeout
	maxReplyLen = max(len(r) for r in (*ACCEPTED_REPLIES, FAILED_REPLY))
	for path in paths:
		if (remaining := deadline - time.monotonic()) <= 0:
			return NO_ANSWER
		caller = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM | socket.SOCK_CLOEXEC)
		try:
			caller.settimeout(remaining)
			try:
				caller.connect(path)
			# A queue full of connections nobody accepts, as a stopped copy's fills. Linux refuses that at
			# once with EAGAIN rather than waiting, as the timeout makes the socket non-blocking.
			except BlockingIOError:
				return NO_ANSWER
			# No socket, or one a copy that ended left behind, or a path too long to be one.
			except OSError:
				continue
			reply = b""
			try:
				caller.sendall(request)
				while not reply.endswith(b"\n") and len(reply) <= maxReplyLen:
					remaining = deadline - time.monotonic()
					if remaining <= 0:
						return NO_ANSWER
					caller.settimeout(remaining)
					chunk = caller.recv(maxReplyLen)
					if not chunk:
						break
					reply += chunk
			except OSError:
				return NO_ANSWER
			if reply in ACCEPTED_REPLIES:
				return ANSWERED
			return FAILED if reply == FAILED_REPLY else NO_ANSWER
		finally:
			caller.close()
	return UNREACHABLE


def takeKeyboard() -> NoReturn:
	"""linda --take-keyboard: ask the copy running on this display to take the keyboard back, say how
	it went, and exit with status 0 if it answered that it had."""
	from . import displayServer, singleInstance

	kind = displayServer.kind()
	# From a console or over ssh. The lock this would look at is then no copy's, and saying that none
	# runs would be wrong while one runs in the graphical session.
	if kind == "none":
		print(_SAID[NO_DISPLAY], flush=True)
		sys.exit(1)
	names = singleInstance.displayNames(kind)
	try:
		lockPaths = [singleInstance.lockFilePath(singleInstance.APP_NAME, name) for name in names]
		outcome = ask([socketPath(path) for path in lockPaths])
		if outcome == UNREACHABLE and not _isAnyHeld(
			[
				singleInstance.lockFilePath(appName, name)
				for name in names
				for appName in (singleInstance.APP_NAME, singleInstance.FORMER_APP_NAME)
			]
		):
			outcome = NOT_RUNNING
	except OSError as e:
		print(f"NVDA's lock cannot be opened to find the running copy: {e}", flush=True)
		sys.exit(1)
	print(_SAID[outcome], flush=True)
	sys.exit(0 if outcome == ANSWERED else 1)


def _isAnyHeld(lockPaths: list[str]) -> bool:
	from . import singleInstance

	for path in lockPaths:
		lock = singleInstance.InstanceLock(path)
		try:
			if lock.holder() is not None:
				return True
		# Held from a process this one cannot see, which is no reason to say nothing runs.
		except OSError:
			return True
		finally:
			lock.close()
	return False
