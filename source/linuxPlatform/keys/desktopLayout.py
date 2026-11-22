"""The keyboard layout of a session with no X server to ask: the names of the keymap the desktop
compiles, which group of it is in use, how keys repeat and a guess at the locks, as the desktop's own
settings give them.

GNOME keeps its input sources in GSettings and its shell compiles them; only the source in use is
compiled here, alone, as group 0, which gives every key the symbols the shell's keymap gives it in
that source without mirroring how the shell packs sources into groups. KWin compiles kxkbrc's layout
list whole and says over the session bus which of it is in use. Elsewhere localed's system layout is
taken, then the XKB_DEFAULT variables, then US English with a notice; neither says which group of
several layouts is in use, so those two feeds follow it from the keys NVDA passes on, which reach them
through notePassed (shadowGroup).

Each feed follows its desktop on a thread with a GLib main context of its own, so that a change is
passed on whatever NVDA's main thread is doing. Only layout names and where they came from are
logged.

Modules other packages add beside this one, with a create(currentDesktop) that gives a feed or None,
are tried first, in OPTIONAL_MODULES' order, so that a feed for one compositor needs no edit here.
"""

import importlib
import math
import os
import threading
from collections.abc import Callable
from typing import NamedTuple, Protocol

from logHandler import log

from .keydClient import LOCK_NUM

#: Modules tried before the desktop's own feed, first first: the compositor's own keymap, sway's IPC
#: and gamescope's Xwayland.
OPTIONAL_MODULES = ("waylandKeymap", "swayLayout", "gamescopeLayout")
#: Seconds start waits for the desktop's first answer before it gives the environment's.
START_TIMEOUT = 3.0
#: Seconds stop waits for the feed's thread to end.
STOP_TIMEOUT = 2.0
#: Milliseconds a call on a bus may take.
BUS_TIMEOUT_MS = 2000

DEFAULT_RULES = "evdev"
DEFAULT_MODEL = "pc105"
DEFAULT_LAYOUT = "us"

INPUT_SOURCES_SCHEMA = "org.gnome.desktop.input-sources"
KEYBOARD_SCHEMA = "org.gnome.desktop.peripherals.keyboard"

KDE_SERVICE = "org.kde.keyboard"
KDE_PATH = "/Layouts"
KDE_INTERFACE = "org.kde.KeyboardLayouts"
#: KWin's model when kxkbrc names none.
KDE_DEFAULT_MODEL = "pc104"
#: kcminputrc's NumLock: 0 turns it on at start, 1 off, and 2 leaves it as it was.
KDE_NUM_LOCK_ON, KDE_NUM_LOCK_OFF = 0, 1

LOCALED_SERVICE = "org.freedesktop.locale1"
LOCALED_PATH = "/org/freedesktop/locale1"

_DBUS = "org.freedesktop.DBus"
_DBUS_PATH = "/org/freedesktop/DBus"
_PROPERTIES = "org.freedesktop.DBus.Properties"


class LayoutSnapshot(NamedTuple):
	"""One state of the desktop's keyboard layout, replaced whole on every change."""

	#: Rules, model, layout, variant and options, as keymap_new_from_names takes them.
	names: tuple[str, str, str, str, str]
	#: The locked group, from 0.
	group: int = 0
	#: Whether keys repeat, and the seconds before the first repeat and between repeats; None where the
	#: desktop does not say.
	repeat: tuple[bool, float, float] | None = None
	#: keydClient's LOCK_ bits of the locks on at start, where the desktop says.
	locksGuess: int | None = None
	#: Where the names came from: gnome, kde, localed, environment, or another feed's name.
	source: str = "environment"
	#: Whether an input method has the keys, so that what a key types is not the layout's to say.
	inputMethod: bool = False
	#: A sentence for the user about the layout, said once.
	notice: str | None = None


class LayoutFeed(Protocol):
	def start(self, onChange: Callable[[LayoutSnapshot], None]) -> LayoutSnapshot:
		"""Give the layout now, and pass each later one to onChange, on the feed's own thread."""

	def stop(self) -> None: ...


