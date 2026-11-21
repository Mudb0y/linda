"""NVDA's menu as a window of its own, which ownGui shows in place of the tray icon's popup on a Wayland
display.

There a popup menu needs a mapped parent and the serial of input NVDA's own windows received; the tray
icon's menu hangs off a window that is never mapped, and NVDA takes its keys from its key route, not from
the compositor. A new top-level window is what Mutter, KWin and sway give the keyboard to as it maps, so
the menu is shown as a modal dialog holding a tree built from the menu as it is at that moment, a
submenu being a branch.

popUp returns only once the window has closed, as the popup's PopupMenu does, so gui._isNvdaMenuOpen
holds while the menu is open; and the chosen item's event reaches the tray icon before popUp returns and
after the window has gone, where wxGTK delivers a popup's: from the hide of the menu inside PopupMenu's
loop. So NVDA's handlers find gui.mainFrame.prevFocus and the open menu as they do on X.
"""

import buildVersion
import controlTypes
import wx

#: The window's least size in pixels, enough for the longest of NVDA's own items.
_MIN_SIZE = (320, 360)
#: The keys that choose the selected item, or open it when it holds a submenu.
_CHOOSING_KEYS = (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER, wx.WXK_SPACE)


def _mnemonic(item: wx.MenuItem) -> str:
	"""The lowercased letter that follows the first lone & in item's label, or "" when it has none."""
	label = item.GetItemLabel().split("\t", 1)[0]
	index = 0
	while (index := label.find("&", index)) != -1 and index + 1 < len(label):
		if label[index + 1] != "&":
			return label[index + 1].lower()
		index += 2
	return ""


def _text(item: wx.MenuItem) -> str:
	"""What the tree says for item: its label, with the state a menu would say beside it."""
	parts = [item.GetItemLabelText()]
	if item.IsCheckable():
		checked = controlTypes.State.CHECKED
		parts.append(checked.displayString if item.IsChecked() else checked.negativeDisplayString)
	if not item.IsEnabled():
		parts.append(controlTypes.State.UNAVAILABLE.displayString)
	return " ".join(parts)


