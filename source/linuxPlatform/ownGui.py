"""NVDA's own GUI, written against wxMSW, made to behave on wxGTK.

install() replaces what NVDA's dialogs do for MSAA. On Windows NVDA patches what its wx controls
tell MSAA: a wx.Accessible for its check lists and settings panels, and WinEvents it sends itself
when a check box or slider changes. wxGTK has no wx.Accessible, whose constructor raises
NotImplementedError there, and no WinEvents, so the settings dialog could not open and a check list
could not be toggled. GTK's own accessibility already covers what those repairs add on Windows: a
check list is a table whose cells carry their checked state, and GTK reports the state and value
changes it makes itself.

It names controls by their labels. Windows names a control after the static text before it; wxGTK
guesses the same by making a label the mnemonic widget of the next control made after it, which
picks the wrong control when that one is a slider, whose widget is a box around the scale, and
which lands on the box around a list or a multi-line field rather than the list or field itself.
Each label NVDA pairs with a control is made that control's label, and a label wx paired with a box
is moved to the control inside it. Mnemonics follow, so Alt with a label's letter reaches the
control as it does on Windows.

It gives a dialog the focus NVDA gave it while building it. NVDA puts the focus on a control before
the dialog is shown, which wxMSW keeps and GTK does not: GTK focuses the default button as it shows
the dialog. The focus is given again once the dialog is first active.

Enter in a dialog presses its default button from a choice, a check box, a radio button or a
slider, as Windows' dialog manager does; GTK opens the choice's list or toggles the box instead.

selectFirstMenuItemWhenShown makes a popup menu open on its first item. wx pops a menu up as GTK
does for the pointer, with nothing selected, so that the focus lands on the menu's unnamed window
and the first arrow key is spent selecting an item. A menu opened from the keyboard should open on
its first item, as Windows opens NVDA's menu and as GTK opens a context menu from the Menu key.
Escape in a submenu closes that submenu only, as on Windows; GTK closes every menu.

Each of NVDA's dialogs is made the active window as it is shown, as NVDA brings its dialogs to the
foreground on Windows. marco gives a new window the keyboard only when its user time is later than
that of the window that has it, and GTK stamps a window with the time of the last key NVDA's own
windows received. Once one of NVDA's dialogs had taken keys, every later one was stamped with that
time, older than the keys typed in the browser since, and was left behind it.

wxGTK draws its tree control itself and gives GTK only an empty panel, so the Input Gestures dialog
was silent: a tree's moves, expansions and collapses are passed from wx to NVDA's focus, which the
NVDA app module makes the tree's selected item. A popup menu NVDA opens from its dialogs opens on
its first item too, and the menu of a captured gesture's forms waits for the NVDA keys to be let go.

wx's own log goes to NVDA's log, where wxMSW's goes to the debugger rather than to the user;
wxGTK would print it on standard error.
"""

import atexit
import ctypes
import gc
import importlib.util
import time
import weakref
from collections.abc import Callable

import controlTypes
import wx
from gui import guiHelper, nvdaControls, settingsDialogs
from logHandler import log

#: Milliseconds between looks for a menu being shown, and how many looks it gets to appear.
_MENU_INTERVAL = 20
_MENU_ATTEMPTS = 25
#: Milliseconds between looks for the NVDA keys being released, and how many looks to take.
_RELEASE_INTERVAL = 20
_RELEASE_ATTEMPTS = 150
#: Seconds from a dialog becoming active to the focus NVDA chose for it, at most.
_FIRST_FOCUS_TIMEOUT = 1.0

#: Accessible roles of the boxes wx puts around a control's GTK widget, which a label should skip.
_WRAPPER_ROLES = frozenset({"filler", "scroll pane", "panel"})
#: Accessible roles inside such a box that are not the control.
_NOT_THE_CONTROL_ROLES = frozenset({"scroll bar", "label", "separator"})

#: Controls from which Enter presses the dialog's default button, as on Windows.
_ENTER_PRESSES_DEFAULT = (wx.Choice, wx.CheckBox, wx.RadioButton, wx.Slider)


class _GList(ctypes.Structure):
	pass


_GList._fields_ = [
	("data", ctypes.c_void_p),
	("next", ctypes.POINTER(_GList)),
	("prev", ctypes.POINTER(_GList)),
]


