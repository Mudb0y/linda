"""A global plugin for test_boot.py: once NVDA has started, it says one sentence.

BASEDREADER_BOOT_PROBE turns it on and says what follows the sentence:
- "exit" asks NVDA to exit through core.triggerNVDAExit.
- "wait" leaves NVDA running until something signals it.
- "restart" restarts NVDA through core.restart, with the variable changed to "exit" for the copy
	that replaces it.
- "messageBox" opens a modal error message box through gui.messageBox, which most of NVDA still
	uses, says messageBoxSentence once it is open, and waits.
- "question" does the same with a yes or no question, which has no answer to fall back on.
- "alert" does the same through gui.message.MessageDialog, which replaces gui.messageBox.
- "exitDialog" runs NVDA's exit command, which asks first under upstream's default configuration,
	says exitDialogSentence once that dialog is open, and waits.
- "magnifier" runs the magnifier's toggle command and then exits as "exit" does.
Unset, the plugin does nothing, so that other tests which add this directory to globalPlugins
never notice it.
"""

import os
from collections.abc import Callable

import core
import globalPluginHandler
import ui
import wx

environmentVariable = "BASEDREADER_BOOT_PROBE"
afterSpeaking = ("exit", "wait", "restart", "messageBox", "question", "alert", "exitDialog", "magnifier")
#: test_boot.py looks for exactly these in the transcript.
sentence = "the boot probe has started"
messageBoxSentence = "the boot probe's message box is open"
exitDialogSentence = "the exit dialog is open"
#: Milliseconds between looks for the dialog the probe opened.
_POLL_INTERVAL = 50


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
		elif self._after in ("messageBox", "question", "alert"):
			import gui.message

			if self._after == "alert":
				show = (gui.message.MessageDialog.alert, "Opened by the boot probe.", "Boot probe")
			else:
				# An error, as most of NVDA's message boxes are, which also plays the system's error sound.
				style = wx.YES_NO if self._after == "question" else wx.OK | wx.ICON_ERROR
				show = (gui.message.messageBox, "Opened by the boot probe.", "Boot probe", style)
			# Each shows the box and returns only once it is answered, so it runs after this handler.
			wx.CallAfter(*show)
			self._sayOnceOpen(gui.message.isModalMessageBoxActive, messageBoxSentence)
		elif self._after == "exitDialog":
			import gui

			gui.mainFrame.onExitCommand(None)
			self._sayOnceOpen(
				lambda: any(isinstance(w, gui.ExitDialog) and w.IsShown() for w in wx.GetTopLevelWindows()),
				exitDialogSentence,
			)
		elif self._after == "magnifier":
			import _magnifier.commands

			_magnifier.commands.toggleMagnifier()
			core.triggerNVDAExit()

	def _sayOnceOpen(self, isOpen: Callable[[], bool], text: str) -> None:
		if isOpen():
			ui.message(text)
		else:
			wx.CallLater(_POLL_INTERVAL, self._sayOnceOpen, isOpen, text)

	def terminate(self) -> None:
		core.postNvdaStartup.unregister(self._onNvdaStarted)
		super().terminate()
