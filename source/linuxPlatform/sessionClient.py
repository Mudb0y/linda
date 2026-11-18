"""NVDA as a client of the desktop's session manager, when that is what started it.

A session manager starting a program from an autostart entry gives it a startup ID in
DESKTOP_AUTOSTART_ID. In an early phase, such as the Initialization phase MATE starts screen readers
in, it waits for every program it started to register with that ID before it starts the next phase,
the window manager and the panel among them, or gives up after 30 seconds. MATE's session manager
serves org.gnome.SessionManager on the session bus, as GNOME's does, and a program registers there.

A client is asked before the session ends, with QueryEndSession, and told when it ends, with
EndSession, and has to answer each with EndSessionResponse, which the logout waits for; CancelEndSession
says the logout asked about will not happen, and Stop that the client is to exit now. NVDA registers
once it has started, and answers on a thread of its own with a connection of its own, as
linuxPlatform.speakService serves its name, so that no answer waits for the main thread. At the end
of the session it exits as it does on SIGTERM, which saves the configuration first when NVDA saves it
on exit, and it answers once the configuration is saved, or after SAVE_TIMEOUT if the main thread is
held up.

Any failure to register or to answer is logged once and leaves NVDA running unregistered, as a program
the session manager started and knows nothing more of, which is how NVDA ran before it registered.

The startup ID is taken out of the environment as NVDA starts, as the specification of the variable
asks, so that no program NVDA starts, its own copy on a restart among them, uses it.
"""

import os
import signal
import threading

from logHandler import log

STARTUP_ID_VARIABLE = "DESKTOP_AUTOSTART_ID"
BUS_NAME = "org.gnome.SessionManager"
OBJECT_PATH = "/org/gnome/SessionManager"
INTERFACE = "org.gnome.SessionManager"
CLIENT_INTERFACE = "org.gnome.SessionManager.ClientPrivate"
#: The application ID NVDA registers with, which the session manager falls back on to find the
#: autostart entry it started when the startup ID is not one it gave.
APP_ID = "linda.desktop"
#: Milliseconds the session manager has to answer the registration; until then NVDA is only unregistered.
REGISTER_TIMEOUT_MS = 10_000
#: Milliseconds the session manager has to take an answer, and the unregistration at exit.
CALL_TIMEOUT_MS = 2000
#: Seconds the answer to EndSession waits for the configuration to be saved.
SAVE_TIMEOUT = 3.0
#: Seconds the main thread waits for the client's thread to end.
STOP_TIMEOUT = 3.0

#: The startup ID NVDA was started with, once takeStartupId has taken it out of the environment.
_startupId = ""


def takeStartupId() -> None:
	"""Take the session manager's startup ID out of the environment, before NVDA starts any program."""
	global _startupId
	_startupId = os.environ.pop(STARTUP_ID_VARIABLE, "")


def _exitAsOnSignal() -> None:
	# The quit signal's handler, which runs from the main loop, ends NVDA's dialogs and exits.
	os.kill(os.getpid(), signal.SIGTERM)


