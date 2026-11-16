"""Linux replacement for IAccessibleHandler.

MSAA and IAccessible2 are Windows APIs, so on Linux there is no IAccessible support to start or
stop, and no MSAA win event ever arrives to be pumped. Of its submodules only types, which is
portable, is kept; the rest are Windows only and are not provided. Any other name upstream's
module defines raises NoLinuxAttribute when read, which is both the AttributeError of a missing
name and NoLinuxImplementation.
"""

from typing import NoReturn as _NoReturn

from linuxPlatform import NoLinuxAttribute as _NoLinuxAttribute

from .types import RelationType  # noqa: F401


def initialize() -> None:
	pass


def terminate() -> None:
	pass


def pumpAll() -> None:
	# The core pump calls this every cycle to deliver queued MSAA win events. Linux has none, so
	# there is nothing to deliver; AT-SPI events get their own place in the pump.
	pass


def __getattr__(name: str) -> _NoReturn:
	# Special names are how Python and its tools probe a module, as `from ... import *` does for
	# __all__; they need the AttributeError that says the module has no such name.
	if name.startswith("__") and name.endswith("__"):
		raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
	raise _NoLinuxAttribute(__name__, name)
