"""Keys from the X server, read on a thread of their own through a connection of their own.

Every key press and release comes from XInput 2 events selected on the root window: the slave
keyboard's own events, which carry key repeat and the XKB state the key was pressed in, and raw
events, which no client can hide. A raw event whose slave event does not follow is delivered on its
own, since an application that selects slave events on its window stops them reaching the root.
The NVDA keys and the numpad review keys are taken with passive grabs on the window that has the
keyboard focus, moved as the focus moves, so a chord with the NVDA key held goes to NVDA alone while
the grab is active; everything else reaches the application too, and is marked as having done so.
A key such a grab took that NVDA passes, as one NVDA has no command for, is sent on to the
application, as the Windows hook lets through what its callback does not block.

The keys of browse mode are captured with synchronous passive grabs on the window that has the
keyboard focus: the X server holds the whole keyboard on such a press until this source answers it,
replaying the key to the application as typed or dropping it. NVDA's answer comes from the decider,
a thread that hands every key to NVDA in order; the X thread only reads events and answers them,
and replays a captured key NVDA has not answered within CAPTURE_TIMEOUT, so that a slow NVDA costs
the user a delay and never a key. Should this whole process stop, no thread is left to answer, and
the keyboard would stay held: a guard, a process of its own that this one feeds a heartbeat, then
lets the keyboard go and closes this source's connection to the X server; the source connects again
once it runs again.
"""

import functools
import itertools
import json
import os
import queue
import select
import struct
import subprocess
import sys
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field, replace

from logHandler import log
from xkbcommon import xkb
from Xlib import X, display, error
from Xlib.ext import xinput, xtest
from Xlib.protocol import rq

from . import CaptureMode, GestureKey, KeyEvent, keyMap

XKB_EXTENSION = "XKEYBOARD"
#: XkbUseCoreKbd: the keyboard the core protocol uses, the master keyboard.
XKB_CORE_KEYBOARD = 0x100


class _XkbUseExtension(rq.ReplyRequest):
	"""The request the X server must have from a client before any other of the XKB extension."""

	_request = rq.Struct(
		rq.Card8("opcode"),
		rq.Opcode(0),
		rq.RequestLength(),
		rq.Card16("wantedMajor"),
		rq.Card16("wantedMinor"),
	)
	_reply = rq.Struct(
		rq.ReplyCode(),
		rq.Bool("supported"),
		rq.Card16("sequence_number"),
		rq.ReplyLength(),
		rq.Card16("serverMajor"),
		rq.Card16("serverMinor"),
		rq.Pad(20),
	)


class _XkbGetState(rq.ReplyRequest):
	_request = rq.Struct(
		rq.Card8("opcode"),
		rq.Opcode(4),
		rq.RequestLength(),
		rq.Card16("deviceSpec"),
		rq.Pad(2),
	)
	_reply = rq.Struct(
		rq.ReplyCode(),
		rq.Card8("deviceID"),
		rq.Card16("sequence_number"),
		rq.ReplyLength(),
		rq.Card8("mods"),
		rq.Card8("baseMods"),
		rq.Card8("latchedMods"),
		rq.Card8("lockedMods"),
		rq.Pad(20),
	)


class _XkbLatchLockState(rq.Request):
	"""Set the locked modifiers without a key, so that no grab and no application sees one."""

	_request = rq.Struct(
		rq.Card8("opcode"),
		rq.Opcode(5),
		rq.RequestLength(),
		rq.Card16("deviceSpec"),
		rq.Card8("affectModLocks"),
		rq.Card8("modLocks"),
		rq.Bool("lockGroup"),
		rq.Card8("groupLock"),
		rq.Card8("affectModLatches"),
		rq.Card8("modLatches"),
		rq.Pad(1),
		rq.Bool("latchGroup"),
		rq.Int16("groupLatch"),
	)


#: Modifiers that make a key a command rather than typing: Control, Alt and Super, as X maps them.
COMMAND_MODS = X.ControlMask | X.Mod1Mask | X.Mod4Mask
#: Num Lock's modifier, as X maps it.
NUM_LOCK_MASK = X.Mod2Mask
#: Modifiers a numpad review grab is made with every combination of: Shift, which NVDA's review
#: commands use, and the locks and AltGr, which would otherwise make the grab miss.
NUMPAD_GRAB_MODS = (X.ShiftMask, X.LockMask, X.Mod3Mask, X.Mod5Mask)
#: evdev codes of the numpad review keys: 1 to 9, plus, divide and multiply.
NUMPAD_REVIEW_KEYS = (71, 72, 73, 75, 76, 77, 79, 80, 81, 78, 98, 55)
INSERT_KEY = 110
NUMPAD_INSERT_KEY = 82
CAPS_LOCK_KEY = 58
NVDA_KEYCODES = (INSERT_KEY + 8, NUMPAD_INSERT_KEY + 8, CAPS_LOCK_KEY + 8)
#: config.configFlags.NVDAKey, which this must not import: config imports far too much for a thread
#: started this early to wait on.
CAPS_LOCK, NUMPAD_INSERT, EXTENDED_INSERT = 1, 2, 4
#: Seconds a raw event waits for its slave event before it is delivered on its own.
RAW_WAIT = 0.02
#: Seconds a key injected here is waited for, to drop its echo.
ECHO_WAIT = 2.0
#: XI 2.2, or master raw events vanish while a client holds a grab.
XI_VERSION = (2, 2)
RULES_NAMES_PROPERTY = "_XKB_RULES_NAMES"
#: What a keymap is built from when the root window does not say.
DEFAULT_RULES_NAMES = ("evdev", "pc105", "us", "", "")
#: Virtual-key codes of keys a grab took that are never sent on to the application when NVDA passes
#: them: the modifiers, which change nothing but the state the next key comes in, and the locks, whose
#: state the X server changed as they went down.
NEVER_SENT_ON = frozenset(
	keyMap.VK[name]
	for name in (
		"SHIFT",
		"CONTROL",
		"MENU",
		"LSHIFT",
		"RSHIFT",
		"LCONTROL",
		"RCONTROL",
		"LMENU",
		"RMENU",
		"LWIN",
		"RWIN",
		"CAPITAL",
		"NUMLOCK",
		"SCROLL",
	)
)
#: Virtual-key code of each lock key: the modifier it locks, as X maps it.
LOCK_MASKS = {keyMap.VK["CAPITAL"]: X.LockMask, keyMap.VK["NUMLOCK"]: NUM_LOCK_MASK}
#: Modifiers a key sent on is not given again when they were let go of: the locks, and Caps Lock held
#: as the NVDA key, whose modifier the X server holds with it.
SENT_ON_WITHOUT = X.LockMask | NUM_LOCK_MASK
#: The generic modifier each left or right one also holds down, for keyState.
GENERIC_FOR = {
	keyMap.VK["LSHIFT"]: keyMap.VK["SHIFT"],
	keyMap.VK["RSHIFT"]: keyMap.VK["SHIFT"],
	keyMap.VK["LCONTROL"]: keyMap.VK["CONTROL"],
	keyMap.VK["RCONTROL"]: keyMap.VK["CONTROL"],
	keyMap.VK["LMENU"]: keyMap.VK["MENU"],
	keyMap.VK["RMENU"]: keyMap.VK["MENU"],
}

