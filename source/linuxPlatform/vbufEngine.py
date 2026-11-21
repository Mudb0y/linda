"""The engine's document renderer (engine/crates/vbufengine), loaded into NVDA's process.

It walks a document over its own peer-to-peer connection to the application and returns the render
stream libnvdaVbuf builds, as UTF-16 code units in the engine's memory, which linuxPlatform.vbufLib
reads without a copy. It then watches the document, and next hands out each change as an update
batch for the buffer. Its work runs on a thread of its own; ctypes releases the GIL for the whole of
each call, so render and next block only the thread that calls them. Found through
LINDA_VBUF_ENGINE or the path an installation fixed for it (linuxPlatform.installSettings), never on
the library path.
"""

import ctypes
import json
import os
import threading
from dataclasses import dataclass

try:
	from linuxPlatform.installSettings import get as _setting
except ModuleNotFoundError:
	# Loaded by its file alone, as tests/linux loads it, with nothing of NVDA's on sys.path: there is no
	# installation then, and the environment is all there is.
	_setting = os.environ.get

LIB_VARIABLE = "LINDA_VBUF_ENGINE"

#: Render flags: the top of a large document first, the rest as an update; updates may keep
#: unchanged children as they are.
FLAG_PROGRESSIVE = 1
FLAG_REUSE = 2

_lib: ctypes.CDLL | None = None
_loadLock = threading.Lock()


def isAvailable() -> bool:
	"""Whether LINDA_VBUF_ENGINE, or the installation, names the library."""
	path = _setting(LIB_VARIABLE)
	return bool(path) and os.path.isfile(path)


def _load() -> ctypes.CDLL:
	global _lib
	with _loadLock:
		if _lib is None:
			path = _setting(LIB_VARIABLE)
			if not path:
				raise FileNotFoundError(f"{LIB_VARIABLE} does not name the document renderer")
			lib = ctypes.CDLL(path)
			out = (
				ctypes.POINTER(ctypes.c_void_p),
				ctypes.POINTER(ctypes.c_size_t),
				ctypes.POINTER(ctypes.c_void_p),
			)
			lib.bre_vbuf_render.restype = ctypes.c_int32
			lib.bre_vbuf_render.argtypes = (
				ctypes.c_char_p,
				ctypes.c_char_p,
				ctypes.c_int32,
				ctypes.c_uint32,
				*out,
			)
			lib.bre_vbuf_next.restype = ctypes.c_int32
			lib.bre_vbuf_next.argtypes = (ctypes.c_uint32, *out)
			lib.bre_vbuf_unwatch.restype = None
			lib.bre_vbuf_unwatch.argtypes = (ctypes.c_char_p, ctypes.c_char_p)
			lib.bre_vbuf_shutdown.restype = None
			lib.bre_vbuf_shutdown.argtypes = ()
			lib.bre_vbuf_free_stream.restype = None
			lib.bre_vbuf_free_stream.argtypes = (ctypes.c_void_p, ctypes.c_size_t)
			lib.bre_vbuf_free_str.restype = None
			lib.bre_vbuf_free_str.argtypes = (ctypes.c_void_p,)
			lib.bre_vbuf_named_id.restype = ctypes.c_int32
			lib.bre_vbuf_named_id.argtypes = (ctypes.c_char_p,)
			lib.bre_vbuf_named_path.restype = ctypes.c_void_p
			lib.bre_vbuf_named_path.argtypes = (ctypes.c_int32,)
			_lib = lib
	return _lib


class Stream:
	"""A render stream in the engine's memory: pointer to length UTF-16 code units, until free."""

	#: The storage linuxPlatform.vbufLib.buildReplacement built from this update batch off the main
	#: thread, for vbufLib.update to take whole: its handle, and the docHandle and ID it replaces.
	replacement: tuple[int, int, int] | None = None

	def __init__(self, pointer: int, length: int) -> None:
		self.pointer = pointer
		self.length = length

	def free(self) -> None:
		if self.pointer:
			_load().bre_vbuf_free_stream(self.pointer, self.length)
			self.pointer = 0
			self.length = 0

	def text(self) -> str:
		"""The stream as a str, for logs and tests."""
		if not self.pointer:
			return ""
		return ctypes.string_at(self.pointer, self.length * 2).decode("utf-16-le", "surrogatepass")


@dataclass(frozen=True)
class Rendered:
	stream: Stream
	#: complete, busy, docURL, objects, seconds, and the engine's timings.
	info: dict