class _Gtk:
	"""The few GTK and ATK calls wx gives no way to make, on the libraries wx has already loaded."""

	def __init__(self) -> None:
		gtk = ctypes.CDLL("libgtk-3.so.0")
		atk = ctypes.CDLL("libatk-1.0.so.0")
		gobject = ctypes.CDLL("libgobject-2.0.so.0")
		glib = ctypes.CDLL("libglib-2.0.so.0")
		pointer = ctypes.c_void_p
		self._functions = {}
		for library, name, result, arguments in (
			(gtk, "gtk_label_get_mnemonic_widget", pointer, (pointer,)),
			(gtk, "gtk_label_set_mnemonic_widget", None, (pointer, pointer)),
			(gtk, "gtk_label_get_type", ctypes.c_size_t, ()),
			(gtk, "gtk_container_get_type", ctypes.c_size_t, ()),
			(gtk, "gtk_container_get_children", ctypes.POINTER(_GList), (pointer,)),
			(gtk, "gtk_widget_get_accessible", pointer, (pointer,)),
			(atk, "atk_object_get_role", ctypes.c_int, (pointer,)),
			(atk, "atk_role_get_name", ctypes.c_char_p, (ctypes.c_int,)),
			(gtk, "gtk_window_get_focus", pointer, (pointer,)),
			(gtk, "gtk_widget_is_ancestor", ctypes.c_int, (pointer, pointer)),
			(atk, "atk_object_set_name", None, (pointer, ctypes.c_char_p)),
			(atk, "atk_object_add_relationship", ctypes.c_int, (pointer, ctypes.c_int, pointer)),
			(atk, "atk_relation_type_for_name", ctypes.c_int, (ctypes.c_char_p,)),
			(gobject, "g_type_check_instance_is_a", ctypes.c_int, (pointer, ctypes.c_size_t)),
			(glib, "g_list_free", None, (ctypes.POINTER(_GList),)),
		):
			function = getattr(library, name)
			function.restype = result
			function.argtypes = arguments
			self._functions[name] = function
		self._labelType = self._functions["gtk_label_get_type"]()
		self._containerType = self._functions["gtk_container_get_type"]()
		self._labelledBy = self._functions["atk_relation_type_for_name"](b"labelled-by")
		self._labelFor = self._functions["atk_relation_type_for_name"](b"label-for")

	def isLabel(self, widget: int) -> bool:
		return bool(self._functions["g_type_check_instance_is_a"](widget, self._labelType))

	def role(self, widget: int) -> str:
		accessible = self._functions["gtk_widget_get_accessible"](widget)
		if not accessible:
			return ""
		role = self._functions["atk_object_get_role"](accessible)
		return (self._functions["atk_role_get_name"](role) or b"").decode()

	def children(self, widget: int) -> list[int]:
		if not self._functions["g_type_check_instance_is_a"](widget, self._containerType):
			return []
		head = self._functions["gtk_container_get_children"](widget)
		children = []
		node = head
		while node:
			children.append(node.contents.data)
			node = node.contents.next
		if head:
			self._functions["g_list_free"](head)
		return children

	def control(self, widget: int) -> int:
		"""The widget whose accessible is the control, inside the boxes wx may have put it in."""
		for _depth in range(4):
			if self.role(widget) not in _WRAPPER_ROLES:
				break
			inner = [
				child for child in self.children(widget) if self.role(child) not in _NOT_THE_CONTROL_ROLES
			]
			if not inner:
				break
			widget = inner[0]
		return widget

	def mnemonicWidget(self, label: int) -> int | None:
		return self._functions["gtk_label_get_mnemonic_widget"](label)

	def focus(self, window: int) -> int | None:
		"""The widget that has the focus in a top-level GTK window."""
		return self._functions["gtk_window_get_focus"](window)

	def contains(self, widget: int, descendant: int) -> bool:
		return descendant == widget or bool(self._functions["gtk_widget_is_ancestor"](descendant, widget))

	def setMnemonicWidget(self, label: int, widget: int) -> None:
		self._functions["gtk_label_set_mnemonic_widget"](label, widget)

	def setLabel(self, label: int, widget: int) -> None:
		"""Give widget's accessible label as its label, which GTK keeps, before any it would work out
		from the mnemonic labels wx sets and sets again."""
		labelAccessible = self._functions["gtk_widget_get_accessible"](label)
		accessible = self._functions["gtk_widget_get_accessible"](widget)
		if labelAccessible and accessible:
			self._functions["atk_object_add_relationship"](accessible, self._labelledBy, labelAccessible)
			self._functions["atk_object_add_relationship"](labelAccessible, self._labelFor, accessible)

	def setAccessibleName(self, widget: int, name: str) -> None:
		accessible = self._functions["gtk_widget_get_accessible"](widget)
		if accessible:
			self._functions["atk_object_set_name"](accessible, name.encode())


_gtk: _Gtk | None = None


def _gtkCalls() -> _Gtk:
	global _gtk
	if _gtk is None:
		_gtk = _Gtk()
	return _gtk


class _NoAccessible:
	"""Takes the place of a wx.Accessible subclass, which wxGTK cannot construct."""

	def __init__(self, window: wx.Window | None = None) -> None:
		self.Window = window


def _setNoAccessible(window: wx.Window, accessible: object) -> None:
	pass


