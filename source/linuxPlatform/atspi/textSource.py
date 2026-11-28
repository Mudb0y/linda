"""The questions AtspiTextInfo asks of an accessible's Text interface, each one round trip through the
AT-SPI client.

Offsets are the toolkit's own: GTK counts code points and Qt and Java UTF-16 units, which unitFor tells
apart, so that the TextInfo can tell NVDA which. Nothing here keeps anything between calls; a mirror
of the text can later stand behind the same methods without the TextInfo changing.

Two calls are never made: GetCharacterAtOffset, which hands out the real characters of a GTK 4
password field and the UTF-8 bytes of a Qt 5 one, and the toolkit's own words and sentences, of
which one call at the end of a Qt 5 line edit never returns and takes the application with it. The
words of Firefox alone are asked for, which its caret movement follows.
"""

from typing import Literal

import gi

gi.require_version("Atspi", "2.0")
from gi.repository import Atspi, GLib

from . import client as atspiClient
from .client import Node, NodeGone

Unit = Literal["codepoints", "utf16"]

#: The granularities stringAtOffset takes. Sentence is left out on purpose.
GRANULARITIES = {
	"char": Atspi.TextGranularity.CHAR,
	"word": Atspi.TextGranularity.WORD,
	"line": Atspi.TextGranularity.LINE,
	"paragraph": Atspi.TextGranularity.PARAGRAPH,
}


def unitFor(toolkit: str | None) -> Unit:
	"""What one offset counts in text from toolkit, as the application's ToolkitName gives it.

	Qt and Java, through the Java ATK wrapper, count UTF-16 units; GTK 3 ("gtk"), GTK 4 ("GTK") and wx
	count code points, and so does any toolkit not measured yet.
	"""
	if toolkit and (toolkit.lower().startswith("qt") or toolkit == "J2SE-access-bridge"):
		return "utf16"
	return "codepoints"


def _call(node: Node, func, *args):
	call = getattr(atspiClient, "call", None)
	if call is not None:
		return call(node, func, *args)
	try:
		return func(*args)
	except GLib.Error as e:
		raise NodeGone(e.message) from None