@dataclass(frozen=True)
class Batch:
	"""Changes to one watched document: an update stream of S and R operations, or None when the
	batch carries events only."""

	stream: Stream | None
	busName: str
	docPath: str
	#: Drawn from "loadComplete", "reload", "gone", "complete" and "urlChanged".
	events: tuple[str, ...]
	#: bus, doc, events and docURL, and the engine's timings.
	info: dict


def _takeInfo(pointer: ctypes.c_void_p) -> dict:
	if not pointer.value:
		return {}
	try:
		return json.loads(ctypes.string_at(pointer.value).decode())
	finally:
		_load().bre_vbuf_free_str(pointer)


def render(
	busName: str,
	docPath: str,
	docHandle: int,
	*,
	progressive: bool = True,
	reuse: bool = True,
) -> Rendered:
	"""The render stream of the document at docPath, which the engine then watches. Never call it on
	the main thread: it waits for the application.

	:param progressive: For a large document, return the top first, with info["complete"] False; the
		rest comes from next as a batch on the whole document with the event "complete".
	:param reuse: Let updates keep children that did not change as R operations.
	:raise RuntimeError: The engine could not render the document.
	"""
	lib = _load()
	pointer = ctypes.c_void_p()
	length = ctypes.c_size_t()
	info = ctypes.c_void_p()
	flags = (FLAG_PROGRESSIVE if progressive else 0) | (FLAG_REUSE if reuse else 0)
	ok = lib.bre_vbuf_render(
		busName.encode(),
		docPath.encode(),
		docHandle,
		flags,
		ctypes.byref(pointer),
		ctypes.byref(length),
		ctypes.byref(info),
	)
	details = _takeInfo(info)
	if not ok or not pointer.value:
		raise RuntimeError(f"the engine could not render {busName} {docPath}: {details.get('error')}")
	return Rendered(Stream(pointer.value, length.value), details)


def logTransport(log, busName: str, info: dict) -> None:
	"""Say in log how the engine reaches the application, when info, of a render or a batch, says the
	engine has just decided it: once for each application, and again if it moves to the bus. Reading
	over the bus because the program's own server could not be used is a warning, since browse mode
	is slower there and a bug report should show it at the default level."""
	transport = info.get("transport")
	if not transport:
		return
	way = (
		"over its own peer-to-peer connection"
		if transport == "peer-to-peer"
		else "over the accessibility bus"
	)
	if info.get("peerToPeerFailed"):
		way += ", which is slower,"
	text = f"Browse mode reads {info.get('appName') or 'an application'} ({busName}) {way} because {info.get('transportWhy')}"
	if info.get("peerToPeerFailed"):
		log.warning(text)
	else:
		log.info(text)


def next(timeoutMs: int) -> Batch | None:
	"""The next batch of changes to any watched document, or None when none came within timeoutMs or
	the engine has shut down. Never call it on the main thread."""
	lib = _load()
	pointer = ctypes.c_void_p()
	length = ctypes.c_size_t()
	info = ctypes.c_void_p()
	got = lib.bre_vbuf_next(timeoutMs, ctypes.byref(pointer), ctypes.byref(length), ctypes.byref(info))
	if got != 1:
		return None
	details = _takeInfo(info)
	stream = Stream(pointer.value, length.value) if pointer.value else None
	return Batch(
		stream,
		details.get("bus", ""),
		details.get("doc", ""),
		tuple(details.get("events", ())),
		details,
	)


def namedId(path: str) -> int:
	"""The ID the engine gives the object at path, whose last part is not a number, as WebKitGTK's
	are not; the same ID a render gives it."""
	return _load().bre_vbuf_named_id(path.encode())


def namedPath(ID: int) -> str | None:
	"""The path of the object the engine gave ID, or None when it gave it to none."""
	lib = _load()
	pointer = lib.bre_vbuf_named_path(ID)
	if not pointer:
		return None
	try:
		return ctypes.string_at(pointer).decode()
	finally:
		lib.bre_vbuf_free_str(pointer)


def unwatch(busName: str, docPath: str) -> None:
	"""Stop watching the document; nothing more comes from next for it."""
	if _lib is None:
		return
	_lib.bre_vbuf_unwatch(busName.encode(), docPath.encode())


def shutdown() -> None:
	"""Stop the engine's thread and close its connections; a later render starts it again."""
	if _lib is None:
		return
	_lib.bre_vbuf_shutdown()
