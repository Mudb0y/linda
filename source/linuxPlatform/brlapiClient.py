"""BRLTTY's BrlAPI server, as NVDA's BRLTTY driver reaches it on Linux.

On Windows the driver knows BRLTTY runs from the named pipes its server listens on. Here the server
listens on a local socket in BrlAPI's sockets directory, or on TCP where BRLAPI_HOST names a host, and
which display it drives, and whether it drives one at all, only the server can say.

Each call into the client library that can wait for the server runs on a thread of its own, waited
for at most a bounded time, so that a BRLTTY which accepts and never answers, as a stopped one does,
holds up only that thread. Reading a key without waiting is the one call the driver makes itself.

With Automatic chosen, the background detection of NVDA's braille handler finds BRLTTY through scan,
which this module registers as a device scanner. While no braille display is in use, and either
detection is running or BRLTTY is the display chosen, a timer of this module looks every
POLL_INTERVAL milliseconds for the socket, and tries BRLTTY again when the socket comes or is made
anew, and every RETRY_INTERVAL seconds while it stays: a BRLTTY that has started, has been restarted
or has been given a display is taken up without the user doing anything. Without a socket a look is
one stat call.
"""

import os
import stat
import threading
import time
from collections.abc import Callable
from typing import Any

from logHandler import log

DRIVER_NAME = "brltty"
#: BrlAPI's sockets directory: BRLTTY's default, which distributions and nixpkgs keep. The socket
#: named 0 is the server's default, and BRLAPI_HOST=:N names socket N.
SOCKET_DIRECTORY = "/var/lib/BrlAPI"
#: Seconds the server may take to accept a connection and put it in tty mode, and to leave it, which
#: a server that answers at all does in milliseconds.
CONNECT_TIMEOUT = 3.0
DISCONNECT_TIMEOUT = 1.0
POLL_INTERVAL = 2000
RETRY_INTERVAL = 5.0


class NoDisplayError(RuntimeError):
	"""BRLTTY's server answers, but BRLTTY drives no braille display."""


def localSocketPath() -> str | None:
	"""The local socket the client library connects to, or None if BRLAPI_HOST names a TCP host."""
	host = os.environ.get("BRLAPI_HOST", "")
	if not host:
		return os.path.join(SOCKET_DIRECTORY, "0")
	name, separator, number = host.rpartition(":")
	if separator and not name:
		return os.path.join(SOCKET_DIRECTORY, number)
	return None


def _socketIdentity() -> tuple[int, int] | None:
	path = localSocketPath()
	if path is None:
		return (0, 0)
	try:
		status = os.stat(path)
	except OSError:
		return None
	if not stat.S_ISSOCK(status.st_mode):
		return None
	return (status.st_dev, status.st_ino)


def isReachable() -> bool:
	"""Whether a BrlAPI server may be listening: its local socket is there, or a TCP host is named,
	which only a connection can tell. Costs a stat call and never waits on the server."""
	return _socketIdentity() is not None


def callWithin[T](timeout: float, call: Callable[[], T], abandoned: Callable[[T], Any] | None = None) -> T:
	"""What call returns, from a thread of its own that is waited for at most timeout seconds.

	:param abandoned: Given what call returns if it returns after the wait has given up, so that a
		connection made too late can be closed.
	:raise TimeoutError: If call has not finished within timeout.
	"""
	lock = threading.Lock()
	finished = threading.Event()
	outcome: dict[str, Any] = {}

	def run() -> None:
		try:
			outcome["result"] = call()
		except BaseException as error:  # noqa: BLE001
			outcome["error"] = error
		with lock:
			finished.set()
			if not outcome.get("waited", True) and abandoned and "result" in outcome:
				try:
					abandoned(outcome["result"])
				except Exception:  # noqa: BLE001
					log.debugWarning("Could not clean up after an abandoned BrlAPI call", exc_info=True)

	thread = threading.Thread(target=run, name=f"{__name__}.callWithin", daemon=True)
	thread.start()
	finished.wait(timeout)
	with lock:
		if not finished.is_set():
			outcome["waited"] = False
			raise TimeoutError(f"BRLTTY did not answer within {timeout} s")
	if "error" in outcome:
		raise outcome["error"]
	return outcome["result"]


