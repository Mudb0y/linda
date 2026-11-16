"""Stand-in for comtypes, which only works on Windows.

Modules that declare COM interfaces and structures import: interfaces can be defined, subclassed
and named in ctypes declarations, GUIDs are real 16-byte structures, and COMError is a real
exception. Nothing ever succeeds in creating, obtaining or calling a COM object.
"""

import ctypes
import uuid
from ctypes import (
	HRESULT,
	POINTER,
	Structure,
	Union,
	byref,
	c_int32,
	c_ubyte,
	c_uint16,
	c_uint32,
	c_void_p,
	c_wchar_p,
)
from typing import Any, ClassVar, NoReturn

from linuxPlatform import NoLinuxImplementation

__version__ = "0+linux.standin"

__all__ = [
	"BSTR",
	"CLSCTX_INPROC_SERVER",
	"COINIT_MULTITHREADED",
	"COMMETHOD",
	"DISPMETHOD",
	"GUID",
	"HRESULT",
	"STDMETHOD",
	"COMError",
	"COMObject",
	"CoCreateInstance",
	"CoInitialize",
	"CoInitializeEx",
	"CoUninitialize",
	"IServiceProvider",
	"IUnknown",
	"ReturnHRESULT",
	"byref",
	"dispid",
	"tagBIND_OPTS2",
	"wireHWND",
]

CLSCTX_INPROC_SERVER = 0x1
COINIT_MULTITHREADED = 0x0


def _notOnLinux(what: str) -> NoLinuxImplementation:
	return NoLinuxImplementation(what, f"{what} needs COM, which does not exist on Linux")


class COMError(Exception):
	"""Raised by a failed COM call. Linux makes none, so nothing raises it here."""

	def __init__(self, hresult: int, text: str | None, details: tuple | None):
		super().__init__(hresult, text, details)
		self.hresult = hresult
		self.text = text
		self.details = details


class ReturnHRESULT(Exception):
	def __init__(self, hresult: int, text: str | None):
		super().__init__(hresult, text)
		self.hresult = hresult
		self.text = text


class GUID(Structure):
	_fields_ = [
		("Data1", c_uint32),
		("Data2", c_uint16),
		("Data3", c_uint16),
		("Data4", c_ubyte * 8),
	]

	def __init__(self, name: str | None = None):
		super().__init__()
		if name is not None:
			# Parsing the text form is arithmetic, not a call into Windows, so it is exact here.
			ctypes.memmove(byref(self), uuid.UUID(name).bytes_le, ctypes.sizeof(self))

	def __str__(self) -> str:
		return "{" + str(uuid.UUID(bytes_le=bytes(self))).upper() + "}"

	def __repr__(self) -> str:
		return f'GUID("{self}")'

	def __bool__(self) -> bool:
		return bytes(self) != bytes(ctypes.sizeof(self))

	def __eq__(self, other: object) -> bool:
		return isinstance(other, GUID) and bytes(self) == bytes(other)

	def __hash__(self) -> int:
		return hash(bytes(self))

	@classmethod
	def from_progid(cls, progid: str) -> "GUID":
		raise _notOnLinux("GUID.from_progid")

	def as_progid(self) -> str:
		raise _notOnLinux("GUID.as_progid")


class BSTR(c_wchar_p):
	pass


class _InterfacePointer(c_void_p):
	"""A pointer to a COM interface. On Linux there is never anything for it to point at."""


class _InterfaceMeta(type):
	def __new__(mcls, name: str, bases: tuple, namespace: dict) -> "_InterfaceMeta":
		cls = super().__new__(mcls, name, bases, namespace)
		# As comtypes does, POINTER(interface) is a pointer-sized type of its own, so that
		# interfaces can be named in argtypes, structure fields and other declarations.
		pointerType = type(_InterfacePointer)(f"POINTER({name})", (_InterfacePointer,), {"_iface_": cls})
		ctypes._pointer_type_cache[cls] = pointerType
		return cls


class IUnknown(metaclass=_InterfaceMeta):
	_iid_ = GUID("{00000000-0000-0000-C000-000000000046}")
	_methods_: ClassVar[list] = []
	_case_insensitive_ = False


class IServiceProvider(IUnknown):
	_iid_ = GUID("{6D5140C1-7436-11CE-8034-00AA006009FA}")


class dispid(int):
	pass


def COMMETHOD(idlflags: list, restype: Any, methodname: str, *argspec: Any) -> tuple:
	return ("COMMETHOD", idlflags, restype, methodname, argspec)


def STDMETHOD(restype: Any, name: str, argtypes: tuple | list = ()) -> tuple:
	return ("STDMETHOD", restype, name, tuple(argtypes))


def DISPMETHOD(idlflags: list, restype: Any, name: str, *argspec: Any) -> tuple:
	return ("DISPMETHOD", idlflags, restype, name, argspec)


class COMObject:
	"""Base of COM objects implemented in Python. None can be created on Linux."""

	_com_interfaces_: ClassVar[list] = []

	def __new__(cls, *args: Any, **kwargs: Any) -> NoReturn:
		raise _notOnLinux(f"Creating the COM object {cls.__qualname__}")


def CoCreateInstance(*args: Any, **kwargs: Any) -> NoReturn:
	raise _notOnLinux("comtypes.CoCreateInstance")


def CoInitialize(*args: Any, **kwargs: Any) -> NoReturn:
	raise _notOnLinux("comtypes.CoInitialize")


def CoInitializeEx(*args: Any, **kwargs: Any) -> NoReturn:
	raise _notOnLinux("comtypes.CoInitializeEx")


def CoUninitialize(*args: Any, **kwargs: Any) -> NoReturn:
	raise _notOnLinux("comtypes.CoUninitialize")


class tagBIND_OPTS2(Structure):
	_fields_ = [
		("cbStruct", c_uint32),
		("grfFlags", c_uint32),
		("grfMode", c_uint32),
		("dwTickCountDeadline", c_uint32),
		("dwTrackFlags", c_uint32),
		("dwClassContext", c_uint32),
		("locale", c_uint32),
		("pServerInfo", c_void_p),
	]


class _remotableHandleUnion(Union):
	_fields_: ClassVar[list] = [("hInproc", c_int32), ("hRemote", c_int32)]


class _RemotableHandle(Structure):
	_fields_ = [("fContext", c_int32), ("u", _remotableHandleUnion)]


wireHWND = POINTER(_RemotableHandle)
