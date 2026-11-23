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
from . import liveRegions
from .client import ROOT_PATH, AppHung, Client, Node, NodeGone

#: What ATK's focus tracker sends, which GTK 3 called before it reported focus as a change of the focused
#: state: a program that bundles an older GTK 3 and bridge sends only this for its menus. Orca takes it
#: as focus. A bridge sends it only to a listener registered for it, and it is taken only from the
#: toolkits in LEGACY_FOCUS_TOOLKITS.
LEGACY_FOCUS_EVENT = "focus:"
#: The toolkits whose focus: is a focus, by their ToolkitName in lower case: GTK 3's, and GTK 2's. Today's
#: GTK 3 never sends it. Firefox and Qt send it beside the state change, and Firefox also for a menu as it
#: opens, which NVDA on Windows never takes for the focus, as the item focused in it is.
LEGACY_FOCUS_TOOLKITS = frozenset({"gtk", "gail"})
#: The events that say an object has the focus, the state change only with detail1 set.
_FOCUS_EVENTS = frozenset({"object:state-changed:focused", LEGACY_FOCUS_EVENT})

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
	LEGACY_FOCUS_EVENT,
]
WINDOW_ACTIVATION_EVENTS = frozenset({"window:activate", "window:deactivate"})
#: What Qt's QAccessibleAnnouncementEvent and GTK 4's gtk_accessible_announce send: a message for the
#: screen reader to speak, as a UIA notification is on Windows.
ANNOUNCEMENT_EVENT = "object:announcement"

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
#: The same after a shell's own surface deactivated, in a Wayland session: GNOME Shell lets go of the
#: keyboard for some 2.3 s while it checks a password typed into its prompts, and takes it back.
SHELL_RESCAN_DELAYS_SECONDS = (0.25, 1.0, 2.0, 3.0, 4.0)
#: Seconds after a top-level shows at which its states are read, in a Wayland session, for an activation
#: nothing announced: GTK 4 now and then sends no window event, active state or focus for a window it
#: maps, whose states say active once it has the keyboard (experiment E3). The keyboard came 20 ms after
#: the map in one set-up and 2.3 to 4.1 s after it in another.
SHOWN_PROBE_SECONDS = (0.1, 0.5, 1.0, 2.0, 3.0, 4.5, 6.0)
#: Of those reads, the ones after which an accessible that could not be read at all is given up on.
UNREAD_SHOWN_PROBES = 2
#: The state changes that say a top-level was mapped; GTK 4 sends both, with detail1 0.
_SHOWN_STATES = frozenset({"showing", "visible"})
#: Roles whose "selected" state stands for focus in GTK, whose menu items and tabs announce focus with
#: no focused state.
SELECTED_AS_FOCUS_ROLES = frozenset({"menu", "menu item", "check menu item", "radio menu item", "page tab"})
#: The ToolkitName of the Java ATK wrapper, through which Swing programs, JetBrains IDEs among them, reach
#: AT-SPI.
JAVA_TOOLKIT = "J2SE-access-bridge"
#: What a Swing menu's highlighted item carries in place of the focused state the wrapper announces for it:
#: selected in a menu bar's menus, and only armed in a popup menu.
_JAVA_HIGHLIGHT_STATES = frozenset({"selected", "armed"})
#: Seconds after a JetBrains IDE's submenu item lost its highlight before the submenu is asked whether it
#: closed: the IDE hides it, and takes the selection from every menu when all close, some 30 ms after.
JAVA_MENU_RETURN_SECONDS = 0.15
#: The menu each Java popup menu was opened from, by the popup menu's key, as NVDA's focus went from the
#: one to an item of the other.
_javaSubmenus: dict[tuple[str, str], Node] = {}
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
#: Whether those looks have been made longer once, in a Wayland session, for an application that is busy.
_rescansExtended = False
#: Roles of event sources, read once each, to know a password field before keeping what it sent.
_roles: dict[tuple[str, str], str] = {}
#: The name each accessible was last given by an event, by key.
_names: dict[tuple[str, str], str] = {}
#: Whether shown top-levels are read for an activation nothing announced. Only in a Wayland session: in
#: an X session no toolkit has been seen to miss one, and what is said there stays as it is.
_probesShown = False
#: Whether the rules for what the desktops' own shells and lock screens send apply, which were seen only
#: in a Wayland session: a window activated with no focus found in it leaves NVDA's focus inside it
#: alone, a focus that waited for its application is not replaced by that window's activation, a focus
#: in a window wins over the window's activation in the same batch, the events of the window activated
#: last are heard before NVDA's foreground follows it, GNOME Shell's switcher's choice is never dropped
#: among its other events, and a shell that lets go of the keyboard, or an application that is busy, is
#: waited for longer before the desktop is given the focus. In an X session what is said stays as it is.
_waylandRules = False
#: Accessibles whose showing or visible state changed, with when, not yet looked at.
_shown: collections.deque = collections.deque(maxlen=MAX_QUEUED)
#: Top-levels shown with no window event for them since, by key: the node, when it showed, and how many
#: of SHOWN_PROBE_SECONDS have been read.
_shownProbes: dict[tuple[str, str], tuple[Node, float, int]] = {}
#: When the latest window:activate was handled.
_activatedAt = 0.0
#: When a pump is already asked for at, for the next read of a shown top-level.
_probePumpAt: float | None = None


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
_STATUS_BAR_POPUP_APPLICATIONS = frozenset({"marco"})
#: Applications whose top-level says it is active while Alt+Tab is held, before the window chosen
#: does: marco's switcher, by the name it gives the accessibility bus.
_SWITCHER_APPLICATIONS = frozenset({"marco"})
#: The same in a Wayland session only, GNOME Shell's stage, which holds its switcher, and KWin's
#: switcher: in an X session running either, their active window is the foreground as it always was.
_WAYLAND_SWITCHER_APPLICATIONS = frozenset({"gnome-shell", "kwin"})
#: Of those, the ones whose switcher marks the item chosen selected and never gives it the focus.
_SELECTING_SWITCHERS = frozenset({"gnome-shell"})


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
	if role is None and _applicationName(node.busName) in _STATUS_BAR_POPUP_APPLICATIONS:
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
	lets one call through, which is how one that was busy as NVDA started is found again. One that has
	just answered slowly is asked last: marco's switcher is found active after it."""
	c = client()
	apps = applications()
	apps.sort(key=lambda app: _clientModule.answeredSlowlyWithin(app.busName, BUSY_SECONDS))
	for app in apps:
		try:
			for window in c.children(app):
				if "active" in c.states(window):
					return window
		except NodeGone:
			continue
	return None


