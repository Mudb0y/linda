"""Sound themes and per-sound overrides for Linda's wave sounds.

Built-in themes:
- ``onj`` (default): Andre Louis's Onj NVDA sound pack for startup, exit, error, browse mode and
  focus mode, falling back to ``legacy`` for the remaining built-in sounds.
- ``legacy``: NVDA's upstream wave files in ``source/waves/``.

User themes can also be placed in subdirectories of ``<configDir>/soundThemes/``, each holding
``.wav`` files named after the built-in sound events (matched case-insensitively) and an optional
``theme.json`` with ``name`` and ``fallback`` fields.

This module intentionally avoids importing wx so non-GUI audio callers (such as ``linda say`` and
audio tests) never pull in GTK. The settings UI lives in ``linuxPlatform.soundThemes.gui``.
"""

from __future__ import annotations

import builtins
import contextlib
import json
import os
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass

#: Default built-in sound theme identifier.
DEFAULT_THEME = "onj"
#: Upstream NVDA wave files theme identifier.
LEGACY_THEME = "legacy"

#: Per-sound setting value meaning "use the active theme's sound".
OVERRIDE_THEME = "theme"
#: Per-sound setting value meaning "do not play this sound".
OVERRIDE_OFF = "off"
#: Per-sound setting prefix meaning "use the sound from a named theme".
OVERRIDE_THEME_PREFIX = "theme:"
#: Per-sound setting prefix meaning "play a specific custom .wav file".
OVERRIDE_FILE_PREFIX = "file:"

#: Subdirectory under the user's configuration directory holding user sound themes.
USER_THEMES_SUBDIR = "soundThemes"


def _tr(text: str) -> str:
	translate = getattr(builtins, "_", None)
	return translate(text) if callable(translate) else text


@dataclass(frozen=True)
class SoundEvent:
	"""One built-in wave sound event that a theme or per-sound setting can customize."""

	id: str
	fileName: str
	labelGetter: Callable[[], str]

	@property
	def label(self) -> str:
		return self.labelGetter()


#: Existing wave sounds shipped by NVDA/Linda, in presentation order.
SOUND_EVENTS: tuple[SoundEvent, ...] = (
	SoundEvent(
		"start",
		"start.wav",
		# Translators: Name of the startup sound in Sound Themes settings.
		lambda: _tr("Startup"),
	),
	SoundEvent(
		"exit",
		"exit.wav",
		# Translators: Name of the exit sound in Sound Themes settings.
		lambda: _tr("Exit"),
	),
	SoundEvent(
		"error",
		"error.wav",
		# Translators: Name of the error sound in Sound Themes settings.
		lambda: _tr("Error"),
	),
	SoundEvent(
		"browseMode",
		"browseMode.wav",
		# Translators: Name of the browse mode sound in Sound Themes settings.
		lambda: _tr("Browse mode"),
	),
	SoundEvent(
		"focusMode",
		"focusMode.wav",
		# Translators: Name of the focus mode sound in Sound Themes settings.
		lambda: _tr("Focus mode"),
	),
	SoundEvent(
		"textError",
		"textError.wav",
		# Translators: Name of the spelling and grammar error sound in Sound Themes settings.
		lambda: _tr("Spelling and grammar error"),
	),
	SoundEvent(
		"suggestionsOpened",
		"suggestionsOpened.wav",
		# Translators: Name of the auto-suggestions opened sound in Sound Themes settings.
		lambda: _tr("Auto-suggestions opened"),
	),
	SoundEvent(
		"suggestionsClosed",
		"suggestionsClosed.wav",
		# Translators: Name of the auto-suggestions closed sound in Sound Themes settings.
		lambda: _tr("Auto-suggestions closed"),
	),
	SoundEvent(
		"screenCurtainOn",
		"screenCurtainOn.wav",
		# Translators: Name of the Screen Curtain enabled sound in Sound Themes settings.
		lambda: _tr("Screen Curtain on"),
	),
	SoundEvent(
		"screenCurtainOff",
		"screenCurtainOff.wav",
		# Translators: Name of the Screen Curtain disabled sound in Sound Themes settings.
		lambda: _tr("Screen Curtain off"),
	),
	SoundEvent(
		"connected",
		"connected.wav",
		# Translators: Name of the Remote Access connected sound in Sound Themes settings.
		lambda: _tr("Remote Access connected"),
	),
	SoundEvent(
		"disconnected",
		"disconnected.wav",
		# Translators: Name of the Remote Access disconnected sound in Sound Themes settings.
		lambda: _tr("Remote Access disconnected"),
	),
	SoundEvent(
		"controlling",
		"controlling.wav",
		# Translators: Name of the Remote Access controlling sound in Sound Themes settings.
		lambda: _tr("Remote Access controlling"),
	),
	SoundEvent(
		"controlled",
		"controlled.wav",
		# Translators: Name of the Remote Access controlled sound in Sound Themes settings.
		lambda: _tr("Remote Access controlled"),
	),
	SoundEvent(
		"clipboardPush",
		"clipboardPush.wav",
		# Translators: Name of the Remote Access clipboard sent sound in Sound Themes settings.
		lambda: _tr("Remote Access clipboard sent"),
	),
	SoundEvent(
		"clipboardReceive",
		"clipboardReceive.wav",
		# Translators: Name of the Remote Access clipboard received sound in Sound Themes settings.
		lambda: _tr("Remote Access clipboard received"),
	),
)

