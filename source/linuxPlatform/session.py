"""Facts about the login session NVDA runs in, and what NVDA asks of it."""

import functools
import os

from logHandler import log


@functools.cache
def isGreeter() -> bool:
	"""Whether this is a display manager's login screen, where nobody has signed in yet.

	pam_systemd sets XDG_SESSION_CLASS for each session it registers; logind's Session.Class property
	would be the authoritative answer, at the cost of a D-Bus call. A session's class never changes.
	"""
	return os.environ.get("XDG_SESSION_CLASS") == "greeter"


def isBelowLockScreen(obj: object) -> bool:
	"""Whether obj lies below the screen locker, so that NVDA must not present it while the session is
	locked. utils.security asks this only while winAPI.sessionTracking reports the session locked.

	The desktop and the screen locker's own processes, as linuxPlatform.sessionLock knows them, are
	above it. Everything else is below, including any object whose process cannot be told.
	"""
	import api

	desktop = api.getDesktopObject()
	if obj is desktop:
		return False
	# While the session is locked every new object gets an overlay class, so a desktop object made
	# since is of another class than the one NVDA holds and never compares equal to it.
	if desktop is not None and obj.APIClass is desktop.APIClass and obj._isEqual(desktop):
		return False
	from linuxPlatform import sessionLock

	try:
		processID = obj.processID
	except Exception:  # noqa: BLE001
		# Whatever the failure, the object is withheld rather than read out over the lock.
		log.debugWarning("Cannot tell the process of an object while the session is locked", exc_info=True)
		return True
	return not (isinstance(processID, int) and sessionLock.isLockerProcess(processID))


def isProcessWithheld(processID: int) -> bool:
	"""Whether what a process says of itself, such as an announcement or a live region's change, must
	not be spoken now: the session is locked and the process is not the screen locker's."""
	from winAPI.sessionTracking import isLockScreenModeActive

	if not isLockScreenModeActive():
		return False
	from linuxPlatform import sessionLock

	return not (processID and sessionLock.isLockerProcess(processID))


@functools.cache
def _warnIdleNotPrevented() -> None:
	log.warning(
		"NVDA cannot keep this Linux session from going idle,"
		" so the screen may blank or lock during say all or while only a braille display is used.",
	)


def preventIdle() -> None:
	"""Keep the session from going idle, as NVDA does on Windows for braille keys and say all.

	This holds nothing: org.freedesktop.ScreenSaver's SimulateUserActivity and Inhibit on the session
	bus are what would count a braille key as activity and keep the session awake for say all. Missing
	them only lets the screen blank, so it is warned about once and never stops the key or the say all
	that asked.
	"""
	_warnIdleNotPrevented()
