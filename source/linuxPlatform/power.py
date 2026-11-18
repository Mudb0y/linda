"""The power status from UPower, in the form GetSystemPowerStatus gives it on Windows.

winAPI._powerTracking reads the power status through winKernel.GetSystemPowerStatus and says what
NVDA says for it; here that status is filled from UPower's display device, the battery, or batteries
taken together, that desktops show, and from whether UPower says the computer runs on battery.
When the charger is plugged in or pulled out, UPower's OnBattery changes, and NVDA is told as the
Windows message window tells it of WM_POWERBROADCAST.

UPower is on the system bus, which DBUS_SYSTEM_BUS_ADDRESS in the environment can name, as for any
D-Bus client. Signals are heard on a thread of their own; a report is queued to the main thread.
"""

import threading

from logHandler import log

BUS_NAME = "org.freedesktop.UPower"
OBJECT_PATH = "/org/freedesktop/UPower"
DISPLAY_DEVICE_PATH = "/org/freedesktop/UPower/devices/DisplayDevice"
DEVICE_INTERFACE = "org.freedesktop.UPower.Device"
PROPERTIES_INTERFACE = "org.freedesktop.DBus.Properties"
#: Milliseconds UPower has to answer.
BUS_TIMEOUT_MS = 1000
#: Seconds the main thread waits for the signal thread to end.
STOP_TIMEOUT = 2.0

#: UPower's device states.
STATE_CHARGING = 1
STATE_DISCHARGING = 2
STATE_EMPTY = 3
STATE_PENDING_DISCHARGE = 6
#: SYSTEM_POWER_STATUS's values, as winAPI._powerTracking names them.
AC_OFFLINE, AC_ONLINE = 0, 1
BATTERY_HIGH, BATTERY_LOW, BATTERY_CRITICAL, BATTERY_CHARGING = 0x1, 0x2, 0x4, 0x8
NO_SYSTEM_BATTERY, BATTERY_UNKNOWN = 0x80, 0xFF
LIFE_TIME_UNKNOWN = 0xFFFFFFFF
PERCENT_UNKNOWN = 0xFF

_busLock = threading.Lock()
_bus = None
_monitor: "_Monitor | None" = None


def _connection():
	"""A connection to the system bus, opened at first use and kept; None when there is no bus."""
	global _bus
	from gi.repository import Gio, GLib

	with _busLock:
		if _bus is not None and not _bus.is_closed():
			return _bus
		try:
			address = Gio.dbus_address_get_for_bus_sync(Gio.BusType.SYSTEM, None)
			_bus = Gio.DBusConnection.new_for_address_sync(
				address,
				Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
				| Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
				None,
				None,
			)
		except GLib.Error as e:
			log.debug(f"No system bus, so no power status: {e}")
			_bus = None
		return _bus


def _properties(connection, path: str, interface: str) -> dict:
	from gi.repository import GLib

	reply = connection.call_sync(
		BUS_NAME,
		path,
		PROPERTIES_INTERFACE,
		"GetAll",
		GLib.Variant("(s)", (interface,)),
		GLib.VariantType.new("(a{sv})"),
		0,
		BUS_TIMEOUT_MS,
		None,
	)
	return reply.unpack()[0]


def status() -> tuple[int, int, int, int] | None:
	"""(ACLineStatus, BatteryFlag, BatteryLifePercent, BatteryLifeTime) as Windows would give them,
	or None when UPower cannot be asked."""
	from gi.repository import GLib

	connection = _connection()
	if connection is None:
		return None
	try:
		device = _properties(connection, DISPLAY_DEVICE_PATH, DEVICE_INTERFACE)
		daemon = _properties(connection, OBJECT_PATH, BUS_NAME)
	except GLib.Error as e:
		log.debug(f"UPower did not give the power status: {e}")
		return None
	state = device.get("State", 0)
	onBattery = daemon.get("OnBattery")
	if onBattery is None:
		onBattery = state in (STATE_DISCHARGING, STATE_EMPTY, STATE_PENDING_DISCHARGE)
	ac = AC_OFFLINE if onBattery else AC_ONLINE
	if not device.get("IsPresent", False):
		return ac, NO_SYSTEM_BATTERY, PERCENT_UNKNOWN, LIFE_TIME_UNKNOWN
	percent = max(0, min(100, round(device.get("Percentage", 0.0))))
	flags = 0
	if percent > 66:
		flags |= BATTERY_HIGH
	elif percent < 33:
		flags |= BATTERY_LOW
	if percent < 5:
		flags |= BATTERY_CRITICAL
	if state == STATE_CHARGING:
		flags |= BATTERY_CHARGING
	timeToEmpty = device.get("TimeToEmpty", 0)
	# Windows knows the time left only while the battery runs the computer.
	lifeTime = timeToEmpty if onBattery and timeToEmpty > 0 else LIFE_TIME_UNKNOWN
	return ac, flags, percent, lifeTime