def _announceCheckedItem() -> None:
	"""Have the focus, a check list's item, say its new state, as NVDA's own WinEvent makes it say on
	Windows. wx toggles the item in its own model, of which GTK tells no one."""
	import api
	import eventHandler

	eventHandler.queueEvent("stateChange", api.getFocusObject())


def _notifyCheckListBox(self, evt: wx.CommandEvent) -> None:
	evt.Skip()
	wx.CallAfter(_announceCheckedItem)


def _notifyCheckListCtrl(self, index: int) -> None:
	wx.CallAfter(_announceCheckedItem)


def _setSliderValue(self, i: int) -> None:
	wx.Slider.SetValue(self, i)
	evt = wx.CommandEvent(wx.wxEVT_COMMAND_SLIDER_UPDATED, self.GetId())
	evt.SetInt(i)
	self.ProcessEvent(evt)


def _isGone(window: wx.Window) -> bool:
	return not window or window.IsBeingDeleted()


def _widget(window: wx.Window) -> int:
	"""The address of a window's GTK widget; GetHandle gives its X window."""
	return int(window.GetGtkWidget())


def _labelControl(label: wx.StaticText, control: wx.Window) -> None:
	# Kept, because wx pairs labels again whenever a control is moved in the tab order, as the driver
	# settings are, and the mnemonic has to be set again after it.
	label._basedreaderControl = weakref.ref(control)
	try:
		gtk = _gtkCalls()
		if gtk.isLabel(_widget(label)):
			inner = gtk.control(_widget(control))
			gtk.setLabel(_widget(label), inner)
			gtk.setMnemonicWidget(_widget(label), inner)
	except (OSError, AttributeError):
		log.debugWarning("Could not label a control through GTK", exc_info=True)


def _repairLabels(window: wx.Window) -> None:
	"""Set the mnemonic of each label NVDA paired with a control to that control again, label the
	control inside each box wx paired a label with, and name a read-only field that stands alone in a
	group box after the box, as Windows names it."""
	try:
		gtk = _gtkCalls()
		pending = list(window.GetChildren())
		while pending:
			child = pending.pop()
			pending.extend(child.GetChildren())
			if isinstance(child, wx.StaticText):
				label = _widget(child)
				if not gtk.isLabel(label):
					continue
				control = getattr(child, "_basedreaderControl", lambda: None)()
				if control is not None and not _isGone(control):
					gtk.setMnemonicWidget(label, gtk.control(_widget(control)))
					continue
				paired = gtk.mnemonicWidget(label)
				if paired:
					inner = gtk.control(paired)
					if inner != paired:
						# The box keeps the label wx gave it, so that a dialog's text still leaves the
						# label out as the label of the object after it.
						gtk.setLabel(label, inner)
			elif (
				isinstance(child, wx.TextCtrl)
				and child.HasFlag(wx.TE_READONLY)
				and isinstance(child.GetParent(), wx.StaticBox)
			):
				gtk.setAccessibleName(gtk.control(_widget(child)), child.GetParent().GetLabelText())
	except (OSError, AttributeError):
		log.debugWarning("Could not repair labels through GTK", exc_info=True)


def _associateElements(firstElement, secondElement) -> wx.BoxSizer:
	sizer = _upstreamAssociateElements(firstElement, secondElement)
	if isinstance(firstElement, wx.StaticText) and isinstance(secondElement, wx.Window):
		_labelControl(firstElement, secondElement)
	return sizer


def _buildSettingsPanel(self) -> None:
	_upstreamBuildSettingsPanel(self)
	_repairLabels(self)


def _repairingLabels(method):
	"""method, followed by _repairLabels on the panel it was called on."""

	def repairing(self, *args, **kwargs):
		result = method(self, *args, **kwargs)
		_repairLabels(self)
		# wx pairs the labels of a panel again as GTK first shows it, which happens after this returns.
		wx.CallAfter(lambda: None if _isGone(self) else _repairLabels(self))
		return result

	return repairing


#: The label of the default button of the dialog whose first focus is still to come, and the time
#: by which it is no longer awaited.
_awaitedFirstFocus: tuple[str, float] | None = None


def _awaitFirstFocus(dialog: wx.TopLevelWindow) -> None:
	global _awaitedFirstFocus
	if not isinstance(dialog, wx.Dialog):
		return
	button = dialog.GetDefaultItem() or dialog.FindWindow(dialog.GetAffirmativeId())
	if isinstance(button, wx.Button):
		_awaitedFirstFocus = (button.GetLabelText(), time.monotonic() + _FIRST_FOCUS_TIMEOUT)


