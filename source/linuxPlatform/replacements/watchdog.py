"""Linux replacement for watchdog.

Upstream notices a frozen core through a Windows waitable timer, and recovers by cancelling the COM
and SendMessage calls the main thread is blocked in. Linux has neither kind of call. What carries
over unchanged is the diagnosis: the core reports alive and asleep, and a watcher thread logs every
thread's stack, with upstream's messages and thresholds, once the core stops reporting.

Recovery is reduced to its signal, and only cancellableExecute acts on it, by refusing new calls
from the main thread. While recovery runs, isCancellingCalls() is True and the descriptor from
cancelCallFd() is readable, until the core next reports alive or asleep. Upstream's cancel event is
a kernel handle that native code waits on beside its call; a descriptor is the Linux equivalent,
which a GLib source, tokio's AsyncFd or an asyncio reader can wait on beside a D-Bus socket. A call
already blocked on the main thread is not abandoned: one that does not wait on the descriptor
freezes the core for as long as it takes, and the watchdog only logs where it is stuck.
"""

import os
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
#: True while the watcher is recovering a frozen core, until alive() or asleep().
_cancellingCalls = False
#: Readable exactly while calls are being cancelled. An eventfd stays readable until it is read, so
#: a waiter that registers late still sees a recovery already under way.
_cancelCallFd = os.eventfd(0, os.EFD_NONBLOCK | os.EFD_CLOEXEC)


def isCancellingCalls() -> bool:
	"""Whether the watchdog is recovering a frozen core, so that a blocking call should give up."""
	return _cancellingCalls


def cancelCallFd() -> int:
	"""A descriptor that is readable while the watchdog is cancelling calls.

	A call that blocks on another process waits on it beside its reply, and gives up with
	CallCancelled when it becomes readable. The waiter must not read it: the watchdog drains it when
	the core recovers. It stays open for the life of the process.
	"""
	return _cancelCallFd


def _startCancellingCalls() -> None:
	global _cancellingCalls
	# Written before the flag is set, so that the core, which drains only after seeing the flag,
	# never leaves the descriptor readable with the flag clear.
	os.eventfd_write(_cancelCallFd, 1)
	_cancellingCalls = True


def _stopCancellingCalls() -> None:
	global _cancellingCalls
	_cancellingCalls = False
	try:
		os.eventfd_read(_cancelCallFd)
	except BlockingIOError:
		pass


def alive() -> None:
	"""Inform the watchdog that the core is alive."""
	global _coreIsAsleep, _lastAlive
	_lastAlive = _timer()
	_coreIsAsleep = False
	# This runs around every queued function. Reading an Event takes no lock; setting or clearing
	# one does, so each is changed only when it has to be.
	if not _coreAwake.is_set():
		_coreAwake.set()
	if _cancellingCalls:
		_stopCancellingCalls()


def asleep() -> None:
	"""Inform the watchdog that the core is going to sleep."""
	global _coreIsAsleep, _lastAlive
	_lastAlive = _timer()
	_coreIsAsleep = True
	_coreAwake.clear()
	if _cancellingCalls:
		_stopCancellingCalls()


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
	# from Win32. Nothing here learns that focus has moved to another application, so the full
	# timeout always applies, as upstream's does for its safe window classes.
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
	_startCancellingCalls()
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
	# A recovery under way when the core stopped has no alive() left to end it.
	if _cancellingCalls:
		_stopCancellingCalls()


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

	Upstream runs the function on another thread, which the main thread abandons when the watchdog
	cancels calls. Here the function runs inline and nothing interrupts it once it has started: only
	a call made while recovery is already cancelling calls is refused. A function that can block on
	another process must bound its own wait, and give up when cancelCallFd() becomes readable.
	ccPumpMessages lets Windows pump messages during the wait and has nothing to do on Linux.
	@raise CallCancelled: If the call was refused because recovery is cancelling calls.
	"""
	if (
		isRunning
		and not _suspended
		and _cancellingCalls
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