class Link:
	"""A connection to BRLTTY in tty mode, with a thread of its own for every write and for the leaving.

	The server takes cells without answering, but a server that has stopped reading lets about a
	hundred writes fill the socket and then holds the next, which on NVDA's shared I/O thread would
	hold up every display write after it and NVDA's exit, which waits for that thread. Here only this
	thread waits; cells that come meanwhile replace those still to be written, as NVDA's handler does
	with its own queue.
	"""

	def __init__(
		self, connection: Any, displaySize: tuple[int, int], driverName: str, onLost: Callable[[], None]
	):
		self.connection = connection
		self.displaySize = displaySize
		self.driverName = driverName
		self._onLost = onLost
		self._wake = threading.Condition()
		self._pending: bytes | None = None
		self._closing = False
		self._thread = threading.Thread(target=self._run, name=f"{__name__}.Link", daemon=True)
		self._thread.start()

	def write(self, cells: bytes) -> None:
		with self._wake:
			self._pending = cells
			self._wake.notify()

	def close(self) -> None:
		"""Write what is still to be written, then leave tty mode and close, waiting for BRLTTY at most
		DISCONNECT_TIMEOUT seconds."""
		with self._wake:
			self._closing = True
			self._wake.notify()
		self._thread.join(DISCONNECT_TIMEOUT)
		if self._thread.is_alive():
			log.debugWarning("BRLTTY did not answer as its connection was closed")

	def _run(self) -> None:
		lost = False
		while True:
			with self._wake:
				while (self._pending is None or lost) and not self._closing:
					self._wake.wait()
				cells, self._pending = self._pending, None
				if self._closing and (cells is None or lost):
					break
			try:
				self.connection.writeDots(cells)
			# Whatever the binding raises, the connection can take no more cells.
			except Exception:  # noqa: BLE001
				lost = True
				self._onLost()
		# Only once asked: closing frees what a key read on another thread may still be using.
		_close(self.connection)


def connect(setUp: Callable[[Any], None], onLost: Callable[[], None]) -> Link:
	"""A connection to BRLTTY in tty mode, with the size of its display and the name of its driver.

	Under X the client library takes the virtual terminal of the session from WINDOWPATH, from
	XDG_VTNR or from logind, so that BRLTTY shows this client while that terminal is in front.

	:param setUp: Given the connection in tty mode, on the connecting thread.
	:param onLost: Called, on the link's thread, when a write finds the connection gone.
	:raise NoDisplayError: If BRLTTY drives no display, which would leave NVDA writing to none.
	"""
	import brlapi

	def openConnection() -> tuple[Any, tuple[int, int], str]:
		connection = brlapi.Connection()
		try:
			connection.enterTtyModeWithPath()
			setUp(connection)
			columns, rows = connection.displaySize
			if not columns * rows:
				raise NoDisplayError("BRLTTY has no braille display")
			return connection, (columns, rows), connection.driverName.decode()
		except BaseException:
			_close(connection)
			raise

	connection, displaySize, driverName = callWithin(
		CONNECT_TIMEOUT, openConnection, abandoned=lambda opened: _close(opened[0])
	)
	return Link(connection, displaySize, driverName, onLost)


def _close(connection: Any) -> None:
	try:
		connection.leaveTtyMode()
	except Exception:  # noqa: BLE001, S110
		# Not in tty mode, or the connection is already gone.
		pass
	connection.closeConnection()


def deviceMatch():
	"""What a scan reports for BRLTTY: a device of NVDA's own kind for drivers that find their
	displays themselves."""
	import bdDetect

	return bdDetect.DeviceMatch(
		type=bdDetect.ProtocolType.CUSTOM,
		id="BrlAPI",
		port=os.environ.get("BRLAPI_HOST") or localSocketPath(),
		deviceInfo={},
	)


