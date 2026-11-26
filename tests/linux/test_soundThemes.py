"""Sound themes, per-sound overrides, user theme discovery, and the Sound Themes settings panel.

LINDA_RIG_WM=marco nix develop . -c tests/linux/rig python3 -m pytest -q tests/linux/test_soundThemes.py
"""

import json
import struct
import textwrap
import time
import wave
from collections.abc import Iterator
from pathlib import Path

import desktopSession
import harness
import pytest
from desktopSession import DesktopSession

WAVES = harness.SOURCE / "waves"
ONJ_WAVES = harness.SOURCE / "linuxPlatform" / "soundThemes" / "onj"
SESSION_CONFIG = harness.HERE / "fixtures" / "sessionConfig" / "nvda.ini"
STARTUP_BOUND = 60.0
SETTLE = 0.5
GTK = ("gtkFixture", "Fixture window")
AT_START = ("Fixture window", "Press me button Does nothing Alt plus P")


def _writeTestWav(path: Path, sampleValue: int, frames: int = 800, rate: int = 8000) -> Path:
	"""Write a small distinct mono 16-bit PCM WAV file for testing."""
	path.parent.mkdir(parents=True, exist_ok=True)
	with wave.open(str(path), "wb") as w:
		w.setnchannels(1)
		w.setsampwidth(2)
		w.setframerate(rate)
		w.writeframes(struct.pack(f"<{frames}h", *([sampleValue] * frames)))
	return path


def _captured(captureDir: Path) -> list[dict]:
	files = []
	for path in sorted(captureDir.iterdir()):
		with wave.open(str(path), "rb") as w:
			files.append(
				{
					"name": path.name,
					"channels": w.getnchannels(),
					"rate": w.getframerate(),
					"width": w.getsampwidth(),
					"frames": w.readframes(w.getnframes()),
				},
			)
	return files


def _readWavFrames(path: Path) -> bytes:
	with wave.open(str(path), "rb") as w:
		return w.readframes(w.getnframes())


def _runSoundChild(
	tmpPath: Path, code: str, *, captureDir: Path | None = None, configDir: Path | None = None
):
	cfgSetup = ""
	if configDir is not None:
		cfgSetup = f"""
		from NVDAState import WritePaths
		WritePaths.configDir = {str(configDir)!r}
		"""
	body = (
		textwrap.dedent(
			"""
			import os
			import sys
			import config
			"""
		)
		+ textwrap.dedent(cfgSetup)
		+ textwrap.dedent(
			"""
			config.initConfigPath()
			config.initialize()
			import NVDAHelper  # noqa: F401
			import globalVars
			import nvwave
			from linuxPlatform import soundThemes

			nvwave.initialize()
			"""
		)
		+ textwrap.dedent(code)
	)
	env = {
		"LINDA_AUDIO_SINK": None,
		"LINDA_AUDIO_CAPTURE_DIR": str(captureDir) if captureDir else "",
	}
	done = harness.runChild(tmpPath, "result = None\n" + body, env=env)
	done.assertClean()
	return done.value


def test_defaultThemeIsOnjWithLegacyFallbackAndNoWxImport(tmp_path: Path):
	"""Onj is the default theme for the 5 sounds it ships, falling back to legacy for the other 11,
	and importing/resolving sound themes never pulls in wx."""
	noWxDir = tmp_path / "noWx"
	noWxDir.mkdir()
	noWx = harness.runChild(
		noWxDir,
		"""
		import os
		import sys
		from linuxPlatform import soundThemes

		resolved = soundThemes.resolveWaveFile(os.path.join(soundThemes.appWavesDir(), "start.wav"))
		result = {"resolved": resolved, "wxImported": "wx" in sys.modules}
		""",
		prepare="bootstrap",
	)
	noWx.assertClean()
	assert not noWx.value["wxImported"]
	assert Path(noWx.value["resolved"]) == ONJ_WAVES / "start.wav"

	configDir = tmp_path / "config"
	configDir.mkdir()
	value = _runSoundChild(
		tmp_path,
		"""
		resolved = {
			event.id: soundThemes.resolveWaveFile(os.path.join(globalVars.appDir, "waves", event.fileName))
			for event in soundThemes.SOUND_EVENTS
		}
		backslashTextError = soundThemes.resolveWaveFile(r"waves\\textError.wav")
		external = soundThemes.resolveWaveFile("/tmp/third-party-addon-sound.wav")
		result = {
			"activeTheme": soundThemes.activeThemeId(),
			"eventCount": len(soundThemes.SOUND_EVENTS),
			"resolved": resolved,
			"backslashTextError": backslashTextError,
			"external": external,
		}
		""",
		configDir=configDir,
	)
	assert value["activeTheme"] == "onj"
	assert value["eventCount"] == 16
	onjIds = {"start", "exit", "error", "browseMode", "focusMode"}
	for soundId, path in value["resolved"].items():
		assert path is not None
		if soundId in onjIds:
			assert Path(path).parent == ONJ_WAVES, (soundId, path)
		else:
			assert Path(path).parent == WAVES, (soundId, path)
	assert Path(value["backslashTextError"]) == WAVES / "textError.wav"
	assert value["external"] == "/tmp/third-party-addon-sound.wav"


