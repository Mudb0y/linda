"""NVDA's virtual buffer storage, compiled for Linux, behind the VBuf_* names NVDAHelper.localLib has.

The library is upstream's nvdaHelper/vbufBase/storage.cpp with a C entry point per VBuf_* function and
four for filling it from render streams, which the engine's renderer writes (linuxPlatform.vbufEngine):
one builds a buffer, one applies an update batch to it, and two let a batch that replaces the whole
document be built off the main thread and taken there whole.
It is found through LINDA_VBUF_LIB or the path an installation fixed for it (linuxPlatform.installSettings),
never on the library path. Text crosses its interface as
UTF-16 code units, so buffer offsets are the code units NVDA counts on Windows.

Each function takes Python arguments as upstream's ctypes bindings do, so virtualBuffers/__init__.py
calls them unchanged; the library takes the buffer's lock in each, and ctypes releases the GIL around
each call. A handle that is not a live buffer, or a node that is not in it, gets 0, as a failed call.
"""

import ctypes
import os
from collections.abc import Callable
from ctypes import POINTER, c_int, c_size_t, c_uint64, c_void_p
from typing import TYPE_CHECKING

try:
	from linuxPlatform.installSettings import get as _setting
except ModuleNotFoundError:
	# Loaded by its file alone, as tests/linux loads it, with nothing of NVDA's on sys.path: there is no
	# installation then, and the environment is all there is.
	_setting = os.environ.get

if TYPE_CHECKING:
	from linuxPlatform.vbufEngine import Stream

LIB_VARIABLE = "LINDA_VBUF_LIB"

_lib: ctypes.CDLL | None = None
_pUnits = POINTER(ctypes.c_uint16)


def isAvailable() -> bool:
	"""Whether LINDA_VBUF_LIB, or the installation, names the library."""
	path = _setting(LIB_VARIABLE)
	return bool(path) and os.path.isfile(path)


def lib() -> ctypes.CDLL:
	global _lib
	if _lib is None:
		path = _setting(LIB_VARIABLE)
		if not path:
			raise FileNotFoundError(f"{LIB_VARIABLE} does not name the virtual buffer library")
		dll = ctypes.CDLL(path)
		_bind(dll)
		_lib = dll
	return _lib


def _bind(dll: ctypes.CDLL) -> None:
	pInt = POINTER(c_int)
	pNode = POINTER(c_uint64)
	signatures = {
		"VBufLinux_create": (c_void_p, ()),
		"VBufLinux_destroy": (None, (c_void_p,)),
		"VBufLinux_build": (c_int, (c_void_p, c_void_p, c_size_t)),
		"VBufLinux_update": (c_int, (c_void_p, c_void_p, c_size_t)),
		"VBufLinux_buildReplacement": (c_int, (c_void_p, c_void_p, c_size_t, pInt, pInt)),
		"VBufLinux_adopt": (c_int, (c_void_p, c_void_p, c_int, c_int)),
		"VBufLinux_free": (None, (c_void_p,)),
		"VBuf_getFieldNodeOffsets": (c_int, (c_void_p, c_uint64, pInt, pInt)),
		"VBuf_isFieldNodeAtOffset": (c_int, (c_void_p, c_uint64, c_int)),
		"VBuf_locateTextFieldNodeAtOffset": (c_int, (c_void_p, c_int, pInt, pInt, pNode)),
		"VBuf_locateControlFieldNodeAtOffset": (c_int, (c_void_p, c_int, pInt, pInt, pInt, pInt, pNode)),
		"VBuf_getControlFieldNodeWithIdentifier": (c_int, (c_void_p, c_int, c_int, pNode)),
		"VBuf_getIdentifierFromControlFieldNode": (c_int, (c_void_p, c_uint64, pInt, pInt)),
		"VBuf_findNodeByAttributes": (
			c_int,
			(c_void_p, c_int, c_int, _pUnits, c_size_t, _pUnits, c_size_t, pInt, pInt, pNode),
		),
		"VBuf_getSelectionOffsets": (c_int, (c_void_p, pInt, pInt)),
		"VBuf_setSelectionOffsets": (c_int, (c_void_p, c_int, c_int)),
		"VBuf_getTextLength": (c_int, (c_void_p,)),
		"VBuf_getTextInRange": (c_void_p, (c_void_p, c_int, c_int, c_int, POINTER(c_size_t))),
		"VBuf_getLineOffsets": (c_int, (c_void_p, c_int, c_int, c_int, pInt, pInt)),
	}
	for name, (restype, argtypes) in signatures.items():
		function = getattr(dll, name)
		function.restype = restype
		function.argtypes = argtypes


def _units(text: str) -> tuple[ctypes.Array, int]:
	"""text as UTF-16 code units for the library, and how many there are."""
	data = text.encode("utf-16-le", "surrogatepass")
	count = len(data) // 2
	return (ctypes.c_uint16 * max(count, 1)).from_buffer_copy(data or b"\0\0"), count


def _decode(pointer: int, count: int) -> str:
	return ctypes.string_at(pointer, count * 2).decode("utf-16-le", "surrogatepass")


def create() -> int:
	"""A new, empty buffer; its handle, which destroy frees."""
	return lib().VBufLinux_create()


def build(handle: int, stream: "Stream") -> None:
	"""Fill the empty buffer handle from the render stream, which stays the caller's.

	:raise ValueError: The stream is malformed, or the buffer was not empty; it is left empty.
	"""
	if not lib().VBufLinux_build(handle, stream.pointer, stream.length):
		raise ValueError("malformed render stream")