def isPassingFocus(obj) -> bool:
	"""Whether obj is the default button of a dialog of NVDA's that GTK focused as it showed it, before
	the control NVDA chose takes the focus, so that the focus there should not be reported.

	Any other focused object ends the wait, as does the time it may take.
	"""
	global _awaitedFirstFocus
	if _awaitedFirstFocus is None:
		return False
	label, deadline = _awaitedFirstFocus
	if time.monotonic() >= deadline:
		_awaitedFirstFocus = None
		return False
	if obj.role == controlTypes.Role.BUTTON and obj.name == label:
		return True
	if controlTypes.State.FOCUSED in obj.states:
		_awaitedFirstFocus = None
	return False


def _focusAndItsParent() -> tuple[wx.Window | None, wx.Window | None]:
	# The active window first; a dialog just shown may not be active yet as NVDA reads it.
	topLevels = sorted(
		(window for window in wx.GetTopLevelWindows() if window.IsShown()),
		key=lambda window: not window.IsActive(),
	)
	focus = _focusedWindow(topLevels)
	return focus, focus.GetParent() if focus else None


def focusedListItemText() -> str:
	"""The text of the focused item of the wx list control that has the focus, if one has it."""
	for window in _focusAndItsParent():
		if isinstance(window, wx.ListCtrl):
			index = window.GetFocusedItem()
			if index < 0:
				index = window.GetFirstSelected()
			return window.GetItemText(index) if index >= 0 else ""
	return ""


def focusedTree() -> wx.TreeCtrl | None:
	"""The wx tree control that has the focus, if one has it."""
	focus, _parent = _focusAndItsParent()
	return focus if isinstance(focus, wx.TreeCtrl) else None


def _onTreeEvent(evt: wx.TreeEvent) -> None:
	evt.Skip()
	tree = evt.GetEventObject()
	if not isinstance(tree, wx.TreeCtrl):
		return
	isStateChange = evt.GetEventType() in (wx.wxEVT_TREE_ITEM_EXPANDED, wx.wxEVT_TREE_ITEM_COLLAPSED)
	# After the tree has finished with the event, so that the item it reports is the new one.
	wx.CallAfter(_presentTreeChange, weakref.ref(tree), isStateChange)


def hasSelection(tree: wx.TreeCtrl) -> bool:
	"""Whether an item of tree is selected; wx's generic tree may report its hidden root instead of none."""
	selection = tree.GetSelection()
	return selection.IsOk() and not (tree.HasFlag(wx.TR_HIDE_ROOT) and selection == tree.GetRootItem())


def _selectedGesturesData(self):
	"""_GesturesTree.getSelectedItemData, which on GTK must not be asked about no selection.

	wxMSW raises AssertionError for the index of the item no selection gives, which upstream catches;
	wx's generic tree gives the empty index of its hidden root, on which upstream asserts, so that the key
	handler that asked raised and every key in the tree was lost until something was selected.
	"""
	if not hasSelection(self):
		return None
	return _upstreamSelectedGesturesData(self)


def _postInitInputGestures(self) -> None:
	_upstreamPostInitInputGestures(self)
	# A Windows tree view puts its focus on the first item when it has none selected; wx's generic tree
	# leaves it nowhere, which a screen reader cannot present.
	tree = self.tree
	if not hasSelection(tree):
		first, _cookie = tree.GetFirstChild(tree.GetRootItem())
		if first.IsOk():
			tree.SelectItem(first)


def _doRefreshAndPresent(self, *args, **kwargs) -> None:
	_upstreamDoRefresh(self, *args, **kwargs)
	# A gesture removed or added changes the tree under its selection, which wx does not report.
	wx.CallAfter(_presentTreeChange, weakref.ref(self), False)


def _addCapturedOnceKeysReleased(self, *args) -> None:
	# The gesture captured may have been an NVDA key chord, and the menu of its forms cannot open while
	# that key is held.
	whenNvdaKeysReleased(lambda: None if _isGone(self) else _upstreamAddCaptured(self, *args))


def _popupMenuOnFirstItem(self, menu, *args, **kwargs):
	selectFirstMenuItemWhenShown()
	return _upstreamPopupMenu(self, menu, *args, **kwargs)


def _presentTreeChange(treeRef: weakref.ref, isStateChange: bool) -> None:
	"""Have NVDA's focus, when it is an item of the tree, present the tree's change.

	wxGTK draws its generic tree control itself and tells GTK nothing about its items, so NVDA hears of
	a move, an expansion or a collapse only from wx.
	"""
	import api

	tree = treeRef()
	if tree is None or _isGone(tree):
		return
	present = getattr(api.getFocusObject(), "presentWxTreeChange", None)
	if present is not None:
		present(tree, isStateChange)


def focusIsCheckList() -> bool:
	"""Whether one of NVDA's check lists has the focus."""
	return any(
		isinstance(window, (wx.CheckListBox, nvdaControls.AutoWidthColumnCheckListCtrl))
		for window in _focusAndItsParent()
	)


