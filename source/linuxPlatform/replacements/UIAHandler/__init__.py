"""Linux replacement for UIAHandler.

UI Automation is a Windows API, so on Linux NVDA never has a UI Automation client. handler stays
None, which every caller already treats as UI Automation being unavailable, as it is on Windows
when UI Automation is turned off. Its submodules are Windows only and are not provided. Any other
name upstream's module defines raises NoLinuxImplementation when read.
"""

from typing import NoReturn as _NoReturn

from linuxPlatform import NoLinuxImplementation as _NoLinuxImplementation

#: The UI Automation client. There is none on Linux.
handler = None


def initialize() -> None:
	pass


def terminate() -> None:
	pass


def __getattr__(name: str) -> _NoReturn:
	# Special names are how Python and its tools probe a module, as `from ... import *` does for
	# __all__; they need the AttributeError that says the module has no such name.
	if name.startswith("__") and name.endswith("__"):
		raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
	raise _NoLinuxImplementation(f"UIAHandler.{name}")
