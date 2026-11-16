"""The one place NVDA's Linux object layer reads AT-SPI from.

Everything above this module asks for facts about a Node and never touches libatspi. A Node is an
accessible's identity, (unique bus name, object path), with a reference this client uses to reach it.
This client answers each question with libatspi through PyGObject, one D-Bus round trip at a time;
the engine's mirror can stand behind the same calls later and answer most of them from memory.
"""

import dataclasses
from collections.abc import Callable

import gi

gi.require_version("Atspi", "2.0")
from gi.repository import Atspi, GLib

#: The registry's path for the desktop, which every application root shares with it.
ROOT_PATH = "/org/a11y/atspi/accessible/root"


class NodeGone(LookupError):
	"""The accessible no longer exists, or its application does not answer."""


@dataclasses.dataclass(frozen=True, eq=False)
class Node:
	busName: str
	path: str
	#: libatspi's proxy; the engine would hold its own handle here.
	ref: object = dataclasses.field(repr=False, compare=False)

	@property
	def key(self) -> tuple[str, str]:
		return (self.busName, self.path)

	def __eq__(self, other: object) -> bool:
		return isinstance(other, Node) and self.key == other.key

	def __hash__(self) -> int:
		return hash(self.key)


@dataclasses.dataclass
class Relation:
	type: str
	targets: list[Node]


#: Round trips made through _call, for measurement.
callCount = 0


def _call(func, *args):
	global callCount
	callCount += 1
	try:
		return func(*args)
	except GLib.Error as e:
		raise NodeGone(e.message) from None


