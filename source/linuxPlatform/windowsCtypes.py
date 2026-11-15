"""The Windows-only names NVDA imports from ctypes, for Linux.

Upstream code binds Windows library functions at import time, so binding has to work:
attribute access on a library, setting argtypes and restype, and building a prototype from a
(name, library) pair. Calling any of those functions raises NotImplementedError naming the
library and function; nothing here ever returns a value a caller could mistake for a result.
"""

import ctypes
import os
from collections.abc import Callable
from typing import Any


class StandInFunction:
	"""A function exported by a Windows library, as seen from Linux."""

	def __init__(self, libraryName: str, name: str | int):
		self._libraryName = libraryName
		self.__name__ = str(name)
		self.argtypes = None
		self.restype = None
		self.errcheck = None

	def __call__(self, *args, **kwargs):
		raise NotImplementedError(
			f"{self._libraryName}.{self.__name__} is a Windows function with no Linux implementation",
		)

	def __repr__(self) -> str:
		return f"<Windows function {self._libraryName}.{self.__name__}, not callable on Linux>"


class WinDLL:
	"""A Windows library. Linux never loads it; its functions can be bound but not called."""

	# ctypes reads _handle when a real prototype is bound to a library. None makes that a
	# TypeError; 0 would be dlsym's RTLD_DEFAULT and quietly find a Linux symbol of the same name.
	_handle = None

	def __init__(
		self,
		name: str | os.PathLike | None,
		mode: int = 0,
		handle: int | None = None,
		use_errno: bool = False,
		use_last_error: bool = False,
		winmode: int | None = None,
	):
		self._name = os.fspath(name) if name is not None else None

	def __getattr__(self, name: str) -> StandInFunction:
		if name.startswith("_"):
			raise AttributeError(name)
		func = self[name]
		setattr(self, name, func)
		return func

	def __getitem__(self, nameOrOrdinal: str | int) -> StandInFunction:
		return StandInFunction(self._name, nameOrOrdinal)

	def __bool__(self) -> bool:
		# Code that truth-tests a library is asking whether it can be used, as the Lilli braille
		# driver's check() does. No Windows library can be used here.
		return False

	def __repr__(self) -> str:
		return f"<{type(self).__name__} {self._name!r}, a Windows library with no Linux implementation>"


class OleDLL(WinDLL):
	pass


windll = ctypes.LibraryLoader(WinDLL)
oledll = ctypes.LibraryLoader(OleDLL)


class HRESULT(ctypes._SimpleCData):
	# Windows' HRESULT is a 32-bit LONG; ctypes' "l" is 64 bits on Linux.
	_type_ = "i"


_winFunctionTypes: dict[tuple, type] = {}


def WINFUNCTYPE(restype: Any, *argtypes: Any, use_errno: bool = False, use_last_error: bool = False) -> type:
	"""A function prototype as on Windows.

	x86-64 has one C calling convention, so applied to a Python callable this makes the same
	callback CFUNCTYPE does, which Linux libraries such as liblouis can call. Bound to a function in
	a Windows library, it gives a StandInFunction.
	"""
	key = (restype, argtypes, use_errno, use_last_error)
	try:
		return _winFunctionTypes[key]
	except KeyError:
		pass
	base = ctypes.CFUNCTYPE(restype, *argtypes, use_errno=use_errno, use_last_error=use_last_error)

	def __new__(cls, *args):
		if args and isinstance(args[0], tuple) and len(args[0]) == 2 and isinstance(args[0][1], WinDLL):
			name, library = args[0]
			return library[name]
		if len(args) >= 2 and isinstance(args[0], int) and isinstance(args[1], str):
			# (vtable index, name): a COM method prototype, which only comtypes builds.
			return StandInFunction("COM interface", args[1])
		return base.__new__(cls, *args)

	# A subclass of a ctypes function type has to restate these three in its own namespace.
	prototype = type(base)(
		"WinFunctionType",
		(base,),
		{
			"__new__": __new__,
			"_flags_": base._flags_,
			"_restype_": base._restype_,
			"_argtypes_": base._argtypes_,
		},
	)
	_winFunctionTypes[key] = prototype
	return prototype


def WinError(code: int | None = None, descr: str | None = None) -> OSError:
	raise NotImplementedError("ctypes.WinError has no Linux implementation")


def FormatError(code: int | None = None) -> str:
	raise NotImplementedError("ctypes.FormatError has no Linux implementation")


GetLastError: Callable[[], int] = windll.kernel32.GetLastError

#: Every name added to ctypes, and nothing else.
names: dict[str, Any] = {
	"windll": windll,
	"oledll": oledll,
	"WinDLL": WinDLL,
	"OleDLL": OleDLL,
	"WINFUNCTYPE": WINFUNCTYPE,
	"WinError": WinError,
	"FormatError": FormatError,
	"GetLastError": GetLastError,
	"HRESULT": HRESULT,
}


def install() -> None:
	for name, value in names.items():
		existing = getattr(ctypes, name, None)
		if existing is None:
			setattr(ctypes, name, value)
		elif existing is not value:
			raise RuntimeError(f"ctypes.{name} already exists and is not the Linux stand-in")
