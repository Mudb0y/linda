"""Where a Linux desktop keeps a user's files, after the XDG Base Directory Specification."""

import configparser
import os
import shutil
import subprocess

#: The name of the directory Basedreader keeps under each XDG base directory.
appDirName = "basedreader"


def _baseDir(variable: str, default: str) -> str:
	path = os.environ.get(variable, "")
	# The specification says a relative path in these variables is invalid and must be ignored.
	if not os.path.isabs(path):
		path = os.path.join(os.path.expanduser("~"), default)
	return path


def configHome() -> str:
	return _baseDir("XDG_CONFIG_HOME", ".config")


def stateHome() -> str:
	return _baseDir("XDG_STATE_HOME", os.path.join(".local", "state"))


def configDirs() -> list[str]:
	"""The system configuration directories, most important first."""
	dirs = [d for d in os.environ.get("XDG_CONFIG_DIRS", "").split(":") if os.path.isabs(d)]
	return dirs or ["/etc/xdg"]


def userConfigDir() -> str:
	"""The directory that holds the user's NVDA configuration: nvda.ini, profiles, add-ons."""
	return os.path.join(configHome(), appDirName)


def userStateDir() -> str:
	"""The directory that holds what NVDA keeps between runs but is not configuration: the log."""
	return os.path.join(stateHome(), appDirName)


#: The desktop entry that would start Basedreader at login.
autostartEntryName = f"{appDirName}.desktop"


def currentDesktops() -> list[str]:
	"""The names of the running desktop environment, such as "MATE", most specific first."""
	return [name for name in os.environ.get("XDG_CURRENT_DESKTOP", "").split(":") if name]


def _isShownIn(entry: configparser.SectionProxy, desktops: list[str]) -> bool:
	"""Whether a desktop entry applies to the running desktop, by its OnlyShowIn and NotShowIn keys."""
	onlyShowIn = {name for name in entry.get("OnlyShowIn", "").split(";") if name}
	if onlyShowIn and onlyShowIn.isdisjoint(desktops):
		return False
	notShowIn = {name for name in entry.get("NotShowIn", "").split(";") if name}
	return notShowIn.isdisjoint(desktops)


def isAutostartEnabled(entryName: str) -> bool:
	"""Whether the desktop session starts the entry at login, as the Desktop Application Autostart
	Specification decides it: the user's own directory hides the system directories' entry of the same
	name, and an entry does not start when it is marked hidden, is not shown in the running desktop,
	names a TryExec program that is not installed, or is disabled by the running desktop's own key.
	"""
	desktops = currentDesktops()
	for directory in (configHome(), *configDirs()):
		path = os.path.join(directory, "autostart", entryName)
		if not os.path.isfile(path):
			continue
		entry = configparser.ConfigParser(interpolation=None, strict=False)
		entry.optionxform = str
		try:
			entry.read(path, encoding="utf-8")
			section = entry["Desktop Entry"]
		except (configparser.Error, KeyError, UnicodeDecodeError):
			# A session manager skips an entry it cannot parse.
			return False
		if section.get("Hidden", "false").strip() == "true" or not _isShownIn(section, desktops):
			return False
		tryExec = section.get("TryExec", "").strip()
		if tryExec and shutil.which(tryExec) is None:
			return False
		# MATE, GNOME and others each honour only their own X-<desktop>-Autostart-enabled key.
		return not any(
			section.get(f"X-{desktop}-Autostart-enabled", "true").strip() == "false" for desktop in desktops
		)
	return False


def openWithDefaultApplication(path: str) -> None:
	"""Open a file or directory as the desktop would on a double click, without waiting for it."""
	subprocess.Popen(
		["xdg-open", path],
		stdin=subprocess.DEVNULL,
		stdout=subprocess.DEVNULL,
		stderr=subprocess.DEVNULL,
		start_new_session=True,
	)