def destroy(handle: int) -> None:
	lib().VBufLinux_destroy(handle)


def update(handle: int, stream: "Stream") -> bool:
	"""Apply the update batch in stream, which stays the caller's, to the buffer handle.

	Every field a batch replaces goes in one step, and the caret stays where it was relative to the
	deepest of its fields that is still there.

	A stream whose replacement buildReplacement built, and which replaces the buffer's root, is taken
	whole: the buffer swaps its fields for the replacement's without building anything, and the
	replacement holds the old ones until the stream is freed.

	:return: Whether everything was applied. A malformed batch changes nothing; a batch that names a
		field the buffer no longer has applies the rest.
	"""
	replacement = getattr(stream, "replacement", None)
	if replacement is not None and lib().VBufLinux_adopt(handle, *replacement):
		return True
	return bool(lib().VBufLinux_update(handle, stream.pointer, stream.length))


def buildReplacement(stream: "Stream") -> tuple[int, int, int] | None:
	"""Off the main thread: build the one field an update batch replaces another with, so that update
	takes it whole when that other is the root, instead of building a whole document on the main
	thread.

	:return: The new buffer's handle, which destroy frees, and the docHandle and ID of the field the
		batch replaces; None for a batch of more than one replacement, or one that keeps fields.
	"""
	handle = create()
	docHandle, ID = c_int(), c_int()
	if lib().VBufLinux_buildReplacement(
		handle, stream.pointer, stream.length, ctypes.byref(docHandle), ctypes.byref(ID)
	):
		return handle, docHandle.value, ID.value
	destroy(handle)
	return None


def anchor(handle: int, offset: int) -> tuple[int, int, int] | None:
	"""Where offset is in the buffer handle, in terms an update keeps: the identifier of the deepest
	field there and how far into that field offset is. place finds the offset again after an update.

	:return: docHandle, ID and the distance, or None when no field is at offset.
	"""
	start, end, docHandle, ID = c_int(), c_int(), c_int(), c_int()
	node = c_uint64()
	found = lib().VBuf_locateControlFieldNodeAtOffset(
		handle,
		offset,
		ctypes.byref(start),
		ctypes.byref(end),
		ctypes.byref(docHandle),
		ctypes.byref(ID),
		ctypes.byref(node),
	)
	if not found or not node:
		return None
	return docHandle.value, ID.value, offset - start.value


def place(handle: int, where: tuple[int, int, int]) -> int | None:
	"""The offset anchor gave where for, in the buffer handle as it is now: as far into the same
	field, or its end when the field has shrunk; None when the field is gone."""
	docHandle, ID, distance = where
	node = c_uint64()
	lib().VBuf_getControlFieldNodeWithIdentifier(handle, docHandle, ID, ctypes.byref(node))
	if not node:
		return None
	start, end = c_int(), c_int()
	if not lib().VBuf_getFieldNodeOffsets(handle, node, ctypes.byref(start), ctypes.byref(end)):
		return None
	return min(start.value + distance, max(start.value, end.value))


def VBuf_destroyBuffer(handleRef) -> None:
	"""Upstream passes byref() of the buffer handle, and expects it cleared."""
	handle = handleRef._obj if hasattr(handleRef, "_obj") else handleRef.contents
	if handle.value:
		lib().VBufLinux_destroy(handle.value)
	handle.value = None


def VBuf_getTextInRange(handle, startOffset: int, endOffset: int, useMarkup: bool) -> str | None:
	"""Upstream's binding returns the text, or None for none, as its BSTR out parameter."""
	length = c_size_t()
	pointer = lib().VBuf_getTextInRange(
		handle, startOffset, endOffset, 1 if useMarkup else 0, ctypes.byref(length)
	)
	if not pointer:
		return None
	try:
		return _decode(pointer, length.value)
	finally:
		lib().VBufLinux_free(pointer)


def VBuf_findNodeByAttributes(
	handle,
	offset,
	direction: int,
	attribs: str,
	regexp: str,
	startOffset,
	endOffset,
	node,
) -> int:
	attribUnits, attribCount = _units(attribs)
	regexpUnits, regexpCount = _units(regexp)
	return lib().VBuf_findNodeByAttributes(
		handle,
		offset,
		direction,
		attribUnits,
		attribCount,
		regexpUnits,
		regexpCount,
		startOffset,
		endOffset,
		node,
	)


_DIRECT = (
	"VBuf_getFieldNodeOffsets",
	"VBuf_isFieldNodeAtOffset",
	"VBuf_locateTextFieldNodeAtOffset",
	"VBuf_locateControlFieldNodeAtOffset",
	"VBuf_getControlFieldNodeWithIdentifier",
	"VBuf_getIdentifierFromControlFieldNode",
	"VBuf_getSelectionOffsets",
	"VBuf_setSelectionOffsets",
	"VBuf_getTextLength",
	"VBuf_getLineOffsets",
)


def _loadedOnFirstCall(name: str) -> Callable:
	def call(*args):
		return getattr(lib(), name)(*args)

	call.__name__ = name
	return call


def entryPoints() -> dict[str, Callable]:
	"""Every VBuf_* name virtualBuffers calls, bound to this library, which the first call loads: NVDA
	binds them as it starts, and a session that reads no web page never needs it."""
	points = {name: _loadedOnFirstCall(name) for name in _DIRECT}
	points["VBuf_destroyBuffer"] = VBuf_destroyBuffer
	points["VBuf_getTextInRange"] = VBuf_getTextInRange
	points["VBuf_findNodeByAttributes"] = VBuf_findNodeByAttributes
	return points
