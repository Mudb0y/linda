"""Speaks for scripts that ask Orca to speak, while NVDA runs in Orca's place.

Scripts written for Orca speak by calling PresentMessage with a string on Orca's service on the
session bus, and get back true. This answers that call under the same name, path and interface:
the text is spoken as NVDA speaks a message of its own, queued after what is being spoken, as Orca
queues it, and the call returns at once.

The name is asked for without taking it from an owner, and while one has it NVDA waits in the
bus's queue. Orca asks for its name without taking it either, and does not start when it is put in
the queue, so NVDA gives the name up as soon as an Orca process connects to the bus and asks for
it again, behind Orca, once Orca has it or has gone.

In a Wayland session NVDA says once, when it finds Orca with the name, that both will speak: there
the desktop's own screen reader switch starts Orca, and someone who had Orca on to get speech before
starting NVDA hears two readers. The way to turn Orca off is named only on the desktops whose switch
is known.

The bus is served on a thread of its own, so that a call is answered whatever NVDA's main thread is
doing; what is spoken is queued to the main thread.

Programs that stop speech through it, such as FastSM and the Prism library behind TeamTalk, look for
the InterruptSpeech command of Orca's speech module first, so that module answers too, with that
command alone.

LINDA_SPEAK_SERVICE=off in the environment leaves the service out.
"""

import os
import threading
import time

from logHandler import log

from linuxPlatform import displayServer

BUS_NAME = "org.gnome.Orca.Service"
OBJECT_PATH = "/org/gnome/Orca/Service"
INTERFACE = "org.gnome.Orca.Service"
MODULE_INTERFACE = "org.gnome.Orca.Module"
#: The speech module, under the name Orca gives it.
SPEECH_MODULE = "SpeechAndVerbosityManager"
INTERRUPT_COMMAND = "InterruptSpeech"
#: The environment variable that turns the service off when it is "off".
SWITCH_VARIABLE = "LINDA_SPEAK_SERVICE"
#: Characters of a message that are spoken; NVDA's speech can take seconds over a book's worth.
MAX_MESSAGE_LENGTH = 10_000
#: Messages waiting for the main thread beyond which more are dropped, so that a script calling in a
#: loop cannot bury NVDA's own speech.
MAX_PENDING_MESSAGES = 100
#: Program names in the command line of an Orca process: its script, and the script Nix wraps it as.
ORCA_PROGRAMS = frozenset({"orca", ".orca-wrapped"})
#: Milliseconds the bus has to answer one of NVDA's own requests.
BUS_TIMEOUT_MS = 2000
#: Seconds the main thread waits for the service's thread to end.
STOP_TIMEOUT = 2.0
#: The shortcut that switches the desktop's screen reader, by displayServer.compositor(): GNOME 49 and
#: later and Plasma 6.8 start and stop Orca with it.
ORCA_SWITCHES = {"gnome": "Super+Alt+S", "kde": "Meta+Alt+S"}
#: Seconds after start-up the notice waits for the first focus at most, and how often it looks.
FIRST_FOCUS_WAIT = 5.0
FIRST_FOCUS_POLL_MS = 100

_DBUS = "org.freedesktop.DBus"
_DBUS_PATH = "/org/freedesktop/DBus"
_ALLOW_REPLACEMENT = 0x1
_REQUEST_REPLIES = {
	1: "has the name",
	2: "is waiting for the name",
	3: "could not wait for the name",
	4: "has the name",
}
_NOT_SUPPORTED = "org.freedesktop.DBus.Error.NotSupported"

_INTROSPECTION = f"""
<node>
	<interface name="{INTERFACE}">
		<method name="PresentMessage">
			<arg direction="in" name="message" type="s"/>
			<arg direction="out" name="return" type="b"/>
		</method>
		<method name="GetVersion">
			<arg direction="out" name="return" type="s"/>
		</method>
		<method name="ListModules">
			<arg direction="out" name="return" type="as"/>
		</method>
		<method name="ListCommands">
			<arg direction="out" name="return" type="a(ss)"/>
		</method>
		<method name="ShowPreferences">
			<arg direction="out" name="return" type="b"/>
		</method>
		<method name="Quit">
			<arg direction="out" name="return" type="b"/>
		</method>
	</interface>
	<interface name="{MODULE_INTERFACE}">
		<method name="ListCommands">
			<arg direction="out" name="return" type="a(ss)"/>
		</method>
		<method name="ListRuntimeGetters">
			<arg direction="out" name="return" type="a(ss)"/>
		</method>
		<method name="ListRuntimeSetters">
			<arg direction="out" name="return" type="a(ss)"/>
		</method>
		<method name="ExecuteCommand">
			<arg direction="in" name="command" type="s"/>
			<arg direction="in" name="notifyUser" type="b"/>
			<arg direction="out" name="return" type="b"/>
		</method>
	</interface>
</node>
"""

