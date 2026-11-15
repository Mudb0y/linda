"""Linux replacement for watchdog.

Upstream notices a frozen core through a Windows waitable timer, and recovers by cancelling the COM
and SendMessage calls the main thread is blocked in. Linux has neither kind of call. What carries
over unchanged is the diagnosis: the core reports alive and asleep, and a watcher thread logs every
thread's stack, with upstream's messages and thresholds, once the core stops reporting. Recovery is
reduced to its signal: while it runs, _cancelCallEvent is set, as upstream's is, and alive() clears
it, so a blocking call into another process can wait on it beside its reply and give up.
"""

import threading
from collections.abc import Callable
from time import perf_counter as _timer
from typing import Any, NoReturn

import exceptions
import logHandler
import NVDAState
from linuxPlatform import NoLinuxImplementation
from logHandler import log


def __getattr__(attrName: str) -> Any:
	"""Module level `__getattr__` used to preserve backward compatibility."""
	if attrName == "getFormattedStacksForAllThreads" and NVDAState._allowDeprecatedAPI():
		log.warning(
			"Importing getFormattedStacksForAllThreads from here is deprecated. "
			"getFormattedStacksForAllThreads should be imported from logHandler instead.",
			stack_info=True,
		)
		return logHandler.getFormattedStacksForAllThreads
	raise AttributeError(f"module {__name__!r} has no attribute {attrName!r}")


MIN_CORE_ALIVE_TIMEOUT = 0.5
"""The minimum time (seconds) to wait for the core to be alive."""
NORMAL_CORE_ALIVE_TIMEOUT = 10
"""Seconds to wait for the core to be alive under normal circumstances."""
RECOVER_ATTEMPT_INTERVAL = 0.05
"""Seconds between checks whether a frozen core has recovered."""
FROZEN_WARNING_TIMEOUT = 15
"""Seconds before the core should be considered severely frozen and a warning logged."""

isRunning = False
isAttemptingRecovery: bool = False
_coreIsAsleep = False
_lastAlive = _timer()
_suspended = False
_watcherThread: threading.Thread | None = None
#: Set from the first alive() after the core went to sleep until the next asleep(). The watcher
#: waits on it, so an idle screen reader does not wake to check on a core that is not running.
_coreAwake = threading.Event()
#: Set by terminate(), and the watcher's every timed wait is on it, so terminating never waits.
_terminating = threading.Event()
#: Set while the watcher is recovering a frozen core, cleared by alive() and asleep().
_cancelCallEvent = threading.Event()


def alive() -> None:
	"""Inform the watchdog that the core is alive."""
	global _coreIsAsleep, _lastAlive
	_lastAlive = _timer()
	_coreIsAsleep = False
	# This runs around every queued function. Reading an Event takes no lock; setting or clearing
	# one does, so each is changed only when it has to be.
	if not _coreAwake.is_set():
		_coreAwake.set()
	if _cancelCallEvent.is_set():
		_cancelCallEvent.clear()


def asleep() -> None:
	"""Inform the watchdog that the core is going to sleep."""
	global _coreIsAsleep, _lastAlive
	_lastAlive = _timer()
	_coreIsAsleep = True
	_coreAwake.clear()
	if _cancelCallEvent.is_set():
		_cancelCallEvent.clear()


def isCoreAsleep() -> bool:
	"""
	Finds out if the core is currently asleep (I.e. not in a core cycle).
	Note that if the core is actually frozen, this function will return false
	as it is frozen in a core cycle while awake.
	"""
	return _coreIsAsleep


def _isAlive() -> bool:
	# #5189: Once terminated, the core counts as alive, so that recovery stops and the watcher ends.
	return not isRunning or _coreIsAsleep or _timer() - _lastAlive < MIN_CORE_ALIVE_TIMEOUT


def _waitUntilNormalCoreAliveTimeout(waitedSince: float) -> None:
	# Upstream cuts this wait short when the foreground window or thread has changed, which it reads
	# from Win32. Until the AT-SPI backend can say that focus has moved to another application, the
	# full timeout always applies, as upstream's does for its safe window classes.
	logged = False
	while _timer() - waitedSince < NORMAL_CORE_ALIVE_TIMEOUT and not _isAlive():
		if not logged:
			logged = True
			log.debug(f"Potential freeze, waiting up to {NORMAL_CORE_ALIVE_TIMEOUT} seconds.")
		_terminating.wait(MIN_CORE_ALIVE_TIMEOUT)
	if logged and _isAlive():
		log.debug(f"Recovered from potential freeze after {_timer() - waitedSince} seconds.")


