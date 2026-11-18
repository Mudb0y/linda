"""The Linux side of screenBitmap.ScreenBitmap, whose capture goes through GDI: the screen's pixels as
the X server gives them."""

from dataclasses import dataclass

from linuxPlatform import NoLinuxImplementation


@dataclass(frozen=True)
class RGBQuad:
	"""A pixel as screenBitmap's RGBQUAD names its colours."""

	rgbBlue: int
	rgbGreen: int
	rgbRed: int
	rgbReserved: int = 0


class ScreenBitmap:
	"""Takes the place of screenBitmap.ScreenBitmap.

	mouseHandler keeps one for the whole session, and audio coordinates read the brightness under the
	pointer from it on every mouse move when they are set to. The pixels are read from the root
	window, which a compositing window manager draws the screen on as well.
	"""

	def __init__(self, width: int, height: int) -> None:
		self.width = width
		self.height = height

	def captureImage(self, x: int, y: int, w: int, h: int) -> list[list[RGBQuad]]:
		"""The pixels of the screen from (x, y), w wide and h high, scaled to this bitmap's size as GDI's
		StretchBlt would, nearest pixel taken: rows of pixels, top first."""
		from Xlib import X

		from linuxPlatform import pointer

		with pointer._lock:
			connection = pointer._connection()
			screen = connection.screen()
			depth = screen.root_depth
			formats = {f.depth: f.bits_per_pixel for f in connection.display.info.pixmap_formats}
			if depth not in (24, 32) or formats.get(depth) != 32:
				raise NoLinuxImplementation("screen capture", f"the X screen's pixels are {depth} bits deep")
			image = screen.root.get_image(x, y, w, h, X.ZPixmap, 0xFFFFFFFF)
		data = image.data
		littleEndian = connection.display.info.image_byte_order == X.LSBFirst
		rows = []
		for row in range(self.height):
			sourceY = row * h // self.height
			pixels = []
			for column in range(self.width):
				offset = (sourceY * w + column * w // self.width) * 4
				b0, b1, b2, b3 = data[offset : offset + 4]
				# Each pixel is 0x00RRGGBB, stored in the server's byte order.
				red, green, blue = (b2, b1, b0) if littleEndian else (b1, b2, b3)
				pixels.append(RGBQuad(blue, green, red))
			rows.append(pixels)
		return rows
