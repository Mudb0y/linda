"""The text of a rich text editor in Firefox, read as one across the objects it is made of.

Firefox exposes an editable element of a page, such as the prompt field of a chat site, as an object
whose own text has an embedded object character for each paragraph inside it, and each paragraph has
its own text, caret and selection. NVDA reads such an editor on Windows with MozillaCompoundTextInfo,
which walks the embedded objects through IAccessible2's hypertext; AT-SPI's Hypertext and Text carry
the same model, and this is that TextInfo over AT-SPI, kept close to NVDA's so that a fix there can be
carried over. What NVDA asks its in-process helper for, the deepest object holding the first or last
character, the caret or an end of the selection, is found here by following the embedded object
characters down, one call or two a level.

AT-SPI has nothing like IAccessible2's caret offset, which tells the insertion point at the end of a
wrapped line from the start of the next, so a caret put at the end of a wrapped line by End reads as
the start of the line after it.
"""

import textInfos
import textUtils
from compoundDocuments import CompoundTextInfo
from gi.repository import Atspi
from linuxPlatform.atspi import handler
from linuxPlatform.atspi.client import NodeGone, call
from logHandler import log
from textInfos import offsets

from NVDAObjects import NVDAObjectTextInfo

from .textInfo import AtspiTextInfo

#: How deep the objects of an editor nest before the walk down gives up: paragraphs, lists, their items,
#: links and formatting inside them.
MAX_DEPTH = 32


def _hasText(obj) -> bool:
	try:
		return "Text" in obj.atspiInterfaces
	except NodeGone:
		return False


def _getRawTextInfo(obj) -> type[offsets.OffsetsTextInfo]:
	return AtspiTextInfo if _hasText(obj) else NVDAObjectTextInfo


def _characterCount(obj) -> int:
	try:
		return handler.client().characterCount(obj.node)
	except NodeGone:
		return 0


def _getEmbedded(obj, offset: int):
	"""The object the embedded object character at offset in obj's text stands for, or None when
	there is none there."""
	if offset < 0 or "Hypertext" not in obj.atspiInterfaces:
		return None
	node = obj.node
	try:
		link = call(node, Atspi.Hypertext.get_link_index, offset)
		if link < 0:
			return None
		hyperlink = call(node, Atspi.Hypertext.get_link, link)
		# Firefox gives the last link for the offset at the very end of the text.
		if hyperlink is None or call(node, lambda: hyperlink.get_start_index()) != offset:
			return None
		child = handler.client().node(call(node, lambda: hyperlink.get_object(0)))
	except NodeGone:
		return None
	if child is None:
		return None
	return handler.getObject(child)


