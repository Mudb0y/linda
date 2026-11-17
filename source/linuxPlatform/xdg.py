"""Where a Linux desktop keeps a user's files, after the XDG Base Directory Specification."""

import configparser
import os
import shutil
import subprocess

#: The name of the directory Linda keeps under each XDG base directory.
appDirName = "linda"
#: What that directory was called while the program was Basedreader.
formerAppDirName = "basedreader"


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


def adoptFormerDirectories() -> list[str]:
	"""Give the configuration and state directories of the former name the current one, where only
	the former exists, so that the user's settings carry over. The former name stays as a link to
	the directory: a copy from before the rename, which is what gets started when this one fails,
	then speaks with the settings its user chose.

	Run only while no copy started under the former name is left to write into its directories.

	:return: A sentence for each directory that could not be moved.
	"""
	failures = []
	for base in (configHome(), stateHome()):
		former = os.path.join(base, formerAppDirName)
		current = os.path.join(base, appDirName)
		if os.path.lexists(current) or not os.path.isdir(former):
			continue
		try:
			os.rename(former, current)
		except OSError as e:
			failures.append(f"could not move {former} to {current}: {e}")
			continue
		try:
			os.symlink(appDirName, former)
		except OSError as e:
			failures.append(f"could not leave {former} as a link to {current}: {e}")
	return failures


def readableUserConfigDir() -> str:
	"""The user's configuration directory to read from, which is still the former name's until a
	full start has moved it."""
	current = userConfigDir()
	former = os.path.join(configHome(), formerAppDirName)
	if not os.path.lexists(current) and os.path.isdir(former):
		return former
	return current


#: The desktop entry that would start Linda at login.
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
