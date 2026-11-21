"""The keyboard layout sway gives linda-keyd's virtual keyboard, read over sway's IPC.

What NVDA passes on is typed through that keyboard, so its layout and group are the ones keys are
read in, whatever the keyboards the helper holds are given. sway configures each input by its
identifier, then by its type, then by "*", so a configuration that names only the user's own keyboard
leaves the virtual one on xkbcommon's default; that is said once.

sway's IPC names layouts only by the descriptions their symbols files give them, so each is found again
in the rules' XML registry on xkbcommon's include paths. A description the registry does not have
gives localed's layout instead. The model and options, which the IPC does not give, are xkbcommon's
defaults from the environment, as sway's own are for an input its configuration says nothing of.
"""

import json
import os
import socket
import stat
import struct
import threading
from collections.abc import Callable
from typing import NamedTuple

from logHandler import log

from . import VIRTUAL_KEYBOARD
from .desktopLayout import (
	DEFAULT_MODEL,
	DEFAULT_RULES,
	START_TIMEOUT,
	STOP_TIMEOUT,
	LayoutSnapshot,
	activeLayoutName,
	systemSnapshot,
)

#: VIRTUAL_VENDOR and VIRTUAL_PRODUCT in engine/crates/keyd/src/evdev.rs.
VIRTUAL_VENDOR = 0x4C69
VIRTUAL_PRODUCT = 0x6E64
#: How sway names linda-keyd's keyboard: vendor, product and name with spaces as underscores
#: (input_device_get_identifier in sway/input/input-manager.c).
VIRTUAL_IDENTIFIER = f"{VIRTUAL_VENDOR}:{VIRTUAL_PRODUCT}:{VIRTUAL_KEYBOARD.replace(' ', '_')}"

SOURCE = "sway"
#: Seconds virtualKeyboard waits for sway.
STATE_TIMEOUT = 1.0

_MAGIC = b"i3-ipc"
_HEADER = struct.Struct("=6sII")
SUBSCRIBE = 2
GET_INPUTS = 100
#: The input event: the high bit marks an event, and 21 is input's.
EVENT_INPUT = 0x80000015
#: A reply larger than this is not sway's.
_MAX_PAYLOAD = 16 * 1024 * 1024

#: The interval given with keys that do not repeat, as desktopLayout's KDE feed gives it.
_NO_REPEAT_INTERVAL = 0.04


def _mismatchNotice() -> str:
	# Translators: Said on Linux in a sway session where the keyboard linda-keyd types through has
	# another keyboard layout than the user's keyboard, so keys passed on would type other characters.
	return _("The desktop gives linda-keyd's keyboard another layout than your keyboard.")


def socketPath(currentDesktop: str | None = None) -> str | None:
	"""sway's IPC socket: SWAYSOCK's, else in a session XDG_CURRENT_DESKTOP says is sway's, the one
	socket in the runtime directory whose sway still runs."""
	path = os.environ.get("SWAYSOCK")
	if path:
		return path if _isSocket(path) else None
	desktops = {name.strip().lower() for name in (currentDesktop or "").split(":")}
	if "sway" not in desktops:
		return None
	directory = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
	prefix = f"sway-ipc.{os.getuid()}."
	found = []
	try:
		names = os.listdir(directory)
	except OSError:
		return None
	for name in names:
		if not (name.startswith(prefix) and name.endswith(".sock")):
			continue
		pid = name[len(prefix) : -len(".sock")]
		path = os.path.join(directory, name)
		if pid.isdigit() and os.path.exists(f"/proc/{pid}") and _isSocket(path):
			found.append(path)
	if len(found) != 1:
		log.debug(f"Keyboard layout: {len(found)} sway sockets with a running sway, so none is taken")
		return None
	return found[0]


def _isSocket(path: str) -> bool:
	try:
		return stat.S_ISSOCK(os.stat(path).st_mode)
	except OSError:
		return False


