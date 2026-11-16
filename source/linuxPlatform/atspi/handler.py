"""AT-SPI events in, NVDA events out: the Linux counterpart of IAccessibleHandler.

The listener runs on the main thread whenever GLib's context iterates, which includes the wx.Yield at
the start of api.processPendingEvents, in the middle of a script. So it only appends the raw event to
a queue and asks for a core pump. pumpAll, which the core pump and api.processPendingEvents call,
filters, coalesces, makes the NVDAObjects and queues NVDA's events, as IAccessibleHandler.pumpAll does
for win events.
"""

import collections
import threading
import weakref

from logHandler import log

from .client import ROOT_PATH, Client, Node, NodeGone

#: The events registered with the registry. Registration filters nothing in at-spi2-core 2.62, so the
#: callback filters again.
EVENT_TYPES = [
	"object:state-changed",
	"object:property-change:accessible-name",
	"object:property-change:accessible-description",
	"object:property-change:accessible-value",
	"object:property-change:accessible-role",
	"object:value-changed",
	"object:text-caret-moved",
	"object:text-selection-changed",
	"object:text-changed",
	"object:active-descendant-changed",
	"object:announcement",
	"window:activate",
]

#: AT-SPI state changes NVDA presents as stateChange, by the state's name.
STATE_CHANGE_STATES = frozenset(
	{
		"checked",
		"pressed",
		"expanded",
		"collapsed",
		"selected",
		"busy",
		"indeterminate",
		"sensitive",
		"enabled",
		"invalid-entry",
		"required",
		"visited",
		"read-only",
		"editable",
		"has-popup",
	},
)
#: Events per application per cycle beyond which events not for the focus are dropped, as
#: orderedWinEventLimiter caps each thread's.
MAX_EVENTS_PER_APPLICATION = 10
#: Raw events kept between pumps at most; a flood beyond this loses its oldest events.
MAX_QUEUED = 5000

_client: Client | None = None
_desktopNode: Node | None = None
_queue: collections.deque = collections.deque(maxlen=MAX_QUEUED)
_queueLock = threading.Lock()
#: Every NVDAObject made for an accessible and still referenced, by node key, so that an event reaches
#: the instance NVDA already holds; eventHandler and the event_ methods compare by identity.
liveObjects: "weakref.WeakValueDictionary[tuple[str, str], object]" = weakref.WeakValueDictionary()
#: The last active descendant each container reported, by the container's key.
_activeDescendants: dict[tuple[str, str], Node] = {}
_listening = False


def client() -> Client:
	global _client
	if _client is None:
		_client = Client()
	return _client


def desktopNode() -> Node:
	global _desktopNode
	if _desktopNode is None:
		_desktopNode = client().desktop()
	return _desktopNode


def applications() -> list[Node]:
	try:
		return client().applications()
	except NodeGone:
		return []


def isApplication(node: Node) -> bool:
	return node.path == ROOT_PATH and node != desktopNode()


def registerObject(obj) -> None:
	liveObjects[obj.node.key] = obj


def getObject(node: Node):
	"""A new NVDAObject for node, as navigation makes them on Windows; the desktop is the one NVDA holds.

	A fresh instance carries none of the per-instance state NVDA leaves on objects it has spoken or
	focused (a cached container, wasGainFocusObj, the spoken-properties cache), which would otherwise
	make, among other things, speech for a former focus count as cancelled when the navigator reaches it.
	"""
	import api

	if node == desktopNode():
		return api.getDesktopObject()
	from NVDAObjects.AtSpi import AtSpi

	return AtSpi(node=node)


def eventObject(node: Node):
	"""The NVDAObject an event for node goes to: the instance NVDA already holds for it when there is one,
	as the event_ methods and eventHandler compare by identity."""
	import api
	import eventHandler

	if node == desktopNode():
		return api.getDesktopObject()
	for candidate in (eventHandler.lastQueuedFocusObject, api.getFocusObject(), *api.getFocusAncestors()):
		if getattr(candidate, "node", None) == node:
			return candidate
	obj = liveObjects.get(node.key)
	if obj is not None:
		return obj
	return getObject(node)


def findStatusBar(window) -> object | None:
	"""The first status bar inside window, found by role in one Collection call."""
	node = getattr(window, "node", None)
	if node is None:
		return None
	try:
		found = client().firstMatchWithRole(node, "status bar")
	except NodeGone:
		return None
	return getObject(found) if found else None


def inTreeOrder(nodes: list[Node]) -> list[Node]:
	"""nodes in the order a user meets them; GTK lists a radio group's members last first."""
	c = client()
	try:
		parents = {c.parent(n) for n in nodes}
		if len(parents) == 1:
			return sorted(nodes, key=c.indexInParent)
		return sorted(nodes, key=lambda n: (c.extents(n) or (0, 0, 0, 0))[1::-1])
	except NodeGone:
		return list(nodes)


# Finding the focus, the foreground and what is under a point, which AT-SPI has no call for.