class _Client:
	"""The connection, the registration and the answers, all on the client's own thread."""

	def __init__(self, startupId: str) -> None:
		from gi.repository import Gio, GLib

		self._startupId = startupId
		self._context = GLib.MainContext.new()
		self._loop = GLib.MainLoop.new(self._context, False)
		self._cancellable = Gio.Cancellable.new()
		self._connection = None
		self._subscription: int | None = None
		#: The object path of NVDA's client, once registered.
		self.clientPath: str | None = None
		#: Whether the session is ending, so that NVDA is exiting and must not be asked to again.
		self.isEnding = False
		self.thread = threading.Thread(target=self._run, name="lindaSessionClient", daemon=True)

	def _run(self) -> None:
		self._context.push_thread_default()
		try:
			if self._connect():
				self._loop.run()
		except Exception:
			log.exception("Session manager: the client failed, so NVDA is no longer registered")
		finally:
			self._disconnect()
			self._context.pop_thread_default()

	def _connect(self) -> bool:
		from gi.repository import Gio, GLib

		try:
			address = Gio.dbus_address_get_for_bus_sync(Gio.BusType.SESSION, None)
			self._connection = Gio.DBusConnection.new_for_address_sync(
				address,
				Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
				| Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
				None,
				self._cancellable,
			)
		except GLib.Error as e:
			log.warning(f"Session manager: no session bus, so NVDA does not register with one: {e.message}")
			return False
		# Before registering, so that no question asked as soon as the registration is answered is missed.
		self._subscription = self._connection.signal_subscribe(
			BUS_NAME,
			CLIENT_INTERFACE,
			None,
			None,
			None,
			Gio.DBusSignalFlags.NONE,
			self._onSignal,
		)
		self._connection.call(
			BUS_NAME,
			OBJECT_PATH,
			INTERFACE,
			"RegisterClient",
			GLib.Variant("(ss)", (APP_ID, self._startupId)),
			GLib.VariantType.new("(o)"),
			Gio.DBusCallFlags.NO_AUTO_START,
			REGISTER_TIMEOUT_MS,
			self._cancellable,
			self._onRegistered,
		)
		return True

	def _onRegistered(self, connection, result) -> None:
		from gi.repository import GLib

		try:
			(self.clientPath,) = connection.call_finish(result).unpack()
		except GLib.Error as e:
			if not self._cancellable.is_cancelled():
				log.warning(
					f"Session manager: could not register with {BUS_NAME}, so NVDA runs unregistered: {e.message}"
				)
			return
		log.info(f"Session manager: registered as {self.clientPath}")

	def _disconnect(self) -> None:
		from gi.repository import GLib

		connection = self._connection
		if connection is None:
			return
		self._connection = None
		if self._subscription is not None:
			connection.signal_unsubscribe(self._subscription)
		try:
			connection.close_sync(None)
		except GLib.Error:
			log.debugWarning("Session manager: closing the bus connection failed", exc_info=True)

	def _onSignal(self, connection, sender, path, interface, member, parameters) -> None:
		if path != self.clientPath:
			return
		if member == "QueryEndSession":
			log.info("Session manager: asked whether the session may end, and says it may")
			self._respond()
		elif member == "EndSession":
			log.info("Session manager: the session is ending, so NVDA exits")
			self._endSession()
		elif member == "CancelEndSession":
			log.info("Session manager: the session goes on")
		elif member == "Stop":
			log.info("Session manager: told to exit")
			self._exit()

	def _respond(self) -> None:
		from gi.repository import Gio, GLib

		connection = self._connection
		if connection is None or self.clientPath is None:
			return

		def onAnswered(connection, result) -> None:
			try:
				connection.call_finish(result)
			except GLib.Error as e:
				log.warning(f"Session manager: could not answer the end of the session: {e.message}")

		connection.call(
			BUS_NAME,
			self.clientPath,
			CLIENT_INTERFACE,
			"EndSessionResponse",
			GLib.Variant("(bs)", (True, "")),
			None,
			Gio.DBusCallFlags.NO_AUTO_START,
			CALL_TIMEOUT_MS,
			None,
			onAnswered,
		)

	def _endSession(self) -> None:
		from gi.repository import GLib

		if self.isEnding:
			self._respond()
			return
		self.isEnding = True
		answered = False

		def answer(*args) -> bool:
			nonlocal answered
			if not answered:
				answered = True
				self._respond()
			return GLib.SOURCE_REMOVE

		def answerHere() -> None:
			source = GLib.idle_source_new()
			source.set_callback(answer)
			source.attach(self._context)

		def onTimeout(*args) -> bool:
			if not answered:
				log.warning(
					f"Session manager: NVDA did not save its configuration within {SAVE_TIMEOUT} s of the end"
					" of the session, which is let go on regardless",
				)
				# The main thread is held up; the signal makes it exit once it runs again.
				_exitAsOnSignal()
			return answer()

		def saveAndExit() -> None:
			import config

			try:
				config.saveOnExit()
			finally:
				answerHere()
				_exitAsOnSignal()

		timeout = GLib.timeout_source_new(int(SAVE_TIMEOUT * 1000))
		timeout.set_callback(onTimeout)
		timeout.attach(self._context)
		import queueHandler

		queueHandler.queueFunction(queueHandler.eventQueue, saveAndExit)

	def _exit(self) -> None:
		if self.isEnding:
			return
		self.isEnding = True
		_exitAsOnSignal()

	def _unregister(self) -> None:
		from gi.repository import Gio, GLib

		connection = self._connection
		if connection is None or self.clientPath is None or self.isEnding:
			return
		try:
			connection.call_sync(
				BUS_NAME,
				OBJECT_PATH,
				INTERFACE,
				"UnregisterClient",
				GLib.Variant("(o)", (self.clientPath,)),
				None,
				Gio.DBusCallFlags.NO_AUTO_START,
				CALL_TIMEOUT_MS,
				None,
			)
		except GLib.Error as e:
			log.warning(f"Session manager: could not unregister: {e.message}")
		else:
			log.info("Session manager: unregistered")

	def stop(self) -> None:
		from gi.repository import GLib

		def finish(*args) -> bool:
			self._unregister()
			self._loop.quit()
			return GLib.SOURCE_REMOVE

		self._cancellable.cancel()
		source = GLib.idle_source_new()
		source.set_callback(finish)
		source.attach(self._context)


_client: _Client | None = None


def start() -> None:
	"""Register with the session manager that started NVDA, if one did."""
	global _client
	if not _startupId:
		log.debug(f"Session manager: none started NVDA, as {STARTUP_ID_VARIABLE} was not set")
		return
	if _client is not None:
		return
	log.debug(f"Session manager: started NVDA with the startup ID {_startupId}")
	_client = _Client(_startupId)
	_client.thread.start()


def stop() -> None:
	"""Unregister, unless the session is ending, and stop answering the session manager."""
	global _client
	client = _client
	_client = None
	if client is None:
		return
	client.stop()
	client.thread.join(STOP_TIMEOUT)
	if client.thread.is_alive():
		log.debugWarning("Session manager: the client's thread did not end in time")
