"""Whether the login session is locked, and which processes draw the screen locker.

Two sources tell it, and both are only listened to: nothing here calls a method that locks, unlocks
or inhibits anything.

- logind, on the system bus: the session's LockedHint property, which a screen locker sets while it
  holds the screen, and the session's Lock and Unlock signals, which ask the locker to lock or unlock.
- The desktop's screen saver, on the session bus: the ActiveChanged signal and GetActive method of
  org.mate.ScreenSaver, org.gnome.ScreenSaver and org.freedesktop.ScreenSaver.

The latest change from either decides, so that a source which never reports the end of a lock, as
LockedHint does under a locker that does not set it, cannot keep the session locked after another
has reported the end. A Lock signal counts only while a screen saver is on the session bus: it is a
request, which nothing may carry out, and only a screen saver also reports when the lock ends. A
locker such as i3lock under xss-lock reports neither, and NVDA cannot tell its lock.

The screen saver that owns one of those names, and those of its children whose program is named as a
screen saver's, locker's or greeter's (mate-screensaver-dialog, kscreenlocker_greet), are the locker:
what they show is what the user sees over the locked session, and NVDA presents it.
"""

import atexit
import os
import sys
import threading

from logHandler import log

#: Each screen saver's bus name, which is also its interface, and its object's path.
SCREEN_SAVERS = (
	("org.mate.ScreenSaver", "/org/mate/ScreenSaver"),
	("org.gnome.ScreenSaver", "/org/gnome/ScreenSaver"),
	("org.freedesktop.ScreenSaver", "/org/freedesktop/ScreenSaver"),
)
LOGIND = "org.freedesktop.login1"
LOGIND_PATH = "/org/freedesktop/login1"
LOGIND_MANAGER = "org.freedesktop.login1.Manager"
LOGIND_SESSION = "org.freedesktop.login1.Session"
#: Words in the program name of a screen saver's child that draws its unlock prompt.
LOCKER_CHILD_WORDS = ("screensaver", "locker", "greet")
#: Milliseconds a bus has to answer.
BUS_TIMEOUT_MS = 2000
#: Seconds the thread has to end when NVDA exits.
STOP_TIMEOUT = 2.0

_DBUS = "org.freedesktop.DBus"
_DBUS_PATH = "/org/freedesktop/DBus"
_PROPERTIES = "org.freedesktop.DBus.Properties"


def _programName(processID: int) -> str:
	try:
		with open(f"/proc/{processID}/cmdline", "rb") as f:
			program = f.read().split(b"\0", 1)[0]
	except OSError:
		return ""
	return os.path.basename(program.decode(errors="replace"))


def _parentOf(processID: int) -> int | None:
	try:
		with open(f"/proc/{processID}/stat", "rb") as f:
			stat = f.read()
	except OSError:
		return None
	# The program's name, in parentheses, may itself hold spaces and parentheses.
	fields = stat[stat.rfind(b")") + 2 :].split()
	try:
		return int(fields[1])
	except (IndexError, ValueError):
		return None


