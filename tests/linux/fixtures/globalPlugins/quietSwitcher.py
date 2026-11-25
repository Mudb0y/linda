"""A global plugin for test_altTabKeepsTheWindow.py: with LINDA_QUIET_SWITCHER set, marco's windows
never say they are active, nor send a window event, as marco 1.26's Alt+Tab switcher does not, where the
rig's 1.28 says it is active while Alt is held. Its popup still names each window Tab chooses. Unset, the
plugin does nothing.
"""

import os

import globalPluginHandler
from linuxPlatform.atspi import client as clientModule
from linuxPlatform.atspi import handler

ENVIRONMENT_VARIABLE = "LINDA_QUIET_SWITCHER"
SWITCHER = "marco"


def _isSwitchers(node) -> bool:
	return node is not None and handler._applicationName(node.busName) == SWITCHER


class GlobalPlugin(globalPluginHandler.GlobalPlugin):
	def __init__(self) -> None:
		super().__init__()
		self._originals = None
		if not os.environ.get(ENVIRONMENT_VARIABLE):
			return
		states, asWindowEvent = clientModule.Client.states, handler._asWindowEvent
		self._originals = (states, asWindowEvent)

		def quietStates(client, node):
			found = states(client, node)
			return found - {"active"} if _isSwitchers(node) else found

		def quietWindowEvent(event):
			if event[0].startswith("window:") and _isSwitchers(event[1]):
				return None
			converted = asWindowEvent(event)
			if converted is not None and converted[0].startswith("window:") and _isSwitchers(converted[1]):
				return None
			return converted

		clientModule.Client.states = quietStates
		handler._asWindowEvent = quietWindowEvent

	def terminate(self) -> None:
		if self._originals is not None:
			clientModule.Client.states, handler._asWindowEvent = self._originals
		super().terminate()
