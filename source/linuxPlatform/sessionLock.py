"""Whether the login session is locked, and which processes draw the screen locker.

Everything here is only listened to: nothing calls a method that locks, unlocks or inhibits anything.

A wrong "locked" silences the screen reader, while a wrong "unlocked" reads out over a lock screen
what NVDA would have withheld, as Linda did before it knew of locks at all. So the session counts as
locked only on evidence that a locker of this session holds the screen, and every doubt, gap or
loss of a source counts as unlocked.

The witness is a screen saver on the session bus that owns org.mate.ScreenSaver,
org.gnome.ScreenSaver or org.freedesktop.ScreenSaver, answers GetActive, says it is active, and
belongs to this login session: its XDG_SESSION_ID, or failing that its DISPLAY, is this process's,
or, where its environment cannot be read, its control group is.
The session bus is the user's, shared by every session they have, so a saver of another session
drives nothing here. Being active is not enough, as no saver says whether it locked:
mate-screensaver 1.28 sends ActiveChanged(true) as it merely blanks the screen, and GNOME Shell as
it shows its curtain. One of these, seen during the saver's active spell, makes it a lock until the
saver says it is inactive, leaves the bus or the bus goes:

- The saver's unlock dialog is running: mate-screensaver starts mate-screensaver-dialog as its child
  only to ask for the password of a locked screen, and ends a blank-only spell at the first key
  instead. This is how a lock from the user's own shortcut or from idling is known, and only once
  a key has woken it.
- logind asked the session to lock (its Lock signal, which lightdm, loginctl lock-session and the lid
  send) within LOCK_REQUEST_WINDOW seconds of the saver's becoming active: mate-screensaver
  activates to carry out that request. The request alone counts for nothing, since a saver may
  refuse it (locking disabled, or another client holding a grab) and would then never report an end.
- logind's LockedHint became true while this module listened, during the saver's active spell or just
  before it: GNOME Shell, xfce4-screensaver and light-locker set it once they hold the lock, and
  clear it as they let go. A hint found set when listening begins, or after a reconnection, counts
  for nothing, as a locker that died while locked leaves it set; so does a hint with no active saver
  of this session, and mate-screensaver never sets it.

With no saver of this session that answers, as under i3lock and xss-lock, the session never counts
as locked. A logind Unlock signal ends the evidence there is, and a bus connection that closes takes
its evidence with it until it is made again.

Both buses are connected on this module's own thread, each attempt bounded by CONNECT_TIMEOUT
seconds, so that a bus which accepts and never answers holds up nothing but this thread.

The saver whose evidence makes the lock, and those of its children whose program is named as a
screen saver's, locker's or greeter's (mate-screensaver-dialog), are the locker: what they show is
what the user sees over the locked session, and NVDA presents it.
"""

import atexit
import os
import sys
import threading
import time

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
#: Programs a screen saver runs as its child only to ask for the password of a locked screen.
UNLOCK_DIALOGS = ("mate-screensaver-dialog",)
#: Words in the program name of a locker's child that draws its unlock prompt.
LOCKER_CHILD_WORDS = ("screensaver", "locker", "greet")
#: Seconds between logind's Lock signal and the saver's activation for the one to carry out the
#: other; the two come on different buses, in either order.
LOCK_REQUEST_WINDOW = 10.0
#: Milliseconds between looks for an unlock dialog while a saver is active and not known to lock.
#: Without CONFIG_PROC_CHILDREN each look reads every process's parent, a few milliseconds' work.
DIALOG_POLL_MS = 500
#: Milliseconds after the end of a lock at which the core is asked to look at the focus again.
REFOCUS_DELAY_MS = 400
#: Seconds a bus has to accept a connection.
CONNECT_TIMEOUT = 5
#: Milliseconds a bus has to answer a call.
BUS_TIMEOUT_MS = 2000
#: Seconds before each further attempt to connect to a bus that failed or went away.
RECONNECT_DELAYS = (2, 5, 15, 30, 60)
#: Attempts to connect to a bus that has never answered before this module gives up on it.
MAX_FAILED_ATTEMPTS = 8
#: Attempts that ran out of time before this module gives up on a bus: each leaves a thread of GIO's
#: waiting on a bus that does not answer.
MAX_TIMED_OUT_ATTEMPTS = 2
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