def _isSwitcherChoice(event) -> bool:
	"""Whether event is an item of a switcher in _SELECTING_SWITCHERS becoming the one chosen. GNOME Shell
	selects the first as it shows its switcher, among some thirty other events of its own."""
	return (
		event[0] == "object:state-changed:selected"
		and bool(event[2])
		and _applicationName(event[1].busName) in _SELECTING_SWITCHERS
	)


#: Applications that show their notifications inside a window of their own, among some twenty other
#: showing events of theirs that would leave a notification's past the per-application limit.
_NOTIFYING_SHELLS = frozenset({"gnome-shell"})


def _isShellNotificationShown(event) -> bool:
	"""Whether event is a notification of an application in _NOTIFYING_SHELLS showing."""
	if (
		event[0] != "object:state-changed:showing"
		or not event[2]
		or _applicationName(event[1].busName) not in _NOTIFYING_SHELLS
	):
		return False
	try:
		return client().role(event[1]) in ("alert", "notification")
	except (AppHung, NodeGone):
		return False


def _anyApplicationBusy() -> bool:
	"""Whether an application has answered slowly within BUSY_SECONDS, which a window it is about to
	activate waits for."""
	return any(_clientModule.answeredSlowlyWithin(app.busName, BUSY_SECONDS) for app in applications())


def _isSwitcher(window: Node) -> bool:
	"""Whether window is a switcher's, which says it is active while Alt is held, as marco's does though it
	never has the keyboard: a switch is under way, and the window it ends in will say so."""
	name = _applicationName(window.busName)
	return name in _SWITCHER_APPLICATIONS or (_waylandRules and name in _WAYLAND_SWITCHER_APPLICATIONS)


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


#: Accessibles found to lie under their application's root, by key. Only that is remembered: an
#: accessible gone and its path given to one beside the root would at worst take the focus as before.
_underRoot: set[tuple[str, str]] = set()


def _inShadowTree(node: Node) -> bool:
	"""Whether node is in a tree a toolkit keeps beside its application's own, under an application object
	that is not the application's root, which holds none of it.

	Firefox draws its windows in GTK widgets, and GTK makes accessibles of its own for them: a frame
	named as the window, under GTK's application object, holding a grouping for the widget the page is
	drawn in. GTK says that grouping has the focus whenever the window is activated, beside Firefox
	saying where the focus is; taken for the focus, it would move NVDA's out of the page and back, each
	time into a frame named as the window, whose title would be said again.

	The walk up stops at an accessible already found under the root: each level costs a round trip or
	two, and a control deep in a page has thirty of them, which took 20 ms from every Tab.
	"""
	c = client()
	walked = []
	current = node
	for _ in range(64):
		if current.key in _underRoot:
			break
		parent = c.parent(current)
		if parent is None:
			return False
		walked.append(current.key)
		if parent.path == ROOT_PATH or parent.key in _underRoot:
			break
		if c.role(parent) == "application":
			return True
		current = parent
	else:
		return False
	if len(_underRoot) > MAX_QUEUED:
		_underRoot.clear()
	_underRoot.update(walked)
	return False


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


#: Focused objects read at most to find the one that has the keyboard, among those Qt leaves focused.
FOCUS_CANDIDATES = 8
#: Items Qt marks focused whenever they are current in their container, focused or not: the current tab
#: of every tab bar and the current item of every combo box's hidden list.
_QT_ITEM_ROLES = frozenset({"page tab", "list item", "tree item", "table cell"})
_QT_MENU_ROLES = frozenset({"menu item", "check menu item", "radio menu item", "menu", "popup menu"})


def _focusIn(window: Node) -> Node | None:
	"""The focus inside window, if it can be read.

	:raise AppHung: Its application does not answer, so the focus is not known yet.
	"""
	c = client()
	try:
		if "focused" in c.states(window):
			return window
		if c.toolkit(window) == "Qt":
			focused = _qtFocusAmong(c.matchesWithState(window, "focused", FOCUS_CANDIDATES))
		else:
			focused = c.firstMatchWithState(window, "focused")
	except AppHung:
		raise
	except NodeGone:
		return None
	return activeDescendant(focused) or focused if focused else None


def _qtFocusAmong(candidates: list[Node]) -> Node | None:
	"""The object with the keyboard among the focused ones Qt lists in tree order: an item counts only
	while its container has the focus too, and then wins over it."""
	c = client()
	chosen = None
	inMenus = False
	for node in candidates:
		try:
			role = c.role(node)
			if role == "menu bar":
				# Only while its menus have the keyboard, which then goes to the newest item they focus,
				# whatever else keeps the focus behind them.
				inMenus = True
				chosen = node
			elif inMenus:
				if role in _QT_MENU_ROLES:
					chosen = node
			elif role in _QT_ITEM_ROLES:
				parent = c.parent(node)
				if parent is None or "focused" not in c.states(parent):
					continue
				if chosen is None or parent == chosen:
					chosen = node
			elif chosen is None:
				chosen = node
		except AppHung:
			raise
		except NodeGone:
			continue
	return chosen


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
		try:
			return _focusIn(window) if window else None
		except NodeGone:
			return None
	if isinstance(relation, tuple) and len(relation) == 2:
		x, y = relation
		c = client()
		apps = [app for app in applications() if not c.isHung(app.busName)]
		# The X server knows whose window is uppermost at the point, which AT-SPI does not: that
		# application's windows are tried alone first, and every window only when none of them holds
		# the point, as when the application runs in a sandbox that gives it process IDs of its own.
		owner = _processAtPoint(x, y)
		if owner:
			found = _hitTest(
				[w for app in apps if processIDOf(app) == owner for w in _safeChildren(app)], window, x, y
			)
			if found is not None:
				return found
		return _hitTest([w for app in apps for w in _safeChildren(app)], window, x, y)
	return None


def _processAtPoint(x: int, y: int) -> int | None:
	from linuxPlatform import pointer

	if not pointer.isAvailable():
		# Asked at every start, for the object under the pointer, and only an X session's server can say.
		return None
	try:
		return pointer.processAt(x, y)
	# No display, or a window that went as it was asked about: every window is tried instead.
	except Exception:  # noqa: BLE001
		log.debugWarning(f"Finding the window at {x}, {y} on the X display", exc_info=True)
		return None


