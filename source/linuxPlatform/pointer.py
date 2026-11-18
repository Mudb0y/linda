"""The pointer on the X display: what winUser's mouse functions do on Windows, done through XTEST.

Clicks, button locks, wheel turns and moves are sent as XTEST's pointer device sends them, so an
application has them at the pointer as from a mouse, and the pointer mapping applies to them as it
applies to a mouse's buttons, as Windows' button swap applies to mouse_event's. They go through a
connection of their own, never the key source's, whose thread must answer keys whatever this does.

Every event sent here is remembered until its echo comes back to the watcher, which tells NVDA's
own events from those of other programs that send through XTEST too, as the Windows hook tells
them by NVDA's flag on its own.
"""

import os
import select
import struct
import threading
import time
from collections import deque
from collections.abc import Callable

from linuxPlatform import NoLinuxImplementation

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_XDOWN = 0x0080
MOUSEEVENTF_XUP = 0x0100
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_HWHEEL = 0x1000
MOUSEEVENTF_ABSOLUTE = 0x8000
WHEEL_DELTA = 120
#: The range MOUSEEVENTF_ABSOLUTE's coordinates are normalised to.
ABSOLUTE_RANGE = 65536

#: (down flag, up flag, X button) for each button mouse_event presses, in the order Windows applies them.
BUTTON_FLAGS = (
	(MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP, 1),
	(MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP, 3),
	(MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP, 2),
)
#: X buttons of the extra buttons, by XBUTTON1 and XBUTTON2 in mouse_event's data.
X_BUTTONS = {1: 8, 2: 9}
#: X buttons a wheel turn is sent as: up, down, left, right.
WHEEL_UP, WHEEL_DOWN, WHEEL_LEFT, WHEEL_RIGHT = 4, 5, 6, 7
#: X button behind each virtual-key code of a mouse button, for getKeyState.
BUTTON_VKS = {0x01: 1, 0x02: 3, 0x04: 2, 0x05: 8, 0x06: 9}
#: Seconds an event sent here is waited for, to know its echo.
ECHO_WAIT = 2.0
#: Seconds at least between two moves handed to NVDA, each of which has NVDA find and read the object
#: under the pointer on its main thread; the moves between are let go, the last of them handed on
#: once this has passed.
MOVE_INTERVAL = 0.05
#: Windows' mouse messages, as mouseHandler names them.
WM_MOUSEMOVE = 0x0200
#: X button: the messages for its press and release.
BUTTON_MESSAGES = {1: (0x0201, 0x0202), 3: (0x0204, 0x0205)}

_lock = threading.Lock()
_display = None
#: (deadline, kind, detail) of each event sent here whose echo has not come: kind "button" with the X
#: button pressed or released, or "motion".
_echoes: deque[tuple[float, str, int]] = deque()
_echoLock = threading.Lock()


def isAvailable() -> bool:
	return bool(os.environ.get("DISPLAY"))


def _connection():
	"""The connection events are sent on, opened at first use; call with _lock held."""
	global _display
	if _display is None:
		if not isAvailable():
			raise NoLinuxImplementation("the mouse pointer", "there is no X display")
		from Xlib import display

		connection = display.Display()
		if not connection.has_extension("XTEST"):
			connection.close()
			raise NoLinuxImplementation("the mouse pointer", "the X server has no XTEST extension")
		_display = connection
	return _display


def close() -> None:
	global _display
	with _lock:
		if _display is not None:
			_display.close()
			_display = None


def _expectEcho(kind: str, detail: int = 0) -> None:
	with _echoLock:
		_echoes.append((time.monotonic() + ECHO_WAIT, kind, detail))


def isOwnEcho(kind: str, detail: int = 0) -> bool:
	"""Whether an event XTEST's device sent is the echo of one sent here, which it then consumes."""
	now = time.monotonic()
	with _echoLock:
		while _echoes and _echoes[0][0] < now:
			_echoes.popleft()
		for index, (_deadline, expectedKind, expectedDetail) in enumerate(_echoes):
			if expectedKind == kind and (kind == "motion" or expectedDetail == detail):
				del _echoes[index]
				return True
	return False


