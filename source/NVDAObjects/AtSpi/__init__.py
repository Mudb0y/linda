"""NVDAObjects for AT-SPI accessibles: the Linux counterpart of NVDAObjects.IAccessible and NVDAObjects.UIA.

Every fact comes from linuxPlatform.atspi's client through a Node; nothing here calls libatspi. The
tree is flattened as Windows' is: each application's top-level windows are children of the desktop,
and the per-process application object is never part of NVDA's hierarchy.
"""

import re

import api
from controlTypes import Role, State
from linuxPlatform.atspi import handler
from linuxPlatform.atspi.client import Node, NodeGone
from linuxPlatform.atspi.roles import ROLES, mapStates
from locationHelper import RectLTWH

from NVDAObjects import NVDAObject
from NVDAObjects.AtSpi.textInfo import AtspiTextInfo
from NVDAObjects.behaviors import (
	Dialog,
	EditableTextWithAutoSelectDetection,
	EditableTextWithoutAutoSelectDetection,
	Notification,
	ProgressBar,
	ToolTip,
)

#: Roles whose number comes from the Value interface.
VALUE_ROLES = frozenset({Role.SLIDER, Role.SPINBUTTON, Role.SCROLLBAR, Role.PROGRESSBAR, Role.DIAL})
#: Roles whose items NVDA counts as "n of m" among their siblings.
COUNTED_ROLES = frozenset(
	{
		Role.LISTITEM,
		Role.TREEVIEWITEM,
		Role.MENUITEM,
		Role.CHECKMENUITEM,
		Role.RADIOMENUITEM,
		Role.TAB,
	},
)
MENU_ROLES = frozenset({Role.MENUITEM, Role.CHECKMENUITEM, Role.RADIOMENUITEM, Role.MENUBAR, Role.POPUPMENU})
_keyName = re.compile(r"<(\w+)>")
_MODIFIERS = {"control": "Ctrl", "primary": "Ctrl", "alt": "Alt", "shift": "Shift", "super": "Windows"}


def _formatAccelerator(gtkAccel: str) -> str:
	"""GTK's "<Control><Shift>o" as NVDA spells a shortcut, "Ctrl+Shift+O"."""
	mods = [_MODIFIERS.get(m.lower(), m) for m in _keyName.findall(gtkAccel)]
	key = _keyName.sub("", gtkAccel)
	if len(key) == 1:
		key = key.upper()
	return "+".join([*mods, key]) if key else ""


