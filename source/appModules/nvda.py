# A part of NonVisual Desktop Access (NVDA)
# Copyright (C) 2008-2025 NV Access Limited, James Teh, Michael Curran, Leonard de Ruijter, Reef Turner,
# Julien Cochuyt
# This file may be used under the terms of the GNU General Public License, version 2 or later.
# For more details see: https://www.gnu.org/licenses/gpl-2.0.html


import typing  # noqa: I001
import sys

import appModuleHandler
import api
import buildVersion
import controlTypes
from NVDAObjects.IAccessible import IAccessible
from baseObject import ScriptableObject
import gui
from scriptHandler import script
import speech
import textInfos
import config
import ui

if typing.TYPE_CHECKING:
	import inputCore


nvdaMenuIaIdentity = None


class NvdaDialog(IAccessible):
	"""Fix to ensure NVDA message dialogs get reported when they pop up."""

	def _get_presentationType(self):
		presType = super().presentationType
		# Sometimes, NVDA message dialogs briefly report the invisible state
		# after they're focused.
		# This causes them to be treated as unavailable and they are thus not reported.
		# If this dialog is in the foreground, treat it as content.
		if presType == self.presType_unavailable and self == api.getForegroundObject():
			return self.presType_content
		return presType


class NvdaDialogEmptyDescription(IAccessible):
	"""Fix to ensure the NVDA settings dialog does not run getDialogText including panel descriptions
	when alt+tabbing back to a focused control on a panel with a description. This would result in the
	description being spoken twice.
	"""

	def _get_description(self):
		"""Override the description property (because we can't override the classmethod getDialogText)
		However this will do to ensure that the dialog is described as we wish.
		"""
		return ""


if sys.platform != "win32":
	import core
	import wx
	from linuxPlatform import ownGui
	from linuxPlatform.atspi import client as atspiClient
	from NVDAObjects import NVDAObject
	from NVDAObjects.AtSpi import TableCell, TableRowItem
	from NVDAObjects.behaviors import Dialog

	class NvdaGtkDialog(NVDAObject):
		"""One of NVDA's own wx dialogs, which wxGTK shows as a frame, so that its text is read as a
		dialog's is on Windows."""

		def _get_role(self):
			return controlTypes.Role.DIALOG

	class NvdaGtkSettingsDialog(NVDAObject):
		"""A settings dialog, whose panel descriptions would be read twice, as NvdaDialogEmptyDescription
		says."""

		def _get_description(self):
			return ""

	class NvdaGtkReadOnlyField(NVDAObject):
		"""A read-only field that takes the focus, such as the synthesizer's name in the speech settings,
		which GTK presents as text that cannot be edited and Windows as a read-only edit field, whose
		text is read when it is focused."""

		def _get_role(self):
			return controlTypes.Role.EDITABLETEXT

		def _get_states(self):
			return super().states | {controlTypes.State.READONLY}

	class NvdaGtkCheckListItem(NVDAObject):
		"""The check box of an item in one of NVDA's check lists, such as the NVDA modifier keys, which
		GTK presents as a table whose rows have a check box cell and a text cell; Windows presents each
		item as a check box named by its text."""

		def _get_role(self):
			return controlTypes.Role.CHECKBOX

		def _get_name(self):
			textCell = self.next
			if textCell is None:
				return ""
			# The cell's own text: as a row item, the text cell would read the whole row.
			try:
				return atspiClient.client.name(textCell.node)
			except LookupError:
				return ""

		def _get_states(self):
			return (super().states | {controlTypes.State.CHECKABLE}) - {
				controlTypes.State.SELECTED,
				controlTypes.State.SELECTABLE,
			}

		def _get_positionInfo(self):
			try:
				return self._tableCellPosition()
			except LookupError:
				return {}

		def _get_rowNumber(self):
			return None

		def _get_columnNumber(self):
			return None

	class NvdaGtkWxList(NVDAObject):
		"""A wx list control, such as the settings dialog's categories, which wxGTK draws itself and
		presents with its label on the scroll pane around it, and without saying which of its items has
		the focus until it moves."""

		def _get_name(self):
			name = super().name
			if name:
				return name
			obj = self.parent
			for _level in range(2):
				if obj is None:
					break
				if obj.role == controlTypes.Role.SCROLLPANE:
					return obj.name
				obj = obj.parent
			return ""

		def _get_value(self):
			return ownGui.focusedListItemText()

	class NvdaGtkWxListItem(NVDAObject):
		"""An item of a wx list control, which wxGTK reports focused a moment before it reports it
		selected, so that it would be read as not selected. NVDA's own lists select the item they focus."""

		def _get_states(self):
			states = super().states
			if controlTypes.State.FOCUSED in states:
				states = states | {controlTypes.State.SELECTED}
			return states

	class NvdaGtkGroupingLabel(NVDAObject):
		"""The label GTK keeps inside a group box, which Windows does not have as an object of its own and
		which would otherwise be read as part of a dialog's text."""

		def _get_states(self):
			return super().states | {controlTypes.State.INVISIBLE}

	def _shownWxDialog(title: str) -> wx.Dialog | None:
		for window in wx.GetTopLevelWindows():
			if isinstance(window, wx.Dialog) and window.IsShown() and window.GetTitle() == title:
				return window
		return None


