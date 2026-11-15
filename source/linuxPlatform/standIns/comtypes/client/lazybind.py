"""Stand-in for comtypes.client.lazybind."""

from comtypes import _notOnLinux


class Dispatch:
	def __init__(self, *args, **kwargs):
		raise _notOnLinux("comtypes.client.lazybind.Dispatch")
