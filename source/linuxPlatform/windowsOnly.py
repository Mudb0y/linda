"""Names from Windows-only modules that upstream modules import, as each platform has them.

An upstream module that kept code imports on Linux, and that imports winreg or os.startfile, imports
the name from here instead, in the same place. On Windows the name is the real module or function,
so Windows runs exactly what it did.

On Linux, startfile opens a file, directory or URL with the desktop's default application, and
winreg is a stand-in. Class bodies and default arguments read winreg's constants while the module
imports, so an upper-case name such as HKEY_LOCAL_MACHINE gives an inert placeholder. Every other
name, functions and types alike, raises NoLinuxImplementation naming the module and the attribute,
and so does every use of a placeholder: calling it, converting it, comparing it or testing its
truth. Code that names winreg in an except clause, an annotation evaluated at import or an
isinstance test therefore fails loudly too.

The stand-in is never put in sys.modules: the standard library looks for winreg there and behaves
as it does on Windows when it finds it.

NoWindowsLibrary is what Linux raises for the path of one of NVDA's own Windows DLLs.
"""

import os
import sys
from typing import Any, NoReturn

from linuxPlatform import NoLinuxImplementation

__all__ = ["NoWindowsLibrary", "startfile", "winreg"]


class NoWindowsLibrary(NoLinuxImplementation, FileNotFoundError):
	"""Raised for the path of NVDA's own Windows libraries, nvdaHelperLocal.dll, cppjieba.dll and the
	rest, which Linux does not have.

	Loading a library that is not there raises FileNotFoundError on Windows, so code that takes that
	as a feature being unavailable, as Chinese word segmentation does, takes this the same way.
	"""


class _Placeholder:
	"""A constant of a Windows-only module, which Linux code can hold but not use."""

	__slots__ = ("_what",)

	def __init__(self, what: str):
		self._what = what

	def __repr__(self) -> str:
		return f"<{self._what}, which Linux does not have>"

	def __getattr__(self, name: str) -> NoReturn:
		if name.startswith("_"):
			raise AttributeError(name)
		raise NoLinuxImplementation(f"{self._what}.{name}")

	def _refuse(self, *args: Any, **kwargs: Any) -> NoReturn:
		raise NoLinuxImplementation(self._what)

	__call__ = _refuse
	__bool__ = __int__ = __index__ = __float__ = __str__ = __format__ = _refuse
	__eq__ = __ne__ = __lt__ = __le__ = __gt__ = __ge__ = _refuse
	__iter__ = __len__ = __getitem__ = __contains__ = __enter__ = _refuse
	# A placeholder can still be a key of a table built at import time, and is equal only to itself.
	__hash__ = object.__hash__

	# Access flags are combined with | in default arguments too, as KEY_READ | KEY_WOW64_64KEY.
	def __or__(self, other: object) -> "_Placeholder":
		return _Placeholder(f"{self._what} | {other!r}")

	def __ror__(self, other: object) -> "_Placeholder":
		return _Placeholder(f"{other!r} | {self._what}")


class WindowsOnlyModule:
	"""A Windows-only module as Linux code sees it, under the rules in this module's docstring."""

	def __init__(self, name: str):
		self.__name__ = name

	def __getattr__(self, name: str) -> _Placeholder:
		if name.startswith("_"):
			# Probes such as copy's and inspect's look for underscore names and expect AttributeError.
			raise AttributeError(name)
		what = f"{self.__name__}.{name}"
		if not name.isupper():
			raise NoLinuxImplementation(what)
		placeholder = _Placeholder(what)
		setattr(self, name, placeholder)
		return placeholder

	def __repr__(self) -> str:
		return f"<stand-in for the Windows module {self.__name__}, which Linux does not have>"


if sys.platform == "win32":
	import winreg
	from os import startfile
else:
	from linuxPlatform import xdg

	winreg = WindowsOnlyModule("winreg")

	def startfile(filepath: str | os.PathLike[str], operation: str | None = None) -> None:
		"""os.startfile on Linux: open filepath with the desktop's default application, without waiting.

		Only the default verb exists on Linux; Windows' other verbs, such as print, belong to the
		application each file type is registered with.
		"""
		if operation not in (None, "open"):
			raise NoLinuxImplementation(f"os.startfile(operation={operation!r})")
		xdg.openWithDefaultApplication(os.fspath(filepath))