def test_themeSelectionOverridesCustomFileMuteAndUserThemePlayback(tmp_path: Path):
	"""Switching themes, per-sound theme/custom/off overrides, missing custom fallback, and user theme
	discovery (including theme.json and case-insensitive filenames) all reach the audio sink."""
	configDir = tmp_path / "config"
	userThemeDir = configDir / "soundThemes" / "myPack"
	userThemeDir.mkdir(parents=True)
	(userThemeDir / "theme.json").write_text(
		json.dumps({"name": "My Pack", "fallback": "onj"}),
		encoding="utf-8",
	)
	userBrowse = _writeTestWav(userThemeDir / "BrowseMode.wav", sampleValue=1200)
	customError = _writeTestWav(tmp_path / "customError.wav", sampleValue=-900)
	missingFile = tmp_path / "doesNotExist.wav"
	captureDir = tmp_path / "capture"
	captureDir.mkdir()

	value = _runSoundChild(
		tmp_path,
		f"""
		themes = [(t.id, t.name, t.builtIn) for t in soundThemes.availableThemes()]
		# 1. Default Onj plays Onj start.wav
		nvwave.playWaveFile(os.path.join(globalVars.appDir, "waves", "start.wav"), asynchronous=False)
		# 2. Switch to Legacy -> plays legacy start.wav
		config.conf["soundThemes"]["theme"] = "legacy"
		nvwave.playWaveFile(os.path.join(globalVars.appDir, "waves", "start.wav"), asynchronous=False)
		# 3. Switch to user theme myPack -> BrowseMode.wav (case-insensitive) plays custom user wav,
		#    focusMode.wav falls back to Onj, textError.wav falls back to Legacy.
		config.conf["soundThemes"]["theme"] = "myPack"
		nvwave.playWaveFile(os.path.join(globalVars.appDir, "waves", "browseMode.wav"), asynchronous=False)
		nvwave.playWaveFile(os.path.join(globalVars.appDir, "waves", "focusMode.wav"), asynchronous=False)
		nvwave.playWaveFile(r"waves\\textError.wav", asynchronous=False)
		# 4. Per-sound overrides: off (plays nothing), theme:legacy, custom file, missing custom fallback.
		config.conf["soundThemes"]["sounds"]["exit"] = "off"
		nvwave.playWaveFile(os.path.join(globalVars.appDir, "waves", "exit.wav"), asynchronous=False)
		config.conf["soundThemes"]["sounds"]["browseMode"] = "theme:legacy"
		nvwave.playWaveFile(os.path.join(globalVars.appDir, "waves", "browseMode.wav"), asynchronous=False)
		config.conf["soundThemes"]["sounds"]["error"] = "file:" + {str(customError)!r}
		nvwave.playWaveFile(os.path.join(globalVars.appDir, "waves", "error.wav"), asynchronous=False)
		config.conf["soundThemes"]["sounds"]["start"] = "file:" + {str(missingFile)!r}
		nvwave.playWaveFile(os.path.join(globalVars.appDir, "waves", "start.wav"), asynchronous=False)
		result = {{"themes": themes}}
		""",
		captureDir=captureDir,
		configDir=configDir,
	)

	assert value["themes"] == [
		["onj", "Onj", True],
		["legacy", "Legacy", True],
		["myPack", "My Pack", False],
	]
	files = _captured(captureDir)
	expectedSources = [
		ONJ_WAVES / "start.wav",
		WAVES / "start.wav",
		userBrowse,
		ONJ_WAVES / "focusMode.wav",
		WAVES / "textError.wav",
		# exit.wav was muted -> nothing captured here
		WAVES / "browseMode.wav",
		customError,
		# missing custom start.wav fell back to myPack -> fallback onj -> Onj start.wav
		ONJ_WAVES / "start.wav",
	]
	assert len(files) == len(expectedSources), [f["name"] for f in files]
	for got, expectedPath in zip(files, expectedSources):
		assert got["frames"] == _readWavFrames(expectedPath), expectedPath.name


@pytest.fixture
def desktop(tmp_path: Path) -> Iterator[DesktopSession]:
	with DesktopSession(tmp_path / "desktop") as session:
		yield session


def _step(desktop: DesktopSession, chord: str, *expected: str, timeout: float = 5) -> int:
	mark = desktop.mark()
	desktop.press(chord)
	desktop.expect(*expected, since=mark, timeout=timeout)
	return mark