def _childrenOf(processID: int) -> list[int]:
	children = []
	try:
		threads = os.listdir(f"/proc/{processID}/task")
	except OSError:
		return children
	for thread in threads:
		try:
			with open(f"/proc/{processID}/task/{thread}/children", "rb") as f:
				children.extend(int(c) for c in f.read().split())
		except OSError:
			return _childrenByParent(processID)
		except ValueError:
			continue
	return children


#: Each process's parent as _childrenByParent last found it.
_parents: dict[int, int | None] = {}


def _childrenByParent(processID: int) -> list[int]:
	"""A kernel without CONFIG_PROC_CHILDREN has every process asked for its parent instead, which for
	some 800 processes takes 20 ms; so only those new since the last look are asked."""
	present = {int(entry) for entry in os.listdir("/proc") if entry.isdigit()}
	for gone in _parents.keys() - present:
		del _parents[gone]
	for new in present - _parents.keys():
		_parents[new] = _parentOf(new)
	return [child for child, parent in _parents.items() if parent == processID]


def _environmentOf(processID: int) -> dict[str, str] | None:
	try:
		with open(f"/proc/{processID}/environ", "rb") as f:
			raw = f.read()
	except OSError:
		return None
	environment = {}
	for entry in raw.split(b"\0"):
		name, sep, value = entry.partition(b"=")
		if sep and name in (b"XDG_SESSION_ID", b"DISPLAY"):
			environment[name.decode()] = value.decode(errors="replace")
	return environment


def _cgroupOf(processID: int) -> str | None:
	try:
		with open(f"/proc/{processID}/cgroup", encoding="utf-8", errors="replace") as f:
			return f.read()
	except OSError:
		return None


def _isOfThisSession(processID: int) -> bool:
	if processID <= 0:
		return False
	theirs = _environmentOf(processID)
	if theirs is None:
		# Reading another's environment takes the right to trace it, which a namespace can deny; a
		# control group anyone may read, and a login session's processes share logind's scope for it.
		cgroup = _cgroupOf(processID)
		if cgroup is not None and cgroup == _cgroupOf(os.getpid()):
			return True
		log.debug(f"Session lock: cannot read the environment of process {processID}, in {cgroup}")
		return False
	for name in ("XDG_SESSION_ID", "DISPLAY"):
		mine = os.environ.get(name)
		if mine and theirs.get(name):
			if theirs[name] != mine:
				log.debug(f"Session lock: process {processID} has {name} {theirs[name]}, this one {mine}")
			return theirs[name] == mine
	log.debug(f"Session lock: neither process {processID} nor this one says which session it is in")
	return False


class _Saver:
	"""What one screen saver bus name has said."""

	def __init__(self, name: str, processID: int, isOfThisSession: bool) -> None:
		self.name = name
		self.processID = processID
		self.isOfThisSession = isOfThisSession
		#: Whether it says it is active; None while it has not answered GetActive.
		self.active: bool | None = None
		#: When it was seen to become active; None when it already was as listening began.
		self.activeSince: float | None = None
		#: Why its active spell is a lock, once there is evidence.
		self.evidence: str | None = None
		self.evidenceFromHint = False