SOUND_EVENTS_BY_ID: dict[str, SoundEvent] = {event.id: event for event in SOUND_EVENTS}
_SOUND_EVENTS_BY_FILENAME_LOWER: dict[str, SoundEvent] = {
	event.fileName.casefold(): event for event in SOUND_EVENTS
}


@dataclass(frozen=True)
class SoundTheme:
	"""A directory of wave files, optionally falling back to another theme."""

	id: str
	name: str
	directory: str
	fallback: str | None = LEGACY_THEME
	builtIn: bool = False


_local = threading.local()


def _appDir() -> str:
	with contextlib.suppress(ImportError, AttributeError):
		import globalVars

		if globalVars.appDir:
			return globalVars.appDir
	# Fallback when called before globalVars.appDir is set: source/linuxPlatform/soundThemes/__init__.py
	return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def appWavesDir() -> str:
	"""Upstream NVDA's waves directory, backing the legacy theme."""
	return os.path.join(_appDir(), "waves")


def builtInThemesDir() -> str:
	"""Directory holding bundled Linux sound theme directories (such as ``onj``)."""
	return os.path.dirname(os.path.abspath(__file__))


def userThemesDir() -> str:
	"""Directory under the active user configuration directory where user sound themes live."""
	configDir = ""
	with contextlib.suppress(ImportError, AttributeError):
		from NVDAState import WritePaths

		configDir = WritePaths.configDir or ""
	if not configDir:
		from linuxPlatform import xdg

		configDir = xdg.readableUserConfigDir()
	return os.path.join(configDir, USER_THEMES_SUBDIR)


def _loadUserTheme(themeId: str, directory: str) -> SoundTheme:
	name = themeId
	fallback: str | None = LEGACY_THEME
	manifestPath = os.path.join(directory, "theme.json")
	if os.path.isfile(manifestPath):
		try:
			with open(manifestPath, encoding="utf-8") as f:
				data = json.load(f)
			if isinstance(data, dict):
				manifestName = data.get("name")
				if isinstance(manifestName, str) and manifestName.strip():
					name = manifestName.strip()
				if "fallback" in data:
					manifestFallback = data["fallback"]
					if manifestFallback is None or isinstance(manifestFallback, str):
						fallback = manifestFallback
		except Exception:  # noqa: BLE001
			from logHandler import log

			log.debugWarning(f"Could not read sound theme manifest {manifestPath!r}", exc_info=True)
	return SoundTheme(id=themeId, name=name, directory=directory, fallback=fallback, builtIn=False)


def availableThemes() -> list[SoundTheme]:
	"""Built-in themes followed by any user themes discovered in the configuration directory."""
	themes: list[SoundTheme] = [
		SoundTheme(
			id=DEFAULT_THEME,
			# Translators: Display name of the default Onj sound theme.
			name=_tr("Onj"),
			directory=os.path.join(builtInThemesDir(), DEFAULT_THEME),
			fallback=LEGACY_THEME,
			builtIn=True,
		),
		SoundTheme(
			id=LEGACY_THEME,
			# Translators: Display name of the legacy (classic) sound theme.
			name=_tr("Legacy"),
			directory=appWavesDir(),
			fallback=None,
			builtIn=True,
		),
	]
	builtInIds = {theme.id for theme in themes}
	userDir = userThemesDir()
	if os.path.isdir(userDir):
		try:
			entries = sorted(os.scandir(userDir), key=lambda e: e.name.casefold())
		except OSError:
			entries = []
		for entry in entries:
			if entry.name.startswith(".") or entry.name in builtInIds or not entry.is_dir():
				continue
			themes.append(_loadUserTheme(entry.name, entry.path))
	return themes


def findWaveInDirectory(directory: str, fileName: str) -> str | None:
	"""Find ``fileName`` inside ``directory``, trying an exact match first and then case-insensitively."""
	exact = os.path.join(directory, fileName)
	if os.path.isfile(exact):
		return exact
	if not os.path.isdir(directory):
		return None
	target = fileName.casefold()
	try:
		for entry in os.scandir(directory):
			if entry.name.casefold() == target and entry.is_file():
				return entry.path
	except OSError:
		return None
	return None


