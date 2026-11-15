"""Linux replacement for winAPI.sessionTracking, skeleton.

Upstream tracks whether Windows is locked through the WTS API. On Linux the lock state comes from
the session manager instead; until that is built, each function raises.
"""


def _notImplemented(name: str) -> NotImplementedError:
	return NotImplementedError(f"winAPI.sessionTracking.{name} has no Linux implementation")


def initialize():
	raise _notImplemented("initialize")


def pumpAll():
	raise _notImplemented("pumpAll")


def isLockScreenModeActive() -> bool:
	raise _notImplemented("isLockScreenModeActive")
