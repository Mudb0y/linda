"""A global plugin for test_ownGui.py: with LINDA_BIG_LOG set to a number of megabytes, it writes about
that much to NVDA's log as it is loaded, as a long session with debug logging does. Unset, the plugin
does nothing.
"""

import os

import globalPluginHandler
from logHandler import log

ENVIRONMENT_VARIABLE = "LINDA_BIG_LOG"
#: Lines of 160 bytes each, numbered so that a test can tell which part of the log it hears.
_LINES_PER_RECORD = 6400


class GlobalPlugin(globalPluginHandler.GlobalPlugin):
	def __init__(self) -> None:
		super().__init__()
		megabytes = int(os.environ.get(ENVIRONMENT_VARIABLE) or 0)
		line = "x" * 150
		number = 0
		for _record in range(megabytes):
			log.info("\n".join(f"{number + i:08d} {line}" for i in range(_LINES_PER_RECORD)))
			number += _LINES_PER_RECORD
