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

Where a document's keys go is the X source's account: its grabs take browse mode's keys on the
window of the document while that window has the focus, and a key of this source goes where those
grabs would send it, by the snapshot of them the X source keeps.

The helper passes a press on by itself when no verdict comes within its bound, so the thread that
reads the helper's socket answers by itself a little before that: a key of browse mode is passed, a
key under the NVDA key kept, as on the X route. A helper that grants WAIT holds a press longer when
asked, and NVDA then has as long as the X route gives it. That thread also sends the heartbeat, so
that the helper lets go should it hang. It must answer in time however busy NVDA's other threads keep the
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
#: answered, by what the press is, with a helper that cannot be asked to wait. NVDA answers in under a
#: millisecond as a rule, and in a few dozen while its other threads keep the interpreter busy, as
#: this thread then waits for it each time it reads or sends. An NVDA key is kept early, as one passed
#: on by the helper would reach the application; browse mode's key, which the helper would pass on as
#: this source would, waits nearly to the end, so that NVDA has the longest to take it, while a late
#: answer still counts as one. With a helper that can, the NVDA key's share is when it is asked to wait
#: for a press NVDA has not answered, early for the same reason.
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
#: Seconds the layout's thread waits for the X server before it looks whether to stop, and whether the
#: X source grabs a document's keys on another window, whose focus it then watches.
CAPTURE_LOOK = 0.1
#: Seconds past the time it asked to wake by after which the thread that answers keys takes it that
#: every thread of NVDA's was stopped, as every process is around the machine's sleep, while the
#: monotonic clock ran on, and linda-keyd, stopped as well, was not silent.
#: The helper's WATCHDOG_FROZEN, beyond any ordinary lateness of a thread that sleeps.
FROZEN = 0.5
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
#: Modifiers that reach the application while the NVDA key is held, so that the X server has them down
#: as the user does should they outlast the NVDA key; a chord with them stays NVDA's, as its key is.
#: Alt and Super are kept, as on the X route: alone, the application would take them for a press of
#: their own, which opens a menu.
MODIFIERS_PASSED_UNDER_NVDA = X.ShiftMask | X.ControlMask
MODIFIERS_KEPT_UNDER_NVDA = X.Mod1Mask | X.Mod4Mask
#: Seconds beyond the helper's bound for a press that a press released is remembered for, by its number:
#: the helper may say it passed the press on after the release has come.
EXPIRY_SLACK = 1.0

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
	#: Whether it came while the X source held a document's keys for NVDA's focus to come back to it.
	document: bool = False
	#: When the presses before it were all decided, from which NVDA has its time for it; None until.
	started: float | None = None
	#: Whether the helper was asked to hold it longer.
	waited: bool = False
	#: The keyboard it came from, by the helper's number; None for a typed key.
	device: int | None = None
	#: For a press of a key already held down on another keyboard, the press NVDA was told of, whose
	#: verdict this one shares: the helper's virtual keyboard holds the key down from the first press
	#: passed to the last release, so the application had only that press.
	follows: "_Press | None" = None
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
		xSource: x11.X11KeySource | None = None,
	) -> None:
		"""
		:param onLost: Called once, on the source's own thread, when the helper lets go of the keyboard
			for good or the connection ends, with why and whether it was the emergency chord. Every key
			NVDA was told is down has been released to it first.
		:param onHeld: Called on the source's own thread when the helper first says it holds a keyboard,
			before any key of that keyboard is read.
		:param xSource: The X source beside this one, whose grabs say where a document's keys go; without
			it, none are captured.
		"""
		self._sock = connection
		self._welcome = welcome
		self._onLost = onLost
		self._onHeld = onHeld
		self._x = xSource
		self._canWait = bool(welcome.features & keydClient.FEATURE_WAIT)
		self._answerWithin = {kind: welcome.verdictTimeout * share for kind, share in ANSWER_SHARES.items()}
		#: Seconds after which the helper passes on a press it was not answered about.
		self._bound = keydClient.VERDICT_WAIT_TIMEOUT if self._canWait else welcome.verdictTimeout
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
		#: The window the layout's thread watches the focus of, the X source's capture window, and
		#: (that window, whether it has the focus) as the last of its events said, replaced whole.
		self._watchedWindow = 0
		self._focusSeen: tuple[int, bool] = (0, False)
		#: Key codes down, the locks left out: what holds the modifiers down.
		self._down: set[int] = set()
		#: Key code: the press of each key down, from the keyboards and from typeKey; of a key held on
		#: more than one keyboard, the first, which NVDA was told of.
		self._presses: dict[int, _Press] = {}
		self._typed: dict[int, _Press] = {}
		#: Key code: keyboard: its press, for each key down on a keyboard. A key is down until the last
		#: keyboard that holds it lets it go, as the helper's virtual keyboard has it.
		self._holders: dict[int, dict[int, _Press]] = {}
		#: Sequence number: the press, while it is down.
		self._bySeq: dict[int, _Press] = {}
		#: Sequence number: the press, released while the helper may still pass it on; and when each
		#: is forgotten, in order.
		self._released: dict[int, _Press] = {}
		self._forget: deque[tuple[float, int]] = deque()
		#: Key code: the press of each Alt or Super kept under the NVDA key and still held, which goes
		#: down in the X display before a key passed on with it; and the key codes of those that have.
		self._keptModifiers: dict[int, _Press] = {}
		self._modifiersPutDown: set[int] = set()
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
		log.debug(f"linda-keyd {'holds' if self._canWait else 'cannot hold'} a press longer when asked")
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
		# Which keys are captured where is the X source's, which is asked the same.
		if mode != "off" and not self._modeSets:
			self._makeModeSets()

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
				self._watchCapture()
				select.select([d.fileno()], [], [], CAPTURE_LOOK)
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
		elif (
			event.type in (X.FocusIn, X.FocusOut)
			and event.window.id == self._watchedWindow
			# As the X source reads them: the focus moving within the window, or a grab's, moves none.
			and event.mode in (X.NotifyNormal, X.NotifyWhileGrabbed)
			and event.detail not in (X.NotifyInferior, X.NotifyPointer, X.NotifyPointerRoot)
		):
			self._focusSeen = (event.window.id, event.type == X.FocusIn)

	def _watchCapture(self) -> None:
		"""Watch the focus of the window the X source grabs a document's keys on, on this connection,
		whose thread does next to nothing else and so reads the X server's word sooner than the X
		source's: that one makes and moves grabs as the focus moves, waiting on the X server each time,
		and in the guests had the focus coming back to a document a tenth of a second after the X server
		did, while the first key typed there was already being decided."""
		window = self._captureState().window
		if window == self._watchedWindow:
			return
		d = self._events
		gone = error.CatchError(error.BadWindow)
		if self._watchedWindow:
			d.create_resource_object("window", self._watchedWindow).change_attributes(
				event_mask=X.NoEventMask, onerror=gone
			)
		self._watchedWindow = window
		self._focusSeen = (0, False)
		if window:
			watched = d.create_resource_object("window", window)
			watched.change_attributes(event_mask=X.FocusChangeMask, onerror=gone)
			# Asked once the window is watched, so that no change after it is missed.
			self._focusSeen = (window, self._hasFocus(watched))
		d.flush()

	def _hasFocus(self, window) -> bool:
		"""Whether the keyboard focus is on window or a window inside it."""
		d = self._events
		root = d.screen().root.id
		try:
			focus = d.get_input_focus().focus
			for _level in range(16):
				if isinstance(focus, int) or focus.id == root:
					return False
				if focus.id == window.id:
					return True
				focus = focus.query_tree().parent
		except error.BadWindow:
			return False
		return False

	def _captureNow(self, now: float) -> x11.CaptureState:
		"""The X source's capture state, with the focus as this source's own watch has it where the two
		differ, the watch being the sooner to read the X server's word: what the X source makes of the
		focus coming back to the window, once it reads it, is made of it here as the X source makes it."""
		state = self._captureState()
		window, focused = self._focusSeen
		if not state.window or window != state.window or focused == state.focused:
			return state
		if not focused:
			return state._replace(focused=False)
		if state.mode == "off" and state.dormant is not None:
			return state._replace(
				focused=True, mode=state.dormant, dormant=None, documentUntil=now + x11.DOCUMENT_GRACE
			)
		return state._replace(focused=True)

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

	def _captureState(self) -> x11.CaptureState:
		return self._x.captureState if self._x is not None else x11.NO_CAPTURE

	@staticmethod
	def _grabsTake(state: x11.CaptureState) -> bool:
		"""Whether the X source's grabs of a document's keys take a key typed now, by state.

		They are on the window that had the focus as NVDA's focus reached the document, and stay there
		while the focus is away, taking nothing, so that a key typed in another window, of the same
		application or another, before NVDA's focus has followed it there reaches that window. Beyond
		the X route, never while NVDA says the document is another process's than that window's, as
		when NVDA's focus reached a document as the focus left for another application.
		"""
		if not state.window or not state.focused:
			return False
		return not (state.processID and state.windowProcess and state.processID != state.windowProcess)

	def _putDownKeptModifiers(self) -> None:
		"""Put down in the X display, before a key passed on reaches it, each Alt or Super kept under the
		NVDA key and still held, so that the key comes with it as the user typed it, as on the X route,
		where a grab keeps a key from the application and not from the X server's state."""
		codes = [
			code
			for code, press in self._keptModifiers.items()
			if code not in self._modifiersPutDown and self._presses.get(code) is press
		]
		if codes and self._sendToX(codes, False):
			self._modifiersPutDown.update(codes)

	def _sendToX(self, keycodes: list[int], isUp: bool) -> bool:
		"""Send keys through XTEST as the X source's own, which it gives to no one, and wait for the X
		server to have them, ahead of any key the helper passes on after."""
		if self._x is None:
			return False
		with self._xLock:
			if self._requests is None:
				return False
			try:
				for keycode in keycodes:
					self._x.expectEcho(keycode, isUp)
					xtest.fake_input(self._requests, X.KeyRelease if isUp else X.KeyPress, keycode)
				self._requests.get_input_focus()
			except (error.ConnectionClosedError, OSError):
				log.debugWarning("Sending a modifier to the X display", exc_info=True)
				return False
		return True

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
			# The helper says why before it closes, and a send that finds it closed comes before that is
			# read: what it said is the reason, not the broken pipe.
			self._readHelper(atEnd=False)
			self._loseHelper(f"its connection failed: {e.strerror or e}")

	def _receive(self) -> bytes | None:
		"""The helper's next message, b"" once it has closed, or None while nothing more has come.

		:raise OSError: When the connection failed.
		"""
		try:
			return self._sock.recv(keydClient.MAX_MESSAGE)
		except BlockingIOError:
			return None
		except ConnectionResetError:
			# The kernel reports a peer that closed with something of ours unread once, ahead of what it
			# had sent, which is still there to be read.
			try:
				return self._sock.recv(keydClient.MAX_MESSAGE)
			except BlockingIOError:
				return b""

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
		woke = time.monotonic()
		while not self._stopping and not self._lost:
			while self._commands:
				try:
					self._commands.popleft()()
				except Exception:
					log.exception("Acting on a request to the kernel key source")
			now = time.monotonic()
			if now - woke > FROZEN:
				# Stopped between two waits, as the commands above take next to nothing.
				self._thawed(now - woke, now)
			deadlines = [self._answer(now), self._beat(now), self._repeatNow(now)]
			if self._lost:
				return
			due = min((t for t in deadlines if t is not None), default=None)
			began = time.monotonic()
			readable, _w, _x = select.select(
				[self._sock, self._wakeRead],
				[],
				[],
				None if due is None else max(0.0, due - began),
			)
			woke = time.monotonic()
			# There is always a heartbeat due, so a wait never runs open.
			if due is not None and woke - max(due, began) > FROZEN:
				self._thawed(woke - max(due, began), woke)
			if self._wakeRead in readable:
				os.read(self._wakeRead, 4096)
			if self._sock in readable:
				self._readHelper()

	def _readHelper(self, atEnd: bool = True) -> None:
		"""Act on every message the helper has sent.

		:param atEnd: Whether the connection ending, or failing, loses the helper here; without, the
			caller does that with a reason of its own once the helper's own is read.
		"""
		while not self._lost:
			try:
				data = self._receive()
			except OSError as e:
				if atEnd:
					self._loseHelper(f"its connection failed: {e.strerror or e}")
				return
			if data is None:
				return
			if not data:
				if atEnd:
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

	def _thawed(self, frozen: float, now: float) -> None:
		"""Count the helper's silence afresh from now, after this thread was stopped for frozen seconds:
		a helper that has really stopped answering still takes the whole bound from now to show. The next
		repeat of a key held down comes once, not as a burst of those missed.

		The presses NVDA has yet to answer keep their time: this thread cannot tell every process
		frozen, which the helper allows for, from itself alone kept from running, which the helper does
		not, and answered later than the helper's bound an NVDA key's chord would reach the application.
		"""
		log.debug(
			f"The thread that answers keys was stopped for {frozen * 1000:.0f} ms; linda-keyd is not held to it"
		)
		self._lastEcho = max(self._lastEcho, now)
		if self._nextRepeat is not None:
			self._nextRepeat += frozen

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
		first = True
		for press in list(self._pending):
			if self._lost:
				# A verdict that could not be sent lost the helper, and every press with it.
				return None
			if press.passed is None and press.follows is not None and press.follows.passed is not None:
				press.decide(press.follows.passed)
			if press.passed is None:
				if first and press.started is None:
					press.started = now
				first = False
				limit = self._answerBy(press)
				if now < limit:
					waitAt = self._waitAt(press)
					if waitAt is not None and not press.waited:
						if now >= waitAt:
							press.waited = True
							self._send(keydClient.wait(press.seq))
						else:
							limit = min(limit, waitAt)
					due = limit if due is None else min(due, limit)
					continue
				# A slow NVDA costs a key of browse mode a delay and never the key, and a chord with the NVDA
				# key stays NVDA's, as on the X route.
				press.defaulted = press.decide(press.kind == CAPTURE_KIND)
			self._pending.remove(press)
			self._settle(press)
		return due

	def _answerBy(self, press: _Press) -> float:
		"""When this source answers a press NVDA has not answered by itself.

		With a helper that can wait, as on the X route: NVDA has CAPTURE_TIMEOUT for a press from the
		moment every press before it is decided, as the X server holds the keyboard on one press at a
		time and NVDA learns of the next only then, and DOCUMENT_WAIT for one of a document whose window
		has the focus again before NVDA's focus is back in it; never beyond DOCUMENT_WAIT from its
		coming, which leaves the helper's longer bound a tenth of a second to have the answer. Without,
		a share of the helper's bound from its coming, by what the press is.
		"""
		if not self._canWait:
			return press.arrival + self._answerWithin.get(press.kind, self._answerWithin[NVDA_KIND])
		latest = press.arrival + x11.DOCUMENT_WAIT
		if press.started is None:
			return latest
		return min(latest, press.started + (x11.DOCUMENT_WAIT if press.document else x11.CAPTURE_TIMEOUT))

	def _waitAt(self, press: _Press) -> float | None:
		"""When to ask the helper to hold a press NVDA has not answered: at once for a document's key
		that waits for NVDA's focus, early for any other, as this thread may be late itself."""
		if not self._canWait:
			return None
		return press.arrival if press.document else press.arrival + self._answerWithin[NVDA_KIND]

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
			if press.passed and self._keptModifiers:
				self._putDownKeptModifiers()
			self._send(keydClient.verdict(press.seq, keep=not press.passed))
		if press.passed and press.follows is None:
			self._passed(press)
		elif (
			press.under
			and press.kind == NVDA_KIND
			and self._tables.keyMods.get(press.keycode, 0)
			and self._presses.get(press.keycode) is press
		):
			# A modifier kept under the NVDA key, which the X server has up until a key passed on needs it.
			self._keptModifiers[press.keycode] = press

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
		# By number only, as a key typed is kept out of the log; a count of these says how often NVDA
		# answered too late for the helper.
		log.debug(f"linda-keyd passed key {seq} on before NVDA answered it")
		# Released already, the helper read the release before the press's bound came.
		press = self._bySeq.get(seq) or self._released.get(seq)
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
			if press.follows is None:
				self._passed(press)
			if (
				self._keptModifiers.get(press.keycode) is press
				and press.keycode not in self._modifiersPutDown
			):
				del self._keptModifiers[press.keycode]

	# Keys, in plain Python on the tables.

	def _ownKind(self, keycode: int, effective: int, capture: x11.CaptureState) -> str:
		"""What a press would be by itself, with no NVDA key held, the X source's grabs being capture."""
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
		if self._grabsTake(capture):
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
			decided = not key.flags & keydClient.KEY_ASK
			holders = self._holders.setdefault(keycode, {})
			if holders and keycode in self._presses:
				holders[key.device] = self._follow(keycode, now, key.seq, key.device, decided)
			else:
				holders[key.device] = self._press(
					keycode, now, key.seq, typed=False, decided=decided, device=key.device
				)
		elif key.value == KEY_RELEASE:
			holders = self._holders.get(keycode)
			if holders is not None:
				press = holders.pop(key.device, None)
				if holders:
					# Still held on another keyboard, which the application has it held down by too.
					if press is not None:
						self._remember(press, now)
					return
				del self._holders[keycode]
				if press is not None and press is not self._presses.get(keycode):
					self._remember(press, now)
			self._release(keycode, now, typed=False)
		# The kernel's own repeats are ignored: they follow the kernel's rate, a quarter of a second to
		# the first, where the X server, which repeats a key passed on, follows the desktop's setting.

	def _press(
		self,
		keycode: int,
		now: float,
		seq: int,
		typed: bool,
		decided: bool = False,
		device: int | None = None,
	) -> _Press:
		tables = self._tables
		held = self._held
		depressed = self._depressed(tables)
		locked = _lockedMods(self._locks)
		effective = depressed | locked
		levelShift = keycode in tables.levelShift
		if keycode not in LOCK_KEYCODES:
			self._down.add(keycode)
		capture = self._captureNow(now)
		own = self._ownKind(keycode, effective, capture)
		under: tuple[_Press, ...] = ()
		mods = tables.keyMods.get(keycode, 0)
		if held:
			kind = HOLD_KIND
		elif own == NVDA_KIND:
			kind = NVDA_KIND
		elif self._nvdaHeld and (
			levelShift or mods & MODIFIERS_PASSED_UNDER_NVDA and not mods & MODIFIERS_KEPT_UNDER_NVDA
		):
			# The chord is NVDA's by its key; what this one changes the X server must see.
			kind = own
		elif self._nvdaHeld:
			kind = NVDA_KIND
			under = tuple(self._nvdaHeld.values())
		else:
			kind = own
		press = _Press(seq, keycode, now, kind, own, effective, typed=typed, under=under, device=device)
		if kind == CAPTURE_KIND and capture.documentUntil is not None and now < capture.documentUntil:
			press.document = True
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
		if not levelShift or held:
			self._decisions.put(
				_Item(
					keycode, KEY_PRESS, press, tables, depressed, locked, now, typed=typed, altGr=levelShift
				)
			)
		return press

	def _follow(self, keycode: int, now: float, seq: int, device: int, decided: bool) -> _Press:
		"""A press of a key already held down on another keyboard. It reaches neither NVDA nor the
		application, whose virtual keyboard holds the key down already or not at all, as NVDA decided
		the first press: the helper has its verdict from the first's."""
		first = self._presses[keycode]
		press = _Press(
			seq, keycode, now, first.kind, first.own, first.effective, device=device, follows=first
		)
		self._bySeq[seq] = press
		if decided:
			press.decide(True)
			press.settled = True
		elif first.passed is not None:
			press.decide(first.passed)
			self._settle(press)
		else:
			self._pending.append(press)
		return press

	def _release(self, keycode: int, now: float, typed: bool) -> None:
		tables = self._tables
		held = self._held
		depressed = self._depressed(tables)
		locked = _lockedMods(self._locks)
		if keycode not in (self._holders if typed else self._typed):
			self._down.discard(keycode)
		presses = self._typed if typed else self._presses
		# None for a key down before the helper held the keyboard, whose press the application had.
		press = presses.pop(keycode, None)
		if press is not None:
			self._retire(press, now)
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

	def _retire(self, press: _Press, now: float) -> None:
		"""Forget a press released, by its number only once the helper can no longer pass it on."""
		if self._keptModifiers.get(press.keycode) is press:
			del self._keptModifiers[press.keycode]
			if press.keycode in self._modifiersPutDown:
				self._modifiersPutDown.discard(press.keycode)
				self._sendToX([press.keycode], True)
		self._remember(press, now)

	def _remember(self, press: _Press, now: float) -> None:
		"""Keep a press released by its number for a late EXPIRED, until the helper's bound is past."""
		self._bySeq.pop(press.seq, None)
		while self._forget and self._forget[0][0] <= now:
			self._released.pop(self._forget.popleft()[1], None)
		if not press.typed:
			self._released[press.seq] = press
			self._forget.append((press.arrival + self._bound + EXPIRY_SLACK, press.seq))

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
		self._holders.clear()
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
				# By number, as which key it was is the user's typing.
				log.debug(
					f"linda-keyd's key {press.seq} answered without NVDA: {'passed' if press.passed else 'kept'}"
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

	def _askMode(self) -> CaptureMode | None:
		if self._modeNow is None:
			return None
		try:
			return self._modeNow()
		# Whatever NVDA's focus raises, the key stays NVDA's to decide, as it was captured for.
		except Exception:  # noqa: BLE001
			log.debugWarning("Asking NVDA's mode for a captured key", exc_info=True)
			return None

	def _capturedNow(self, press: _Press) -> bool:
		"""Whether NVDA, in the mode it is in at this moment, takes this press of a key a mode captures,
		as the X source's decider has it: a key typed in a document's window before NVDA's focus is back
		in it waits for that focus as long as the X route lets it, half a second, less the time to
		answer; then, whether the grabs would still take it, the focus having had time to move."""
		waitUntil = self._answerBy(press) - x11.CAPTURE_TIMEOUT / 2 if press.document else None
		mode = self._askMode()
		while mode == "off" and waitUntil is not None and time.monotonic() < waitUntil:
			time.sleep(0.01)
			mode = self._askMode()
		if mode is not None:
			if mode == "off":
				return False
			masks = self._modeSets.get(mode, {}).get(press.keycode)
			if masks is None or press.effective & ~LOCK_MODS not in masks:
				return False
		return self._grabsTake(self._captureNow(time.monotonic()))
