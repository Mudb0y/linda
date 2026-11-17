"""App module for Chromium on Linux, and the base of those for programs built on it, Electron's among
them. On Windows these programs are served by NVDA's IAccessible2 support, and the module gives them
the default app module, as before.

Chromium joins the accessibility bus only when the desktop's accessibility status was on as it
started; then it shows its windows, but builds the accessible tree of a web page only once a client
asks for something that, by Chromium's reckoning, only a screen reader asks for: an object's
attributes or its relations. Asking for the attributes of each window that comes to the foreground
makes sure that happens whatever else NVDA happens to ask first.

A web page's landmarks are named for their kind, "main landmark" rather than "landmark", from the
ARIA roles Chromium gives with them, as NVDA does for Chromium's IAccessible2 objects on Windows.

A rich text editor, an editable element such as the one chat programs write messages in, keeps its
text in the paragraphs inside it, and its own text has an embedded object character for each of
them. Read as it is, the editor says that character and nothing of what was typed, and its caret is
nowhere. Its text is therefore read as one, its paragraphs' text with a line break after each, as
NVDA reads such an editor on Windows.
"""

import sys

import appModuleHandler

if sys.platform == "linux":
	import aria
	import controlTypes
	import textInfos
	from gi.repository import Atspi
	from linuxPlatform.atspi import handler
	from linuxPlatform.atspi.client import Node, NodeGone, call
	from locationHelper import RectLTWH
	from NVDAObjects.AtSpi import AtSpi
	from textInfos.offsets import OffsetsTextInfo, findEndOfLine, findStartOfLine

	#: What stands in an object's text for each child that is not text.
	EMBEDDED_OBJECT = "￼"
	#: How deep paragraphs, spans and links nest inside an editor before the rest is left out.
	MAX_DEPTH = 12
	#: How many characters of an editor are read; a message is far shorter.
	MAX_CHARACTERS = 100000

	class Run:
		"""Characters flatStart to flatEnd of an editor's text: those of node from nodeStart on, or,
		when separator, a line break after node's last character."""

		__slots__ = ("flatEnd", "flatStart", "node", "nodeStart", "separator")

		def __init__(self, flatStart: int, flatEnd: int, node: Node, nodeStart: int, separator: bool = False):
			self.flatStart = flatStart
			self.flatEnd = flatEnd
			self.node = node
			self.nodeStart = nodeStart
			self.separator = separator

		def nodeOffset(self, flatOffset: int) -> int:
			if self.separator:
				return self.nodeStart
			return self.nodeStart + flatOffset - self.flatStart

	def _embeddedChild(node: Node, offset: int, children: list[Node], index: int) -> Node | None:
		"""The child the embedded object character at offset in node's text stands for, by its
		hyperlink, or else as the index-th child that is not a run of text."""
		client = handler.client()
		try:
			link = call(node, Atspi.Hypertext.get_link_index, offset)
			if link >= 0:
				hyperlink = call(node, Atspi.Hypertext.get_link, link)
				if hyperlink is not None:
					child = client.node(call(node, lambda: hyperlink.get_object(0)))
					if child is not None:
						return child
		except NodeGone:
			pass
		try:
			others = [c for c in children if not (client.role(c) == "static" and client.childCount(c) == 0)]
		except NodeGone:
			return None
		return others[index] if index < len(others) else None

	def _isBlock(node: Node) -> bool:
		try:
			display = handler.client().attributes(node).get("display", "block")
		except NodeGone:
			return False
		return not display.startswith("inline")

	class EditorText:
		"""An editor's text read as one, and where each of its characters comes from."""

		def __init__(self, root: Node) -> None:
			self.root = root
			self.runs: list[Run] = []
			self.depths: dict[Node, int] = {}
			self._parts: list[str] = []
			self.length = 0
			#: The line break owed after a paragraph, given once something follows it, as (its node, its length).
			self._break: tuple[Node, int] | None = None
			self._add(root, 0)
			self.text = "".join(self._parts)

		def _append(self, text: str, node: Node, nodeStart: int, separator: bool = False) -> None:
			if not text:
				return
			if self._break is not None and not separator:
				breakNode, end = self._break
				self._break = None
				if not self._parts[-1].endswith("\n"):
					self._append("\n", breakNode, end, separator=True)
			self.runs.append(Run(self.length, self.length + len(text), node, nodeStart, separator))
			self._parts.append(text)
			self.length += len(text)

		def _add(self, node: Node, depth: int) -> None:
			client = handler.client()
			self.depths[node] = depth
			try:
				text = client.text(node)
			except NodeGone:
				return
			if EMBEDDED_OBJECT not in text or depth >= MAX_DEPTH:
				self._append(text, node, 0)
				return
			try:
				children = client.children(node)
			except NodeGone:
				children = []
			runStart = 0
			index = 0
			for offset, character in enumerate(text):
				if self.length >= MAX_CHARACTERS:
					break
				if character != EMBEDDED_OBJECT:
					continue
				self._append(text[runStart:offset], node, runStart)
				runStart = offset + 1
				child = _embeddedChild(node, offset, children, index)
				index += 1
				try:
					hasText = child is not None and "Text" in client.interfaces(child)
				except NodeGone:
					hasText = False
				if not hasText:
					self._append(EMBEDDED_OBJECT, node, offset)
					continue
				block = _isBlock(child)
				if block and self._break is None and self._parts and not self._parts[-1].endswith("\n"):
					self._append("\n", node, offset, separator=True)
				before = self.length
				self._add(child, depth + 1)
				if block and self.length > before:
					try:
						self._break = (child, client.characterCount(child))
					except NodeGone:
						self._break = (child, 0)
			self._append(text[runStart:], node, runStart)

		def locate(self, offset: int) -> tuple[Node, int]:
			"""The object and its own offset for the character at offset, or for the end."""
			for run in self.runs:
				if run.flatStart <= offset < run.flatEnd:
					return run.node, run.nodeOffset(offset)
			if self.runs:
				last = self.runs[-1]
				return last.node, last.nodeOffset(last.flatEnd)
			return self.root, 0

		def offsetOf(self, node: Node, nodeOffset: int) -> int | None:
			"""The offset in the whole text of node's own nodeOffset."""
			ending = None
			for run in self.runs:
				if run.node != node or run.separator:
					continue
				length = run.flatEnd - run.flatStart
				if run.nodeStart <= nodeOffset < run.nodeStart + length:
					return run.flatStart + nodeOffset - run.nodeStart
				if nodeOffset == run.nodeStart + length:
					ending = run.flatEnd
			if ending is not None:
				return ending
			return None

		def caret(self) -> int:
			"""Where the caret is in the whole text; the deepest object holding it says."""
			client = handler.client()
			holders = sorted(self.depths, key=self.depths.__getitem__, reverse=True)
			for node in holders:
				try:
					offset = client.caretOffset(node)
				except NodeGone:
					continue
				if offset < 0:
					continue
				found = self.offsetOf(node, offset)
				if found is not None:
					return found
			raise RuntimeError("no caret in this editor")

	class EditorTextInfo(OffsetsTextInfo):
		"""The text of a rich text editor, its paragraphs' text read as one. The whole text is read
		again for each TextInfo, which an editor of a message or two allows."""

		detectFormattingAfterCursorMaybeSlow = False
		encoding = None

		def _get_editorText(self) -> EditorText:
			editorText = self.__dict__.get("editorTextCache")
			if editorText is None:
				editorText = self.editorTextCache = EditorText(self.obj.node)
			return editorText

		def _getStoryText(self) -> str:
			return self.editorText.text

		def _getStoryLength(self) -> int:
			return self.editorText.length

		def _getTextRange(self, start: int, end: int) -> str:
			return self.editorText.text[start:end]

		def _getCaretOffset(self) -> int:
			return self.editorText.caret()

		def _setCaretOffset(self, offset: int) -> None:
			node, nodeOffset = self.editorText.locate(offset)
			try:
				handler.client().setCaretOffset(node, nodeOffset)
			except NodeGone:
				pass

		def _getSelectionOffsets(self) -> tuple[int, int]:
			caret = self._getCaretOffset()
			return (caret, caret)

		def _setSelectionOffsets(self, start: int, end: int) -> None:
			self._setCaretOffset(start)

		def _getLineOffsets(self, offset: int) -> tuple[int, int]:
			text = self.editorText.text
			return (findStartOfLine(text, offset), findEndOfLine(text, offset))

		def _getParagraphOffsets(self, offset: int) -> tuple[int, int]:
			return self._getLineOffsets(offset)

		def _getBoundingRectFromOffset(self, offset: int) -> RectLTWH:
			node, nodeOffset = self.editorText.locate(offset)
			try:
				extents = call(
					node,
					Atspi.Text.get_character_extents,
					nodeOffset,
					Atspi.CoordType.SCREEN,
				)
			except NodeGone as e:
				raise LookupError(str(e)) from None
			if extents.width <= 0 and extents.height <= 0:
				raise LookupError(f"no extents at offset {offset}")
			return RectLTWH(extents.x, extents.y, extents.width, extents.height)

		def _getFormatFieldAndOffsets(self, offset, formatConfig, calculateOffsets=True):
			return (textInfos.FormatField(), self._getLineOffsets(offset))

	class RichTextEditor(AtSpi):
		def _get_TextInfo(self):
			return EditorTextInfo

	class WebLandmark(AtSpi):
		def _get_landmark(self) -> str | None:
			xmlRoles = self.atspiAttributes.get("xml-roles", "").split(" ")
			return next((role for role in xmlRoles if role in aria.landmarkRoles), None)

	class AppModule(appModuleHandler.AppModule):
		def chooseNVDAObjectOverlayClasses(self, obj, clsList):
			if not isinstance(obj, AtSpi):
				return
			if obj.role == controlTypes.Role.LANDMARK:
				clsList.insert(0, WebLandmark)
			elif (
				obj.role == controlTypes.Role.EDITABLETEXT
				and controlTypes.State.MULTILINE in obj.states
				and "editable" in obj.atspiStates
				and "Text" in obj.atspiInterfaces
			):
				clsList.insert(0, RichTextEditor)

		def event_foreground(self, obj, nextHandler):
			if isinstance(obj, AtSpi):
				try:
					handler.client().attributes(obj.node)
				except NodeGone:
					pass
			nextHandler()

else:
	AppModule = appModuleHandler.AppModule