class _Connection:
	"""One connection to sway's IPC."""

	def __init__(self, path: str, timeout: float | None) -> None:
		self._socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM | socket.SOCK_CLOEXEC)
		try:
			self._socket.settimeout(timeout)
			self._socket.connect(path)
		except BaseException:
			self._socket.close()
			raise

	def settimeout(self, timeout: float | None) -> None:
		self._socket.settimeout(timeout)

	def send(self, messageType: int, payload: str = "") -> None:
		data = payload.encode()
		self._socket.sendall(_HEADER.pack(_MAGIC, len(data), messageType) + data)

	def _exactly(self, size: int) -> bytes:
		buffer = bytearray(size)
		view = memoryview(buffer)
		got = 0
		while got < size:
			count = self._socket.recv_into(view[got:])
			if not count:
				raise EOFError("sway closed its IPC connection")
			got += count
		return bytes(buffer)

	def read(self) -> tuple[int, object]:
		magic, size, messageType = _HEADER.unpack(self._exactly(_HEADER.size))
		if magic != _MAGIC or size > _MAX_PAYLOAD:
			raise ValueError("not sway's IPC")
		return messageType, json.loads(self._exactly(size)) if size else None

	def ask(self, messageType: int, payload: str = "") -> object:
		"""Send a request and give its reply, passing over events that come first."""
		self.send(messageType, payload)
		while True:
			replyType, reply = self.read()
			if replyType == messageType:
				return reply

	def shutdown(self) -> None:
		try:
			self._socket.shutdown(socket.SHUT_RDWR)
		except OSError:
			pass

	def close(self) -> None:
		self._socket.close()


_registries: dict[str, dict[str, tuple[str, str]]] = {}
_registryLock = threading.Lock()


def _includePaths() -> list[str]:
	from xkbcommon import xkb

	context = xkb.Context()
	return [context.include_path_get(i) for i in range(context.num_include_paths())]


def registry(rules: str = DEFAULT_RULES) -> dict[str, tuple[str, str]]:
	"""Each description the rules' XML registry gives a layout or a variant, with its layout and
	variant names, from the first include path that has the registry; the first of a description wins.
	Empty where none has it."""
	with _registryLock:
		found = _registries.get(rules)
		if found is None:
			found = _registries[rules] = _readRegistry(rules)
		return found


def _readRegistry(rules: str) -> dict[str, tuple[str, str]]:
	from xml.etree import ElementTree

	descriptions: dict[str, tuple[str, str]] = {}
	for directory in _includePaths():
		main = os.path.join(directory, "rules", f"{rules}.xml")
		if not os.path.isfile(main):
			continue
		for path in (main, os.path.join(directory, "rules", f"{rules}.extras.xml")):
			try:
				root = ElementTree.parse(path).getroot()
			except (OSError, ElementTree.ParseError):
				log.debug(f"Keyboard layout: the registry {os.path.basename(path)} could not be read")
				continue
			for layout in root.iterfind("layoutList/layout"):
				name = layout.findtext("configItem/name")
				if not name:
					continue
				description = layout.findtext("configItem/description")
				if description:
					descriptions.setdefault(description, (name, ""))
				for variant in layout.iterfind("variantList/variant"):
					variantName = variant.findtext("configItem/name")
					description = variant.findtext("configItem/description")
					if variantName and description:
						descriptions.setdefault(description, (name, variantName))
		break
	return descriptions


def _layoutNames(entry: dict) -> list[str]:
	names = entry.get("xkb_layout_names")
	if not isinstance(names, list):
		return []
	return [n if isinstance(n, str) else "" for n in names]


def isHeldKeyboard(entry: dict) -> bool:
	"""Whether a keyboard sway lists may be one linda-keyd holds: a device libinput reads, not a
	program's virtual keyboard, not one of the firmware's buttons, whose vendor is 0, and not
	linda-keyd's own."""
	return "libinput" in entry and bool(entry.get("vendor")) and entry.get("identifier") != VIRTUAL_IDENTIFIER


def _repeat(entry: dict) -> tuple[bool, float, float] | None:
	delay = entry.get("repeat_delay")
	rate = entry.get("repeat_rate")
	if not isinstance(delay, int) or not isinstance(rate, int):
		return None
	return (rate > 0, delay / 1000, 1 / rate if rate > 0 else _NO_REPEAT_INTERVAL)


class _Choice(NamedTuple):
	"""The keyboards of one listing that the snapshot turns on."""

	#: The entry whose layout is the snapshot's.
	entry: dict | None
	#: Whether that entry is linda-keyd's keyboard.
	isVirtual: bool
	#: The layout names of each keyboard linda-keyd may hold.
	held: list[list[str]]


