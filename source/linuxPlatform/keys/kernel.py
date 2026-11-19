"""Keys from the kernel, through linda-keyd, the helper that grabs the real keyboards for Linda.

The helper reads every key before anything else on the machine sees it and asks this source about
each press: kept, the key reaches NVDA alone, as one the Windows keyboard hook blocks; passed, it goes
on through the helper's virtual keyboard to the X server and the applications. Unlike the X route,
nothing here grabs on a window, so a key NVDA keeps never reaches a program that holds the keyboard
itself, as an open menu or a password prompt does.

A key comes as a kernel key code, so the layout and the modifiers are this source's own account: the
keymap the X server was given, from the root window as the X source reads it, the group from the X
server's XKB state, the locks from the lights the helper reports, and the modifiers from the keys
themselves, since a key NVDA keeps never reaches the X server to change its state.

The helper passes a press on by itself when no verdict comes within its bound, so the thread that
reads the helper's socket answers by itself a little before that: a key of browse mode is passed, a
key under the NVDA key kept, as on the X route. That thread also sends the heartbeat, so that the
helper lets go should it hang. It must answer in time however busy NVDA's other threads keep the
interpreter, and every call into a C library gives the interpreter up and waits to have it back, so
it works in plain Python on tables made ahead: the layout is followed, and those tables made, on a
thread of its own, and each key is turned into NVDA's terms by the decider, the thread that hands
NVDA every key in order, as the X source's does.
"""

import os
import queue
import select
import socket
import struct
import sys
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field, replace

from logHandler import log
from xkbcommon import xkb
from Xlib import X, display, error
from Xlib.ext import xtest
from Xlib.protocol import rq

from . import CaptureMode, GestureKey, KeyEvent, keydClient, keyMap, x11
from .keydClient import KEY_PRESS, KEY_RELEASE, KEY_REPEAT, LOCK_CAPS, LOCK_NUM, LOCK_SCROLL

#: What becomes of a press: NVDA's to decide and kept without an answer, as an NVDA key's; NVDA's to
#: decide and passed without one, as browse mode's; kept at once, while the keyboard is held for input
#: help; or passed at once, as every other key is, NVDA having it after the application.
NVDA_KIND, CAPTURE_KIND, HOLD_KIND, PASS_KIND = x11.NVDA_GRAB, x11.CAPTURE_GRAB, "hold", "pass"
#: The share of the helper's bound after which this source answers by itself a press NVDA has not
#: answered, by what the press is. NVDA answers in under a millisecond as a rule, and in a few dozen
#: while its other threads keep the interpreter busy, as this thread then waits for it each time it
#: reads or sends. An NVDA key is kept early, as one passed on by the helper would reach the
#: application; browse mode's key, which the helper would pass on as this source would, waits nearly
#: to the end, so that NVDA has the longest to take it, while a late answer still counts as one.
ANSWER_SHARES = {x11.NVDA_GRAB: 0.4, x11.CAPTURE_GRAB: 0.7}
#: Seconds the interpreter lets a busy thread run before it hands over to one that waits, while keys
#: come from the helper: five by default. The thread that answers keys waits for the interpreter each
#: time it reads or sends, and with NVDA's main thread busy in Python it waited five each time, which
#: made a press NVDA had not answered reach the helper some sixty milliseconds after it was read, and
#: now and then past the bound; at one, some forty-five. A busy thread loses a little to the handovers.
SWITCH_INTERVAL = 0.001
#: Seconds after the last echo of a heartbeat, beyond the helper's own silence bound, after which the
#: helper is taken for hung.
ECHO_SLACK = 1.0
#: Seconds the layout's thread waits for the X server before it looks whether to stop.
LAYOUT_TICK = 0.5
#: Key code: its lock's light.
LOCK_KEYCODES = {
	x11.CAPS_LOCK_KEY + 8: LOCK_CAPS,
	69 + 8: LOCK_NUM,
	70 + 8: LOCK_SCROLL,
}
#: Virtual-key code of each lock: its light.
LOCK_LIGHTS = {
	keyMap.VK["CAPITAL"]: LOCK_CAPS,
	keyMap.VK["NUMLOCK"]: LOCK_NUM,
	keyMap.VK["SCROLL"]: LOCK_SCROLL,
}
#: Modifiers kept whatever NVDA says when it decides them, which change nothing but the state the next
#: key comes in, so that a chord goes on being NVDA's, as the X source keeps them. The locks are not
#: among them here: kept, a lock would not change, where on X the server changed it as it went down.
KEPT_MODIFIERS = x11.MODIFIER_VKS
NVDA_KEYCODES = {
	x11.INSERT_KEY + 8: x11.EXTENDED_INSERT,
	x11.CAPS_LOCK_KEY + 8: x11.CAPS_LOCK,
	x11.NUMPAD_INSERT_KEY + 8: x11.NUMPAD_INSERT,
}
NUMPAD_REVIEW_KEYCODES = frozenset(evdev + 8 for evdev in x11.NUMPAD_REVIEW_KEYS)
NUMPAD_REVIEW_MODS = sum(x11.NUMPAD_GRAB_MODS)
NUMPAD_INSERT_MODS = sum(x11.NUMPAD_INSERT_GRAB_MODS)
#: Modifiers that have nothing to do with which keys browse mode captures.
LOCK_MODS = X.LockMask | x11.NUM_LOCK_MASK
#: XKB's StateNotify, the one XKB event this source asks for.
XKB_STATE_NOTIFY = 2
#: XKB's ControlsNotify, which says the repeat changed among other things.
XKB_CONTROLS_NOTIFY = 3
#: XkbRepeatKeysMask: whether keys repeat at all.
XKB_REPEAT_KEYS = 1
#: How many windows' processes are remembered, for asking whose window has the focus.
WINDOW_PIDS_KEPT = 256

_DEPRESSED = xkb.StateComponent.XKB_STATE_MODS_DEPRESSED


class _XkbSelectEvents(rq.Request):
	"""Ask the X server for XKB's events, here every detail of the kinds named."""

	_request = rq.Struct(
		rq.Card8("opcode"),
		rq.Opcode(1),
		rq.RequestLength(),
		rq.Card16("deviceSpec"),
		rq.Card16("affectWhich"),
		rq.Card16("clear"),
		rq.Card16("selectAll"),
		rq.Card16("affectMap"),
		rq.Card16("map"),
	)


