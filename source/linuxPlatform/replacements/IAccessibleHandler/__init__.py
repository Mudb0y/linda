"""Linux replacement for IAccessibleHandler.

MSAA and IAccessible2 are Windows APIs, so on Linux there is no IAccessible support to start or
stop, and no MSAA win event ever arrives to be pumped. Of its submodules only types, which is
portable, is kept; the rest are Windows only and are not provided. Any other name upstream's
module defines raises NoLinuxAttribute when read, which is both the AttributeError of a missing
name and NoLinuxImplementation.
"""

from typing import NoReturn as _NoReturn

from linuxPlatform import missingAttribute as _missingAttribute

from .types import RelationType  # noqa: F401

# core.main and api.processPendingEvents call these at the points where Windows' accessibility API
# handler starts, pumps and stops; on Linux that API is AT-SPI.


def initialize() -> None:
	from linuxPlatform.atspi import handler, status

	# Before registering for events, which newer launchers answer by switching IsEnabled on
	# themselves: what claim finds is then what the desktop had before NVDA.
	status.claim()
	handler.initialize()
	status.reportUnreadable()


def terminate() -> None:
	from linuxPlatform.atspi import handler, status

	handler.terminate()
	status.release()


def pumpAll() -> None:
	from linuxPlatform.atspi import handler

	handler.pumpAll()


def __getattr__(name: str) -> _NoReturn:
	_missingAttribute(__name__, name)
