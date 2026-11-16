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


@functools.cache
def _warnIdleNotPrevented() -> None:
	log.warning(
		"NVDA cannot yet keep this Linux session from going idle,"
		" so the screen may blank or lock during say all or while only a braille display is used.",
	)


def preventIdle() -> None:
	"""Keep the session from going idle, as NVDA does on Windows for braille keys and say all.

	Nothing does yet. org.freedesktop.ScreenSaver's SimulateUserActivity on the session bus will count
	a braille key as activity, and its Inhibit will hold the session awake for say all. Missing that
	only lets the screen blank, so it is warned about once and never stops the key or the say all
	that asked.
	"""
	_warnIdleNotPrevented()
