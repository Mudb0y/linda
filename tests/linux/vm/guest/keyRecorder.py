"""Records every key that reaches the guest's input devices or its X server, with times, one JSON
object a line.

	keyRecorder.py evdev OUT
	keyRecorder.py x OUT
	keyRecorder.py now

evdev reads every keyboard node under /dev/input, as the X server's libinput does, and never grabs:
while another process holds a grab on a device, nothing from it is seen here, which is the point of
a grab. New nodes are picked up as they appear. x selects XInput 2 raw key events on the root
window, which the X server sends whatever grabs are held, so they are every key the X server
received, from whichever device.

Every record has "t", the time of the event on the guest's CLOCK_MONOTONIC in seconds, and
"received", when this process read it. For evdev "t" is the kernel's own stamp of the event. For x
it is "received": the X server stamps its events in whole milliseconds of a clock that lags this one
by as much as two, which is kept as "serverTime". A key record has "code" and "key", the evdev code and its name (X key
codes less 8), "value" (1 press, 0 release, 2 the kernel's repeat) and "device", the device's name.
A device record says a device was found ("added") or went ("removed"). A first record of kind
"ready" says the recorder is listening.

The test window records what reaches the application in the same form (testWindow.py).
"""

import errno
import fcntl
import json
import os
import select
import struct
import sys
import time

from evdev import InputDevice, ecodes, list_devices

#: _IOW('E', 0xa0, int): which clock the kernel stamps this open file's events with.
EVIOCSCLOCKID = 0x400445A0
CLOCK_MONOTONIC = 1
#: How often /dev/input is looked at for new nodes.
SCAN_INTERVAL = 0.05


def keyName(code: int) -> str:
	name = ecodes.KEY.get(code, str(code))
	return name[0] if isinstance(name, list) else name


class Output:
	def __init__(self, path: str) -> None:
		self._file = open(path, "a", buffering=1)  # noqa: SIM115

	def __call__(self, kind: str, **fields: object) -> None:
		self._file.write(json.dumps({"kind": kind, "received": time.monotonic(), **fields}) + "\n")


def _isKeyboard(device: InputDevice) -> bool:
	keys = device.capabilities().get(ecodes.EV_KEY, [])
	return any(code < ecodes.BTN_MISC for code in keys)


def recordEvdev(out: Output) -> None:
	devices: dict[int, InputDevice] = {}
	known: set[str] = set()
	out("ready")
	nextScan = 0.0
	while True:
		now = time.monotonic()
		if now >= nextScan:
			nextScan = now + SCAN_INTERVAL
			for path in sorted(set(list_devices()) - known):
				try:
					device = InputDevice(path)
				except OSError:
					# Not yet given its permissions by udev, or gone already; the next scan sees.
					continue
				known.add(path)
				if not _isKeyboard(device):
					device.close()
					continue
				fcntl.ioctl(device.fd, EVIOCSCLOCKID, struct.pack("i", CLOCK_MONOTONIC))
				devices[device.fd] = device
				info = device.info
				out(
					"device",
					change="added",
					t=time.monotonic(),
					node=path,
					device=device.name,
					phys=device.phys,
					bustype=info.bustype,
					vendor=info.vendor,
					product=info.product,
				)
		ready, _, _ = select.select(list(devices), [], [], SCAN_INTERVAL)
		for fd in ready:
			device = devices[fd]
			try:
				events = list(device.read())
			except OSError as e:
				if e.errno not in (errno.ENODEV, errno.EIO):
					raise
				del devices[fd]
				known.discard(device.path)
				out("device", change="removed", t=time.monotonic(), node=device.path, device=device.name)
				device.close()
				continue
			for event in events:
				if event.type == ecodes.EV_KEY:
					out(
						"key",
						t=event.timestamp(),
						node=device.path,
						device=device.name,
						code=event.code,
						key=keyName(event.code),
						value=event.value,
					)


def recordX(out: Output) -> None:
	from Xlib import display
	from Xlib.ext import xinput

	connection = display.Display()
	# python-xlib's own query asks for 2.0, and a client that announced less than 2.1 is sent no raw
	# event while another client holds a grab.
	version = xinput.XIQueryVersion(
		display=connection.display,
		opcode=connection.display.get_extension_major(xinput.extname),
		major_version=2,
		minor_version=2,
	)
	assert (version.major_version, version.minor_version) >= (2, 2), "XInput 2.2 is needed"
	root = connection.screen().root

	def readDevices() -> dict[int, str]:
		return {
			info.deviceid: info.name for info in connection.xinput_query_device(xinput.AllDevices).devices
		}

	names = readDevices()
	for deviceId, name in sorted(names.items()):
		out("device", change="added", t=time.monotonic(), id=deviceId, device=name)
	# Master devices only, as Linda selects them: each key once, with the slave it came from.
	root.xinput_select_events(
		[
			(xinput.AllMasterDevices, xinput.RawKeyPressMask | xinput.RawKeyReleaseMask),
			(xinput.AllDevices, xinput.HierarchyChangedMask),
		],
	)
	connection.sync()
	out("ready")
	while True:
		event = connection.next_event()
		evtype = getattr(event, "evtype", None)
		if evtype in (xinput.RawKeyPress, xinput.RawKeyRelease):
			# python-xlib does not parse raw events: XIRawEvent starts deviceid, time, detail, sourceid.
			_deviceId, serverTime, detail, sourceId = struct.unpack_from("=HIIH", event.data, 0)
			if sourceId not in names:
				names = readDevices()
			out(
				"key",
				t=time.monotonic(),
				serverTime=serverTime / 1000,
				device=names.get(sourceId, str(sourceId)),
				source=sourceId,
				code=detail - 8,
				key=keyName(detail - 8),
				value=1 if evtype == xinput.RawKeyPress else 0,
			)
		elif evtype == xinput.HierarchyChanged:
			found = readDevices()
			for deviceId in sorted(found.keys() - names.keys()):
				out("device", change="added", t=time.monotonic(), id=deviceId, device=found[deviceId])
			for deviceId in sorted(names.keys() - found.keys()):
				out("device", change="removed", t=time.monotonic(), id=deviceId, device=names[deviceId])
			names = found


if __name__ == "__main__":
	if sys.argv[1:] == ["now"]:
		# The clock every record is stamped with, for a test to mark where it is.
		print(time.monotonic())
		sys.exit()
	if len(sys.argv) != 3 or sys.argv[1] not in ("evdev", "x"):
		sys.exit("usage: keyRecorder.py evdev|x OUT | now")
	os.makedirs(os.path.dirname(os.path.abspath(sys.argv[2])), exist_ok=True)
	(recordEvdev if sys.argv[1] == "evdev" else recordX)(Output(sys.argv[2]))
