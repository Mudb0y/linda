"""A global plugin for test_atspiTerminal.py: with LINDA_TERMINAL_ADDON set, it does to every terminal
what an add-on written for NVDA, Terminal Access for one, does: it puts a class of its own before the
terminal's, which overrides _reportNewLines with NVDA's signature and logs each call. Unset, the
plugin does nothing.
"""

import os

import controlTypes
import globalPluginHandler
from logHandler import log
from NVDAObjects.behaviors import Terminal

ENVIRONMENT_VARIABLE = "LINDA_TERMINAL_ADDON"


class AddonTerminal(Terminal):
	def _reportNewLines(self, lines):
		log.info(f"terminal add-on: reporting {lines!r}")
		super()._reportNewLines(lines)


class GlobalPlugin(globalPluginHandler.GlobalPlugin):
	def chooseNVDAObjectOverlayClasses(self, obj, clsList):
		if os.environ.get(ENVIRONMENT_VARIABLE) and obj.role == controlTypes.Role.TERMINAL:
			clsList.insert(0, AddonTerminal)
