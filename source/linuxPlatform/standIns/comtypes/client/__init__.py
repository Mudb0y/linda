"""Stand-in for comtypes.client. Every way of obtaining a COM object or type library fails."""

from comtypes import _notOnLinux

# comtypes.client imports these itself, and NVDA reaches them as its attributes.
from . import dynamic, lazybind  # noqa: F401, E402


def GetModule(tlib, *args, **kwargs):
	raise _notOnLinux(f"comtypes.client.GetModule({tlib!r})")


def CreateObject(progid, *args, **kwargs):
	raise _notOnLinux(f"comtypes.client.CreateObject({progid!r})")


def GetActiveObject(progid, *args, **kwargs):
	raise _notOnLinux(f"comtypes.client.GetActiveObject({progid!r})")


def CoGetObject(displayName, *args, **kwargs):
	raise _notOnLinux(f"comtypes.client.CoGetObject({displayName!r})")


def GetBestInterface(punk):
	raise _notOnLinux("comtypes.client.GetBestInterface")


def PumpEvents(timeout):
	raise _notOnLinux("comtypes.client.PumpEvents")