def _hitTest(windows: list[Node], active: Node | None, x: int, y: int) -> Node | None:
	"""The deepest node at (x, y) in the first of windows that holds the point, the active one first."""
	c = client()
	if active is not None and active in windows:
		windows = [active] + [w for w in windows if w != active]
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


def _takesLegacyFocus(busName: str) -> bool:
	"""Whether busName's toolkit is known, without asking, to be one in LEGACY_FOCUS_TOOLKITS. Looking for
	an application's focus asks its toolkit, so a GTK program's is known once one of its windows has been
	activated. Asking for an application not asked yet would be a round trip in the middle of wx.Yield,
	which every event of Firefox's content process after it would then wait for."""
	toolkit = _clientModule.knownToolkit(busName)
	return toolkit is not None and toolkit.lower() in LEGACY_FOCUS_TOOLKITS


def _onEvent(eventType: str, source: Node | None, detail1: int, detail2: int, anyData) -> None:
	# Runs inside wx.Yield in the middle of whatever NVDA was doing: append and ask for a pump, nothing
	# more.
	if source is None or _exiting:
		return
	import core

	if core._hasShutdownBeenTriggered:
		return
	if eventType == LEGACY_FOCUS_EVENT and not _takesLegacyFocus(source.busName):
		return
	_clientModule.heardFrom(source.busName)
	now = time.monotonic()
	if eventType.startswith("object:state-changed:"):
		state = eventType[len("object:state-changed:") :]
		if _probesShown and state in _SHOWN_STATES:
			with _queueLock:
				_shown.append((source, now))
			if state == "visible":
				# Not queued below, so nothing else asks for the pump that looks at it.
				core.requestPump()
		# libatspi sends defunct itself whenever it frees its own proxy for a live object. Active is
		# kept for _asWindowEvent, and a Java menu item's disarming for _asJavaMenuReturn.
		if (
			state not in STATE_CHANGE_STATES
			and state not in ("focused", "showing", "active")
			and not (
				state == "armed"
				and not detail1
				and _clientModule.knownToolkit(source.busName) == JAVA_TOOLKIT
			)
		):
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
	elif eventType == "object:active-descendant-changed":
		if len(_descendantChanges) > MAX_QUEUED:
			_descendantChanges.clear()
			_quickChanges.clear()
		previous = _descendantChanges.get(source.key)
		gap = now - previous[0] if previous else DESCENDANT_FLOOD_SECONDS
		_descendantChanges[source.key] = (now, gap)
		quick = _quickChanges.get(source.key, 0) + 1 if gap < DESCENDANT_FLOOD_SECONDS else 0
		_quickChanges[source.key] = quick
		if quick >= FLOOD_QUICK_CHANGES:
			_floodingUntil[source.busName] = now + FLOOD_SECONDS
	elif eventType in WINDOW_ACTIVATION_EVENTS:
		global _lastWindowEventAt, _settleStartedAt
		_lastWindowEventAt = now
		if _settleStartedAt is None:
			_settleStartedAt = now
	_firstSeen.setdefault(source.key, now)
	with _queueLock:
		_queue.append((eventType, source, detail1, detail2, anyData))
	core.requestPump(
		immediate=eventType in _FOCUS_EVENTS
		or eventType == "object:active-descendant-changed"
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


def _sayThereIsNoBus() -> None:
	import ui

	ui.message(
		# Translators: Said as NVDA starts on Linux when it cannot connect to the accessibility bus,
		# through which it reads every program and its own windows; it can still speak and run its
		# commands. at-spi2-core is the package that provides the bus.
		_(
			"NVDA cannot reach the accessibility bus, so it cannot read any program or its own windows. "
			"Check that at-spi2-core is installed and its bus launcher is running, then restart NVDA.",
		),
	)


def initialize() -> None:
	global _listening, _exiting, _probesShown, _waylandRules
	_exiting = False
	import core

	from linuxPlatform import displayServer

	_probesShown = displayServer.isWayland()
	_waylandRules = displayServer.isWayland()
	if not client().hasBus:
		core.postNvdaStartup.register(_sayThereIsNoBus)
		return
	client().listen(EVENT_TYPES, _onEvent)
	_listening = True
	core.postNvdaStartup.register(_onPostNvdaStartup)


def terminate() -> None:
	global _listening, _desktopNode, _settleStartedAt, _probePumpAt, _rescansExtended
	import core

	core.postNvdaStartup.unregister(_onPostNvdaStartup)
	core.postNvdaStartup.unregister(_sayThereIsNoBus)
	if _listening:
		client().stopListening(EVENT_TYPES)
	_listening = False
	with _queueLock:
		_queue.clear()
		_shown.clear()
	_shownProbes.clear()
	_probePumpAt = None
	liveObjects.clear()
	_activeDescendants.clear()
	_firstSeen.clear()
	_retries.clear()
	_rescans.clear()
	_descendantChanges.clear()
	_quickChanges.clear()
	_floodingUntil.clear()
	_heldDescendantChanges.clear()
	_holdingSince.clear()
	_processIDs.clear()
	_roles.clear()
	_names.clear()
	liveRegions.forget()
	_applicationNames.clear()
	_topLevels.clear()
	_underRoot.clear()
	_focusWhileHung.clear()
	_activationWhileHung.clear()
	_switcherEventsWhileHung.clear()
	_switcherHandedBack.clear()
	_javaSubmenus.clear()
	_rescansExtended = False
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
		or eventType == LEGACY_FOCUS_EVENT
		or eventType == "window:activate"
		or (eventType == "object:active-descendant-changed")
	)


