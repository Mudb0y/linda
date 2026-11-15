from typing import Any, NoReturn

from comtypes import _notOnLinux


class AudioSession:
	def __init__(self, *args: Any, **kwargs: Any):
		raise _notOnLinux("pycaw.utils.AudioSession")


class AudioUtilities:
	@staticmethod
	def GetAudioSessionManager() -> NoReturn:
		raise _notOnLinux("pycaw.utils.AudioUtilities.GetAudioSessionManager")

	@staticmethod
	def GetAllSessions() -> NoReturn:
		raise _notOnLinux("pycaw.utils.AudioUtilities.GetAllSessions")

	@staticmethod
	def GetDeviceEnumerator() -> NoReturn:
		raise _notOnLinux("pycaw.utils.AudioUtilities.GetDeviceEnumerator")

	@staticmethod
	def CreateDevice(dev: Any) -> NoReturn:
		raise _notOnLinux("pycaw.utils.AudioUtilities.CreateDevice")