def _button(connection, button: int, down: bool) -> None:
	from Xlib import X
	from Xlib.ext import xtest

	_expectEcho("button", button)
	xtest.fake_input(connection, X.ButtonPress if down else X.ButtonRelease, button)


def mouseEvent(flags: int, dx: int, dy: int, data: int = 0, extraInfo: int = 0) -> None:
	"""Do what user32.mouse_event does with these arguments, at the pointer on the X display."""
	from Xlib import X
	from Xlib.ext import xtest

	with _lock:
		connection = _connection()
		if flags & MOUSEEVENTF_MOVE:
			if flags & MOUSEEVENTF_ABSOLUTE:
				geometry = connection.screen().root.get_geometry()
				x = dx * geometry.width // ABSOLUTE_RANGE
				y = dy * geometry.height // ABSOLUTE_RANGE
				_expectEcho("motion")
				xtest.fake_input(connection, X.MotionNotify, False, x=x, y=y)
			elif dx or dy:
				_expectEcho("motion")
				xtest.fake_input(connection, X.MotionNotify, True, x=dx, y=dy)
		for downFlag, upFlag, button in BUTTON_FLAGS:
			if flags & downFlag:
				_button(connection, button, True)
			if flags & upFlag:
				_button(connection, button, False)
		if flags & (MOUSEEVENTF_XDOWN | MOUSEEVENTF_XUP) and data in X_BUTTONS:
			if flags & MOUSEEVENTF_XDOWN:
				_button(connection, X_BUTTONS[data], True)
			if flags & MOUSEEVENTF_XUP:
				_button(connection, X_BUTTONS[data], False)
		if flags & (MOUSEEVENTF_WHEEL | MOUSEEVENTF_HWHEEL) and data:
			if flags & MOUSEEVENTF_WHEEL:
				button = WHEEL_UP if data > 0 else WHEEL_DOWN
			else:
				button = WHEEL_RIGHT if data > 0 else WHEEL_LEFT
			# X has no fine wheel steps to send: a turn of less than a notch is still one.
			for _ in range(max(1, round(abs(data) / WHEEL_DELTA))):
				_button(connection, button, True)
				_button(connection, button, False)
		connection.sync()


def moveTo(x: int, y: int) -> None:
	"""Move the pointer to (x, y) on the screen, as SetPhysicalCursorPos does, and as a mouse moves it,
	so that applications hear it come."""
	from Xlib import X
	from Xlib.ext import xtest

	with _lock:
		connection = _connection()
		_expectEcho("motion")
		xtest.fake_input(connection, X.MotionNotify, False, x=int(x), y=int(y))
		connection.sync()


def position() -> tuple[int, int]:
	"""Where the pointer is on the screen."""
	with _lock:
		reply = _connection().screen().root.query_pointer()
	return reply.root_x, reply.root_y


def _clientWindow(connection, window, depth: int = 3):
	"""The application's own window in window, which a window manager reparents into a frame of its
	own: the one with WM_STATE, which the window manager puts on the windows it manages."""
	from Xlib import X

	if window.get_full_property(connection.intern_atom("WM_STATE"), X.AnyPropertyType) is not None:
		return window
	if depth == 0:
		return None
	# Uppermost first, as the query lists them bottom to top.
	for child in reversed(window.query_tree().children):
		found = _clientWindow(connection, child, depth - 1)
		if found is not None:
			return found
	return None


def processAt(x: int, y: int) -> int | None:
	"""The process whose window is uppermost at (x, y) on the screen, as its _NET_WM_PID says; None when
	no window there says."""
	from Xlib import X

	with _lock:
		connection = _connection()
		root = connection.screen().root
		child = root.translate_coords(root, x, y).child
		if not child:
			return None
		client = _clientWindow(connection, child)
		if client is None:
			return None
		pid = client.get_full_property(connection.intern_atom("_NET_WM_PID"), X.AnyPropertyType)
	return int(pid.value[0]) if pid is not None and len(pid.value) else None