class _XkbGetState(rq.ReplyRequest):
	"""The keyboard's modifiers and group as the X server has them."""

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
		rq.Card8("group"),
		rq.Card8("lockedGroup"),
		rq.Int16("baseGroup"),
		rq.Int16("latchedGroup"),
		rq.Pad(14),
	)


class _XkbGetControls(rq.ReplyRequest):
	"""XKB's controls, of which this source wants the repeat: whether keys repeat, after how long, how
	often, and which keys."""

	_request = rq.Struct(
		rq.Card8("opcode"),
		rq.Opcode(6),
		rq.RequestLength(),
		rq.Card16("deviceSpec"),
		rq.Pad(2),
	)
	_reply = rq.Struct(
		rq.ReplyCode(),
		rq.Card8("deviceID"),
		rq.Card16("sequence_number"),
		rq.ReplyLength(),
		rq.Pad(12),
		rq.Card16("repeatDelay"),
		rq.Card16("repeatInterval"),
		rq.Pad(32),
		rq.Card32("enabledCtrls"),
		rq.FixedBinary("perKeyRepeat", 32),
	)


@dataclass(frozen=True)
class _Repeat:
	"""How the X server repeats a key held down, which a key NVDA keeps must do as well."""

	enabled: bool = True
	#: Seconds before the first repeat, and between repeats.
	delay: float = 0.66
	interval: float = 0.04
	#: A bit for each key code, set for each key that repeats.
	perKey: bytes = b"\xff" * 32

	def repeats(self, keycode: int) -> bool:
		return self.enabled and bool(self.perKey[keycode >> 3] & 1 << (keycode & 7))


#: StateNotify after its first four bytes, which python-xlib gives as the data of an event it does not
#: know: time, device, the four modifier sets, the group, then the base, latched and locked groups.
_STATE_NOTIFY = struct.Struct("=IBBBBBBhhB")


@dataclass(frozen=True)
class _Tables:
	"""What the thread that answers keys needs of the layout, made ahead, in a group of a keymap."""

	keymap: xkb.Keymap
	names: tuple[str, ...]
	#: Base, latched and locked group.
	group: tuple[int, int, int]
	#: Key code: the modifiers it holds down, as X masks them.
	keyMods: dict[int, int]
	#: Key codes of the keys that pick a level rather than being Alt: AltGr.
	levelShift: frozenset[int]


def _tables(keymap: xkb.Keymap, names: tuple[str, ...], group: tuple[int, int, int]) -> _Tables:
	keyMods: dict[int, int] = {}
	levelShift = set()
	for keycode in range(keymap.min_keycode(), keymap.max_keycode() + 1):
		if keycode in LOCK_KEYCODES:
			continue
		state = keymap.state_new()
		state.update_mask(0, 0, 0, *group)
		try:
			layout = state.key_get_layout(keycode)
		except xkb.XKBError:
			continue
		syms = keymap.key_get_syms_by_level(keycode, layout, 0)
		if syms and syms[0] in keyMap.LEVEL_SHIFT_KEYSYMS:
			levelShift.add(keycode)
		# What a modifier key does is the group's: right Alt is AltGr in a Polish group, Alt in a US one.
		state.update_key(keycode, xkb.KeyDirection.XKB_KEY_DOWN)
		mods = state.serialize_mods(_DEPRESSED) & 0xFF
		if mods:
			keyMods[keycode] = mods
	return _Tables(keymap, names, group, keyMods, frozenset(levelShift))


def _lockedMods(locks: int) -> int:
	return (X.LockMask if locks & LOCK_CAPS else 0) | (x11.NUM_LOCK_MASK if locks & LOCK_NUM else 0)


@dataclass(eq=False)
class _Press:
	"""A key press, and what became of it."""

	seq: int
	keycode: int
	#: time.monotonic() when it came.
	arrival: float
	kind: str
	#: The kind it would be without the NVDA key held with it.
	own: str
	effective: int
	#: Typed through typeKey, as Remote Access types: sent through XTEST, not the helper.
	typed: bool = False
	#: The presses of NVDA keys it was taken for NVDA's under, which were not yet decided.
	under: tuple["_Press", ...] = ()
	#: Its virtual-key code, once the decider has worked it out.
	vk: int | None = None
	#: True once it is passed to the application, False once kept; None until then.
	passed: bool | None = None
	#: Whether the helper has the verdict, or for a typed key, whether it was sent.
	settled: bool = False
	#: Whether the helper passed it on before its verdict came.
	expired: bool = False
	#: Whether it was decided by this source's own thread, NVDA not having answered in time.
	defaulted: bool = False
	#: Whether the helper dropped it undecided, as it does the keys of the emergency chord.
	dropped: bool = False
	#: A typed key released before its press was decided.
	releasedEarly: bool = False
	lock: threading.Lock = field(default_factory=threading.Lock)

	def decide(self, passed: bool) -> bool:
		"""Decide it, unless it is decided already; True when this decided it."""
		with self.lock:
			if self.passed is not None:
				return False
			self.passed = passed
			return True


@dataclass(frozen=True)
class _Item:
	"""A key event for the decider, with the state it came in: the decider puts it in NVDA's terms."""

	keycode: int
	value: int
	press: _Press | None
	tables: _Tables
	depressed: int
	locked: int
	time: float
	typed: bool = False
	#: AltGr while the keyboard is held, which Windows' hook reports with a left Control first.
	altGr: bool = False