def _isMenuSelection(event) -> bool:
	"""Whether event is a GTK 3 menu item becoming selected, which is its focus. GTK 3 says that an item
	with a submenu has the focus only once the submenu shows, which it opens 225 ms after the item was
	selected, and not at all when the menu is rebuilt before then, as nm-applet's is while it looks for
	networks; the focus it says later is a repeat, which _processFocus drops. The toolkit is the one known
	without asking, as for _takesLegacyFocus: a GTK program's is known once a window of it is activated,
	as the window its menu opens in is."""
	eventType, source, detail1, _detail2, _anyData = event
	if eventType != "object:state-changed:selected" or not detail1:
		return False
	if _clientModule.knownToolkit(source.busName) != "gtk":
		return False
	try:
		return client().role(source) in _SELECTED_NOT_A_STATE_ROLES
	except (AppHung, NodeGone):
		return False


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
		# GTK 3 menu items and notebook tabs announce focus but carry only "selected", and the Java ATK
		# wrapper's menu items only _JAVA_HIGHLIGHT_STATES; a later loss of focus in the same batch has
		# already ruled stale events out.
		if not states & _JAVA_HIGHLIGHT_STATES or c.role(source) not in SELECTED_AS_FOCUS_ROLES:
			return None
		toolkit = c.toolkit(source)
		if toolkit != JAVA_TOOLKIT and ("selected" not in states or toolkit.lower() != "gtk"):
			return None
	role = c.role(source)
	if role == "root pane" and c.toolkit(source) == JAVA_TOOLKIT:
		# Swing gives the keyboard focus to the window's root pane while one of its menus is open, as
		# the menu opens, before any item in it is highlighted; that is no control to report.
		return None
	if role == "menu bar" and c.toolkit(source) == "Qt":
		# Qt focuses its menu bar after the menu it opens, whose first item has the focus without an
		# event of its own.
		inside = [n for n in c.matchesWithState(source, "focused", FOCUS_CANDIDATES) if n != source]
		return inside[-1] if inside else source
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


def _focusBefore(event, earlier: list) -> bool:
	"""Whether event is a window's activation with a focus event of the same application among the
	earlier ones of its batch, which is tried first."""
	return event[0] == "window:activate" and any(
		other[0] != "window:activate" and other[1].busName == event[1].busName for other in earlier
	)


def _keepsFocusIn(window: Node) -> bool:
	"""Whether a window activated with no focus found in it is to leave NVDA's focus where it is: inside
	that window, on an object that still says it is focused. Plasma's lock screen activated its frame
	twice just after its password field took the focus, and the frame took it from the field, which was
	not found in it.

	Only in a Wayland session, where that was seen: what an X session says stays as it is.
	"""
	import eventHandler

	if not _waylandRules:
		return False
	focus = getattr(eventHandler.lastQueuedFocusObject, "node", None)
	if focus is None or focus == window or not _hasFocusIn(window):
		return False
	try:
		return "focused" in client().states(focus)
	except NodeGone:
		return False


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
		if node is not None and _inShadowTree(node):
			log.debug(
				f"AT-SPI focus: {event[0]} on {source} dropped; it is in a tree beside its application's"
			)
			return False
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
	if event[0] == "window:activate" and node == source and _keepsFocusIn(source):
		log.debug(f"AT-SPI focus: {event[0]} on {source} leaves the focus inside it where it is")
		return True
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
	# What speech reads first, read now, in the same core cycle, whose cache speech then reads from: an
	# application that stops answering part way would otherwise be spoken with a role and no name.
	for prop in _FOCUS_PROPERTIES:
		try:
			getattr(obj, prop)
		except NotImplementedError:
			pass
	if client().isHung(node.busName):
		# Made while its application stopped answering, the object knows nothing of itself, and as the
		# focus would be spoken as an unknown, unavailable object.
		_deferWhileHung(event)
		return True
	if getattr(obj, "atspiRole", None) == "invalid":
		# libatspi saw it go between the event and now, as Caja's views do while they are rebuilt; the
		# focus is looked for again once, after the application has had a moment.
		log.debug(f"AT-SPI focus: {event[0]} on {source} went as it was read")
		if mayRetry and _foreground is not None:
			again = ("window:activate", _foreground, 0, 0, None)
			_retries.append((time.monotonic() + INVALID_RETRY_SECONDS, again))
		return False
	_focusWhileHung.clear()
	_activationWhileHung.clear()
	_noteSelectionFocus(node, obj)
	eventHandler.queueEvent("gainFocus", obj)
	return True


#: The newest focus event from each application that stopped answering while it was handled, with when,
#: handled again once the application answers.
_focusWhileHung: dict[str, tuple[tuple, float]] = {}
#: In a Wayland session, a window:activate from such an application that came while the focus event of
#: an object waited, handled after it: Plasma's lock screen, slow to answer as it started, activated
#: its frame after its password field took the focus, and the frame cannot say the field is in it.
_activationWhileHung: dict[str, tuple] = {}
#: Seconds after which such a focus event is given up.
HUNG_FOCUS_SECONDS = 10.0
#: Seconds before the focus is looked for again when the object a focus event named went as it was read.
INVALID_RETRY_SECONDS = 0.2
#: The properties of a new focus that speech reads, read before it is queued.
_FOCUS_PROPERTIES = ("role", "name", "states", "value", "description", "keyboardShortcut", "positionInfo")


def _deferWhileHung(event) -> None:
	import core

	log.debug(f"AT-SPI focus: {event[0]} on {event[1]} waits for its application to answer")
	busName = event[1].busName
	waiting = _focusWhileHung.get(busName)
	if (
		_waylandRules
		and waiting is not None
		and event[0] == "window:activate"
		and waiting[0][0] != "window:activate"
	):
		_activationWhileHung[busName] = event
	else:
		_activationWhileHung.pop(busName, None)
		_focusWhileHung[busName] = (event, waiting[1] if waiting else time.monotonic())
	core.requestPump()


#: In a Wayland session, the choices and renamings of a switcher in _SELECTING_SWITCHERS that could not
#: be read because it did not answer, by bus name, with since when, handled once it answers: GNOME
#: Shell, drawing its window switcher's thumbnails, did not answer as it selected the first window.
_switcherEventsWhileHung: dict[str, tuple[float, list]] = {}
#: How many such events are kept for each switcher, the newest.
MAX_SWITCHER_EVENTS_WHILE_HUNG = 20
#: Since when each switcher whose held events were handed back in this pump had been held: one that
#: answers the probe but not the next call is held again from then, or it would never be given up.
_switcherHandedBack: dict[str, float] = {}


def _holdWhileHung(event, now: float) -> None:
	import core

	busName = event[1].busName
	since, held = _switcherEventsWhileHung.get(busName, (_switcherHandedBack.get(busName, now), []))
	held.append(event)
	_switcherEventsWhileHung[busName] = (since, held[-MAX_SWITCHER_EVENTS_WHILE_HUNG:])
	core.requestPump()