#: Seconds the X server holds the keyboard on a captured press for NVDA's answer before the key is
#: replayed to the application without it.
CAPTURE_TIMEOUT = 0.1
#: Seconds a captured press waits for the X server to hold it; it does not when another program had
#: the keyboard grabbed as the key went down.
CAPTURE_EXPIRY = 0.25
#: Each captured combination is grabbed once with each of these, so that the locks do not make the
#: grab miss. AnyModifier would cover them too, but the window manager's own grabs refuse it.
LOCK_COMBINATIONS = (0, X.LockMask, NUM_LOCK_MASK, X.LockMask | NUM_LOCK_MASK)
#: evdev code: the modifier combinations it is captured with in browse mode, besides the character
#: keys. These are browse mode's own keys: cursorManager's, browseMode's, and its quick navigation.
BROWSE_KEYS: dict[int, tuple[str, ...]] = {
	# Up and down arrows: line, paragraph, selection and, with Alt, sentence.
	103: ("", "control", "shift", "shift+control", "alt"),
	108: ("", "control", "shift", "shift+control", "alt"),
	# Left and right arrows.
	105: ("", "control", "shift", "shift+control"),
	106: ("", "control", "shift", "shift+control"),
	# Page up and down.
	104: ("", "shift"),
	109: ("", "shift"),
	# Home and End.
	102: ("", "control", "shift", "shift+control"),
	107: ("", "control", "shift", "shift+control"),
	# Enter and numpad Enter, to activate or to pass through.
	28: ("", "control", "shift", "shift+control", "alt"),
	96: ("", "control", "shift", "shift+control", "alt"),
	1: ("",),
	15: ("", "shift"),
	# The applications key, and Shift+F10.
	127: ("", "shift"),
	68: ("shift",),
}
#: Virtual-key codes captured with Control in browse mode: select all and copy.
BROWSE_CONTROL_LETTERS = ("A", "C")
#: Modifier combinations every key that types a character is captured with in browse mode.
CHARACTER_COMBINATIONS = ("", "shift", "altgr", "shift+altgr")
SPACE_KEY = 57
#: Modifier names of NVDA's gestures, as X maps them.
GESTURE_MASKS = {"shift": X.ShiftMask, "control": X.ControlMask, "alt": X.Mod1Mask, "windows": X.Mod4Mask}
#: evdev code: modifier combinations captured in focus mode, where Escape goes back to browse mode.
FOCUS_KEYS: dict[int, tuple[str, ...]] = {1: ("",)}

#: Seconds between heartbeats to the guard while it guards.
HEARTBEAT_INTERVAL = 0.5
#: Seconds of silence after which the guard frees the keyboard.
GUARD_SILENCE = 2.0
#: Attempts, a second apart, to connect again after the X server closed the connection.
RECONNECT_ATTEMPTS = 10
#: The guard. It shares nothing with NVDA's interpreter, so that it runs whatever NVDA's threads do.
#: Its standard input carries the heartbeat, "STATE RESOURCE WINDOW": STATE 1 while the keyboard can
#: be held and 0 while it cannot, RESOURCE one of the source's connection, WINDOW the one the captured
#: keys are grabbed on. After GUARD_SILENCE without one while the keyboard can be held, it frees the
#: keyboard and closes that connection; it ends when its input does.
#:
#: Closing the connection alone would free the keyboard but lose every key held up behind the
#: captured press: the X server ends a closing client's active grab before its passive ones, and
#: each held-up press then activates a passive grab of the client that is going and goes with it.
#: So the guard first writes to the source's own connection, whose socket it was handed, the
#: requests the source would have made: release every passive key grab on the root and on the
#: window, replay the held press to the application, and let go of the keyboard. The source's next
#: request would then be numbered wrongly, which is one more reason to close the connection after
#: them.
GUARD_PROGRAM = r"""
import json, os, select, struct, sys, time
socket, root = int(sys.argv[1]), int(sys.argv[2])
sys.path[:0] = json.loads(sys.argv[3])
silence = float(sys.argv[4])
from Xlib import X, display, error
from Xlib.xobject.resource import Resource


def free(window):
	# UngrabKey, AllowEvents and UngrabKeyboard, in the byte order the source's connection uses: this
	# machine's.
	requests = b"".join(
		struct.pack("=BBHIHxx", 34, X.AnyKey, 3, grabWindow, X.AnyModifier)
		for grabWindow in {root, window or root}
	)
	requests += struct.pack("=BBHI", 35, X.ReplayKeyboard, 2, X.CurrentTime)
	requests += struct.pack("=BxHI", 32, 2, X.CurrentTime)
	try:
		os.write(socket, requests)
	except OSError:
		pass


connection = display.Display()
guarding, resource, window, heard, pending = False, 0, 0, time.monotonic(), b""
while True:
	timeout = max(0.0, heard + silence - time.monotonic()) if guarding and resource else None
	readable, _w, _x = select.select([0], [], [], timeout)
	if readable:
		chunk = os.read(0, 4096)
		if not chunk:
			# NVDA has gone, or let the connection go; this guard's copy of the socket is the last.
			if guarding:
				free(window)
			break
		*lines, pending = (pending + chunk).split(b"\n")
		for line in lines:
			state, number, grabWindow = (line.split() + [b"0", b"0", b"0"])[:3]
			guarding, resource, window = state == b"1", int(number), int(grabWindow)
			heard = time.monotonic()
	elif guarding and resource:
		free(window)
		try:
			Resource(connection.display, resource).kill_client()
			connection.sync()
		except error.XError:
			pass
		resource = 0
"""


@dataclass
class _Ticket:
	"""A captured press, which the X server holds the keyboard on until it is answered."""

	keycode: int
	#: time.monotonic() when the source learnt of the press.
	arrival: float
	#: True to replay the key to the application, False to drop it; None until NVDA says.
	verdict: bool | None = None
	#: Whether the X server's held press has come.
	frozen: bool = False
	#: Whether it has been answered, by its verdict or without one.
	answered: bool = False
	#: Set once the X server's held press has come, or once it is known that none will.
	settled: threading.Event = field(default_factory=threading.Event)


