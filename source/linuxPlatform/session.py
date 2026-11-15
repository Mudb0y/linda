"""Facts about the login session NVDA runs in."""

import functools
import os


@functools.cache
def isGreeter() -> bool:
	"""Whether this is a display manager's login screen, where nobody has signed in yet.

	pam_systemd sets XDG_SESSION_CLASS for each session it registers; logind's Session.Class property
	would be the authoritative answer, at the cost of a D-Bus call. A session's class never changes.
	"""
	return os.environ.get("XDG_SESSION_CLASS") == "greeter"
