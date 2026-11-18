"""Linux replacement for easeOfAccess: whether NVDA starts by itself.

Upstream registers NVDA with the Windows Ease of Access Center and reads its start-up settings from
the registry. Linux has no Ease of Access Center. A desktop session starts programs at login from
XDG autostart entries, and a display manager's login screen starts what its own configuration says.
"""

from enum import IntEnum
from typing import NoReturn

from linuxPlatform import NoLinuxImplementation, branding, launcher, missingAttribute, xdg
from logHandler import log


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
	# here reads or writes any of them. The settings that ask this are disabled on Linux, as they are
	# for a copy of NVDA that is not installed.
	return False


def setAutoStart(autoStartContext: AutoStartContext, enable: bool) -> None:
	"""Start NVDA after login, through the user's XDG autostart entry, or stop it starting there.

	When that cannot be done, the user is told why in a message box, as the settings that call this
	expect it to return.
	"""
	if autoStartContext != AutoStartContext.AFTER_LOGON:
		raise NoLinuxImplementation(f"easeOfAccess.setAutoStart({autoStartContext.name})")
	try:
		# A hidden entry starts nothing, so the command it names matters only when it is not.
		command = launcher.lastingCommand() if enable else launcher.LAUNCHER_NAME
	except launcher.NoLastingCommand as e:
		_tellCannotStartAfterLogon(f"{branding.NAME} cannot start after you sign in, because {e}.")
		return
	try:
		xdg.writeAutostartEntry(xdg.autostartEntryName, branding.NAME, command, enable)
	except OSError as e:
		_tellCannotStartAfterLogon(
			f"{branding.NAME} could not change whether it starts after you sign in,"
			f" because {xdg.autostartEntryPath(xdg.autostartEntryName)} could not be written: {e}",
		)


def _tellCannotStartAfterLogon(message: str) -> None:
	log.warning(message)
	from gui.message import MessageDialog

	# Translators: The title of a message box saying why NVDA cannot be made to start after sign-in.
	MessageDialog.alert(message, _("Start after sign-in"))


def __getattr__(name: str) -> NoReturn:
	missingAttribute(__name__, name)