class KernelKeySource:
	"""A KeySource taking keys from linda-keyd over a connection keydClient.connect made."""

	def __init__(
		self,
		connection: socket.socket,
		welcome: keydClient.Welcome,
		onLost: Callable[[str, bool], None] | None = None,
		onHeld: Callable[[], None] | None = None,
	) -> None:
		"""
		:param onLost: Called once, on the source's own thread, when the helper lets go of the keyboard
			for good or the connection ends, with why and whether it was the emergency chord. Every key
			NVDA was told is down has been released to it first.
		:param onHeld: Called on the source's own thread when the helper first says it holds a keyboard,
			before any key of that keyboard is read.
		"""
		self._sock = connection
		self._welcome = welcome
		self._onLost = onLost
		self._onHeld = onHeld
		self._answerWithin = {kind: welcome.verdictTimeout * share for kind, share in ANSWER_SHARES.items()}
		self._wakeRead, self._wakeWrite = os.pipe()
		self._commands: deque[Callable[[], None]] = deque()
		self._stopping = False
		self._lost = False
		self._thread: threading.Thread | None = None
		self._layoutThread: threading.Thread | None = None
		self._decider: threading.Thread | None = None
		self._decisions: queue.SimpleQueue[_Item | None] = queue.SimpleQueue()
		self._deliver: Callable[[KeyEvent], bool] | None = None
		self._modeNow: Callable[[], CaptureMode | None] | None = None
		#: Replaced whole as the layout changes, never changed in place, so that the thread that
		#: answers keys reads them without a lock.
		self._tables: _Tables | None = None
		#: keydClient's LOCK_ bits of the locks that are on.
		self._locks = 0
		#: The translator other threads ask about the layout, under _queryLock, and the decider's own.
		self._query: keyMap.Translator | None = None
		self._queryLock = threading.Lock()
		self._deciderTranslator: keyMap.Translator | None = None
		#: The X connection whose events the layout's thread reads, and the one other threads ask
		#: things of and send keys through, under _xLock.
		self._events: display.Display | None = None
		self._requests: display.Display | None = None
		self._xLock = threading.Lock()
		self._xkbOpcode: int | None = None
		self._xkbEvent: int | None = None
		self._rulesAtom: int | None = None
		self._pidAtom: int | None = None
		self._windowPids: dict[int, int | None] = {}
		#: Key codes down, the locks left out: what holds the modifiers down.
		self._down: set[int] = set()
		#: Key code: the press of each key down, from the keyboards and from typeKey.
		self._presses: dict[int, _Press] = {}
		self._typed: dict[int, _Press] = {}
		#: Sequence number: the press, while it is down.
		self._bySeq: dict[int, _Press] = {}
		#: Presses whose verdict the helper has not had, in the order they came.
		self._pending: deque[_Press] = deque()
		#: Key code: the press of each NVDA key down that was not passed on, which makes every press
		#: NVDA's while it lasts.
		self._nvdaHeld: dict[int, _Press] = {}
		#: Key code: (vk, scan) a press was delivered as when it was not the key's own, for its release.
		self._pressedAs: dict[int, tuple[int, int]] = {}
		# What NVDA asked, each replaced whole, as the thread that answers keys reads them without a lock.
		self._consumed = (0, False)
		self._held = False
		self._captureMode: CaptureMode = "off"
		self._capturePid: int | None = None
		self._gestureKeys: dict[str, frozenset[GestureKey]] = {"browse": frozenset(), "focus": frozenset()}
		#: Mode: key code: the modifier masks it is captured with; both modes or none.
		self._modeSets: dict[str, dict[int, set[int]]] = {}
		#: Whether Num Lock was on before typeKey changed it for a numpad key; None while it has not.
		self._numLockBefore: bool | None = None
		self._beatCounter = 0
		self._nextBeat = 0.0
		self._lastEcho = 0.0
		self._holding = False
		#: The X server's repeat, replaced whole as it changes.
		self._repeatSettings = _Repeat()
		#: The press of the key that repeats now, and when it repeats next: the last key pressed that
		#: repeats at all, as the X server has it, until it is released.
		self._repeating: _Press | None = None
		self._nextRepeat: float | None = None
		#: The interpreter's switch interval before this source changed it, while it has.
		self._switchBefore: float | None = None

	def start(
		self,
		deliver: Callable[[KeyEvent], bool],
		modeNow: Callable[[], CaptureMode | None] | None = None,
	) -> None:
		"""Read the layout from the X server, then ask the helper to capture.

		:raise RuntimeError: When there is no X server to read the layout from, which the first release
			needs; the caller then takes keys another way.
		"""
		self._deliver = deliver
		self._modeNow = modeNow
		if not os.environ.get("DISPLAY"):
			raise RuntimeError("there is no X display to read the keyboard layout from")
		self._connectX()
		now = time.monotonic()
		self._lastEcho = now
		self._nextBeat = now
		self._decider = threading.Thread(target=self._decide, name="KernelKeySource decider", daemon=True)
		self._decider.start()
		self._layoutThread = threading.Thread(
			target=self._followLayout, name="KernelKeySource layout", daemon=True
		)
		self._layoutThread.start()
		self._thread = threading.Thread(target=self._run, name="KernelKeySource", daemon=True)
		self._thread.start()
		if sys.getswitchinterval() > SWITCH_INTERVAL:
			self._switchBefore = sys.getswitchinterval()
			sys.setswitchinterval(SWITCH_INTERVAL)
		self._post(lambda: self._send(keydClient.start()))

	def stop(self) -> None:
		"""Let go of the helper, which lets go of the keyboards, and tell NVDA every key it holds is up."""
		self._stopping = True
		self._wake()
		for thread in (self._thread, self._layoutThread):
			if thread is not None:
				thread.join(2)
		self._thread = self._layoutThread = None
		self._decisions.put(None)
		if self._decider is not None:
			self._decider.join(2)
			self._decider = None
		for connection in (self._events, self._requests):
			if connection is not None:
				try:
					connection.close()
				except (OSError, error.ConnectionClosedError):
					pass
		self._events = self._requests = None
		try:
			# Never started, its thread did not close it; the helper lets go as it closes.
			self._sock.close()
		except OSError:
			pass
		os.close(self._wakeRead)
		os.close(self._wakeWrite)
		if self._switchBefore is not None and sys.getswitchinterval() == SWITCH_INTERVAL:
			sys.setswitchinterval(self._switchBefore)

	def setCallbacks(
		self, onLost: Callable[[str, bool], None] | None, onHeld: Callable[[], None] | None
	) -> None:
		"""As the constructor's, for a caller that needs the source to make them."""
		self._onLost, self._onHeld = onLost, onHeld

	@property
	def isHolding(self) -> bool:
		"""Whether the helper holds a keyboard for this source."""
		return self._holding

	def setConsumed(self, nvdaModifierKeys: int, numpadReview: bool) -> None:
		self._consumed = (nvdaModifierKeys, numpadReview)

	def setCapture(self, mode: CaptureMode, processID: int | None = None) -> None:
		if mode != "off" and not self._modeSets:
			self._makeModeSets()
		changed = mode != self._captureMode
		self._capturePid = processID if mode != "off" else None
		self._captureMode = mode
		if changed:
			log.debug(f"Keys captured for {mode} mode")

	def setCaptureGestures(self, browse: frozenset[GestureKey], focus: frozenset[GestureKey]) -> None:
		wanted = {"browse": browse, "focus": focus}
		if wanted != self._gestureKeys:
			self._gestureKeys = wanted
			self._makeModeSets()

	def holdKeyboard(self, hold: bool) -> None:
		self._held = hold

	def setCapsLock(self, on: bool) -> None:
		# The X route puts back the lock the X server changed as a key NVDA kept went down. Here a kept
		# key never reaches the X server, and the lock never changes.
		pass

	def inject(self, vkCode: int, scanCode: int, extended: bool, isUp: bool) -> None:
		keycode = self._keycodeFor(vkCode, extended)
		if keycode is None:
			log.debugWarning(f"No key on this keyboard sends virtual key {vkCode:#x}")
			return
		# Straight to the X server: the helper never sees it, so it never comes back as a key.
		self._xtest(keycode, isUp)

	def typeKey(self, vkCode: int, extended: bool, isUp: bool) -> None:
		keycode = self._keycodeFor(vkCode, extended)
		if keycode is None:
			log.debugWarning(f"No key on this keyboard sends virtual key {vkCode:#x}")
			return
		self._post(lambda: self._typeKey(keycode, vkCode, extended, isUp))

	def releaseTypedKeys(self) -> None:
		self._post(self._releaseTypedKeys)

	def keyState(self, vkCode: int) -> int:
		state = 0
		# Copies made in one step each, as the thread that answers keys changes these as it goes.
		for press in (*tuple(self._presses.values()), *tuple(self._typed.values())):
			# Down as the applications have it: a key NVDA kept never reached them, as on Windows a key
			# the hook blocks is up to everyone else.
			if press.passed and press.vk is not None and vkCode in (press.vk, x11.GENERIC_FOR.get(press.vk)):
				state = 0x8000
				break
		light = LOCK_LIGHTS.get(vkCode)
		if light is not None and self._locks & light:
			state |= 1
		return state

	def charForVk(self, vkCode: int) -> int:
		with self._queryLock:
			return self._query.mapVirtualKeyToChar(vkCode) if self._query else 0

	def vkForChar(self, ch: str) -> tuple[int, int]:
		with self._queryLock:
			if self._query is None:
				raise LookupError(ch)
			return self._query.vkKeyScan(ch)

	def keyName(self, scanCode: int, extended: bool) -> str:
		with self._queryLock:
			return self._query.keyName(scanCode, extended) if self._query else ""

	def _keycodeFor(self, vkCode: int, extended: bool) -> int | None:
		with self._queryLock:
			return self._query.keycodeFor(vkCode, extended) if self._query else None

	def _post(self, command: Callable[[], None]) -> None:
		self._commands.append(command)
		self._wake()

	def _wake(self) -> None:
		try:
			os.write(self._wakeWrite, b"\0")
		except OSError:
			pass

	# The layout, from the X server, on the layout's own thread.

	def _connectX(self) -> None:
		self._events = display.Display()
		try:
			d = self._events
			info = d.query_extension(x11.XKB_EXTENSION)
			if info is None:
				raise RuntimeError("the X server has no XKB extension to follow the layout with")
			opcode = info.major_opcode
			if not x11._XkbUseExtension(
				display=d.display, opcode=opcode, wantedMajor=1, wantedMinor=0
			).supported:
				raise RuntimeError("the X server's XKB is not one NVDA knows")
			self._xkbOpcode, self._xkbEvent = opcode, info.first_event
			wanted = 1 << XKB_STATE_NOTIFY | 1 << XKB_CONTROLS_NOTIFY
			_XkbSelectEvents(
				display=d.display,
				opcode=opcode,
				deviceSpec=x11.XKB_CORE_KEYBOARD,
				affectWhich=wanted,
				clear=0,
				selectAll=wanted,
				affectMap=0,
				map=0,
			)
			root = d.screen().root
			root.change_attributes(event_mask=X.PropertyChangeMask)
			self._rulesAtom = d.intern_atom(x11.RULES_NAMES_PROPERTY)
			self._pidAtom = d.intern_atom("_NET_WM_PID")
			state = _XkbGetState(display=d.display, opcode=opcode, deviceSpec=x11.XKB_CORE_KEYBOARD)
			leds = d.get_keyboard_control().led_mask
			# Until the helper says which lights are on: the X server's own locks, Scroll Lock's light
			# being its third.
			self._locks = (
				(LOCK_CAPS if state.lockedMods & X.LockMask else 0)
				| (LOCK_NUM if state.lockedMods & x11.NUM_LOCK_MASK else 0)
				| (LOCK_SCROLL if leds & 4 else 0)
			)
			self._loadKeymap((state.baseGroup, state.latchedGroup, state.lockedGroup))
			self._readRepeat()
			d.sync()
			self._requests = display.Display()
		except Exception:
			self._events.close()
			self._events = None
			raise

	def _readRulesNames(self) -> tuple[str, ...]:
		prop = self._events.screen().root.get_full_property(self._rulesAtom, X.AnyPropertyType)
		if prop is None or not prop.value:
			return x11.DEFAULT_RULES_NAMES
		value = prop.value.decode("utf-8", "replace") if isinstance(prop.value, bytes) else str(prop.value)
		return tuple((value.split("\0") + [""] * 5)[:5])

	def _loadKeymap(self, group: tuple[int, int, int] | None = None) -> None:
		names = self._readRulesNames()
		tables = self._tables
		if tables is not None and names == tables.names:
			return
		group = group if group is not None else tables.group
		rules, model, layout, variant, options = names
		keymap = xkb.Context().keymap_new_from_names(
			rules=rules or None,
			model=model or None,
			layout=layout or None,
			variant=variant or None,
			options=options or None,
		)
		self._useTables(_tables(keymap, names, group))
		log.debug(f"Keyboard layout {layout or 'us'} {variant}".rstrip())

	def _useTables(self, tables: _Tables) -> None:
		with self._queryLock:
			if self._query is None or self._query.keymap is not tables.keymap:
				self._query = keyMap.Translator(tables.keymap, tables.keymap.state_new())
			self._query.state.update_mask(0, 0, 0, *tables.group)
			self._tables = tables
		self._makeModeSets()

	def _makeModeSets(self) -> None:
		"""Work out ahead which keys each mode captures, in the layout and group there are now."""
		with self._queryLock:
			if self._query is None:
				return
			self._modeSets = {
				mode: x11.captureSet(self._query, mode, self._gestureKeys[mode])
				for mode in ("browse", "focus")
			}

	def _followLayout(self) -> None:
		d = self._events
		try:
			while not self._stopping:
				select.select([d.fileno()], [], [], LAYOUT_TICK)
				while d.pending_events():
					self._onXEvent(d.next_event())
		except (error.ConnectionClosedError, OSError):
			if not self._stopping:
				log.warning("The X server closed the connection the keyboard layout is read through")
		except Exception:
			log.exception("Following the keyboard layout")

	def _onXEvent(self, event) -> None:
		if event.type == self._xkbEvent and getattr(event, "detail", None) == XKB_STATE_NOTIFY:
			_time, _device, _mods, _base, _latched, _locked, _group, baseGroup, latchedGroup, lockedGroup = (
				_STATE_NOTIFY.unpack_from(event.data)
			)
			group = (baseGroup, latchedGroup, lockedGroup)
			tables = self._tables
			if tables is not None and group != tables.group:
				self._useTables(_tables(tables.keymap, tables.names, group))
		elif event.type == self._xkbEvent and getattr(event, "detail", None) == XKB_CONTROLS_NOTIFY:
			self._readRepeat()
		elif event.type == X.MappingNotify:
			self._events.refresh_keyboard_mapping(event)
			self._loadKeymap()
		elif event.type == X.PropertyNotify and event.atom == self._rulesAtom:
			self._loadKeymap()

	def _readRepeat(self) -> None:
		controls = _XkbGetControls(
			display=self._events.display, opcode=self._xkbOpcode, deviceSpec=x11.XKB_CORE_KEYBOARD
		)
		self._repeatSettings = _Repeat(
			enabled=bool(controls.enabledCtrls & XKB_REPEAT_KEYS),
			delay=max(controls.repeatDelay, 1) / 1000,
			interval=max(controls.repeatInterval, 1) / 1000,
			perKey=bytes(controls.perKeyRepeat),
		)

	def _focusTakesCapture(self) -> bool:
		"""Whether the window with the keyboard focus is one a document's keys are captured in: of the
		application whose document set the mode, when that is known, and never one of NVDA's own.

		The X route grabs the keys on that window alone, so that a key typed in another application
		before NVDA's focus has followed it there reaches that application.
		"""
		with self._xLock:
			d = self._requests
			if d is None:
				return False
			try:
				focus = d.get_input_focus().focus
				if isinstance(focus, int) or focus.id == d.screen().root.id:
					return False
				pid = self._windowPids.get(focus.id, -1)
				if pid == -1:
					pid = self._windowPid(focus)
					if len(self._windowPids) >= WINDOW_PIDS_KEPT:
						self._windowPids.clear()
					self._windowPids[focus.id] = pid
			except (error.XError, error.ConnectionClosedError, OSError):
				return True
		if pid == os.getpid():
			return False
		wanted = self._capturePid
		return not (wanted and pid and pid != wanted)

	def _windowPid(self, window) -> int | None:
		"""The process a window belongs to, by its own _NET_WM_PID or its nearest parent's."""
		root = self._requests.screen().root
		try:
			for _level in range(4):
				pid = window.get_full_property(self._pidAtom, X.AnyPropertyType)
				if pid is not None and len(pid.value):
					return int(pid.value[0])
				parent = window.query_tree().parent
				if isinstance(parent, int) or parent.id == root.id:
					return None
				window = parent
		except error.BadWindow:
			return None
		return None

	def _xtest(self, keycode: int, isUp: bool) -> None:
		with self._xLock:
			if self._requests is None:
				return
			try:
				xtest.fake_input(self._requests, X.KeyRelease if isUp else X.KeyPress, keycode)
				self._requests.flush()
			except (error.ConnectionClosedError, OSError):
				log.debugWarning("Sending a key to the X display", exc_info=True)

	def _setNumLock(self, on: bool) -> None:
		with self._xLock:
			if self._requests is None:
				return
			x11._XkbLatchLockState(
				display=self._requests.display,
				opcode=self._xkbOpcode,
				deviceSpec=x11.XKB_CORE_KEYBOARD,
				affectModLocks=x11.NUM_LOCK_MASK,
				modLocks=x11.NUM_LOCK_MASK if on else 0,
				lockGroup=False,
				groupLock=0,
				affectModLatches=0,
				modLatches=0,
				latchGroup=False,
				groupLatch=0,
			)
			self._requests.flush()
		self._locks = (self._locks & ~LOCK_NUM) | (LOCK_NUM if on else 0)

	# The helper's connection, on the source's own thread.

	def _send(self, message: bytes) -> None:
		if self._lost:
			return
		try:
			self._sock.send(message)
		except BlockingIOError:
			# The helper is not reading what this sends; it drops a client that does not read its own.
			log.debugWarning("linda-keyd is not reading what NVDA sends")
		except OSError as e:
			self._loseHelper(f"its connection failed: {e.strerror or e}")

	def _run(self) -> None:
		try:
			self._serve()
		except Exception:
			log.exception("Keys stopped being read from linda-keyd")
			self._loseHelper("NVDA's side of the connection failed")
		finally:
			if not self._lost:
				# Stopped by NVDA: the helper lets go as the connection closes.
				self._releaseTypedKeys()
				self._releaseAll()
				self._lost = True
			try:
				self._sock.close()
			except OSError:
				pass

	def _serve(self) -> None:
		while not self._stopping and not self._lost:
			while self._commands:
				try:
					self._commands.popleft()()
				except Exception:
					log.exception("Acting on a request to the kernel key source")
			now = time.monotonic()
			deadlines = [self._answer(now), self._beat(now), self._repeatNow(now)]
			if self._lost:
				return
			due = min((t for t in deadlines if t is not None), default=None)
			readable, _w, _x = select.select(
				[self._sock, self._wakeRead],
				[],
				[],
				None if due is None else max(0.0, due - time.monotonic()),
			)
			if self._wakeRead in readable:
				os.read(self._wakeRead, 4096)
			if self._sock in readable:
				self._readHelper()

	def _readHelper(self) -> None:
		while not self._lost:
			try:
				data = self._sock.recv(keydClient.MAX_MESSAGE)
			except BlockingIOError:
				return
			except OSError as e:
				self._loseHelper(f"its connection failed: {e.strerror or e}")
				return
			if not data:
				self._loseHelper("it closed the connection")
				return
			try:
				message = keydClient.decode(data)
			except keydClient.Malformed as e:
				self._loseHelper(f"it sent a message the protocol does not allow: {e}")
				return
			try:
				self._onMessage(message, time.monotonic())
			except Exception:
				log.exception("Handling a message from linda-keyd")

	def _onMessage(self, message: keydClient.Message | None, now: float) -> None:
		match message:
			case keydClient.Key():
				self._onKey(message, now)
			case keydClient.HeartbeatEcho():
				self._lastEcho = now
			case keydClient.Expired(seq=seq):
				self._onExpired(seq)
			case keydClient.Locks(leds=leds):
				self._locks = leds & (LOCK_CAPS | LOCK_NUM | LOCK_SCROLL)
			case keydClient.Device():
				status = keydClient.DEVICE_NAMES.get(message.status, f"in state {message.status}")
				log.info(f"linda-keyd: keyboard {message.device} {message.name!r} {status}")
				if message.status == keydClient.DEVICE_HELD and not self._holding:
					self._holding = True
					log.info("Keys are taken from the kernel through linda-keyd")
					if self._onHeld is not None:
						try:
							self._onHeld()
						except Exception:
							log.exception("Reporting that linda-keyd holds the keyboard")
			case keydClient.Released(reason=reason):
				self._onReleased(reason)
			case keydClient.Goodbye():
				self._loseHelper(f"it said goodbye: {message.describe()}")
			case _:
				# A message of a later version or feature, which this client did not ask for.
				pass

	def _onReleased(self, reason: int) -> None:
		why = keydClient.RELEASED_NAMES.get(reason, f"reason {reason}")
		self._holding = False
		emergency = reason == keydClient.RELEASED_EMERGENCY
		self._releaseAll(dropUndecided=emergency)
		if reason == keydClient.RELEASED_BACKGROUND:
			# It captures again by itself once this session is in front again, before which no key of
			# this session's comes anywhere.
			log.info(f"linda-keyd let go of the keyboard, as {why}")
			return
		if reason == keydClient.RELEASED_STOPPED and self._stopping:
			return
		self._loseHelper(f"it let go of the keyboard, as {why}", emergency=emergency)

	def _loseHelper(self, why: str, emergency: bool = False) -> None:
		"""Give the helper up: tell NVDA every key it holds is up, close the connection, and say so."""
		if self._lost:
			return
		self._releaseTypedKeys()
		self._releaseAll()
		self._lost = True
		self._holding = False
		try:
			self._sock.close()
		except OSError:
			pass
		if self._stopping:
			return
		try:
			if self._onLost is not None:
				self._onLost(why, emergency)
		except Exception:
			log.exception("Reporting that linda-keyd was lost")

	def _beat(self, now: float) -> float | None:
		"""Send the heartbeat when it is due, and notice a helper that stopped echoing it.

		:return: When the next one is due.
		"""
		if now - self._lastEcho > self._welcome.heartbeatTimeout + ECHO_SLACK:
			self._loseHelper("it stopped answering")
			return None
		if now >= self._nextBeat:
			self._beatCounter += 1
			self._send(keydClient.heartbeat(self._beatCounter))
			self._nextBeat = now + self._welcome.heartbeatInterval / 2
		return self._nextBeat

	def _answer(self, now: float) -> float | None:
		"""Send the verdicts NVDA has given, and answer by itself a press NVDA has not answered in time.

		:return: When to look again, or None while no press waits.
		"""
		due = None
		for press in list(self._pending):
			if press.passed is None:
				limit = press.arrival + self._answerWithin.get(press.kind, self._answerWithin[NVDA_KIND])
				if now < limit:
					due = limit if due is None else min(due, limit)
					continue
				# A slow NVDA costs a key of browse mode a delay and never the key, and a chord with the NVDA
				# key stays NVDA's, as on the X route.
				press.defaulted = press.decide(press.kind == CAPTURE_KIND)
			self._pending.remove(press)
			self._settle(press)
		return due

	def _settle(self, press: _Press) -> None:
		"""Act on a press's verdict: tell the helper, or send a typed key, and follow what it changes."""
		if press.settled:
			return
		press.settled = True
		if press.typed:
			if press.passed:
				self._xtest(press.keycode, False)
				if press.releasedEarly:
					self._xtest(press.keycode, True)
		elif not press.expired:
			self._send(keydClient.verdict(press.seq, keep=not press.passed))
		if press.passed:
			self._passed(press)

	def _passed(self, press: _Press) -> None:
		if self._nvdaHeld.get(press.keycode) is press:
			# The NVDA key pressed twice to pass it on: no chord comes of it.
			del self._nvdaHeld[press.keycode]
		light = LOCK_KEYCODES.get(press.keycode)
		if light is not None and not press.typed:
			# The X server locks or unlocks as the key reaches it, and the light the helper reports
			# follows a moment later; a key typed in between has it already.
			self._locks ^= light

	def _onExpired(self, seq: int) -> None:
		press = self._bySeq.get(seq)
		if press is None:
			return
		press.expired = True
		if press.decide(True):
			if press in self._pending:
				self._pending.remove(press)
			self._settle(press)
		elif not press.passed:
			# Kept here, too late: the application has it all the same.
			with press.lock:
				press.passed = True
			self._passed(press)

	# Keys, in plain Python on the tables.

	def _ownKind(self, keycode: int, effective: int) -> str:
		"""What a press would be by itself, with no NVDA key held."""
		nvdaModifierKeys, numpadReview = self._consumed
		flag = NVDA_KEYCODES.get(keycode)
		if (
			flag is not None
			and nvdaModifierKeys & flag
			# Numpad Insert is 0 with Num Lock on.
			and (flag != x11.NUMPAD_INSERT or not effective & ~NUMPAD_INSERT_MODS)
		):
			return NVDA_KIND
		if numpadReview and keycode in NUMPAD_REVIEW_KEYCODES and not effective & ~NUMPAD_REVIEW_MODS:
			return NVDA_KIND
		if self._captureMode != "off":
			modeSets = self._modeSets
			if not modeSets:
				self._makeModeSets()
				modeSets = self._modeSets
			for masks in (modeSets.get("browse", {}).get(keycode), modeSets.get("focus", {}).get(keycode)):
				if masks is not None and effective & ~LOCK_MODS in masks:
					return CAPTURE_KIND
		return PASS_KIND

	def _depressed(self, tables: _Tables) -> int:
		mods = 0
		for keycode in self._down:
			mods |= tables.keyMods.get(keycode, 0)
		return mods

	def _onKey(self, key: keydClient.Key, now: float) -> None:
		keycode = key.code + 8
		if self._tables is None:
			if key.value == KEY_PRESS and key.flags & keydClient.KEY_ASK:
				self._send(keydClient.verdict(key.seq, keep=False))
			return
		if key.value == KEY_PRESS:
			# Every press asks; one that does not was decided without NVDA, and NVDA has it after the
			# application, as any key it does not take.
			self._press(keycode, now, key.seq, typed=False, decided=not key.flags & keydClient.KEY_ASK)
		elif key.value == KEY_RELEASE:
			self._release(keycode, now, typed=False)
		# The kernel's own repeats are ignored: they follow the kernel's rate, a quarter of a second to
		# the first, where the X server, which repeats a key passed on, follows the desktop's setting.

	def _press(self, keycode: int, now: float, seq: int, typed: bool, decided: bool = False) -> None:
		tables = self._tables
		held = self._held
		depressed = self._depressed(tables)
		locked = _lockedMods(self._locks)
		effective = depressed | locked
		levelShift = keycode in tables.levelShift
		if keycode not in LOCK_KEYCODES:
			self._down.add(keycode)
		own = self._ownKind(keycode, effective)
		under: tuple[_Press, ...] = ()
		if held:
			kind = HOLD_KIND
		elif own == NVDA_KIND:
			kind = NVDA_KIND
		elif self._nvdaHeld:
			kind = NVDA_KIND
			under = tuple(self._nvdaHeld.values())
		else:
			kind = own
		press = _Press(seq, keycode, now, kind, own, effective, typed=typed, under=under)
		if own == NVDA_KIND and keycode in NVDA_KEYCODES and kind in (NVDA_KIND, HOLD_KIND):
			self._nvdaHeld[keycode] = press
		presses = self._typed if typed else self._presses
		old = presses.get(keycode)
		if old is not None:
			self._bySeq.pop(old.seq, None)
		presses[keycode] = press
		if not typed:
			self._bySeq[seq] = press
			if self._repeatSettings.repeats(keycode):
				self._repeating = press
				self._nextRepeat = now + self._repeatSettings.delay
		if decided:
			press.decide(True)
			press.settled = True
			self._passed(press)
		else:
			if kind == PASS_KIND:
				press.decide(True)
			elif kind == HOLD_KIND or levelShift:
				# AltGr changes only the level the next key comes in, and is kept with the other modifiers
				# when NVDA has the key.
				press.decide(False)
			if press.passed is None:
				self._pending.append(press)
			else:
				self._settle(press)
		if levelShift and not held:
			return
		self._decisions.put(
			_Item(keycode, KEY_PRESS, press, tables, depressed, locked, now, typed=typed, altGr=levelShift)
		)

	def _release(self, keycode: int, now: float, typed: bool) -> None:
		tables = self._tables
		held = self._held
		depressed = self._depressed(tables)
		locked = _lockedMods(self._locks)
		self._down.discard(keycode)
		presses = self._typed if typed else self._presses
		# None for a key down before the helper held the keyboard, whose press the application had.
		press = presses.pop(keycode, None)
		if press is not None:
			self._bySeq.pop(press.seq, None)
			if self._nvdaHeld.get(keycode) is press:
				del self._nvdaHeld[keycode]
			if typed:
				if press.settled:
					if press.passed:
						self._xtest(keycode, True)
				else:
					press.releasedEarly = True
		levelShift = keycode in tables.levelShift
		if levelShift and not held:
			return
		self._decisions.put(
			_Item(keycode, KEY_RELEASE, press, tables, depressed, locked, now, typed=typed, altGr=levelShift)
		)

	def _repeatNow(self, now: float) -> float | None:
		"""Repeat the key held down that repeats, when it is due, as the X server repeats it: to NVDA alone,
		whether the key reached the application, where the X server repeats it, or not.

		:return: When it repeats next, or None while no key repeats.
		"""
		press = self._repeating
		if press is None:
			return None
		if self._presses.get(press.keycode) is not press:
			self._repeating = self._nextRepeat = None
			return None
		if now >= self._nextRepeat:
			self._repeat(press.keycode, now)
			interval = self._repeatSettings.interval
			# Behind by more than a repeat, as after a pause of the interpreter: the X server does not
			# make up the repeats it missed either.
			self._nextRepeat = max(self._nextRepeat + interval, now + interval / 2)
		return self._nextRepeat

	def _repeat(self, keycode: int, now: float) -> None:
		tables = self._tables
		if keycode in tables.levelShift and not self._held:
			return
		press = self._presses.get(keycode)
		self._decisions.put(
			_Item(keycode, KEY_REPEAT, press, tables, self._depressed(tables), _lockedMods(self._locks), now)
		)

	def _releaseAll(self, dropUndecided: bool = False) -> None:
		"""Release to NVDA every key it was told is down, as the helper has let go of them all.

		:param dropUndecided: Whether the helper dropped the presses still undecided, as it does after
			the emergency chord, rather than passed them on, as it does whenever else it lets go.
		"""
		now = time.monotonic()
		for press in list(self._pending):
			if press.typed:
				continue
			self._pending.remove(press)
			press.dropped = dropUndecided
			press.decide(not dropUndecided)
			press.settled = True
		if self._tables is not None:
			for keycode in sorted(self._presses):
				self._release(keycode, now, typed=False)
		self._presses.clear()
		self._down.clear()
		self._bySeq.clear()
		self._nvdaHeld.clear()
		self._repeating = self._nextRepeat = None

	def _typeKey(self, keycode: int, vkCode: int, extended: bool, isUp: bool) -> None:
		"""Press or release a key as a keyboard would, so that NVDA has it as it has any key, and the
		application has it unless NVDA takes it."""
		if self._tables is None:
			return
		now = time.monotonic()
		if isUp:
			if keycode not in self._typed:
				return
			self._release(keycode, now, typed=True)
			if self._numLockBefore is not None and not any(code - 8 in keyMap.NUMPAD for code in self._typed):
				self._setNumLock(self._numLockBefore)
				self._numLockBefore = None
			return
		numpad = keyMap.NUMPAD.get(keycode - 8)
		if numpad is not None and not extended and vkCode in numpad[1:] and self._xkbOpcode is not None:
			# The key has one code with Num Lock on and another with it off, and the X server picks by the
			# lock; it is put back once no such key is held.
			wanted = vkCode == numpad[1]
			numLockOn = bool(self._locks & LOCK_NUM)
			if self._numLockBefore is None and numLockOn != wanted:
				self._numLockBefore = numLockOn
			if self._numLockBefore is not None:
				self._setNumLock(wanted)
		self._press(keycode, now, 0, typed=True)

	def _releaseTypedKeys(self) -> None:
		now = time.monotonic()
		if self._tables is not None:
			for keycode in list(self._typed):
				self._release(keycode, now, typed=True)
		if self._numLockBefore is not None:
			self._setNumLock(self._numLockBefore)
			self._numLockBefore = None

	# The decider: each key in NVDA's terms, and NVDA's answer.

	def _decide(self) -> None:
		while True:
			item = self._decisions.get()
			if item is None:
				return
			try:
				self._decideKey(item)
			# Every key after this one reaches NVDA only through this thread, which must go on whatever
			# one key raised. A press left unanswered is answered by the source's thread in time.
			except Exception:  # noqa: BLE001
				x11._logError("Deciding a key")

	def _event(self, item: _Item) -> KeyEvent:
		"""The key in NVDA's terms, in the state it came in."""
		translator = self._deciderTranslator
		if translator is None or translator.keymap is not item.tables.keymap:
			translator = self._deciderTranslator = keyMap.Translator(
				item.tables.keymap, item.tables.keymap.state_new()
			)
		state = translator.state
		state.update_mask(item.depressed, 0, item.locked, *item.tables.group)
		keycode = item.keycode
		vk, scan, extended = translator.translate(keycode)
		isUp = item.value == KEY_RELEASE
		text = "" if isUp else state.key_get_string(keycode)
		press = item.press
		taken = press is not None and press.kind in (NVDA_KIND, CAPTURE_KIND)
		if item.value == KEY_PRESS:
			if press is not None:
				press.vk = vk
			if taken and text and len(text) == 1 and text.isprintable() and translator.level(keycode) >= 2:
				# AltGr is not a modifier to NVDA, so the key would be taken for its letter alone, and run
				# that letter's quick navigation; as the character it types, it is trapped.
				vk, scan, extended = keyMap.VK["PACKET"], ord(text), False
				self._pressedAs[keycode] = (vk, scan)
		elif keycode in self._pressedAs:
			vk, scan = self._pressedAs.pop(keycode) if isUp else self._pressedAs[keycode]
			extended = False
		effective = item.depressed | item.locked
		return KeyEvent(
			vkCode=vk,
			scanCode=scan,
			extended=extended,
			injected=item.typed,
			isUp=isUp,
			repeat=item.value == KEY_REPEAT,
			reachedApplication=True,
			text=text,
			typing=bool(text) and text.isprintable() and not effective & x11.COMMAND_MODS,
			time=item.time,
			capsLockWasOn=bool(item.locked & X.LockMask),
		)

	def _decideKey(self, item: _Item) -> None:
		event = self._event(item)
		press = item.press
		if item.altGr:
			# Held for input help or for another computer, AltGr is what Windows' hook reports for it: a
			# left Control of its own scan code, then right Alt, so that it types on a Windows computer
			# that the keys are sent to.
			control = replace(
				event, vkCode=keyMap.VK["LCONTROL"], scanCode=x11.ALTGR_CONTROL_SCAN, extended=False, text=""
			)
			self._call(replace(control, reachedApplication=False))
		if item.value != KEY_PRESS:
			if press is not None:
				# Its press came before it, and is decided by now.
				reached = bool(press.passed) or press.expired
				event = replace(
					event,
					reachedApplication=reached,
					nvdaKeyGrab=item.value == KEY_REPEAT and press.kind == NVDA_KIND and not reached,
				)
			self._call(event)
			return
		if press is None or press.dropped:
			return
		kind = press.kind
		if kind == NVDA_KIND and press.under and all(p.passed for p in press.under):
			# Taken for a chord with an NVDA key, which NVDA passed on: no chord.
			kind = press.kind = press.own
			if kind == PASS_KIND and press.decide(True):
				self._wake()
		event = replace(
			event,
			reachedApplication=kind == PASS_KIND or bool(press.passed),
			captured=kind in (NVDA_KIND, CAPTURE_KIND),
			nvdaKeyGrab=kind == NVDA_KIND,
		)
		if kind not in (NVDA_KIND, CAPTURE_KIND):
			self._call(event)
			return
		if press.passed is not None:
			if press.defaulted:
				log.debug(
					f"Key {press.keycode} answered without NVDA: {'passed' if press.passed else 'kept'}"
				)
			if press.passed:
				# The application has had the key, as NVDA did not answer in time; NVDA acting on it as
				# well would act twice.
				return
			# Kept for NVDA without its answer, as the NVDA key's chords are: NVDA's still.
			self._call(event)
			return
		kept = event.vkCode in KEPT_MODIFIERS
		if kind == CAPTURE_KIND and not kept and not self._capturedNow(press):
			if press.decide(True):
				self._wake()
			self._call(replace(event, captured=False, reachedApplication=True))
			return
		passed = self._call(event)
		if press.decide(passed is not False and not kept):
			self._wake()

	def _call(self, event: KeyEvent) -> bool:
		try:
			return self._deliver(event)
		except Exception:  # noqa: BLE001
			x11._logError("Delivering a key")
			return True

	def _capturedNow(self, press: _Press) -> bool:
		"""Whether NVDA, in the mode it is in at this moment, takes this press of a key a mode captures."""
		mode: CaptureMode | None = None
		if self._modeNow is not None:
			try:
				mode = self._modeNow()
			# Whatever NVDA's focus raises, the key stays NVDA's to decide, as it was captured for.
			except Exception:  # noqa: BLE001
				log.debugWarning("Asking NVDA's mode for a captured key", exc_info=True)
		if mode is not None:
			if mode == "off":
				return False
			masks = self._modeSets.get(mode, {}).get(press.keycode)
			if masks is None or press.effective & ~LOCK_MODS not in masks:
				return False
		return self._focusTakesCapture()
