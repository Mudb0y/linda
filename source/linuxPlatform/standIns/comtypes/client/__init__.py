"""Stand-in for comtypes.client. Every way of obtaining a COM object or type library fails."""

from typing import Any, NoReturn

from comtypes import _notOnLinux

# comtypes.client imports these itself, and NVDA reaches them as its attributes.
from . import dynamic, lazybind  # noqa: F401


def GetModule(tlib: Any, *args: Any, **kwargs: Any) -> NoReturn:
	raise _notOnLinux(f"comtypes.client.GetModule({tlib!r})")


def CreateObject(progid: Any, *args: Any, **kwargs: Any) -> NoReturn:
	raise _notOnLinux(f"comtypes.client.CreateObject({progid!r})")


def GetActiveObject(progid: Any, *args: Any, **kwargs: Any) -> NoReturn:
	raise _notOnLinux(f"comtypes.client.GetActiveObject({progid!r})")


def CoGetObject(displayName: str, *args: Any, **kwargs: Any) -> NoReturn:
	raise _notOnLinux(f"comtypes.client.CoGetObject({displayName!r})")


def GetBestInterface(punk: Any) -> NoReturn:
	raise _notOnLinux("comtypes.client.GetBestInterface")


def PumpEvents(timeout: float) -> NoReturn:
	raise _notOnLinux("comtypes.client.PumpEvents")
