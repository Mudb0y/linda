"""Stand-in for comtypes.automation: IDispatch, VARIANT and the VT_ constants."""

from ctypes import Structure, Union, c_double, c_int64, c_uint16, c_void_p
from typing import Any, NoReturn

from comtypes import BSTR, GUID, IUnknown, _notOnLinux

__all__ = [
	"BSTR",
	"VARIANT",
	"IDispatch",
	"IEnumVARIANT",
	"tagVARIANT",
]

VT_EMPTY = 0
VT_NULL = 1
VT_I2 = 2
VT_I4 = 3
VT_R4 = 4
VT_R8 = 5
VT_CY = 6
VT_DATE = 7
VT_BSTR = 8
VT_DISPATCH = 9
VT_ERROR = 10
VT_BOOL = 11
VT_VARIANT = 12
VT_UNKNOWN = 13
VT_DECIMAL = 14
VT_I1 = 16
VT_UI1 = 17
VT_UI2 = 18
VT_UI4 = 19
VT_I8 = 20
VT_UI8 = 21
VT_INT = 22
VT_UINT = 23
VT_VOID = 24
VT_HRESULT = 25
VT_PTR = 26
VT_SAFEARRAY = 27
VT_CARRAY = 28
VT_USERDEFINED = 29
VT_LPSTR = 30
VT_LPWSTR = 31
VT_RECORD = 36
VT_INT_PTR = 37
VT_UINT_PTR = 38
VT_ARRAY = 0x2000
VT_BYREF = 0x4000

__all__ += [name for name in dict(globals()) if name.startswith("VT_")]


class IDispatch(IUnknown):
	_iid_ = GUID("{00020400-0000-0000-C000-000000000046}")


class IEnumVARIANT(IUnknown):
	_iid_ = GUID("{00020404-0000-0000-C000-000000000046}")


class _variantValue(Union):
	_fields_ = [("llVal", c_int64), ("dblVal", c_double), ("ptr", c_void_p), ("_record", c_void_p * 2)]


class tagVARIANT(Structure):
	"""A VARIANT's layout. Converting to or from its value is COM marshalling and never happens here."""

	_fields_ = [
		("vt", c_uint16),
		("wReserved1", c_uint16),
		("wReserved2", c_uint16),
		("wReserved3", c_uint16),
		("_", _variantValue),
	]

	def __init__(self, *args: Any):
		super().__init__()
		if args:
			self.value = args[0]

	@property
	def value(self) -> NoReturn:
		raise _notOnLinux("Reading a VARIANT's value")

	@value.setter
	def value(self, value: Any) -> NoReturn:
		raise _notOnLinux("Setting a VARIANT's value")


VARIANT = tagVARIANT