def _setFocusOnceActive(self) -> None:
	_upstreamSetFocus(self)
	topLevel = wx.GetTopLevelParent(self)
	if topLevel is None or topLevel is self or topLevel.IsShown():
		return
	target = weakref.ref(self)
	if getattr(topLevel, "_basedreaderFocusTarget", None) is None:

		def focusOnceActive(evt: wx.ActivateEvent) -> None:
			evt.Skip()
			if not evt.GetActive():
				return
			topLevel.Unbind(wx.EVT_ACTIVATE, handler=focusOnceActive)
			target = topLevel._basedreaderFocusTarget
			topLevel._basedreaderFocusTarget = None
			window = target() if target else None
			if window is None:
				return
			_awaitFirstFocus(topLevel)
			# After wx has given the focus to whatever it last saw focused, as it does on activation.
			wx.CallAfter(lambda: None if _isGone(window) or _isGone(topLevel) else _upstreamSetFocus(window))

		topLevel.Bind(wx.EVT_ACTIVATE, focusOnceActive)
	topLevel._basedreaderFocusTarget = target


def _initSettingsDialog(self, *args, **kwargs) -> None:
	postInit = type(self).postInit

	def postInitOnceActive(evt: wx.ActivateEvent) -> None:
		evt.Skip()
		if not evt.GetActive():
			return
		self.Unbind(wx.EVT_ACTIVATE, handler=postInitOnceActive)
		# wx pairs labels again as GTK first shows the dialog.
		_repairLabels(self)
		_awaitFirstFocus(self)
		# After wx has given the focus back to whatever it last saw focused, as it does on activation.
		wx.CallAfter(lambda: None if _isGone(self) else postInit(self))

	def postInitOnceShown() -> None:
		# Later calls, after Apply, focus a dialog already shown and need no deferring.
		del self.postInit
		self.Bind(wx.EVT_ACTIVATE, postInitOnceActive)

	self.postInit = postInitOnceShown
	_upstreamSettingsDialogInit(self, *args, **kwargs)
	# postInit chooses the focus, over any control focused while the dialog was being built.
	self._basedreaderFocusTarget = None
	_repairLabels(self)


def _newSettingsDialog(cls, *args, **kwargs):
	"""A new settings dialog, made after collecting garbage when one destroyed is still alive.

	A destroyed dialog lives on in reference cycles until the collector next runs, and while it lives
	upstream refuses to open another, so that a settings dialog opened soon after one was closed did
	not open at all.
	"""
	if any(
		isinstance(dialog, cls) and state is settingsDialogs.SettingsDialog.DialogState.DESTROYED
		for dialog, state in list(settingsDialogs.SettingsDialog._instances.items())
	):
		gc.collect()
	return _upstreamSettingsDialogNew(cls, *args, **kwargs)


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


def _focusedWindow(topLevels: list[wx.Window]) -> wx.Window | None:
	"""The window that has the focus, as GTK says when wx has lost track of it, as it does once a
	choice's list has been open: the window of the first of topLevels that holds GTK's focus."""
	focus = wx.Window.FindFocus()
	# After NVDA's menu wx may still name the hidden window it popped the menu up from, so that Enter
	# on the check box a dialog opened from the menu focuses toggled it instead of pressing OK.
	topLevel = wx.GetTopLevelParent(focus) if focus is not None else None
	if (
		topLevel is not None
		and topLevel.IsShown()
		and (not topLevels or any(topLevel is window for window in topLevels))
	):
		return focus
	try:
		gtk = _gtkCalls()
		for topLevel in topLevels:
			focused = gtk.focus(_widget(topLevel))
			if not focused:
				continue
			pending = list(topLevel.GetChildren())
			found = None
			while pending:
				child = pending.pop()
				if gtk.contains(_widget(child), focused):
					found = child
					pending = list(child.GetChildren())
			if found is not None:
				return found
	except (OSError, AttributeError):
		log.debugWarning("Could not find the focus through GTK", exc_info=True)
	return None


def _onlyNotebook(window: wx.Window) -> wx.Notebook | None:
	"""The one notebook shown in window, if it has exactly one."""
	notebooks = []
	pending = list(window.GetChildren())
	while pending:
		child = pending.pop()
		pending.extend(child.GetChildren())
		if isinstance(child, wx.Notebook) and child.IsShown():
			notebooks.append(child)
	return notebooks[0] if len(notebooks) == 1 else None


def _switchTabs(evt: wx.KeyEvent) -> bool:
	"""Move to the next or previous tab of the dialog's notebook on Control+Tab or Control+Shift+Tab, as
	wxMSW does from anywhere in a dialog: GTK moves the focus out of the control instead, so that the
	add-on store's tabs could not be reached from its list. The tab chosen takes the focus, so that it
	is read."""
	if evt.GetKeyCode() != wx.WXK_TAB or not evt.ControlDown() or evt.AltDown():
		return False
	source = evt.GetEventObject()
	dialog = wx.GetTopLevelParent(source) if isinstance(source, wx.Window) else None
	notebook = _onlyNotebook(dialog) if isinstance(dialog, wx.Dialog) else None
	if notebook is None or notebook.GetPageCount() < 2:
		return False
	notebook.AdvanceSelection(not evt.ShiftDown())
	notebook.SetFocus()
	return True


