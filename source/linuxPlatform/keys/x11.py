"""Keys from the X server, read on a thread of their own through a connection of their own.

Every key press and release comes from XInput 2 events selected on the root window: the slave
keyboard's own events, which carry key repeat and the XKB state the key was pressed in, and raw
events, which no client can hide. A raw event whose slave event does not follow is delivered on its
own, since an application that selects slave events on its window stops them reaching the root.
The NVDA keys and the numpad review keys are taken with passive grabs on the root, so a chord with
the NVDA key held goes to NVDA alone while the grab is active; everything else reaches the
application too, and is marked as having done so.
"""

import itertools
import os
import select
import struct
import threading
import time
from collections import deque
from collections.abc import Callable

from logHandler import log
from xkbcommon import xkb
from Xlib import X, display, error
from Xlib.ext import xinput, xtest

from . import KeyEvent, keyMap

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
#: The generic modifier each left or right one also holds down, for keyState.
GENERIC_FOR = {
	keyMap.VK["LSHIFT"]: keyMap.VK["SHIFT"],
	keyMap.VK["RSHIFT"]: keyMap.VK["SHIFT"],
	keyMap.VK["LCONTROL"]: keyMap.VK["CONTROL"],
	keyMap.VK["RCONTROL"]: keyMap.VK["CONTROL"],
	keyMap.VK["LMENU"]: keyMap.VK["MENU"],
	keyMap.VK["RMENU"]: keyMap.VK["MENU"],
}


