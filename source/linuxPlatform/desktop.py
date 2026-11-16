"""The object NVDA holds as the desktop until the AT-SPI backend provides the real one."""

import controlTypes
from NVDAObjects import NVDAObject


class Desktop(NVDAObject):
	"""The root of NVDA's object hierarchy, standing in for the AT-SPI desktop.

	core.main makes it the desktop, foreground, focus, navigator and mouse object, as it does the
	desktop window on Windows. Nothing can be asked of the applications on screen yet, so it has no
	parent, children or text, and the object with focus, in the foreground or under a point is not
	known: each of those answers None, which NVDA takes as nothing found.
	"""

	#: It belongs to no application, as upstream's placeholder objects belong to none.
	processID = None
	windowThreadID = 0
	role = controlTypes.Role.DESKTOPPANE

	def _isEqual(self, other: object) -> bool:
		# There is one desktop, whichever object stands for it.
		return True

	@staticmethod
	def objectWithFocus() -> None:
		return None

	@staticmethod
	def objectInForeground() -> None:
		return None

	@staticmethod
	def objectFromPoint(x: int, y: int) -> None:
		return None
