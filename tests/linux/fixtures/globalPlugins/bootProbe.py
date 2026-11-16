"""A global plugin for test_boot.py: once NVDA has started, it says one sentence.

BASEDREADER_BOOT_PROBE turns it on and says what follows the sentence:
- "exit" asks NVDA to exit through core.triggerNVDAExit.
- "wait" leaves NVDA running until something signals it.
- "restart" restarts NVDA through core.restart, with the variable changed to "exit" for the copy
	that replaces it.
- "exitDialog" runs NVDA's exit command, which asks first under upstream's default configuration,
	says exitDialogSentence once that dialog is open, and waits.
- "magnifier" runs the magnifier's toggle command and then exits as "exit" does.
- "slowStart" takes six seconds longer to start, as a plugin with a slow constructor would, and then
	waits.
- "slowExit" makes this plugin's terminate, which NVDA's exit calls, take two seconds.
- "hangAtExit" changes a setting and leaves a thread running that NVDA's exit waits for after its
	main loop, and never ends it.
- "hangInTerminate" changes a setting and makes this plugin's terminate never return.
- "hangInC" changes a setting and makes the save of the configuration that NVDA's exit makes after
	its main loop never return, blocked in C code that holds the GIL, so that no Python code runs
	again in the process.
Each of the following changes a setting, opens one or more modal dialogs and says dialogSentence once
the innermost is open. Each call that showed a dialog logs the answer it got, as
"boot probe: NAME answered ANSWER".
- "messageBox" opens an error message box through gui.messageBox, which most of NVDA still uses.
- "question" does the same with a yes or no question, which has no answer to fall back on.
- "okCancel" does the same with OK and Cancel, the confirmation whose answer matters most.
- "alert" opens one through gui.message.MessageDialog.alert, which returns no answer.
- "confirm" opens one through gui.message.MessageDialog.confirm.
- "nativeMessageBox" opens wx.MessageBox, which wxGTK shows with GTK's own message dialog.
- "fileDialog" opens wx.FileDialog, which wxGTK shows with GTK's own file chooser.
- "nestedDialogs" opens a wx dialog, and another inside it.
- "dialogInWelcome" opens a wx dialog inside the welcome dialog of a first start.
- "reopening" opens wx.MessageBox again each time it is answered, for ever.
- "followUp" opens a wx dialog, and another inside it. Once the inner one is answered, the call that
	showed it goes on handling events for a while, as code that reports what its cancelled dialog
	left undone may, and then asks a question with wx.MessageBox, logged as "followUp".
- "neverReturns" does the same as "followUp", but goes on handling events for ever instead.
Those that wait, wait for something to signal NVDA, or with "+exit" appended ask NVDA to exit through
core.triggerNVDAExit once they are ready, from inside the innermost dialog where there is one.
Unset, the plugin does nothing, so that other tests which add this directory to globalPlugins
never notice it.
"""

import ctypes
import os
import threading
import time
from collections.abc import Callable

import config
import core
import globalPluginHandler
import ui
import wx
from logHandler import log

ENVIRONMENT_VARIABLE = "BASEDREADER_BOOT_PROBE"
dialogModes = (
	"messageBox",
	"question",
	"okCancel",
	"alert",
	"confirm",
	"nativeMessageBox",
	"fileDialog",
	"nestedDialogs",
	"dialogInWelcome",
	"reopening",
	"followUp",
	"neverReturns",
)
otherModes = (
	"exit",
	"wait",
	"restart",
	"exitDialog",
	"magnifier",
	"slowStart",
	"slowExit",
	"hangAtExit",
	"hangInTerminate",
	"hangInC",
)
#: Seconds "slowExit" adds to this plugin's terminate.
SLOW_EXIT = 2.0
#: Seconds "followUp" goes on handling events after its inner dialog is answered: many turns of the
#: main loop, and well within modalDialogs.RETURN_TIMEOUT.
FOLLOW_UP_DELAY = 0.5
#: test_boot.py looks for exactly these in the transcript.
sentence = "the boot probe has started"
dialogSentence = "the boot probe's dialog is open"
exitDialogSentence = "the exit dialog is open"
#: What dialog modes change before opening a dialog, so that a test can see the configuration saved.
#: Nothing the probe does types, so it changes nothing that is spoken.
changedSetting = ("keyboard", "speakTypedWords", 2)
#: Milliseconds between looks for the dialog the probe opened.
_POLL_INTERVAL = 50
#: Seconds "slowStart" adds to NVDA's start, more than a replacing copy's singleInstance.QUIT_TIMEOUT.
SLOW_START = 6.0