def _activeWindow() -> Node | None:
	c = client()
	for app in applications():
		try:
			for window in c.children(app):
				if "active" in c.states(window):
					return window
		except NodeGone:
			continue
	return None


def _focusIn(window: Node) -> Node | None:
	c = client()
	try:
		if "focused" in c.states(window):
			return window
		focused = c.firstMatchWithState(window, "focused")
	except NodeGone:
		return None
	return _activeDescendantOf(focused) if focused else None


def _activeDescendantOf(node: Node) -> Node:
	"""For a container that manages its descendants, the descendant that has the focus within it."""
	child = _activeDescendants.get(node.key)
	if child is None:
		return node
	try:
		if "focused" in client().states(child):
			return child
	except NodeGone:
		pass
	return node


def nodeForRelation(relation) -> Node | None:
	window = _activeWindow()
	if relation == "foreground":
		return window
	if relation == "focus":
		return _focusIn(window) if window else None
	if isinstance(relation, tuple) and len(relation) == 2:
		x, y = relation
		c = client()
		windows = ([window] if window else []) + [
			w for app in applications() for w in _safeChildren(app) if w != window
		]
		for top in windows:
			try:
				extents = c.extents(top)
			except NodeGone:
				continue
			if not extents or not (
				extents[0] <= x < extents[0] + extents[2] and extents[1] <= y < extents[1] + extents[3]
			):
				continue
			node = top
			for _ in range(64):
				try:
					deeper = c.accessibleAtPoint(node, x, y)
				except NodeGone:
					deeper = None
				if deeper is None or deeper == node:
					break
				node = deeper
			return node
	return None


def _safeChildren(node: Node) -> list[Node]:
	try:
		return client().children(node)
	except NodeGone:
		return []


# Event intake.


def _onEvent(eventType: str, source: Node | None, detail1: int, detail2: int, anyData) -> None:
	# Runs inside wx.Yield in the middle of whatever NVDA was doing: append and ask for a pump, nothing
	# more.
	if source is None:
		return
	if eventType.startswith("object:state-changed:"):
		state = eventType[len("object:state-changed:") :]
		if state == "defunct":
			if detail1:
				liveObjects.pop(source.key, None)
			return
		if state not in STATE_CHANGE_STATES and state not in ("focused", "showing", "active"):
			return
	with _queueLock:
		_queue.append((eventType, source, detail1, detail2, anyData))
	import core

	core.requestPump(immediate=eventType in ("object:state-changed:focused", "window:activate"))


def initialize() -> None:
	global _listening
	import os

	if os.environ.get("BASEDREADER_PROTO_NOLISTEN"):
		return
	client().listen(EVENT_TYPES, _onEvent)
	_listening = True


def terminate() -> None:
	global _listening
	if _listening and _client is not None:
		_client.stopListening(EVENT_TYPES)
	_listening = False
	liveObjects.clear()
	_activeDescendants.clear()


# Turning raw events into NVDA's.


def _isFocusEvent(event) -> bool:
	eventType, _source, detail1, _detail2, _anyData = event
	return (
		(eventType == "object:state-changed:focused" and detail1)
		or eventType == "window:activate"
		or (eventType == "object:active-descendant-changed")
	)


def _isBlur(event) -> bool:
	return event[0] == "object:state-changed:focused" and not event[2]


def _processFocus(event) -> bool:
	"""Queue gainFocus for a focus event if it still holds; True if it did, or was already the focus."""
	import eventHandler

	eventType, source, _detail1, _detail2, anyData = event
	c = client()
	try:
		if eventType == "window:activate":
			node = _focusIn(source) or source
		elif eventType == "object:active-descendant-changed":
			if not isinstance(anyData, Node) or "focused" not in c.states(source):
				return False
			node = anyData
		else:
			states = c.states(source)
			# GTK 3 menu items and notebook tabs announce focus but carry only "selected" in their
			# state set; a later loss of focus in the same batch has already ruled stale events out.
			if "defunct" in states or not ({"focused", "selected"} & states):
				return False
			if c.role(source) == "menu" and _hasSelectedChild(source):
				# GTK 3 focuses a menu item again as its submenu opens on a child that is already
				# highlighted; the child's own focus event follows.
				return False
			node = source
			if "manages-descendants" in states or c.role(source) in _ITEM_CONTAINERS:
				node = _activeDescendantOf(source)
				if node == source:
					# Qt focuses a list or tab bar while one of its items carries the focused state, as
					# Win32 lists fire focus on child 0; IAccessibleHandler asks accFocus there.
					node = _focusedItemIn(source) or source
	except NodeGone:
		return False
	last = eventHandler.lastQueuedFocusObject
	if getattr(last, "node", None) == node:
		return True
	obj = eventObject(node)
	if obj is None:
		return False
	eventHandler.queueEvent("gainFocus", obj)
	return True


#: Containers whose focus belongs to the item within them that has it.
_ITEM_CONTAINERS = frozenset({"list", "list box", "page tab list", "tree", "tree table", "table"})


