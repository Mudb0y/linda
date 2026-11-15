"""UTF-16 text for C functions, on a platform whose wchar_t is UTF-32.

NVDA counts text offsets in UTF-16 code units. On Windows, ctypes.create_unicode_buffer makes a buffer
whose length, indices and slices count those units, and upstream code relies on that. On Linux c_wchar
is four bytes, so the same buffer counts code points instead and every offset past an astral character
is wrong. Utf16Buffer is the Linux stand-in for that Windows buffer.
"""

import ctypes

ENCODING = "utf_16_le"

Utf16Pointer = ctypes.POINTER(ctypes.c_uint16)
"""What a C function taking UTF-16 text is declared with on Linux, in place of c_wchar_p."""


class Utf16Buffer:
	"""A NUL-terminated array of UTF-16 code units.

	Its length, indices and slices count code units, as a buffer from create_unicode_buffer does on
	Windows; a slice is returned as a str, with lone surrogates kept. A function whose argtypes name
	Utf16Pointer accepts it, and refuses a c_wchar buffer, which on Linux holds UTF-32.
	"""

	def __init__(self, text: str):
		encoded = text.encode(ENCODING, errors="surrogatepass")
		self.units = (ctypes.c_uint16 * (len(encoded) // 2 + 1)).from_buffer_copy(encoded + b"\0\0")
		# ctypes converts an object through _as_parameter_ only when that is a pointer, not an array.
		self._as_parameter_ = ctypes.cast(self.units, Utf16Pointer)

	def __len__(self) -> int:
		return len(self.units)

	def __getitem__(self, key: int | slice) -> str:
		if isinstance(key, slice):
			data = memoryview(self.units)[key].tobytes()
		else:
			data = self.units[key].to_bytes(2, "little")
		return data.decode(ENCODING, errors="surrogatepass")

	@property
	def value(self) -> str:
		"""The text up to the first NUL, as the value of a c_wchar buffer is."""
		return self[: list(self.units).index(0)]
