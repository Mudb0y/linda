"""Nodes for accessibles known only by bus name and object path, as the engine's buffers know them.

libatspi has no public call that makes an accessible from a bus name and path. _atspi_ref_accessible is
exported and is what libatspi itself uses for every reference in a reply; PyGObject's pygobject_new,
from its C API capsule, wraps the pointer. Neither makes a D-Bus call.
"""

import ctypes

from linuxPlatform.atspi import client as atspiClient
from linuxPlatform.atspi.client import Atspi, Node

_nativeCalls: dict[str, object] = {}


def nodeFor(busName: str, path: str) -> Node | None:
	"""The node for path in the application busName, or None; no D-Bus call."""
	ptr = _atspiRefAccessible()(busName.encode(), path.encode())
	if not ptr:
		return None
	try:
		return atspiClient.client.node(_pygobjectNew()(ptr))
	finally:
		_gObjectUnref()(ptr)


def scrollTo(node: Node) -> bool:
	"""Scroll node into view, as browse mode does on every caret move.

	:raise NodeGone: The accessible is gone or its application does not answer.
	"""
	return atspiClient.call(node, Atspi.Component.scroll_to, Atspi.ScrollType.ANYWHERE)


def _libatspi() -> ctypes.CDLL:
	lib = _nativeCalls.get("libatspi")
	if lib is None:
		# The library GObject introspection already loaded for Atspi.
		lib = _nativeCalls["libatspi"] = ctypes.CDLL("libatspi.so.0")
	return lib


def _atspiRefAccessible():
	func = _nativeCalls.get("ref")
	if func is None:
		func = _libatspi()._atspi_ref_accessible
		func.restype = ctypes.c_void_p
		func.argtypes = (ctypes.c_char_p, ctypes.c_char_p)
		_nativeCalls["ref"] = func
	return func


def _gObjectUnref():
	func = _nativeCalls.get("unref")
	if func is None:
		func = ctypes.CDLL("libgobject-2.0.so.0").g_object_unref
		func.restype = None
		func.argtypes = (ctypes.c_void_p,)
		_nativeCalls["unref"] = func
	return func


def _pygobjectNew():
	"""PyGObject's pygobject_new, which wraps a GObject pointer and takes a reference of its own."""
	func = _nativeCalls.get("new")
	if func is None:
		import gi._gobject

		getPointer = ctypes.pythonapi.PyCapsule_GetPointer
		getPointer.restype = ctypes.c_void_p
		getPointer.argtypes = (ctypes.py_object, ctypes.c_char_p)
		api = getPointer(gi._gobject._PyGObject_API, b"gobject._PyGObject_API")
		# struct _PyGObject_Functions: register_class, register_wrapper, lookup_class, newgobj, ...
		newgobj = ctypes.c_void_p.from_address(api + 3 * ctypes.sizeof(ctypes.c_void_p)).value
		func = ctypes.PYFUNCTYPE(ctypes.py_object, ctypes.c_void_p)(newgobj)
		_nativeCalls["new"] = func
	return func