class AtSpi(NVDAObject):
	"""An accessible reached through AT-SPI."""

	@classmethod
	def kwargsFromSuper(cls, kwargs, relation=None) -> bool:
		node = handler.nodeForRelation(relation)
		if node is None:
			return False
		kwargs["node"] = node
		return True

	def __init__(self, node: Node):
		self.node = node
		super().__init__()
		handler.registerObject(self)

	def _isEqual(self, other) -> bool:
		return self.node.key == other.node.key

	def findOverlayClasses(self, clsList) -> None:
		role = self.role
		states = self.atspiStates
		ifaces = self.atspiInterfaces
		if role == Role.TOGGLEBUTTON and self._comboBoxAbove is not None:
			clsList.append(ComboBoxButton)
		if role == Role.SPINBUTTON and "Text" in ifaces and "Value" in ifaces:
			clsList.append(SpinButton)
		elif "Text" in ifaces and ("editable" in states or role == Role.EDITABLETEXT):
			clsList.append(EditableTextWithAutoSelectDetection)
		if role == Role.LISTITEM and self.atspiRole == "table cell":
			clsList.append(ListCell)
		elif role == Role.TABLECELL:
			clsList.append(TableCell)
		if role == Role.DIALOG:
			clsList.append(Dialog)
		elif role == Role.PROGRESSBAR:
			clsList.append(ProgressBar)
		elif role == Role.TOOLTIP:
			clsList.append(ToolTip)
		elif role == Role.ALERT:
			clsList.append(Notification)
		if self.atspiParentRole == "application":
			clsList.append(TopLevelWindow)
		clsList.append(AtSpi)
		super().findOverlayClasses(clsList)

	# Raw AT-SPI facts, each cached for one core cycle like every NVDAObject property.

	def _get_atspiRole(self) -> str:
		try:
			return handler.client().role(self.node)
		except NodeGone:
			return "invalid"

	def _get_atspiStates(self) -> frozenset[str]:
		try:
			return handler.client().states(self.node)
		except NodeGone:
			return frozenset({"defunct"})

	def _get_atspiAttributes(self) -> dict[str, str]:
		try:
			return handler.client().attributes(self.node)
		except NodeGone:
			return {}

	def _get_atspiInterfaces(self) -> frozenset[str]:
		try:
			return handler.client().interfaces(self.node)
		except NodeGone:
			return frozenset()

	def _get_atspiName(self) -> str:
		try:
			return handler.client().name(self.node)
		except NodeGone:
			return ""

	def _get_atspiParentNode(self) -> Node | None:
		try:
			return handler.client().parent(self.node)
		except NodeGone:
			return None

	def _get_atspiParentRole(self) -> str | None:
		parent = self.atspiParentNode
		if parent is None:
			return None
		try:
			return handler.client().role(parent)
		except NodeGone:
			return None

	def _get_atspiRelations(self) -> dict[str, list[Node]]:
		try:
			return {r.type: r.targets for r in handler.client().relations(self.node)}
		except NodeGone:
			return {}

	def _get_processID(self) -> int:
		try:
			return handler.client().processID(self.node)
		except NodeGone:
			return 0

	# What speech, braille and navigation read.

	def _get_role(self) -> Role:
		raw = self.atspiRole
		if raw == "text":
			# GTK 3 uses one role for entries, text views and labels in some places.
			return Role.EDITABLETEXT if "editable" in self.atspiStates else Role.STATICTEXT
		if raw == "label":
			return Role.STATICTEXT
		if raw == "filler":
			# GTK's boxes: layout, and a container Dialog.getDialogText descends into.
			return Role.PANEL
		if raw == "panel":
			# A GTK frame is a panel named by its label: Windows' group box.
			return Role.GROUPING if self.atspiName else Role.PANEL
		if raw == "menu":
			# Java's model, inherited by GTK: a menu is the item that opens a submenu.
			parentRole = self.atspiParentRole
			if parentRole in ("menu bar", "menu") or "selectable" in self.atspiStates:
				return Role.MENUITEM
			return Role.POPUPMENU
		if raw == "table":
			if self._isSingleColumnTable:
				return Role.LIST
			return Role.TABLE
		if raw == "table cell":
			parent = self.atspiParentNode
			if parent is not None and handler.getObject(parent).role == Role.LIST:
				return Role.LISTITEM
			return Role.TABLECELL
		if raw == "invalid":
			return Role.UNKNOWN
		if raw == "button" and "Hyperlink" in self.atspiInterfaces:
			# GTK's link button.
			return Role.LINK
		return ROLES.get(raw, Role.UNKNOWN)

	def _get__comboBoxAbove(self) -> Node | None:
		"""The combo box within two levels above: GTK 3 puts its button inside a filler inside it."""
		node = self.atspiParentNode
		for _ in range(2):
			if node is None:
				return None
			try:
				if handler.client().role(node) == "combo box":
					return node
				node = handler.client().parent(node)
			except NodeGone:
				return None
		return None

	def _get__isPopupShowing(self) -> bool:
		try:
			popup = handler.client().childAt(self.node, 0)
			return popup is not None and "showing" in handler.client().states(popup)
		except NodeGone:
			return False

	def _get__isSingleColumnTable(self) -> bool:
		if "Table" not in self.atspiInterfaces:
			return False
		try:
			_rows, columns = handler.client().tableSize(self.node)
		except NodeGone:
			return False
		return columns == 1

	def _get_roleText(self) -> str | None:
		if self.role == Role.UNKNOWN and self.atspiRole not in ("unknown", "invalid"):
			return self.atspiRole
		return super().roleText

	def _get_name(self) -> str:
		role = self.role
		name = self.atspiName
		if role == Role.COMBOBOX or not name:
			# A GTK combo box is named by its current choice, which NVDA calls its value; its label is
			# the name, as on Windows.
			labels = [handler.getObject(n).atspiName for n in self.atspiRelations.get("labelled-by", [])]
			labelled = " ".join(label for label in labels if label)
			if labelled or role == Role.COMBOBOX:
				return labelled
		return name

	def _get_value(self) -> str | None:
		role = self.role
		if role == Role.COMBOBOX:
			return self.atspiName or None
		if role in VALUE_ROLES and "Value" in self.atspiInterfaces:
			try:
				current, minimum, maximum, text = handler.client().value(self.node)
			except NodeGone:
				return None
			if text:
				return text
			if role == Role.PROGRESSBAR and maximum > minimum:
				return f"{round((current - minimum) * 100 / (maximum - minimum))}%"
			return str(int(current)) if current == int(current) else f"{current:g}"
		return None

	def _get_description(self) -> str | None:
		try:
			description = handler.client().description(self.node)
		except NodeGone:
			return None
		if not description or description == self.atspiName:
			return None
		if self.role in VALUE_ROLES and "Value" in self.atspiInterfaces:
			# GTK 3 describes a scale by the value it draws.
			try:
				current = handler.client().value(self.node)[0]
			except NodeGone:
				current = None
			if current is not None and description.strip() in (
				str(current),
				f"{current:g}",
				str(int(current)),
			):
				return None
		return description

	def _get_states(self) -> set[State]:
		raw = self.atspiStates
		role = self.role
		states = mapStates(raw, role, self.atspiAttributes)
		if self.atspiRole == "menu" and role == Role.MENUITEM:
			states.add(State.HASPOPUP)
		if role in MENU_ROLES:
			# GTK marks the highlighted menu item selected; Windows menus have no such state.
			states.discard(State.SELECTED)
		if State.MULTILINE in states and not (
			State.EDITABLE in states or role in (Role.EDITABLETEXT, Role.DOCUMENT, Role.TERMINAL)
		):
			# GTK puts multi-line on every label and notebook tab; only fields have it on Windows.
			states.discard(State.MULTILINE)
		if role == Role.COMBOBOX and not {State.EXPANDED, State.COLLAPSED} & states:
			# A GTK 3 combo box has no expanded state; its popup's showing state stands in for it.
			states.add(State.EXPANDED if self._isPopupShowing else State.COLLAPSED)
		if role == Role.LINK:
			states.add(State.LINKED)
		return states

	def _get_keyboardShortcut(self) -> str | None:
		if "Action" not in self.atspiInterfaces:
			return None
		try:
			actions = handler.client().actions(self.node)
		except NodeGone:
			return None
		if not actions:
			return None
		binding = actions[0][1] or ""
		parts = binding.split(";")
		mnemonic = parts[0] if parts else ""
		accelerator = parts[2] if len(parts) > 2 else ""
		mnemonicKey = _formatAccelerator(mnemonic) if mnemonic else ""
		if self.role in MENU_ROLES:
			# On Windows a menu item's mnemonic is the bare letter; its accelerator comes first.
			letter = mnemonicKey.split("+")[-1] if mnemonicKey else ""
			if letter and self.atspiParentRole == "menu bar":
				# A menu bar item opens with Alt and its letter, a menu item with its letter alone.
				letter = f"Alt+{letter}"
			return "  ".join(k for k in (_formatAccelerator(accelerator), letter) if k) or None
		if mnemonicKey and "+" not in mnemonicKey:
			mnemonicKey = f"Alt+{mnemonicKey}"
		return mnemonicKey or _formatAccelerator(accelerator) or None

	def _get_location(self) -> RectLTWH | None:
		if "Component" not in self.atspiInterfaces:
			return None
		try:
			extents = handler.client().extents(self.node)
		except NodeGone:
			return None
		return RectLTWH(*extents) if extents else None

	def _get_parent(self):
		parent = self.atspiParentNode
		if parent is None or parent == handler.desktopNode() or handler.isApplication(parent):
			return api.getDesktopObject()
		return handler.getObject(parent)

	def _get_childCount(self) -> int:
		try:
			return handler.client().childCount(self.node)
		except NodeGone:
			return 0

	def getChild(self, index: int):
		try:
			child = handler.client().childAt(self.node, index)
		except NodeGone:
			return None
		return handler.getObject(child) if child else None

	def _get_children(self) -> list:
		try:
			nodes = handler.client().children(self.node)
		except NodeGone:
			return []
		return [handler.getObject(n) for n in nodes]

	def _get_firstChild(self):
		return self.getChild(0) if self.childCount > 0 else None

	def _get_lastChild(self):
		count = self.childCount
		return self.getChild(count - 1) if count > 0 else None

	def _get_indexInParent(self) -> int | None:
		try:
			index = handler.client().indexInParent(self.node)
		except NodeGone:
			return None
		return index if index >= 0 else None

	def _sibling(self, step: int):
		index = self.indexInParent
		if index is None:
			return None
		parentNode = self.atspiParentNode
		if parentNode is None:
			return None
		try:
			sibling = handler.client().childAt(parentNode, index + step) if index + step >= 0 else None
		except NodeGone:
			return None
		return handler.getObject(sibling) if sibling else None

	def _get_next(self):
		return self._sibling(1)

	def _get_previous(self):
		return self._sibling(-1)

	def _get_positionInfo(self) -> dict[str, int]:
		info = {}
		attrs = self.atspiAttributes
		if "level" in attrs and attrs["level"].isdigit():
			info["level"] = int(attrs["level"])
		if attrs.get("posinset", "").isdigit() and attrs.get("setsize", "").isdigit():
			info["indexInGroup"] = int(attrs["posinset"])
			info["similarItemsInGroup"] = int(attrs["setsize"])
			return info
		role = self.role
		if role in (Role.RADIOBUTTON, Role.RADIOMENUITEM):
			members = self.atspiRelations.get("member-of", [])
			if len(members) > 1:
				ordered = handler.inTreeOrder(members)
				if self.node in ordered:
					info["indexInGroup"] = ordered.index(self.node) + 1
					info["similarItemsInGroup"] = len(ordered)
					return info
		if role in COUNTED_ROLES or role == Role.RADIOBUTTON:
			# Qt gives radio buttons no member-of relation; their siblings in the group box count.
			parent = self.atspiParentNode
			if parent is not None:
				siblings = [
					handler.getObject(n)
					for n in handler.client().children(parent)
					if handler.client().role(n)
					not in ("separator", "table column header", "tearoff menu item")
				]
				siblings = [s for s in siblings if State.INVISIBLE not in s.states]
				if role == Role.RADIOBUTTON:
					siblings = [s for s in siblings if s.role == Role.RADIOBUTTON]
				for index, sibling in enumerate(siblings):
					if sibling.node == self.node:
						info["indexInGroup"] = index + 1
						info["similarItemsInGroup"] = len(siblings)
						break
		return info

	def _get_labeledBy(self):
		targets = self.atspiRelations.get("labelled-by", [])
		return handler.getObject(targets[0]) if targets else None

	def _get_controllerFor(self) -> list:
		return [handler.getObject(n) for n in self.atspiRelations.get("controller-for", [])]

	def _get_flowsTo(self):
		targets = self.atspiRelations.get("flows-to", [])
		return handler.getObject(targets[0]) if targets else None

	def _get_flowsFrom(self):
		targets = self.atspiRelations.get("flows-from", [])
		return handler.getObject(targets[0]) if targets else None

	def isDescendantOf(self, obj) -> bool:
		node = self.atspiParentNode
		for _ in range(100):
			if node is None:
				return False
			if isinstance(obj, AtSpi) and node.key == obj.node.key:
				return True
			try:
				node = handler.client().parent(node)
			except NodeGone:
				return False
		return False

	def _get_isInForeground(self) -> bool:
		foreground = api.getForegroundObject()
		return isinstance(foreground, AtSpi) and foreground.node.busName == self.node.busName

	def setFocus(self) -> None:
		try:
			handler.client().grabFocus(self.node)
		except NodeGone:
			pass

	def scrollIntoView(self) -> None:
		raise NotImplementedError

	def _get_actionCount(self) -> int:
		if "Action" not in self.atspiInterfaces:
			return 0
		try:
			return len(handler.client().actions(self.node))
		except NodeGone:
			return 0

	def getActionName(self, index=None) -> str:
		index = index or self.defaultActionIndex
		try:
			return handler.client().actions(self.node)[index][0]
		except (NodeGone, IndexError):
			raise NotImplementedError from None

	def doAction(self, index=None) -> None:
		index = index or self.defaultActionIndex
		try:
			if not handler.client().doAction(self.node, index):
				raise NotImplementedError
		except NodeGone:
			raise NotImplementedError from None

	def _get_selectionContainer(self):
		parent = self.atspiParentNode
		if parent is not None and "Selection" in handler.client().interfaces(parent):
			return handler.getObject(parent)
		return None

	def getSelectedItemsCount(self, maxCount=2) -> int:
		if "Selection" not in self.atspiInterfaces:
			return 0
		try:
			return handler.client().selectedCount(self.node)
		except NodeGone:
			return 0

	def _get_TextInfo(self):
		if "Text" in self.atspiInterfaces and self.role in (
			Role.EDITABLETEXT,
			Role.STATICTEXT,
			Role.TERMINAL,
			Role.DOCUMENT,
			Role.SPINBUTTON,
			Role.PASSWORDEDIT,
		):
			return AtspiTextInfo
		return super().TextInfo

	def _get_devInfo(self) -> list[str]:
		info = super().devInfo
		info.append(f"AT-SPI node: {self.node.busName} {self.node.path}")
		info.append(f"AT-SPI role: {self.atspiRole}")
		info.append(f"AT-SPI states: {', '.join(sorted(self.atspiStates))}")
		info.append(f"AT-SPI interfaces: {', '.join(sorted(self.atspiInterfaces))}")
		info.append(f"AT-SPI attributes: {self.atspiAttributes}")
		return info


