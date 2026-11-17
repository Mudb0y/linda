"""AT-SPI events in, NVDA events out: the Linux counterpart of IAccessibleHandler.

The listener runs on the main thread whenever GLib's context iterates, which includes the wx.Yield at
the start of api.processPendingEvents, in the middle of a script. So it only appends the raw event to
a queue and asks for a core pump. pumpAll, which the core pump and api.processPendingEvents call,
filters, coalesces, makes the NVDAObjects and queues NVDA's events, as IAccessibleHandler.pumpAll does
for win events.

The foreground and the focus come from AT-SPI alone. Toolkits bounce activation between the window
being left and the one being entered for a few milliseconds around every window switch, so after any
window activation or deactivation the focus is decided only once window events have settled, and only
for an application that owns the window activated last.
"""

import collections
import os
import threading
import time
import weakref

from logHandler import log

from . import client as _clientModule
from .client import ROOT_PATH, AppHung, Client, Node, NodeGone

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
	"window:deactivate",
	"window:create",
]
WINDOW_ACTIVATION_EVENTS = frozenset({"window:activate", "window:deactivate"})

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
#: Seconds without a window event, and at most in all, that focus waits for after one.
SETTLE_QUIET_SECONDS = 0.03
SETTLE_MAX_SECONDS = 0.15
#: GTK 4 announces objects of a new window before they can be read; one seen this recently that cannot
#: be read is tried again once, after RETRY_DELAY_SECONDS.
NEW_OBJECT_SECONDS = 1.0
RETRY_DELAY_SECONDS = 0.02
#: How long after the foreground deactivated, with no window activated since, to look for an active
#: window again.
RESCAN_DELAYS_SECONDS = (0.25, 1.0)
#: Roles whose "selected" state stands for focus in GTK, whose menu items and tabs announce focus with
#: no focused state.
SELECTED_AS_FOCUS_ROLES = frozenset({"menu", "menu item", "check menu item", "radio menu item", "page tab"})
#: Containers whose focus belongs to the item within them that has it.
_ITEM_CONTAINERS = frozenset(
	{"list", "list box", "page tab list", "tree", "tree table", "table", "layered pane"},
)
#: Containers too big to search for their focused item, whose selected item stands for it: Caja's icon
#: view holds an accessible for every file in the folder.
_SELECTION_CONTAINERS = frozenset({"layered pane"})
#: Roles whose selected state is NVDA's focus and never said as a state: GTK marks the highlighted menu
#: item selected, which is how its focus is known, not a change to announce.
_SELECTED_NOT_A_STATE_ROLES = frozenset({"menu", "menu item", "check menu item", "radio menu item"})

_queue: collections.deque = collections.deque(maxlen=MAX_QUEUED)
_queueLock = threading.Lock()
#: Every NVDAObject made for an accessible and still referenced, by node key, so that an event reaches
#: the instance NVDA already holds; eventHandler and the event_ methods compare by identity.
liveObjects: "weakref.WeakValueDictionary[tuple[str, str], object]" = weakref.WeakValueDictionary()
#: The last active descendant each container reported, by the container's key.
_activeDescendants: dict[tuple[str, str], Node] = {}
_listening = False
_exiting = False
_desktopNode: Node | None = None
#: The top-level activated last, and whether it has deactivated since.
_foreground: Node | None = None
_foregroundActive = False
#: When the latest window activation or deactivation arrived, and the first of those still settling.
_lastWindowEventAt = 0.0
_settleStartedAt: float | None = None
#: When each accessible was first named by an event, for the GTK 4 retry.
_firstSeen: dict[tuple[str, str], float] = {}
#: Focus events waiting to be tried again, with when.
_retries: list[tuple[float, tuple]] = []
#: When to look again for an active window, after the foreground deactivated and none took its place.
_rescans: list[float] = []
#: Roles of event sources, read once each, to know a password field before keeping what it sent.
_roles: dict[tuple[str, str], str] = {}
#: The name each accessible was last given by an event, by key.
_names: dict[tuple[str, str], str] = {}


def knownRole(node: Node) -> str | None:
	"""The role node had when it last sent text, for an accessible gone before its event is handled."""
	return _roles.get(node.key)