class TextSource:
	"""An accessible's Text interface. Every method raises NodeGone when the accessible is gone or its
	application does not answer."""

	def __init__(self, node: Node, unit: Unit, toolkit: str = "") -> None:
		self.node = node
		self.unit = unit
		#: The application's ToolkitName, as unitFor takes it.
		self.toolkit = toolkit

	def characterCount(self) -> int:
		return _call(self.node, Atspi.Text.get_character_count, self.node.ref)

	def text(self, start: int, end: int) -> str:
		return _call(self.node, Atspi.Text.get_text, self.node.ref, start, end) or ""

	def stringAtOffset(self, offset: int, granularity: str) -> tuple[int, int, str]:
		"""The unit of granularity ("char", "word", "line" or "paragraph") around offset, as (start, end,
		text)."""
		r = _call(
			self.node,
			Atspi.Text.get_string_at_offset,
			self.node.ref,
			offset,
			GRANULARITIES[granularity],
		)
		return (r.start_offset, r.end_offset, r.content or "")

	def caretOffset(self) -> int:
		return _call(self.node, Atspi.Text.get_caret_offset, self.node.ref)

	def setCaretOffset(self, offset: int) -> bool:
		return _call(self.node, Atspi.Text.set_caret_offset, self.node.ref, offset)

	def selections(self) -> list[tuple[int, int]]:
		count = _call(self.node, Atspi.Text.get_n_selections, self.node.ref)
		result = []
		for index in range(count):
			r = _call(self.node, Atspi.Text.get_selection, self.node.ref, index)
			result.append((r.start_offset, r.end_offset))
		return result

	def setSelection(self, start: int, end: int) -> bool:
		count = _call(self.node, Atspi.Text.get_n_selections, self.node.ref)
		if start == end:
			for index in reversed(range(count)):
				_call(self.node, Atspi.Text.remove_selection, self.node.ref, index)
			return self.setCaretOffset(start)
		ref = self.node.ref
		replace = (Atspi.Text.set_selection, ref, 0, start, end)
		if self._addSelectionCrashes():
			return _call(self.node, *replace)
		add = (Atspi.Text.add_selection, ref, start, end)
		# GTK 3 and the GTK 4 entry refuse to set a selection that does not exist yet and want one
		# added; the GTK 4 text view and Qt refuse to add one and want the first set.
		first, second = (replace, add) if count else (add, replace)
		try:
			if _call(self.node, *first):
				return True
		except NodeGone:
			pass
		return _call(self.node, *second)

	def _addSelectionCrashes(self) -> bool:
		"""Whether AddSelection would take the application down: GTK 4 asks the widget for its
		selections without wanting them, which VTE before 0.85 does not allow for and crashes on
		(VTE issue 2959, fixed in commit 56bad78b). AT-SPI does not say which VTE an application has,
		so no GTK 4 terminal is asked. Its SetSelection does not crash, though it selects nothing."""
		if self.toolkit != "GTK":
			return False
		try:
			return _call(self.node, Atspi.Accessible.get_role, self.node.ref) == Atspi.Role.TERMINAL
		except NodeGone:
			return True

	def characterExtents(self, offset: int) -> tuple[int, int, int, int]:
		"""The character's x, y, width and height, in the space the client gives the accessible's own in:
		the screen on X, and in a Wayland session its window's, as a WindowExtents."""
		if atspiClient.windowCoordinates():
			kind = atspiClient.client.coordinateType(self.node)
			r = atspiClient.firstPosition(
				self.node,
				atspiClient.TEXT_POSITIONS,
				lambda: _call(self.node, Atspi.Text.get_character_extents, self.node.ref, offset, kind),
				lambda r: r.width > 0 or r.height > 0,
			)
			return atspiClient.WindowExtents((r.x, r.y, r.width, r.height), self.node)
		r = _call(
			self.node,
			Atspi.Text.get_character_extents,
			self.node.ref,
			offset,
			Atspi.CoordType.SCREEN,
		)
		return (r.x, r.y, r.width, r.height)

	def offsetAtPoint(self, x: int, y: int) -> int:
		"""The offset of the character at x, y, taken in the space characterExtents answers in."""
		if atspiClient.windowCoordinates():
			kind = atspiClient.client.coordinateType(self.node)
			return atspiClient.firstPosition(
				self.node,
				atspiClient.TEXT_POSITIONS,
				lambda: _call(self.node, Atspi.Text.get_offset_at_point, self.node.ref, x, y, kind),
				lambda offset: offset >= 0,
			)
		return _call(
			self.node,
			Atspi.Text.get_offset_at_point,
			self.node.ref,
			x,
			y,
			Atspi.CoordType.SCREEN,
		)

	def attributeRun(self, offset: int) -> tuple[dict[str, str], int, int]:
		"""The attributes at offset, defaults included, with the run of text that has them."""
		attributes, start, end = _call(
			self.node,
			Atspi.Text.get_attribute_run,
			self.node.ref,
			offset,
			True,
		)
		return (dict(attributes or {}), start, end)

	def defaultAttributes(self) -> dict[str, str]:
		return dict(_call(self.node, Atspi.Text.get_default_attributes, self.node.ref) or {})


_toolkits: dict[str, str] = {}


def toolkitOf(node: Node) -> str:
	"""The ToolkitName of node's application, asked once per application."""
	toolkit = _toolkits.get(node.busName)
	if toolkit is None:
		try:
			# ToolkitName is a property of the application object, which Qt's widgets do not answer for.
			# Asked in one function of its own: the call wrapper would hand a libatspi function node's own
			# accessible as well as the application, which it does not take.
			toolkit = (
				_call(
					node,
					lambda: (Atspi.Accessible.get_application(node.ref) or node.ref).get_toolkit_name(),
				)
				or ""
			)
		except NodeGone:
			return ""
		_toolkits[node.busName] = toolkit
	return toolkit