class TopLevelWindow(AtSpi):
	"""A window whose AT-SPI parent is its application, presented as a child of the desktop."""

	def _sibling(self, step: int):
		siblings = api.getDesktopObject().children
		for index, sibling in enumerate(siblings):
			if sibling.node == self.node:
				target = index + step
				return siblings[target] if 0 <= target < len(siblings) else None
		return None

	def _get_indexInParent(self) -> int | None:
		for index, sibling in enumerate(api.getDesktopObject().children):
			if sibling.node == self.node:
				return index
		return None


class ComboBoxButton(AtSpi):
	"""The button inside a GTK 3 combo box, which takes the keyboard focus for it."""

	def _get_focusRedirect(self):
		combo = self._comboBoxAbove
		return handler.getObject(combo) if combo is not None else None


class SpinButton(EditableTextWithoutAutoSelectDetection, AtSpi):
	"""A GTK spin button: an edit field whose number is its value, spoken as a value on Windows."""

	def _get_value(self) -> str | None:
		try:
			current, _minimum, _maximum, text = handler.client().value(self.node)
		except NodeGone:
			return None
		return text or (str(int(current)) if current == int(current) else f"{current:g}")

	def _get__hasNavigableText(self) -> bool:
		# Said as its value on focus, as Windows' up-down controls are, not as the text of a field.
		return False

	def _get_states(self):
		states = super().states
		states.discard(State.EDITABLE)
		return states


