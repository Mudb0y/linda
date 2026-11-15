"""Linux replacement for wasapi, skeleton.

Upstream binds the WASAPI player in nvdaHelperLocal.dll. nvwave sets restype on these and calls
them; each raises until there is a Linux audio output behind it.
"""

from collections.abc import Callable


def _notImplemented(name: str) -> Callable:
	def entryPoint(*args, **kwargs):
		raise NotImplementedError(f"wasapi.{name} has no Linux implementation")

	entryPoint.__name__ = entryPoint.__qualname__ = name
	return entryPoint


wasPlay_create = _notImplemented("wasPlay_create")
wasPlay_destroy = _notImplemented("wasPlay_destroy")
wasPlay_open = _notImplemented("wasPlay_open")
wasPlay_feed = _notImplemented("wasPlay_feed")
wasPlay_stop = _notImplemented("wasPlay_stop")
wasPlay_sync = _notImplemented("wasPlay_sync")
wasPlay_idle = _notImplemented("wasPlay_idle")
wasPlay_pause = _notImplemented("wasPlay_pause")
wasPlay_resume = _notImplemented("wasPlay_resume")
wasPlay_setChannelVolume = _notImplemented("wasPlay_setChannelVolume")
wasPlay_startup = _notImplemented("wasPlay_startup")
wasPlay_startTrimmingLeadingSilence = _notImplemented("wasPlay_startTrimmingLeadingSilence")
wasSilence_init = _notImplemented("wasSilence_init")
wasSilence_playFor = _notImplemented("wasSilence_playFor")
wasSilence_terminate = _notImplemented("wasSilence_terminate")
