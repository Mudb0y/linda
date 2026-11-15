from comtypes import _notOnLinux


class AudioSession:
	def __init__(self, *args, **kwargs):
		raise _notOnLinux("pycaw.utils.AudioSession")


class AudioUtilities:
	@staticmethod
	def GetAudioSessionManager():
		raise _notOnLinux("pycaw.utils.AudioUtilities.GetAudioSessionManager")

	@staticmethod
	def GetAllSessions():
		raise _notOnLinux("pycaw.utils.AudioUtilities.GetAllSessions")

	@staticmethod
	def GetDeviceEnumerator():
		raise _notOnLinux("pycaw.utils.AudioUtilities.GetDeviceEnumerator")

	@staticmethod
	def CreateDevice(dev):
		raise _notOnLinux("pycaw.utils.AudioUtilities.CreateDevice")
