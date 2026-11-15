"""Linux replacement for IAccessibleHandler, skeleton.

MSAA and IAccessible2 are Windows APIs, so on Linux there is no IAccessible support to start or
stop. Its submodules are Windows only and are not provided.
"""


def initialize() -> None:
	pass


def terminate() -> None:
	pass


def pumpAll() -> None:
	# The core pump calls this every cycle. Whether Linux leaves it empty or puts its own event
	# source here is not decided, so it raises until it is.
	raise NotImplementedError("IAccessibleHandler.pumpAll has no Linux implementation")