def _onCharHook(evt: wx.KeyEvent) -> None:
	if _switchTabs(evt):
		return
	if evt.GetKeyCode() in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER) and not evt.HasAnyModifiers():
		source = evt.GetEventObject()
		focus = _focusedWindow([wx.GetTopLevelParent(source)] if isinstance(source, wx.Window) else [])
		dialog = wx.GetTopLevelParent(focus) if focus else None
		if isinstance(focus, _ENTER_PRESSES_DEFAULT) and isinstance(dialog, wx.Dialog):
			button = dialog.GetDefaultItem() or dialog.FindWindow(dialog.GetAffirmativeId())
			if isinstance(button, wx.Button) and button.IsShown() and button.IsEnabled():
				click = wx.CommandEvent(wx.wxEVT_BUTTON, button.GetId())
				click.SetEventObject(button)
				button.GetEventHandler().ProcessEvent(click)
				return
	evt.Skip()


def _disableRememberingFocus(self) -> bool:
	if isinstance(self, wx.TopLevelWindow):
		focus = wx.Window.FindFocus()
		if focus is not None and wx.GetTopLevelParent(focus) is self:
			self._basedreaderFocusBeforeDisable = weakref.ref(focus)
	return _upstreamDisable(self)


def _enableRestoringFocus(self, enable: bool = True) -> bool:
	"""wx.Window.Enable, after which a top-level window gives the focus back to the control that had it
	when the window was disabled.

	The profiles dialog disables itself while the dialog for a new profile is open. Windows gives the
	control that had the focus its focus back as the window is enabled and activated again; GTK takes the
	focus from a window as it is disabled and gives it to nothing again, so that the profiles dialog was
	left with no focus, in which even Escape did nothing.
	"""
	if not enable:
		return _disableRememberingFocus(self)
	result = _upstreamEnable(self, enable)
	remembered = getattr(self, "_basedreaderFocusBeforeDisable", None)
	if isinstance(self, wx.TopLevelWindow) and remembered is not None:
		self._basedreaderFocusBeforeDisable = None
		wx.CallAfter(_restoreFocus, weakref.ref(self), remembered)
	return result


def _restoreFocus(topLevelRef: weakref.ref, focusRef: weakref.ref) -> None:
	topLevel = topLevelRef()
	focus = focusRef()
	if topLevel is None or _isGone(topLevel) or not topLevel.IsShown() or not topLevel.IsEnabled():
		return
	try:
		if _gtkCalls().focus(_widget(topLevel)):
			return
	except (OSError, AttributeError):
		log.debugWarning("Could not find the focus through GTK", exc_info=True)
		return
	if focus is not None and not _isGone(focus) and focus.IsShownOnScreen() and focus.IsEnabled():
		_upstreamSetFocus(focus)


def _addPageOnce(self, page: wx.Window, text: str, *args, **kwargs) -> bool:
	"""wx.Notebook.AddPage, with an empty page of its own in place of a window that is already a page.

	The add-on store gives one window as the page of each of its tabs, which wxMSW allows. A GTK widget
	has one parent, and the notebook's other pages were left holding a widget taken from them, so that
	closing the store, in whatever way, crashed NVDA.
	"""
	if any(self.GetPage(index) is page for index in range(self.GetPageCount())):
		page = wx.Panel(self)
	return _upstreamAddPage(self, page, text, *args, **kwargs)


class _NvdaLogTarget(wx.Log):
	def DoLogTextAtLevel(self, level: int, msg: str) -> None:
		if level <= wx.LOG_Error:
			log.error(f"wx: {msg}")
		elif level == wx.LOG_Warning:
			log.warning(f"wx: {msg}")
		elif level <= wx.LOG_Info:
			log.info(f"wx: {msg}")
		else:
			log.debug(f"wx: {msg}")


_logTarget: _NvdaLogTarget | None = None


def _quietAutoSettingsAtExit() -> None:
	"""Let the driver settings that live until the interpreter is torn down go quietly.

	NVDAHighlighter keeps its settings in a class attribute and globalVars.settingsRing keeps the last
	synth, so AutoSettings.__del__ runs only as the interpreter takes config apart, when there is no
	extension point left to leave, and its AttributeError was printed on standard error.

	The class is changed rather than each live instance: finding those takes an isinstance check
	against an abstract base class for every object in the process, which slows the exit enough that
	the copy a restart starts finds this one still there and its SIGTERM ends it.
	"""
	from autoSettingsUtils.autoSettings import AutoSettings

	AutoSettings._unregisterConfigSaveAction = _leaveNothing


