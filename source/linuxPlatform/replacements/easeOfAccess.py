"""Linux replacement for easeOfAccess, skeleton.

Upstream registers NVDA with the Windows Ease of Access Center through the registry. Starting a
screen reader at login works differently on Linux, so each function raises until that is built.
"""

from enum import IntEnum


class AutoStartContext(IntEnum):
	"""Where NVDA can start automatically. Upstream's values are Windows registry HKEYs."""

	ON_LOGON_SCREEN = 0x80000002
	AFTER_LOGON = 0x80000001


def _notImplemented(name: str) -> NotImplementedError:
	return NotImplementedError(f"easeOfAccess.{name} has no Linux implementation")


def isRegistered() -> bool:
	raise _notImplemented("isRegistered")


def notify(signal: int) -> None:
	raise _notImplemented("notify")


def willAutoStart(autoStartContext: AutoStartContext) -> bool:
	raise _notImplemented("willAutoStart")


def setAutoStart(autoStartContext: AutoStartContext, enable: bool) -> None:
	raise _notImplemented("setAutoStart")
