"""Linux replacement for easeOfAccess, skeleton.

Upstream registers NVDA with the Windows Ease of Access Center through the registry. Starting a
screen reader at login works differently on Linux, so each function raises until that is built.
"""

from enum import IntEnum
from typing import NoReturn

from linuxPlatform import NoLinuxImplementation


class AutoStartContext(IntEnum):
	"""Where NVDA can start automatically. Upstream's values are Windows registry HKEYs."""

	ON_LOGON_SCREEN = 0x80000002
	AFTER_LOGON = 0x80000001


def isRegistered() -> NoReturn:
	raise NoLinuxImplementation("easeOfAccess.isRegistered")


def notify(signal: int) -> NoReturn:
	raise NoLinuxImplementation("easeOfAccess.notify")


def willAutoStart(autoStartContext: AutoStartContext) -> NoReturn:
	raise NoLinuxImplementation("easeOfAccess.willAutoStart")


def setAutoStart(autoStartContext: AutoStartContext, enable: bool) -> NoReturn:
	raise NoLinuxImplementation("easeOfAccess.setAutoStart")