def activeThemeId() -> str:
	"""Currently configured sound theme identifier, defaulting to ``onj``."""
	with contextlib.suppress(ImportError, KeyError, AttributeError, TypeError):
		import config

		if config.conf is not None:
			theme = config.conf["soundThemes"]["theme"]
			if isinstance(theme, str) and theme:
				return theme
	return DEFAULT_THEME


def soundOverride(soundId: str) -> str:
	"""Currently configured per-sound override for ``soundId``, or ``theme`` by default."""
	with contextlib.suppress(ImportError, KeyError, AttributeError, TypeError):
		import config

		if config.conf is not None:
			sounds = config.conf["soundThemes"]["sounds"]
			if soundId in sounds:
				value = sounds[soundId]
				if isinstance(value, str) and value:
					return value
	return OVERRIDE_THEME


def resolveSoundForTheme(
	soundId: str,
	themeId: str,
	themesById: dict[str, SoundTheme] | None = None,
) -> str | None:
	"""Resolve ``soundId`` against ``themeId``, following theme fallbacks down to legacy waves."""
	event = SOUND_EVENTS_BY_ID.get(soundId)
	if event is None:
		return None
	if themesById is None:
		themesById = {theme.id: theme for theme in availableThemes()}
	visited: set[str] = set()
	current: str | None = themeId
	while current is not None and current not in visited:
		visited.add(current)
		theme = themesById.get(current)
		if theme is None:
			break
		found = findWaveInDirectory(theme.directory, event.fileName)
		if found is not None:
			return found
		current = theme.fallback
	return findWaveInDirectory(appWavesDir(), event.fileName)


def resolveSound(
	soundId: str,
	*,
	themeId: str | None = None,
	override: str | None = None,
	themesById: dict[str, SoundTheme] | None = None,
) -> str | None:
	"""Resolve ``soundId`` to a wave file path, or ``None`` if muted."""
	if soundId not in SOUND_EVENTS_BY_ID:
		return None
	if override is None:
		override = soundOverride(soundId)
	if themeId is None:
		themeId = activeThemeId()
	if override == OVERRIDE_OFF:
		return None
	if override.startswith(OVERRIDE_FILE_PREFIX):
		customPath = override[len(OVERRIDE_FILE_PREFIX) :]
		if customPath and os.path.isfile(customPath):
			return customPath
		from logHandler import log

		log.debugWarning(
			f"Custom sound file for {soundId!r} not found: {customPath!r}; falling back to theme"
		)
		return resolveSoundForTheme(soundId, themeId, themesById)
	if override.startswith(OVERRIDE_THEME_PREFIX):
		targetTheme = override[len(OVERRIDE_THEME_PREFIX) :]
		return resolveSoundForTheme(soundId, targetTheme, themesById)
	return resolveSoundForTheme(soundId, themeId, themesById)


def soundEventForWavePath(fileName: str) -> SoundEvent | None:
	"""Return the built-in ``SoundEvent`` named by ``fileName``, or ``None`` for external files."""
	if not fileName:
		return None
	normalized = fileName.replace("\\", "/")
	trimmed = normalized
	while trimmed.startswith("./"):
		trimmed = trimmed[2:]
	parts = trimmed.split("/")
	if len(parts) == 2 and parts[0].casefold() == "waves":
		return _SOUND_EVENTS_BY_FILENAME_LOWER.get(parts[1].casefold())
	try:
		dirPath = os.path.abspath(os.path.dirname(normalized))
		wavesPath = os.path.abspath(appWavesDir())
	except OSError:
		return None
	if dirPath == wavesPath:
		return _SOUND_EVENTS_BY_FILENAME_LOWER.get(os.path.basename(normalized).casefold())
	return None


@contextlib.contextmanager
def bypassResolution() -> Iterator[None]:
	"""Temporarily bypass theme resolution in ``resolveWaveFile`` on the current thread."""
	previous = getattr(_local, "bypass", False)
	_local.bypass = True
	try:
		yield
	finally:
		_local.bypass = previous


def resolveWaveFile(fileName: str) -> str | None:
	"""Map a built-in NVDA wave path through the active sound theme and per-sound overrides.

	Returns ``None`` when the sound is muted, or the original ``fileName`` unchanged when it is not
	one of the built-in sounds.
	"""
	if getattr(_local, "bypass", False):
		return fileName
	event = soundEventForWavePath(fileName)
	if event is None:
		return fileName
	return resolveSound(event.id)


def playDirect(fileName: str, *, asynchronous: bool = True) -> None:
	"""Play a specific wave file directly without theme remapping (used for settings preview)."""
	import nvwave

	with bypassResolution():
		nvwave.playWaveFile(fileName, asynchronous=asynchronous)
