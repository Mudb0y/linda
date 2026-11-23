"""GTK 2's menus, a context menu and a menu bar's, each item spoken as the arrows reach it, those of a
menu that a menu item opens among them.

nix develop . -c tests/linux/rig python3 -m pytest -q tests/linux/test_gtk2Menus.py

GTK 2 is not in the dev shell, and today's PyGObject no longer loads it, so what its accessibility
module, GAIL, was heard to send in GTK 2.24.33, the last release, is replayed through handler.pumpAll
with a client that answers as GAIL did. GAIL marks no menu item selected, and takes the focused state
from every item of a menu that a menu item opens, as each of a menu bar's menus is: which item is
highlighted only a menu, or the item that opens it, says, through its selection. It says an item with
a submenu has the focus only once the submenu shows, 225 ms after the item is selected, and when the
highlight came from another item, says first that the menu, or the item that opens it, has the focus.
"""

from pathlib import Path

import harness

GAIL = """
	import types

	import core
	import eventHandler
	from linuxPlatform.atspi import client as atspiClient
	from linuxPlatform.atspi import handler
	from linuxPlatform.atspi.client import ROOT_PATH, Node

	core.callLater = lambda *args, **kwargs: None
	core.requestPump = lambda *args, **kwargs: None
	handler.liveRegions.handle = lambda event: False
	BUS = ":1.42"
	# Known as it is once one of the program's windows has been activated.
	atspiClient._toolkits[BUS] = "GAIL"
	application = Node(BUS, ROOT_PATH, None)
	parents = {}
	children = {}
	roles = {}
	states = {}
	# The item each menu, and each item that opens one, says is selected, or None.
	selected = {}


	class Client:
		def parent(self, n):
			return parents[n.key]

		def children(self, n, limit=None):
			return list(children.get(n.key, []))

		def role(self, n):
			return roles[n.key]

		def states(self, n):
			return frozenset(states[n.key])

		def toolkit(self, n):
			return "GAIL"

		def interfaces(self, n):
			return frozenset({"Accessible", "Selection"} if n.key in selected else {"Accessible"})

		def selectedChild(self, n, index=0):
			return selected[n.key]

		def selectedCount(self, n):
			return 0 if selected[n.key] is None else 1

		def isHung(self, busName):
			return False


	atspiClient._client = Client()


	def node(path, parent, role, *nodeStates):
		made = Node(BUS, path, None)
		parents[made.key] = parent
		children.setdefault(parent.key, []).append(made)
		roles[made.key] = role
		states[made.key] = set(nodeStates)
		if role in ("menu bar", "menu"):
			selected[made.key] = None
		return made


	queued = []


	def queueEvent(name, obj):
		queued.append((name, obj.node.path))
		if name == "gainFocus":
			eventHandler.lastQueuedFocusObject = obj


	eventHandler.queueEvent = queueEvent
	handler.eventObject = lambda n: types.SimpleNamespace(
		node=n, role=None, name="", states=set(), value=None, description=None, keyboardShortcut=None,
		positionInfo={}, atspiRole=roles[n.key],
	)


	def selection(item, isSelected):
		return ("object:state-changed:selected", item, int(isSelected), 0, None)


	def focus(n):
		return ("focus:", n, 0, 0, None)


	def shown(n):
		return ("object:state-changed:showing", n, 1, 0, None)


	def pump(*events):
		queued.clear()
		with handler._queueLock:
			handler._queue.extend(events)
		handler.pumpAll()
		return list(queued)
"""

