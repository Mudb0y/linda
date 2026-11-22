"""The desktop's clipboard for api.copyToClip and api.getClipData, through GTK, which wx already runs.

GTK serves what NVDA copies from the main loop for as long as NVDA owns the clipboard, as
Windows serves it from its own clipboard. Reading asks the owner, another process, for its text,
and GTK hands the answer to the main loop; NVDA's callers want the text back at once, so the main
loop is run here until it comes, but only for a moment, since an owner that is hung or gone
would otherwise keep NVDA waiting as long as GTK waits for it, half a minute.

On a Wayland display GTK's clipboard cannot be trusted. A program may set the clipboard only while
one of its windows has the keyboard, which a screen reader's almost never has: Mutter drops the copy,
wlroots ignores it without a word, and either way GTK goes on answering NVDA's own read-back from its
own copy, so a copy nobody else can paste would be reported as made. And a program hears of a new
clipboard only while it has the keyboard, so what GTK would read is whatever was there when NVDA last
had it. So there NVDA speaks the protocol itself on a connection of its own. Where the compositor
offers data control (sway, KWin and the other wlroots compositors), the clipboard is read and set
through it with no window at all. Where it does not, as on GNOME, NVDA maps a window of its own,
which the compositor gives the keyboard as it maps, sets or reads the clipboard through it and closes
it: a window made on that connection rather than through GTK, so that it is on no accessibility bus
and NVDA does not read its own window coming and going. A copy counts as made only once the text has
come back through the compositor from the clipboard it now names. Where neither works, NVDA says it
cannot copy or cannot read, instead of claiming a copy or reading stale text. On an X display
everything here does what it did before.
"""

import array
import collections
import os
import select
import socket
import struct
import threading
import time
from collections.abc import Callable

from logHandler import log

#: Seconds to wait for the clipboard's owner to give its text.
READ_TIMEOUT = 1.0
#: Seconds a window of NVDA's own has to be given the keyboard as it maps, on a Wayland display whose
#: compositor offers no data control.
FOCUS_TIMEOUT = 1.0
#: Seconds the compositor has to answer a request on NVDA's own connection.
ROUND_TRIP_TIMEOUT = 1.0
#: Seconds another program has to take what NVDA copied, once it asks, before NVDA stops writing.
SEND_TIMEOUT = 5.0
#: How many times a copy is read back while the clipboard changes under the read, as it does when a
#: clipboard manager takes the copy over as NVDA reads it.
_READS_OF_A_COPY = 3

#: The data control managers NVDA can use, the standard one first, with the prefix of their objects.
DATA_CONTROL_MANAGERS = (
	("ext_data_control_manager_v1", "ext_data_control"),
	("zwlr_data_control_manager_v1", "zwlr_data_control"),
)
#: The types NVDA offers what it copies as, which are those GTK offers text as.
OFFERED_TYPES = ("text/plain;charset=utf-8", "UTF8_STRING", "text/plain", "TEXT", "STRING")
#: The types NVDA reads text as, best first, and whether each is surely UTF-8.
READ_TYPES = (
	("text/plain;charset=utf-8", True),
	("UTF8_STRING", True),
	("text/plain", False),
	("TEXT", False),
	("STRING", False),
)


def _gtk():
	# Imported only once wx is running GTK: importing Gtk initialises GTK, which must not happen
	# before wx.App exists.
	import gi

	gi.require_version("Gtk", "3.0")
	gi.require_version("Gdk", "3.0")
	from gi.repository import Gdk, GLib, Gtk

	return Gtk, Gdk, GLib


def _clipboard():
	Gtk, Gdk, _GLib = _gtk()
	clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
	if clipboard is None:
		raise OSError("there is no clipboard: GTK has no display")
	return clipboard


def _requireMainThread() -> None:
	import core

	# GTK, like the Windows clipboard NVDA opens with its main window, belongs to the main thread.
	if threading.get_ident() != core.mainThreadId:
		raise OSError("the clipboard is only used from NVDA's main thread")


def _onWaylandDisplay() -> bool:
	from linuxPlatform import ownGui

	return ownGui.isWaylandDisplay()


class ClipboardRefused(OSError):
	"""Neither the compositor nor a window of NVDA's own lets NVDA use the clipboard. getText says so to
	the user before it raises this."""


def _cannotCopy() -> str:
	# Translators: Said on Linux when NVDA was asked to copy text and the desktop let it put nothing on
	# the clipboard, as can happen in a Wayland session.
	return _("NVDA cannot copy to the clipboard in this session")


def _cannotRead() -> str:
	# Translators: Said on Linux when NVDA was asked for the clipboard's text and the desktop would not
	# let it read the clipboard, as can happen in a Wayland session.
	return _("NVDA cannot read the clipboard here")


