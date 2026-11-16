"""NVDA's own GUI, written against wxMSW, made to behave on wxGTK.

install() replaces what NVDA's dialogs do for MSAA. On Windows NVDA patches what its wx controls
tell MSAA: a wx.Accessible for its check lists and settings panels, and WinEvents it sends itself
when a check box or slider changes. wxGTK has no wx.Accessible, whose constructor raises
NotImplementedError there, and no WinEvents, so the settings dialog could not open and a check list
could not be toggled. GTK's own accessibility already covers what those repairs add on Windows: a
check list is a table whose cells carry their checked state, and GTK reports the state and value
changes it makes itself.

It also gives a settings dialog its first focus once the dialog is shown. NVDA puts it on a
control while the dialog is being built, before it is shown, which wxMSW keeps and GTK does not:
GTK focuses the OK button as it shows the dialog.

selectFirstMenuItemWhenShown makes a popup menu open on its first item. wx pops a menu up as GTK
does for the pointer, with nothing selected, so that the focus lands on the menu's unnamed window
and the first arrow key is spent selecting an item. A menu opened from the keyboard should open on
its first item, as Windows opens NVDA's menu and as GTK opens a context menu from the Menu key.
"""

import wx
from gui import nvdaControls, settingsDialogs

#: Milliseconds between looks for a menu being shown, and how many looks it gets to appear.
_MENU_INTERVAL = 20
_MENU_ATTEMPTS = 25


class _NoAccessible:
	"""Takes the place of a wx.Accessible subclass, which wxGTK cannot construct."""

	def __init__(self, window: wx.Window | None = None) -> None:
		self.Window = window


def _setNoAccessible(window: wx.Window, accessible: object) -> None:
	pass


def _skipEvent(self, evt: wx.CommandEvent) -> None:
	evt.Skip()


def _notifyNothing(self, index: int) -> None:
	pass


def _setSliderValue(self, i: int) -> None:
	wx.Slider.SetValue(self, i)
	evt = wx.CommandEvent(wx.wxEVT_COMMAND_SLIDER_UPDATED, self.GetId())
	evt.SetInt(i)
	self.ProcessEvent(evt)


def _isGone(window: wx.Window) -> bool:
	return not window or window.IsBeingDeleted()


def _initSettingsDialog(self, *args, **kwargs) -> None:
	postInit = type(self).postInit

	def postInitOnceActive(evt: wx.ActivateEvent) -> None:
		evt.Skip()
		if not evt.GetActive():
			return
		self.Unbind(wx.EVT_ACTIVATE, handler=postInitOnceActive)
		# After wx has given the focus back to whatever it last saw focused, as it does on activation.
		wx.CallAfter(lambda: None if _isGone(self) else postInit(self))

	def postInitOnceShown() -> None:
		# Later calls, after Apply, focus a dialog already shown and need no deferring.
		del self.postInit
		self.Bind(wx.EVT_ACTIVATE, postInitOnceActive)

	self.postInit = postInitOnceShown
	_upstreamSettingsDialogInit(self, *args, **kwargs)


def _ignoredOnceDestroyed(handler):
	"""A handler that does nothing for a dialog being destroyed.

	wxGTK's generic list control reports its focused item as it is torn down with its dialog, and
	the category change that then runs on the dead dialog raised, leaving the dialog alive through
	the traceback, so that it could not be opened again.
	"""

	def ignoring(self, evt) -> None:
		if not _isGone(self):
			handler(self, evt)

	return ignoring


_upstreamSettingsDialogInit = settingsDialogs.SettingsDialog.__init__


def install() -> None:
	"""Make the changes, once gui has imported the modules they are made to."""
	wx.Window.SetAccessible = _setNoAccessible
	nvdaControls.ListCtrlAccessible = _NoAccessible
	settingsDialogs.SettingsPanelAccessible = _NoAccessible
	nvdaControls.CustomCheckListBox.notifyIAccessible = _skipEvent
	nvdaControls.AutoWidthColumnCheckListCtrl.notifyIAccessible = _notifyNothing
	nvdaControls.EnhancedInputSlider.SetValue = _setSliderValue
	settingsDialogs.SettingsDialog.__init__ = _initSettingsDialog
	for cls in (settingsDialogs.MultiCategorySettingsDialog, settingsDialogs.NVDASettingsDialog):
		cls.onCategoryChange = _ignoredOnceDestroyed(cls.onCategoryChange)


def selectFirstMenuItemWhenShown() -> None:
	"""Select the first item of the popup menu about to be shown, once GTK has mapped it.

	Called just before a blocking PopupMenu; wx runs the calls queued here inside that menu's loop.
	"""
	wx.CallAfter(_selectFirstMenuItem, _MENU_ATTEMPTS)


def _shownPopupMenus() -> list:
	# Imported only once wx is running GTK: importing it initialises GTK, which gui must not do as it
	# is imported, before wx.App exists.
	import gi

	gi.require_version("Gtk", "3.0")
	from gi.repository import Gtk

	return [
		window.get_child()
		for window in Gtk.Window.list_toplevels()
		if window.get_window_type() == Gtk.WindowType.POPUP
		and window.get_mapped()
		and isinstance(window.get_child(), Gtk.Menu)
	]


def _selectFirstMenuItem(attemptsLeft: int) -> None:
	menus = _shownPopupMenus()
	if menus:
		menu = menus[0]
		if menu.get_selected_item() is None:
			menu.select_first(True)
	elif attemptsLeft > 1:
		wx.CallLater(_MENU_INTERVAL, _selectFirstMenuItem, attemptsLeft - 1)