#: An entry's context menu, open with no item highlighted: Select All, then Input Methods, whose submenu
#: has more radio items than the ten events of one program a batch keeps, as GTK 2's has.
CONTEXT_MENU = (
	GAIL
	+ """
	window = node("/window", application, "window", "active", "showing")
	menu = node("/menu", window, "menu", "focused", "showing")
	selectAll = node("/selectAll", menu, "menu item", "showing")
	inputMethods = node("/inputMethods", menu, "menu", "showing")
	methods = [node(f"/method{index}", inputMethods, "radio menu item") for index in range(14)]
	handler._setForeground(window)
	eventHandler.lastQueuedFocusObject = handler.eventObject(menu)
	result = {}

	# Down: the item is selected, then focused at once, as it opens no submenu.
	selected[menu.key] = selectAll
	states[selectAll.key] = {"focused", "showing"}
	states[menu.key] = {"showing"}
	result["Down to Select All"] = pump(selection(selectAll, True), focus(selectAll))

	# Down: the highlight leaves Select All for an item with a submenu, and GAIL says the menu has the
	# focus.
	selected[menu.key] = inputMethods
	states[selectAll.key] = {"showing"}
	states[menu.key] = {"focused", "showing"}
	result["Down to Input Methods"] = pump(
		selection(selectAll, False),
		selection(inputMethods, True),
		*[shown(method) for method in methods],
		focus(menu),
	)

	# 225 ms later the submenu shows, and GAIL says the item has the focus.
	states[menu.key] = {"showing"}
	states[inputMethods.key] = {"focused", "showing"}
	result["submenu shows"] = pump(focus(inputMethods))

	# Right: the submenu's first item, which says neither selected nor focused nor showing.
	selected[inputMethods.key] = methods[0]
	result["Right"] = pump(selection(methods[0], True), focus(methods[0]))
	selected[inputMethods.key] = methods[1]
	result["Down in the submenu"] = pump(
		selection(methods[0], False), selection(methods[1], True), focus(methods[1])
	)

	# Left: GAIL gives the focus back to the item that opens the submenu.
	selected[inputMethods.key] = None
	result["Left"] = pump(selection(methods[1], False), focus(inputMethods))

	# A selection of an item its menu no longer holds, as the highlight moved on before it was read.
	result["stale selection"] = pump(selection(selectAll, True))
"""
)


def test_contextMenu(tmp_path: Path) -> None:
	"""Each item is the focus as it is selected: Input Methods at once and not the menu GAIL says has the
	focus first, and once, though more of its submenu's items show than a batch keeps of one program;
	the submenu's items, which say nothing of their highlight, as their menu says they are selected; Left
	back to Input Methods; and no item its menu does not hold."""
	done = harness.runChild(tmp_path, CONTEXT_MENU, prepare="config", timeout=120)
	done.assertClean()
	assert done.value == {
		"Down to Select All": [["gainFocus", "/selectAll"]],
		"Down to Input Methods": [["gainFocus", "/inputMethods"]],
		"submenu shows": [],
		"Right": [["gainFocus", "/method0"]],
		"Down in the submenu": [["gainFocus", "/method1"]],
		"Left": [["gainFocus", "/inputMethods"]],
		"stale selection": [],
	}, done.value


#: A menu bar with a File menu: Open, then Recent, whose submenu holds two files. The items of the File
#: menu, which the File item opens, say neither selected nor focused nor showing.
MENU_BAR = (
	GAIL
	+ """
	frame = node("/frame", application, "frame", "active", "showing")
	menuBar = node("/menuBar", frame, "menu bar", "showing")
	fileItem = node("/file", menuBar, "menu", "showing")
	openItem = node("/open", fileItem, "menu item")
	recent = node("/recent", fileItem, "menu")
	files = [node(f"/recent{index}", recent, "menu item") for index in range(2)]
	handler._setForeground(frame)
	# F10 has highlighted the File item, whose menu shows with no item highlighted.
	selected[menuBar.key] = fileItem
	eventHandler.lastQueuedFocusObject = handler.eventObject(fileItem)
	result = {}

	selected[fileItem.key] = openItem
	result["Down to Open"] = pump(selection(openItem, True), focus(openItem))

	# Down: the highlight leaves Open for an item with a submenu, and GAIL says the File item has the
	# focus.
	selected[fileItem.key] = recent
	states[fileItem.key] = {"focused", "showing"}
	result["Down to Recent"] = pump(
		selection(openItem, False),
		selection(recent, True),
		*[shown(item) for item in files],
		focus(fileItem),
	)
	states[fileItem.key] = {"showing"}
	result["submenu shows"] = pump(focus(recent))
	selected[recent.key] = files[0]
	result["Right"] = pump(selection(files[0], True), focus(files[0]))
	selected[recent.key] = None
	result["Left"] = pump(selection(files[0], False), focus(recent))
"""
)


def test_menuBar(tmp_path: Path) -> None:
	"""Each item of the File menu, and of Recent's submenu, as it is selected; Recent at once and not the
	File item GAIL says has the focus first; and Left back to Recent."""
	done = harness.runChild(tmp_path, MENU_BAR, prepare="config", timeout=120)
	done.assertClean()
	assert done.value == {
		"Down to Open": [["gainFocus", "/open"]],
		"Down to Recent": [["gainFocus", "/recent"]],
		"submenu shows": [],
		"Right": [["gainFocus", "/recent0"]],
		"Left": [["gainFocus", "/recent"]],
	}, done.value