def _logicalButton(connection, button: int) -> int:
	mapping = connection.get_pointer_mapping()
	return mapping[button - 1] if 0 < button <= len(mapping) else button


def buttonsSwapped() -> bool:
	"""Whether the left button acts as the right one, as GetSystemMetrics(SM_SWAPBUTTON) says it."""
	try:
		with _lock:
			return _logicalButton(_connection(), 1) == 3
	except NoLinuxImplementation:
		return False


def keyState(vkCode: int) -> int:
	"""The state of a mouse button, as winUser.getKeyState gives it: bit 15 set while it is down.

	NVDA asks to know whether it locked a button, which it cannot have done before it sent anything,
	so until then the display is left alone: NVDA asks at exit too."""
	if _display is None:
		return 0
	try:
		with _lock:
			connection = _connection()
			logical = _logicalButton(connection, BUTTON_VKS[vkCode])
			mask = connection.screen().root.query_pointer().mask
	except NoLinuxImplementation:
		return 0
	# The core protocol has a mask bit for the first five buttons alone.
	if not 1 <= logical <= 5:
		return 0
	return 1 << 15 if mask & (1 << (7 + logical)) else 0


def refreshMouseObject() -> None:
	"""Make NVDA's mouse object the one under the pointer now.

	On Windows it is the one the last mouse move found, which is stale while mouse tracking is off,
	as moves are then not followed; asked for, it is better found where the pointer is."""
	import api
	from logHandler import log

	try:
		x, y = position()
	except NoLinuxImplementation:
		return
	try:
		obj = api.getDesktopObject().objectFromPoint(x, y)
	# Whatever the application under the pointer raises: the mouse object stays as it was.
	except Exception:  # noqa: BLE001
		log.debugWarning(f"Finding the object at {x}, {y}", exc_info=True)
		return
	while obj and obj.beTransparentToMouse:
		obj = obj.parent
	if obj:
		api.setMouseObject(obj)


