"""Stand-in for comtypes.client.lazybind."""

from typing import Any

from comtypes import _notOnLinux


class Dispatch:
	def __init__(self, *args: Any, **kwargs: Any):
		raise _notOnLinux("comtypes.client.lazybind.Dispatch")