class ListCell(AtSpi):
	"""A cell of a one-column GTK tree view, which Windows users know as a list item."""

	def _get_positionInfo(self) -> dict[str, int]:
		try:
			row, _column = handler.client().cellPosition(self.node)
			table = handler.client().cellTable(self.node)
			rows, _columns = handler.client().tableSize(table) if table else (0, 0)
		except NodeGone:
			return {}
		if row < 0 or rows <= 0:
			return {}
		return {"indexInGroup": row + 1, "similarItemsInGroup": rows}


class TableCell(AtSpi):
	def _get_table(self):
		try:
			table = handler.client().cellTable(self.node)
		except NodeGone:
			return None
		return handler.getObject(table) if table else None

	def _get_tableID(self):
		table = self.table
		return table.node.key if table else None

	def _get_rowNumber(self) -> int:
		return handler.client().cellPosition(self.node)[0] + 1

	def _get_columnNumber(self) -> int:
		return handler.client().cellPosition(self.node)[1] + 1

	def _get_rowCount(self) -> int:
		return handler.client().tableSize(self.table.node)[0]

	def _get_columnCount(self) -> int:
		return handler.client().tableSize(self.table.node)[1]

	def _get_columnHeaderText(self) -> str:
		return " ".join(h for h in handler.client().columnHeaderNames(self.node) if h)


