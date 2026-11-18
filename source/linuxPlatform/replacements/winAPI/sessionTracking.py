"""Linux replacement for winAPI.sessionTracking: whether the session is locked.

Upstream's policy is kept as it is: the lock state is read once each core cycle and a change is
announced through utils.security.post_sessionLockStateChanged, nothing counts as locked until NVDA
has finished starting, and the secure desktop takes precedence over the lock screen. Where the lock
state comes from differs: linuxPlatform.sessionLock listens to logind and the desktop's screen saver.

Windows' lock screen speaks for itself and moves the focus as it closes; here nothing does either. So
the change is spoken, and once the lock has gone the focus is read again, unless something took the
focus in the meantime, as whatever took it while the lock withheld it was never presented.

While the session is locked, utils.security asks linuxPlatform.session.isBelowLockScreen of every
object NVDA would present, which lets through the desktop and the screen locker's own processes,
the unlock prompt among them, and withholds the rest.
"""

import time
from typing import NoReturn

from baseObject import AutoPropertyObject
from linuxPlatform import missingAttribute, sessionLock
from logHandler import log
from NVDAState import _TrackNVDAInitialization

_lockStateTracker: "_SessionLockedState | None" = None
"""Caches the session's lock state for one core cycle."""
_wasLockedPreviousPumpAll = False
"""The lock state the previous core cycle saw, so that a change can be announced."""
_refocus: "tuple[float, object] | None" = None
"""When to look at the focus again after a lock ended, and the focus NVDA had queued as it ended."""
REFOCUS_DELAY = 0.3
"""Seconds an application has after the end of a lock to report its focus before it is looked up."""


def _querySessionLocked() -> bool:
	return sessionLock.isLocked()


class _SessionLockedState(AutoPropertyObject):
	# Refer to AutoPropertyObject for notes on caching
	_cache_isSessionLocked = True

	isSessionLocked: bool

	def _get_isSessionLocked(self) -> bool:
		return _querySessionLocked()


def initialize() -> None:
	global _lockStateTracker
	_lockStateTracker = _SessionLockedState()
	# It connects on a thread of its own, and warns from there when it cannot tell.
	sessionLock.start()


def pumpAll() -> None:
	"""Used to track the session lock state every core cycle, and detect changes."""
	global _wasLockedPreviousPumpAll
	from utils.security import post_sessionLockStateChanged

	isNowLocked = _isSessionLocked()
	if isNowLocked != _wasLockedPreviousPumpAll:
		_wasLockedPreviousPumpAll = isNowLocked
		post_sessionLockStateChanged.notify(isNowLocked=isNowLocked)
		_announce(isNowLocked)
	elif _refocus is not None and time.monotonic() >= _refocus[0]:
		_refocusAfterLock()


def _announce(isNowLocked: bool) -> None:
	global _refocus
	import eventHandler
	import queueHandler
	import ui

	queueHandler.queueFunction(
		queueHandler.eventQueue,
		ui.message,
		# Translators: Reported when the screen locker of the session takes over the screen.
		_("Screen locked")
		if isNowLocked
		# Translators: Reported when the screen locker of the session lets the user back in.
		else _("Screen unlocked"),
	)
	_refocus = None if isNowLocked else (time.monotonic() + REFOCUS_DELAY, eventHandler.lastQueuedFocusObject)


def _refocusAfterLock() -> None:
	global _refocus
	import eventHandler
	from NVDAObjects import NVDAObject

	_at, focusAtUnlock = _refocus
	_refocus = None
	if eventHandler.lastQueuedFocusObject is not focusAtUnlock:
		return
	try:
		focus = NVDAObject.objectWithFocus()
	except Exception:
		log.exception("Cannot find the focus after the session was unlocked")
		return
	if focus:
		eventHandler.queueEvent("gainFocus", focus)


def _isSessionLocked() -> bool:
	if not _TrackNVDAInitialization.isInitializationComplete():
		# Wait until initialization is complete,
		# so NVDA and other consumers can register the lock state
		# via post_sessionLockStateChanged.
		return False
	if _lockStateTracker is None:
		log.error(
			"_TrackNVDAInitialization.markInitializationComplete was called before sessionTracking.initialize",
		)
		return False
	return _lockStateTracker.isSessionLocked


def isLockScreenModeActive() -> bool:
	"""Whether the session's screen locker is active.

	Every NVDAObject construction and every script lookup asks this, so it stays cheap.
	"""
	from utils.security import isRunningOnSecureDesktop

	if isRunningOnSecureDesktop():
		# Use secure mode instead if on the secure desktop
		return False
	return _isSessionLocked()


def __getattr__(name: str) -> NoReturn:
	missingAttribute(__name__, name)