class AtspiCompoundTextInfo(CompoundTextInfo):
	"""MozillaCompoundTextInfo over AT-SPI."""

	def _getControlFieldForObject(self, obj, ignoreEditableText=True):
		controlField = super()._getControlFieldForObject(obj, ignoreEditableText=ignoreEditableText)
		if controlField is None:
			return None
		controlField["uniqueID"] = obj.node.key
		return controlField

	def __init__(self, obj, position):
		super().__init__(obj, position)
		if isinstance(position, AtspiCompoundTextInfo):
			self._start = position._start.copy()
			self._startObj = position._startObj
			if position._end is position._start:
				self._end = self._start
			else:
				self._end = position._end.copy()
			self._endObj = position._endObj
		elif position in (textInfos.POSITION_FIRST, textInfos.POSITION_LAST):
			self._start, self._startObj = self._findContentDescendant(obj, position)
			self._end = self._start
			self._endObj = self._startObj
		elif position == textInfos.POSITION_ALL:
			self._start, self._startObj = self._findContentDescendant(obj, textInfos.POSITION_FIRST)
			self._start.expand(textInfos.UNIT_STORY)
			self._end, self._endObj = self._findContentDescendant(obj, textInfos.POSITION_LAST)
			self._end.expand(textInfos.UNIT_STORY)
		elif position == textInfos.POSITION_CARET:
			try:
				caretTi, caretObj = self._findContentDescendant(obj, textInfos.POSITION_CARET)
			except LookupError:
				raise RuntimeError("No caret")
			if (
				caretObj != obj
				and caretObj.atspiAttributes.get("display") == "inline"
				and caretTi.compareEndPoints(
					self._makeRawTextInfo(caretObj, textInfos.POSITION_ALL),
					"startToEnd",
				)
				== 0
			):
				# The caret is at the end of an inline object; the character after it is in the object
				# after.
				try:
					caretTi, caretObj = self._findNextContent(caretTi, limitToInline=True)
				except LookupError:
					pass
			self._start = self._end = caretTi
			self._startObj = self._endObj = caretObj
		elif position == textInfos.POSITION_SELECTION:
			tempTi, tempObj = self._getSelectionBase()
			if tempTi.isCollapsed:
				self._start = self._end = tempTi
				self._startObj = self._endObj = tempObj
			else:
				self._start, self._startObj, self._end, self._endObj = self._findUnitEndpoints(
					tempTi,
					position,
				)
		elif hasattr(position, "node"):
			try:
				self._start, self._startObj = self._findContentDescendant(position, textInfos.POSITION_FIRST)
				self._end, self._endObj = self._findContentDescendant(position, textInfos.POSITION_LAST)
				self._end.move(textInfos.UNIT_CHARACTER, 1)
			except LookupError:
				self._start = self._end = self._getEmbedding(position)
				if self._start is None:
					raise ValueError(f"Object {position} not in document")
				self._startObj = self._endObj = self._start.obj
			self._normalizeStartAndEnd()
		else:
			raise NotImplementedError

	def _getSelectionBase(self):
		"""A TextInfo and its object somewhere within the selection, from which to find its ends."""
		try:
			ti, obj = self._findContentDescendant(self.obj, textInfos.POSITION_CARET)
		except LookupError:
			ti = None
		else:
			try:
				ti = self._makeRawTextInfo(obj, textInfos.POSITION_SELECTION)
			except RuntimeError:
				return ti, obj
			else:
				if not ti.isCollapsed:
					return ti, obj
		# Firefox leaves the caret's object without a selection when the caret is at an edge of it,
		# as after shift+Down at the end of a paragraph; the editor's own selection then covers the
		# paragraphs selected.
		rootTi = self._makeRawTextInfo(self.obj, textInfos.POSITION_SELECTION)
		if not rootTi.isCollapsed:
			return rootTi, self.obj
		if ti:
			return ti, obj
		raise RuntimeError("No selection or caret")

	def _makeRawTextInfo(self, obj, position) -> offsets.OffsetsTextInfo:
		return _getRawTextInfo(obj)(obj, position)

	def _getEmbedding(self, obj):
		"""A TextInfo on the embedded object character that stands for obj in its parent's text."""
		parent = obj.parent
		if not parent:
			return None
		try:
			hyperlink = call(obj.node, Atspi.Accessible.get_hyperlink)
			offset = call(obj.node, lambda: hyperlink.get_start_index()) if hyperlink is not None else -1
		except NodeGone:
			return None
		if offset < 0:
			return None
		info = self._makeRawTextInfo(parent, textInfos.POSITION_FIRST)
		info._startOffset = offset
		info._endOffset = offset + 1
		return info

	POSITION_SELECTION_START = 3
	POSITION_SELECTION_END = 4

	def _offsetFor(self, obj, position) -> int | None:
		"""Where in obj's own text position is, or None when obj does not hold it."""
		client = handler.client()
		node = obj.node
		if position == textInfos.POSITION_FIRST:
			return 0
		if position == textInfos.POSITION_LAST:
			return max(client.characterCount(node) - 1, 0)
		if position == textInfos.POSITION_CARET:
			offset = client.caretOffset(node)
			return offset if offset >= 0 else None
		# Firefox gives the paragraph the caret is at the edge of an empty selection where the caret is.
		selections = client.selections(node)
		if not selections:
			return None
		start = min(min(s, e) for s, e in selections)
		end = max(max(s, e) for s, e in selections)
		if position == self.POSITION_SELECTION_START:
			return start
		# The last character selected, as NVDA's helper gives it; the caller makes it the end.
		return end - 1

	def _findContentDescendant(self, obj, position):
		"""The deepest object with text that holds position, as a TextInfo collapsed there, and that
		object; as nvdaInProcUtils_IA2Text_findContentDescendant finds it on Windows.

		:raise LookupError: obj has no text, or does not hold position.
		"""
		if not _hasText(obj):
			raise LookupError("Object has no text")
		found = None
		for _depth in range(MAX_DEPTH):
			try:
				offset = self._offsetFor(obj, position)
			except NodeGone:
				offset = None
			if offset is None:
				break
			found = (obj, offset)
			child = _getEmbedded(obj, offset)
			if child is None or not _hasText(child):
				break
			obj = child
		if found is None:
			raise LookupError("No descendant holds this position")
		obj, offset = found
		if position == self.POSITION_SELECTION_END:
			offset += 1
		ti = self._makeRawTextInfo(obj, textInfos.POSITION_FIRST)
		ti._startOffset = ti._endOffset = offset
		return ti, obj

	def _iterTextWithEmbeddedObjects(self, ti, withFields, formatConfig=None):
		"""ti's text and fields, with the offset of each embedded object character in place of it."""
		if withFields:
			items = ti.getTextWithFields(formatConfig=formatConfig)
		else:
			items = [ti.text]
		offset = ti._startOffset
		for item in items:
			if not isinstance(item, str):
				yield item
				continue
			chunkStart = 0
			while chunkStart < len(item):
				try:
					chunkEnd = item.index(textUtils.OBJ_REPLACEMENT_CHAR, chunkStart)
				except ValueError:
					yield item[chunkStart:]
					break
				if chunkStart != chunkEnd:
					yield item[chunkStart:chunkEnd]
				yield offset + self._encodedLength(ti, item[:chunkEnd])
				chunkStart = chunkEnd + 1
			offset += self._encodedLength(ti, item)

	@staticmethod
	def _encodedLength(ti, text: str) -> int:
		if ti.encoding is None:
			return len(text)
		return textUtils.WideStringOffsetConverter(text).encodedStringLength

	def _iterRecursiveText(self, ti: offsets.OffsetsTextInfo, controlStack, formatConfig):
		if ti.obj == self._endObj:
			end = True
			ti.setEndPoint(self._end, "endToEnd")
		else:
			end = False

		for item in self._iterTextWithEmbeddedObjects(ti, controlStack is not None, formatConfig):
			if item is None:
				yield ""
			elif isinstance(item, str):
				yield item
			elif isinstance(item, int):
				embedded = _getEmbedded(ti.obj, item)
				if embedded is None:
					continue
				notText = _getRawTextInfo(embedded) is NVDAObjectTextInfo
				if controlStack is not None:
					controlField = self._getControlFieldForObject(embedded)
					controlStack.append(controlField)
					if controlField:
						if notText:
							controlField["content"] = embedded.name
						controlField["_startOfNode"] = True
						yield textInfos.FieldCommand("controlStart", controlField)
				if notText:
					# As NVDA does: a stand-in character, so that braille routing works and nothing is said.
					yield " "
				else:
					for subItem in self._iterRecursiveText(
						self._makeRawTextInfo(embedded, textInfos.POSITION_ALL),
						controlStack,
						formatConfig,
					):
						yield subItem
						if subItem is None:
							return
				if controlStack is not None and controlField:
					controlField["_endOfNode"] = True
					del controlStack[-1]
					yield textInfos.FieldCommand("controlEnd", None)
			else:
				yield item

		if end:
			yield None

	def _getText(self, withFields, formatConfig=None):
		fields = []
		if self.isCollapsed:
			return fields

		if withFields:
			controlStack = []
			rootObj = self.obj
			obj = self._startObj
			ti = self._start
			cannotBeStart = False
			while obj and obj != rootObj:
				field = self._getControlFieldForObject(obj)
				if field:
					if ti._startOffset == 0:
						if not cannotBeStart:
							field["_startOfNode"] = True
					else:
						cannotBeStart = True
					fields.insert(0, textInfos.FieldCommand("controlStart", field))
				controlStack.insert(0, field)
				ti = self._getEmbedding(obj)
				if not ti:
					log.debugWarning("No embedding while getting initial fields; the object is probably gone")
					return []
				obj = ti.obj
		else:
			controlStack = None

		fields += list(self._iterRecursiveText(self._start, controlStack, formatConfig))
		if not fields:
			return fields
		obj = self._startObj
		while fields[-1] is not None:
			# The end is not inside obj, so the text goes on after obj's place in its parent.
			if withFields:
				try:
					field = controlStack.pop()
				except IndexError:
					log.debugWarning("Walked up past the root; objects in the range are probably gone")
					return []
				if field:
					field["_endOfNode"] = True
					fields.append(textInfos.FieldCommand("controlEnd", None))
			ti = self._getEmbedding(obj)
			if not ti:
				log.debugWarning("No embedding while ascending for more text; the object is probably gone")
				return []
			obj = ti.obj
			if ti.move(textInfos.UNIT_OFFSET, 1) == 0:
				continue
			ti.setEndPoint(self._makeRawTextInfo(obj, textInfos.POSITION_ALL), "endToEnd")
			fields.extend(self._iterRecursiveText(ti, controlStack, formatConfig))
		del fields[-1]

		if withFields:
			obj = self._endObj
			ti = self._end
			while obj and obj != rootObj:
				field = controlStack.pop()
				if field:
					fields.append(textInfos.FieldCommand("controlEnd", None))
				if ti.compareEndPoints(self._makeRawTextInfo(obj, textInfos.POSITION_ALL), "endToEnd") == 0:
					if field:
						field["_endOfNode"] = True
				else:
					break
				ti = self._getEmbedding(obj)
				if not ti:
					break
				obj = ti.obj

		return fields

	def _get_text(self):
		return "".join(self._getText(False))

	def getTextWithFields(self, formatConfig: dict | None = None) -> textInfos.TextInfo.TextWithFieldsT:
		return self._getText(True, formatConfig)

	def _findUnitEndpoints(self, baseTi, unit, findStart=True, findEnd=True):
		start = startObj = end = endObj = None
		baseTi.collapse()
		obj = baseTi.obj

		while True:
			if unit == textInfos.POSITION_SELECTION:
				expandTi = self._makeRawTextInfo(obj, unit)
			else:
				expandTi = baseTi.copy()
				expandTi.expand(unit)
				if expandTi.isCollapsed:
					# Firefox gives an empty line at the very end of a paragraph's text.
					expandTi.expand(textInfos.UNIT_OFFSET)
			allTi = self._makeRawTextInfo(obj, textInfos.POSITION_ALL)

			if not start and findStart and expandTi.compareEndPoints(allTi, "startToStart") != 0:
				start = expandTi
				startObj = start.obj
				if unit == textInfos.POSITION_SELECTION:
					startDescPos = self.POSITION_SELECTION_START
				elif baseTi.compareEndPoints(expandTi, "startToStart") == 0:
					startDescPos = textInfos.POSITION_FIRST
				else:
					startDescPos = textInfos.POSITION_LAST

			if not end and findEnd and expandTi.compareEndPoints(allTi, "endToEnd") != 0:
				end = expandTi
				endObj = end.obj
				if unit == textInfos.POSITION_SELECTION:
					endDescPos = self.POSITION_SELECTION_END
				elif baseTi.compareEndPoints(expandTi, "endToEnd") == 0:
					endDescPos = textInfos.POSITION_LAST
				else:
					endDescPos = textInfos.POSITION_FIRST

			if (start or not findStart) and (end or not findEnd):
				break

			if obj == self.obj:
				embedTi = None
			else:
				embedTi = self._getEmbedding(obj)
			if not embedTi:
				if findStart and not start:
					start = expandTi
					startObj = start.obj
					if unit == textInfos.POSITION_SELECTION:
						startDescPos = self.POSITION_SELECTION_START
					elif baseTi.compareEndPoints(expandTi, "startToStart") == 0:
						startDescPos = textInfos.POSITION_FIRST
					else:
						startDescPos = textInfos.POSITION_LAST
				if findEnd and not end:
					end = expandTi
					endObj = end.obj
					if unit == textInfos.POSITION_SELECTION:
						endDescPos = self.POSITION_SELECTION_END
					elif baseTi.compareEndPoints(expandTi, "endToEnd") == 0:
						endDescPos = textInfos.POSITION_LAST
					else:
						endDescPos = textInfos.POSITION_FIRST
				break

			obj = embedTi.obj
			baseTi = embedTi

		if findStart:
			embedded = _getEmbedded(startObj, start._startOffset)
			if embedded:
				try:
					start, startObj = self._findContentDescendant(embedded, startDescPos)
				except LookupError:
					pass
				else:
					if unit == textInfos.POSITION_SELECTION:
						start = self._makeRawTextInfo(startObj, unit)
					else:
						start.expand(unit)
			if not findEnd:
				return start, startObj
		if findEnd:
			embedded = _getEmbedded(endObj, end._endOffset - 1)
			if embedded:
				try:
					end, endObj = self._findContentDescendant(embedded, endDescPos)
				except LookupError:
					pass
				else:
					if unit == textInfos.POSITION_SELECTION:
						end = self._makeRawTextInfo(endObj, unit)
					else:
						end.expand(unit)
			if not findStart:
				return end, endObj

		return start, startObj, end, endObj

	def expand(self, unit):
		if unit in (textInfos.UNIT_CHARACTER, textInfos.UNIT_OFFSET):
			self._end = self._start
			self._endObj = self._startObj
			self._start.expand(unit)
			return

		start, startObj, end, endObj = self._findUnitEndpoints(self._start, unit)
		if startObj == endObj:
			end = start
			endObj = startObj

		self._start = start
		self._startObj = startObj
		self._end = end
		self._endObj = endObj

	def _findNextContent(self, origin, moveBack=False, limitToInline=False):
		if isinstance(origin, textInfos.TextInfo):
			ti = origin
			obj = ti.obj
		else:
			ti = None
			obj = origin

		direction = -1 if moveBack else 1
		while True:
			if ti and ti.move(textInfos.UNIT_OFFSET, direction) != 0:
				break
			if obj == self.obj:
				raise LookupError
			if limitToInline and obj.atspiAttributes.get("display") != "inline":
				raise LookupError
			ti = self._getEmbedding(obj)
			if not ti:
				raise LookupError
			obj = ti.obj

		embedded = _getEmbedded(obj, ti._startOffset)
		if embedded:
			try:
				return self._findContentDescendant(
					embedded,
					textInfos.POSITION_LAST if moveBack else textInfos.POSITION_FIRST,
				)
			except LookupError:
				pass
		return ti, obj

	def move(
		self,
		unit: str,
		direction: int,
		endPoint: str | None = None,
	) -> int:
		if direction == 0:
			return 0
		if _characterCount(self.obj) == 0:
			return 0

		if not endPoint or endPoint == "start":
			moveTi = self._start
			moveObj = self._startObj
			if endPoint and moveTi is self._end:
				moveTi = moveTi.copy()
			moveTi.collapse()
		elif endPoint == "end":
			moveTi = self._end
			moveObj = self._endObj
			if endPoint and moveTi is self._start:
				moveTi = moveTi.copy()
			moveTi.collapse(end=True)
			if (
				moveTi.compareEndPoints(self._makeRawTextInfo(moveObj, textInfos.POSITION_ALL), "endToEnd")
				== 0
			):
				try:
					moveTi, moveObj = self._findNextContent(moveObj)
				except LookupError:
					return 0

		remainingMovement = direction
		moveBack = direction < 0
		while remainingMovement != 0:
			if moveBack:
				try:
					moveTi, moveObj = self._findNextContent(moveTi, moveBack=True)
				except LookupError:
					break

			moveTi, moveObj = self._findUnitEndpoints(moveTi, unit, findStart=moveBack, findEnd=not moveBack)

			if moveBack:
				moveTi.collapse()
			else:
				moveTi.collapse(end=True)
				if (
					moveTi.compareEndPoints(
						self._makeRawTextInfo(moveObj, textInfos.POSITION_ALL),
						"endToEnd",
					)
					== 0
				):
					if remainingMovement == 1 and endPoint == "end":
						# Not the start of the next object, which would make bogus control fields.
						remainingMovement -= 1
						break
					try:
						moveTi, moveObj = self._findNextContent(moveObj)
					except LookupError:
						if endPoint == "end" and moveTi.compareEndPoints(self._end, "endToEnd") != 0:
							# Moving the end to the end of the document counts as a move.
							remainingMovement -= 1
						break
				else:
					embedded = _getEmbedded(moveObj, moveTi._startOffset)
					if embedded:
						try:
							moveTi, moveObj = self._findContentDescendant(embedded, textInfos.POSITION_FIRST)
						except LookupError:
							pass

			remainingMovement -= -1 if moveBack else 1

		if not endPoint or endPoint == "start":
			self._start = moveTi
			self._startObj = moveObj
		if not endPoint or endPoint == "end":
			self._end = moveTi
			self._endObj = moveObj
		self._normalizeStartAndEnd()
		return direction - remainingMovement

	def _getAncestors(self, ti, obj):
		data = []
		while True:
			data.insert(0, (ti, obj))
			if obj == self.obj:
				break
			ti = self._getEmbedding(obj)
			if not ti:
				break
			obj = ti.obj
		return data

	def compareEndPoints(self, other, which):
		if which in ("startToStart", "startToEnd"):
			selfTi = self._start
			selfObj = self._startObj
		else:
			selfTi = self._end
			selfObj = self._endObj
		if which in ("startToStart", "endToStart"):
			otherTi = other._start
			otherObj = other._startObj
		else:
			otherTi = other._end
			otherObj = other._endObj

		if selfObj == otherObj:
			return selfTi.compareEndPoints(otherTi, which)

		selfAncs = self._getAncestors(selfTi, selfObj)
		otherAncs = self._getAncestors(otherTi, otherObj)
		maxAncIndex = min(len(selfAncs), len(otherAncs)) - 1
		for (selfAncTi, selfAncObj), (otherAncTi, otherAncObj) in zip(
			selfAncs[maxAncIndex::-1],
			otherAncs[maxAncIndex::-1],
		):
			if selfAncObj == otherAncObj:
				break
		else:
			return 1
		return selfAncTi.compareEndPoints(otherAncTi, which)

	def _get_NVDAObjectAtStart(self):
		embedded = _getEmbedded(self._startObj, self._start._startOffset)
		if embedded:
			return embedded
		return self._startObj

	def _get_boundingRects(self):
		rects = []
		copy = self.copy()
		obj = copy._startObj
		ti = copy._start
		while obj:
			try:
				rects.extend(ti.boundingRects)
			except (NotImplementedError, LookupError):
				pass
			if obj == copy._endObj:
				break
			try:
				ti, obj = copy._findNextContent(ti)
			except LookupError:
				break
			if obj == copy._endObj:
				ti = copy._end
		return rects

	def updateCaret(self):
		# A paragraph is not focusable: asked to take the focus, Firefox would give it to the document
		# and take it from the editor. Setting the caret inside the editor keeps the focus there.
		self._start.updateCaret()

	def updateSelection(self):
		if self._startObj == self._endObj:
			self._start.updateSelection()
			return
		# A selection across objects cannot be set through one object's Text: the editor's own covers
		# whole paragraphs. The caret goes to its start instead.
		self._start.updateCaret()
