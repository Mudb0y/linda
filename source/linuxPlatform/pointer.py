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
import threading
import time
from collections import deque

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
	"""The state of a mouse button, as winUser.getKeyState gives it: bit 15 set while it is down."""
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
