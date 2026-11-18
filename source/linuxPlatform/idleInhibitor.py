"""Keeping the session from going idle while NVDA asks it to, as SetThreadExecutionState does on
Windows for say all.

NVDA asks with systemUtils.preventSystemIdle(persistent=True) and lets go with
resetThreadExecutionState. Each request is counted, so that two that overlap, say all and braille's
auto-scroll, keep the session awake until both have let go, and a release with nothing asked does
nothing. While any request stands Linda holds one idle inhibitor, from the first of these services
the session has:

- org.gnome.SessionManager's Inhibit with the idle flag, served by mate-session and gnome-session,
  whose presence, and with it MATE's and GNOME's screen savers and power managers, goes idle only
  while no idle inhibitor is held;
- org.freedesktop.ScreenSaver's Inhibit, served on KDE by its screen locker and power management, on
  Xfce by xfce4-screensaver and xfce4-power-manager, and on GNOME by a proxy for its session manager.

logind's idle lock is not among them: only logind's own IdleAction heeds it, and none of those
desktops blanks or locks the screen through that.

Either inhibitor belongs to the bus connection that took it, and the service drops it as that
connection closes, which the kernel does however Linda ends, killed or crashed. Linda also lets go
by hand when the last request ends and when it exits. The calls are made on a thread of Linda's own,
with a connection of its own, so that no request waits on a service slow to answer. When no service
holds one, the session may still go idle, which is warned of once.
"""

import threading

from logHandler import log

#: What the inhibitor is said to be for, as a session manager lists it.
APP_ID = "linda"
REASON = "Reading continuously"
#: org.gnome.SessionManager's flag for inhibiting the session from being marked idle.
SESSION_MANAGER_IDLE = 8
#: Milliseconds a service has to answer.
CALL_TIMEOUT_MS = 2000
#: Seconds the main thread waits at exit for the inhibitor to be let go.
STOP_TIMEOUT = 3.0

#: Each service as bus name, object path, interface, the inhibiting method and its arguments' type,
#: and the releasing method.
SERVICES = (
	(
		"org.gnome.SessionManager",
		"/org/gnome/SessionManager",
		"org.gnome.SessionManager",
		"Inhibit",
		"(susu)",
		(APP_ID, 0, REASON, SESSION_MANAGER_IDLE),
		"Uninhibit",
	),
	(
		"org.freedesktop.ScreenSaver",
		"/org/freedesktop/ScreenSaver",
		"org.freedesktop.ScreenSaver",
		"Inhibit",
		"(ss)",
		(APP_ID, REASON),
		"UnInhibit",
	),
)

_condition = threading.Condition()
#: How many requests stand.
_requests = 0
#: How many times the requests have gone from none to some, so that the thread tries each service once
#: for each time and not again while the requests last.
_generation = 0
_stopping = False
_thread: threading.Thread | None = None


def hold() -> None:
	"""Count a request to keep the session awake; the first makes Linda take an inhibitor."""
	global _requests, _generation, _thread
	with _condition:
		if _stopping:
			return
		_requests += 1
		if _requests > 1:
			return
		_generation += 1
		if _thread is None:
			_thread = threading.Thread(target=_run, name="lindaIdleInhibitor", daemon=True)
			_thread.start()
		_condition.notify()


def release() -> None:
	"""End a request; the last makes Linda let the inhibitor go."""
	global _requests
	with _condition:
		if _requests == 0:
			return
		_requests -= 1
		if _requests == 0:
			_condition.notify()


def stop() -> None:
	"""Let the inhibitor go and close the connection, as Linda exits."""
	global _stopping
	with _condition:
		_stopping = True
		thread = _thread
		_condition.notify()
	if thread is None:
		return
	thread.join(STOP_TIMEOUT)
	if thread.is_alive():
		log.debugWarning(
			"Idle inhibitor: the thread did not end in time; the inhibitor goes with the process"
		)


def _run() -> None:
	connection = None
	#: The index in SERVICES of the service holding Linda's inhibitor, and its cookie.
	held: tuple[int, int] | None = None
	tried = 0
	try:
		while True:
			with _condition:
				while not (
					_stopping
					or (_requests > 0 and held is None and tried != _generation)
					or (_requests == 0 and held is not None)
				):
					_condition.wait()
				stopping = _stopping
				wanted = _requests > 0 and not stopping
				generation = _generation
			if wanted and held is None:
				tried = generation
				if connection is None:
					connection = _connect()
				held = _inhibit(connection) if connection is not None else None
				if held is None:
					from linuxPlatform import session

					session._warnIdleNotPrevented()
			elif not wanted and held is not None:
				if not _uninhibit(connection, *held):
					# Closing the connection is what lets go of an inhibitor the service would not.
					_close(connection)
					connection = None
				held = None
			if stopping:
				return
	except Exception:
		log.exception("Idle inhibitor: the thread failed; any inhibitor goes with the bus connection")
	finally:
		if connection is not None:
			_close(connection)


def _close(connection) -> None:
	from gi.repository import GLib

	try:
		connection.close_sync(None)
	except GLib.Error:
		log.debugWarning("Idle inhibitor: closing the bus connection failed", exc_info=True)


def _connect():
	from gi.repository import Gio, GLib

	try:
		address = Gio.dbus_address_get_for_bus_sync(Gio.BusType.SESSION, None)
		return Gio.DBusConnection.new_for_address_sync(
			address,
			Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
			None,
			None,
		)
	except GLib.Error as e:
		log.debugWarning(f"Idle inhibitor: no session bus: {e.message}")
		return None


def _inhibit(connection) -> tuple[int, int] | None:
	from gi.repository import Gio, GLib

	for index, (name, path, interface, method, signature, arguments, _release) in enumerate(SERVICES):
		try:
			reply = connection.call_sync(
				name,
				path,
				interface,
				method,
				GLib.Variant(signature, arguments),
				GLib.VariantType.new("(u)"),
				Gio.DBusCallFlags.NO_AUTO_START,
				CALL_TIMEOUT_MS,
				None,
			)
		except GLib.Error as e:
			log.debug(f"Idle inhibitor: {name} did not inhibit: {e.message}")
			continue
		(cookie,) = reply.unpack()
		log.info(f"Idle inhibitor: the session is kept awake through {name}")
		return index, cookie
	return None


def _uninhibit(connection, index: int, cookie: int) -> bool:
	from gi.repository import Gio, GLib

	name, path, interface, _method, _signature, _arguments, release = SERVICES[index]
	try:
		connection.call_sync(
			name,
			path,
			interface,
			release,
			GLib.Variant("(u)", (cookie,)),
			None,
			Gio.DBusCallFlags.NO_AUTO_START,
			CALL_TIMEOUT_MS,
			None,
		)
	except GLib.Error as e:
		log.warning(f"Idle inhibitor: {name} did not let the inhibitor go: {e.message}")
		return False
	log.info(f"Idle inhibitor: the session may go idle again, through {name}")
	return True
