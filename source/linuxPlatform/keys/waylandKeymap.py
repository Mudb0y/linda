"""The keymap the compositor itself compiled, as it gives it to every client's keyboard, held against
the names the desktop's layout feed gives.

The desktop's feeds name a keymap from the desktop's settings, and those names are not always what
the compositor compiles: GNOME's shell packs every input source into one keymap, and a key the source
in use leaves undefined takes the first source's symbols there, so AltGr stays AltGr in US English
beside Polish; sway's configuration may give options its IPC never reports; and compositors with no
listing of their own say nothing at all. So a private Wayland connection on a thread of its own binds
the seat's keyboard, maps no surface, and so takes no focus and hears no key, and reads the keymap and
repeat_info the compositor sends.

What the kernel source compiles is still names, so the names that compile to exactly the compositor's
keymap are found: the desktop's own, else the layouts the compositor's keymap names, found again by
their descriptions, with the desktop's options, else with the options whose components the keymap's
own section names carry. Each is held to the compositor's keymap key by key in every group, and only
one that matches is given. The group still comes from the desktop's feed, which says which layout is
in use; where it does not, the keys NVDA passes on lock it, as for any feed of names alone. Where no
names match, the desktop's own stand, and that is logged once with a count.

Only layout names and counts are logged.
"""

import hashlib
import mmap
import os
import re
import select
import threading
from collections.abc import Callable
from typing import NamedTuple

from logHandler import log
from xkbcommon import xkb

from .. import displayServer
from . import desktopLayout
from .desktopLayout import STOP_TIMEOUT, LayoutFeed, LayoutSnapshot, activeLayoutName

#: wl_seat.capability.keyboard, and wl_keyboard.keymap_format.xkb_v1.
SEAT_KEYBOARD = 2
KEYMAP_FORMAT_XKB_V1 = 1
#: The newest wl_seat bound: wl_keyboard's repeat_info came with 4, and 7 requires the keymap to be
#: mapped privately, as it is here.
SEAT_VERSION = 7
#: wl_keyboard.release came with version 3.
RELEASE_VERSION = 3
#: Seconds start waits for the compositor's first keymap, beside the desktop's first answer.
KEYMAP_WAIT = 1.0
#: A keymap larger than this is not one.
MAX_KEYMAP = 16 * 1024 * 1024
#: The sources whose feed says which layout is in use; any other is followed by the keys passed on.
GROUP_SOURCES = ("gnome", "kde", "sway")
#: The interval given with keys that do not repeat, as desktopLayout's KDE feed gives it.
_NO_REPEAT_INTERVAL = 0.04
#: The highest key code the kernel source reads.
_MAX_KEYCODE = 255
_LIGHTS = ("Caps Lock", "Num Lock", "Scroll Lock")
_DOWN = xkb.KeyDirection.XKB_KEY_DOWN
_UP = xkb.KeyDirection.XKB_KEY_UP
_DEPRESSED = xkb.StateComponent.XKB_STATE_MODS_DEPRESSED
#: The eight real modifiers, whose bits are their indices in every keymap.
_REAL_MODS = 0xFF
#: Every mix of Shift, Lock, Mod2, Mod3 and Mod5, the real modifiers Num Lock and the third and fifth
#: levels map to, by their bits.
_LEVEL_MIXES = tuple(
	sum(bit for i, bit in enumerate((1 << 0, 1 << 1, 1 << 4, 1 << 5, 1 << 7)) if mix & 1 << i)
	for mix in range(32)
)
#: A keymap section's name, as xkbcommon writes the components it was made of.
_SECTION = re.compile(r'^\s*xkb_(keycodes|types|compatibility|symbols)\s+"([^"]*)"', re.MULTILINE)
_SECTION_KINDS = {"compatibility": "compat"}
#: How many compiled keymaps, signatures and namings are kept, each.
_CACHE_SIZE = 16


class Naming(NamedTuple):
	"""Names that compile to the compositor's keymap."""

	names: tuple[str, str, str, str, str]
	#: Each layout of the keymap as (layout, variant), in its order.
	layouts: tuple[tuple[str, str], ...]
	#: Whether the names are the desktop feed's own.
	desktops: bool


class _Cache(dict):
	"""A dict that forgets what it learnt first once it holds _CACHE_SIZE entries."""

	def put(self, key, value):
		if len(self) >= _CACHE_SIZE:
			del self[next(iter(self))]
		self[key] = value
		return value