def activeLayoutName(snapshot: LayoutSnapshot) -> str:
	"""The layout and variant of the snapshot's group, for the log."""
	layouts = snapshot.names[2].split(",")
	variants = snapshot.names[3].split(",")
	group = snapshot.group if 0 <= snapshot.group < len(layouts) else 0
	variant = variants[group] if group < len(variants) else ""
	return f"{layouts[group] or DEFAULT_LAYOUT} {variant}".rstrip()


def _usNotice() -> str:
	# Translators: Said on Linux in a Wayland session where neither the desktop nor the system says
	# which keyboard layout is in use.
	return _("NVDA cannot find this desktop's keyboard layout, so it reads keys as US English.")


def environmentSnapshot(*, notice: bool = True) -> LayoutSnapshot:
	"""The layout the XKB_DEFAULT variables name, as xkbcommon itself takes them, else US English."""
	env = os.environ
	layout = env.get("XKB_DEFAULT_LAYOUT", "")
	names = (
		env.get("XKB_DEFAULT_RULES") or DEFAULT_RULES,
		env.get("XKB_DEFAULT_MODEL") or DEFAULT_MODEL,
		layout or DEFAULT_LAYOUT,
		env.get("XKB_DEFAULT_VARIANT", "") if layout else "",
		env.get("XKB_DEFAULT_OPTIONS", ""),
	)
	return LayoutSnapshot(names, source="environment", notice=None if layout or not notice else _usNotice())


def _connect(busType):
	"""A connection of the feed's own, so that nothing else on the bus can close it or hold it up."""
	from gi.repository import Gio

	address = Gio.dbus_address_get_for_bus_sync(busType, None)
	return Gio.DBusConnection.new_for_address_sync(
		address,
		Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
		None,
		None,
	)


def _close(connection) -> None:
	from gi.repository import GLib

	try:
		connection.close_sync(None)
	except GLib.Error:
		log.debugWarning("Keyboard layout: closing a bus connection failed", exc_info=True)


def localedSnapshot() -> LayoutSnapshot | None:
	"""The system's layout from localed, or None where it cannot be asked or names none."""
	from gi.repository import Gio, GLib

	try:
		connection = _connect(Gio.BusType.SYSTEM)
	except GLib.Error as e:
		log.debug(f"Keyboard layout: no system bus to ask localed on: {e.message}")
		return None
	try:
		(properties,) = connection.call_sync(
			LOCALED_SERVICE,
			LOCALED_PATH,
			_PROPERTIES,
			"GetAll",
			GLib.Variant("(s)", (LOCALED_SERVICE,)),
			GLib.VariantType.new("(a{sv})"),
			Gio.DBusCallFlags.NONE,
			BUS_TIMEOUT_MS,
			None,
		).unpack()
	except GLib.Error as e:
		log.debug(f"Keyboard layout: localed did not answer: {e.message}")
		return None
	finally:
		_close(connection)

	def text(name: str) -> str:
		value = properties.get(name, "")
		return value if isinstance(value, str) else ""

	layout = text("X11Layout")
	if not layout:
		log.debug("Keyboard layout: localed names no layout")
		return None
	names = (DEFAULT_RULES, text("X11Model") or DEFAULT_MODEL, layout, text("X11Variant"), text("X11Options"))
	return LayoutSnapshot(names, source="localed")


def systemSnapshot() -> LayoutSnapshot:
	"""localed's layout, else the environment's."""
	return localedSnapshot() or environmentSnapshot()


