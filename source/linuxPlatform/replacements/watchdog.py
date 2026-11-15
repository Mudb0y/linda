"""Linux replacement for watchdog, skeleton.

Upstream detects a frozen core with a Windows waitable timer and recovers by cancelling COM calls
and SendMessage calls in flight, neither of which exists on Linux. The Linux watchdog has to be
built on its own terms; until it is, everything that would watch or recover raises.
"""

from typing import Any

#: True while recovering from a frozen core. Nothing recovers until a Linux watchdog exists.
isAttemptingRecovery: bool = False


def _notImplemented(name: str) -> NotImplementedError:
	return NotImplementedError(f"watchdog.{name} has no Linux implementation")


def initialize():
	raise _notImplemented("initialize")


def terminate():
	raise _notImplemented("terminate")


def alive():
	raise _notImplemented("alive")


def asleep():
	raise _notImplemented("asleep")


def cancellableExecute(func, *args, ccPumpMessages=True, **kwargs) -> Any:
	raise _notImplemented("cancellableExecute")


def cancellableSendMessage(hwnd, msg, wParam, lParam, flags=0, timeout=60000):
	raise _notImplemented("cancellableSendMessage")


class WatchdogObserver:
	@property
	def isAttemptingRecovery(self) -> bool:
		raise _notImplemented("WatchdogObserver.isAttemptingRecovery")
