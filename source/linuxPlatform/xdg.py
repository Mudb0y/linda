"""Where a Linux desktop keeps a user's files, after the XDG Base Directory Specification."""

import configparser
import os
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


def configDirs() -> list[str]:
	"""The system configuration directories, most important first."""
	dirs = [d for d in os.environ.get("XDG_CONFIG_DIRS", "").split(":") if os.path.isabs(d)]
	return dirs or ["/etc/xdg"]


def userConfigDir() -> str:
	"""The directory that holds the user's NVDA configuration: nvda.ini, profiles, add-ons."""
	return os.path.join(configHome(), appDirName)


#: The desktop entry that would start Basedreader at login.
autostartEntryName = f"{appDirName}.desktop"


def isAutostartEnabled(entryName: str) -> bool:
	"""Whether the desktop session starts the entry at login, as the Desktop Application Autostart
	Specification decides it: the user's own directory hides the system directories' entry of the same
	name, and an entry marked hidden, or disabled by a desktop's own key, does not start.
	"""
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
		if section.get("Hidden", "false").strip() == "true":
			return False
		# MATE, GNOME and others each honour their own X-<desktop>-Autostart-enabled key.
		return not any(
			key.startswith("X-") and key.endswith("-Autostart-enabled") and value.strip() == "false"
			for key, value in section.items()
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