class _Monitor:
	"""The bus connections and the state they report. The connections are made on the thread that
	calls connect, and every signal is handled on the monitor's own thread after start."""

	def __init__(self) -> None:
		from gi.repository import GLib

		self._context = GLib.MainContext.new()
		self._loop = GLib.MainLoop.new(self._context, False)
		self._connections = []
		#: The latest report of the lock, which the main thread reads.
		self.isLocked = False
		#: Whether logind knows this session.
		self.hasLogind = False
		#: Each screen saver's bus name that has an owner, and the owner's process ID.
		self._saverProcesses: dict[str, int] = {}
		#: The screen savers' process IDs, which the main thread reads.
		self.saverProcessIDs: frozenset[int] = frozenset()
		self._lockerCache: dict[int, bool] = {}
		self.thread = threading.Thread(target=self._run, name="lindaSessionLock", daemon=True)

	def connect(self) -> None:
		self._context.push_thread_default()
		try:
			self._connectSessionBus()
			self._connectLogind()
		finally:
			self._context.pop_thread_default()

	def _run(self) -> None:
		from gi.repository import GLib

		self._context.push_thread_default()
		try:
			self._loop.run()
		finally:
			for connection in self._connections:
				try:
					connection.close_sync(None)
				except GLib.Error:
					log.debugWarning("Session lock: closing a bus connection failed", exc_info=True)
			self._context.pop_thread_default()

	def stop(self) -> None:
		from gi.repository import GLib

		source = GLib.idle_source_new()
		source.set_callback(lambda *args: self._loop.quit() or GLib.SOURCE_REMOVE)
		source.attach(self._context)

	def _bus(self, busType):
		from gi.repository import Gio, GLib

		try:
			address = Gio.dbus_address_get_for_bus_sync(busType, None)
			connection = Gio.DBusConnection.new_for_address_sync(
				address,
				Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
				| Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
				None,
				None,
			)
		except GLib.Error as e:
			log.debug(f"Session lock: no {busType.value_nick} bus: {e}")
			return None
		self._connections.append(connection)
		return connection

	@staticmethod
	def _call(connection, name: str, path: str, interface: str, method: str, parameters, replyType: str):
		from gi.repository import GLib

		return connection.call_sync(
			name,
			path,
			interface,
			method,
			parameters,
			GLib.VariantType.new(replyType),
			0,
			BUS_TIMEOUT_MS,
			None,
		).unpack()

	def _connectSessionBus(self) -> None:
		from gi.repository import Gio, GLib

		connection = self._bus(Gio.BusType.SESSION)
		if connection is None:
			return
		for name, path in SCREEN_SAVERS:
			connection.signal_subscribe(
				name,
				name,
				"ActiveChanged",
				None,
				None,
				Gio.DBusSignalFlags.NONE,
				self._onActiveChanged,
			)
			connection.signal_subscribe(
				_DBUS,
				_DBUS,
				"NameOwnerChanged",
				_DBUS_PATH,
				name,
				Gio.DBusSignalFlags.NONE,
				self._onSaverOwnerChanged,
			)
			try:
				(owner,) = self._call(
					connection, _DBUS, _DBUS_PATH, _DBUS, "GetNameOwner", GLib.Variant("(s)", (name,)), "(s)"
				)
			except GLib.Error:
				continue
			self._saverAppeared(connection, name, owner)
			try:
				(active,) = self._call(connection, name, path, name, "GetActive", None, "(b)")
			except GLib.Error as e:
				log.debug(f"Session lock: {name} did not say whether it is active: {e}")
				continue
			if active:
				self._report(True, f"{name} is active")

	def _connectLogind(self) -> None:
		from gi.repository import Gio, GLib

		connection = self._bus(Gio.BusType.SYSTEM)
		if connection is None:
			return
		sessionID = os.environ.get("XDG_SESSION_ID")
		try:
			if sessionID:
				(sessionPath,) = self._call(
					connection,
					LOGIND,
					LOGIND_PATH,
					LOGIND_MANAGER,
					"GetSession",
					GLib.Variant("(s)", (sessionID,)),
					"(o)",
				)
			else:
				(sessionPath,) = self._call(
					connection,
					LOGIND,
					LOGIND_PATH,
					LOGIND_MANAGER,
					"GetSessionByPID",
					GLib.Variant("(u)", (os.getpid(),)),
					"(o)",
				)
		except GLib.Error as e:
			log.debug(f"Session lock: logind does not know this session: {e}")
			return
		for member, handler in (
			("Lock", self._onLogindLock),
			("Unlock", self._onLogindUnlock),
		):
			connection.signal_subscribe(
				LOGIND, LOGIND_SESSION, member, sessionPath, None, Gio.DBusSignalFlags.NONE, handler
			)
		connection.signal_subscribe(
			LOGIND,
			_PROPERTIES,
			"PropertiesChanged",
			sessionPath,
			LOGIND_SESSION,
			Gio.DBusSignalFlags.NONE,
			self._onLogindPropertiesChanged,
		)
		try:
			(lockedHint,) = self._call(
				connection,
				LOGIND,
				sessionPath,
				_PROPERTIES,
				"Get",
				GLib.Variant("(ss)", (LOGIND_SESSION, "LockedHint")),
				"(v)",
			)
		except GLib.Error as e:
			log.debug(f"Session lock: logind did not give the session's LockedHint: {e}")
			return
		self.hasLogind = True
		log.debug(f"Session lock: logind's session {sessionPath}")
		if lockedHint:
			self._report(True, "logind's LockedHint is set")

	def _saverAppeared(self, connection, name: str, owner: str) -> None:
		from gi.repository import GLib

		try:
			(processID,) = self._call(
				connection,
				_DBUS,
				_DBUS_PATH,
				_DBUS,
				"GetConnectionUnixProcessID",
				GLib.Variant("(s)", (owner,)),
				"(u)",
			)
		except GLib.Error as e:
			log.debug(f"Session lock: no process ID for {name}: {e}")
			processID = -1
		log.debug(f"Session lock: {name} is process {processID}")
		self._saverProcesses[name] = processID
		self._publishSavers()

	def _publishSavers(self) -> None:
		self.saverProcessIDs = frozenset(self._saverProcesses.values())
		self._lockerCache = {}

	def _onSaverOwnerChanged(self, connection, sender, path, interface, signal, parameters) -> None:
		name, _oldOwner, newOwner = parameters.unpack()
		if newOwner:
			self._saverAppeared(connection, name, newOwner)
			return
		if self._saverProcesses.pop(name, None) is not None:
			self._publishSavers()
			# What has gone can no longer cover the screen.
			self._report(False, f"{name} has left the bus")

	def _onActiveChanged(self, connection, sender, path, interface, signal, parameters) -> None:
		(active,) = parameters.unpack()
		self._report(bool(active), f"{interface} became {'active' if active else 'inactive'}")

	def _onLogindLock(self, *args) -> None:
		if not self._saverProcesses:
			log.debug("Session lock: logind asked for a lock, which no screen saver is here to carry out")
			return
		self._report(True, "logind asked for a lock")

	def _onLogindUnlock(self, *args) -> None:
		self._report(False, "logind asked for an unlock")

	def _onLogindPropertiesChanged(self, connection, sender, path, interface, signal, parameters) -> None:
		_interface, changed, _invalidated = parameters.unpack()
		if "LockedHint" in changed:
			self._report(bool(changed["LockedHint"]), f"logind's LockedHint became {changed['LockedHint']}")

	def _report(self, isLocked: bool, why: str) -> None:
		log.debug(f"Session lock: {'locked' if isLocked else 'unlocked'}, as {why}")
		if isLocked == self.isLocked:
			return
		self._lockerCache = {}
		self.isLocked = isLocked
		# Not imported here, which on this thread could deadlock against the main thread's imports.
		if (core := sys.modules.get("core")) is not None:
			core.requestPump()

	def isLockerProcess(self, processID: int) -> bool:
		cache = self._lockerCache
		if (known := cache.get(processID)) is not None:
			return known
		savers = self.saverProcessIDs
		isLocker = processID in savers or (
			_parentOf(processID) in savers
			and any(word in _programName(processID).lower() for word in LOCKER_CHILD_WORDS)
		)
		cache[processID] = isLocker
		return isLocker


_monitor: _Monitor | None = None


def start() -> bool:
	"""Start listening.

	:return: Whether any source can tell the lock now: logind knows this session or a screen saver is
		on the session bus. One that comes to the session bus later is listened to as well.
	"""
	global _monitor
	if _monitor is not None:
		return _monitor.hasLogind or bool(_monitor.saverProcessIDs)
	monitor = _Monitor()
	monitor.connect()
	monitor.thread.start()
	_monitor = monitor
	atexit.register(stop)
	return monitor.hasLogind or bool(monitor.saverProcessIDs)


def stop() -> None:
	global _monitor
	monitor = _monitor
	_monitor = None
	if monitor is None:
		return
	monitor.stop()
	monitor.thread.join(STOP_TIMEOUT)
	if monitor.thread.is_alive():
		log.debugWarning("Session lock: its thread did not end in time")


def isLocked() -> bool:
	"""Whether a source reported the session locked last. Cheap, as each core cycle asks it."""
	monitor = _monitor
	return monitor is not None and monitor.isLocked


def isLockerProcess(processID: int) -> bool:
	"""Whether the process draws the screen locker, so that what it shows is above the lock."""
	monitor = _monitor
	return monitor is not None and monitor.isLockerProcess(processID)
