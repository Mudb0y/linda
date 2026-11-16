"""A global plugin for test_boot.py: once NVDA has started, it says one sentence.

BASEDREADER_BOOT_PROBE turns it on and says what follows the sentence: "exit" asks NVDA to exit
through core.triggerNVDAExit, "wait" leaves NVDA running until something signals it, and "restart"
restarts NVDA through core.restart, with the variable changed to "exit" for the copy that replaces
it. Unset, the plugin does nothing, so that other tests which add this directory to globalPlugins
never notice it.
"""

import os

import core
import globalPluginHandler
import ui

environmentVariable = "BASEDREADER_BOOT_PROBE"
afterSpeaking = ("exit", "wait", "restart")
#: test_boot.py looks for exactly this in the transcript.
sentence = "the boot probe has started"


class GlobalPlugin(globalPluginHandler.GlobalPlugin):
	def __init__(self) -> None:
		super().__init__()
		self._after = os.environ.get(environmentVariable) or None
		if self._after is None:
			return
		if self._after not in afterSpeaking:
			raise ValueError(f"{environmentVariable} is {self._after!r}, not one of {afterSpeaking}")
		# postNvdaStartup holds its handlers weakly; globalPluginHandler keeps this plugin alive.
		core.postNvdaStartup.register(self._onNvdaStarted)

	def _onNvdaStarted(self) -> None:
		ui.message(sentence)
		if self._after == "exit":
			core.triggerNVDAExit()
		elif self._after == "restart":
			# The new copy inherits this environment.
			os.environ[environmentVariable] = "exit"
			core.restart()

	def terminate(self) -> None:
		core.postNvdaStartup.unregister(self._onNvdaStarted)
		super().terminate()