# Translators: The name of a category of NVDA commands.
SCRCAT_PYTHON_CONSOLE = _("Python Console")


class NvdaPythonConsoleUIOutputClear(ScriptableObject):
	# Allow the bound gestures to be edited through the Input Gestures dialog (see L{gui.prePopup})
	isPrevFocusOnNvdaPopup = True

	@script(
		gesture="kb:control+l",
		# Translators: Description of a command to clear the Python Console output pane
		description=_("Clear the output pane"),
		category=SCRCAT_PYTHON_CONSOLE,
	)
	def script_clearOutput(self, gesture: "inputCore.InputGesture"):
		from pythonConsole import consoleUI

		consoleUI.clear()
		# Translators: Description of a message spoken when clearing the Python Console output pane
		ui.message(_("Output pane cleared"))


class NvdaPythonConsoleUIOutputCtrl(ScriptableObject):
	# Allow the bound gestures to be edited through the Input Gestures dialog (see L{gui.prePopup})
	isPrevFocusOnNvdaPopup = True

	@script(
		gesture="kb:alt+downArrow",
		# Translators: Description of a command to move to the next result in the Python Console output pane
		description=_("Move to the next result"),
		category=SCRCAT_PYTHON_CONSOLE,
	)
	def script_moveToNextResult(self, gesture: "inputCore.InputGesture"):
		self._resultNavHelper(direction="next", select=False)

	@script(
		gesture="kb:alt+upArrow",
		# Translators: Description of a command to move to the previous result
		# in the Python Console output pane
		description=_("Move to the previous result"),
		category=SCRCAT_PYTHON_CONSOLE,
	)
	def script_moveToPrevResult(self, gesture: "inputCore.InputGesture"):
		self._resultNavHelper(direction="previous", select=False)

	@script(
		gesture="kb:alt+downArrow+shift",
		# Translators: Description of a command to select from the current caret position to the end
		# of the current result in the Python Console output pane
		description=_("Select until the end of the current result"),
		category=SCRCAT_PYTHON_CONSOLE,
	)
	def script_selectToResultEnd(self, gesture: "inputCore.InputGesture"):
		self._resultNavHelper(direction="next", select=True)

	@script(
		gesture="kb:alt+shift+upArrow",
		# Translators: Description of a command to select from the current caret position to the start
		# of the current result in the Python Console output pane
		description=_("Select until the start of the current result"),
		category=SCRCAT_PYTHON_CONSOLE,
	)
	def script_selectToResultStart(self, gesture: "inputCore.InputGesture"):
		self._resultNavHelper(direction="previous", select=True)

	def _resultNavHelper(self, direction: str = "next", select: bool = False):
		from pythonConsole import consoleUI

		startPos, endPos = consoleUI.outputCtrl.GetSelection()
		if self.isTextSelectionAnchoredAtStart:
			curPos = endPos
		else:
			curPos = startPos
		if direction == "previous":
			for pos in reversed(consoleUI.outputPositions):
				if pos < curPos:
					break
			else:
				# Translators: Reported when attempting to move to the previous result in the Python Console
				# output pane while there is no previous result.
				speech.speakMessage(_("Top"))
				return
		elif direction == "next":
			for pos in consoleUI.outputPositions:
				if pos > curPos:
					break
			else:
				# Translators: Reported when attempting to move to the next result in the Python Console
				# output pane while there is no next result.
				speech.speakMessage(_("Bottom"))
				return
		else:
			raise ValueError(f"Unexpected direction: {direction!r}")
		if select:
			consoleUI.outputCtrl.Freeze()
			anchorPos = startPos if self.isTextSelectionAnchoredAtStart else endPos
			consoleUI.outputCtrl.SetSelection(anchorPos, pos)
			consoleUI.outputCtrl.Thaw()
			self.detectPossibleSelectionChange()
			self.isTextSelectionAnchoredAtStart = anchorPos < pos
		else:
			consoleUI.outputCtrl.SetSelection(pos, pos)
		info = self.makeTextInfo(textInfos.POSITION_CARET)
		copy = info.copy()
		info.expand(textInfos.UNIT_LINE)
		if copy.move(textInfos.UNIT_CHARACTER, 4, endPoint="end") == 4 and copy.text == ">>> ":
			info.move(textInfos.UNIT_CHARACTER, 4, endPoint="start")
		speech.speakTextInfo(info, reason=controlTypes.OutputReason.CARET)


