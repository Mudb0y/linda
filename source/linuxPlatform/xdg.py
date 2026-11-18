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


def cacheHome() -> str:
	return _baseDir("XDG_CACHE_HOME", ".cache")


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


def userCacheDir() -> str:
	"""The directory that holds what NVDA can fetch or work out again: the add-on store's cache and
	downloads, and the update check's state."""
	return os.path.join(cacheHome(), appDirName)


#: What NVDA keeps in its configuration directory that is a cache, and so lives in userCacheDir
#: wherever the configuration directory is the default one.
cacheNames = ("addonStore", "updateCheckState.pickle")


def isDefaultConfigDir(configDir: str) -> bool:
	"""Whether configDir is the user's own configuration directory rather than another one chosen
	with -c, which keeps its caches inside it so that it stays self-contained."""
	return os.path.realpath(configDir) == os.path.realpath(userConfigDir())


def cacheDirFor(configDir: str) -> str:
	"""Where the caches of the configuration in configDir belong."""
	return userCacheDir() if isDefaultConfigDir(configDir) else configDir


def moveCachesOutOfConfigDir() -> list[str]:
	"""Move the caches that a copy from before they had a directory of their own left in the user's
	configuration directory, where there are none in the cache directory yet, and make the cache
	directory, so that the update check, which writes its state without making any directory, can.

	Run only while no other copy is left to write into either directory.

	:return: A sentence for each cache that could not be moved, which then stays where it was.
	"""
	cacheDir = userCacheDir()
	try:
		os.makedirs(cacheDir, exist_ok=True)
	except OSError as e:
		return [f"could not make {cacheDir}: {e}"]
	failures = []
	for name in cacheNames:
		former = os.path.join(userConfigDir(), name)
		current = os.path.join(cacheDir, name)
		if os.path.lexists(current) or not os.path.lexists(former):
			continue
		try:
			# Copies and then removes where the two are on different file systems.
			shutil.move(former, current)
		except OSError as e:
			failures.append(f"could not move {former} to {current}: {e}")
	return failures


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


#: Characters that the Desktop Entry Specification reserves in an Exec argument, which then has to be
#: quoted.
_EXEC_RESERVED = frozenset(" \t\n\"'\\><~|&;$*?#()`")


def desktopEntryExec(argument: str) -> str:
	"""argument as one argument of an Exec key, quoted and escaped as the Desktop Entry Specification
	says, which escapes it twice: once inside the quotes and again as a string value."""
	argument = argument.replace("%", "%%")
	if any(c in _EXEC_RESERVED for c in argument):
		quoted = "".join("\\" + c if c in '"`$\\' else c for c in argument)
		argument = f'"{quoted}"'
	return argument.replace("\\", "\\\\")


def autostartEntryPath(entryName: str) -> str:
	"""The user's own autostart entry of that name, which overrides the system directories' entry."""
	return os.path.join(configHome(), "autostart", entryName)


def writeAutostartEntry(entryName: str, name: str, command: str, enable: bool) -> None:
	"""Write the user's autostart entry so that the session starts command at login, or does not.

	Not starting is an entry marked Hidden, which the Desktop Application Autostart Specification
	reads as deleted, so that it also overrides an entry of the same name in a system directory.
	The entry is replaced whole, so that nothing left in an older one, such as a desktop's own key that
	disables it, keeps it from doing what was asked.

	:raise OSError: When the entry cannot be written.
	"""
	path = autostartEntryPath(entryName)
	lines = [
		"[Desktop Entry]",
		"Type=Application",
		f"Name={name}",
		f"Exec={desktopEntryExec(command)}",
	]
	if not enable:
		lines.append("Hidden=true")
	os.makedirs(os.path.dirname(path), exist_ok=True)
	temporary = f"{path}.new"
	with open(temporary, "w", encoding="utf-8") as f:
		f.write("".join(f"{line}\n" for line in lines))
	os.replace(temporary, path)


def openWithDefaultApplication(path: str) -> None:
	"""Open a file or directory as the desktop would on a double click, without waiting for it."""
	subprocess.Popen(
		["xdg-open", path],
		stdin=subprocess.DEVNULL,
		stdout=subprocess.DEVNULL,
		stderr=subprocess.DEVNULL,
		start_new_session=True,
	)
