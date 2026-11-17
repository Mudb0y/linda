"""The engine's document renderer (engine/crates/vbufengine), loaded into NVDA's process.

It walks a document over its own peer-to-peer connection to the application and returns the render
stream libnvdaVbuf builds, as UTF-16 code units in the engine's memory, which linuxPlatform.vbufLib
reads without a copy. ctypes releases the GIL for the whole call, so it runs on a thread while NVDA's
main thread goes on. Found through BASEDREADER_VBUF_ENGINE, never on the library path.
"""

import ctypes
import json
import os
import threading
from dataclasses import dataclass

LIB_VARIABLE = "BASEDREADER_VBUF_ENGINE"

_lib: ctypes.CDLL | None = None
_loadLock = threading.Lock()


def isAvailable() -> bool:
	"""Whether BASEDREADER_VBUF_ENGINE names the library."""
	path = os.environ.get(LIB_VARIABLE)
	return bool(path) and os.path.isfile(path)


def _load() -> ctypes.CDLL:
	global _lib
	with _loadLock:
		if _lib is None:
			lib = ctypes.CDLL(os.environ[LIB_VARIABLE])
			lib.bre_vbuf_render.restype = ctypes.c_int32
			lib.bre_vbuf_render.argtypes = (
				ctypes.c_char_p,
				ctypes.c_char_p,
				ctypes.c_int32,
				ctypes.c_uint32,
				ctypes.POINTER(ctypes.c_void_p),
				ctypes.POINTER(ctypes.c_size_t),
				ctypes.POINTER(ctypes.c_void_p),
			)
			lib.bre_vbuf_free_stream.restype = None
			lib.bre_vbuf_free_stream.argtypes = (ctypes.c_void_p, ctypes.c_size_t)
			lib.bre_vbuf_free_str.restype = None
			lib.bre_vbuf_free_str.argtypes = (ctypes.c_void_p,)
			_lib = lib
	return _lib


class Stream:
	"""A render stream in the engine's memory: pointer to length UTF-16 code units, until free."""

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


def _takeInfo(pointer: ctypes.c_void_p) -> dict:
	if not pointer.value:
		return {}
	try:
		return json.loads(ctypes.string_at(pointer.value).decode())
	finally:
		_load().bre_vbuf_free_str(pointer)


def render(busName: str, docPath: str, docHandle: int, *, progressive: bool = True) -> Rendered:
	"""The render stream of the document at docPath. Never call it on the main thread: it waits for the
	application.

	:param progressive: Ask for the top of a large document first and the rest later; not yet done.
	:raise RuntimeError: The engine could not render the document.
	"""
	lib = _load()
	pointer = ctypes.c_void_p()
	length = ctypes.c_size_t()
	info = ctypes.c_void_p()
	ok = lib.bre_vbuf_render(
		busName.encode(),
		docPath.encode(),
		docHandle,
		0,
		ctypes.byref(pointer),
		ctypes.byref(length),
		ctypes.byref(info),
	)
	details = _takeInfo(info)
	if not ok or not pointer.value:
		raise RuntimeError(f"the engine could not render {busName} {docPath}: {details.get('error')}")
	return Rendered(Stream(pointer.value, length.value), details)
