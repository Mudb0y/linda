"""Linux replacement for UIAHandler.

UI Automation is a Windows API, so on Linux NVDA never has a UI Automation client. handler stays
None, which every caller already treats as UI Automation being unavailable, as it is on Windows
when UI Automation is turned off. Its submodules are Windows only and are not provided. Any other
name upstream's module defines raises NoLinuxAttribute when read, which is both the AttributeError
of a missing name and NoLinuxImplementation.
"""

from typing import NoReturn as _NoReturn

from linuxPlatform import missingAttribute as _missingAttribute

#: The UI Automation client. There is none on Linux.
handler = None


def initialize() -> None:
	pass


def terminate() -> None:
	pass


def __getattr__(name: str) -> _NoReturn:
	_missingAttribute(__name__, name)