class Desktop(AtSpi):
	"""The registry's desktop, whose children are every application's top-level windows."""

	def __init__(self, node: Node | None = None):
		super().__init__(node=node or handler.desktopNode())

	def _isEqual(self, other) -> bool:
		return True

	processID = None
	role = Role.DESKTOPPANE
	isPresentableFocusAncestor = False

	def _get_name(self) -> str:
		# Translators: the desktop as NVDA names it on Windows.
		return _("Desktop")

	def _get_parent(self):
		return None

	def _get_next(self):
		return None

	def _get_previous(self):
		return None

	def _get_indexInParent(self):
		return None

	def _get_children(self) -> list:
		result = []
		for app in handler.applications():
			try:
				windows = handler.client().children(app)
			except NodeGone:
				continue
			result.extend(handler.getObject(w) for w in windows)
		return result

	def _get_childCount(self) -> int:
		return len(self.children)

	def getChild(self, index: int):
		children = self.children
		return children[index] if 0 <= index < len(children) else None

	def _get_states(self) -> set[State]:
		return set()

	def _get_location(self):
		try:
			extents = handler.client().extents(self.node)
		except NodeGone:
			extents = None
		if extents:
			return RectLTWH(*extents)
		import wx

		width, height = wx.GetDisplaySize()
		return RectLTWH(0, 0, width, height)
