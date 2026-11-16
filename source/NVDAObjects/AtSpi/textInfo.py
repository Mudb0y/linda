"""Text of AT-SPI accessibles, through their Text interface."""

from linuxPlatform.atspi import handler
from textInfos.offsets import OffsetsTextInfo
from textUtils.segFlag import CharSegFlag


class AtspiTextInfo(OffsetsTextInfo):
	"""Text through the AT-SPI Text interface, enough to speak a focused field. Offsets are Python str
	indices: GTK counts code points, Gecko UTF-16 units with a U+FEFF filler, and in both len(text)
	equals the character count."""

	encoding = None
	charSegFlag = CharSegFlag.NONE

	def _getStoryText(self) -> str:
		return handler.client().text(self.obj.node)

	def _getStoryLength(self) -> int:
		return handler.client().characterCount(self.obj.node)

	def _getTextRange(self, start: int, end: int) -> str:
		if start == end:
			return ""
		return handler.client().text(self.obj.node, start, end)

	def _getCaretOffset(self) -> int:
		offset = handler.client().caretOffset(self.obj.node)
		if offset < 0:
			raise RuntimeError("no caret")
		return offset

	def _setCaretOffset(self, offset: int) -> None:
		handler.client().setCaretOffset(self.obj.node, offset)

	def _getSelectionOffsets(self) -> tuple[int, int]:
		selections = handler.client().selections(self.obj.node)
		if selections:
			return selections[0]
		offset = self._getCaretOffset()
		return (offset, offset)

	def _getLineOffsets(self, offset: int) -> tuple[int, int]:
		start, end, _text = handler.client().stringAtOffset(self.obj.node, offset, "line")
		if start == end or not (start <= offset < end):
			return super()._getLineOffsets(offset)
		return (start, end)