def setText(text: str) -> None:
	"""Own the clipboard with text, which GTK serves to any program that asks while NVDA owns it.

	:raises OSError: When GTK cannot take the clipboard.
	"""
	_requireMainThread()
	if _onWaylandDisplay():
		if not _wayland().copy(text):
			raise ClipboardRefused("the compositor took nothing NVDA could put on the clipboard")
		return
	clipboard = _clipboard()
	try:
		clipboard.set_text(text, -1)
	except (TypeError, ValueError) as e:
		# A NUL, which neither GTK's text nor Windows' CF_UNICODETEXT can carry.
		raise OSError(f"the clipboard cannot hold this text: {e}") from e
	# A clipboard manager, where the desktop runs one, keeps it once NVDA exits.
	clipboard.set_can_store(None)


def getText(timeout: float = READ_TIMEOUT) -> str:
	"""The clipboard's text, or "" when it holds none.

	:param timeout: Seconds to wait for the clipboard's owner.
	:raises OSError: When the owner has not answered within timeout.
	:raises ClipboardRefused: On a Wayland display that let NVDA read no clipboard, once NVDA has said so.
	"""
	_requireMainThread()
	if _onWaylandDisplay():
		return _readOnWayland(timeout)
	_Gtk, _Gdk, GLib = _gtk()
	clipboard = _clipboard()
	answer: list[str | None] = []

	def received(_clipboard, text: str | None, *_data) -> None:
		answer.append(text)

	clipboard.request_text(received)
	if not answer:
		_runMainLoopUntil(lambda: bool(answer), timeout, GLib)
	if not answer:
		log.debugWarning(f"The clipboard's owner did not give its text within {timeout} s")
		raise OSError("the clipboard's owner did not answer")
	return answer[0] or ""


def _runMainLoopUntil(done, timeout: float, GLib) -> None:
	"""Run the main loop's sources until done() or until timeout seconds have passed.

	The source being dispatched, such as the timer of the core pump that runs NVDA's scripts, is not
	run again inside it, as GLib does not recurse into a source unless it asks.
	"""
	context = GLib.MainContext.default()
	deadline = time.monotonic() + timeout
	expired = []

	def expire() -> bool:
		expired.append(True)
		return GLib.SOURCE_REMOVE

	# Wakes the blocking iteration below once time is up, whatever else happens.
	wakeUp = GLib.timeout_add(max(1, int(timeout * 1000)), expire)
	try:
		while not done() and not expired and time.monotonic() < deadline:
			context.iteration(True)
	finally:
		if not expired:
			GLib.source_remove(wakeUp)


def copyToClip(text: str, notify: bool | None = False) -> bool:
	"""api.copyToClip on Linux: the same contract, with the copy checked by reading it back, on a
	Wayland display through the compositor."""
	import ui

	if not isinstance(text, str) or len(text) == 0:
		return False
	# NVDA joins copied lines with Windows' line ends, which GTK hands out as Linux's, so the copy
	# would never read back as it went in.
	text = text.replace("\r\n", "\n")
	if _onWaylandDisplay():
		return _copyOnWayland(text, notify)
	try:
		setText(text)
		got = getText()
	except OSError:
		log.debugWarning("Could not copy to the clipboard", exc_info=True)
		if notify:
			ui.reportTextCopiedToClipboard()  # No argument reports a failure.
		return False
	if got == text:
		if notify:
			ui.reportTextCopiedToClipboard(text)
		return True
	if notify:
		ui.reportTextCopiedToClipboard()  # No argument reports a failure.
	return False


# On a Wayland display.


def _copyOnWayland(text: str, notify: bool | None) -> bool:
	import ui

	try:
		_requireMainThread()
		copied = _wayland().copy(text)
	except OSError:
		log.debugWarning("Could not copy to the clipboard", exc_info=True)
		copied = False
	except Exception:
		# Whatever went wrong, the user hears that nothing was copied, as on X, where every fault comes
		# back as False.
		log.exception("Could not copy to the clipboard")
		copied = False
	if copied:
		if notify:
			ui.reportTextCopiedToClipboard(text)
		return True
	# Said whether or not the caller asked, since several of NVDA's callers say the text was copied
	# without looking at the answer.
	ui.message(_cannotCopy())
	return False


def _readOnWayland(timeout: float) -> str:
	import ui

	try:
		route = _wayland()
	except OSError as e:
		log.debugWarning("Could not reach the compositor for the clipboard", exc_info=True)
		ui.message(_cannotRead())
		raise ClipboardRefused("NVDA cannot reach the compositor") from e
	text = route.read(timeout)
	if text is None:
		ui.message(_cannotRead())
		raise ClipboardRefused("the compositor let NVDA read no clipboard")
	return text