class _Monitor:
	"""The bus connections and the state they report. All of it lives on the monitor's own thread;
	the main thread reads only isLocked and lockerProcessIDs, each replaced whole."""

	def __init__(self) -> None:
		from gi.repository import GLib

		self._context = GLib.MainContext.new()
		self._loop = GLib.MainLoop.new(self._context, False)
		#: Whether the session counts as locked, which the main thread reads.
		self.isLocked = False
		#: The processes whose evidence makes the lock, which the main thread reads.
		self.lockerProcessIDs: frozenset[int] = frozenset()
		self._lockerCache: dict[int, bool] = {}
		self._savers: dict[str, _Saver] = {}
		self._connections: dict[str, object] = {}
		self._attempts: dict[str, object] = {}
		self._failures: dict[str, int] = {}
		self._timeouts: dict[str, int] = {}
		self._everConnected: set[str] = set()
		self._sessionPath: str | None = None
		self._hintTrueSince: float | None = None
		self._lockRequestedAt: float | None = None
		self._pollSource = None
		self._firstAttemptsLeft = 2
		#: Set once the first attempt at each bus has ended, either way.
		self.firstAttemptsEnded = threading.Event()
		self.thread = threading.Thread(target=self._run, name="lindaSessionLock", daemon=True)

	def _run(self) -> None:
		from gi.repository import Gio

		self._context.push_thread_default()
		try:
			self._connect(Gio.BusType.SESSION)
			self._connect(Gio.BusType.SYSTEM)
			self._loop.run()
		except Exception:
			log.exception("Session lock: the monitor failed, so the session counts as unlocked")
			self._set(False, "the monitor failed")
		finally:
			for connection in list(self._connections.values()):
				try:
					connection.close_sync(None)
				except Exception:  # noqa: BLE001
					log.debugWarning("Session lock: closing a bus connection failed", exc_info=True)
			self._context.pop_thread_default()

	def stop(self) -> None:
		from gi.repository import GLib

		source = GLib.idle_source_new()
		source.set_callback(lambda *args: self._loop.quit() or GLib.SOURCE_REMOVE)
		source.attach(self._context)

	def _later(self, milliseconds: int, callback, *args):
		from gi.repository import GLib

		source = GLib.timeout_source_new(milliseconds)
		source.set_callback(lambda *ignored: self._guarded(callback, *args) or GLib.SOURCE_REMOVE)
		source.attach(self._context)
		return source

	def _guarded(self, callback, *args):
		"""Run a handler so that a failure in it counts as unlocked rather than ending the thread."""
		try:
			return callback(*args)
		except Exception:
			log.exception("Session lock: a handler failed, so the session counts as unlocked")
			for saver in self._savers.values():
				saver.evidence = None
			self._set(False, "a handler failed")
			return None

	# Connections.

	def _connect(self, busType) -> None:
		from gi.repository import Gio, GLib

		bus = busType.value_nick
		if bus in self._attempts or bus in self._connections:
			return
		try:
			address = Gio.dbus_address_get_for_bus_sync(busType, None)
		except GLib.Error as e:
			log.debug(f"Session lock: no {bus} bus: {e}")
			self._attemptEnded(bus)
			return
		attempt = Gio.Cancellable()
		self._attempts[bus] = attempt
		self._later(CONNECT_TIMEOUT * 1000, self._connectTimedOut, busType, attempt)
		Gio.DBusConnection.new_for_address(
			address,
			Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
			None,
			attempt,
			lambda source, result: self._guarded(self._connected, busType, attempt, result),
		)

	def _connectTimedOut(self, busType, attempt) -> None:
		bus = busType.value_nick
		if self._attempts.get(bus) is not attempt:
			return
		log.debugWarning(f"Session lock: the {bus} bus did not accept a connection in {CONNECT_TIMEOUT} s")
		del self._attempts[bus]
		attempt.cancel()
		self._attemptEnded(bus)
		self._timeouts[bus] = self._timeouts.get(bus, 0) + 1
		if self._timeouts[bus] >= MAX_TIMED_OUT_ATTEMPTS:
			log.debugWarning(f"Session lock: giving up on the {bus} bus")
			return
		self._retryLater(busType)

	def _connected(self, busType, attempt, result) -> None:
		from gi.repository import Gio, GLib

		bus = busType.value_nick
		try:
			connection = Gio.DBusConnection.new_for_address_finish(result)
		except GLib.Error as e:
			if self._attempts.get(bus) is attempt:
				del self._attempts[bus]
				log.debug(f"Session lock: cannot connect to the {bus} bus: {e}")
				self._attemptEnded(bus)
				self._retryLater(busType)
			return
		if self._attempts.get(bus) is not attempt:
			# It came after its time was up, and another attempt may be under way.
			connection.close(None, None, None)
			return
		del self._attempts[bus]
		self._connections[bus] = connection
		self._failures[bus] = 0
		self._everConnected.add(bus)
		connection.connect(
			"closed",
			lambda connection, peerVanished, error: self._guarded(self._closed, busType, connection),
		)
		try:
			if busType == Gio.BusType.SESSION:
				self._watchSavers(connection)
			else:
				self._watchLogind(connection)
		finally:
			self._attemptEnded(bus)

	def _closed(self, busType, connection) -> None:
		bus = busType.value_nick
		if self._connections.get(bus) is not connection:
			return
		del self._connections[bus]
		if bus == "session":
			self._savers.clear()
		else:
			self._forgetLogind()
		self._decide(f"the {bus} bus went away")
		self._retryLater(busType)

	def _retryLater(self, busType) -> None:
		bus = busType.value_nick
		failures = self._failures.get(bus, 0)
		if bus not in self._everConnected and failures >= MAX_FAILED_ATTEMPTS:
			log.debug(f"Session lock: giving up on the {bus} bus")
			return
		self._failures[bus] = failures + 1
		delay = RECONNECT_DELAYS[min(failures, len(RECONNECT_DELAYS) - 1)]
		self._later(delay * 1000, self._connect, busType)

	def _attemptEnded(self, bus: str) -> None:
		if self.firstAttemptsEnded.is_set():
			return
		self._firstAttemptsLeft -= 1
		if self._firstAttemptsLeft > 0:
			return
		self.firstAttemptsEnded.set()
		if not any(s.isOfThisSession and s.active is not None for s in self._savers.values()):
			log.warning(
				"NVDA cannot tell whether this Linux session is locked,"
				" so it does not withhold content below a screen locker.",
			)

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

	# The screen savers, on the session bus.

	def _watchSavers(self, connection) -> None:
		from gi.repository import Gio, GLib

		for name, path in SCREEN_SAVERS:
			connection.signal_subscribe(
				name,
				name,
				"ActiveChanged",
				None,
				None,
				Gio.DBusSignalFlags.NONE,
				lambda c, sender, path, interface, signal, parameters: self._guarded(
					self._onActiveChanged,
					interface,
					parameters,
				),
			)
			connection.signal_subscribe(
				_DBUS,
				_DBUS,
				"NameOwnerChanged",
				_DBUS_PATH,
				name,
				Gio.DBusSignalFlags.NONE,
				lambda c, sender, path, interface, signal, parameters: self._guarded(
					self._onSaverOwnerChanged,
					c,
					parameters,
				),
			)
		for name, path in SCREEN_SAVERS:
			try:
				(owner,) = self._call(
					connection, _DBUS, _DBUS_PATH, _DBUS, "GetNameOwner", GLib.Variant("(s)", (name,)), "(s)"
				)
			except GLib.Error:
				continue
			self._saverAppeared(connection, name, owner)
		self._decide("the screen savers answered")

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
			processID = 0
		saver = _Saver(name, processID, _isOfThisSession(processID))
		self._savers[name] = saver
		path = dict(SCREEN_SAVERS)[name]
		try:
			(active,) = self._call(connection, name, path, name, "GetActive", None, "(b)")
		except GLib.Error as e:
			log.debug(f"Session lock: {name} does not say whether it is active: {e}")
		else:
			saver.active = bool(active)
		log.debug(
			f"Session lock: {name} is process {processID}, "
			f"{'of this session' if saver.isOfThisSession else 'not known to be of this session'}, "
			f"active {saver.active}",
		)

	def _onSaverOwnerChanged(self, connection, parameters) -> None:
		name, _oldOwner, newOwner = parameters.unpack()
		self._savers.pop(name, None)
		if newOwner:
			self._saverAppeared(connection, name, newOwner)
		self._decide(f"{name} {'changed hands' if newOwner else 'left the bus'}")

	def _onActiveChanged(self, interface: str, parameters) -> None:
		(active,) = parameters.unpack()
		saver = self._savers.get(interface)
		if saver is None:
			return
		active = bool(active)
		if active == saver.active:
			return
		saver.activeSince = time.monotonic() if active else None
		saver.active = active
		if not active and saver.isOfThisSession:
			# A lock request the saver has carried out and ended is spent.
			self._lockRequestedAt = None
		saver.evidence = None
		saver.evidenceFromHint = False
		self._decide(f"{interface} became {'active' if active else 'inactive'}")

	# logind, on the system bus.

	def _watchLogind(self, connection) -> None:
		from gi.repository import Gio

		connection.signal_subscribe(
			_DBUS,
			_DBUS,
			"NameOwnerChanged",
			_DBUS_PATH,
			LOGIND,
			Gio.DBusSignalFlags.NONE,
			lambda c, sender, path, interface, signal, parameters: self._guarded(
				self._onLogindOwnerChanged,
				c,
				parameters,
			),
		)
		self._findSession(connection)

	def _findSession(self, connection) -> None:
		from gi.repository import Gio, GLib

		sessionID = os.environ.get("XDG_SESSION_ID")
		try:
			if sessionID:
				method, argument = "GetSession", GLib.Variant("(s)", (sessionID,))
			else:
				method, argument = "GetSessionByPID", GLib.Variant("(u)", (os.getpid(),))
			(sessionPath,) = self._call(
				connection, LOGIND, LOGIND_PATH, LOGIND_MANAGER, method, argument, "(o)"
			)
		except GLib.Error as e:
			log.debug(f"Session lock: logind does not know this session: {e}")
			return
		self._sessionPath = sessionPath
		for member, handler in (
			("Lock", self._onLogindLock),
			("Unlock", self._onLogindUnlock),
		):
			connection.signal_subscribe(
				LOGIND,
				LOGIND_SESSION,
				member,
				sessionPath,
				None,
				Gio.DBusSignalFlags.NONE,
				lambda c, sender, path, interface, signal, parameters, handler=handler: self._guarded(
					handler
				),
			)
		connection.signal_subscribe(
			LOGIND,
			_PROPERTIES,
			"PropertiesChanged",
			sessionPath,
			LOGIND_SESSION,
			Gio.DBusSignalFlags.NONE,
			lambda c, sender, path, interface, signal, parameters: self._guarded(
				self._onLogindPropertiesChanged,
				parameters,
			),
		)
		log.debug(f"Session lock: logind's session {sessionPath}")

	def _forgetLogind(self) -> None:
		self._sessionPath = None
		self._hintTrueSince = None
		self._lockRequestedAt = None
		for saver in self._savers.values():
			if saver.evidenceFromHint:
				saver.evidence = None
				saver.evidenceFromHint = False

	def _onLogindOwnerChanged(self, connection, parameters) -> None:
		_name, _oldOwner, newOwner = parameters.unpack()
		if self._sessionPath is not None:
			# Its signals are already subscribed to by path and survive a new owner; what a new logind
			# says of the hint at first is no more evidence than what it said at start.
			self._hintTrueSince = None
			self._lockRequestedAt = None
			for saver in self._savers.values():
				if saver.evidenceFromHint:
					saver.evidence = None
					saver.evidenceFromHint = False
		elif newOwner:
			self._findSession(connection)
		self._decide(f"logind {'came back' if newOwner else 'left the bus'}")

	def _onLogindLock(self) -> None:
		self._lockRequestedAt = time.monotonic()
		self._decide("logind asked for a lock")

	def _onLogindUnlock(self) -> None:
		self._lockRequestedAt = None
		self._hintTrueSince = None
		for saver in self._savers.values():
			saver.evidence = None
			saver.evidenceFromHint = False
		self._decide("logind asked for an unlock")

	def _onLogindPropertiesChanged(self, parameters) -> None:
		_interface, changed, _invalidated = parameters.unpack()
		if "LockedHint" not in changed:
			return
		if changed["LockedHint"]:
			if self._hintTrueSince is None:
				self._hintTrueSince = time.monotonic()
		else:
			self._hintTrueSince = None
			for saver in self._savers.values():
				if saver.evidenceFromHint:
					saver.evidence = None
					saver.evidenceFromHint = False
		self._decide(f"logind's LockedHint became {bool(changed['LockedHint'])}")

	# The decision.

	def _evidenceFor(self, saver: _Saver) -> str | None:
		if saver.processID > 0:
			for child in _childrenOf(saver.processID):
				program = _programName(child)
				if any(dialog in program for dialog in UNLOCK_DIALOGS):
					return f"{saver.name}'s unlock dialog, {program}, is running"
		since = saver.activeSince
		requested = self._lockRequestedAt
		if since is not None and requested is not None and abs(requested - since) <= LOCK_REQUEST_WINDOW:
			return f"{saver.name} became active as logind asked for a lock"
		hint = self._hintTrueSince
		if hint is not None and (since is None or hint >= since - LOCK_REQUEST_WINDOW):
			saver.evidenceFromHint = True
			return f"logind's LockedHint was set while {saver.name} was active"
		return None

	def _decide(self, why: str) -> None:
		lockers = []
		waiting = False
		for saver in self._savers.values():
			if not (saver.isOfThisSession and saver.active):
				continue
			if saver.evidence is None:
				saver.evidence = self._evidenceFor(saver)
			if saver.evidence is None:
				waiting = True
			else:
				lockers.append(saver)
		self._pollForDialog(waiting)
		lockerProcessIDs = frozenset(saver.processID for saver in lockers)
		if lockerProcessIDs != self.lockerProcessIDs:
			self.lockerProcessIDs = lockerProcessIDs
			self._lockerCache = {}
		self._set(bool(lockers), "; ".join(saver.evidence for saver in lockers) or why)

	def _pollForDialog(self, wanted: bool) -> None:
		if wanted and self._pollSource is None:
			self._pollSource = self._later(DIALOG_POLL_MS, self._poll)
		elif not wanted and self._pollSource is not None:
			self._pollSource.destroy()
			self._pollSource = None

	def _poll(self) -> None:
		self._pollSource = None
		self._decide("still active")

	def _set(self, isLocked: bool, why: str) -> None:
		if isLocked == self.isLocked:
			return
		log.info(f"Session lock: {'locked' if isLocked else 'unlocked'}, as {why}")
		self._lockerCache = {}
		self.isLocked = isLocked
		self._requestPump()
		if not isLocked:
			# Whatever took the focus while it was withheld is looked at once the lock has gone.
			self._later(REFOCUS_DELAY_MS, self._requestPump)

	@staticmethod
	def _requestPump() -> None:
		# Not imported here, which on this thread could deadlock against the main thread's imports.
		if (core := sys.modules.get("core")) is not None:
			core.requestPump()

	def isLockerProcess(self, processID: int) -> bool:
		cache = self._lockerCache
		if (known := cache.get(processID)) is not None:
			return known
		lockers = self.lockerProcessIDs
		isLocker = processID in lockers or (
			_parentOf(processID) in lockers
			and any(word in _programName(processID).lower() for word in LOCKER_CHILD_WORDS)
		)
		cache[processID] = isLocker
		return isLocker


