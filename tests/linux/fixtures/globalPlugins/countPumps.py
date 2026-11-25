"""A global plugin for test_altTabKeepsTheWindow.py: with LINDA_COUNT_PUMPS naming a file, the AT-SPI
handler's pumps and its looks over every application for an active window are counted, and the counts
written to that file as JSON after each, for a test to read how much work Linda does while it waits.
Unset, the plugin does nothing.
"""

import json
import os

import globalPluginHandler
from linuxPlatform.atspi import handler

ENVIRONMENT_VARIABLE = "LINDA_COUNT_PUMPS"


class GlobalPlugin(globalPluginHandler.GlobalPlugin):
	def __init__(self) -> None:
		super().__init__()
		self._originals = None
		path = os.environ.get(ENVIRONMENT_VARIABLE)
		if not path:
			return
		pumpAll, scan = handler.pumpAll, handler._scanForActiveWindow
		self._originals = (pumpAll, scan)
		counts = {"pumps": 0, "scans": 0}

		def write() -> None:
			with open(path + ".tmp", "w") as file:
				json.dump(counts, file)
			os.replace(path + ".tmp", path)

		def countedPump() -> None:
			counts["pumps"] += 1
			pumpAll()
			write()

		def countedScan():
			counts["scans"] += 1
			return scan()

		handler.pumpAll = countedPump
		handler._scanForActiveWindow = countedScan

	def terminate(self) -> None:
		if self._originals is not None:
			handler.pumpAll, handler._scanForActiveWindow = self._originals
		super().terminate()