def getSystemPowerStatus(sps) -> int:
	"""Fill sps, a SYSTEM_POWER_STATUS, as kernel32's GetSystemPowerStatus does.

	:return: Always 1: when UPower cannot be asked, the battery is unknown, which NVDA reports, as
		Windows' failure would have NVDA ask for its last error, which only Windows has.
	"""
	current = status()
	if current is None:
		sps.ACLineStatus = 0xFF
		sps.BatteryFlag = BATTERY_UNKNOWN
		sps.BatteryLifePercent = PERCENT_UNKNOWN
		sps.BatteryLifeTime = LIFE_TIME_UNKNOWN
		sps.BatteryFullLifeTime = LIFE_TIME_UNKNOWN
		return 1
	sps.ACLineStatus, sps.BatteryFlag, sps.BatteryLifePercent, sps.BatteryLifeTime = current
	sps.BatteryFullLifeTime = LIFE_TIME_UNKNOWN
	return 1


class _Monitor:
	"""Hears UPower say the computer has gone on or off battery, on a thread of its own."""

	def __init__(self) -> None:
		from gi.repository import GLib

		self._context = GLib.MainContext.new()
		self._loop = GLib.MainLoop.new(self._context, False)
		self._subscription: int | None = None
		self._connection = None
		self.thread = threading.Thread(target=self._run, name="lindaPower", daemon=True)

	def _run(self) -> None:
		self._context.push_thread_default()
		try:
			if self._subscribe():
				self._loop.run()
		except Exception:
			log.exception("Power monitor failed")
		finally:
			if self._subscription is not None and self._connection is not None:
				self._connection.signal_unsubscribe(self._subscription)
			self._context.pop_thread_default()

	def _subscribe(self) -> bool:
		from gi.repository import Gio
		from winAPI import _powerTracking

		self._connection = _connection()
		if self._connection is None:
			return False
		self._subscription = self._connection.signal_subscribe(
			BUS_NAME,
			PROPERTIES_INTERFACE,
			"PropertiesChanged",
			OBJECT_PATH,
			BUS_NAME,
			Gio.DBusSignalFlags.NONE,
			self._onPropertiesChanged,
		)
		# As the Windows message window does at its start, so that the first change is told from the
		# state it changed from; only when UPower answers, as that logs an error otherwise.
		if status() is not None:
			_powerTracking.initialize()
		return True

	def _onPropertiesChanged(self, _connection, _sender, _path, _interface, _signal, parameters) -> None:
		_name, changed, invalidated = parameters.unpack()
		if "OnBattery" not in changed and "OnBattery" not in invalidated:
			return
		import queueHandler
		from winAPI import _powerTracking

		queueHandler.queueFunction(queueHandler.eventQueue, _powerTracking.reportACStateChange)

	def stop(self) -> None:
		from gi.repository import GLib

		source = GLib.idle_source_new()
		source.set_callback(lambda *args: self._loop.quit() or GLib.SOURCE_REMOVE)
		source.attach(self._context)
		self.thread.join(STOP_TIMEOUT)


def start() -> None:
	"""Start hearing UPower's changes, which NVDA announces as it does on Windows."""
	global _monitor
	if _monitor is not None:
		return
	_monitor = _Monitor()
	_monitor.thread.start()


def stop() -> None:
	global _monitor, _bus
	monitor, _monitor = _monitor, None
	if monitor is not None:
		monitor.stop()
	with _busLock:
		bus, _bus = _bus, None
	if bus is not None:
		from gi.repository import GLib

		try:
			bus.close_sync(None)
		except GLib.Error:
			log.debugWarning("Closing the system bus connection failed", exc_info=True)