class MenuWindow(wx.Dialog):
	"""A menu shown as a tree in a modal dialog of its own.

	Up and Down move, Right, Enter or Space opens a submenu, Left or Escape in a submenu goes back to the
	item that opened it, and a letter chooses the item it is the mnemonic of. Enter or Space on any other
	item ends the dialog with it as chosen; Escape at the top ends it with nothing chosen.
	"""

	#: Profile triggers wait while it is open, as they do while NVDA's popup menu is.
	shouldSuspendConfigProfileTriggers = True

	def __init__(self, menu: wx.Menu, title: str) -> None:
		super().__init__(None, title=title, style=wx.DEFAULT_DIALOG_STYLE | wx.DIALOG_NO_PARENT)
		#: The item that ended the dialog, which the caller delivers once the dialog has gone.
		self.chosen: wx.MenuItem | None = None
		self.tree = wx.TreeCtrl(
			self,
			style=wx.TR_HIDE_ROOT | wx.TR_HAS_BUTTONS | wx.TR_LINES_AT_ROOT | wx.TR_SINGLE,
		)
		self.tree.SetMinSize(_MIN_SIZE)
		self._addItems(self.tree.AddRoot(title), menu)
		sizer = wx.BoxSizer(wx.VERTICAL)
		sizer.Add(self.tree, proportion=1, flag=wx.EXPAND)
		self.SetSizerAndFit(sizer)
		self.Bind(wx.EVT_CHAR_HOOK, self._onCharHook)
		# Opening or leaving a submenu also selects an item, and that selection alone is presented: kept
		# here, an expansion does not reach ownGui's presentation, which would say the item twice.
		for binder in (wx.EVT_TREE_ITEM_EXPANDED, wx.EVT_TREE_ITEM_COLLAPSED):
			self.tree.Bind(binder, lambda evt: None)
		first, _cookie = self.tree.GetFirstChild(self.tree.GetRootItem())
		if first.IsOk():
			self.tree.SelectItem(first)
		self.tree.SetFocus()

	def _addItems(self, parent: wx.TreeItemId, menu: wx.Menu) -> None:
		for item in menu.GetMenuItems():
			if item.IsSeparator():
				continue
			node = self.tree.AppendItem(parent, _text(item), data=item)
			if item.IsSubMenu():
				self._addItems(node, item.GetSubMenu())

	def _selected(self) -> wx.TreeItemId | None:
		selection = self.tree.GetSelection()
		if not selection.IsOk() or selection == self.tree.GetRootItem():
			return None
		return selection

	def _parentOf(self, node: wx.TreeItemId) -> wx.TreeItemId | None:
		parent = self.tree.GetItemParent(node)
		return None if not parent.IsOk() or parent == self.tree.GetRootItem() else parent

	def _siblings(self, node: wx.TreeItemId) -> list[wx.TreeItemId]:
		siblings = []
		child, cookie = self.tree.GetFirstChild(self.tree.GetItemParent(node))
		while child.IsOk():
			siblings.append(child)
			child, cookie = self.tree.GetNextChild(self.tree.GetItemParent(node), cookie)
		return siblings

	def _open(self, node: wx.TreeItemId) -> None:
		first, _cookie = self.tree.GetFirstChild(node)
		if first.IsOk():
			self.tree.Expand(node)
			self.tree.SelectItem(first)

	def _back(self, node: wx.TreeItemId) -> bool:
		parent = self._parentOf(node)
		if parent is None:
			return False
		self.tree.Collapse(parent)
		self.tree.SelectItem(parent)
		return True

	def _choose(self, node: wx.TreeItemId) -> None:
		if self.tree.ItemHasChildren(node):
			self._open(node)
			return
		item = self.tree.GetItemData(node)
		if not item.IsEnabled():
			wx.Bell()
			return
		self.chosen = item
		self.EndModal(wx.ID_OK)

	def _chooseByMnemonic(self, node: wx.TreeItemId, letter: str) -> bool:
		siblings = self._siblings(node)
		matching = [sibling for sibling in siblings if _mnemonic(self.tree.GetItemData(sibling)) == letter]
		if not matching:
			return False
		if len(matching) == 1:
			self.tree.SelectItem(matching[0])
			self._choose(matching[0])
			return True
		# Shared by several items, the letter moves among them, as it does in a menu.
		later = [sibling for sibling in matching if siblings.index(sibling) > siblings.index(node)]
		self.tree.SelectItem((later or matching)[0])
		return True

	def _onCharHook(self, evt: wx.KeyEvent) -> None:
		node = self._selected()
		key = evt.GetKeyCode()
		modifiers = evt.GetModifiers()
		if node is None or modifiers not in (wx.MOD_NONE, wx.MOD_SHIFT):
			evt.Skip()
			return
		if key == wx.WXK_ESCAPE:
			# A dialog with neither a Cancel nor an OK button ignores Escape.
			if not self._back(node):
				self.EndModal(wx.ID_CANCEL)
		elif modifiers != wx.MOD_NONE:
			evt.Skip()
		elif key in _CHOOSING_KEYS:
			self._choose(node)
		elif key in (wx.WXK_RIGHT, wx.WXK_NUMPAD_RIGHT):
			if self.tree.ItemHasChildren(node):
				self._open(node)
		elif key in (wx.WXK_LEFT, wx.WXK_NUMPAD_LEFT):
			self._back(node)
		else:
			character = chr(evt.GetUnicodeKey()) if evt.GetUnicodeKey() != wx.WXK_NONE else ""
			if not (character.isalnum() and self._chooseByMnemonic(node, character.lower())):
				evt.Skip()


def _deliver(trayIcon: wx.EvtHandler, item: wx.MenuItem) -> None:
	"""Send item's menu event as wxGTK sends a popup item's: to its menu and the menus above it, then to
	the window the popup was shown for, which passes it to the tray icon."""
	if item.IsCheckable():
		item.Check(True if item.GetKind() == wx.ITEM_RADIO else not item.IsChecked())
	event = wx.CommandEvent(wx.wxEVT_MENU, item.GetId())
	event.SetInt(int(item.IsChecked()) if item.IsCheckable() else -1)
	menu = item.GetMenu()
	event.SetEventObject(menu)
	while menu is not None:
		if menu.ProcessEventLocally(event):
			return
		menu = menu.GetParent()
	trayIcon.ProcessEvent(event)


def popUp(trayIcon: wx.EvtHandler, menu: wx.Menu) -> bool:
	"""Show menu as a MenuWindow, and once it has closed, send the event of the item chosen in it."""
	window = MenuWindow(menu, buildVersion.name)
	try:
		window.ShowModal()
		chosen = window.chosen
	finally:
		window.Destroy()
	if chosen is not None:
		_deliver(trayIcon, chosen)
	return True