def _switcherEventsAnswered(now: float) -> list:
	"""The held events of each switcher that answers again, in the order they came; those of one that
	has not answered for HUNG_FOCUS_SECONDS are given up."""
	_switcherHandedBack.clear()
	answered = []
	for busName, (since, held) in list(_switcherEventsWhileHung.items()):
		if now - since > HUNG_FOCUS_SECONDS:
			del _switcherEventsWhileHung[busName]
		elif not client().isHung(busName):
			del _switcherEventsWhileHung[busName]
			_switcherHandedBack[busName] = since
			answered += held
	return answered


def _resumeFocusIn(event, since: float) -> None:
	"""Take the focus an application has now, once it answers again after its own focus event no longer
	holds: a busy program moves its focus while NVDA waits, as Caja does while it fills a list view."""
	busName = event[1].busName
	if _foreground is None or not _foregroundActive or _foreground.busName != busName:
		return
	try:
		node = _focusIn(_foreground)
	except AppHung:
		# Still too busy to say; waited for again, within the time the first event had.
		_focusWhileHung[busName] = (event, since)
		import core

		core.requestPump()
		return
	except NodeGone:
		return
	if node is not None:
		log.debug(f"AT-SPI focus: {busName} answers again; its focus is now {node}")
		_processFocus(("object:state-changed:focused", node, 1, 0, None), mayRetry=False)


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
		except AppHung:
			if _waylandRules and _applicationName(source.busName) in _SELECTING_SWITCHERS:
				# Not gone: asked again once the switcher answers.
				raise
			role = roleOfVanished(source)
			if role is None:
				return []
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
	if (
		_waylandRules
		and _foregroundActive
		and _foreground is not None
		and _foreground.busName == node.busName
	):
		# The window activated in this batch, whose focus NVDA has not had yet: GNOME Shell selects its
		# switcher's first choice in the batch that activates the switcher.
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


def _isWithheldWhileLocked(node: Node) -> bool:
	"""Whether the session is locked and node's application is not the screen locker's."""
	from winAPI.sessionTracking import isLockScreenModeActive

	if not isLockScreenModeActive():
		return False
	from linuxPlatform import session

	return session.isProcessWithheld(processIDOf(node))


def _announce(event) -> None:
	"""Speak what an application announced, from any application: a chat program announces new
	messages while another window has the focus, as it would to Orca."""
	import queueHandler

	text = event[4]
	if isinstance(text, str) and text.strip():
		queueHandler.queueFunction(queueHandler.eventQueue, _speakAnnouncement, text.strip(), event[1])


def _speakAnnouncement(text: str, node: Node) -> None:
	import ui

	# Asked again as it is spoken, as a lock may have begun since it was queued.
	if _isWithheldWhileLocked(node):
		return
	ui.message(text)


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
	does; focus, window and text change events are all kept, apart from a container's active descendant
	changes, of which only the newest says where its focus went."""
	seen: set = set()
	kept = []
	for event in reversed(events):
		eventType = event[0]
		if (
			(_isFocusEvent(event) and eventType != "object:active-descendant-changed")
			or eventType in WINDOW_ACTIVATION_EVENTS
			or eventType.startswith("object:text-changed:")
			or eventType == ANNOUNCEMENT_EVENT
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


def _noteTopLevel(key: tuple[str, str], topLevel: bool) -> None:
	if len(_topLevels) > MAX_QUEUED:
		_topLevels.clear()
	_topLevels[key] = topLevel


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
		_noteTopLevel(source.key, topLevel)
	if not topLevel:
		return None
	# GTK can give a window back the focus, when a dialog over it closes, with only this.
	return ("window:activate" if detail1 else "window:deactivate", source, detail1, detail2, anyData)


#: The toolkits, by their ToolkitName in lower case, whose menus mark the item the arrows reach both
#: selected and focused and send only the selection: Chromium's own, which Electron programs share.
SELECTION_FOCUS_TOOLKITS = frozenset({"chromium"})
#: The menu item of such a toolkit that last took the focus, or that _asMenuItemFocus last made a focus
#: event of, by key.
_selectionFocusKey: tuple[str, str] | None = None


def _noteSelectionFocus(node: Node, obj) -> None:
	"""Remember node, about to become the focus, if it is a menu item of a toolkit in
	SELECTION_FOCUS_TOOLKITS: the item a submenu closes back to gets the focus by its menu's activation
	and not by a selection _asMenuItemFocus sees, and as the menu is taken down it no longer says it is a
	menu item."""
	global _selectionFocusKey
	toolkit = _clientModule.knownToolkit(node.busName)
	if (
		toolkit is not None
		and toolkit.lower() in SELECTION_FOCUS_TOOLKITS
		and getattr(obj, "atspiRole", None) in _SELECTED_NOT_A_STATE_ROLES
	):
		_selectionFocusKey = node.key
	else:
		_selectionFocusKey = None


def _asMenuItemFocus(event) -> tuple:
	"""event, or for a menu item of a toolkit in SELECTION_FOCUS_TOOLKITS becoming selected the focus event
	it stands for, which _focusCandidate checks against the item's focused state as for any other; and for
	that item losing its selection, its loss of focus. Chromium takes the selection from the item as it
	takes its menu down, when the item no longer says it is a menu item or available, which as a state
	change of the focus was said as "unavailable"."""
	global _selectionFocusKey
	eventType, source, detail1, detail2, anyData = event
	if eventType != "object:state-changed:selected":
		return event
	if not detail1:
		if source.key != _selectionFocusKey:
			return event
		_selectionFocusKey = None
		return ("object:state-changed:focused", source, 0, detail2, anyData)
	toolkit = _clientModule.knownToolkit(source.busName)
	if toolkit is None or toolkit.lower() not in SELECTION_FOCUS_TOOLKITS:
		return event
	try:
		if client().role(source) not in _SELECTED_NOT_A_STATE_ROLES:
			return event
	except NodeGone:
		return event
	_selectionFocusKey = source.key
	return ("object:state-changed:focused", source, 1, detail2, anyData)


def _asJavaMenuReturn(event) -> tuple | None:
	"""event, or for a Java menu item's disarming, which only that toolkit's is kept for, a focus on its
	menu if the item was NVDA's focus and its submenu closed with the menu still open: Swing highlights
	the menu again as Left closes the submenu, and the Java ATK wrapper says nothing of it. Without the
	menu as the focus, Right's focus on the item it had would also be dropped as a repeat.

	A JetBrains IDE hides the submenu only after the item says it lost the highlight, and says nothing of
	its menus being checked; its submenus also give their popup menu as their items' parent, which leads
	nowhere back to the menu, so the menu is noted in _javaSubmenus as the focus first reaches one of
	its items. When the menu is not found open at once, _returnToJavaMenu looks again
	JAVA_MENU_RETURN_SECONDS later."""
	eventType, source, detail1, _detail2, _anyData = event
	if eventType == "object:state-changed:focused" and detail1:
		_noteJavaSubmenu(source)
	if eventType != "object:state-changed:armed":
		return event
	import eventHandler

	if detail1 or getattr(eventHandler.lastQueuedFocusObject, "node", None) != source:
		return None
	c = client()
	try:
		menu = c.parent(source)
		role = c.role(menu) if menu is not None else None
		# The highlight may have moved on within the submenu, whose next item says so. The menu bar's
		# menus give their items as their children. Swing says a menu is checked while it is open, which
		# Escape's closing every menu has already undone.
		if role == "menu" and "showing" not in c.states(source) and {"checked", "showing"} <= c.states(menu):
			return ("object:state-changed:focused", menu, 1, 0, None)
		opener = menu if role == "menu" else _javaSubmenus.get(menu.key) if role == "popup menu" else None
	except NodeGone:
		return None
	if opener is not None:
		import core

		core.callLater(round(JAVA_MENU_RETURN_SECONDS * 1000), _returnToJavaMenu, source, opener)
	return None


def _returnToJavaMenu(item: Node, menu: Node) -> None:
	"""Queue a focus on menu, which _focusCandidate takes only while the menu is highlighted, if item, whose
	highlight went JAVA_MENU_RETURN_SECONDS ago, is still NVDA's focus and its submenu has closed."""
	import core
	import eventHandler

	if _exiting or not _listening or getattr(eventHandler.lastQueuedFocusObject, "node", None) != item:
		return
	try:
		if "showing" in client().states(item):
			# The highlight moved on within the submenu, whose next item says so.
			return
	except NodeGone:
		# Gone with its submenu.
		pass
	with _queueLock:
		_queue.append(("object:state-changed:focused", menu, 1, 0, None))
	core.requestPump()


