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

	Nothing here can tell the screen locker's own objects from the applications it covers, so every
	object except the desktop counts as below it: while locked, NVDA withholds the unlock prompt along
	with everything else rather than read out what the locker hides. That is why
	winAPI.sessionTracking._querySessionLocked must not report a locked session until this function
	recognises the locker's objects, by their process or their AT-SPI application.
	"""
	import api

	desktop = api.getDesktopObject()
	if obj is desktop:
		return False
	# While the session is locked every new object gets an overlay class, so a desktop object made
	# since is of another class than the one NVDA holds and never compares equal to it.
	return desktop is None or obj.APIClass is not desktop.APIClass or not obj._isEqual(desktop)


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