_route: "_DataControlRoute | _WindowRoute | None" = None


def _wayland() -> "_DataControlRoute | _WindowRoute":
	"""The way to the clipboard on this display, chosen on first use and again if the connection is lost.

	:raises OSError: When NVDA cannot connect to the compositor at all.
	"""
	global _route
	if _route is not None and _route.wire.alive:
		return _route
	from linuxPlatform import displayServer

	path = displayServer.waylandSocket()
	if path is None:
		raise ClipboardRefused("there is no Wayland socket to reach the compositor by")
	wire = _Wire(path)
	try:
		for managerName, prefix in DATA_CONTROL_MANAGERS:
			if managerName in wire.globals:
				_route = _DataControlRoute(wire, managerName, prefix)
				log.info(f"The clipboard is used through the compositor's {managerName}")
				break
		else:
			_route = _WindowRoute(wire)
			log.info(
				"The compositor offers no data control: the clipboard is used from a window of NVDA's own"
			)
	except BaseException:
		wire.close()
		raise
	return _route


# The Wayland protocol, as far as the clipboard needs it.

_HEADER = struct.Struct("=II")
#: The display object, which every connection has as object 1.
_DISPLAY = 1
#: Ancillary room for the descriptors one read may bring; libwayland sends at most 28 a message.
_FD_ROOM = socket.CMSG_SPACE(64 * array.array("i").itemsize)
#: wl_shm's format of a pixel, ARGB with eight bits each.
_SHM_ARGB8888 = 0

#: Each interface's events by opcode, as the argument types of the protocol's wire format: u and i
#: integers, f fixed point, s a string, a an array, o an object, n a new object, h a descriptor. Each
#: is bound at the version these list, so that no event beyond them can come.
_EVENTS = {
	"wl_display": ("ous", "u"),
	"wl_registry": ("usu", "u"),
	"wl_callback": ("u",),
	"wl_seat": ("u",),
	"wl_keyboard": ("uhu", "uoa", "uo", "uuuu", "uuuuu"),
	"wl_compositor": (),
	"wl_surface": ("o", "o"),
	"wl_shm": ("u",),
	"wl_shm_pool": (),
	"wl_buffer": ("",),
	"xdg_wm_base": ("u",),
	"xdg_surface": ("u",),
	"xdg_toplevel": ("iia", ""),
	"wl_data_device_manager": (),
	"wl_data_device": ("n", "uoffo", "", "uff", "", "o"),
	"wl_data_source": ("s", "sh", ""),
	"wl_data_offer": ("s",),
}
for _manager, _prefix in DATA_CONTROL_MANAGERS:
	_EVENTS[_manager] = ()
	_EVENTS[f"{_prefix}_device_v1"] = ("n", "o", "", "o")
	_EVENTS[f"{_prefix}_source_v1"] = ("sh", "")
	_EVENTS[f"{_prefix}_offer_v1"] = ("s",)


def _uint(value: int) -> bytes:
	return struct.pack("=I", value)


def _int(value: int) -> bytes:
	return struct.pack("=i", value)


def _string(value: str) -> bytes:
	data = value.encode("utf-8") + b"\0"
	return _uint(len(data)) + data + b"\0" * (-len(data) % 4)


def _parse(signature: str, payload: bytes, fds: collections.deque) -> list:
	arguments: list = []
	offset = 0
	for kind in signature:
		if kind == "h":
			arguments.append(fds.popleft() if fds else -1)
		elif kind in "sa":
			(length,) = struct.unpack_from("=I", payload, offset)
			offset += 4
			data = payload[offset : offset + length]
			offset += length + (-length % 4)
			if kind == "a":
				arguments.append(data)
			else:
				arguments.append(data[:-1].decode("utf-8", "replace") if length else None)
		else:
			(value,) = struct.unpack_from("=i" if kind in "if" else "=I", payload, offset)
			offset += 4
			arguments.append(value)
	return arguments


Handler = Callable[[int, list], None]


def _ignore(_opcode: int, _arguments: list) -> None:
	pass


