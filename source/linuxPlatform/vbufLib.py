"""NVDA's virtual buffer storage, compiled for Linux, behind the VBuf_* names NVDAHelper.localLib has.

The library is upstream's nvdaHelper/vbufBase/storage.cpp with a C entry point per VBuf_* function and
two for filling it from a render stream, which the engine's renderer writes (linuxPlatform.vbufEngine).
It is found through BASEDREADER_VBUF_LIB, never on the library path. Text crosses its interface as
UTF-16 code units, so buffer offsets are the code units NVDA counts on Windows.

Each function takes Python arguments as upstream's ctypes bindings do, so virtualBuffers/__init__.py
calls them unchanged; the library takes the buffer's lock in each, and ctypes releases the GIL around
each call.
"""

import ctypes
import os
from collections.abc import Callable
from ctypes import POINTER, c_int, c_size_t, c_uint64, c_void_p
from typing import TYPE_CHECKING

if TYPE_CHECKING:
	from linuxPlatform.vbufEngine import Stream

LIB_VARIABLE = "BASEDREADER_VBUF_LIB"

_lib: ctypes.CDLL | None = None
_pUnits = POINTER(ctypes.c_uint16)


def isAvailable() -> bool:
	"""Whether BASEDREADER_VBUF_LIB names the library."""
	path = os.environ.get(LIB_VARIABLE)
	return bool(path) and os.path.isfile(path)


def lib() -> ctypes.CDLL:
	global _lib
	if _lib is None:
		path = os.environ.get(LIB_VARIABLE)
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
		"VBufLinux_replaceSubtree": (c_int, (c_void_p, c_int, c_int, c_void_p, c_size_t)),
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

	:raise ValueError: The stream is malformed; the buffer holds what was built before the fault.
	"""
	if not lib().VBufLinux_build(handle, stream.pointer, stream.length):
		raise ValueError("malformed render stream")


def destroy(handle: int) -> None:
	lib().VBufLinux_destroy(handle)


def replaceSubtree(handle: int, docHandle: int, ID: int, stream: "Stream") -> bool:
	"""Replace the control field docHandle, ID with the stream, which renders it anew."""
	return bool(lib().VBufLinux_replaceSubtree(handle, docHandle, ID, stream.pointer, stream.length))


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


def entryPoints() -> dict[str, Callable]:
	"""Every VBuf_* name virtualBuffers calls, bound to this library."""
	dll = lib()
	points = {name: getattr(dll, name) for name in _DIRECT}
	points["VBuf_destroyBuffer"] = VBuf_destroyBuffer
	points["VBuf_getTextInRange"] = VBuf_getTextInRange
	points["VBuf_findNodeByAttributes"] = VBuf_findNodeByAttributes
	return points