_lock = threading.Lock()
_compiled = _Cache()
_signatures = _Cache()
_optionTables: dict[str, dict[str, list[tuple[str, frozenset[str]]]]] = {}


def _quietContext():
	"""A context that writes nothing to standard error: names tried here that make nothing, or that
	name an option a layout does not take, are only candidates."""
	context = xkb.Context()
	context.set_log_level(xkb.lib.XKB_LOG_LEVEL_CRITICAL)
	return context


def compileNames(names: tuple[str, ...]):
	"""The keymap names make, as the kernel source compiles them; None when they make none."""
	names = tuple(names)
	with _lock:
		if names in _compiled:
			return _compiled[names]
	rules, model, layout, variant, options = names
	try:
		keymap = _quietContext().keymap_new_from_names(
			rules=rules or None,
			model=model or None,
			layout=layout or None,
			variant=variant or None,
			options=options or None,
		)
	except xkb.XKBError:
		keymap = None
	with _lock:
		return _compiled.put(names, keymap)


def signature(keymap) -> tuple:
	"""What typing does with each key in each layout of keymap, as the kernel source's tables read it:
	each level's keysyms and the level each mix of the modifiers that choose levels gives, whether the
	key repeats, the real modifiers it sets and the lights it changes pressed alone. By behaviour and
	real modifiers alone, because a keymap written out and read again lists its types' entries
	differently, and the compositor's comes that way."""
	lights = []
	for name in _LIGHTS:
		try:
			lights.append(keymap.led_get_index(name))
		except xkb.XKBError:
			lights.append(None)
	low = keymap.min_keycode()
	high = min(keymap.max_keycode(), _MAX_KEYCODE)
	groups = []
	for group in range(keymap.num_layouts()):
		mixes = []
		for mask in _LEVEL_MIXES:
			mixed = keymap.state_new()
			mixed.update_mask(mask, 0, 0, 0, 0, group)
			mixes.append(mixed)
		keys = []
		for keycode in range(low, high + 1):
			state = keymap.state_new()
			state.update_mask(0, 0, 0, 0, 0, group)
			try:
				layout = state.key_get_layout(keycode)
			except xkb.XKBInvalidLayoutIndex:
				# A key the keymap gives no symbols.
				keys.append(None)
				continue
			levels = tuple(
				tuple(keymap.key_get_syms_by_level(keycode, layout, level))
				for level in range(keymap.num_levels_for_key(keycode, layout))
			)
			chosen = tuple(mixed.key_get_level(keycode, layout) for mixed in mixes)
			before = [state.led_index_is_active(i) if i is not None else False for i in lights]
			state.update_key(keycode, _DOWN)
			sets = state.serialize_mods(_DEPRESSED) & _REAL_MODS
			state.update_key(keycode, _UP)
			after = [state.led_index_is_active(i) if i is not None else False for i in lights]
			changed = tuple(name for name, b, a in zip(_LIGHTS, before, after) if a != b)
			keys.append((keycode, levels, chosen, keymap.key_repeats(keycode), sets, changed))
		groups.append(tuple(keys))
	return tuple(groups)


def _signatureOf(key, keymap) -> tuple:
	with _lock:
		if key in _signatures:
			return _signatures[key]
	made = signature(keymap)
	with _lock:
		return _signatures.put(key, made)


def differences(a: tuple, b: tuple) -> int:
	"""How many keys of how many layouts two signatures differ in."""
	count = abs(len(a) - len(b)) * max((len(g) for g in a + b), default=0)
	for groupA, groupB in zip(a, b):
		keysA = {k[0]: k for k in groupA if k is not None}
		keysB = {k[0]: k for k in groupB if k is not None}
		count += sum(1 for code in keysA.keys() | keysB.keys() if keysA.get(code) != keysB.get(code))
	return count


def sectionNames(text: str) -> dict[str, str]:
	"""The names xkbcommon gave the keymap's sections, by the rules' names for them: symbols, compat,
	types and keycodes. A keymap made from names carries the components it was made of there, with
	their plus signs and colons written as underscores."""
	return {_SECTION_KINDS.get(kind, kind): name for kind, name in _SECTION.findall(text)}


