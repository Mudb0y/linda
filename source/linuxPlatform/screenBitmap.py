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
		"""The pixels of the screen from (x, y), w wide and h high, scaled to this bitmap's size: rows of
		pixels, top first, each the average of the screen's pixels it covers, as audio coordinates' blur
		factor means the area round the pointer to count."""
		from Xlib import X

		from linuxPlatform import pointer

		with pointer._lock:
			connection = pointer._connection()
			screen = connection.screen()
			depth = screen.root_depth
			formats = {f.depth: f.bits_per_pixel for f in connection.display.info.pixmap_formats}
			if depth not in (24, 32) or formats.get(depth) != 32:
				raise NoLinuxImplementation("screen capture", f"the X screen's pixels are {depth} bits deep")
			# X refuses an area reaching past the screen, which GDI reads as black; the area is cut to
			# the screen, the pointer being on it.
			right = min(x + w, screen.width_in_pixels)
			bottom = min(y + h, screen.height_in_pixels)
			x, y = max(x, 0), max(y, 0)
			w, h = right - x, bottom - y
			if w <= 0 or h <= 0:
				return [[RGBQuad(0, 0, 0)] * self.width for _ in range(self.height)]
			image = screen.root.get_image(x, y, w, h, X.ZPixmap, 0xFFFFFFFF)
		data = image.data
		littleEndian = connection.display.info.image_byte_order == X.LSBFirst

		def pixel(sourceX: int, sourceY: int) -> tuple[int, int, int]:
			offset = (sourceY * w + sourceX) * 4
			b0, b1, b2, b3 = data[offset : offset + 4]
			# Each pixel is 0x00RRGGBB, stored in the server's byte order.
			return (b2, b1, b0) if littleEndian else (b1, b2, b3)

		rows = []
		for row in range(self.height):
			ys = range(row * h // self.height, max(row * h // self.height + 1, (row + 1) * h // self.height))
			pixels = []
			for column in range(self.width):
				xs = range(
					column * w // self.width,
					max(column * w // self.width + 1, (column + 1) * w // self.width),
				)
				covered = [pixel(sx, sy) for sy in ys for sx in xs]
				red, green, blue = (sum(channel) // len(covered) for channel in zip(*covered))
				pixels.append(RGBQuad(blue, green, red))
			rows.append(pixels)
		return rows