class SwayLayoutFeed:
	"""The layout and group sway gives linda-keyd's keyboard, or its first keyboard until that one is
	listed, followed through sway's input events on a thread of the feed's own."""

	def __init__(
		self,
		path: str,
		*,
		identifier: str = VIRTUAL_IDENTIFIER,
		isHeld: Callable[[dict], bool] = isHeldKeyboard,
	) -> None:
		"""
		:param identifier: The identifier sway gives the keyboard keys are typed through.
		:param isHeld: Whether a keyboard sway lists, other than that one, may be one linda-keyd holds.
		"""
		self._path = path
		self._identifier = identifier
		self._isHeld = isHeld
		self._lock = threading.Lock()
		self._connection: _Connection | None = None
		self._thread: threading.Thread | None = None
		self._onChange: Callable[[LayoutSnapshot], None] | None = None
		self._stopping = False
		self._last: LayoutSnapshot | None = None
		#: The last listing, and its entry the snapshot came from.
		self._inputs: list = []
		self._chosen: dict | None = None
		#: Whether a listing was asked for and has not come.
		self._listing = False
		#: The layout taken in place of descriptions the registry does not have, by those descriptions.
		self._fallbacks: dict[tuple[str, ...], LayoutSnapshot] = {}

	def start(self, onChange: Callable[[LayoutSnapshot], None]) -> LayoutSnapshot:
		if self._connection is not None:
			raise RuntimeError("the layout feed has started already")
		self._onChange = onChange
		registry()
		connection = _Connection(self._path, START_TIMEOUT)
		try:
			reply = connection.ask(SUBSCRIBE, json.dumps(["input"]))
			if not (isinstance(reply, dict) and reply.get("success")):
				raise RuntimeError("sway refused the subscription to input events")
			# Subscribed first, so that no change between the listing and the events is missed.
			inputs = connection.ask(GET_INPUTS)
			snapshot = self._take(inputs)
			connection.settimeout(None)
		except BaseException:
			connection.close()
			raise
		with self._lock:
			self._last = snapshot
		self._connection = connection
		self._thread = threading.Thread(target=self._run, name="lindaKeyboardLayout", daemon=True)
		self._thread.start()
		log.info(f"Keyboard layout from {snapshot.source}: {activeLayoutName(snapshot)}")
		return snapshot

	def stop(self) -> None:
		self._stopping = True
		connection = self._connection
		if connection is None:
			return
		connection.shutdown()
		thread = self._thread
		if thread is not None and thread is not threading.current_thread():
			thread.join(STOP_TIMEOUT)
			if thread.is_alive():
				log.debugWarning("Keyboard layout: the feed's thread did not end in time")
		connection.close()

	def _run(self) -> None:
		connection = self._connection
		try:
			while not self._stopping:
				messageType, payload = connection.read()
				if messageType == GET_INPUTS:
					self._listing = False
					self._publish(self._take(payload))
				elif messageType == EVENT_INPUT and isinstance(payload, dict):
					self._onEvent(connection, payload)
		except (OSError, EOFError, ValueError, TypeError):
			if not self._stopping:
				log.debugWarning("Keyboard layout: sway's IPC connection ended", exc_info=True)
		except Exception:
			log.exception("Keyboard layout: following sway's layout failed")

	def _onEvent(self, connection: _Connection, event: dict) -> None:
		device = event.get("input")
		chosen = self._chosen
		if (
			event.get("change") == "xkb_layout"
			and isinstance(device, dict)
			and chosen is not None
			and device.get("identifier") == chosen.get("identifier")
			and _layoutNames(device) == _layoutNames(chosen)
		):
			# The group alone, as fast as may be: the new state is in the event.
			inputs = [device if entry is chosen else entry for entry in self._inputs]
			self._publish(self._take(inputs))
			return
		if event.get("change") == "xkb_layout":
			# Another keyboard's group, which nothing here turns on.
			return
		# A listing asked for and not yet come is answered after this event was made, so holds it.
		if not self._listing:
			self._listing = True
			connection.send(GET_INPUTS)

	def _choose(self, inputs: list) -> _Choice:
		keyboards = [
			e for e in inputs if isinstance(e, dict) and e.get("type") == "keyboard" and _layoutNames(e)
		]
		virtual = next((e for e in keyboards if e.get("identifier") == self._identifier), None)
		held = [e for e in keyboards if e is not virtual and self._isHeld(e)]
		if virtual is not None:
			return _Choice(virtual, True, [_layoutNames(e) for e in held])
		# Until linda-keyd's keyboard is listed, what the user's keyboard types is the likeliest.
		entry = held[0] if held else keyboards[0] if keyboards else None
		return _Choice(entry, False, [_layoutNames(e) for e in held])

	def _take(self, inputs: object) -> LayoutSnapshot:
		"""The snapshot for a listing, which becomes the one events are applied to."""
		if not isinstance(inputs, list):
			raise TypeError("sway's listing of inputs is not a list")
		choice = self._choose(inputs)
		self._inputs = inputs
		self._chosen = choice.entry
		return self._snapshot(choice)

	def _snapshot(self, choice: _Choice) -> LayoutSnapshot:
		entry = choice.entry
		if entry is None:
			return self._fallback((), 0, None, None)
		descriptions = _layoutNames(entry)
		group = entry.get("xkb_active_layout_index", 0)
		group = group if isinstance(group, int) and 0 <= group < len(descriptions) else 0
		repeat = _repeat(entry)
		notice = None
		if choice.isVirtual and choice.held and descriptions not in choice.held:
			notice = _mismatchNotice()
		known = registry()
		named = [known.get(d) for d in descriptions]
		if None in named:
			return self._fallback(tuple(descriptions), group, repeat, notice)
		env = os.environ
		names = (
			env.get("XKB_DEFAULT_RULES") or DEFAULT_RULES,
			env.get("XKB_DEFAULT_MODEL") or DEFAULT_MODEL,
			",".join(layout for layout, _variant in named),
			",".join(variant for _layout, variant in named),
			env.get("XKB_DEFAULT_OPTIONS", ""),
		)
		return LayoutSnapshot(names, group=group, repeat=repeat, source=SOURCE, notice=notice)

	def _fallback(
		self,
		descriptions: tuple[str, ...],
		group: int,
		repeat: tuple[bool, float, float] | None,
		notice: str | None,
	) -> LayoutSnapshot:
		fallback = self._fallbacks.get(descriptions)
		if fallback is None:
			if descriptions:
				unknown = [d for d in descriptions if d not in registry()]
				log.info(
					f"Keyboard layout: xkbcommon's registry has no layout sway calls {', '.join(unknown)}, "
					"so localed's layout is taken",
				)
			else:
				log.info(
					"Keyboard layout: sway lists no keyboard with a layout, so localed's layout is taken"
				)
			fallback = self._fallbacks[descriptions] = systemSnapshot()
		count = len(fallback.names[2].split(","))
		return fallback._replace(
			group=group if group < count else 0,
			repeat=repeat if repeat is not None else fallback.repeat,
			notice=notice or fallback.notice,
		)

	def _publish(self, snapshot: LayoutSnapshot) -> None:
		with self._lock:
			if snapshot == self._last:
				return
			self._last = snapshot
		if self._stopping:
			return
		log.debug(f"Keyboard layout from {snapshot.source}: {activeLayoutName(snapshot)}")
		try:
			self._onChange(snapshot)
		except Exception:
			log.exception("Keyboard layout: passing a change on failed")