class GlobalPlugin(globalPluginHandler.GlobalPlugin):
	def __init__(self) -> None:
		super().__init__()
		self._mode = os.environ.get(ENVIRONMENT_VARIABLE) or None
		if self._mode is None:
			return
		self._mode, _, then = self._mode.partition("+")
		self._exitWhenOpen = then == "exit"
		if self._mode not in (*dialogModes, *otherModes) or then not in ("", "exit"):
			raise ValueError(
				f"{ENVIRONMENT_VARIABLE} is {self._mode!r}, not one of {dialogModes + otherModes}"
			)
		if self._mode == "slowStart":
			time.sleep(SLOW_START)
		# postNvdaStartup holds its handlers weakly; globalPluginHandler keeps this plugin alive.
		core.postNvdaStartup.register(self._onNvdaStarted)

	def _onNvdaStarted(self) -> None:
		ui.message(sentence)
		if self._mode == "exit":
			core.triggerNVDAExit()
		elif self._mode == "restart":
			# The new copy inherits this environment.
			os.environ[ENVIRONMENT_VARIABLE] = "exit"
			core.restart()
		elif self._mode == "exitDialog":
			import gui

			gui.mainFrame.onExitCommand(None)
			self._sayOnceOpen(
				lambda: any(isinstance(w, gui.ExitDialog) and w.IsShown() for w in wx.GetTopLevelWindows()),
				exitDialogSentence,
			)
		elif self._mode == "magnifier":
			import _magnifier.commands

			_magnifier.commands.toggleMagnifier()
			core.triggerNVDAExit()
		elif self._mode in ("hangAtExit", "hangInTerminate", "hangInC"):
			self._changeSetting()
			if self._mode == "hangAtExit":
				threading.Thread(target=threading.Event().wait, name="bootProbeHang").start()
			elif self._mode == "hangInC":
				# A function of the module, which the extension point's weak reference keeps after this
				# plugin is terminated, which is before the save.
				config.post_configSave.register(_blockInCOnceOutOfTheMainLoop)
			if self._exitWhenOpen:
				core.triggerNVDAExit()
		elif self._mode in dialogModes:
			self._changeSetting()
			# Each of these returns only once its dialog is answered, so it runs after this handler.
			wx.CallAfter(getattr(self, f"_{self._mode}"))
		elif self._exitWhenOpen:
			core.triggerNVDAExit()

	def _changeSetting(self) -> None:
		section, key, value = changedSetting
		config.conf[section][key] = value

	def _answered(self, name: str, answer: int | None) -> None:
		log.info(f"boot probe: {name} answered {answer if answer is None else int(answer)}")

	def _whenOpen(self, isOpen: Callable[[], bool]) -> None:
		def opened() -> None:
			if self._exitWhenOpen:
				core.triggerNVDAExit()

		self._sayOnceOpen(isOpen, dialogSentence, opened)

	def _sayOnceOpen(
		self, isOpen: Callable[[], bool], text: str, then: Callable[[], None] = lambda: None
	) -> None:
		if isOpen():
			ui.message(text)
			then()
		else:
			wx.CallLater(_POLL_INTERVAL, self._sayOnceOpen, isOpen, text, then)

	def _messageBox(self, style: int = wx.OK | wx.ICON_ERROR) -> None:
		import gui.message

		# An error, as most of NVDA's message boxes are, which also plays the system's error sound.
		self._whenOpen(gui.message.isModalMessageBoxActive)
		self._answered(self._mode, gui.message.messageBox("Opened by the boot probe.", "Boot probe", style))

	def _question(self) -> None:
		self._messageBox(wx.YES_NO)

	def _okCancel(self) -> None:
		self._messageBox(wx.OK | wx.CANCEL)

	def _alert(self) -> None:
		import gui.message

		self._whenOpen(gui.message.isModalMessageBoxActive)
		self._answered(self._mode, gui.message.MessageDialog.alert("Opened by the boot probe.", "Boot probe"))

	def _confirm(self) -> None:
		import gui.message

		self._whenOpen(gui.message.isModalMessageBoxActive)
		self._answered(
			self._mode, gui.message.MessageDialog.confirm("Opened by the boot probe.", "Boot probe")
		)

	def _nativeMessageBox(self) -> None:
		import gui

		self._whenOpen(_isNativeDialogOpen)
		answer = wx.MessageBox("Opened by the boot probe.", "Boot probe", wx.OK | wx.CANCEL, gui.mainFrame)
		self._answered(self._mode, answer)

	def _reopening(self) -> None:
		import gui

		self._whenOpen(_isNativeDialogOpen)
		while True:
			wx.MessageBox("Opened by the boot probe again.", "Boot probe", wx.OK | wx.CANCEL, gui.mainFrame)

	def _fileDialog(self) -> None:
		import gui

		with wx.FileDialog(gui.mainFrame, "Opened by the boot probe") as dialog:
			self._whenOpen(_isNativeDialogOpen)
			self._answered(self._mode, dialog.ShowModal())

	def _nestedDialogs(self) -> None:
		import gui

		with wx.Dialog(gui.mainFrame, title="Outer boot probe dialog") as outer:
			wx.CallAfter(self._innerDialog, outer)
			self._answered("outer", outer.ShowModal())

	def _innerDialog(self, outer: wx.Dialog) -> None:
		with wx.Dialog(outer, title="Inner boot probe dialog") as inner:
			self._whenOpen(inner.IsModal)
			self._answered("inner", inner.ShowModal())

	def _followUp(self) -> None:
		import gui

		with wx.Dialog(gui.mainFrame, title="Outer boot probe dialog") as outer:
			wx.CallAfter(self._innerThenFollowUp, outer)
			self._answered("outer", outer.ShowModal())

	def _neverReturns(self) -> None:
		self._followUp()

	def _innerThenFollowUp(self, outer: wx.Dialog) -> None:
		self._innerDialog(outer)
		deadline = time.monotonic() + FOLLOW_UP_DELAY
		while self._mode == "neverReturns" or time.monotonic() < deadline:
			wx.GetApp().Yield(onlyIfNeeded=True)
			time.sleep(0.01)
		answer = wx.MessageBox(
			"Asked by the boot probe after its dialog.", "Boot probe", wx.OK | wx.CANCEL, outer
		)
		self._answered("followUp", answer)

	def _dialogInWelcome(self) -> None:
		import gui.startupDialogs

		welcome = [
			w
			for w in wx.GetTopLevelWindows()
			if isinstance(w, gui.startupDialogs.WelcomeDialog) and w.IsModal()
		]
		if not welcome:
			wx.CallLater(_POLL_INTERVAL, self._dialogInWelcome)
			return
		self._innerDialog(welcome[0])

	def terminate(self) -> None:
		if self._mode == "hangInTerminate":
			threading.Event().wait()
		elif self._mode == "slowExit":
			time.sleep(SLOW_EXIT)
		core.postNvdaStartup.unregister(self._onNvdaStarted)
		super().terminate()


def _isNativeDialogOpen() -> bool:
	import gi

	gi.require_version("Gtk", "3.0")
	from gi.repository import Gtk

	return isinstance(Gtk.grab_get_current(), Gtk.Dialog)


#: A default pthread mutex, which all zeroes is, and room to spare.
_mutex = ctypes.create_string_buffer(64)


def _blockInCOnceOutOfTheMainLoop() -> None:
	if wx.App.IsMainLoopRunning():
		return
	log.info("boot probe: blocking in C")
	# PyDLL keeps the GIL through the call. A default mutex locked again by its holder never returns.
	libc = ctypes.PyDLL(None)
	libc.pthread_mutex_lock(_mutex)
	libc.pthread_mutex_lock(_mutex)