def onMainThread(call: Callable[[], Any]) -> None:
	"""Run call on the main thread: now if this is it, or from wx's main loop once it gets there. Without
	a wx application there is no main loop to run it, and it is dropped.

	wx makes and starts timers only on the main thread, and the scan and Automatic's driver run on the
	detection thread. wx.IsMainThread cannot tell before a wx application exists.
	"""
	if threading.current_thread() is threading.main_thread():
		call()
		return
	import wx

	if wx.GetApp() is not None:
		wx.CallAfter(call)


def scan(limitToDevices: list[str] | None = None, **_kwargs):
	"""bdDetect's device scanner for BRLTTY: offers it to the detection while its socket is there."""
	if limitToDevices and DRIVER_NAME not in limitToDevices:
		return
	try:
		onMainThread(_watcher.start)
	except Exception:  # noqa: BLE001
		# The detection logs whatever a scanner raises as an error; retrying later is only a nicety.
		log.debugWarning("Could not start looking for BRLTTY again", exc_info=True)
	if not isReachable():
		return
	_watcher.noteAttempt()
	yield DRIVER_NAME, deviceMatch()


class _Watcher:
	"""Tries BRLTTY again while no display is in use; see the module's description.

	A wx timer on the main thread rather than a thread of its own: it runs only while NVDA's main loop
	does, leaves no thread behind for terminate to stop, and stops itself once there is nothing to do.
	"""

	def __init__(self) -> None:
		self._timer = None
		self._lastAttempt: tuple[tuple[int, int] | None, float] = (None, 0.0)
		self._retry: threading.Thread | None = None

	def start(self) -> None:
		"""Start looking; on the main thread only, through onMainThread."""
		import wx

		if wx.GetApp() is None:
			return
		if self._timer is None:
			self._timer = wx.PyTimer(self._look)
		if not self._timer.IsRunning():
			self._timer.Start(POLL_INTERVAL)

	def stop(self) -> None:
		if self._timer is not None:
			self._timer.Stop()

	def noteAttempt(self) -> None:
		self._lastAttempt = (_socketIdentity(), time.monotonic())

	def _look(self) -> None:
		import braille
		import config
		from braille.constants import AUTO_DISPLAY_NAME, NO_BRAILLE_DISPLAY_NAME

		handler = braille.handler
		if handler is None:
			self.stop()
			return
		chosen = config.conf["braille"]["display"]
		detector = handler._detector if chosen == AUTO_DISPLAY_NAME else None
		if detector is None and chosen != DRIVER_NAME:
			self.stop()
			return
		if handler.display is not None and handler.display.name != NO_BRAILLE_DISPLAY_NAME:
			# A display is in use; losing it starts the looking again.
			self.stop()
			return
		identity = _socketIdentity()
		if identity is None:
			return
		lastIdentity, lastTime = self._lastAttempt
		if identity == lastIdentity and time.monotonic() - lastTime < RETRY_INTERVAL:
			return
		self.noteAttempt()
		if detector is not None:
			# As NVDA's handler polls for Bluetooth displays: a scan with the detector's own settings.
			detector._queueBgScan(
				usb=detector._detectUsb,
				bluetooth=detector._detectBluetooth,
				limitToDevices=detector._limitToDevices,
			)
		elif self._retry is None or not self._retry.is_alive():
			self._retry = threading.Thread(target=self._retryChosen, name=f"{__name__}.retry", daemon=True)
			self._retry.start()

	@staticmethod
	def _retryChosen() -> None:
		import braille
		import config
		from braille.constants import NO_BRAILLE_DISPLAY_NAME

		handler = braille.handler
		if (
			handler is None
			or config.conf["braille"]["display"] != DRIVER_NAME
			or handler.display is None
			or handler.display.name != NO_BRAILLE_DISPLAY_NAME
		):
			return
		# As a detected display: kept out of the configuration, and quiet when it fails.
		handler.setDisplayByName(DRIVER_NAME, isFallback=True, detected=deviceMatch())


_watcher = _Watcher()


def watch() -> None:
	"""Look for BRLTTY again while no display is in use: after a connection is lost, or a chosen
	BRLTTY could not be reached. From any thread."""
	onMainThread(_watcher.start)