#: What ListCommands describes, as Orca describes its own.
_COMMANDS = (
	("GetVersion", "Returns the screen reader's version."),
	("PresentMessage", "Presents message to the user."),
)


def _interrupt() -> None:
	import speech

	speech.cancelSpeech()


def _isOrca(pid: int) -> bool:
	try:
		with open(f"/proc/{pid}/cmdline", "rb") as f:
			arguments = f.read().split(b"\0")
	except OSError:
		return False
	return any(os.path.basename(a.decode(errors="replace")) in ORCA_PROGRAMS for a in arguments[:3])


def _present(text: str) -> None:
	global _pending
	with _pendingLock:
		_pending -= 1
	import ui

	ui.message(text)


_pending = 0
_pendingLock = threading.Lock()


def _queueMessage(text: str) -> None:
	global _pending
	if not text:
		# Orca presents nothing for an empty message, and still answers true.
		return
	with _pendingLock:
		if _pending >= MAX_PENDING_MESSAGES:
			log.debugWarning("Speak service: too many messages waiting; this one is dropped")
			return
		_pending += 1
	if len(text) > MAX_MESSAGE_LENGTH:
		log.debug(f"Speak service: only the first {MAX_MESSAGE_LENGTH} of {len(text)} characters are spoken")
		text = text[:MAX_MESSAGE_LENGTH]
	import queueHandler

	queueHandler.queueFunction(queueHandler.eventQueue, _present, text)


def orcaNotice(compositor: str) -> str:
	"""What NVDA says on finding Orca with the name in a Wayland session on that compositor."""
	# Translators: Said once on Linux in a Wayland session, when NVDA finds the Orca screen reader
	# running beside it, so that both speak everything.
	notice = _("Orca is running too, so both will speak.")
	switch = ORCA_SWITCHES.get(compositor)
	if switch is None:
		# Elsewhere no switch is known, and naming one might start Orca again.
		return notice
	# Translators: Follows the sentence saying that Orca is running too, on desktops whose screen reader
	# switch starts and stops Orca. {shortcut} is that shortcut, such as Super+Alt+S.
	advice = _("If Orca was turned on with {shortcut}, press it again to turn it off.").format(
		shortcut=switch
	)
	return f"{notice} {advice}"


#: Whether the service has found Orca with the name in a Wayland session; it says so once in a run.
#: Used on the service's thread.
_orcaNoticed = False
#: Whether the first focus has been reported, and the notice waiting for it. Used on the main thread.
_firstFocusReported = False
_noticeWaiting: str | None = None


def _sayOrcaNotice(compositor: str) -> None:
	global _noticeWaiting
	_noticeWaiting = orcaNotice(compositor)
	if _firstFocusReported:
		_sayWaitingNotice()


def _sayWaitingNotice() -> None:
	global _noticeWaiting
	text, _noticeWaiting = _noticeWaiting, None
	if text is not None:
		import ui

		ui.message(text)


def _onPostNvdaStartup() -> None:
	_waitForFirstFocus(time.monotonic() + FIRST_FOCUS_WAIT)


def _waitForFirstFocus(deadline: float) -> None:
	"""Let the notice be said once the first focus has been reported, whose foreground event would
	cancel it; on Linux that focus may come from AT-SPI after start-up has ended."""
	global _firstFocusReported
	import api
	import core
	import eventHandler

	focus = eventHandler.lastQueuedFocusObject
	if (focus is None or focus != api.getFocusObject()) and time.monotonic() < deadline:
		core.callLater(FIRST_FOCUS_POLL_MS, _waitForFirstFocus, deadline)
		return
	_firstFocusReported = True
	_sayWaitingNotice()


