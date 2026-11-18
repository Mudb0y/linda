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
		" so the screen may blank or lock during say all.",
	)


@functools.cache
def _warnBrailleKeysNotActivity() -> None:
	log.warning(
		"NVDA cannot count a braille display's keys as activity, so this Linux session may go idle"
		" and the screen blank or lock while only a braille display is used.",
	)


def preventIdle(persistent: bool) -> None:
	"""Keep the session from going idle, as NVDA does on Windows for say all and braille keys.

	A persistent request, say all's, holds an idle inhibitor through linuxPlatform.idleInhibitor until
	allowIdle ends it. One that is not, a braille key's, would count as the user's activity on Windows;
	nothing on the session bus does that for every desktop, so it is warned of once and never stops
	the key that asked.
	"""
	if persistent:
		from linuxPlatform import idleInhibitor

		idleInhibitor.hold()
	else:
		_warnBrailleKeysNotActivity()


def allowIdle() -> None:
	"""End a persistent request preventIdle counted."""
	from linuxPlatform import idleInhibitor

	idleInhibitor.release()
