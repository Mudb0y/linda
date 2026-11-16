"""Ends NVDA if its exit hangs once begun.

NVDA reads its own dialogs through AT-SPI, so it is both the client and the server of their
accessibility; tearing those dialogs down at exit can leave the main thread waiting in GTK for ever.
Once exit has begun nothing is left to lose but the configuration, so after a grace period this saves
it and ends the process.
"""

import os
import threading

from logHandler import log

_timer: threading.Timer | None = None


def _expire(seconds: float) -> None:
	log.warning(f"NVDA's exit did not finish within {seconds:g} seconds; ending the process")
	try:
		import config

		config.saveOnExit()
	except Exception:
		log.exception("saving the configuration before ending the process")
	finally:
		os._exit(0)


def arm(seconds: float = 5.0) -> None:
	"""End the process with status 0 seconds from now, unless it has ended by then. Arming again does
	nothing."""
	global _timer
	if _timer is not None:
		return
	_timer = threading.Timer(seconds, _expire, args=(seconds,))
	_timer.name = "exitGuard"
	_timer.daemon = True
	_timer.start()