class _Wire:
	"""A connection of NVDA's own to the compositor, read by a thread of its own.

	Events are handled on that thread with the condition held, and whoever waits on what they bring
	waits on the condition, never long: every wait here is bounded.
	"""

	def __init__(self, path: str) -> None:
		self._socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM | socket.SOCK_CLOEXEC)
		try:
			self._socket.connect(path)
		except OSError:
			self._socket.close()
			raise
		self.alive = True
		self.condition = threading.Condition()
		self._sendLock = threading.Lock()
		self._objects: dict[int, tuple[str, Handler]] = {_DISPLAY: ("wl_display", self._onDisplay)}
		self._nextId = 2
		self._freeIds: list[int] = []
		#: Each global the compositor offers, by interface, as its name and version.
		self.globals: dict[str, tuple[int, int]] = {}
		self._thread = threading.Thread(target=self._read, name="clipboard connection", daemon=True)
		self._thread.start()
		self._registry = self.new("wl_registry", self._onRegistry)
		self.send(_DISPLAY, 1, _uint(self._registry))
		if not self.roundTrip():
			self.close()
			raise OSError("the compositor did not answer NVDA's connection")

	def close(self) -> None:
		with self.condition:
			self.alive = False
			self.condition.notify_all()
		try:
			self._socket.shutdown(socket.SHUT_RDWR)
		except OSError:
			pass
		self._socket.close()

	def new(self, interface: str, handler: Handler = _ignore) -> int:
		"""A new object's number, which the request that makes it must then name."""
		with self.condition:
			if self._freeIds:
				objectId = self._freeIds.pop()
			else:
				objectId = self._nextId
				self._nextId += 1
			self._objects[objectId] = (interface, handler)
			return objectId

	def adopt(self, objectId: int, interface: str, handler: Handler = _ignore) -> None:
		"""Handle the events of an object the compositor made."""
		self._objects[objectId] = (interface, handler)

	def send(self, objectId: int, opcode: int, payload: bytes = b"", fds: tuple[int, ...] = ()) -> None:
		message = _HEADER.pack(objectId, (_HEADER.size + len(payload)) << 16 | opcode) + payload
		with self._sendLock:
			if not self.alive:
				raise OSError("NVDA's connection to the compositor is closed")
			try:
				if fds:
					ancillary = [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", fds))]
					sent = self._socket.sendmsg([message], ancillary)
					if sent < len(message):
						self._socket.sendall(message[sent:])
				else:
					self._socket.sendall(message)
			except OSError:
				self.alive = False
				raise

	def bind(self, interface: str, version: int, handler: Handler = _ignore) -> int:
		""":raises ClipboardRefused: When the compositor offers no such global, such as a seat."""
		with self.condition:
			found = self.globals.get(interface)
		if found is None:
			raise ClipboardRefused(f"the compositor offers no {interface}")
		name, offered = found
		objectId = self.new(interface, handler)
		self.send(
			self._registry,
			0,
			_uint(name) + _string(interface) + _uint(min(version, offered)) + _uint(objectId),
		)
		return objectId

	def waitFor(self, condition: Callable[[], object], timeout: float) -> bool:
		"""Wait until condition, called with the condition held, is true, or the connection ends."""
		with self.condition:
			self.condition.wait_for(lambda: condition() or not self.alive, timeout)
			return bool(condition())

	def roundTrip(self, timeout: float = ROUND_TRIP_TIMEOUT) -> bool:
		"""Whether the compositor has answered every request sent before this, and NVDA has handled
		every event it sent before that answer."""
		done: list[bool] = []
		callback = self.new("wl_callback", lambda _opcode, _arguments: done.append(True))
		self.send(_DISPLAY, 0, _uint(callback))
		return self.waitFor(lambda: done, timeout)

	def receive(self, offer: int, opcode: int, mimeType: str) -> int:
		"""Ask the clipboard's owner to write offer as mimeType; the end of the pipe to read it from,
		which the caller closes."""
		readEnd, writeEnd = os.pipe2(os.O_CLOEXEC)
		try:
			self.send(offer, opcode, _string(mimeType), fds=(writeEnd,))
		except BaseException:
			os.close(readEnd)
			raise
		finally:
			os.close(writeEnd)
		return readEnd

	def _onDisplay(self, opcode: int, arguments: list) -> None:
		if opcode == 0:
			objectId, code, message = arguments
			log.debugWarning(
				f"The compositor ended NVDA's connection: object {objectId}, code {code}: {message}"
			)
			self.alive = False
		elif opcode == 1:
			(objectId,) = arguments
			if self._objects.pop(objectId, None) is not None:
				self._freeIds.append(objectId)

	def _onRegistry(self, opcode: int, arguments: list) -> None:
		if opcode == 0:
			name, interface, version = arguments
			self.globals[interface] = (name, version)
		elif opcode == 1:
			(name,) = arguments
			for interface, (globalName, _version) in list(self.globals.items()):
				if globalName == name:
					del self.globals[interface]

	def _read(self) -> None:
		data = b""
		fds: collections.deque[int] = collections.deque()
		try:
			while True:
				chunk, ancillary, _flags, _address = self._socket.recvmsg(
					65536, _FD_ROOM, socket.MSG_CMSG_CLOEXEC
				)
				if not chunk:
					break
				for level, kind, payload in ancillary:
					if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
						received = array.array("i")
						received.frombytes(payload[: len(payload) - len(payload) % received.itemsize])
						fds.extend(received)
				data += chunk
				while len(data) >= _HEADER.size:
					objectId, word = _HEADER.unpack_from(data)
					size = word >> 16
					if size < _HEADER.size:
						raise OSError(f"a message of {size} bytes")
					if len(data) < size:
						break
					payload, data = data[_HEADER.size : size], data[size:]
					self._dispatch(objectId, word & 0xFFFF, payload, fds)
		except (OSError, struct.error, IndexError):
			if self.alive:
				log.debugWarning("NVDA's connection to the compositor failed", exc_info=True)
		finally:
			with self.condition:
				self.alive = False
				self.condition.notify_all()
			for fd in fds:
				os.close(fd)

	def _dispatch(self, objectId: int, opcode: int, payload: bytes, fds: collections.deque) -> None:
		with self.condition:
			interface, handler = self._objects.get(objectId, (None, _ignore))
			if interface is None:
				# Only an object NVDA has never had can get here, and none of those has a descriptor.
				return
			signature = _EVENTS[interface][opcode]
			arguments = _parse(signature, payload, fds)
			try:
				handler(opcode, arguments)
			except (OSError, LookupError, ValueError, TypeError):
				log.debugWarning(
					f"Could not handle the compositor's {interface} event {opcode}", exc_info=True
				)
			finally:
				for kind, value in zip(signature, arguments):
					# Every descriptor a handler wanted it took a duplicate of.
					if kind == "h" and value >= 0:
						os.close(value)
			self.condition.notify_all()


def _readAll(fd: int, timeout: float) -> bytes:
	deadline = time.monotonic() + timeout
	poller = select.poll()
	poller.register(fd, select.POLLIN)
	chunks = []
	while True:
		remaining = deadline - time.monotonic()
		if remaining <= 0 or not poller.poll(remaining * 1000):
			raise OSError("the clipboard's owner did not answer")
		chunk = os.read(fd, 65536)
		if not chunk:
			return b"".join(chunks)
		chunks.append(chunk)


def _serve(fd: int, data: bytes) -> None:
	"""Write data to fd and close it, on a thread of its own, so that a program that asks and never
	reads holds nothing of NVDA's but that thread, and that for SEND_TIMEOUT at most."""

	def write() -> None:
		try:
			os.set_blocking(fd, False)
			poller = select.poll()
			poller.register(fd, select.POLLOUT)
			view = memoryview(data)
			deadline = time.monotonic() + SEND_TIMEOUT
			while view:
				remaining = deadline - time.monotonic()
				if remaining <= 0 or not poller.poll(remaining * 1000):
					break
				try:
					view = view[os.write(fd, view) :]
				except BlockingIOError:
					continue
		except OSError:
			# A program that closed its end before taking everything.
			pass
		finally:
			os.close(fd)

	threading.Thread(target=write, name="clipboard send", daemon=True).start()


def _encode(text: str) -> bytes:
	try:
		return text.encode("utf-8")
	except UnicodeEncodeError:
		# A lone surrogate, which no program could paste. The error itself is not kept, as it quotes the
		# text.
		raise OSError("the clipboard cannot hold this text: it is not valid Unicode") from None


def _decode(data: bytes, isUtf8: bool) -> str:
	if isUtf8:
		return data.decode("utf-8", "replace")
	try:
		return data.decode("utf-8")
	except UnicodeDecodeError:
		return data.decode("latin-1")


class _Selection:
	"""The clipboard as one data device hears of it, and what NVDA has put there: what both routes
	share. Its handlers run on the connection's thread."""

	#: The opcodes, on this route's interfaces, of the requests and events that differ between them.
	_OFFER_RECEIVE: int
	_OFFER_DESTROY: int
	_SOURCE_OFFER = 0
	_SOURCE_DESTROY = 1
	_SOURCE_SEND: int
	_SOURCE_CANCELLED: int
	_DEVICE_DATA_OFFER = 0
	_DEVICE_SELECTION: int

	def __init__(self, wire: _Wire, offerInterface: str, sourceInterface: str) -> None:
		self.wire = wire
		self._offerInterface = offerInterface
		self._sourceInterface = sourceInterface
		#: Each offer the compositor has made and NVDA has not destroyed, with the types it offers.
		self._offers: dict[int, list[str]] = {}
		#: The offer the clipboard is, as the compositor last said, or None for an empty clipboard.
		self._selection: int | None = None
		#: How many times the compositor has said what the clipboard is.
		self._selections = 0
		#: NVDA's own source and the text it serves, while the clipboard is NVDA's.
		self._source: tuple[int, bytes] | None = None
		#: Every source NVDA has made and not yet destroyed.
		self._liveSources: set[int] = set()

	def _onDeviceEvent(self, opcode: int, arguments: list, primary: int | None = None) -> None:
		if opcode == self._DEVICE_DATA_OFFER:
			(offer,) = arguments
			self._offers[offer] = []
			self.wire.adopt(offer, self._offerInterface, lambda _opcode, args: self._noteType(offer, args[0]))
		elif opcode == self._DEVICE_SELECTION:
			(offer,) = arguments
			self._selection = offer or None
			self._selections += 1
			self._forgetOffers()
		elif opcode == primary:
			# The primary selection, which NVDA does not use.
			(offer,) = arguments
			if offer:
				self._destroyOffer(offer)

	def _noteType(self, offer: int, mimeType: str) -> None:
		types = self._offers.get(offer)
		if types is not None:
			types.append(mimeType)

	def _forgetOffers(self) -> None:
		for offer in list(self._offers):
			if offer != self._selection:
				self._destroyOffer(offer)

	def _destroyOffer(self, offer: int) -> None:
		if self._offers.pop(offer, None) is not None:
			self.wire.send(offer, self._OFFER_DESTROY)

	def _onSourceEvent(self, source: int, opcode: int, arguments: list) -> None:
		current = self._source is not None and self._source[0] == source
		if opcode == self._SOURCE_SEND:
			_mimeType, fd = arguments
			if current and fd >= 0:
				_serve(os.dup(fd), self._source[1])
		elif opcode == self._SOURCE_CANCELLED:
			if current:
				self._source = None
			self._destroySource(source)

	def _destroySource(self, source: int) -> None:
		# Once only: a source NVDA replaced may still be cancelled by an event already on its way, and a
		# request to a destroyed object ends the connection.
		with self.wire.condition:
			if source not in self._liveSources:
				return
			self._liveSources.discard(source)
		self.wire.send(source, self._SOURCE_DESTROY)

	def _offerSource(self, data: bytes) -> int:
		"""A new source for data, which becomes NVDA's own; the one before it is destroyed."""
		source = self.wire.new(
			self._sourceInterface,
			lambda opcode, arguments: self._onSourceEvent(source, opcode, arguments),
		)
		with self.wire.condition:
			self._liveSources.add(source)
		self.wire.send(self._manager, 0, _uint(source))
		for mimeType in OFFERED_TYPES:
			self.wire.send(source, self._SOURCE_OFFER, _string(mimeType))
		with self.wire.condition:
			old, self._source = self._source, (source, data)
		if old is not None:
			self._destroySource(old[0])
		return source

	def _isCopied(self, text: str, selectionsBefore: int, timeout: float) -> bool:
		"""Whether the clipboard has been set since selectionsBefore, by the compositor's word, and
		holds text as it comes back through the compositor.

		The clipboard need not still be NVDA's own source: a clipboard manager, such as Plasma's, may
		have taken the text over already, and the copy is as good.
		"""
		wire = self.wire
		if not wire.roundTrip(timeout):
			return False
		if not wire.waitFor(lambda: self._selections > selectionsBefore, timeout):
			log.debugWarning("The compositor did not take NVDA's copy: it never said what the clipboard is")
			return False
		for _attempt in range(_READS_OF_A_COPY):
			got, readFrom = self._readSelectionAndCount(timeout)
			if got == text:
				return True
			# A clipboard manager that took the copy over as it was read leaves the offer read empty, and
			# what it put in its place is the copy; the compositor's word of that may still be on its way.
			if not wire.roundTrip(timeout):
				return False
			with wire.condition:
				if self._selections == readFrom:
					break
		log.debugWarning("The compositor did not take NVDA's copy: the clipboard holds something else")
		return False

	def _readSelection(self, timeout: float) -> str:
		"""The text of the clipboard as the compositor last named it, or "" when it holds none."""
		return self._readSelectionAndCount(timeout)[0]

	def _readSelectionAndCount(self, timeout: float) -> tuple[str, int]:
		"""_readSelection's text, and how many times the compositor had said what the clipboard is when
		NVDA chose what to read."""
		with self.wire.condition:
			offer = self._selection
			count = self._selections
			types = self._offers.get(offer, ()) if offer is not None else ()
			chosen = next(((t, isUtf8) for t, isUtf8 in READ_TYPES if t in types), None)
			if chosen is None:
				return "", count
			# Asked with the condition held, since the connection's thread destroys an offer once the
			# clipboard moves on, and a request to a destroyed object ends the connection.
			readEnd = self.wire.receive(offer, self._OFFER_RECEIVE, chosen[0])
		try:
			return _decode(_readAll(readEnd, timeout), chosen[1]), count
		finally:
			os.close(readEnd)


class _DataControlRoute(_Selection):
	"""The clipboard through the compositor's data control, which needs no window and no keyboard."""

	_OFFER_RECEIVE = 0
	_OFFER_DESTROY = 1
	_SOURCE_SEND = 0
	_SOURCE_CANCELLED = 1
	_DEVICE_SELECTION = 1
	_DEVICE_PRIMARY_SELECTION = 3
	_DEVICE_SET_SELECTION = 0

	def __init__(self, wire: _Wire, managerName: str, prefix: str) -> None:
		super().__init__(wire, f"{prefix}_offer_v1", f"{prefix}_source_v1")
		self.managerName = managerName
		seat = wire.bind("wl_seat", 1)
		self._manager = wire.bind(managerName, 1)
		self._device = wire.new(
			f"{prefix}_device_v1",
			lambda opcode, arguments: self._onDeviceEvent(opcode, arguments, self._DEVICE_PRIMARY_SELECTION),
		)
		wire.send(self._manager, 1, _uint(self._device) + _uint(seat))
		if not wire.roundTrip():
			raise OSError("the compositor did not answer NVDA's data control")

	def copy(self, text: str) -> bool:
		data = _encode(text)
		with self.wire.condition:
			before = self._selections
		source = self._offerSource(data)
		self.wire.send(self._device, self._DEVICE_SET_SELECTION, _uint(source))
		return self._isCopied(text, before, ROUND_TRIP_TIMEOUT)

	def read(self, timeout: float) -> str:
		if not self.wire.roundTrip():
			raise OSError("the compositor did not answer")
		return self._readSelection(timeout)


class _WindowRoute(_Selection):
	"""The clipboard from a window of NVDA's own, made on this connection, while it has the keyboard.

	While one of NVDA's wx windows has the keyboard, GTK's own clipboard is current and is used instead,
	so that the window the user is in keeps the keyboard.
	"""

	_OFFER_RECEIVE = 1
	_OFFER_DESTROY = 2
	_SOURCE_SEND = 1
	_SOURCE_CANCELLED = 2
	_DEVICE_SELECTION = 5
	_DEVICE_SET_SELECTION = 1

	def __init__(self, wire: _Wire) -> None:
		super().__init__(wire, "wl_data_offer", "wl_data_source")
		missing = [
			name
			for name in ("wl_compositor", "wl_shm", "xdg_wm_base", "wl_seat", "wl_data_device_manager")
			if name not in wire.globals
		]
		if missing:
			raise ClipboardRefused(f"the compositor offers no {', '.join(missing)}")
		self._compositor = wire.bind("wl_compositor", 1)
		self._shell = wire.bind("xdg_wm_base", 1, self._onShell)
		seat = wire.bind("wl_seat", 1)
		wire.send(seat, 1, _uint(wire.new("wl_keyboard", self._onKeyboard)))
		self._manager = wire.bind("wl_data_device_manager", 1)
		self._device = wire.new("wl_data_device", self._onDeviceEvent)
		wire.send(self._manager, 1, _uint(self._device) + _uint(seat))
		self._buffer = self._pixel(wire.bind("wl_shm", 1))
		#: The window's surface while it is open, and the serial of the keyboard's arrival in it.
		self._surface: int | None = None
		self._keyboardSerial: int | None = None
		if not wire.roundTrip():
			raise OSError("the compositor did not answer NVDA's connection")

	def _pixel(self, shm: int) -> int:
		"""A buffer of one pixel, which is all a window needs to be shown."""
		fd = os.memfd_create("linda-clipboard", os.MFD_CLOEXEC)
		try:
			os.ftruncate(fd, 4)
			pool = self.wire.new("wl_shm_pool")
			self.wire.send(shm, 0, _uint(pool) + _int(4), fds=(fd,))
		finally:
			os.close(fd)
		buffer = self.wire.new("wl_buffer")
		self.wire.send(pool, 0, _uint(buffer) + _int(0) + _int(1) + _int(1) + _int(4) + _uint(_SHM_ARGB8888))
		self.wire.send(pool, 1)
		return buffer

	def _onShell(self, opcode: int, arguments: list) -> None:
		if opcode == 0:
			self.wire.send(self._shell, 3, _uint(arguments[0]))

	def _onKeyboard(self, opcode: int, arguments: list) -> None:
		if opcode == 1:
			serial, surface, _keys = arguments
			if surface == self._surface:
				self._keyboardSerial = serial
		elif opcode == 2:
			_serial, surface = arguments
			if surface == self._surface:
				self._keyboardSerial = None

	def _onConfigure(self, surface: int, xdgSurface: int, opcode: int, arguments: list) -> None:
		if opcode == 0 and surface == self._surface:
			self.wire.send(xdgSurface, 4, _uint(arguments[0]))
			self.wire.send(surface, 1, _uint(self._buffer) + _int(0) + _int(0))
			self.wire.send(surface, 6)

	def _withKeyboard(self, action: Callable[[int], object]) -> object:
		"""action's answer, given the serial of the keyboard's arrival in a window of NVDA's own,
		mapped for it and closed after it; refused when the compositor does not give it the keyboard
		within FOCUS_TIMEOUT."""
		wire = self.wire
		surface = wire.new("wl_surface")
		wire.send(self._compositor, 0, _uint(surface))
		with wire.condition:
			self._surface = surface
			self._keyboardSerial = None
		xdgSurface = wire.new(
			"xdg_surface",
			lambda opcode, arguments: self._onConfigure(surface, xdgSurface, opcode, arguments),
		)
		toplevel = wire.new("xdg_toplevel")
		try:
			wire.send(self._shell, 2, _uint(xdgSurface) + _uint(surface))
			wire.send(xdgSurface, 1, _uint(toplevel))
			wire.send(toplevel, 2, _string("Linda"))
			wire.send(surface, 6)
			if not wire.waitFor(lambda: self._keyboardSerial is not None, FOCUS_TIMEOUT):
				raise ClipboardRefused(
					f"the compositor did not give NVDA's window the keyboard within {FOCUS_TIMEOUT} s"
				)
			# The compositor says what the clipboard is as the keyboard arrives, after the arrival or
			# before it.
			if not wire.roundTrip():
				raise OSError("the compositor did not answer")
			with wire.condition:
				serial = self._keyboardSerial
			if serial is None:
				# The user moved the keyboard on in that moment, with Alt+Tab or the Super key.
				raise ClipboardRefused("the compositor took the keyboard from NVDA's window again")
			return action(serial)
		finally:
			with wire.condition:
				self._surface = None
				self._keyboardSerial = None
			if wire.alive:
				wire.send(toplevel, 0)
				wire.send(xdgSurface, 0)
				wire.send(surface, 0)

	def copy(self, text: str) -> bool:
		if _ownWindowHasKeyboard():
			return _copyWithGtk(text)
		data = _encode(text)

		def put(serial: int) -> bool:
			with self.wire.condition:
				before = self._selections
			source = self._offerSource(data)
			self.wire.send(self._device, self._DEVICE_SET_SELECTION, _uint(source) + _uint(serial))
			return self._isCopied(text, before, ROUND_TRIP_TIMEOUT)

		try:
			return bool(self._withKeyboard(put))
		except ClipboardRefused:
			log.debugWarning("Could not copy to the clipboard", exc_info=True)
			return False

	def read(self, timeout: float) -> str | None:
		if _ownWindowHasKeyboard():
			return _getTextThroughGtk(timeout)
		try:
			return self._withKeyboard(lambda _serial: self._readSelection(timeout))
		except ClipboardRefused:
			log.debugWarning("Could not read the clipboard", exc_info=True)
			return None


def _ownWindowHasKeyboard() -> bool:
	"""Whether one of NVDA's wx windows has the keyboard, so that GTK hears of the clipboard as it is
	and may set it."""
	import wx

	return any(window.IsShown() and window.IsActive() for window in wx.GetTopLevelWindows())


def _copyWithGtk(text: str) -> bool:
	"""Copy through GTK from a window of NVDA's that has the keyboard, which the compositor lets set the
	clipboard; it is answered only by GTK's own copy once the compositor has had the request, since a
	refusal would have taken that copy away."""
	_Gtk, Gdk, _GLib = _gtk()
	clipboard = _clipboard()
	try:
		clipboard.set_text(text, -1)
	except (TypeError, ValueError):
		log.debugWarning("The clipboard cannot hold this text", exc_info=True)
		return False
	clipboard.set_can_store(None)
	Gdk.Display.get_default().sync()
	try:
		return _getTextThroughGtk(READ_TIMEOUT) == text
	except OSError:
		log.debugWarning("Could not read back what was copied", exc_info=True)
		return False


def _getTextThroughGtk(timeout: float) -> str:
	"""GTK's clipboard text, which is current only while a window of NVDA's has the keyboard."""
	_Gtk, _Gdk, GLib = _gtk()
	clipboard = _clipboard()
	answer: list[str | None] = []
	clipboard.request_text(lambda _clipboard, text, *_data: answer.append(text))
	if not answer:
		_runMainLoopUntil(lambda: bool(answer), timeout, GLib)
	if not answer:
		raise OSError("the clipboard's owner did not answer")
	return answer[0] or ""