def knownName(node: Node) -> str | None:
	"""The name node was last given by an event, for an accessible gone before its event is handled:
	marco's switcher is destroyed within a tenth of a second of naming a workspace."""
	return _names.get(node.key)


#: Application names by bus name.
_applicationNames: dict[str, str] = {}
#: Applications whose only objects that name themselves and vanish at once are status bars: marco's
#: switcher, a popup that lives about 60 ms, names the window or workspace a key chose in one.
_SWITCHER_APPLICATIONS = frozenset({"marco"})


def _applicationName(busName: str) -> str:
	name = _applicationNames.get(busName)
	if name is None:
		c = client()
		for app in applications():
			try:
				_applicationNames[app.busName] = c.name(app)
			except NodeGone:
				continue
		name = _applicationNames.setdefault(busName, "")
	return name


def roleOfVanished(node: Node) -> str | None:
	"""The role an accessible gone before its event is handled had, where it can be known."""
	role = knownRole(node)
	if role is None and _applicationName(node.busName) in _SWITCHER_APPLICATIONS:
		role = "status bar"
		_roles[node.key] = role
	return role


def _focusDesktop() -> None:
	"""Give the focus to the desktop when the window that had it went and no window took its place, as
	Windows does when the last window closes."""
	import api
	import eventHandler

	focus = api.getFocusObject()
	if focus is api.getDesktopObject():
		return
	node = getattr(focus, "node", None)
	try:
		top = _topLevelOf(node) if node is not None else None
		if top is not None and "active" in client().states(top):
			return
	except NodeGone:
		pass
	log.debug("AT-SPI focus: no window took the focus that went; the desktop has it")
	_setForeground(None)
	eventHandler.queueEvent("gainFocus", api.getDesktopObject())


def client() -> Client:
	return _clientModule.client


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


# The foreground and the focus, which AT-SPI has no call for.


def _scanForActiveWindow() -> Node | None:
	"""The top-level that says it is active, asking every application that answers.

	An application marked hung is not skipped here: the client refuses it at once for a while and then
	lets one call through, which is how one that was busy as NVDA started is found again."""
	c = client()
	for app in applications():
		try:
			for window in c.children(app):
				if "active" in c.states(window):
					return window
		except NodeGone:
			continue
	return None


def _setForeground(window: Node | None, active: bool = True) -> None:
	global _foreground, _foregroundActive
	_foreground = window
	_foregroundActive = active and window is not None


def activeWindow() -> Node | None:
	"""The top-level activated last, which is NVDA's foreground; found by asking only while none is known."""
	if _foreground is None:
		window = _scanForActiveWindow()
		if window is not None:
			_setForeground(window)
		return window
	return _foreground


def _topLevelOf(node: Node) -> Node | None:
	c = client()
	current = node
	for _ in range(64):
		parent = c.parent(current)
		if parent is None:
			return None
		if parent.path == ROOT_PATH:
			return current
		current = parent
	return None


def _ownsActiveWindow(node: Node) -> bool:
	"""Whether node's application owns the window activated last and not deactivated since. Where the
	tracking says no, the state of node's own top-level decides, and the tracking follows it."""
	if _foreground is not None and _foregroundActive and _foreground.busName == node.busName:
		return True
	top = _topLevelOf(node)
	if top is None or "active" not in client().states(top):
		return False
	_setForeground(top)
	return True


def _focusIn(window: Node) -> Node | None:
	c = client()
	try:
		if "focused" in c.states(window):
			return window
		focused = c.firstMatchWithState(window, "focused")
	except NodeGone:
		return None
	return activeDescendant(focused) or focused if focused else None


def activeDescendant(container: Node) -> Node | None:
	"""For a container that manages its descendants, the descendant last reported to have the focus
	within it, while it still has it."""
	child = _activeDescendants.get(container.key)
	if child is None:
		return None
	try:
		if "focused" in client().states(child):
			return child
	except NodeGone:
		pass
	return None


