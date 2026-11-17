"""The session bus service through which programs ask the screen reader to speak a message.

Programs written for screen reader users on Linux speak their own announcements, such as a new chat
message or "No next heading", by calling PresentMessage on org.gnome.Orca.Service, the remote control
Orca 49 and later offers: Paperback runs gdbus for it, FastSM and the Prism library behind TeamTalk
call it directly, and Prism also stops speech through the InterruptSpeech command of Orca's speech
module. With Orca switched off nobody owns that name and all of them are silent, so NVDA answers in
its place, speaking each message as it speaks a UIA notification on Windows.

The name is taken only while nobody holds it, and handed over to a screen reader that asks for it:
Orca, when it is running, keeps it.
"""

from logHandler import log

BUS_NAME = "org.gnome.Orca.Service"
SERVICE_PATH = "/org/gnome/Orca/Service"
SERVICE_INTERFACE = "org.gnome.Orca.Service"
MODULE_INTERFACE = "org.gnome.Orca.Module"
#: The speech module, under the name Orca 49 gives it; Prism and FastSM look for InterruptSpeech here.
SPEECH_MODULE = "SpeechAndVerbosityManager"
INTERRUPT_COMMAND = "InterruptSpeech"
#: Characters of one message spoken at most, so that a program cannot have NVDA read a book at it.
MESSAGE_LIMIT = 10000

_INTROSPECTION = f"""
<node>
	<interface name="{SERVICE_INTERFACE}">
		<method name="PresentMessage">
			<arg type="s" name="message" direction="in"/>
			<arg type="b" name="success" direction="out"/>
		</method>
		<method name="GetVersion">
			<arg type="s" name="version" direction="out"/>
		</method>
		<method name="ListModules">
			<arg type="as" name="modules" direction="out"/>
		</method>
		<method name="ListCommands">
			<arg type="a(ss)" name="commands" direction="out"/>
		</method>
	</interface>
	<interface name="{MODULE_INTERFACE}">
		<method name="ListCommands">
			<arg type="a(ss)" name="commands" direction="out"/>
		</method>
		<method name="ListRuntimeGetters">
			<arg type="a(ss)" name="getters" direction="out"/>
		</method>
		<method name="ListRuntimeSetters">
			<arg type="a(ss)" name="setters" direction="out"/>
		</method>
		<method name="ExecuteCommand">
			<arg type="s" name="command" direction="in"/>
			<arg type="b" name="notifyUser" direction="in"/>
			<arg type="b" name="success" direction="out"/>
		</method>
	</interface>
</node>
"""

_connection = None
_registrations: list[int] = []
_ownerId: int | None = None


def _speak(message: str) -> None:
	import ui

	ui.message(message)


def _interrupt() -> None:
	import speech

	speech.cancelSpeech()


def _queue(func, *args) -> None:
	import queueHandler

	# A call arrives whenever GLib's context runs, which can be inside wx.Yield in the middle of a
	# script; it is handled where events are.
	queueHandler.queueFunction(queueHandler.eventQueue, func, *args)


def _onServiceCall(method: str, parameters) -> object:
	from gi.repository import GLib

	if method == "PresentMessage":
		message = parameters.unpack()[0].strip()[:MESSAGE_LIMIT]
		if message:
			_queue(_speak, message)
		return GLib.Variant("(b)", (True,))
	if method == "GetVersion":
		import buildVersion

		return GLib.Variant("(s)", (f"NVDA {buildVersion.version}",))
	if method == "ListModules":
		return GLib.Variant("(as)", ([SPEECH_MODULE],))
	return GLib.Variant("(a(ss))", ([],))


def _onModuleCall(method: str, parameters) -> object:
	from gi.repository import GLib

	if method == "ListCommands":
		return GLib.Variant("(a(ss))", ([(INTERRUPT_COMMAND, "Interrupts speech.")],))
	if method in ("ListRuntimeGetters", "ListRuntimeSetters"):
		return GLib.Variant("(a(ss))", ([],))
	command, _notifyUser = parameters.unpack()
	if command != INTERRUPT_COMMAND:
		return GLib.Variant("(b)", (False,))
	_queue(_interrupt)
	return GLib.Variant("(b)", (True,))


def _onMethodCall(connection, sender, path, interface, method, parameters, invocation) -> None:
	log.debug(f"{interface}.{method} from {sender}")
	try:
		if interface == SERVICE_INTERFACE:
			reply = _onServiceCall(method, parameters)
		else:
			reply = _onModuleCall(method, parameters)
	except Exception:
		log.exception(f"Answering {interface}.{method} from {sender}")
		invocation.return_dbus_error("org.freedesktop.DBus.Error.Failed", "the call could not be handled")
		return
	invocation.return_value(reply)


def _onNameAcquired(connection, name: str) -> None:
	log.debug(f"Answering {name} for programs that announce through the screen reader")


def _onNameLost(connection, name: str) -> None:
	log.debug(f"Not answering {name}: another screen reader holds it")


def initialize() -> None:
	"""Answer on the session bus, unless there is none or another screen reader holds the name."""
	global _connection, _ownerId
	import gi

	gi.require_version("Gio", "2.0")
	from gi.repository import Gio, GLib

	try:
		connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
	except GLib.Error as e:
		log.debugWarning(f"No session bus to answer {BUS_NAME} on: {e.message}")
		return
	nodeInfo = Gio.DBusNodeInfo.new_for_xml(_INTROSPECTION)
	service = nodeInfo.lookup_interface(SERVICE_INTERFACE)
	module = nodeInfo.lookup_interface(MODULE_INTERFACE)
	_registrations.append(connection.register_object(SERVICE_PATH, service, _onMethodCall, None, None))
	_registrations.append(
		connection.register_object(f"{SERVICE_PATH}/{SPEECH_MODULE}", module, _onMethodCall, None, None),
	)
	_connection = connection
	_ownerId = Gio.bus_own_name_on_connection(
		connection,
		BUS_NAME,
		Gio.BusNameOwnerFlags.ALLOW_REPLACEMENT | Gio.BusNameOwnerFlags.DO_NOT_QUEUE,
		_onNameAcquired,
		_onNameLost,
	)


def terminate() -> None:
	global _connection, _ownerId
	if _connection is None:
		return
	from gi.repository import Gio

	if _ownerId is not None:
		Gio.bus_unown_name(_ownerId)
		_ownerId = None
	for registration in _registrations:
		_connection.unregister_object(registration)
	_registrations.clear()
	# Flushed, so that the name is free the moment NVDA has gone, for a screen reader started after.
	_connection.flush_sync(None)
	_connection = None