def _noteJavaSubmenu(source: Node) -> None:
	"""Note the menu NVDA has the focus on as what source's popup menu was opened from, when the focus is
	going from a Java menu to an item of a popup menu."""
	import eventHandler

	focus = eventHandler.lastQueuedFocusObject
	menu = getattr(focus, "node", None)
	if (
		menu is None
		or menu == source
		or _clientModule.knownToolkit(source.busName) != JAVA_TOOLKIT
		or getattr(focus, "atspiRole", None) != "menu"
	):
		return
	c = client()
	try:
		popup = c.parent(source)
		if popup is None or c.role(popup) != "popup menu":
			return
	except NodeGone:
		return
	if len(_javaSubmenus) > MAX_QUEUED:
		_javaSubmenus.clear()
	_javaSubmenus[popup.key] = menu


#: Seconds after an application last answered slowly during which it counts as busy: a container of it
#: moved through quickly is not asked about at every step, and it is looked in last for the active window.
BUSY_SECONDS = 2.0
#: Seconds after a container last changed its active descendant within DESCENDANT_FLOOD_SECONDS of the
#: change before, FLOOD_QUICK_CHANGES times in a row, during which its application is taken to be
#: flooding.
FLOOD_SECONDS = 1.0
#: Changes in a row, each within DESCENDANT_FLOOD_SECONDS of the one before, that make a flood. One key
#: in a list view Caja has just built makes one such change: GTK names the row the cursor sat on and
#: then the row reached, or the one row twice, a millisecond apart.
FLOOD_QUICK_CHANGES = 2
#: Until when each application is taken to be flooding, by bus name.
_floodingUntil: dict[str, float] = {}
#: How many changes in a row each container's active descendant has made within
#: DESCENDANT_FLOOD_SECONDS of the one before, by the container's key.
_quickChanges: dict[tuple[str, str], int] = {}


def _isFloodBackground(event, now: float) -> bool:
	"""Whether event is about an object other than the focus or the foreground, from an application one
	of whose containers is being moved through faster than its items could be spoken. Reading the object
	an event names takes several round trips: Caja, moved through a large folder, sent events for every
	item passed while it answered each call a tenth of a second late, and reading them held the keyboard
	up for seconds."""
	eventType, node = event[0], event[1]
	if eventType in ("window:create", "object:state-changed:showing", ANNOUNCEMENT_EVENT):
		return False
	if _floodingUntil.get(node.busName, 0.0) <= now:
		return False
	import eventHandler

	return node != getattr(eventHandler.lastQueuedFocusObject, "node", None) and node != _foreground


def _isUnheldDeselection(event) -> bool:
	"""Whether event is an object losing its selected state that is not the focus and that NVDA holds no
	object for, as the item a list's selection moved from: nothing NVDA does with a state change of such
	an object is heard or shown, and making the object costs several round trips for every item a list
	is moved through."""
	if event[0] != "object:state-changed:selected" or event[2]:
		return False
	import api
	import eventHandler

	node = event[1]
	if node.key in liveObjects:
		return False
	held = (eventHandler.lastQueuedFocusObject, api.getFocusObject(), *api.getFocusAncestors())
	return all(getattr(obj, "node", None) != node for obj in held)


#: A container of a busy application whose active descendant changes again within this many seconds of
#: its last change is moved through faster than its items could be read: its change is acted on once it
#: has stood this long, or has waited DESCENDANT_HOLD_SECONDS.
DESCENDANT_FLOOD_SECONDS = 0.05
DESCENDANT_HOLD_SECONDS = 1.0
#: When each container last changed its active descendant, and how long after the change before it.
_descendantChanges: dict[tuple[str, str], tuple[float, float]] = {}
#: The newest active descendant change of each container moved through quickly, waiting for the
#: container to stand still, and since when each such container has been waited for.
_heldDescendantChanges: dict[tuple[str, str], tuple] = {}
_holdingSince: dict[tuple[str, str], float] = {}