class _ThreadedFeed:
	"""A feed whose reading and following runs on a thread with a GLib main context of its own."""

	#: Whether the thread stays to follow changes after the first reading.
	follows = True

	def __init__(self) -> None:
		self._lock = threading.Lock()
		self._ready = threading.Event()
		self._onChange: Callable[[LayoutSnapshot], None] | None = None
		self._last: LayoutSnapshot | None = None
		self._returned = False
		self._stopping = False
		self._thread: threading.Thread | None = None
		self._context = None
		self._loop = None

	def start(self, onChange: Callable[[LayoutSnapshot], None]) -> LayoutSnapshot:
		from gi.repository import GLib

		if self._thread is not None:
			raise RuntimeError("the layout feed has started already")
		self._onChange = onChange
		self._context = GLib.MainContext.new()
		self._loop = GLib.MainLoop.new(self._context, False)
		self._thread = threading.Thread(target=self._run, name="lindaKeyboardLayout", daemon=True)
		self._thread.start()
		answered = self._ready.wait(START_TIMEOUT)
		with self._lock:
			self._returned = True
			if self._last is None:
				# Until the desktop answers; what it says then comes as a change.
				self._last = environmentSnapshot(notice=False)
			snapshot = self._last
		if not answered:
			log.debugWarning(f"Keyboard layout: the desktop did not answer within {START_TIMEOUT} s")
		log.info(f"Keyboard layout from {snapshot.source}: {activeLayoutName(snapshot)}")
		return snapshot

	def stop(self) -> None:
		from gi.repository import GLib

		self._stopping = True
		thread = self._thread
		if thread is None:
			return
		source = GLib.idle_source_new()
		source.set_callback(lambda *args: self._loop.quit() or GLib.SOURCE_REMOVE)
		source.attach(self._context)
		if thread is not threading.current_thread():
			thread.join(STOP_TIMEOUT)
			if thread.is_alive():
				log.debugWarning("Keyboard layout: the feed's thread did not end in time")

	def _run(self) -> None:
		self._context.push_thread_default()
		try:
			try:
				snapshot = self._open()
			except Exception:
				log.exception("Keyboard layout: reading the desktop's layout failed")
				self.follows = False
				snapshot = systemSnapshot()
			self._publish(snapshot)
			if self.follows and not self._stopping:
				self._loop.run()
		except Exception:
			log.exception("Keyboard layout: following the desktop's layout failed")
		finally:
			try:
				self._shut()
			except Exception:  # noqa: BLE001
				log.debugWarning("Keyboard layout: closing the feed failed", exc_info=True)
			self._context.pop_thread_default()
			self._ready.set()

	def _open(self) -> LayoutSnapshot:
		"""Read the layout and start following it, on the feed's thread."""
		raise NotImplementedError

	def _shut(self) -> None:
		"""Stop following, on the feed's thread."""

	def _changed(self, read: Callable[[], LayoutSnapshot]) -> None:
		"""Pass on what read gives after the desktop signalled a change, on the feed's thread."""
		if self._stopping:
			return
		try:
			self._publish(read())
		except Exception:
			log.exception("Keyboard layout: reading a change failed")

	def _publish(self, snapshot: LayoutSnapshot) -> None:
		with self._lock:
			if snapshot == self._last:
				return
			self._last = snapshot
			returned = self._returned
		if not returned:
			self._ready.set()
			return
		if self._stopping:
			return
		log.debug(f"Keyboard layout from {snapshot.source}: {activeLayoutName(snapshot)}")
		try:
			self._onChange(snapshot)
		except Exception:
			log.exception("Keyboard layout: passing a change on failed")


class _FollowsGroup:
	"""A feed of names alone, which follows the group of a keymap of several layouts from the keys NVDA
	passes on."""

	_shadow = None

	def _shadowFor(self, onChange: Callable[[LayoutSnapshot], None]):
		from .shadowGroup import ShadowGroup

		self._shadow = ShadowGroup(onChange)
		return self._shadow

	def notePassed(self, keycode: int, isUp: bool) -> None:
		"""A key passed on, or its release, as the environment hears of it, on the thread that answers keys."""
		shadow = self._shadow
		if shadow is not None:
			shadow.notePassed(keycode, isUp)

	def _stopShadow(self) -> None:
		if self._shadow is not None:
			self._shadow.stop()