def _watcher() -> None:
	global isAttemptingRecovery
	while True:
		_coreAwake.wait()
		if not isRunning:
			return
		lastAlive = _lastAlive
		remaining = MIN_CORE_ALIVE_TIMEOUT - (_timer() - lastAlive)
		if _coreIsAsleep or remaining > 0:
			_terminating.wait(max(remaining, RECOVER_ATTEMPT_INTERVAL))
			continue
		_waitUntilNormalCoreAliveTimeout(lastAlive)
		if _isAlive():
			continue
		isAttemptingRecovery = True
		waitForFreezeRecovery(lastAlive)
		isAttemptingRecovery = False


def waitForFreezeRecovery(waitedSince: float) -> None:
	log.info(f"Starting freeze recovery after {_timer() - waitedSince} seconds.")
	if log.isEnabledFor(log.DEBUGWARNING):
		stacks = logHandler.getFormattedStacksForAllThreads()
		log.debugWarning(
			f"Listing stacks for Python threads:\n{stacks}",
		)
	lastTime = _timer()
	_cancelCallEvent.set()
	while not _isAlive():
		curTime = _timer()
		if curTime - lastTime > FROZEN_WARNING_TIMEOUT:
			lastTime = curTime
			log.error(f"Core frozen in stack! ({curTime - waitedSince} seconds)")
			stacks = logHandler.getFormattedStacksForAllThreads()
			log.info(f"Listing stacks for Python threads:\n{stacks}")
		_terminating.wait(RECOVER_ATTEMPT_INTERVAL)
	log.info(
		f"Recovered from freeze after {_timer() - waitedSince} seconds.",
	)


def initialize() -> None:
	"""Initialize the watchdog."""
	global _watcherThread, isRunning
	if isRunning:
		raise RuntimeError("already running")
	# Upstream installs its crash handler here. On Linux logHandler.initialize arms faulthandler
	# instead, as soon as the log's directory is known, so that a crash during start-up is caught.
	# A crash-loop guard has nothing to guard: faulthandler only writes, it never restarts NVDA.
	isRunning = True
	_terminating.clear()
	_watcherThread = threading.Thread(
		name=__name__,
		target=_watcher,
		daemon=True,
	)
	alive()
	_watcherThread.start()


def terminate() -> None:
	"""Terminate the watchdog."""
	global isRunning
	if not isRunning:
		return
	isRunning = False
	_terminating.set()
	_coreAwake.set()
	_watcherThread.join()


class Suspender:
	"""A context manager to temporarily suspend the watchdog for a block of code."""

	def __enter__(self) -> None:
		global _suspended
		_suspended = True
		asleep()

	def __exit__(self, *args: object) -> None:
		global _suspended
		_suspended = False
		alive()


def cancellableExecute(
	func: Callable[..., Any], *args: Any, ccPumpMessages: bool = True, **kwargs: Any
) -> Any:
	"""Execute a function, unless the watchdog is cancelling calls to recover a frozen core.

	Upstream runs the function on another thread so that a frozen main thread can abandon it. On
	Linux the blocking calls are D-Bus calls into other applications, which take their own timeouts
	and wait on _cancelCallEvent, so the function runs here, and ccPumpMessages, which lets Windows
	pump messages during the wait, has nothing to do.
	@raise CallCancelled: If the call was refused because recovery is cancelling calls.
	"""
	if (
		isRunning
		and not _suspended
		and _cancelCallEvent.is_set()
		and threading.current_thread() is threading.main_thread()
	):
		raise exceptions.CallCancelled
	return func(*args, **kwargs)


def cancellableSendMessage(
	hwnd: int,
	msg: int,
	wParam: int,
	lParam: int,
	flags: int = 0,
	timeout: int = 60000,
) -> NoReturn:
	"""Window messages are Windows' own; only the Windows object classes send them."""
	raise NoLinuxImplementation("watchdog.cancellableSendMessage")


class WatchdogObserver:
	@property
	def isAttemptingRecovery(self) -> bool:
		global isAttemptingRecovery  # noqa: PLW0602
		return isAttemptingRecovery
