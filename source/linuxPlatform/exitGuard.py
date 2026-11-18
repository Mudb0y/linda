"""Ends NVDA if its exit hangs once begun.

NVDA reads its own dialogs through AT-SPI, so it is both the client and the server of their
accessibility; tearing those dialogs down at exit can leave the main thread waiting in GTK for ever.
Once exit has begun nothing is left to lose but the configuration and, when NVDA is restarting, the
copy that is to replace it, so after a grace period this saves the one, starts the other and ends the
process.
"""

import os
import threading
from typing import TYPE_CHECKING

from logHandler import log

if TYPE_CHECKING:
	from core import NewNVDAInstance

_timer: threading.Timer | None = None
#: The copy that is to replace this one, until it has been started.
_newInstance: "NewNVDAInstance | None" = None
_hasStartedNewInstance = False
_newInstanceLock = threading.Lock()


def expectNewInstance(newNVDA: "NewNVDAInstance | None") -> None:
	"""Remember the copy that is to replace this one, so that it is started even if exit hangs before
	core starts it: the old copy gone and no new one would leave the user with no screen reader."""
	global _newInstance
	with _newInstanceLock:
		_newInstance = newNVDA


def startNewInstance(newNVDA: "NewNVDAInstance") -> None:
	"""Start the copy that replaces this one, unless one has been started already."""
	global _hasStartedNewInstance
	with _newInstanceLock:
		if _hasStartedNewInstance:
			log.debug("The new NVDA instance has already been started")
			return
		_hasStartedNewInstance = True
	from linuxPlatform.singleInstance import startNewInstance

	startNewInstance(newNVDA.filePath, newNVDA.parameters, newNVDA.directory)


def _expire(seconds: float) -> None:
	log.warning(f"NVDA's exit did not finish within {seconds:g} seconds; ending the process")
	try:
		import config

		config.saveOnExit()
	except Exception:
		log.exception("saving the configuration before ending the process")
	try:
		if _newInstance is not None:
			log.info(f"Starting new NVDA instance before ending the process: {_newInstance}")
			startNewInstance(_newInstance)
	except Exception:
		log.exception("starting the new NVDA instance before ending the process")
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