class EnvironmentLayoutFeed(_FollowsGroup):
	"""The XKB_DEFAULT variables, else US English with a notice; they never change, the group aside."""

	def start(self, onChange: Callable[[LayoutSnapshot], None]) -> LayoutSnapshot:
		snapshot = environmentSnapshot()
		log.info(f"Keyboard layout from {snapshot.source}: {activeLayoutName(snapshot)}")
		self._shadowFor(onChange).start(snapshot)
		return snapshot

	def stop(self) -> None:
		self._stopShadow()


class LocaledLayoutFeed(_FollowsGroup, _ThreadedFeed):
	"""localed's system layout, read once, then the environment's."""

	follows = False

	def start(self, onChange: Callable[[LayoutSnapshot], None]) -> LayoutSnapshot:
		shadow = self._shadowFor(onChange)
		snapshot = super().start(shadow.change)
		shadow.start(snapshot)
		return snapshot

	def stop(self) -> None:
		super().stop()
		self._stopShadow()

	def _open(self) -> LayoutSnapshot:
		return systemSnapshot()


class GnomeLayoutFeed(_ThreadedFeed):
	"""The input source GNOME's shell has in use, compiled alone, and the keyboard's settings."""

	#: The keys read of each schema.
	INPUT_SOURCES_KEYS = ("sources", "mru-sources", "xkb-options", "xkb-model")
	KEYBOARD_KEYS = ("repeat", "delay", "repeat-interval", "remember-numlock-state", "numlock-state")

	def __init__(self) -> None:
		super().__init__()
		self._handlers: list[tuple[object, int]] = []
		#: The value of each key read, by its name, which the two schemas do not share.
		self._values: dict[str, object] = {}
		#: The layout GNOME's shell falls back on while it has no input source.
		self._fallback: LayoutSnapshot | None = None

	def _open(self) -> LayoutSnapshot:
		from gi.repository import Gio, GObject

		schemas = Gio.SettingsSchemaSource.get_default()
		# Gio.Settings.new aborts the process on a schema that is not installed.
		schema = schemas.lookup(INPUT_SOURCES_SCHEMA, True) if schemas is not None else None
		if schema is None:
			log.debug("Keyboard layout: GNOME's input sources schema is not installed")
			self.follows = False
			return systemSnapshot()
		inputSources = Gio.Settings.new_full(schema, None, None)
		backend = GObject.type_name(inputSources.get_property("backend").__gtype__).lower()
		asked = os.environ.get("GSETTINGS_BACKEND", "").lower()
		# Without dconf's module GLib keeps settings in a key file or in memory of its own, neither of
		# them the shell's.
		if "dconf" not in backend and not (asked and asked in backend):
			log.debugWarning(
				"Keyboard layout: GSettings has no dconf module here, so GNOME's input sources cannot be read",
			)
			self.follows = False
			return systemSnapshot()
		keyboardSchema = schemas.lookup(KEYBOARD_SCHEMA, True)
		watched = [(inputSources, schema, self.INPUT_SOURCES_KEYS)]
		if keyboardSchema is not None:
			watched.append(
				(Gio.Settings.new_full(keyboardSchema, None, None), keyboardSchema, self.KEYBOARD_KEYS),
			)
		for settings, settingsSchema, keys in watched:
			# GSettings signals a change only of a key read since a handler was connected.
			self._handlers.append((settings, settings.connect("changed", self._onSettingChanged)))
			for key in keys:
				if settingsSchema.has_key(key):
					self._values[key] = settings.get_value(key).unpack()
		return self._read()

	def _onSettingChanged(self, settings, key: str) -> None:
		# Only the key that changed is read, and at once: with NVDA's main thread busy, each call into
		# GLib waits a switch interval for the interpreter on its way back.
		if key in self._values:
			self._changed(lambda: self._readKey(settings, key))

	def _readKey(self, settings, key: str) -> LayoutSnapshot:
		self._values[key] = settings.get_value(key).unpack()
		return self._read()

	def _read(self) -> LayoutSnapshot:
		values = self._values
		sources = [tuple(s) for s in values["sources"]]
		recent = [tuple(s) for s in values["mru-sources"]]
		repeat, locksGuess = self._keyboard()
		# The shell's choice as keyboard.js _updateMruSources makes it at start and whenever IBus comes or
		# goes: the most recent entry still among the sources, but only when more than one entry is.
		# Its own activation then is not interactive, so mru-sources may keep removed entries in front.
		known = [s for s in recent if s in sources]
		active = known[0] if len(known) > 1 else sources[0] if sources else None
		if active is None:
			if self._fallback is None:
				self._fallback = systemSnapshot()
			return self._fallback._replace(repeat=repeat, locksGuess=locksGuess)
		kind, ident = active
		inputMethod = kind != "xkb"
		if inputMethod:
			# An input method's engine names its own layout through IBus; the first layout among the
			# sources is the likeliest.
			ident = next((i for k, i in sources if k == "xkb"), DEFAULT_LAYOUT)
		layout, _plus, variant = ident.partition("+")
		names = (
			DEFAULT_RULES,
			values.get("xkb-model") or DEFAULT_MODEL,
			layout or DEFAULT_LAYOUT,
			variant,
			",".join(values["xkb-options"]),
		)
		return LayoutSnapshot(
			names,
			repeat=repeat,
			locksGuess=locksGuess,
			source="gnome",
			inputMethod=inputMethod,
		)

	def _keyboard(self) -> tuple[tuple[bool, float, float] | None, int | None]:
		values = self._values
		if "repeat" not in values:
			return None, None
		repeat = (
			bool(values["repeat"]),
			values.get("delay", 500) / 1000,
			values.get("repeat-interval", 30) / 1000,
		)
		locksGuess = None
		if values.get("remember-numlock-state"):
			locksGuess = LOCK_NUM if values.get("numlock-state") else 0
		return repeat, locksGuess

	def _shut(self) -> None:
		for settings, handler in self._handlers:
			settings.disconnect(handler)
		self._handlers.clear()