class X11KeySource:
	"""A KeySource reading the X display named by DISPLAY."""

	def __init__(self) -> None:
		self._lock = threading.Lock()
		self._commands: deque[Callable[[], None]] = deque()
		self._wakeRead, self._wakeWrite = os.pipe()
		self._stopping = False
		self._thread: threading.Thread | None = None
		self._deliver: Callable[[KeyEvent], None] | None = None
		self._display: display.Display | None = None
		self._rulesNames: tuple[str, ...] | None = None
		self._context = xkb.Context()
		self._translator: keyMap.Translator | None = None
		#: Virtual-key codes held down, with the generic modifiers among them.
		self._down: set[int] = set()
		self._xtestDevices: set[int] = set()
		#: (key code, isUp) of injected keys whose echo has not come yet, with when each was sent.
		self._echoes: deque[tuple[float, int, bool]] = deque()
		#: Raw events waiting for their slave event: (arrival, key code, isUp, source device).
		self._raw: deque[tuple[float, int, bool, int]] = deque()
		#: (key code, isUp) delivered from a raw event, whose late slave event must not come twice.
		self._rawDelivered: deque[tuple[float, int, bool]] = deque()
		#: (key code, isUp) of slave events that came before their raw event.
		self._slaveSeen: deque[tuple[float, int, bool]] = deque()
		self._rulesAtom: int | None = None
		#: Key code: grab modifiers, for every key grabbed now; None means any modifiers.
		self._grabbed: dict[int, list[int] | None] = {}
		#: The grabbed key whose press made a grab active, which lasts until it is released.
		self._activeGrab: int | None = None
		self._held = False

	def start(self, deliver: Callable[[KeyEvent], None]) -> None:
		self._deliver = deliver
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
			root.change_attributes(event_mask=X.PropertyChangeMask)
			self._rulesAtom = self._display.intern_atom(RULES_NAMES_PROPERTY)
			self._findXtestDevices()
			self._loadKeymap()
			self._display.sync()
		except Exception:
			self._display.close()
			self._display = None
			raise
		self._thread = threading.Thread(target=self._run, name="X11KeySource", daemon=True)
		self._thread.start()

	def stop(self) -> None:
		self._stopping = True
		self._wake()
		if self._thread is not None:
			self._thread.join(2)
			self._thread = None
		os.close(self._wakeRead)
		os.close(self._wakeWrite)

	def setConsumed(self, nvdaModifierKeys: int, numpadReview: bool) -> None:
		self._post(lambda: self._applyGrabs(nvdaModifierKeys, numpadReview))

	def holdKeyboard(self, hold: bool) -> None:
		self._post(lambda: self._hold(hold))

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

	def _run(self) -> None:
		d = self._display
		try:
			while not self._stopping:
				while self._commands:
					self._commands.popleft()()
				while d.pending_events():
					self._handle(d.next_event())
				self._flushRaw(time.monotonic())
				timeout = (self._raw[0][0] + RAW_WAIT - time.monotonic()) if self._raw else None
				readable, _w, _x = select.select(
					[d.fileno(), self._wakeRead],
					[],
					[],
					None if timeout is None else max(0, timeout),
				)
				if self._wakeRead in readable:
					os.read(self._wakeRead, 4096)
		except Exception:
			log.exception("Keys stopped being read from the X display")
		finally:
			try:
				d.close()
			except (OSError, error.ConnectionClosedError):
				log.debugWarning("Closing the key source's X connection", exc_info=True)

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
		log.debug(f"Keyboard layout {layout or 'us'} {variant}".rstrip())

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
			for index, (_when, slaveKeycode, slaveIsUp) in enumerate(self._slaveSeen):
				if (slaveKeycode, slaveIsUp) == (keycode, isUp):
					del self._slaveSeen[index]
					return
			self._raw.append((time.monotonic(), keycode, isUp, sourceid))
		elif evtype == xinput.HierarchyChanged:
			self._findXtestDevices()
		elif event.type == X.MappingNotify:
			self._display.refresh_keyboard_mapping(event)
			self._loadKeymap()
		elif event.type == X.PropertyNotify:
			if event.atom == self._rulesAtom:
				self._loadKeymap()

	def _onSlave(self, data, isUp: bool) -> None:
		now = time.monotonic()
		keycode = data.detail
		repeat = bool(data.flags & xinput.KeyRepeat)
		if not repeat:
			for index, (_arrival, rawKeycode, rawIsUp, _source) in enumerate(self._raw):
				if (rawKeycode, rawIsUp) == (keycode, isUp):
					del self._raw[index]
					break
			else:
				for index, (_when, rawKeycode, rawIsUp) in enumerate(self._rawDelivered):
					if (rawKeycode, rawIsUp) == (keycode, isUp):
						del self._rawDelivered[index]
						return
				# The server sends the raw event first, but nothing promises it.
				self._slaveSeen.append((now, keycode, isUp))
		self._process(keycode, isUp, repeat, data.mods, data.groups, data.sourceid, now)

	def _flushRaw(self, now: float) -> None:
		while self._rawDelivered and now - self._rawDelivered[0][0] > 1:
			self._rawDelivered.popleft()
		while self._slaveSeen and now - self._slaveSeen[0][0] > 1:
			self._slaveSeen.popleft()
		while self._raw and now - self._raw[0][0] >= RAW_WAIT:
			arrival, keycode, isUp, sourceid = self._raw.popleft()
			self._rawDelivered.append((now, keycode, isUp))
			self._process(keycode, isUp, False, None, None, sourceid, arrival)

	def _isEcho(self, keycode: int, isUp: bool, now: float) -> bool:
		with self._lock:
			while self._echoes and now - self._echoes[0][0] > ECHO_WAIT:
				self._echoes.popleft()
			for index, (_sent, echoKeycode, echoIsUp) in enumerate(self._echoes):
				if (echoKeycode, echoIsUp) == (keycode, isUp):
					del self._echoes[index]
					return True
		return False

	def _process(self, keycode, isUp, repeat, mods, groups, sourceid, arrival) -> None:
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
			if not repeat:
				# What the server's state is after this key, for keyState until the next event says.
				state.update_key(
					keycode, xkb.KeyDirection.XKB_KEY_UP if isUp else xkb.KeyDirection.XKB_KEY_DOWN
				)
			generic = GENERIC_FOR.get(vk)
			for code in (vk, generic):
				if code is None:
					continue
				if isUp:
					self._down.discard(code)
				else:
					self._down.add(code)
		consumed = self._held or self._activeGrab is not None
		if not consumed and not isUp and self._grabMatches(keycode, effective):
			consumed = True
			self._activeGrab = keycode
		try:
			if self._isEcho(keycode, isUp, arrival) or levelShift:
				return
			event = KeyEvent(
				vkCode=vk,
				scanCode=scan,
				extended=extended,
				injected=sourceid in self._xtestDevices,
				isUp=isUp,
				repeat=repeat,
				reachedApplication=not consumed,
				text=text,
				typing=bool(text) and text.isprintable() and not effective & COMMAND_MODS,
				time=arrival,
			)
			try:
				self._deliver(event)
			except Exception:
				log.exception("Delivering a key")
		finally:
			if isUp and keycode == self._activeGrab:
				self._activeGrab = None

	def _grabMatches(self, keycode: int, effective: int) -> bool:
		if keycode not in self._grabbed:
			return False
		modifiers = self._grabbed[keycode]
		if modifiers is None:
			return True
		return effective in modifiers

	def _applyGrabs(self, nvdaModifierKeys: int, numpadReview: bool) -> None:
		root = self._display.screen().root
		for keycode, modifiers in self._grabbed.items():
			for mod in [X.AnyModifier] if modifiers is None else modifiers:
				root.ungrab_key(keycode, mod)
		self._grabbed = {}
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
		refused = []
		for keycode, modifiers, label in wanted:
			catcher = error.CatchError(error.BadAccess)
			for mod in [X.AnyModifier] if modifiers is None else modifiers:
				root.grab_key(keycode, mod, False, X.GrabModeAsync, X.GrabModeAsync, onerror=catcher)
			self._display.sync()
			if catcher.get_error():
				if label not in refused:
					refused.append(label)
				for mod in [X.AnyModifier] if modifiers is None else modifiers:
					root.ungrab_key(keycode, mod)
			else:
				self._grabbed[keycode] = modifiers
		self._display.sync()
		for label in refused:
			log.warning(
				f"NVDA could not take the {label}, which another program holds; "
				"is another screen reader running?",
			)

	def _hold(self, hold: bool) -> None:
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

	def _fake(self, keycode: int, isUp: bool) -> None:
		d = self._display
		if self._activeGrab is not None and not self._held:
			# A key sent while an NVDA key's grab is active would come back to that grab, not reach the
			# application.
			d.ungrab_keyboard(X.CurrentTime)
			self._activeGrab = None
		xtest.fake_input(d, X.KeyRelease if isUp else X.KeyPress, keycode)
		d.flush()
