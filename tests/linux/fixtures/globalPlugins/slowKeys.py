"""A global plugin for the guest's key tests: while the file SLOW_KEYS_FILE holds a number of seconds,
NVDA takes that long over every key press it is handed, as it does while its other threads keep the
interpreter busy. Without the file the plugin does nothing.
"""

import time

import globalPluginHandler
import winInputHook

SLOW_KEYS_FILE = "/tmp/ground/linda-slow-keys"


def _delay() -> float:
	try:
		with open(SLOW_KEYS_FILE) as file:
			return float(file.read().strip() or 0)
	except (OSError, ValueError):
		return 0.0


class GlobalPlugin(globalPluginHandler.GlobalPlugin):
	def __init__(self) -> None:
		super().__init__()
		self._callback = winInputHook.keyDownCallback
		if self._callback is None:
			return

		def slowly(*args):
			delay = _delay()
			if delay:
				time.sleep(delay)
			return self._callback(*args)

		winInputHook.keyDownCallback = slowly

	def terminate(self) -> None:
		if self._callback is not None:
			winInputHook.keyDownCallback = self._callback
		super().terminate()