def _configFiles(name: str) -> list[str]:
	"""Where KConfig finds a file, the one read last winning."""
	home = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
	dirs = [d for d in (os.environ.get("XDG_CONFIG_DIRS") or "/etc/xdg").split(":") if d]
	return [os.path.join(d, name) for d in reversed(dirs)] + [os.path.join(home, name)]


_KCONFIG_ESCAPES = {"s": " ", "t": "\t", "n": "\n", "r": "\r", "\\": "\\"}


def _unescape(value: str) -> str:
	out = []
	characters = iter(value)
	for character in characters:
		if character == "\\":
			following = next(characters, "")
			out.append(_KCONFIG_ESCAPES.get(following, "\\" + following))
		else:
			out.append(character)
	return "".join(out)


def readKConfigGroup(name: str, group: str) -> dict[str, str]:
	"""A group of a KDE configuration file, as KConfig cascades it from the system's to the user's."""
	values: dict[str, str] = {}
	for path in _configFiles(name):
		try:
			with open(path, encoding="utf-8", errors="replace") as f:
				lines = f.read().splitlines()
		except OSError:
			continue
		current = None
		for line in lines:
			line = line.strip()
			if not line or line.startswith("#"):
				continue
			if line.startswith("["):
				end = line.find("]")
				current = line[1:end] if end > 0 else None
				continue
			if current != group or "=" not in line:
				continue
			key, _equals, value = line.partition("=")
			key = key.strip()
			if "[" in key:
				if "[$" not in key:
					# A translation.
					continue
				key = key[: key.index("[")]
			values[key] = _unescape(value.strip())
	return values


def _number(text: str | None, default: float) -> float:
	try:
		value = float(text) if text is not None else default
	except ValueError:
		return default
	return value if math.isfinite(value) else default


def _kconfigTrue(value: str | None) -> bool:
	"""A bool as KConfig's readEntry reads one: anything present but a negative word."""
	return value is not None and value.lower() not in ("false", "no", "off", "0")