def nodeForRelation(relation: str | tuple[int, int]) -> Node | None:
	"""The node for an NVDAObject made for a relation: "focus", "foreground", or a point (x, y)."""
	window = activeWindow()
	if relation == "foreground":
		return window
	if relation == "focus":
		return _focusIn(window) if window else None
	if isinstance(relation, tuple) and len(relation) == 2:
		x, y = relation
		c = client()
		windows = ([window] if window else []) + [
			w
			for app in applications()
			if not c.isHung(app.busName)
			for w in _safeChildren(app)
			if w != window
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


def _isPasswordText(node: Node) -> bool:
	role = _roles.get(node.key)
	if role is None:
		try:
			role = client().role(node)
		except NodeGone:
			# Unknown, so treated as the one whose text must not be kept.
			return True
		if len(_roles) > MAX_QUEUED:
			_roles.clear()
		_roles[node.key] = role
	return role == "password text"


def _onEvent(eventType: str, source: Node | None, detail1: int, detail2: int, anyData) -> None:
	# Runs inside wx.Yield in the middle of whatever NVDA was doing: append and ask for a pump, nothing
	# more.
	if source is None or _exiting:
		return
	import core

	if core._hasShutdownBeenTriggered:
		return
	_clientModule.heardFrom(source.busName)
	now = time.monotonic()
	if eventType.startswith("object:state-changed:"):
		state = eventType[len("object:state-changed:") :]
		# libatspi sends defunct itself whenever it frees its own proxy for a live object. Active is
		# kept for _asWindowEvent.
		if state not in STATE_CHANGE_STATES and state not in ("focused", "showing", "active"):
			return
	elif eventType.startswith("object:text-changed:"):
		# Qt sends what is typed into a password field in clear.
		if _isPasswordText(source):
			anyData = None
	elif eventType == "object:property-change:accessible-role":
		_roles.pop(source.key, None)
	elif eventType == "object:property-change:accessible-name" and isinstance(anyData, str):
		if len(_names) > MAX_QUEUED:
			_names.clear()
		_names[source.key] = anyData
	elif eventType in WINDOW_ACTIVATION_EVENTS:
		global _lastWindowEventAt, _settleStartedAt
		_lastWindowEventAt = now
		if _settleStartedAt is None:
			_settleStartedAt = now
	_firstSeen.setdefault(source.key, now)
	with _queueLock:
		_queue.append((eventType, source, detail1, detail2, anyData))
	core.requestPump(
		immediate=eventType in ("object:state-changed:focused", "object:active-descendant-changed")
		or eventType in WINDOW_ACTIVATION_EVENTS,
	)


#: Milliseconds between further looks for the focus after start, while none has been found. NVDA looks
#: once as it starts; an application too busy to answer then is not asked again until the client's
#: probe time has passed, and an idle one sends no event that would bring its focus.
INITIAL_FOCUS_RETRY_MS = int((_clientModule.PROBE_AFTER_SECONDS + 0.5) * 1000)
INITIAL_FOCUS_RETRIES = 4


def _lookForInitialFocus(retriesLeft: int = INITIAL_FOCUS_RETRIES) -> None:
	import core
	import eventHandler

	if _exiting or not _listening or eventHandler.lastQueuedFocusObject is not None or retriesLeft <= 0:
		return
	log.debug("AT-SPI focus: none found yet; looking again")
	core._setInitialFocus()
	core.callLater(INITIAL_FOCUS_RETRY_MS, _lookForInitialFocus, retriesLeft - 1)


def _onPostNvdaStartup() -> None:
	import core

	core.callLater(INITIAL_FOCUS_RETRY_MS, _lookForInitialFocus)


def initialize() -> None:
	global _listening, _exiting
	_exiting = False
	client().listen(EVENT_TYPES, _onEvent)
	_listening = True
	import core

	core.postNvdaStartup.register(_onPostNvdaStartup)


def terminate() -> None:
	global _listening, _desktopNode, _settleStartedAt
	import core

	core.postNvdaStartup.unregister(_onPostNvdaStartup)
	if _listening:
		client().stopListening(EVENT_TYPES)
	_listening = False
	with _queueLock:
		_queue.clear()
	liveObjects.clear()
	_activeDescendants.clear()
	_firstSeen.clear()
	_retries.clear()
	_rescans.clear()
	_processIDs.clear()
	_roles.clear()
	_names.clear()
	_applicationNames.clear()
	_topLevels.clear()
	_focusWhileHung.clear()
	_setForeground(None)
	_settleStartedAt = None
	_desktopNode = None
	_clientModule.forgetApplications()


def _beginExit() -> None:
	"""Stop being an event client before _doShutdown destroys NVDA's windows, keep from reading them while
	they are torn down, and end the process if exit hangs all the same."""
	global _exiting
	if _exiting:
		return
	_exiting = True
	from linuxPlatform import exitGuard

	exitGuard.arm()
	c = client()
	ownProcess = os.getpid()
	for app in applications():
		try:
			if c.processID(app) == ownProcess:
				_clientModule.refuse(app.busName)
		except NodeGone:
			continue
	terminate()


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


def _trackWindow(event) -> None:
	eventType, source = event[0], event[1]
	log.debug(f"AT-SPI: {eventType} on {source}")
	if eventType == "window:activate":
		_setForeground(source)
		_rescans.clear()
	elif source == _foreground:
		_setForeground(source, active=False)


def _focusCandidate(event) -> Node | None:
	"""The node a focus event would give the focus to, if it still holds."""
	eventType, source, _detail1, _detail2, anyData = event
	c = client()
	if eventType == "window:activate":
		return _focusIn(source) or _popupMenuIn(source) or source
	if eventType == "object:active-descendant-changed":
		if not isinstance(anyData, Node) or "focused" not in c.states(source):
			return None
		if c.toolkit(source) == "gtk" and "focused" not in c.states(anyData):
			# A GTK 3 tree view's cell keeps the focused state while it is current; one without it was
			# left by keys that arrived while the application was busy.
			return None
		return anyData
	states = c.states(source)
	if "defunct" in states:
		return None
	if "focused" not in states:
		# GTK 3 menu items and notebook tabs announce focus but carry only "selected"; a later loss of
		# focus in the same batch has already ruled stale events out.
		if "selected" not in states or c.role(source) not in SELECTED_AS_FOCUS_ROLES:
			return None
		if c.toolkit(source).lower() != "gtk":
			return None
	role = c.role(source)
	if role == "menu" and _hasSelectedChild(source):
		# GTK 3 focuses a menu item again as its submenu opens on a child that is already highlighted;
		# the child's own focus event follows.
		return None
	if "manages-descendants" in states or role in _ITEM_CONTAINERS:
		node = activeDescendant(source) or _focusKeptIn(source)
		if node is None and role in _SELECTION_CONTAINERS:
			node = c.selectedChild(source) if "Selection" in c.interfaces(source) else None
		if node is None and role not in _SELECTION_CONTAINERS:
			# Qt focuses a list or tab bar while one of its items carries the focused state, as Win32
			# lists fire focus on child 0; IAccessibleHandler asks accFocus there.
			node = _focusedItemIn(source)
		return node or source
	return source


def _focusKeptIn(container: Node) -> Node | None:
	"""The focus NVDA has, if it is an item of container that still has the focus: GTK focuses a
	container again whenever its window gets the keyboard back, as after every NVDA key, while the item
	keeps it."""
	import eventHandler

	node = getattr(eventHandler.lastQueuedFocusObject, "node", None)
	if node is None or node.busName != container.busName:
		return None
	c = client()
	try:
		if c.parent(node) == container and "focused" in c.states(node):
			return node
	except NodeGone:
		pass
	return None


def _popupMenuIn(window: Node) -> Node | None:
	"""The menu of a GTK popup window, a context menu among them, which has no item highlighted when it
	opens and so nothing focused."""
	c = client()
	try:
		if c.role(window) != "window" or c.childCount(window) != 1:
			return None
		menu = c.childAt(window, 0)
		return menu if menu is not None and c.role(menu) == "menu" else None
	except NodeGone:
		return None


def _processFocus(event, mayRetry: bool = True) -> bool:
	"""Queue gainFocus for a focus event if it still holds; True if it did, or was already the focus."""
	import eventHandler

	source = event[1]
	try:
		if not _ownsActiveWindow(source):
			log.debug(f"AT-SPI focus: {event[0]} on {source} dropped; its application is not active")
			return False
		node = _focusCandidate(event)
	except AppHung:
		_deferWhileHung(event)
		return True
	except NodeGone:
		seen = _firstSeen.get(source.key)
		if mayRetry and seen is not None and time.monotonic() - seen < NEW_OBJECT_SECONDS:
			_retries.append((time.monotonic() + RETRY_DELAY_SECONDS, event))
		log.debug(f"AT-SPI focus: {event[0]} on {source} could not be read")
		return False
	if node is None:
		log.debug(f"AT-SPI focus: {event[0]} on {source} no longer holds")
		return False
	# Toolkits announce one focus several times, and a bounce can announce it again; NVDA on Windows
	# drops a repeat of the last queued focus in the same layer.
	last = eventHandler.lastQueuedFocusObject
	if getattr(last, "node", None) == node:
		return True
	try:
		obj = eventObject(node)
	except NodeGone:
		return False
	if obj is None:
		return False
	if client().isHung(node.busName):
		# Made while its application stopped answering, the object knows nothing of itself, and as the
		# focus would be spoken as an unknown, unavailable object.
		_deferWhileHung(event)
		return True
	_focusWhileHung.clear()
	eventHandler.queueEvent("gainFocus", obj)
	return True


#: The newest focus event from each application that stopped answering while it was handled, with when,
#: handled again once the application answers.
_focusWhileHung: dict[str, tuple[tuple, float]] = {}
#: Seconds after which such a focus event is given up.
HUNG_FOCUS_SECONDS = 10.0


def _deferWhileHung(event) -> None:
	import core

	log.debug(f"AT-SPI focus: {event[0]} on {event[1]} waits for its application to answer")
	busName = event[1].busName
	waiting = _focusWhileHung.get(busName)
	_focusWhileHung[busName] = (event, waiting[1] if waiting else time.monotonic())
	core.requestPump()


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


def _nvdaEvents(event) -> list[str]:
	"""The NVDA events a raw event becomes, all for its source."""
	eventType, source, detail1, _detail2, _anyData = event
	if eventType.startswith("object:state-changed:"):
		state = eventType[len("object:state-changed:") :]
		if state == "showing":
			if not detail1:
				return []
			role = client().role(source)
			if role in ("alert", "notification", "info bar"):
				return ["alert"]
			if role == "tool tip":
				return ["show"]
			return []
		if state == "selected" and client().role(source) in _SELECTED_NOT_A_STATE_ROLES:
			return []
		return ["stateChange"]
	if eventType == "window:create":
		# A notification is a top-level alert that never takes the focus.
		return ["alert"] if client().role(source) == "alert" else []
	if eventType == "object:property-change:accessible-name":
		try:
			role = client().role(source)
		except NodeGone:
			role = roleOfVanished(source)
			if role is None:
				return []
		# A GTK 3 combo box is named by its choice, which NVDA reports as its value.
		if role == "combo box":
			return ["valueChange"]
		return ["nameChange"]
	if eventType == "object:property-change:accessible-description":
		return ["descriptionChange"]
	if eventType in ("object:property-change:accessible-value", "object:value-changed"):
		return ["valueChange"]
	if eventType in ("object:text-caret-moved", "object:text-selection-changed"):
		return ["caret"]
	if eventType.startswith("object:text-changed:insert"):
		return ["textInsert", "textChange"]
	if eventType.startswith("object:text-changed:delete"):
		return ["textRemove", "textChange"]
	return []


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
	return nvdaEvent in _backgroundEventsOf(node)


#: Process IDs by bus name; one AT-SPI connection is always one process.
_processIDs: dict[str, int] = {}


def processIDOf(node: Node) -> int:
	"""The process of node's application, also once node itself is gone; 0 when it cannot be known."""
	processID = _processIDs.get(node.busName)
	if processID is not None:
		return processID
	c = client()
	try:
		processID = c.processID(node)
	except NodeGone:
		# Asked of the application itself, which outlives a popup that vanished.
		app = next((app for app in applications() if app.busName == node.busName), None)
		try:
			processID = c.processID(app) if app is not None else 0
		except NodeGone:
			processID = 0
	if processID:
		_processIDs[node.busName] = processID
	return processID


def _backgroundEventsOf(node: Node) -> frozenset[str]:
	"""The events an application's app module asks for while it is in the background, as marco's does
	for the window switcher it shows but never activates."""
	import appModuleHandler

	processID = processIDOf(node)
	if not processID:
		return frozenset()
	appModule = appModuleHandler.getAppModuleFromProcessID(processID)
	return getattr(appModule, "backgroundEvents", frozenset())


def _reportTextChange(obj, event) -> None:
	eventType, _source, detail1, detail2, anyData = event
	try:
		from . import typedChars
	except ImportError:
		return
	kind = "insert" if eventType.startswith("object:text-changed:insert") else "delete"
	typedChars.handleTextChange(obj, kind, detail1, detail2, anyData if isinstance(anyData, str) else None)


def _coalesced(events: list) -> list:
	"""The newest of each (event, object) only, in the position of the newest, as the win event limiter
	does; focus, window and text change events are all kept."""
	seen: set = set()
	kept = []
	for event in reversed(events):
		eventType = event[0]
		if (
			_isFocusEvent(event)
			or eventType in WINDOW_ACTIVATION_EVENTS
			or eventType.startswith("object:text-changed:")
		):
			kept.append(event)
			continue
		key = (eventType, event[1].key, event[2] if eventType == "object:state-changed:focused" else None)
		if key in seen:
			continue
		seen.add(key)
		kept.append(event)
	kept.reverse()
	return kept


#: Whether each accessible that changed its active state is a top-level window, by key.
_topLevels: dict[tuple[str, str], bool] = {}


def _asWindowEvent(event) -> tuple | None:
	"""event, or for a change of a top-level's active state the window event it stands for, or None for
	a change of any other object's: Caja's tree view marks its current cell active."""
	eventType, source, detail1, detail2, anyData = event
	if eventType != "object:state-changed:active":
		return event
	topLevel = _topLevels.get(source.key)
	if topLevel is None:
		try:
			parent = client().parent(source)
		except NodeGone:
			return None
		topLevel = parent is not None and parent.path == ROOT_PATH
		if len(_topLevels) > MAX_QUEUED:
			_topLevels.clear()
		_topLevels[source.key] = topLevel
	if not topLevel:
		return None
	# GTK can give a window back the focus, when a dialog over it closes, with only this.
	return ("window:activate" if detail1 else "window:deactivate", source, detail1, detail2, anyData)


def _isSettling(now: float) -> bool:
	global _settleStartedAt
	if _settleStartedAt is None:
		return False
	if now - _lastWindowEventAt < SETTLE_QUIET_SECONDS and now - _settleStartedAt < SETTLE_MAX_SECONDS:
		return True
	_settleStartedAt = None
	return False


def pumpAll() -> None:
	import core
	import eventHandler

	if core._hasShutdownBeenTriggered:
		_beginExit()
		return
	if _exiting:
		return
	now = time.monotonic()
	if _isSettling(now):
		# Window events are still arriving; everything waits, so that what comes after them is read in
		# order once they stop.
		core.requestPump()
		return
	due = [event for at, event in _retries if at <= now]
	_retries[:] = [(at, event) for at, event in _retries if at > now]
	if _rescans and _rescans[0] <= now:
		del _rescans[0]
		if not _foregroundActive and (window := _scanForActiveWindow()) is not None:
			_rescans.clear()
			_setForeground(window)
			due.append(("window:activate", window, 0, 0, None))
		elif not _foregroundActive and not _rescans:
			_focusDesktop()
	with _queueLock:
		events = list(_queue)
		_queue.clear()
	events = [converted for event in events if (converted := _asWindowEvent(event)) is not None]
	for busName, (event, since) in list(_focusWhileHung.items()):
		if now - since > HUNG_FOCUS_SECONDS:
			del _focusWhileHung[busName]
		elif not client().isHung(busName):
			del _focusWhileHung[busName]
			due.append(event)
	if _retries or _rescans or _focusWhileHung:
		core.requestPump()
	if not events and not due:
		return
	for event in due:
		_processFocus(event, mayRetry=False)
	if len(_firstSeen) > MAX_QUEUED:
		for key in [k for k, at in _firstSeen.items() if now - at >= NEW_OBJECT_SECONDS]:
			del _firstSeen[key]
	# Active descendants are remembered whatever else happens to the events.
	for eventType, source, _d1, _d2, anyData in events:
		if eventType == "object:active-descendant-changed" and isinstance(anyData, Node):
			_activeDescendants[source.key] = anyData
		elif eventType == "object:property-change:accessible-role":
			liveObjects.pop(source.key, None)
	kept = _coalesced(events)
	# A focus event followed in the same batch by that object's loss of focus is stale.
	blurredAfter: set[int] = set()
	lostFocus: set = set()
	for index in range(len(kept) - 1, -1, -1):
		event = kept[index]
		if _isBlur(event):
			lostFocus.add(event[1].key)
		elif event[0] == "object:state-changed:focused" and event[1].key in lostFocus:
			blurredAfter.add(index)
	# GTK clears an entry's selection as it loses the focus, after saying so; read as the focus's, it
	# would be announced as the next control is.
	blurred = {event[1].key for event in kept if _isBlur(event)}
	# After a window switch only the newest focus that holds counts, judged once every window event of
	# the batch is known.
	windowsChanged = any(e[0] in WINDOW_ACTIVATION_EVENTS for e in kept)
	focusEvents: list = []
	alerted: set[tuple[str, str]] = set()
	focus = eventHandler.lastQueuedFocusObject
	focusKey = getattr(getattr(focus, "node", None), "key", None)
	# Each application's newest events are kept, as orderedWinEventLimiter keeps a thread's: Caja
	# changes some hundred menu items before it selects a file in a folder it has opened.
	perApplication: collections.Counter = collections.Counter()
	overLimit: set[int] = set()
	alwaysAllowed = {focusKey, _foreground.key if _foreground is not None else None}
	for index in range(len(kept) - 1, -1, -1):
		event = kept[index]
		if _isFocusEvent(event) or _isBlur(event) or event[0] in WINDOW_ACTIVATION_EVENTS:
			continue
		node = event[1]
		if node.key not in alwaysAllowed:
			perApplication[node.busName] += 1
			if perApplication[node.busName] > MAX_EVENTS_PER_APPLICATION:
				overLimit.add(index)

	def flushFocus() -> None:
		for focusEvent in reversed(focusEvents):
			if _processFocus(focusEvent):
				break
		focusEvents.clear()

	for index, event in enumerate(kept):
		eventType = event[0]
		if eventType in WINDOW_ACTIVATION_EVENTS:
			_trackWindow(event)
		if _isBlur(event):
			continue
		if _isFocusEvent(event):
			if index not in blurredAfter:
				focusEvents.append(event)
			continue
		if not windowsChanged:
			flushFocus()
		if index in overLimit:
			continue
		if event[1].key in blurred and eventType in (
			"object:text-caret-moved",
			"object:text-selection-changed",
		):
			continue
		try:
			nvdaEvents = _nvdaEvents(event)
		except NodeGone:
			continue
		if not nvdaEvents:
			continue
		node = event[1]
		if not _shouldAccept(nvdaEvents[0], node):
			continue
		try:
			obj = eventObject(node)
		except NodeGone:
			continue
		if obj is None:
			continue
		if nvdaEvents == ["alert"]:
			# A notification is created and shown at once; it is announced once.
			if node.key in alerted:
				continue
			alerted.add(node.key)
		for nvdaEvent in nvdaEvents:
			eventHandler.queueEvent(nvdaEvent, obj)
		if eventType.startswith("object:text-changed:"):
			_reportTextChange(obj, event)
	if windowsChanged and not _foregroundActive:
		# A window deactivated and none activated after it: marco can give the focus back to a window
		# without that window saying so, as when a dialog closes.
		window = _scanForActiveWindow()
		if window is not None:
			_setForeground(window)
			focusEvents.insert(0, ("window:activate", window, 0, 0, None))
		else:
			# Nor does it always say so at once.
			_rescans[:] = [now + delay for delay in RESCAN_DELAYS_SECONDS]
			core.requestPump()
	flushFocus()
	log.debug(f"AT-SPI pump: {len(events)} raw events, {len(kept)} after coalescing")