class Client:
	"""Questions about nodes, each one round trip to the application in this implementation."""

	def __init__(self) -> None:
		Atspi.init()
		self._listener = None

	def node(self, acc) -> Node | None:
		if acc is None:
			return None
		try:
			return Node(acc.app.bus_name if acc.app else "", acc.path, acc)
		except AttributeError:
			return None

	def desktop(self) -> Node:
		return self.node(Atspi.get_desktop(0))

	def applications(self) -> list[Node]:
		desktop = Atspi.get_desktop(0)
		apps = []
		for i in range(_call(desktop.get_child_count)):
			app = self.node(_call(desktop.get_child_at_index, i))
			if app:
				apps.append(app)
		return apps

	def role(self, node: Node) -> str:
		return _call(Atspi.Accessible.get_role_name, node.ref)

	def name(self, node: Node) -> str:
		return _call(Atspi.Accessible.get_name, node.ref) or ""

	def description(self, node: Node) -> str:
		return _call(Atspi.Accessible.get_description, node.ref) or ""

	def states(self, node: Node) -> frozenset[str]:
		stateSet = _call(Atspi.Accessible.get_state_set, node.ref)
		return frozenset(s.value_nick for s in stateSet.get_states())

	def attributes(self, node: Node) -> dict[str, str]:
		return dict(_call(Atspi.Accessible.get_attributes, node.ref) or {})

	def interfaces(self, node: Node) -> frozenset[str]:
		return frozenset(_call(Atspi.Accessible.get_interfaces, node.ref))

	def processID(self, node: Node) -> int:
		return _call(Atspi.Accessible.get_process_id, node.ref)

	def parent(self, node: Node) -> Node | None:
		return self.node(_call(Atspi.Accessible.get_parent, node.ref))

	def childCount(self, node: Node) -> int:
		return _call(Atspi.Accessible.get_child_count, node.ref)

	def childAt(self, node: Node, index: int) -> Node | None:
		return self.node(_call(Atspi.Accessible.get_child_at_index, node.ref, index))

	def children(self, node: Node) -> list[Node]:
		count = self.childCount(node)
		return [c for c in (self.childAt(node, i) for i in range(count)) if c]

	def indexInParent(self, node: Node) -> int:
		return _call(Atspi.Accessible.get_index_in_parent, node.ref)

	def relations(self, node: Node) -> list[Relation]:
		result = []
		for rel in _call(Atspi.Accessible.get_relation_set, node.ref) or []:
			targets = [self.node(rel.get_target(i)) for i in range(rel.get_n_targets())]
			result.append(Relation(rel.get_relation_type().value_nick, [t for t in targets if t]))
		return result

	def extents(self, node: Node) -> tuple[int, int, int, int] | None:
		rect = _call(Atspi.Component.get_extents, node.ref, Atspi.CoordType.SCREEN)
		if rect.width <= 0 and rect.height <= 0:
			return None
		return (rect.x, rect.y, rect.width, rect.height)

	def accessibleAtPoint(self, node: Node, x: int, y: int) -> Node | None:
		return self.node(
			_call(Atspi.Component.get_accessible_at_point, node.ref, x, y, Atspi.CoordType.SCREEN),
		)

	def grabFocus(self, node: Node) -> bool:
		return _call(Atspi.Component.grab_focus, node.ref)

	def value(self, node: Node) -> tuple[float, float, float, str]:
		ref = node.ref
		return (
			_call(Atspi.Value.get_current_value, ref),
			_call(Atspi.Value.get_minimum_value, ref),
			_call(Atspi.Value.get_maximum_value, ref),
			_call(Atspi.Value.get_text, ref) or "",
		)

	def actions(self, node: Node) -> list[tuple[str, str]]:
		ref = node.ref
		return [
			(_call(Atspi.Action.get_action_name, ref, i), _call(Atspi.Action.get_key_binding, ref, i))
			for i in range(_call(Atspi.Action.get_n_actions, ref))
		]

	def doAction(self, node: Node, index: int) -> bool:
		return _call(Atspi.Action.do_action, node.ref, index)

	def selectedChild(self, node: Node, index: int = 0) -> Node | None:
		return self.node(_call(Atspi.Selection.get_selected_child, node.ref, index))

	def selectedCount(self, node: Node) -> int:
		return _call(Atspi.Selection.get_n_selected_children, node.ref)

	def tableSize(self, node: Node) -> tuple[int, int]:
		return (_call(Atspi.Table.get_n_rows, node.ref), _call(Atspi.Table.get_n_columns, node.ref))

	def cellPosition(self, node: Node) -> tuple[int, int]:
		_ok, row, column = _call(Atspi.TableCell.get_position, node.ref)
		return (row, column)

	def cellTable(self, node: Node) -> Node | None:
		return self.node(_call(Atspi.TableCell.get_table, node.ref))

	def columnHeaderNames(self, node: Node) -> list[str]:
		return [h.get_name() or "" for h in _call(Atspi.TableCell.get_column_header_cells, node.ref) or []]

	def text(self, node: Node, start: int = 0, end: int = -1) -> str:
		return _call(Atspi.Text.get_text, node.ref, start, end) or ""

	def characterCount(self, node: Node) -> int:
		return _call(Atspi.Text.get_character_count, node.ref)

	def caretOffset(self, node: Node) -> int:
		return _call(Atspi.Text.get_caret_offset, node.ref)

	def setCaretOffset(self, node: Node, offset: int) -> bool:
		return _call(Atspi.Text.set_caret_offset, node.ref, offset)

	def selections(self, node: Node) -> list[tuple[int, int]]:
		count = _call(Atspi.Text.get_n_selections, node.ref)
		result = []
		for i in range(count):
			r = _call(Atspi.Text.get_selection, node.ref, i)
			result.append((r.start_offset, r.end_offset))
		return result

	def stringAtOffset(self, node: Node, offset: int, unit: str) -> tuple[int, int, str]:
		granularity = {
			"char": Atspi.TextGranularity.CHAR,
			"word": Atspi.TextGranularity.WORD,
			"sentence": Atspi.TextGranularity.SENTENCE,
			"line": Atspi.TextGranularity.LINE,
			"paragraph": Atspi.TextGranularity.PARAGRAPH,
		}[unit]
		r = _call(Atspi.Text.get_string_at_offset, node.ref, offset, granularity)
		return (r.start_offset, r.end_offset, r.content or "")

	def firstMatchWithRole(self, node: Node, roleName: str) -> Node | None:
		"""The first descendant of node, in tree order, with the role libatspi names roleName."""
		role = next(
			r for r in (Atspi.Role(i) for i in range(Atspi.ROLE_COUNT)) if Atspi.role_get_name(r) == roleName
		)
		rule = Atspi.MatchRule.new(
			None,
			Atspi.CollectionMatchType.NONE,
			None,
			Atspi.CollectionMatchType.NONE,
			[role],
			Atspi.CollectionMatchType.ANY,
			None,
			Atspi.CollectionMatchType.NONE,
			False,
		)
		matches = _call(
			Atspi.Collection.get_matches,
			node.ref,
			rule,
			Atspi.CollectionSortOrder.CANONICAL,
			1,
			True,
		)
		return self.node(matches[0]) if matches else None

	def firstMatchWithState(self, node: Node, state: str) -> Node | None:
		"""The first descendant of node, in tree order, that has state; through Collection."""
		# Showing too: Qt leaves a closed combo box's current item focused.
		stateSet = Atspi.StateSet.new(
			[getattr(Atspi.StateType, state.upper().replace("-", "_")), Atspi.StateType.SHOWING],
		)
		rule = Atspi.MatchRule.new(
			stateSet,
			Atspi.CollectionMatchType.ALL,
			None,
			Atspi.CollectionMatchType.NONE,
			None,
			Atspi.CollectionMatchType.NONE,
			None,
			Atspi.CollectionMatchType.NONE,
			False,
		)
		matches = _call(
			Atspi.Collection.get_matches,
			node.ref,
			rule,
			Atspi.CollectionSortOrder.CANONICAL,
			1,
			True,
		)
		return self.node(matches[0]) if matches else None

	def listen(self, eventTypes: list[str], callback: Callable[..., None]) -> None:
		"""Call callback(type, node, detail1, detail2, anyData) for each event, on the main thread,
		whenever GLib's default context runs, which is inside wx's main loop and inside wx.Yield."""

		def onEvent(event) -> None:
			anyData = event.any_data
			if isinstance(anyData, Atspi.Accessible):
				anyData = self.node(anyData)
			callback(event.type, self.node(event.source), event.detail1, event.detail2, anyData)

		self._listener = Atspi.EventListener.new(onEvent)
		for eventType in eventTypes:
			self._listener.register(eventType)

	def stopListening(self, eventTypes: list[str]) -> None:
		if self._listener:
			for eventType in eventTypes:
				self._listener.deregister(eventType)
			self._listener = None