def _leaveNothing(self) -> None:
	pass


_upstreamSettingsDialogInit = settingsDialogs.SettingsDialog.__init__
_upstreamSettingsDialogNew = settingsDialogs.SettingsDialog.__new__
_upstreamBuildSettingsPanel = settingsDialogs.SettingsPanel._buildGui
_upstreamAssociateElements = guiHelper.associateElements
_upstreamSetFocus = wx.Window.SetFocus


_upstreamSelectedGesturesData = None
_upstreamPostInitInputGestures = None
_upstreamAddCaptured = None
_upstreamDoRefresh = None
_upstreamPopupMenu = wx.Window.PopupMenu
_upstreamAddPage = wx.Notebook.AddPage
_upstreamEnable = wx.Window.Enable
_upstreamDisable = wx.Window.Disable


def install() -> None:
	"""Make the changes, once gui has imported the modules they are made to."""
	global _upstreamSelectedGesturesData, _upstreamPostInitInputGestures, _upstreamAddCaptured
	global _upstreamDoRefresh
	from gui import inputGestures

	_upstreamSelectedGesturesData = inputGestures._GesturesTree.getSelectedItemData
	_upstreamDoRefresh = inputGestures._GesturesTree.doRefresh
	inputGestures._GesturesTree.doRefresh = _doRefreshAndPresent
	_upstreamPostInitInputGestures = inputGestures.InputGesturesDialog.postInit
	_upstreamAddCaptured = inputGestures.InputGesturesDialog._addCaptured
	inputGestures._GesturesTree.getSelectedItemData = _selectedGesturesData
	inputGestures.InputGesturesDialog.postInit = _postInitInputGestures
	inputGestures.InputGesturesDialog._addCaptured = _addCapturedOnceKeysReleased
	wx.Window.PopupMenu = _popupMenuOnFirstItem
	wx.Notebook.AddPage = _addPageOnce
	wx.Window.Enable = _enableRestoringFocus
	wx.Window.Disable = _disableRememberingFocus
	wx.Window.SetAccessible = _setNoAccessible
	wx.Window.SetFocus = _setFocusOnceActive
	nvdaControls.ListCtrlAccessible = _NoAccessible
	settingsDialogs.SettingsPanelAccessible = _NoAccessible
	nvdaControls.CustomCheckListBox.notifyIAccessible = _notifyCheckListBox
	nvdaControls.AutoWidthColumnCheckListCtrl.notifyIAccessible = _notifyCheckListCtrl
	nvdaControls.EnhancedInputSlider.SetValue = _setSliderValue
	settingsDialogs.SettingsDialog.__init__ = _initSettingsDialog
	settingsDialogs.SettingsDialog.__new__ = staticmethod(_newSettingsDialog)
	settingsDialogs.SettingsPanel._buildGui = _buildSettingsPanel
	for cls, name in (
		(settingsDialogs.SettingsPanel, "onPanelActivated"),
		(settingsDialogs.AutoSettingsMixin, "updateDriverSettings"),
		(settingsDialogs.AutoSettingsMixin, "refreshGui"),
	):
		setattr(cls, name, _repairingLabels(getattr(cls, name)))
	guiHelper.associateElements = _associateElements
	for cls in (settingsDialogs.MultiCategorySettingsDialog, settingsDialogs.NVDASettingsDialog):
		cls.onCategoryChange = _ignoredOnceDestroyed(cls.onCategoryChange)
	categories = settingsDialogs.NVDASettingsDialog.categoryClasses
	if settingsDialogs.MathSettingsPanel in categories and importlib.util.find_spec("libmathcat_py") is None:
		# MathCAT's library is built for Windows only, and its panel cannot be made without it: choosing the
		# category raised and left the previous panel's controls in the tab order.
		categories.remove(settingsDialogs.MathSettingsPanel)
	atexit.register(_quietAutoSettingsAtExit)


def _presentDialogOnMap(window, *args) -> bool:
	Gtk = _gtkModule()
	from gi.repository import Gdk, GLib

	# The hook sees every widget mapped, as map is a signal of every widget.
	if isinstance(window, Gtk.Window) and window.get_window_type() == Gtk.WindowType.TOPLEVEL:
		_leaveOutOfModalGrab(window)
		if window.get_type_hint() == Gdk.WindowTypeHint.DIALOG:
			GLib.idle_add(_presentNow, window)
	return True


def _leaveOutOfModalGrab(window) -> None:
	"""Let a window that is not modal take keys while a modal dialog it is not part of is open.

	A modal dialog holds GTK's grab, which sends every key of every window in its window group to the
	dialog. wxMSW disables only the windows open as a modal dialog is shown, so that the Exit dialog
	NVDA+q opens over the add-on store's warning, or over the welcome dialog, takes the keys as it does on
	Windows. Here its keys went to the dialog under it, which changed that dialog's controls and pressed
	its OK, and NVDA could not be exited or restarted from it. A window group of its own is outside that
	grab.
	"""
	Gtk = _gtkModule()
	grab = Gtk.grab_get_current()
	if window.get_modal() or grab is None or grab.get_toplevel() is window:
		return
	Gtk.WindowGroup().add_window(window)


