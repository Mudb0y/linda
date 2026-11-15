"""Linux replacement for easeOfAccess: whether NVDA starts by itself.

Upstream registers NVDA with the Windows Ease of Access Center and reads its start-up settings from
the registry. Linux has no Ease of Access Center. A desktop session starts programs at login from
XDG autostart entries, and a display manager's login screen starts what its own configuration says.
"""

from enum import IntEnum

from linuxPlatform import NoLinuxImplementation, xdg


class AutoStartContext(IntEnum):
	"""Where NVDA can start automatically. Upstream's values are Windows registry HKEYs."""

	ON_LOGON_SCREEN = 0x80000002
	AFTER_LOGON = 0x80000001


def isRegistered() -> bool:
	# Registering is how Windows learns NVDA is an assistive technology; Linux has no such register.
	return False


def notify(signal: int) -> None:
	# Upstream tells the Ease of Access Center only when registered with it, which NVDA never is here.
	return


def willAutoStart(autoStartContext: AutoStartContext) -> bool:
	"""Whether NVDA starts automatically: at login, or on the display manager's login screen."""
	if autoStartContext == AutoStartContext.AFTER_LOGON:
		return xdg.isAutostartEnabled(xdg.autostartEntryName)
	# Each display manager keeps its own configuration of what runs on its login screen, and nothing
	# here reads or writes any of them yet. The settings that ask this are disabled on Linux, which
	# has no installed copy.
	return False


def setAutoStart(autoStartContext: AutoStartContext, enable: bool) -> None:
	# Writing an autostart entry needs the command that starts NVDA, which only an installed copy has.
	raise NoLinuxImplementation(f"easeOfAccess.setAutoStart({autoStartContext.name})")