class AppModule(appModuleHandler.AppModule):
	# The configuration profile that has been previously edited.
	# This ought to be a class property.
	oldProfile = None

	@classmethod
	def handlePossibleProfileSwitch(cls):
		from gui.settingsDialogs import NvdaSettingsDialogActiveConfigProfile as newProfile

		if cls.oldProfile and newProfile and newProfile != cls.oldProfile:
			# Translators: A message announcing what configuration profile is currently being edited.
			speech.speakMessage(_("Editing profile {profile}").format(profile=newProfile))
		cls.oldProfile = newProfile

	def event_appModule_loseFocus(self):
		self.oldProfile = None

	def isNvdaMenu(self, obj):
		global nvdaMenuIaIdentity
		if sys.platform != "win32":
			# GTK gives each submenu a window of its own too, but parents it to the item that opens it.
			return (
				gui._isNvdaMenuOpen
				and obj.role == controlTypes.Role.POPUPMENU
				and obj.parent is not None
				and obj.parent.role == controlTypes.Role.WINDOW
			)
		if not isinstance(obj, IAccessible):
			return False
		if nvdaMenuIaIdentity and obj.IAccessibleIdentity == nvdaMenuIaIdentity:
			return True
		if nvdaMenuIaIdentity is not True:
			return False
		# nvdaMenuIaIdentity is True, so the next menu we encounter is the NVDA menu.
		if obj.role == controlTypes.Role.POPUPMENU:
			nvdaMenuIaIdentity = obj.IAccessibleIdentity
			return True
		return False

	def event_NVDAObject_init(self, obj):
		# It seems that context menus always get the name "context" and this cannot be overridden.
		# Fudge the name of the NVDA system tray menu to make it more friendly.
		if self.isNvdaMenu(obj):
			obj.name = buildVersion.name

	def event_gainFocus(self, obj, nextHandler):
		if sys.platform != "win32" and core._hasShutdownBeenTriggered:
			# NVDA's windows are being torn down, and asking GTK about them, through NVDA's own
			# accessibility bridge, can then hang the exit.
			return
		if obj.role == controlTypes.Role.UNKNOWN and controlTypes.State.INVISIBLE in obj.states:
			return
		if sys.platform != "win32" and gui._isNvdaMenuOpen and obj.role == controlTypes.Role.WINDOW:
			# The unnamed window GTK puts the NVDA menu in has the focus only until the first item is
			# selected, a moment later.
			return
		if sys.platform != "win32" and ownGui.isPassingFocus(obj):
			# GTK focuses a dialog's default button as it shows it, before NVDA focuses the control it
			# chose, as wxMSW does before the dialog is shown.
			return
		nextHandler()

	# Silence invisible unknowns for stateChange as well.
	event_stateChange = event_gainFocus

	def event_foreground(self, obj, nextHandler):
		if not gui.shouldConfigProfileTriggersBeSuspended():
			config.conf.resumeProfileTriggers()
		else:
			self.handlePossibleProfileSwitch()
		nextHandler()

	def event_nameChange(self, obj, nextHandler):
		self.handlePossibleProfileSwitch()
		nextHandler()

	def isNvdaSettingsDialog(self, obj):
		if not isinstance(obj, IAccessible):
			return False
		windowHandle = obj.windowHandle
		from gui.settingsDialogs import NvdaSettingsDialogWindowHandle

		if windowHandle == NvdaSettingsDialogWindowHandle:  # noqa: SIM103
			return True
		return False

	def isNvdaPythonConsoleUIInputCtrl(self, obj):
		from pythonConsole import consoleUI

		if not consoleUI:
			return
		return obj.windowHandle == consoleUI.inputCtrl.GetHandle()

	def isNvdaPythonConsoleUIOutputCtrl(self, obj):
		from pythonConsole import consoleUI

		if not consoleUI:
			return
		return obj.windowHandle == consoleUI.outputCtrl.GetHandle()

	def chooseNVDAObjectOverlayClasses(self, obj, clsList):
		if sys.platform != "win32":
			# An AT-SPI object, which has no window class to tell NVDA's own dialogs apart by.
			if obj.role == controlTypes.Role.FRAME and (dialog := _shownWxDialog(obj.name)) is not None:
				clsList.insert(0, Dialog)
				clsList.insert(0, NvdaGtkDialog)
				if isinstance(dialog, gui.settingsDialogs.SettingsDialog):
					clsList.insert(0, NvdaGtkSettingsDialog)
			elif obj.role == controlTypes.Role.STATICTEXT and getattr(obj, "atspiRole", None) == "text":
				if controlTypes.State.FOCUSABLE in obj.states:
					clsList.insert(0, NvdaGtkReadOnlyField)
			elif (
				# The cell of a GTK 3 tree view is a row item, and that of any other table a TableCell.
				(TableRowItem in clsList or TableCell in clsList)
				and getattr(obj, "atspiRole", None) == "table cell"
				and not obj.name
				and ownGui.focusIsCheckList()
			):
				clsList.insert(0, NvdaGtkCheckListItem)
			elif obj.role == controlTypes.Role.LIST and getattr(obj, "atspiRole", None) == "list":
				clsList.insert(0, NvdaGtkWxList)
			elif obj.role == controlTypes.Role.LISTITEM and getattr(obj, "atspiRole", None) == "list item":
				clsList.insert(0, NvdaGtkWxListItem)
			elif obj.role == controlTypes.Role.STATICTEXT and getattr(obj, "atspiRole", None) == "label":
				parent = obj.parent
				if (
					parent is not None
					and parent.role == controlTypes.Role.GROUPING
					and parent.name == obj.name
				):
					clsList.insert(0, NvdaGtkGroupingLabel)
			return
		if obj.windowClassName == "#32770" and obj.role == controlTypes.Role.DIALOG:
			clsList.insert(0, NvdaDialog)
			if self.isNvdaSettingsDialog(obj):
				clsList.insert(0, NvdaDialogEmptyDescription)
				return
		if self.isNvdaPythonConsoleUIInputCtrl(obj):
			clsList.insert(0, NvdaPythonConsoleUIOutputClear)
		elif self.isNvdaPythonConsoleUIOutputCtrl(obj):
			clsList.insert(0, NvdaPythonConsoleUIOutputClear)
			clsList.insert(0, NvdaPythonConsoleUIOutputCtrl)