def _focusedItemIn(container: Node) -> Node | None:
	c = client()
	try:
		found = c.firstMatchWithState(container, "focused")
	except NodeGone:
		return None
	return found if found is not None and found != container else None


def _hasSelectedChild(node: Node) -> bool:
	c = client()
	for child in c.children(node):
		if {"selected", "showing"} <= c.states(child):
			return True
	return False


def _nvdaEvent(event) -> tuple[str, Node] | None:
	eventType, source, detail1, _detail2, _anyData = event
	if eventType.startswith("object:state-changed:"):
		state = eventType[len("object:state-changed:") :]
		if state == "showing":
			if not detail1:
				return None
			role = client().role(source)
			if role in ("alert", "notification", "info bar"):
				return ("alert", source)
			if role == "tool tip":
				return ("show", source)
			return None
		if state == "active":
			return None
		return ("stateChange", source)
	if eventType == "object:property-change:accessible-name":
		# A GTK 3 combo box is named by its choice, which NVDA reports as its value.
		if client().role(source) == "combo box":
			return ("valueChange", source)
		return ("nameChange", source)
	if eventType == "object:property-change:accessible-description":
		return ("descriptionChange", source)
	if eventType in ("object:property-change:accessible-value", "object:value-changed"):
		return ("valueChange", source)
	if eventType in ("object:text-caret-moved", "object:text-selection-changed"):
		return ("caret", source)
	if eventType.startswith("object:text-changed:insert"):
		return ("textInsert", source)
	if eventType.startswith("object:text-changed:delete"):
		return ("textRemove", source)
	return None


def _shouldAccept(nvdaEvent: str, node: Node) -> bool:
	"""eventHandler.shouldAcceptEvent, for an event that has no window handle: the application in the
	foreground, or with the focus, is heard; others only for alerts and background progress bars."""
	import api
	import config

	if nvdaEvent in ("alert", "show"):
		return True
	if (
		nvdaEvent == "valueChange"
		and config.conf["presentation"]["progressBarUpdates"]["reportBackgroundProgressBars"]
	):
		return True
	for obj in (api.getFocusObject(), api.getForegroundObject()):
		if getattr(obj, "node", None) is not None and obj.node.busName == node.busName:
			return True
	return False


def pumpAll() -> None:
	import core
	import eventHandler

	with _queueLock:
		events = list(_queue)
		_queue.clear()
	if core._hasShutdownBeenTriggered:
		# Stop being an event client before _doShutdown destroys NVDA's windows.
		terminate()
		return
	if not events:
		# Once exit has begun NVDA destroys its own windows inside nested loops; reading their accessibles
		# then reaches widgets GTK is tearing down, and NVDA hung at exit with the welcome dialog open.
		return
	# Active descendants are remembered whatever else happens to the events.
	for eventType, source, _d1, _d2, anyData in events:
		if eventType == "object:active-descendant-changed" and isinstance(anyData, Node):
			_activeDescendants[source.key] = anyData
		elif eventType == "object:property-change:accessible-role":
			liveObjects.pop(source.key, None)
	# Newest of each (event, object) only, in the position of the newest, as the win event limiter does.
	seen: set = set()
	kept = []
	for event in reversed(events):
		key = (event[0], event[1].key, event[2] if event[0] == "object:state-changed:focused" else None)
		if key in seen and not _isFocusEvent(event):
			continue
		seen.add(key)
		kept.append(event)
	kept.reverse()
	# A focus event followed in the same batch by that object's loss of focus is stale.
	blurredAfter: set[int] = set()
	lostFocus: set = set()
	for index in range(len(kept) - 1, -1, -1):
		event = kept[index]
		if _isBlur(event):
			lostFocus.add(event[1].key)
		elif event[0] == "object:state-changed:focused" and event[1].key in lostFocus:
			blurredAfter.add(index)
	focusEvents: list = []
	perApplication: collections.Counter = collections.Counter()
	focus = eventHandler.lastQueuedFocusObject
	focusKey = getattr(getattr(focus, "node", None), "key", None)

	def flushFocus() -> None:
		for focusEvent in reversed(focusEvents):
			if _processFocus(focusEvent):
				break
		focusEvents.clear()

	for index, event in enumerate(kept):
		if _isBlur(event):
			continue
		if _isFocusEvent(event):
			if index not in blurredAfter:
				focusEvents.append(event)
			continue
		flushFocus()
		try:
			mapped = _nvdaEvent(event)
		except NodeGone:
			continue
		if mapped is None:
			continue
		nvdaEvent, node = mapped
		if node.key != focusKey:
			perApplication[node.busName] += 1
			if perApplication[node.busName] > MAX_EVENTS_PER_APPLICATION:
				continue
		if not _shouldAccept(nvdaEvent, node):
			continue
		try:
			obj = eventObject(node)
		except NodeGone:
			continue
		if obj is None:
			continue
		eventHandler.queueEvent(nvdaEvent, obj)
	flushFocus()
	log.debug(f"AT-SPI pump: {len(events)} raw events, {len(kept)} after coalescing")
