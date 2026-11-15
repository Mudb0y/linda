"""Stand-in for winsound, the Windows sound module.

Its constants are Windows' values; every function raises, because a sound on Linux must go
through NVDA's own audio output and never through a second route.
"""

SND_FILENAME = 0x20000
SND_ALIAS = 0x10000
SND_LOOP = 0x0008
SND_MEMORY = 0x0004
SND_PURGE = 0x0040
SND_ASYNC = 0x0001
SND_NODEFAULT = 0x0002
SND_NOSTOP = 0x0010
SND_NOWAIT = 0x2000

MB_ICONASTERISK = 0x40
MB_ICONEXCLAMATION = 0x30
MB_ICONHAND = 0x10
MB_ICONQUESTION = 0x20
MB_OK = 0x0


def Beep(frequency: int, duration: int) -> None:
	raise NotImplementedError("winsound.Beep has no Linux implementation")


def PlaySound(sound, flags: int) -> None:
	raise NotImplementedError("winsound.PlaySound has no Linux implementation")


def MessageBeep(type: int = MB_OK) -> None:
	raise NotImplementedError("winsound.MessageBeep has no Linux implementation")
