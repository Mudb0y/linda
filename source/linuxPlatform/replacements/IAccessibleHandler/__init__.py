"""Linux replacement for IAccessibleHandler, skeleton.

MSAA and IAccessible2 are Windows APIs, so on Linux there is no IAccessible support to start or
stop, and no MSAA win event ever arrives to be pumped. Of its submodules only types, which is
portable, is kept; the rest are Windows only and are not provided.
"""

from .types import RelationType  # noqa: F401


def initialize() -> None:
	pass


def terminate() -> None:
	pass


def pumpAll() -> None:
	# The core pump calls this every cycle to deliver queued MSAA win events. Linux has none, so
	# there is nothing to deliver; AT-SPI events get their own place in the pump.
	pass
