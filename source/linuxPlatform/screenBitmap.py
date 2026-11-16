"""Screen capture for Linux, where screenBitmap's GDI capture has no counterpart yet."""

from typing import NoReturn

from linuxPlatform import NoLinuxImplementation


class ScreenBitmap:
	"""Takes the place of screenBitmap.ScreenBitmap, so that whatever reads the screen fails naming what is missing.

	mouseHandler keeps one for the whole session, and audio coordinates read the brightness under the
	pointer from it on every mouse move when they are set to.
	"""

	def __init__(self, width: int, height: int) -> None:
		self.width = width
		self.height = height

	def captureImage(self, x: int, y: int, w: int, h: int) -> NoReturn:
		raise NoLinuxImplementation("screen capture")
