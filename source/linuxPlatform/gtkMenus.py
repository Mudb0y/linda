"""NVDA's own popup menus on wxGTK.

wx pops a menu up as GTK does for the pointer, with nothing selected, so that the focus lands on the
menu's unnamed window and the first arrow key is spent selecting an item. A menu opened from the
keyboard should open on its first item, as Windows opens NVDA's menu and as GTK opens a context menu
from the Menu key.
"""

import gi
import wx

# wxGTK has already loaded GTK 3; this binds to that same library.
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk

#: Milliseconds between looks for the menu, and how many looks it gets to appear.
_INTERVAL = 20
_ATTEMPTS = 25


def selectFirstItemWhenShown() -> None:
	"""Select the first item of the popup menu about to be shown, once GTK has mapped it.

	Called just before a blocking PopupMenu; wx runs the calls queued here inside that menu's loop.
	"""
	wx.CallAfter(_selectFirstItem, _ATTEMPTS)


def _shownPopupMenus() -> list[Gtk.Menu]:
	return [
		window.get_child()
		for window in Gtk.Window.list_toplevels()
		if window.get_window_type() == Gtk.WindowType.POPUP
		and window.get_mapped()
		and isinstance(window.get_child(), Gtk.Menu)
	]


def _selectFirstItem(attemptsLeft: int) -> None:
	menus = _shownPopupMenus()
	if menus:
		menu = menus[0]
		if menu.get_selected_item() is None:
			menu.select_first(True)
	elif attemptsLeft > 1:
		wx.CallLater(_INTERVAL, _selectFirstItem, attemptsLeft - 1)