def _holdWhileFlooding(event, now: float) -> bool:
	"""Whether event is an active descendant change to keep until its container stands still. Asking
	whether each change still holds costs round trips to a program that is busy sending them: Caja sent
	sixty a second, each found stale a tenth of a second later, and asking held the keyboard up."""
	if event[0] != "object:active-descendant-changed" or not _clientModule.answeredSlowlyWithin(
		event[1].busName,
		BUSY_SECONDS,
	):
		return False
	key = event[1].key
	at, gap = _descendantChanges.get(key, (0.0, DESCENDANT_FLOOD_SECONDS))
	since = _holdingSince.setdefault(key, now)
	if (
		gap >= DESCENDANT_FLOOD_SECONDS
		or now - at >= DESCENDANT_FLOOD_SECONDS
		or now - since >= DESCENDANT_HOLD_SECONDS
	):
		del _holdingSince[key]
		return False
	_heldDescendantChanges[key] = event
	return True


def _releasedDescendantChanges(now: float) -> list:
	"""The held active descendant changes whose containers have stood still, or that have waited long
	enough for one of a stream to be spoken."""
	released = []
	for key, event in list(_heldDescendantChanges.items()):
		at, _gap = _descendantChanges.get(key, (0.0, 0.0))
		if (
			now - at >= DESCENDANT_FLOOD_SECONDS
			or now - _holdingSince.get(key, 0.0) >= DESCENDANT_HOLD_SECONDS
		):
			del _heldDescendantChanges[key]
			_holdingSince.pop(key, None)
			released.append(event)
	return released


def _isSettling(now: float) -> bool:
	global _settleStartedAt
	if _settleStartedAt is None:
		return False
	if now - _lastWindowEventAt < SETTLE_QUIET_SECONDS and now - _settleStartedAt < SETTLE_MAX_SECONDS:
		return True
	_settleStartedAt = None
	return False


def _isTopLevel(node: Node) -> bool | None:
	"""Whether node is a top-level window; None while it cannot be read."""
	topLevel = _topLevels.get(node.key)
	if topLevel is None:
		try:
			parent = client().parent(node)
		except NodeGone:
			return None
		topLevel = parent is not None and parent.path == ROOT_PATH
		_noteTopLevel(node.key, topLevel)
	return topLevel


def _hasFocusIn(window: Node) -> bool:
	"""Whether the focus NVDA has is window or lies within it."""
	import eventHandler

	focus = getattr(eventHandler.lastQueuedFocusObject, "node", None)
	if focus is None or focus.busName != window.busName:
		return False
	try:
		return focus == window or _topLevelOf(focus) == window
	except NodeGone:
		return False


def _probeShown(events: list, now: float) -> list:
	"""The window:activate events that stand for activations nothing announced: of each top-level that
	showed and has had no window event since, read at SHOWN_PROBE_SECONDS after it showed, whether it
	says it is active.

	Another window announced active after it showed, and still the foreground, is believed over it.
	"""
	global _activatedAt, _probePumpAt
	import core

	with _queueLock:
		shown = list(_shown)
		_shown.clear()
	for node, at in shown:
		if _isTopLevel(node) is not False:
			_shownProbes[node.key] = (node, at, 0)
	for event in events:
		if event[0] in WINDOW_ACTIVATION_EVENTS:
			_shownProbes.pop(event[1].key, None)
			if event[0] == "window:activate":
				_activatedAt = now
	found = []
	for key, (node, shownAt, done) in list(_shownProbes.items()):
		if now < shownAt + SHOWN_PROBE_SECONDS[done]:
			continue
		done += 1
		topLevel = _isTopLevel(node)
		# Readable within the first reads if it is a window that is there at all; a flood of objects
		# shown as they go is not read for seconds.
		if topLevel is False or (topLevel is None and done >= UNREAD_SHOWN_PROBES):
			del _shownProbes[key]
			continue
		try:
			active = topLevel is True and "active" in client().states(node)
		except NodeGone:
			# GTK 4 names a window it maps before it can be read, and a busy program may answer later.
			active = False
		if active and ((_foreground == node and _foregroundActive) or _hasFocusIn(node)):
			del _shownProbes[key]
		elif active and not (_foregroundActive and _activatedAt > shownAt):
			del _shownProbes[key]
			log.debug(f"AT-SPI focus: {node} says it is active with no window event since it showed")
			_setForeground(node)
			_rescans.clear()
			found.append(("window:activate", node, 0, 0, None))
		elif done >= len(SHOWN_PROBE_SECONDS):
			del _shownProbes[key]
		else:
			_shownProbes[key] = (node, shownAt, done)
	if _probePumpAt is not None and now >= _probePumpAt:
		_probePumpAt = None
	if _shownProbes:
		due = min(shownAt + SHOWN_PROBE_SECONDS[done] for _node, shownAt, done in _shownProbes.values())
		if _probePumpAt is None or due < _probePumpAt:
			_probePumpAt = due
			core.callLater(max(1, round((due - now) * 1000)), core.requestPump)
	return found


