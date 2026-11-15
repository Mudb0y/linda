"""Linux replacement for watchdog, skeleton.

Upstream detects a frozen core with a Windows waitable timer and recovers by cancelling COM calls
and SendMessage calls in flight, neither of which exists on Linux. The Linux watchdog has to be
built on its own terms; until it is, everything that would watch or recover raises.
"""

from collections.abc import Callable
from typing import Any, NoReturn

from linuxPlatform import NoLinuxImplementation

#: True while recovering from a frozen core. Nothing recovers until a Linux watchdog exists.
isAttemptingRecovery: bool = False


def initialize() -> NoReturn:
	raise NoLinuxImplementation("watchdog.initialize")


def terminate() -> NoReturn:
	raise NoLinuxImplementation("watchdog.terminate")


def alive() -> NoReturn:
	raise NoLinuxImplementation("watchdog.alive")


def asleep() -> NoReturn:
	raise NoLinuxImplementation("watchdog.asleep")


def cancellableExecute(
	func: Callable[..., Any], *args: Any, ccPumpMessages: bool = True, **kwargs: Any
) -> NoReturn:
	raise NoLinuxImplementation("watchdog.cancellableExecute")


def cancellableSendMessage(
	hwnd: int,
	msg: int,
	wParam: int,
	lParam: int,
	flags: int = 0,
	timeout: int = 60000,
) -> NoReturn:
	raise NoLinuxImplementation("watchdog.cancellableSendMessage")


class WatchdogObserver:
	@property
	def isAttemptingRecovery(self) -> bool:
		raise NoLinuxImplementation("watchdog.WatchdogObserver.isAttemptingRecovery")