def _rulesFile(rules: str) -> str | None:
	context = xkb.Context()
	for i in range(context.num_include_paths()):
		path = os.path.join(context.include_path_get(i), "rules", rules)
		if os.path.isfile(path):
			return path
	return None


def optionTable(rules: str = desktopLayout.DEFAULT_RULES) -> dict[str, list[tuple[str, frozenset[str]]]]:
	"""Each option the rules file names, with the components each of its rules adds, by the section
	they go to; options given by a wildcard or a substitution are left out."""
	with _lock:
		if rules in _optionTables:
			return _optionTables[rules]
	table: dict[str, list[tuple[str, frozenset[str]]]] = {}
	path = _rulesFile(rules)
	text = ""
	if path is not None:
		try:
			with open(path, encoding="utf-8", errors="replace") as f:
				text = f.read()
		except OSError:
			log.debug(f"Keyboard layout: the rules {rules} could not be read")
	columns: list[str] = []
	target = None
	for raw in text.replace("\\\n", " ").splitlines():
		line = raw.split("//", 1)[0].strip()
		if not line:
			continue
		if line.startswith("!"):
			left, _equals, right = line[1:].partition("=")
			heads = left.split()
			columns = [re.sub(r"\[\d+\]$", "", h) for h in heads]
			kind = right.strip()
			isOptions = heads and not heads[0].startswith("$") and "option" in columns
			target = kind if isOptions and kind in ("symbols", "compat", "types") else None
			continue
		if target is None:
			continue
		left, equals, right = line.partition("=")
		tokens = left.split()
		value = right.strip()
		if not equals or len(tokens) != len(columns) or not value or "%" in value:
			continue
		option = tokens[columns.index("option")]
		if "*" in option:
			continue
		parts = frozenset(re.sub(r":\d+$", "", p) for p in re.split(r"[+|]", value) if p)
		table.setdefault(option, []).append((target, parts))
	with _lock:
		_optionTables[rules] = table
	return table


def optionsIn(text: str, rules: str = desktopLayout.DEFAULT_RULES) -> str:
	"""The options, in the rules' order, whose components a keymap's section names carry."""
	sections = {kind: f"_{name}_" for kind, name in sectionNames(text).items()}
	found = []
	for option, entries in optionTable(rules).items():
		for target, parts in entries:
			section = sections.get(target)
			if section is not None and all(f"_{p}_" in section for p in parts):
				found.append(option)
				break
	return ",".join(found)


def _pairs(names: tuple[str, ...], count: int) -> list[tuple[str, str]]:
	layouts = names[2].split(",") if names[2] else [desktopLayout.DEFAULT_LAYOUT]
	variants = names[3].split(",")
	pairs = []
	for i in range(count):
		layout = layouts[i] if i < len(layouts) else ""
		pairs.append((layout, variants[i] if i < len(variants) and layout else ""))
	return pairs


def _described(names: tuple[str, ...]) -> dict[str, tuple[str, str]]:
	"""Each layout of names, by the description its symbols give it."""
	keymap = compileNames(names)
	if keymap is None:
		return {}
	described: dict[str, tuple[str, str]] = {}
	for i, pair in enumerate(_pairs(names, keymap.num_layouts())):
		described.setdefault(keymap.layout_get_name(i), pair)
	return described


def _registry(rules: str) -> dict[str, tuple[str, str]]:
	from .swayLayout import registry

	return registry(rules or desktopLayout.DEFAULT_RULES)


def nameKeymap(keymap, text: str, base: LayoutSnapshot, *, digest: str | None = None) -> Naming | None:
	"""Names that compile to keymap, found from base, the desktop's own; None when none is found.

	:param text: The keymap as the compositor sent it.
	:param digest: What identifies the keymap, for the signatures kept.
	"""
	digest = digest or hashlib.sha256(text.encode()).hexdigest()
	wanted = _signatureOf(("compositor", digest), keymap)
	count = keymap.num_layouts()
	names = tuple(base.names)
	candidates: list[tuple[tuple[str, str, str, str, str], bool]] = [(names, True)]
	described = _described(names)
	registry = None
	layouts = []
	for i in range(count):
		description = keymap.layout_get_name(i)
		pair = described.get(description)
		if pair is None:
			if registry is None:
				registry = _registry(names[0])
			pair = registry.get(description)
		if pair is None:
			layouts = None
			break
		layouts.append(pair)
	if layouts is not None:
		rules, model, _layout, _variant, options = names
		rebuilt = (rules, model, ",".join(p[0] for p in layouts), ",".join(p[1] for p in layouts))
		candidates.append(((*rebuilt, options), False))
		found = optionsIn(text, rules or desktopLayout.DEFAULT_RULES)
		if found != options:
			candidates.append(((*rebuilt, found), False))
	for candidate, desktops in candidates:
		made = compileNames(candidate)
		if made is None or made.num_layouts() != count:
			continue
		if _signatureOf(("names", candidate), made) == wanted:
			return Naming(candidate, tuple(_pairs(candidate, count)), desktops)
	return None