def create(currentDesktop: str | None) -> SwayLayoutFeed | None:
	"""A feed when this is a sway session, for desktopLayout.create; None otherwise."""
	path = socketPath(currentDesktop)
	return None if path is None else SwayLayoutFeed(path)


class VirtualKeyboard(NamedTuple):
	#: Whether sway lists linda-keyd's keyboard.
	listed: bool
	#: Whether sway takes its events, as libinput's send-events mode says; None where sway does not say.
	eventsEnabled: bool | None


def virtualKeyboard(
	path: str | None = None,
	*,
	identifier: str = VIRTUAL_IDENTIFIER,
	timeout: float = STATE_TIMEOUT,
) -> VirtualKeyboard | None:
	"""Whether sway lists linda-keyd's keyboard and takes its events, asked now; None when sway cannot
	be asked."""
	path = path or socketPath()
	if path is None:
		return None
	try:
		connection = _Connection(path, timeout)
		try:
			inputs = connection.ask(GET_INPUTS)
		finally:
			connection.close()
	except (OSError, EOFError, ValueError) as e:
		log.debug(f"Keyboard layout: sway did not list its inputs: {type(e).__name__}")
		return None
	if not isinstance(inputs, list):
		return None
	entry = next(
		(
			e
			for e in inputs
			if isinstance(e, dict) and e.get("type") == "keyboard" and e.get("identifier") == identifier
		),
		None,
	)
	if entry is None:
		return VirtualKeyboard(False, None)
	libinput = entry.get("libinput")
	mode = libinput.get("send_events") if isinstance(libinput, dict) else None
	return VirtualKeyboard(True, {"enabled": True, "disabled": False}.get(mode))