class _Service:
	"""The connection, its name and its object, all used on the service's own thread."""

	def __init__(self) -> None:
		from gi.repository import GLib

		self._context = GLib.MainContext.new()
		self._loop = GLib.MainLoop.new(self._context, False)
		self._connection = None
		self._subscriptions: list[int] = []
		self._registrations: list[int] = []
		#: Whether NVDA has the name.
		self.isOwner = False
		#: The connections of an Orca process that NVDA gave the name up for, while that one has not
		#: taken it.
		self._yieldingTo: set[str] = set()
		self.thread = threading.Thread(target=self._run, name="lindaSpeakService", daemon=True)

	def _run(self) -> None:
		self._context.push_thread_default()
		try:
			if self._connect():
				self._loop.run()
		except Exception:
			log.exception("Speak service failed")
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
				None,
			)
		except GLib.Error as e:
			log.warning(
				f"Speak service: no session bus, so scripts that ask Orca to speak are not heard: {e}"
			)
			return False
		nodeInfo = Gio.DBusNodeInfo.new_for_xml(_INTROSPECTION)
		for path, interface in (
			(OBJECT_PATH, INTERFACE),
			(f"{OBJECT_PATH}/{SPEECH_MODULE}", MODULE_INTERFACE),
		):
			self._registrations.append(
				self._connection.register_object(
					path, nodeInfo.lookup_interface(interface), self._onCall, None, None
				),
			)
		for member, argument, handler in (
			("NameOwnerChanged", None, self._onNameOwnerChanged),
			("NameAcquired", BUS_NAME, self._onNameAcquired),
			("NameLost", BUS_NAME, self._onNameLost),
		):
			self._subscriptions.append(
				self._connection.signal_subscribe(
					_DBUS,
					_DBUS,
					member,
					_DBUS_PATH,
					argument,
					Gio.DBusSignalFlags.NONE,
					handler,
				),
			)
		self._requestName()
		return True

	def _disconnect(self) -> None:
		from gi.repository import GLib

		connection = self._connection
		if connection is None:
			return
		self._connection = None
		for subscription in self._subscriptions:
			connection.signal_unsubscribe(subscription)
		for registration in self._registrations:
			connection.unregister_object(registration)
		self._registrations.clear()
		try:
			connection.close_sync(None)
		except GLib.Error:
			log.debugWarning("Speak service: closing the bus connection failed", exc_info=True)

	def _callBus(self, method: str, parameters, replyType: str):
		from gi.repository import GLib

		return self._connection.call_sync(
			_DBUS,
			_DBUS_PATH,
			_DBUS,
			method,
			parameters,
			GLib.VariantType.new(replyType),
			0,
			BUS_TIMEOUT_MS,
			None,
		).unpack()

	def _requestName(self) -> None:
		from gi.repository import GLib

		try:
			(reply,) = self._callBus(
				"RequestName", GLib.Variant("(su)", (BUS_NAME, _ALLOW_REPLACEMENT)), "(u)"
			)
		except GLib.Error as e:
			log.warning(f"Speak service: could not ask for {BUS_NAME}: {e}")
			return
		log.debug(f"Speak service: NVDA {_REQUEST_REPLIES.get(reply, reply)} {BUS_NAME}")
		if reply in (2, 3):
			self._noteOrcaWithName()

	def _noteOrcaWithName(self) -> None:
		"""Say once in a Wayland session that both readers speak, if the name's owner is Orca.

		NVDA asks for the name whenever it does not have it, Orca's taking it included, so its being
		put in the queue is the one moment to look.
		"""
		global _orcaNoticed
		if not displayServer.isWayland() or _orcaNoticed:
			return
		from gi.repository import GLib

		try:
			(owner,) = self._callBus("GetNameOwner", GLib.Variant("(s)", (BUS_NAME,)), "(s)")
			(pid,) = self._callBus("GetConnectionUnixProcessID", GLib.Variant("(s)", (owner,)), "(u)")
		except GLib.Error:
			# The owner has gone already.
			return
		if not _isOrca(pid):
			return
		_orcaNoticed = True
		compositor = displayServer.compositor()
		log.info(f"Speak service: Orca has {BUS_NAME} in this Wayland session on {compositor}; saying so")
		import queueHandler

		queueHandler.queueFunction(queueHandler.eventQueue, _sayOrcaNotice, compositor)

	def _releaseName(self) -> None:
		from gi.repository import GLib

		try:
			self._callBus("ReleaseName", GLib.Variant("(s)", (BUS_NAME,)), "(u)")
		except GLib.Error as e:
			log.warning(f"Speak service: could not give up {BUS_NAME}: {e}")

	def _onNameAcquired(self, *args) -> None:
		self.isOwner = True
		log.debug(f"Speak service: NVDA now answers {BUS_NAME}")

	def _onNameLost(self, *args) -> None:
		self.isOwner = False
		log.debug(f"Speak service: NVDA no longer answers {BUS_NAME}")

	def _onNameOwnerChanged(self, connection, sender, path, interface, signal, parameters) -> None:
		from gi.repository import GLib

		name, oldOwner, newOwner = parameters.unpack()
		if name == BUS_NAME:
			if self._yieldingTo and newOwner and newOwner != connection.get_unique_name():
				# Orca has it: wait behind it, to answer again once it has gone.
				self._yieldingTo.clear()
				self._requestName()
			return
		if not name.startswith(":"):
			return
		if not newOwner and name in self._yieldingTo:
			self._yieldingTo.discard(name)
			if not self._yieldingTo:
				log.debug("Speak service: Orca left without taking the name")
				self._requestName()
			return
		if oldOwner or not newOwner or not (self.isOwner or self._yieldingTo):
			return
		try:
			(pid,) = self._callBus("GetConnectionUnixProcessID", GLib.Variant("(s)", (name,)), "(u)")
		except GLib.Error:
			# Gone already.
			return
		if not _isOrca(pid):
			return
		self._yieldingTo.add(name)
		if self.isOwner:
			# Orca asks for the name a moment after it connects, and quits when it is not given it.
			log.info("Speak service: Orca is starting, so NVDA gives it its name")
			self.isOwner = False
			self._releaseName()

	def _onCall(self, connection, sender, path, interface, method, parameters, invocation) -> None:
		from gi.repository import GLib

		try:
			if interface == MODULE_INTERFACE:
				invocation.return_value(self._onModuleCall(method, parameters))
			elif method == "PresentMessage":
				(text,) = parameters.unpack()
				_queueMessage(text)
				invocation.return_value(GLib.Variant("(b)", (True,)))
			elif method == "GetVersion":
				import buildVersion

				invocation.return_value(GLib.Variant("(s)", (buildVersion.version,)))
			elif method == "ListModules":
				invocation.return_value(GLib.Variant("(as)", ([SPEECH_MODULE],)))
			elif method == "ListCommands":
				invocation.return_value(GLib.Variant("(a(ss))", (list(_COMMANDS),)))
			else:
				invocation.return_dbus_error(_NOT_SUPPORTED, f"{method} is Orca's own; NVDA does not do it")
		except Exception:
			log.exception(f"Speak service: {method} failed")
			invocation.return_dbus_error("org.freedesktop.DBus.Error.Failed", f"{method} failed")

	def _onModuleCall(self, method: str, parameters):
		from gi.repository import GLib

		if method == "ListCommands":
			return GLib.Variant("(a(ss))", ([(INTERRUPT_COMMAND, "Interrupts speech.")],))
		if method in ("ListRuntimeGetters", "ListRuntimeSetters"):
			return GLib.Variant("(a(ss))", ([],))
		command, _notifyUser = parameters.unpack()
		if command != INTERRUPT_COMMAND:
			return GLib.Variant("(b)", (False,))
		import queueHandler

		queueHandler.queueFunction(queueHandler.eventQueue, _interrupt)
		return GLib.Variant("(b)", (True,))

	def stop(self) -> None:
		from gi.repository import GLib

		source = GLib.idle_source_new()
		source.set_callback(lambda *args: self._loop.quit() or GLib.SOURCE_REMOVE)
		source.attach(self._context)


_service: _Service | None = None


def start() -> None:
	"""Start answering Orca's speaking call, unless the environment turns the service off."""
	global _service
	if os.environ.get(SWITCH_VARIABLE, "").lower() == "off":
		log.debug(f"Speak service: off, as {SWITCH_VARIABLE} says")
		return
	if _service is not None:
		return
	if displayServer.isWayland():
		import core

		core.postNvdaStartup.register(_onPostNvdaStartup)
	_service = _Service()
	_service.thread.start()


def stop() -> None:
	"""Give the name up and stop serving the bus."""
	global _service
	service = _service
	_service = None
	if service is None:
		return
	import core

	core.postNvdaStartup.unregister(_onPostNvdaStartup)
	service.stop()
	service.thread.join(STOP_TIMEOUT)
	if service.thread.is_alive():
		log.debugWarning("Speak service: its thread did not end in time")