_monitor: _Monitor | None = None


def start() -> None:
	"""Start listening on a thread of its own. Nothing here waits for a bus or raises: until a bus
	answers, and whenever none does, the session counts as unlocked."""
	global _monitor
	if _monitor is not None:
		return
	try:
		monitor = _Monitor()
		monitor.thread.start()
	except Exception:
		log.exception("Session lock: cannot listen, so the session counts as unlocked")
		return
	_monitor = monitor
	atexit.register(stop)


def stop() -> None:
	global _monitor
	monitor = _monitor
	_monitor = None
	if monitor is None:
		return
	try:
		monitor.stop()
	except Exception:  # noqa: BLE001
		log.debugWarning("Session lock: cannot ask its thread to end", exc_info=True)
		return
	monitor.thread.join(STOP_TIMEOUT)
	if monitor.thread.is_alive():
		log.debugWarning("Session lock: its thread did not end in time")


def isLocked() -> bool:
	"""Whether the session counts as locked. Cheap, as each core cycle asks it."""
	monitor = _monitor
	return monitor is not None and monitor.isLocked


def isLockerProcess(processID: int) -> bool:
	"""Whether the process draws the screen locker, so that what it shows is above the lock."""
	monitor = _monitor
	return monitor is not None and monitor.isLocked and monitor.isLockerProcess(processID)


def _waitForFirstAttempts(timeout: float) -> bool:
	"""For tests: wait until the first attempt at each bus has ended."""
	monitor = _monitor
	return monitor is not None and monitor.firstAttemptsEnded.wait(timeout)