class X11KeySource:
	"""A KeySource reading the X display named by DISPLAY."""

	def __init__(self) -> None:
		self._lock = threading.Lock()
		self._commands: deque[Callable[[], None]] = deque()
		self._wakeRead, self._wakeWrite = os.pipe()
		self._stopping = False
		self._thread: threading.Thread | None = None
		self._decider: threading.Thread | None = None
		#: (event, ticket, key code, modifiers, whether a grab of this source took the press) for the
		#: decider, in the order the keys came; None ends it.
		self._decisions: queue.SimpleQueue[tuple[KeyEvent, _Ticket | None, int, int, bool] | None] = (
			queue.SimpleQueue()
		)
		self._deliver: Callable[[KeyEvent], bool] | None = None
		self._display: display.Display | None = None
		self._rulesNames: tuple[str, ...] | None = None
		self._context = xkb.Context()
		self._translator: keyMap.Translator | None = None
		#: Virtual-key codes held down, with the generic modifiers among them.
		self._down: set[int] = set()
		#: Key codes held down, as their events came.
		self._keycodesDown: set[int] = set()
		#: Virtual-key code of each lock key held down: whether its lock was on as it went down.
		self._locksHeld: dict[int, bool] = {}
		self._xtestDevices: set[int] = set()
		#: (key code, isUp) of injected keys whose echo has not come yet, with when each was sent.
		self._echoes: deque[tuple[float, int, bool]] = deque()
		#: Raw events waiting for their slave event: (arrival, key code, isUp, source device).
		self._raw: deque[tuple[float, int, bool, int]] = deque()
		#: (key code, isUp) delivered from a raw event, whose late slave event must not come twice.
		self._rawDelivered: deque[tuple[float, int, bool, int]] = deque()
		#: (key code, isUp) of slave events that came before their raw event.
		self._slaveSeen: deque[tuple[float, int, bool, int]] = deque()
		self._rulesAtom: int | None = None
		self._pidAtom: int | None = None
		self._activeWindowAtom: int | None = None
		self._xkbOpcode: int | None = None
		#: Key code: grab modifiers, for every key grabbed now; None means any modifiers.
		self._grabbed: dict[int, list[int] | None] = {}
		#: The window the NVDA keys and the numpad review keys are grabbed on.
		self._keysWindow = None
		#: The NVDA key pressed again here after a key was sent on while it was held, so that its grab
		#: goes on as long as it is; let go of here too once the keyboard lets it go.
		self._rearmed: int | None = None
		#: The grabbed key whose press made a grab active, which lasts until it is released.
		self._activeGrab: int | None = None
		self._held = False
		#: What was last asked for, to be set up again on a new connection.
		self._consumedWanted: tuple[int, bool] | None = None
		self._holdWanted = False
		self._captureMode: CaptureMode = "off"
		#: The mode NVDA is in at the moment of a key, asked by the decider; None when it cannot say.
		self._modeNow: Callable[[], CaptureMode | None] | None = None
		#: (modifier names, virtual-key code, extended) of the gestures bound in browse mode and in
		#: focus mode beyond the fixed lists, as NVDA's gesture maps have them.
		self._gestureKeys: dict[str, frozenset[GestureKey]] = {"browse": frozenset(), "focus": frozenset()}
		#: Mode: its capture set, made once for each keyboard layout and set of gestures.
		self._modeSets: dict[str, dict[int, set[int]]] = {}
		#: Key code: the modifier masks it is captured with now, the lock combinations among them.
		self._captured: dict[int, set[int]] = {}
		#: The window the captured keys are grabbed on: the one with the keyboard focus.
		self._captureWindow = None
		self._loggedCapture: CaptureMode = "off"
		#: Captured presses not yet answered, in the order they came.
		self._tickets: deque[_Ticket] = deque()
		#: The captured key whose press was dropped, which keeps its grab active until released.
		self._captureGrab: int | None = None
		#: Key code: (vk, scan) a press was delivered as when it was not the key's own, for its release.
		self._pressedAs: dict[int, tuple[int, int]] = {}
		self._guard: subprocess.Popen | None = None
		#: The guard's standard input, written without blocking.
		self._guardInput: int | None = None
		#: The resource of this connection the guard would close it by.
		self._guardResource = 0
		#: (time, text) of the last heartbeat sent.
		self._lastBeat: tuple[float, str] = (0.0, "")

	def start(
		self,
		deliver: Callable[[KeyEvent], bool],
		modeNow: Callable[[], CaptureMode | None] | None = None,
	) -> None:
		self._deliver = deliver
		self._modeNow = modeNow
		self._connect()
		self._startGuard()
		self._decider = threading.Thread(target=self._decide, name="X11KeySource decider", daemon=True)
		self._decider.start()
		self._thread = threading.Thread(target=self._run, name="X11KeySource", daemon=True)
		self._thread.start()

	def stop(self) -> None:
		self._stopping = True
		self._wake()
		self._decisions.put(None)
		if self._thread is not None:
			self._thread.join(2)
			self._thread = None
		if self._decider is not None:
			self._decider.join(2)
			self._decider = None
		self._stopGuard()
		os.close(self._wakeRead)
		os.close(self._wakeWrite)

	def setConsumed(self, nvdaModifierKeys: int, numpadReview: bool) -> None:
		self._post(lambda: self._applyGrabs(nvdaModifierKeys, numpadReview))

	def setCapture(self, mode: CaptureMode) -> None:
		self._post(lambda: self._setCapture(mode))

	def setCaptureGestures(self, browse: frozenset[GestureKey], focus: frozenset[GestureKey]) -> None:
		self._post(lambda: self._setGestures(browse, focus))

	def holdKeyboard(self, hold: bool) -> None:
		self._post(lambda: self._hold(hold))

	def setCapsLock(self, on: bool) -> None:
		self._post(lambda: self._setCapsLock(on))

	def inject(self, vkCode: int, scanCode: int, extended: bool, isUp: bool) -> None:
		with self._lock:
			keycode = self._translator.keycodeFor(vkCode, extended) if self._translator else None
			if keycode is None:
				log.debugWarning(f"No key on this keyboard sends virtual key {vkCode:#x}")
				return
			self._echoes.append((time.monotonic(), keycode, isUp))
		self._post(lambda: self._fake(keycode, isUp))

	def keyState(self, vkCode: int) -> int:
		with self._lock:
			state = 0x8000 if vkCode in self._down else 0
			if vkCode in self._locksHeld:
				# A lock key locks as it goes down, and unlocks as it comes up when it was locked: the state
				# it leaves is the one to report while it is held, as Windows toggles as the key goes down.
				return state | int(not self._locksHeld[vkCode])
			led = {keyMap.VK["CAPITAL"]: "Caps Lock", keyMap.VK["NUMLOCK"]: "Num Lock"}.get(vkCode)
			if vkCode == keyMap.VK["SCROLL"]:
				led = "Scroll Lock"
			if led and self._translator is not None:
				try:
					if self._translator.state.led_name_is_active(led):
						state |= 1
				except xkb.XKBError:
					pass
			return state

	def charForVk(self, vkCode: int) -> int:
		with self._lock:
			return self._translator.mapVirtualKeyToChar(vkCode) if self._translator else 0

	def vkForChar(self, ch: str) -> tuple[int, int]:
		with self._lock:
			if self._translator is None:
				raise LookupError(ch)
			return self._translator.vkKeyScan(ch)

	def keyName(self, scanCode: int, extended: bool) -> str:
		with self._lock:
			return self._translator.keyName(scanCode, extended) if self._translator else ""

	def _post(self, command: Callable[[], None]) -> None:
		self._commands.append(command)
		self._wake()

	def _wake(self) -> None:
		try:
			os.write(self._wakeWrite, b"\0")
		except OSError:
			pass

	def _connect(self) -> None:
		self._display = display.Display()
		try:
			if not self._display.has_extension("XInputExtension"):
				raise RuntimeError("the X server has no XInput extension")
			version = xinput.XIQueryVersion(
				display=self._display.display,
				opcode=self._display.display.get_extension_major(xinput.extname),
				major_version=XI_VERSION[0],
				minor_version=XI_VERSION[1],
			)
			if (version.major_version, version.minor_version) < XI_VERSION:
				raise RuntimeError(
					f"the X server has XInput {version.major_version}.{version.minor_version}, not 2.2",
				)
			root = self._display.screen().root
			root.xinput_select_events(
				[
					(
						xinput.AllDevices,
						xinput.KeyPressMask | xinput.KeyReleaseMask | xinput.HierarchyChangedMask,
					),
					(xinput.AllMasterDevices, xinput.RawKeyPressMask | xinput.RawKeyReleaseMask),
				],
			)
			# The root has focus events only when the focus leaves or reaches the root itself, or none.
			root.change_attributes(event_mask=X.PropertyChangeMask | X.FocusChangeMask)
			self._rulesAtom = self._display.intern_atom(RULES_NAMES_PROPERTY)
			self._activeWindowAtom = self._display.intern_atom("_NET_ACTIVE_WINDOW")
			self._pidAtom = self._display.intern_atom("_NET_WM_PID")
			# Any resource of the connection names it to the X server; the guard needs one.
			self._guardResource = root.create_pixmap(1, 1, self._display.screen().root_depth).id
			self._findXtestDevices()
			self._xkbOpcode = None
			# python-xlib has no module for XKB, so has_extension never knows of it.
			xkbInfo = self._display.query_extension(XKB_EXTENSION)
			if xkbInfo is not None:
				opcode = xkbInfo.major_opcode
				if _XkbUseExtension(
					display=self._display.display, opcode=opcode, wantedMajor=1, wantedMinor=0
				).supported:
					self._xkbOpcode = opcode
			self._loadKeymap()
			self._display.sync()
		except Exception:
			self._display.close()
			self._display = None
			raise

	def _reconnect(self) -> bool:
		"""Connect again after the X server closed the connection, and take back every key taken."""
		self._stopGuard()
		try:
			self._display.close()
		except (OSError, error.ConnectionClosedError):
			pass
		self._display = None
		self._grabbed = {}
		self._keysWindow = None
		self._captured = {}
		self._captureWindow = None
		self._activeGrab = None
		self._captureGrab = None
		self._held = False
		for ticket in self._tickets:
			ticket.answered = True
			ticket.settled.set()
		self._tickets.clear()
		self._raw.clear()
		for attempt in range(RECONNECT_ATTEMPTS):
			if self._stopping:
				return False
			try:
				self._connect()
				break
			except Exception:
				if attempt == RECONNECT_ATTEMPTS - 1:
					log.warning(
						"NVDA cannot connect to the X display again, and reads no more keys", exc_info=True
					)
					return False
				time.sleep(1)
		self._startGuard()
		if self._consumedWanted is not None:
			self._applyGrabs(*self._consumedWanted)
		if self._holdWanted:
			self._hold(True)
		self._applyCapture(self._captureMode)
		self._lastBeat = (0.0, "")
		log.info("Keys are read from the X display again")
		return True

	def _run(self) -> None:
		try:
			while not self._stopping:
				try:
					self._serve()
				except (error.ConnectionClosedError, OSError):
					if self._stopping:
						break
					# The guard closes it when this process stopped answering while it held the keyboard.
					log.warning("The X server closed the key source's connection; connecting again")
					if not self._reconnect():
						break
		except Exception:
			log.exception("Keys stopped being read from the X display")
		finally:
			if self._display is not None:
				try:
					self._display.close()
				except (OSError, error.ConnectionClosedError):
					log.debugWarning("Closing the key source's X connection", exc_info=True)

	def _serve(self) -> None:
		d = self._display
		while not self._stopping:
			while self._commands:
				self._commands.popleft()()
			while d.pending_events():
				self._handle(d.next_event())
			now = time.monotonic()
			self._flushRaw(now)
			deadlines = [self._answer(now), self._beat(now)]
			if self._raw:
				deadlines.append(self._raw[0][0] + RAW_WAIT)
			due = min((t for t in deadlines if t is not None), default=None)
			readable, _w, _x = select.select(
				[d.fileno(), self._wakeRead],
				[],
				[],
				None if due is None else max(0, due - time.monotonic()),
			)
			if self._wakeRead in readable:
				os.read(self._wakeRead, 4096)

	def _decide(self) -> None:
		while True:
			item = self._decisions.get()
			if item is None:
				return
			event, ticket, keycode, modifiers, taken = item
			if ticket is not None:
				# Taken for captured from the grabs as they were; whether the server held it decides.
				ticket.settled.wait(CAPTURE_EXPIRY)
				if not ticket.frozen:
					# The application had it as it was typed, as it has every key not captured.
					event = replace(event, captured=False, reachedApplication=True)
					ticket = None
				elif ticket.answered:
					# The application has had the key, as NVDA did not answer in time; NVDA acting on it
					# as well would act twice.
					continue
				elif not self._capturedNow(keycode, modifiers):
					# Grabbed for either mode, and not one NVDA's mode takes at this moment.
					ticket.verdict = True
					self._wake()
					event = replace(event, captured=False, reachedApplication=True)
					ticket = None
			try:
				passed = self._deliver(event)
			except Exception:
				log.exception("Delivering a key")
				passed = True
			if ticket is not None:
				ticket.verdict = passed is not False
				self._wake()
			elif taken and passed is not False:
				# The Windows hook passes a key its callback does not block, whatever else is held.
				self._post(functools.partial(self._sendOn, keycode, modifiers))

	def _findXtestDevices(self) -> None:
		devices = self._display.xinput_query_device(xinput.AllDevices).devices
		self._xtestDevices = {device.deviceid for device in devices if "XTEST" in str(device.name)}

	def _readRulesNames(self) -> tuple[str, ...]:
		d = self._display
		prop = d.screen().root.get_full_property(self._rulesAtom, X.AnyPropertyType)
		if prop is None or not prop.value:
			return DEFAULT_RULES_NAMES
		value = prop.value.decode("utf-8", "replace") if isinstance(prop.value, bytes) else str(prop.value)
		names = (value.split("\0") + [""] * 5)[:5]
		return tuple(names)

	def _loadKeymap(self) -> None:
		names = self._readRulesNames()
		if names == self._rulesNames:
			return
		rules, model, layout, variant, options = names
		keymap = self._context.keymap_new_from_names(
			rules=rules or None,
			model=model or None,
			layout=layout or None,
			variant=variant or None,
			options=options or None,
		)
		with self._lock:
			self._translator = keyMap.Translator(keymap, keymap.state_new())
			self._rulesNames = names
		self._modeSets = {}
		log.debug(f"Keyboard layout {layout or 'us'} {variant}".rstrip())
		if self._captureMode != "off":
			# Which keys type characters, and which modifier AltGr is, are the layout's.
			self._applyCapture(self._captureMode)

	def _handle(self, event) -> None:
		evtype = getattr(event, "evtype", None)
		if evtype in (xinput.KeyPress, xinput.KeyRelease):
			data = event.data
			if data.deviceid == data.sourceid:
				self._onSlave(data, evtype == xinput.KeyRelease)
		elif evtype in (xinput.RawKeyPress, xinput.RawKeyRelease):
			data = event.data
			if isinstance(data, (bytes, bytearray)):
				_deviceid, _time, keycode, sourceid = struct.unpack_from("=HIIH", data, 0)
			else:
				keycode, sourceid = data.detail, data.sourceid
			isUp = evtype == xinput.RawKeyRelease
			for index, (_when, slaveKeycode, slaveIsUp, slaveSource) in enumerate(self._slaveSeen):
				if (slaveKeycode, slaveIsUp, slaveSource) == (keycode, isUp, sourceid):
					del self._slaveSeen[index]
					return
			pressWaiting = any((raw[1], raw[2]) == (keycode, False) for raw in self._raw)
			if isUp and keycode not in self._keycodesDown and not pressWaiting:
				# A release of a key that is up, which only a raw event reports: xdotool releases a modifier
				# twice. Delivered once its slave event had been waited for, it could come between that
				# modifier pressed again and the key pressed with it, and NVDA would have the key alone.
				return
			self._raw.append((time.monotonic(), keycode, isUp, sourceid))
		elif evtype == xinput.HierarchyChanged:
			self._findXtestDevices()
		elif event.type == X.KeyPress:
			self._onHeld(event)
		elif event.type == X.KeyRelease:
			if event.detail == self._captureGrab:
				self._captureGrab = None
		elif event.type in (X.FocusOut, X.FocusIn, X.DestroyNotify):
			# The keys are captured on the window with the focus, which this one may no longer be; a
			# grab moves the focus only for as long as it lasts.
			moved = event.type == X.DestroyNotify or event.mode in (X.NotifyNormal, X.NotifyWhileGrabbed)
			window = self._captureWindow
			if moved and event.type != X.FocusIn and window is not None and event.window.id == window.id:
				# Grabbed again when next asked, once NVDA's focus has followed and the mode is known.
				self._dropCapture()
			if moved:
				self._followFocus()
		elif event.type == X.MappingNotify:
			self._display.refresh_keyboard_mapping(event)
			self._loadKeymap()
		elif event.type == X.PropertyNotify:
			if event.atom == self._rulesAtom:
				self._loadKeymap()
			elif event.atom == self._activeWindowAtom:
				self._followFocus()

	def _onSlave(self, data, isUp: bool) -> None:
		now = time.monotonic()
		keycode = data.detail
		repeat = bool(data.flags & xinput.KeyRepeat)
		if not repeat:
			# Matched by device too: a key sent here may be held on the keyboard at the same time.
			for index, (_arrival, rawKeycode, rawIsUp, rawSource) in enumerate(self._raw):
				if (rawKeycode, rawIsUp, rawSource) == (keycode, isUp, data.sourceid):
					del self._raw[index]
					break
			else:
				for index, (_when, rawKeycode, rawIsUp, rawSource) in enumerate(self._rawDelivered):
					if (rawKeycode, rawIsUp, rawSource) == (keycode, isUp, data.sourceid):
						del self._rawDelivered[index]
						return
				# The server sends the raw event first, but nothing promises it.
				self._slaveSeen.append((now, keycode, isUp, data.sourceid))
		self._process(keycode, isUp, repeat, data.mods, data.groups, data.sourceid, now)

	def _onHeld(self, event) -> None:
		"""A core key press, which comes only through a grab; one of a captured key holds the keyboard
		until it is answered."""
		keycode = event.detail
		if self._held or self._activeGrab is not None or self._captureGrab is not None:
			# A grab was active already, so this press activated none and holds nothing.
			return
		for ticket in self._tickets:
			if ticket.keycode == keycode and not ticket.frozen:
				ticket.frozen = True
				ticket.settled.set()
				return
		if not self._captureMatches(keycode, event.state):
			return
		# Its slave event has not come, and may never: deliver it from its raw event.
		now = time.monotonic()
		ticket = _Ticket(keycode, now, frozen=True)
		ticket.settled.set()
		self._tickets.append(ticket)
		for index, (arrival, rawKeycode, rawIsUp, sourceid) in enumerate(self._raw):
			if (rawKeycode, rawIsUp) == (keycode, False):
				del self._raw[index]
				self._rawDelivered.append((now, keycode, False, sourceid))
				self._process(keycode, False, False, None, None, sourceid, arrival, ticket)
				return
		# Delivered already without being taken for captured, or never seen at all: the application
		# gets it, which is where a key goes that NVDA was not asked about.
		ticket.verdict = True

	def _flushRaw(self, now: float) -> None:
		while self._rawDelivered and now - self._rawDelivered[0][0] > 1:
			self._rawDelivered.popleft()
		while self._slaveSeen and now - self._slaveSeen[0][0] > 1:
			self._slaveSeen.popleft()
		while self._raw and now - self._raw[0][0] >= RAW_WAIT:
			arrival, keycode, isUp, sourceid = self._raw.popleft()
			self._rawDelivered.append((now, keycode, isUp, sourceid))
			self._process(keycode, isUp, False, None, None, sourceid, arrival)

	def _isEchoPending(self, keycode: int, isUp: bool) -> bool:
		"""Whether a key sent here is still expected back."""
		return any(
			(echoKeycode, echoIsUp) == (keycode, isUp) for _sent, echoKeycode, echoIsUp in self._echoes
		)

	def _isEcho(self, keycode: int, isUp: bool, now: float) -> bool:
		with self._lock:
			while self._echoes and now - self._echoes[0][0] > ECHO_WAIT:
				self._echoes.popleft()
			for index, (_sent, echoKeycode, echoIsUp) in enumerate(self._echoes):
				if (echoKeycode, echoIsUp) == (keycode, isUp):
					del self._echoes[index]
					return True
		return False

	def _process(self, keycode, isUp, repeat, mods, groups, sourceid, arrival, ticket=None) -> None:
		with self._lock:
			translator = self._translator
			state = translator.state
			if mods is not None:
				state.update_mask(
					mods.base_mods,
					mods.latched_mods,
					mods.locked_mods,
					groups.base_group,
					groups.latched_group,
					groups.locked_group,
				)
				effective = mods.effective_mods
			else:
				effective = state.serialize_mods(xkb.StateComponent.XKB_STATE_MODS_EFFECTIVE)
			vk, scan, extended = translator.translate(keycode)
			levelShift = translator.isLevelShift(keycode)
			text = "" if isUp else state.key_get_string(keycode)
			level = translator.level(keycode)
			lockedBefore = state.serialize_mods(xkb.StateComponent.XKB_STATE_MODS_LOCKED)
			capsLockWasOn = bool(lockedBefore & X.LockMask)
			if not repeat:
				# What the server's state is after this key, for keyState until the next event says.
				state.update_key(
					keycode, xkb.KeyDirection.XKB_KEY_UP if isUp else xkb.KeyDirection.XKB_KEY_DOWN
				)
				if isUp:
					self._keycodesDown.discard(keycode)
					self._locksHeld.pop(vk, None)
					if keycode == self._rearmed and not (
						sourceid in self._xtestDevices and self._isEchoPending(keycode, True)
					):
						self._rearmed = None
						if sourceid not in self._xtestDevices:
							# The keyboard let it go; so does the device it was pressed again on.
							self._echoes.append((time.monotonic(), keycode, True))
							xtest.fake_input(self._display, X.KeyRelease, keycode)
				else:
					self._keycodesDown.add(keycode)
					if vk in LOCK_MASKS:
						self._locksHeld[vk] = bool(lockedBefore & LOCK_MASKS[vk])
			generic = GENERIC_FOR.get(vk)
			for code in (vk, generic):
				if code is None:
					continue
				if isUp:
					self._down.discard(code)
				else:
					self._down.add(code)
		consumed = self._held or self._activeGrab is not None or self._captureGrab is not None
		if not consumed and not isUp and self._grabMatches(keycode, effective):
			consumed = True
			self._activeGrab = keycode
		if ticket is None and not consumed and not isUp and self._captureMatches(keycode, effective):
			ticket = _Ticket(keycode, arrival)
			self._tickets.append(ticket)
		try:
			# The keyboard's own events are never the echo of a key sent here, even when the same key was
			# sent just now; a release taken for one would leave NVDA thinking it still down.
			echo = sourceid in self._xtestDevices and self._isEcho(keycode, isUp, arrival)
			if echo or levelShift:
				if ticket is not None:
					# NVDA sent it to the application; it goes there at once.
					ticket.verdict = True
				return
			if ticket is not None and text and len(text) == 1 and text.isprintable() and level >= 2:
				# AltGr is not a modifier to NVDA, so the key would be taken for its letter alone, and
				# run that letter's quick navigation; as the character it types, it is trapped.
				vk, scan, extended = keyMap.VK["PACKET"], ord(text), False
				self._pressedAs[keycode] = (vk, scan)
			elif isUp and keycode in self._pressedAs:
				vk, scan = self._pressedAs.pop(keycode)
			event = KeyEvent(
				vkCode=vk,
				scanCode=scan,
				extended=extended,
				injected=sourceid in self._xtestDevices,
				isUp=isUp,
				repeat=repeat,
				reachedApplication=not consumed and ticket is None,
				text=text,
				typing=bool(text) and text.isprintable() and not effective & COMMAND_MODS,
				time=arrival,
				captured=ticket is not None,
				capsLockWasOn=capsLockWasOn,
			)
			taken = consumed and not isUp and not self._held and vk not in NEVER_SENT_ON
			self._decisions.put((event, ticket, keycode, effective, taken))
		finally:
			if isUp and keycode == self._activeGrab:
				self._activeGrab = None
			if isUp and keycode == self._captureGrab:
				self._captureGrab = None

	def _answer(self, now: float) -> float | None:
		"""Answer the press the keyboard is held on once NVDA has, or once it is overdue.

		:return: When to look again, or None when no press waits.
		"""
		while self._tickets:
			while (
				self._tickets
				and not self._tickets[0].frozen
				and now - self._tickets[0].arrival >= CAPTURE_EXPIRY
			):
				expired = self._tickets.popleft()
				expired.answered = True
				expired.settled.set()
			held = next((ticket for ticket in self._tickets if ticket.frozen), None)
			if held is None:
				return self._tickets[0].arrival + CAPTURE_EXPIRY if self._tickets else None
			if held.verdict is None and now < held.arrival + CAPTURE_TIMEOUT:
				return held.arrival + CAPTURE_TIMEOUT
			# Presses before the held one were never held, as the server holds them in order.
			while (ticket := self._tickets.popleft()) is not held:
				ticket.answered = True
				ticket.settled.set()
			held.answered = True
			if held.verdict is False:
				self._display.allow_events(X.AsyncKeyboard, X.CurrentTime)
				self._captureGrab = held.keycode
			else:
				self._display.allow_events(X.ReplayKeyboard, X.CurrentTime)
				if held.verdict is None:
					log.debug(f"Key {held.keycode} replayed without NVDA's answer")
			self._display.flush()
		return None

	def _beat(self, now: float) -> float | None:
		"""Tell the guard the source is alive, and whether the keyboard can be held.

		:return: When the next heartbeat is due, or None while none is.
		"""
		if self._guardInput is None:
			return None
		guarding = self._captureMode != "off" or self._held
		window = self._captureWindow.id if self._captureWindow is not None else 0
		beat = f"{int(guarding)} {self._guardResource} {window}\n"
		lastTime, lastBeat = self._lastBeat
		if beat != lastBeat or (guarding and now - lastTime >= HEARTBEAT_INTERVAL):
			try:
				os.write(self._guardInput, beat.encode())
			except BlockingIOError:
				pass
			except OSError:
				log.warning(
					"The guard that frees the keyboard should NVDA stop answering has gone", exc_info=True
				)
				self._guardInput = None
				return None
			self._lastBeat = lastTime, lastBeat = (now, beat)
		return lastTime + HEARTBEAT_INTERVAL if guarding else None

	def _startGuard(self) -> None:
		"""Start a guard for the connection there is now; it holds that connection's socket, so one is
		started afresh for each connection."""
		socket = self._display.fileno()
		try:
			self._guard = subprocess.Popen(
				[
					sys.executable,
					"-I",
					"-c",
					GUARD_PROGRAM,
					str(socket),
					str(self._display.screen().root.id),
					json.dumps([path for path in sys.path if path]),
					str(GUARD_SILENCE),
				],
				stdin=subprocess.PIPE,
				stdout=subprocess.DEVNULL,
				pass_fds=(socket,),
			)
		except OSError:
			log.warning("No guard frees the keyboard should NVDA stop answering", exc_info=True)
			self._guard = None
			return
		self._guardInput = self._guard.stdin.fileno()
		os.set_blocking(self._guardInput, False)

	def _stopGuard(self) -> None:
		guard, self._guard = self._guard, None
		self._guardInput = None
		if guard is None:
			return
		try:
			guard.stdin.close()
		except OSError:
			pass
		try:
			guard.wait(1)
		except subprocess.TimeoutExpired:
			guard.kill()
			guard.wait()

	def _grabMatches(self, keycode: int, effective: int) -> bool:
		if keycode not in self._grabbed:
			return False
		modifiers = self._grabbed[keycode]
		if modifiers is None:
			return True
		return effective in modifiers

	def _captureMatches(self, keycode: int, effective: int) -> bool:
		masks = self._captured.get(keycode)
		return masks is not None and effective & 0xFF in masks

	def _applyGrabs(self, nvdaModifierKeys: int, numpadReview: bool) -> None:
		self._consumedWanted = (nvdaModifierKeys, numpadReview)
		self._modeSets = {}
		self._ungrabKeys()
		self._grabKeys(self._focusWindow() or self._display.screen().root)

	def _keysWanted(self) -> list[tuple[int, list[int] | None, str]]:
		"""(key code, the modifiers it is grabbed with or None for any, a name for the user) for each
		NVDA key and numpad review key asked for."""
		if self._consumedWanted is None:
			return []
		nvdaModifierKeys, numpadReview = self._consumedWanted
		numpadMods = sorted(
			{
				sum(combination)
				for count in range(len(NUMPAD_GRAB_MODS) + 1)
				for combination in itertools.combinations(NUMPAD_GRAB_MODS, count)
			},
		)
		wanted: list[tuple[int, list[int] | None, str]] = []
		if nvdaModifierKeys & EXTENDED_INSERT:
			wanted.append((INSERT_KEY + 8, None, "Insert key"))
		if nvdaModifierKeys & CAPS_LOCK:
			wanted.append((CAPS_LOCK_KEY + 8, None, "Caps Lock key"))
		if nvdaModifierKeys & NUMPAD_INSERT:
			wanted.append((NUMPAD_INSERT_KEY + 8, numpadMods, "numpad Insert key"))
		if numpadReview:
			wanted.extend((evdev + 8, numpadMods, "numpad review keys") for evdev in NUMPAD_REVIEW_KEYS)
		return wanted

	def _ungrabKeys(self) -> None:
		window, self._keysWindow = self._keysWindow, None
		if window is None:
			return
		# The window may have gone, and its grabs with it.
		gone = error.CatchError(error.BadWindow)
		for keycode, modifiers in self._grabbed.items():
			for mod in modifiers or [X.AnyModifier]:
				window.ungrab_key(keycode, mod, onerror=gone)
		if window.id != self._display.screen().root.id and (
			self._captureWindow is None or window.id != self._captureWindow.id
		):
			window.change_attributes(event_mask=X.NoEventMask, onerror=gone)
		self._grabbed = {}

	def _grabKeys(self, window) -> None:
		"""Grab the NVDA keys and the numpad review keys on window: the one with the focus, so that no
		application sees its window lose the focus while one is held, as one grabbed on the root
		would; the root when no window may be grabbed on."""
		self._keysWindow = window
		refused = []
		for keycode, modifiers, label in self._keysWanted():
			catcher = error.CatchError(error.BadAccess, error.BadWindow)
			for mod in modifiers or [X.AnyModifier]:
				window.grab_key(keycode, mod, False, X.GrabModeAsync, X.GrabModeAsync, onerror=catcher)
			self._display.sync()
			if catcher.get_error():
				if label not in refused:
					refused.append(label)
				for mod in modifiers or [X.AnyModifier]:
					window.ungrab_key(keycode, mod, onerror=error.CatchError(error.BadWindow))
			else:
				self._grabbed[keycode] = modifiers
		for label in refused:
			log.warning(
				f"NVDA could not take the {label}, which another program holds; "
				"is another screen reader running?",
			)

	def _followFocus(self) -> None:
		"""Move the NVDA keys' grabs to the window that has the focus now, if it is another."""
		if self._consumedWanted is None:
			return
		window = self._focusWindow() or self._display.screen().root
		if self._keysWindow is not None and window.id == self._keysWindow.id:
			return
		self._ungrabKeys()
		self._grabKeys(window)

	def _captureSet(self, mode: CaptureMode) -> dict[int, set[int]]:
		"""Key code: the modifier masks, without the locks, it is captured with in mode."""
		if mode == "off" or self._translator is None:
			return {}
		if mode in self._modeSets:
			return self._modeSets[mode]
		masks = {"": 0, "shift": X.ShiftMask, "control": X.ControlMask, "alt": X.Mod1Mask}
		levelThree = self._translator.levelThreeMask()
		if levelThree:
			masks["altgr"] = levelThree

		def combinations(names: tuple[str, ...]) -> set[int]:
			found = set()
			for name in names:
				parts = name.split("+") if name else [""]
				if all(part in masks for part in parts):
					found.add(sum(masks[part] for part in parts))
			return found

		wanted: dict[int, set[int]] = {}
		keys = dict(FOCUS_KEYS) if mode == "focus" else dict(BROWSE_KEYS)
		if mode == "browse":
			for evdev in (*keyMap.LAYOUT, SPACE_KEY):
				keys[evdev] = tuple(dict.fromkeys(keys.get(evdev, ()) + CHARACTER_COMBINATIONS))
		for evdev, names in keys.items():
			wanted.setdefault(evdev + 8, set()).update(combinations(names))
		if mode == "browse":
			for letter in BROWSE_CONTROL_LETTERS:
				keycode = self._translator.keycodeFor(ord(letter), False)
				if keycode is not None:
					wanted.setdefault(keycode, set()).add(X.ControlMask)
		for names, vk, extended in self._gestureKeys[mode]:
			keycode = self._translator.keycodeFor(vk, extended)
			if keycode is not None and names <= GESTURE_MASKS.keys():
				wanted.setdefault(keycode, set()).add(sum(GESTURE_MASKS[name] for name in names))
		for keycode in self._grabbed:
			# The NVDA keys' own grabs, on the same window, would be taken over.
			wanted.pop(keycode, None)
		self._modeSets[mode] = wanted
		return wanted

	def _grabSet(self) -> dict[int, set[int]]:
		"""What is grabbed while the focus is in a document in either mode: the keys of both, as which
		mode is in force is asked at the moment of each key, for the mode can change between a key and
		the next sooner than a grab could follow."""
		grabbed: dict[int, set[int]] = {}
		for mode in ("browse", "focus"):
			for keycode, masks in self._captureSet(mode).items():
				grabbed.setdefault(keycode, set()).update(masks)
		return grabbed

	def _capturedNow(self, keycode: int, modifiers: int) -> bool:
		"""Whether NVDA, in the mode it is in at this moment, takes this press."""
		mode = None
		if self._modeNow is not None:
			try:
				mode = self._modeNow()
			# Whatever NVDA's focus raises, the key stays NVDA's to decide, as it was captured for.
			except Exception:  # noqa: BLE001
				log.debugWarning("Asking NVDA's mode for a captured key", exc_info=True)
		if mode is None:
			return True
		masks = self._captureSet(mode).get(keycode)
		return masks is not None and modifiers & 0xFF & ~(X.LockMask | NUM_LOCK_MASK) in masks

	def _setGestures(self, browse: frozenset[GestureKey], focus: frozenset[GestureKey]) -> None:
		wanted = {"browse": browse, "focus": focus}
		if wanted == self._gestureKeys:
			return
		self._gestureKeys = wanted
		self._modeSets = {}
		if self._captureMode != "off":
			self._applyCapture(self._captureMode)

	def _setCapture(self, mode: CaptureMode) -> None:
		# Asked again and again while the focus is in a document: the grabs follow the focus, which
		# may have gone to another window since they were made. Both modes' keys are grabbed in either,
		# so a change between them changes no grab.
		if (mode == "off") != (self._captureMode == "off") or (mode != "off" and self._captureWindow is None):
			self._applyCapture(mode)
		else:
			self._captureMode = mode
			self._logCapture(mode if self._captureWindow is not None else "off")

	def _dropCapture(self) -> None:
		"""Ungrab the captured keys, leaving the mode as it is."""
		window, self._captureWindow = self._captureWindow, None
		if window is not None:
			# The window may have gone, and its grabs with it.
			gone = error.CatchError(error.BadWindow)
			for keycode, masks in self._captured.items():
				for mask in masks:
					window.ungrab_key(keycode, mask, onerror=gone)
			if self._keysWindow is None or window.id != self._keysWindow.id:
				window.change_attributes(event_mask=X.NoEventMask, onerror=gone)
		self._captured = {}

	def _applyCapture(self, mode: CaptureMode) -> None:
		self._captureMode = mode
		d = self._display
		self._dropCapture()
		wanted = self._grabSet() if mode != "off" else {}
		window = self._focusWindow() if wanted else None
		if window is None:
			d.sync()
			self._logCapture("off")
			return
		catchers: list[tuple[int, int, error.CatchError]] = []
		for keycode, bases in wanted.items():
			for base in bases:
				for lock in LOCK_COMBINATIONS:
					catcher = error.CatchError(error.BadAccess, error.BadWindow)
					window.grab_key(
						keycode, base | lock, False, X.GrabModeAsync, X.GrabModeSync, onerror=catcher
					)
					catchers.append((keycode, base | lock, catcher))
		self._captureWindow = window
		# The window has the focus, which the NVDA keys follow too, the more surely for a second look.
		self._followFocus()
		d.sync()
		refused = 0
		for keycode, mask, catcher in catchers:
			if catcher.get_error():
				refused += 1
			else:
				self._captured.setdefault(keycode, set()).add(mask)
		if refused:
			log.debugWarning(f"{refused} key combinations for {mode} mode are held by another program")
		self._logCapture(mode)

	def _logCapture(self, mode: CaptureMode) -> None:
		# Grabs are made again on every change of window, and tried again and again while there is
		# no window to make them on; only a change of what is captured is worth a record.
		if mode != self._loggedCapture:
			self._loggedCapture = mode
			log.debug(f"Keys captured for {mode} mode")

	def _focusWindow(self):
		"""The window to grab captured keys and the NVDA keys on, watched for the focus leaving it; None
		when there is none to grab them on.

		A grab on the window that has the keyboard focus moves no focus as it activates; one on any
		other window, the root included, takes the focus from the application for as long as the key
		is held, and Firefox then reports its window inactive and active again for every key. A window
		of NVDA's own is never grabbed on: it has the focus only until NVDA's focus follows, and a grab
		there while it opens left wxGTK's modal dialogs crashing as they closed.
		"""
		d = self._display
		root = d.screen().root
		focus = d.get_input_focus().focus
		if isinstance(focus, int) or focus.id == root.id:
			return None
		gone = error.CatchError(error.BadWindow)
		window = focus
		try:
			for _level in range(4):
				pid = window.get_full_property(self._pidAtom, X.AnyPropertyType)
				if pid is not None and len(pid.value) and pid.value[0] == os.getpid():
					return None
				if pid is not None:
					break
				parent = window.query_tree().parent
				if isinstance(parent, int) or parent.id == root.id:
					break
				window = parent
		except error.BadWindow:
			return None
		focus.change_attributes(event_mask=X.FocusChangeMask | X.StructureNotifyMask, onerror=gone)
		d.sync()
		return None if gone.get_error() else focus

	def _hold(self, hold: bool) -> None:
		self._holdWanted = hold
		d = self._display
		if hold:
			status = d.screen().root.grab_keyboard(False, X.GrabModeAsync, X.GrabModeAsync, X.CurrentTime)
			self._held = status == X.GrabSuccess
			if not self._held:
				log.warning(
					"Another program holds the keyboard, so keys still reach applications during input help"
				)
		else:
			d.ungrab_keyboard(X.CurrentTime)
			d.sync()
			self._held = False
			self._activeGrab = None
			self._captureGrab = None

	def _fake(self, keycode: int, isUp: bool) -> None:
		d = self._display
		if (self._activeGrab is not None or self._captureGrab is not None) and not self._held:
			# A key sent while a grab of this source is active would come back to that grab, not reach
			# the application.
			d.ungrab_keyboard(X.CurrentTime)
			self._activeGrab = None
			self._captureGrab = None
		xtest.fake_input(d, X.KeyRelease if isUp else X.KeyPress, keycode)
		d.flush()

	def _xkbState(self):
		"""The X server's modifier state now, or None when it cannot say."""
		if self._xkbOpcode is None:
			return None
		return _XkbGetState(
			display=self._display.display, opcode=self._xkbOpcode, deviceSpec=XKB_CORE_KEYBOARD
		)

	def _setCapsLock(self, on: bool) -> None:
		if self._xkbOpcode is None:
			log.debugWarning("The X server has no XKB extension to set Caps Lock with")
			return
		_XkbLatchLockState(
			display=self._display.display,
			opcode=self._xkbOpcode,
			deviceSpec=XKB_CORE_KEYBOARD,
			affectModLocks=X.LockMask,
			modLocks=X.LockMask if on else 0,
			lockGroup=False,
			groupLock=0,
			affectModLatches=0,
			modLatches=0,
			latchGroup=False,
			groupLatch=0,
		)
		self._display.flush()

	def _sendOn(self, keycode: int, modifiers: int) -> None:
		"""Give the application a press that a grab of this source took and NVDA passed, with the
		modifiers it was pressed with, which are held for it again if they have been let go since.

		The X server ignores a press of a key it has down already, so a key still held goes up first,
		to the grab while one is active. So does the key whose press made the grab active: Caps Lock
		held down would have the key typed in capitals, as the X server adds its modifier while it is
		held, and the grab ends with it, as nothing sent while it lasts would reach the application. A
		key grabbed for NVDA is let go of while it is sent, or its own grab would take it again.
		"""
		if self._held:
			return
		d = self._display
		# Asked of the server, as a release may be on the way here; a release sent for a key already up
		# would be expected back, and the real one taken for it.
		down = d.query_keymap()
		activating = self._activeGrab if self._activeGrab != keycode else None
		releases = [
			code for code in (keycode, activating) if code is not None and down[code // 8] & (1 << (code % 8))
		]
		if activating not in releases:
			activating = None
		state = self._xkbState()
		missing = modifiers & ~SENT_ON_WITHOUT & ~(state.mods if state is not None else 0)
		mapping = d.get_modifier_mapping() if missing else []
		held = [
			next((code for code in mapping[index] if code), None)
			for index in range(8)
			if missing & (1 << index)
		]
		held = [code for code in held if code is not None]
		with self._lock:
			now = time.monotonic()
			self._echoes.extend((now, code, True) for code in releases)
			self._echoes.extend((now, code, False) for code in held)
			self._echoes.extend(((now, keycode, False), (now, keycode, True)))
			self._echoes.extend((now, code, True) for code in held)
		for code in releases:
			xtest.fake_input(d, X.KeyRelease, code)
		if state is not None and activating is not None:
			# A lock key let go of unlocks what was locked as it went down.
			self._setCapsLock(bool(state.lockedMods & X.LockMask))
		if self._activeGrab is not None or self._captureGrab is not None:
			d.ungrab_keyboard(X.CurrentTime)
			self._activeGrab = None
			self._captureGrab = None
		window = self._keysWindow
		lifted: list[int] = []
		if keycode in self._grabbed and window is not None:
			lifted = self._grabbed[keycode] or [X.AnyModifier]
		gone = error.CatchError(error.BadWindow)
		for mod in lifted:
			window.ungrab_key(keycode, mod, onerror=gone)
		for code in held:
			xtest.fake_input(d, X.KeyPress, code)
		xtest.fake_input(d, X.KeyPress, keycode)
		xtest.fake_input(d, X.KeyRelease, keycode)
		for code in reversed(held):
			xtest.fake_input(d, X.KeyRelease, code)
		if activating in NVDA_KEYCODES:
			# The NVDA key is still held: pressed again, its grab has the next key with it NVDA's again,
			# as on Windows, where the NVDA key goes on being held through a key the hook passed.
			nvdaKey = activating
			with self._lock:
				self._echoes.append((time.monotonic(), nvdaKey, False))
			xtest.fake_input(d, X.KeyPress, nvdaKey)
			self._rearmed = nvdaKey
			if state is not None:
				self._setCapsLock(bool(state.lockedMods & X.LockMask))
		if lifted:
			catcher = error.CatchError(error.BadAccess, error.BadWindow)
			for mod in lifted:
				window.grab_key(keycode, mod, False, X.GrabModeAsync, X.GrabModeAsync, onerror=catcher)
			d.sync()
			if catcher.get_error():
				log.warning("NVDA could not take back a key it sent on, which another program now holds")
				del self._grabbed[keycode]
		d.flush()
