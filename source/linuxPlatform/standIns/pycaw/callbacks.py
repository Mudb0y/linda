"""pycaw's callback bases. In pycaw they are COM objects, so on Linux none can be created."""

from comtypes import COMObject


class AudioSessionNotification(COMObject):
	pass


class AudioSessionEvents(COMObject):
	pass