def kdeNames(layout: dict[str, str]) -> tuple[str, str, str, str, str]:
	"""The names KWin compiles from kxkbrc's Layout group, with the XKB_DEFAULT variables filling
	what it leaves empty, as KWin's applyEnvironmentRules does. KWin reads no Use key, and takes
	Options only with ResetOldOptions."""
	env = os.environ
	layouts = layout.get("LayoutList")
	variants = layout.get("VariantList", "")
	if not layouts:
		layouts = env.get("XKB_DEFAULT_LAYOUT")
		# KWin takes the variant with the layout, and xkbcommon drops a variant that has no layout.
		variants = env.get("XKB_DEFAULT_VARIANT", "") if layouts else ""
	if _kconfigTrue(layout.get("ResetOldOptions")):
		options = layout.get("Options", "")
	else:
		options = env.get("XKB_DEFAULT_OPTIONS", "")
	return (
		env.get("XKB_DEFAULT_RULES") or DEFAULT_RULES,
		layout.get("Model") or KDE_DEFAULT_MODEL,
		layouts or DEFAULT_LAYOUT,
		variants,
		options,
	)


def kdeKeyboard(keyboard: dict[str, str]) -> tuple[tuple[bool, float, float], int | None]:
	"""The repeat and the Num Lock guess from kcminputrc's Keyboard group, as KWin reads them."""
	delay = int(_number(keyboard.get("RepeatDelay"), 600))
	rate = math.ceil(_number(keyboard.get("RepeatRate"), 25.0))
	enabled = keyboard.get("KeyRepeat", "repeat") == "repeat" and rate > 0
	repeat = (enabled, delay / 1000, 1 / rate if rate > 0 else 0.04)
	numLock = keyboard.get("NumLock")
	locksGuess = None
	if numLock == str(KDE_NUM_LOCK_ON):
		locksGuess = LOCK_NUM
	elif numLock == str(KDE_NUM_LOCK_OFF):
		locksGuess = 0
	return repeat, locksGuess


class PlasmaLayoutFeed(_ThreadedFeed):
	"""kxkbrc's layouts and kcminputrc's keyboard, with the group KWin says over the session bus."""

	def __init__(self) -> None:
		super().__init__()
		self._connection = None
		self._subscriptions: list[int] = []
		self._group = 0
		#: What kxkbrc and kcminputrc said when they were last read, in group 0.
		self._files: LayoutSnapshot | None = None

	def _open(self) -> LayoutSnapshot:
		from gi.repository import Gio, GLib

		try:
			self._connection = _connect(Gio.BusType.SESSION)
		except GLib.Error as e:
			log.debug(f"Keyboard layout: no session bus to ask KWin on: {e.message}")
			self.follows = False
			return self._read()
		for sender, path, interface, member, argument, handler in (
			(KDE_SERVICE, KDE_PATH, KDE_INTERFACE, "layoutChanged", None, self._onLayoutChanged),
			(KDE_SERVICE, KDE_PATH, KDE_INTERFACE, "layoutListChanged", None, self._onReconfigured),
			(_DBUS, _DBUS_PATH, _DBUS, "NameOwnerChanged", KDE_SERVICE, self._onReconfigured),
		):
			self._subscriptions.append(
				self._connection.signal_subscribe(
					sender, interface, member, path, argument, Gio.DBusSignalFlags.NONE, handler
				),
			)
		return self._read()

	def _askGroup(self) -> int:
		from gi.repository import Gio, GLib

		if self._connection is None:
			return 0
		try:
			(group,) = self._connection.call_sync(
				KDE_SERVICE,
				KDE_PATH,
				KDE_INTERFACE,
				"getLayout",
				None,
				GLib.VariantType.new("(u)"),
				Gio.DBusCallFlags.NO_AUTO_START,
				BUS_TIMEOUT_MS,
				None,
			).unpack()
		except GLib.Error as e:
			log.debug(f"Keyboard layout: KWin did not say which layout is in use: {e.message}")
			return 0
		return group

	def _snapshot(self) -> LayoutSnapshot:
		files = self._files
		count = len(files.names[2].split(","))
		return files._replace(group=self._group if 0 <= self._group < count else 0)

	def _read(self) -> LayoutSnapshot:
		names = kdeNames(readKConfigGroup("kxkbrc", "Layout"))
		repeat, locksGuess = kdeKeyboard(readKConfigGroup("kcminputrc", "Keyboard"))
		self._files = LayoutSnapshot(names, repeat=repeat, locksGuess=locksGuess, source="kde")
		self._group = self._askGroup()
		return self._snapshot()

	def _onLayoutChanged(self, connection, sender, path, interface, signal, parameters) -> None:
		# As few calls into GLib as may be: with NVDA's main thread busy, each waits a switch interval
		# for the interpreter on its way back.
		self._group = parameters.get_child_value(0).get_uint32()
		self._changed(self._snapshot)

	def _onReconfigured(self, *args) -> None:
		self._changed(self._read)

	def _shut(self) -> None:
		connection = self._connection
		if connection is None:
			return
		self._connection = None
		for subscription in self._subscriptions:
			connection.signal_unsubscribe(subscription)
		self._subscriptions.clear()
		_close(connection)


