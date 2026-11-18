"""The RandR calls of the Screen Curtain, shared by NVDA and the curtain's guard.

It imports nothing of NVDA's, as the guard runs it in an interpreter of its own.
"""

import json
import os

from Xlib import display, error
from Xlib.ext import randr

#: A CRTC's ramps: red, green and blue. The record and the guard's state hold them by the CRTC's ID
#: in decimal.
Ramps = tuple[list[int], list[int], list[int]]

#: What connecting, or a request on a connection the X server has closed, raises.
connectionErrors = (error.DisplayError, error.ConnectionClosedError, OSError)


def connect(displayName: str) -> display.Display:
	connection = display.Display(displayName)
	# python-xlib registers RandR's three errors under the codes 0, 1 and 2 rather than above the
	# extension's first error, so a BadRequest or BadValue arrives as a class it cannot read, and the
	# AttributeError it then raises leaves the connection's queue of requests broken. The table is
	# shared by every connection and put wrong again by each one opened, so this connection gets its own.
	connection.display.error_classes = dict(error.xerror_class)
	return connection


def read(connection: display.Display, crtc: int) -> Ramps:
	reply = randr.get_crtc_gamma(connection, crtc)
	return list(reply.red), list(reply.green), list(reply.blue)


def isBlack(ramps: Ramps) -> bool:
	return not any(any(channel) for channel in ramps)


def linear(size: int) -> Ramps:
	"""The ramp an X server starts a CRTC with, which shows every colour as it is."""
	ramp = [(index << 16) // size for index in range(size)]
	return ramp, list(ramp), list(ramp)


def blacken(connection: display.Display, crtc: int, size: int) -> None:
	black = [0] * size
	randr.set_crtc_gamma(connection, crtc, size, black, black, black)


def putBack(connection: display.Display, saved: dict[int, Ramps], *, onlyWhereBlack: bool) -> bool:
	"""Set each CRTC's saved ramps; whether the X server took them.

	:param onlyWhereBlack: Leave alone a CRTC whose ramps are no longer black, which someone has
		set since the curtain was raised.
	"""
	try:
		for crtc, ramps in saved.items():
			try:
				if onlyWhereBlack and not isBlack(read(connection, crtc)):
					continue
				randr.set_crtc_gamma(connection, crtc, len(ramps[0]), *ramps)
				connection.sync()
			except error.XError:
				# A CRTC that has gone has nothing left to show.
				continue
	except connectionErrors:
		return False
	return True


def toJson(saved: dict[int, Ramps]) -> dict[str, list[list[int]]]:
	return {str(crtc): [list(channel) for channel in ramps] for crtc, ramps in saved.items()}


def fromJson(value: dict) -> dict[int, Ramps]:
	saved = {}
	for crtc, channels in value.items():
		red, green, blue = ([int(level) for level in channel] for channel in channels)
		if not len(red) == len(green) == len(blue) or not red:
			raise ValueError(f"the ramps of CRTC {crtc} are not of one size")
		saved[int(crtc)] = (red, green, blue)
	return saved


def removeRecord(path: str, token: str | None) -> None:
	"""Remove the record at path if it is still the one token names."""
	try:
		with open(path, encoding="utf-8") as f:
			if json.load(f).get("token") != token:
				return
		os.remove(path)
	except (OSError, ValueError, AttributeError):
		pass
