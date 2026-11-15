"""Stand-in for comtypes.client.dynamic: late-bound IDispatch wrappers, none of which can exist."""

from comtypes import _notOnLinux


def Dispatch(obj):
	raise _notOnLinux("comtypes.client.dynamic.Dispatch")


class _Dispatch:
	def __init__(self, *args, **kwargs):
		raise _notOnLinux("comtypes.client.dynamic._Dispatch")


class MethodCaller:
	def __init__(self, *args, **kwargs):
		raise _notOnLinux("comtypes.client.dynamic.MethodCaller")