def test_soundThemesSettingsPanelChangesThemeOverridesPreviewsAndSaves(
	desktop: DesktopSession, tmp_path: Path
):
	"""The Sound Themes category appears right after Audio, lists built-in and user themes plus all
	16 existing sounds, allows changing theme and per-sound overrides, previews muted sounds cleanly,
	resets overrides, and persists changes to nvda.ini."""
	configDir = tmp_path / "config"
	configDir.mkdir()
	(configDir / "nvda.ini").write_text(SESSION_CONFIG.read_text(encoding="utf-8"), encoding="utf-8")
	userThemeDir = configDir / "soundThemes" / "retro"
	userThemeDir.mkdir(parents=True)
	(userThemeDir / "theme.json").write_text(json.dumps({"name": "Retro"}), encoding="utf-8")
	_writeTestWav(userThemeDir / "start.wav", sampleValue=500)

	app = desktop.startApp(desktop.fixture(GTK[0]), title=GTK[1])
	assert app.stdout.readline() == "ready\n"
	desktopSession.activate(GTK[1])
	nvda = desktop.startNVDA(configDir)
	desktop.expect(*AT_START, since=0, timeout=STARTUP_BOUND)

	_step(desktop, "Insert+n", "Preferences sub Menu P 1 of 9")
	_step(desktop, "Return", "Settings... S 1 of 4")
	_step(
		desktop,
		"Return",
		"Linda Settings: General (normal configuration) dialog",
		"General 1 of 18",
	)
	for category in ("Speech 2 of 18", "Braille 3 of 18", "Audio 4 of 18", "Sound Themes 5 of 18"):
		_step(desktop, "Down", category)

	# Theme choice defaults to Onj and includes Legacy and the discovered user theme Retro.
	_step(desktop, "Tab", "Sound theme: combo box Onj collapsed Alt plus T")
	_step(desktop, "Down", "Legacy")
	_step(desktop, "Down", "Retro")
	_step(desktop, "Up", "Legacy")

	# Sounds list has all 16 existing sounds, starting at Startup.
	_step(desktop, "Tab", "Sounds: list", "Startup, Theme default 1 of 16")
	_step(desktop, "Down", "Exit, Theme default 2 of 16")
	_step(desktop, "End", "Remote Access clipboard received, Theme default 16 of 16")
	_step(desktop, "Home", "Startup, Theme default 1 of 16")

	# Change Startup to Off and press Preview -> announces "Sound is off".
	_step(desktop, "Tab", "Sound source: combo box Theme default collapsed Alt plus U")
	_step(desktop, "Down", "Off")
	_step(desktop, "Tab", "Browse... button Alt plus B")
	_step(desktop, "Tab", "Preview button Alt plus P")
	_step(desktop, "space", "Sound is off")

	# Change Exit (second item) to Onj override so we test both Off and theme:onj persistence.
	_step(desktop, "shift+Tab", "Browse... button Alt plus B")
	_step(desktop, "shift+Tab", "Sound source: combo box Off collapsed Alt plus U")
	_step(desktop, "shift+Tab", "Sounds: list", "Startup, Off 1 of 16")
	_step(desktop, "Down", "Exit, Theme default 2 of 16")
	_step(desktop, "alt+u", "Sound source: combo box Theme default collapsed Alt plus U")
	_step(desktop, "Down", "Off")
	_step(desktop, "Down", "Onj")
	# Reset button clears overrides, then re-apply Startup=Off and Exit=Onj and save.
	_step(desktop, "alt+r", "All sounds reset to theme defaults")
	_step(desktop, "shift+Tab", "Sounds: list", "Exit, Theme default 2 of 16")
	_step(desktop, "alt+u", "Sound source: combo box Theme default collapsed Alt plus U")
	_step(desktop, "Down", "Off")
	_step(desktop, "Down", "Onj")
	_step(desktop, "shift+Tab", "Sounds: list", "Exit, Onj 2 of 16")
	_step(desktop, "Up", "Startup, Theme default 1 of 16")
	_step(desktop, "alt+u", "Sound source: combo box Theme default collapsed Alt plus U")
	_step(desktop, "Down", "Off")
	# Enter on wx.Choice presses OK.
	_step(desktop, "Return", *AT_START)

	_step(desktop, "Insert+control+c", "Configuration saved")
	time.sleep(SETTLE)
	saved = (nvda.directory / "config" / "nvda.ini").read_text(encoding="utf-8")
	assert "[soundThemes]" in saved, saved
	assert "theme = legacy" in saved, saved
	assert "start = off" in saved, saved
	assert "exit = theme:onj" in saved, saved

	desktop.close()
	assert nvda.process.returncode == 0, nvda.describe()
	nvda.assertClean(logNames=("nvda.log",))