def pumpAll() -> None:
	global _rescansExtended
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
		if not _foregroundActive:
			left = _foreground
			window = _scanForActiveWindow()
			if window is not None and _isSwitcher(window):
				_rescans[:] = [now + delay for delay in RESCAN_DELAYS_SECONDS]
			elif window is not None:
				_rescans.clear()
				_setForeground(window)
				# The window left can say it is active again for a moment as another program's grab of
				# the keyboard ends, as marco's does on Alt+Tab before it activates the window chosen; the
				# focus never left it.
				if window != left:
					due.append(("window:activate", window, 0, 0, None))
			elif not _rescans and _waylandRules and not _rescansExtended and _anyApplicationBusy():
				# KWin, building its switcher the first time it is asked for, answered nothing for two
				# seconds, and the desktop would have been said before the switcher.
				_rescansExtended = True
				_rescans[:] = [now + delay for delay in SHELL_RESCAN_DELAYS_SECONDS]
			elif not _rescans:
				_focusDesktop()
	with _queueLock:
		events = list(_queue)
		_queue.clear()
	if _switcherEventsWhileHung or _switcherHandedBack:
		events = _switcherEventsAnswered(now) + events
	for event in events:
		if event[0] == "object:active-descendant-changed":
			_heldDescendantChanges.pop(event[1].key, None)
			if isinstance(event[4], Node):
				# What a container says has its focus is no window, so its active state need not be asked
				# about: a list moved through quickly sends it for every item passed.
				_noteTopLevel(event[4].key, False)
	released = _releasedDescendantChanges(now)
	events = [converted for event in events if (converted := _asWindowEvent(event)) is not None]
	events = [_asMenuItemFocus(event) for event in events]
	events = [converted for event in events if (converted := _asJavaMenuReturn(event)) is not None]
	found = _probeShown(events, now) if _probesShown else []
	resumed = []
	for busName, (event, since) in list(_focusWhileHung.items()):
		if now - since > HUNG_FOCUS_SECONDS:
			del _focusWhileHung[busName]
			_activationWhileHung.pop(busName, None)
		elif not client().isHung(busName):
			del _focusWhileHung[busName]
			log.debug(f"AT-SPI focus: trying {event[0]} on {event[1]} again")
			resumed.append((event, since))
	if _retries or _rescans or _focusWhileHung or _switcherEventsWhileHung:
		core.requestPump()
	if not events and not due and not resumed and not released and not found:
		if _heldDescendantChanges:
			core.requestPump()
		return
	for event in due:
		_processFocus(event, mayRetry=False)
	for event in found:
		_processFocus(event)
	for event in released:
		_processFocus(event)
	for event, since in resumed:
		# Taken first: a focus that holds forgets every event still waiting.
		activation = _activationWhileHung.pop(event[1].busName, None)
		if not _processFocus(event, mayRetry=False):
			_resumeFocusIn(event, since)
		if activation is not None:
			log.debug(f"AT-SPI focus: trying {activation[0]} on {activation[1]} after it")
			_processFocus(activation, mayRetry=False)
	if len(_firstSeen) > MAX_QUEUED:
		for key in [k for k, at in _firstSeen.items() if now - at >= NEW_OBJECT_SECONDS]:
			del _firstSeen[key]
	# Active descendants are remembered whatever else happens to the events.
	for eventType, source, _d1, _d2, anyData in events:
		if eventType == "object:active-descendant-changed" and isinstance(anyData, Node):
			_activeDescendants[source.key] = anyData
		elif eventType == "object:property-change:accessible-role":
			liveObjects.pop(source.key, None)
	kept = [event for event in _coalesced(events) if not _holdWhileFlooding(event, now)]
	if _heldDescendantChanges:
		core.requestPump()
	# A focus event followed in the same batch by that object's loss of focus is stale.
	blurredAfter: set[int] = set()
	lostFocus: set = set()
	for index in range(len(kept) - 1, -1, -1):
		event = kept[index]
		if _isBlur(event):
			lostFocus.add(event[1].key)
		elif event[0] in _FOCUS_EVENTS and event[1].key in lostFocus:
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
	# A GTK 3 menu's selection is kept whatever came after it, as a focus is: GTK says that each item of a
	# submenu shows just after the item that opens it is selected. It is asked about here, and not as it
	# comes, as _asMenuItemFocus asks of a Chromium menu's: only an application's newest selection can be
	# its menu's focus, and a GTK list can select thousands of rows at once.
	menuSelections: set[int] = set()
	selecting: set[str] = set()
	for index in range(len(kept) - 1, -1, -1):
		event = kept[index]
		if (
			_isFocusEvent(event)
			or _isBlur(event)
			or event[0] in WINDOW_ACTIVATION_EVENTS
			or event[0] == ANNOUNCEMENT_EVENT
			or (_waylandRules and _isSwitcherChoice(event))
		):
			continue
		node = event[1]
		if event[0] == "object:state-changed:selected" and event[2] and node.busName not in selecting:
			selecting.add(node.busName)
			if _isMenuSelection(event):
				menuSelections.add(index)
				continue
		if node.key not in alwaysAllowed:
			perApplication[node.busName] += 1
			# Asked only past the limit, as it costs a round trip for each of the shell's showing events.
			if perApplication[node.busName] > MAX_EVENTS_PER_APPLICATION and not (
				_waylandRules and _isShellNotificationShown(event)
			):
				overLimit.add(index)

	def flushFocus() -> None:
		activations: list = []
		for index in range(len(focusEvents) - 1, -1, -1):
			focusEvent = focusEvents[index]
			if _waylandRules and _focusBefore(focusEvent, focusEvents[:index]):
				# Qt Quick's windows, KWin's switcher and Plasma's lock screen among them, cannot say what
				# in them has the focus; the object that said so before the window was activated can.
				activations.append(focusEvent)
				continue
			if _processFocus(focusEvent):
				break
		else:
			for focusEvent in activations:
				if _processFocus(focusEvent):
					break
		focusEvents.clear()

	for index, event in enumerate(kept):
		eventType = event[0]
		if eventType in WINDOW_ACTIVATION_EVENTS:
			_trackWindow(event)
		if _isBlur(event):
			continue
		if _isFocusEvent(event) or index in menuSelections:
			if index not in blurredAfter:
				focusEvents.append(event)
			continue
		if not windowsChanged:
			flushFocus()
		# What these speak goes past eventHandler, where NVDA withholds the rest while locked.
		withheld = _isWithheldWhileLocked(event[1])
		if not withheld and liveRegions.handle(event):
			continue
		if index in overLimit or _isFloodBackground(event, now) or _isUnheldDeselection(event):
			continue
		if eventType == ANNOUNCEMENT_EVENT:
			if not withheld:
				_announce(event)
			continue
		if event[1].key in blurred and eventType in (
			"object:text-caret-moved",
			"object:text-selection-changed",
		):
			continue
		try:
			nvdaEvents = _nvdaEvents(event)
		except AppHung:
			if _waylandRules and (
				_isSwitcherChoice(event)
				or (
					eventType == "object:property-change:accessible-name"
					and _applicationName(event[1].busName) in _SELECTING_SWITCHERS
				)
			):
				_holdWhileHung(event, now)
			continue
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
	flushFocus()
	if windowsChanged and not _foregroundActive:
		# A window deactivated and none activated after it: marco can give the focus back to a window
		# without that window saying so, as when a dialog closes, nor does it always say so at once. The
		# look asks every application, one of which may be busy, so it waits until what came with the
		# deactivation has been spoken: marco's switcher names the window Alt+Tab chose just after.
		delays = RESCAN_DELAYS_SECONDS
		if _waylandRules and _foreground is not None and _isSwitcher(_foreground):
			delays = SHELL_RESCAN_DELAYS_SECONDS
		_rescans[:] = [now + delay for delay in (SETTLE_QUIET_SECONDS, *delays)]
		_rescansExtended = False
		core.requestPump()
	log.debug(f"AT-SPI pump: {len(events)} raw events, {len(kept)} after coalescing")