class Watcher:
	"""Follows the pointer on a connection and a thread of their own, and hands each move and each
	press or release of the left or right button to deliver, as the Windows mouse hook does.

	Raw events on the root window report every move and button of every device, whatever window has
	them; the pointer's place is asked once all the events that came together are read, so that a
	storm of motion costs one question, and the moves handed on are at least MOVE_INTERVAL apart.
	Nothing here touches the key source, whose keys never wait for the pointer.
	"""

	def __init__(self, deliver: Callable[[int, int, int, bool], object]) -> None:
		self._deliver = deliver
		self._display = None
		self._xtestDevices: set[int] = set()
		self._stopping = False
		self._wakeRead, self._wakeWrite = os.pipe()
		self._thread = threading.Thread(target=self._run, name="lindaPointerWatch", daemon=True)
		#: Whether a move has come that has not been handed on, and whether all such moves were sent
		#: by a program.
		self._moved = False
		self._movedInjected = True
		self._lastMove = 0.0
		self._lastPosition: tuple[int, int] | None = None

	def start(self) -> None:
		from Xlib import display
		from Xlib.ext import xinput

		self._display = display.Display()
		if not self._display.has_extension("XInputExtension"):
			self._display.close()
			raise NoLinuxImplementation("mouse tracking", "the X server has no XInput extension")
		self._display.screen().root.xinput_select_events(
			[
				(
					xinput.AllMasterDevices,
					xinput.RawMotionMask | xinput.RawButtonPressMask | xinput.RawButtonReleaseMask,
				),
				(xinput.AllDevices, xinput.HierarchyChangedMask),
			],
		)
		self._findXtestDevices()
		self._lastPosition = self._position()
		self._display.sync()
		self._thread.start()

	def stop(self) -> None:
		self._stopping = True
		try:
			os.write(self._wakeWrite, b"\0")
		except OSError:
			pass
		if self._thread.is_alive():
			self._thread.join(2)
		os.close(self._wakeRead)
		os.close(self._wakeWrite)

	def _findXtestDevices(self) -> None:
		from Xlib.ext import xinput

		devices = self._display.xinput_query_device(xinput.AllDevices).devices
		self._xtestDevices = {device.deviceid for device in devices if "XTEST" in str(device.name)}

	def _run(self) -> None:
		from logHandler import log
		from Xlib import error

		d = self._display
		try:
			while not self._stopping:
				while d.pending_events():
					self._handle(d.next_event())
				timeout = None
				if self._moved:
					due = self._lastMove + MOVE_INTERVAL
					now = time.monotonic()
					if now >= due:
						self._handMove(now)
					else:
						timeout = due - now
				readable, _w, _x = select.select([d.fileno(), self._wakeRead], [], [], timeout)
				if self._wakeRead in readable:
					os.read(self._wakeRead, 4096)
		except (error.ConnectionClosedError, OSError):
			if not self._stopping:
				log.warning("The X server closed the pointer's connection; the mouse is no longer followed")
		except Exception:
			log.exception("The mouse stopped being followed")
		finally:
			try:
				d.close()
			except (OSError, error.ConnectionClosedError):
				pass

	def _handle(self, event) -> None:
		from Xlib.ext import xinput

		evtype = getattr(event, "evtype", None)
		if evtype not in (xinput.RawMotion, xinput.RawButtonPress, xinput.RawButtonRelease):
			if evtype == xinput.HierarchyChanged:
				self._findXtestDevices()
			return
		data = event.data
		if isinstance(data, (bytes, bytearray)):
			_deviceid, _time, detail, sourceid = struct.unpack_from("=HIIH", data, 0)
		else:
			detail, sourceid = data.detail, data.sourceid
		injected = sourceid in self._xtestDevices
		if evtype == xinput.RawMotion:
			if injected and isOwnEcho("motion"):
				return
			self._moved = True
			self._movedInjected = self._movedInjected and injected
			return
		if injected and isOwnEcho("button", detail):
			return
		messages = BUTTON_MESSAGES.get(detail)
		if messages is None:
			return
		x, y = self._position()
		self._call(messages[1] if evtype == xinput.RawButtonRelease else messages[0], x, y, injected)

	def _position(self) -> tuple[int, int]:
		reply = self._display.screen().root.query_pointer()
		return reply.root_x, reply.root_y

	def _handMove(self, now: float) -> None:
		injected = self._movedInjected
		self._moved = False
		self._movedInjected = True
		position = self._position()
		if position == self._lastPosition:
			return
		self._lastPosition = position
		self._lastMove = now
		self._call(WM_MOUSEMOVE, *position, injected)

	def _call(self, message: int, x: int, y: int, injected: bool) -> None:
		from logHandler import log

		try:
			self._deliver(message, x, y, injected)
		except Exception:
			log.exception("Handing a mouse event to NVDA")


_watcher: Watcher | None = None


def startWatching() -> None:
	"""Start following the pointer, handing its moves and clicks to NVDA as the Windows hook does."""
	global _watcher
	from logHandler import log

	from linuxPlatform import inputHook, keys

	# With the keyboard left alone the pointer is too: NVDA is then at rest, touching no device.
	if _watcher is not None or not isAvailable() or not keys.isEnabled():
		return

	watcher = Watcher(inputHook.deliverMouse)
	try:
		watcher.start()
	except Exception:
		log.warning("NVDA cannot follow the mouse on the X display", exc_info=True)
		return
	_watcher = watcher


def stopWatching() -> None:
	global _watcher
	watcher, _watcher = _watcher, None
	if watcher is not None:
		watcher.stop()
