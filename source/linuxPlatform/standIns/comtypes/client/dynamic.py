"""Stand-in for comtypes.client.dynamic: late-bound IDispatch wrappers, none of which can exist."""

from typing import Any, NoReturn

from comtypes import _notOnLinux


def Dispatch(obj: Any) -> NoReturn:
	raise _notOnLinux("comtypes.client.dynamic.Dispatch")


class _Dispatch:
	def __init__(self, *args: Any, **kwargs: Any):
		raise _notOnLinux("comtypes.client.dynamic._Dispatch")


class MethodCaller:
	def __init__(self, *args: Any, **kwargs: Any):
		raise _notOnLinux("comtypes.client.dynamic.MethodCaller")