def _presentNow(window) -> bool:
	import gi

	gi.require_version("GdkX11", "3.0")
	from gi.repository import GdkX11

	gdkWindow = window.get_window()
	if gdkWindow is not None and window.get_visible() and isinstance(gdkWindow, GdkX11.X11Window):
		window.present_with_time(GdkX11.x11_get_server_time(gdkWindow))
	return False


def initialize(app: wx.App) -> None:
	"""Make the changes that need the application, once it exists."""
	global _logTarget
	from gi.repository import GObject

	app.Bind(wx.EVT_CHAR_HOOK, _onCharHook)
	GObject.add_emission_hook(_gtkModule().Window, "map", _presentDialogOnMap)
	# Tree events travel up to the application unless a handler keeps them, which none of NVDA's does.
	for binder in (wx.EVT_TREE_SEL_CHANGED, wx.EVT_TREE_ITEM_EXPANDED, wx.EVT_TREE_ITEM_COLLAPSED):
		app.Bind(binder, _onTreeEvent)
	# wx.App sets its own target as it starts.
	_logTarget = _NvdaLogTarget()
	wx.Log.SetActiveTarget(_logTarget)


def whenNvdaKeysReleased(callback: Callable[[], None], attemptsLeft: int = _RELEASE_ATTEMPTS) -> None:
	"""Call callback once no NVDA modifier key is held, or once waiting has taken too long.

	While an NVDA key is held, X keeps the keyboard grabbed for NVDA's key capture, and a menu, which
	needs a grab of its own, fails to open: NVDA+n would open nothing for a person who is still
	holding Insert as the command runs.
	"""
	import keyboardHandler

	held = any(
		keyboardHandler.isNVDAModifierKey(vkCode, extended)
		for vkCode, extended in list(keyboardHandler.currentModifiers)
	)
	if held and attemptsLeft > 1:
		wx.CallLater(_RELEASE_INTERVAL, whenNvdaKeysReleased, callback, attemptsLeft - 1)
		return
	callback()


def selectFirstMenuItemWhenShown() -> None:
	"""Select the first item of the popup menu about to be shown, once GTK has mapped it, and make
	Escape in its submenus close only them.

	Called just before a blocking PopupMenu; wx runs the calls queued here inside that menu's loop.
	"""
	wx.CallAfter(_selectFirstMenuItem, _MENU_ATTEMPTS)


def _gtkModule():
	# Imported only once wx is running GTK: importing it initialises GTK, which gui must not do as it
	# is imported, before wx.App exists.
	import gi

	gi.require_version("Gtk", "3.0")
	from gi.repository import Gtk

	return Gtk


def _popupMenus(*, shown: bool) -> list:
	Gtk = _gtkModule()
	return [
		window.get_child()
		for window in Gtk.Window.list_toplevels()
		if window.get_window_type() == Gtk.WindowType.POPUP
		and isinstance(window.get_child(), Gtk.Menu)
		and (window.get_mapped() or not shown)
	]


def _shownPopupMenus() -> list:
	return _popupMenus(shown=True)


#: The submenus whose Escape this module already handles. Holding them keeps their Python wrappers,
#: so that a submenu is found to be one of them however often it is looked up.
_submenusWithEscape: list = []


def _closeOnlyThisSubmenu(menu, event) -> bool:
	Gtk = _gtkModule()
	from gi.repository import Gdk

	# A submenu left with Left or Escape still gets the keys, with nothing selected in it; Escape then
	# closes the whole menu, as it does from the top of a menu on Windows.
	if event.keyval != Gdk.KEY_Escape or menu.get_selected_item() is None:
		return False
	menu.emit("move-current", Gtk.MenuDirectionType.PARENT)
	return True


def _handleEscapeInSubmenus() -> None:
	Gtk = _gtkModule()
	for menu in _popupMenus(shown=False):
		if any(menu is known for known in _submenusWithEscape) or not isinstance(
			menu.get_attach_widget(),
			Gtk.MenuItem,
		):
			continue
		_submenusWithEscape.append(menu)
		menu.connect("key-press-event", _closeOnlyThisSubmenu)


def _selectFirstMenuItem(attemptsLeft: int) -> None:
	menus = _shownPopupMenus()
	if menus:
		_handleEscapeInSubmenus()
		menu = menus[0]
		if menu.get_selected_item() is None:
			menu.select_first(True)
	elif attemptsLeft > 1:
		wx.CallLater(_MENU_INTERVAL, _selectFirstMenuItem, attemptsLeft - 1)