def mapGroup(naming: Naming, base: LayoutSnapshot, keymap) -> int | None:
	"""The group of naming that is base's layout in use; None when the keymap does not hold it."""
	if naming.desktops:
		return base.group
	count = len(naming.layouts)
	names = tuple(base.names)
	baseKeymap = compileNames(names)
	baseCount = baseKeymap.num_layouts() if baseKeymap is not None else 1
	group = base.group if 0 <= base.group < baseCount else 0
	pair = _pairs(names, baseCount)[group]
	if pair in naming.layouts:
		return naming.layouts.index(pair)
	if baseKeymap is not None:
		description = baseKeymap.layout_get_name(group)
		for i in range(count):
			if keymap.layout_get_name(i) == description:
				return i
	return 0 if count == 1 else None


def readKeymap(fd: int, size: int) -> bytes | None:
	"""The keymap a wl_keyboard.keymap event's file holds, without its closing NUL."""
	if not 0 < size <= MAX_KEYMAP:
		return None
	with mmap.mmap(fd, size, mmap.MAP_PRIVATE, mmap.PROT_READ) as data:
		return data[:].rstrip(b"\0")


class WaylandKeymapFeed:
	"""A layout feed that gives the desktop feed's layout as the compositor compiled it."""

	def __init__(self, base: LayoutFeed, socketPath: str | None) -> None:
		"""
		:param base: The desktop's own feed, which says which layout is in use.
		:param socketPath: The compositor's socket; None reads no keymap, and base is given as it is.
		"""
		self._baseFeed = base
		self._socketPath = socketPath
		self._lock = threading.RLock()
		self._onChange: Callable[[LayoutSnapshot], None] | None = None
		self._base: LayoutSnapshot | None = None
		#: The compositor's keymap as it came, compiled, and what identifies it.
		self._text: str | None = None
		self._keymap = None
		self._digest: str | None = None
		#: The compositor's repeat_info, as LayoutSnapshot.repeat has it.
		self._repeat: tuple[bool, float, float] | None = None
		self._namings = _Cache()
		#: The digests of the keymaps no names were found for, logged once each.
		self._unnamed: set[str] = set()
		self._lastWord: str | None = None
		self._last: LayoutSnapshot | None = None
		self._returned = False
		self._stopping = False
		self._shadow = None
		self._thread: threading.Thread | None = None
		self._wake: tuple[int, int] | None = None
		self._firstKeymap = threading.Event()

	# The feed.

	def start(self, onChange: Callable[[LayoutSnapshot], None]) -> LayoutSnapshot:
		self._onChange = onChange
		if self._socketPath is not None:
			self._wake = os.pipe2(os.O_CLOEXEC | os.O_NONBLOCK)
			self._thread = threading.Thread(target=self._run, name="lindaKeyboardLayout", daemon=True)
			self._thread.start()
		else:
			self._firstKeymap.set()
		base = self._baseFeed.start(self._baseChanged)
		self._firstKeymap.wait(KEYMAP_WAIT)
		with self._lock:
			if self._base is None:
				self._base = base
			snapshot = self._snapshot()
			self._returned = True
			self._last = snapshot
			if self._shadows(snapshot):
				self._shadowFor().start(snapshot)
		return snapshot

	def stop(self) -> None:
		self._stopping = True
		thread = self._thread
		if self._wake is not None:
			try:
				os.write(self._wake[1], b"x")
			except OSError:
				pass
		if thread is not None and thread is not threading.current_thread():
			thread.join(STOP_TIMEOUT)
			if thread.is_alive():
				log.debugWarning("Keyboard layout: the compositor's keymap thread did not end in time")
		try:
			self._baseFeed.stop()
		finally:
			if self._shadow is not None:
				self._shadow.stop()
			if self._wake is not None and (thread is None or not thread.is_alive()):
				for fd in self._wake:
					os.close(fd)
				self._wake = None

	def notePassed(self, keycode: int, isUp: bool) -> None:
		"""A key passed on, or its release, on the thread that answers keys: for the group, where the
		desktop's feed does not say it."""
		shadow = self._shadow
		if shadow is not None:
			shadow.notePassed(keycode, isUp)

	# Following.

	def _baseChanged(self, snapshot: LayoutSnapshot) -> None:
		with self._lock:
			self._base = snapshot
			if self._returned:
				self._passOn()

	def _keymapChanged(self, data: bytes) -> None:
		try:
			text = data.decode("ascii")
			keymap = _quietContext().keymap_new_from_buffer(data, length=len(data))
		except (UnicodeDecodeError, xkb.XKBError):
			log.debugWarning("Keyboard layout: the compositor sent a keymap xkbcommon cannot read")
			return
		digest = hashlib.sha256(data).hexdigest()
		with self._lock:
			if digest == self._digest:
				return
			self._text, self._keymap, self._digest = text, keymap, digest
			log.debug(f"Keyboard layout: the compositor's keymap has {keymap.num_layouts()} layouts")
			if self._returned:
				self._passOn()

	def _repeatChanged(self, rate: int, delay: int) -> None:
		with self._lock:
			self._repeat = (rate > 0, delay / 1000, 1 / rate if rate > 0 else _NO_REPEAT_INTERVAL)
			if self._returned:
				self._passOn()

	def _passOn(self) -> None:
		"""Pass on the layout now, on the thread that saw the change, with the feed's lock held."""
		if self._stopping:
			return
		try:
			snapshot = self._snapshot()
		except Exception:
			log.exception("Keyboard layout: naming the compositor's keymap failed")
			return
		if self._shadows(snapshot):
			self._shadowFor().change(snapshot)
		else:
			self._emit(snapshot)

	def _shadows(self, snapshot: LayoutSnapshot) -> bool:
		return snapshot.source not in GROUP_SOURCES

	def _shadowFor(self):
		if self._shadow is None:
			from .shadowGroup import ShadowGroup

			self._shadow = ShadowGroup(self._emit)
		return self._shadow

	def _emit(self, snapshot: LayoutSnapshot) -> None:
		with self._lock:
			if self._stopping or snapshot == self._last:
				return
			self._last = snapshot
			try:
				self._onChange(snapshot)
			except Exception:
				log.exception("Keyboard layout: passing a change on failed")

	def _snapshot(self) -> LayoutSnapshot:
		"""The desktop's layout as the compositor's keymap names it, with the feed's lock held."""
		base = self._base
		if base.repeat is None and self._repeat is not None:
			base = base._replace(repeat=self._repeat)
		keymap = self._keymap
		if keymap is None:
			self._say(
				"none", "Keyboard layout: the compositor has given no keymap, so the desktop's layout stands"
			)
			return base
		key = (self._digest, tuple(base.names))
		if key in self._namings:
			naming = self._namings[key]
		else:
			naming = self._namings.put(key, nameKeymap(keymap, self._text, base, digest=self._digest))
		if naming is None:
			if self._digest not in self._unnamed:
				self._unnamed.add(self._digest)
				wanted = _signatureOf(("compositor", self._digest), keymap)
				own = compileNames(tuple(base.names))
				count = differences(wanted, _signatureOf(("names", tuple(base.names)), own)) if own else 0
				log.info(
					"Keyboard layout: no names make the compositor's keymap, so the desktop's layout stands; "
					f"{count} keys differ",
				)
			self._lastWord = "unnamed"
			return base
		group = mapGroup(naming, base, keymap)
		if group is None:
			self._say(
				"ungrouped",
				"Keyboard layout: the compositor's keymap does not hold the desktop's layout in use, "
				"so the desktop's layout stands",
			)
			return base
		if base.notice is not None and base.notice == desktopLayout._usNotice():
			# The layout is no longer a guess: it is the compositor's.
			base = base._replace(notice=None)
		if naming.desktops:
			self._say("desktops", "Keyboard layout: the compositor's keymap is the desktop's layout")
			return base
		snapshot = base._replace(names=naming.names, group=group)
		self._say(
			f"named {naming.names[2]}",
			f"Keyboard layout: named from the compositor's keymap: {naming.names[2]}, "
			f"in use {activeLayoutName(snapshot)}",
		)
		return snapshot

	def _say(self, word: str, line: str) -> None:
		if word != self._lastWord:
			self._lastWord = word
			log.info(line)

	# The Wayland connection, on the feed's own thread.

	def _run(self) -> None:
		display = None
		try:
			from pywayland.client import Display

			display = Display(self._socketPath)
			display.connect()
		except Exception as e:  # noqa: BLE001
			log.debug(f"Keyboard layout: no connection to the compositor for its keymap: {type(e).__name__}")
			self._firstKeymap.set()
			return
		try:
			self._follow(display)
		except Exception:  # noqa: BLE001
			# The compositor closed the connection, or broke the protocol: the last keymap stands.
			if not self._stopping:
				log.debugWarning(
					"Keyboard layout: the compositor's keymap is no longer followed", exc_info=True
				)
		finally:
			self._firstKeymap.set()
			try:
				display.disconnect()
			except Exception:  # noqa: BLE001
				log.debugWarning("Keyboard layout: closing the compositor connection failed", exc_info=True)

	def _follow(self, display) -> None:
		from pywayland.protocol.wayland import WlSeat

		registry = display.get_registry()
		seats: list[tuple[int, int]] = []

		def onGlobal(_registry, name, interface, version):
			if interface == "wl_seat":
				seats.append((name, version))

		registry.dispatcher["global"] = onGlobal
		display.roundtrip()
		if not seats:
			log.debug("Keyboard layout: the compositor offers no seat")
			self._firstKeymap.set()
			return
		seatName, seatVersion = seats[0]
		version = min(seatVersion, SEAT_VERSION)
		seat = registry.bind(seatName, WlSeat, version)
		keyboards: list = []

		def onCapabilities(_seat, capabilities):
			if capabilities & SEAT_KEYBOARD and not keyboards:
				keyboard = seat.get_keyboard()
				keyboard.dispatcher["keymap"] = onKeymap
				keyboard.dispatcher["repeat_info"] = onRepeat
				keyboards.append(keyboard)
			elif not capabilities & SEAT_KEYBOARD and keyboards:
				keyboard = keyboards.pop()
				if version >= RELEASE_VERSION:
					keyboard.release()
				else:
					keyboard.destroy()
			if not keyboards:
				# The last keymap stands until a keyboard comes.
				self._firstKeymap.set()

		def onKeymap(_keyboard, keymapFormat, fd, size):
			try:
				data = readKeymap(fd, size) if keymapFormat == KEYMAP_FORMAT_XKB_V1 else None
			except (OSError, ValueError):
				data = None
			finally:
				os.close(fd)
			if data:
				self._keymapChanged(data)
			else:
				log.debugWarning("Keyboard layout: the compositor sent a keymap that is not XKB's")
			self._firstKeymap.set()

		def onRepeat(_keyboard, rate, delay):
			self._repeatChanged(rate, delay)

		seat.dispatcher["capabilities"] = onCapabilities
		fd = display.get_fd()
		wake = self._wake[0]
		while not self._stopping:
			display.dispatch(block=False)
			display.flush()
			readable, _w, _x = select.select([fd, wake], [], [])
			if wake in readable or self._stopping:
				return
			display.read()
			display.dispatch(block=False)


#: Set while create asks desktopLayout for the feed this one stands in front of, so that this module
#: does not offer itself again.
_creating = threading.local()


def create(currentDesktop: str | None) -> WaylandKeymapFeed | None:
	"""A feed in a Wayland session whose compositor can be reached, for desktopLayout.create, in front of
	the feed it would otherwise give; None otherwise."""
	if getattr(_creating, "active", False) or not displayServer.isWayland():
		return None
	socketPath = displayServer.waylandSocket()
	if socketPath is None:
		log.debug("Keyboard layout: no compositor socket to read its keymap from")
		return None
	try:
		import pywayland.client  # noqa: F401
	except ImportError:
		log.debug("Keyboard layout: pywayland is not installed, so the compositor's keymap is not read")
		return None
	_creating.active = True
	try:
		base = desktopLayout.create(currentDesktop)
	finally:
		_creating.active = False
	return WaylandKeymapFeed(base, socketPath)