class _WithFallback:
	"""Another module's feed, with the desktop's own in its place should it fail to start."""

	def __init__(self, name: str, feed: LayoutFeed, fallback: Callable[[], LayoutFeed]) -> None:
		self._name = name
		self._feed = feed
		self._fallback = fallback

	def start(self, onChange: Callable[[LayoutSnapshot], None]) -> LayoutSnapshot:
		try:
			return self._feed.start(onChange)
		except Exception:  # noqa: BLE001
			log.debugWarning(f"Keyboard layout: the {self._name} feed did not start", exc_info=True)
		try:
			self._feed.stop()
		except Exception:  # noqa: BLE001
			log.debugWarning(f"Keyboard layout: stopping the {self._name} feed failed", exc_info=True)
		self._feed = self._fallback()
		return self._feed.start(onChange)

	def stop(self) -> None:
		self._feed.stop()

	def notePassed(self, keycode: int, isUp: bool) -> None:
		notePassed = getattr(self._feed, "notePassed", None)
		if notePassed is not None:
			notePassed(keycode, isUp)


def desktopFeed(currentDesktop: str | None) -> LayoutFeed:
	"""This module's own feed for the desktops XDG_CURRENT_DESKTOP names."""
	desktops = {name.strip().upper() for name in (currentDesktop or "").split(":")}
	if "GNOME" in desktops:
		return GnomeLayoutFeed()
	if "KDE" in desktops:
		return PlasmaLayoutFeed()
	return LocaledLayoutFeed()


def create(currentDesktop: str | None) -> LayoutFeed:
	"""The feed for this session: the first of OPTIONAL_MODULES that offers one, else the desktop's.

	:param currentDesktop: XDG_CURRENT_DESKTOP as the caller has it.
	"""
	for name in OPTIONAL_MODULES:
		fullName = f"{__package__}.{name}"
		try:
			module = importlib.import_module(fullName)
		except ModuleNotFoundError as e:
			if e.name == fullName:
				log.debug(f"Keyboard layout: no {name} module")
			else:
				log.debugWarning(f"Keyboard layout: the {name} module could not be loaded", exc_info=True)
			continue
		except Exception:  # noqa: BLE001
			log.debugWarning(f"Keyboard layout: the {name} module could not be loaded", exc_info=True)
			continue
		try:
			feed = module.create(currentDesktop)
		except Exception:  # noqa: BLE001
			log.debugWarning(f"Keyboard layout: the {name} module gave no feed", exc_info=True)
			continue
		if feed is not None:
			log.debug(f"Keyboard layout: taken from the {name} module")
			return _WithFallback(name, feed, lambda: desktopFeed(currentDesktop))
	return desktopFeed(currentDesktop)
