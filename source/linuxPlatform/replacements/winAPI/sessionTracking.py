"""Linux replacement for winAPI.sessionTracking, skeleton.

Upstream tracks whether Windows is locked through the WTS API. On Linux the lock state will come
from the session manager; until that is built, initialize and pumpAll raise.
"""

from typing import NoReturn

from linuxPlatform import NoLinuxImplementation


def initialize() -> NoReturn:
	raise NoLinuxImplementation("winAPI.sessionTracking.initialize")


def pumpAll() -> NoReturn:
	raise NoLinuxImplementation("winAPI.sessionTracking.pumpAll")


def isLockScreenModeActive() -> bool:
	# Every NVDAObject construction and every script lookup asks this, so it has to answer rather
	# than raise. Upstream answers False until it knows, during start-up, and so does Linux until
	# it tracks the session's lock state; content below a Linux screen locker is not yet withheld.
	return False
