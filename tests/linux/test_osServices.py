"""Paths, saved configuration, language, the command line and the operating system's answers on Linux.

nix develop . -c tests/linux/rig python -m pytest tests/linux/test_osServices.py
"""

import locale
import os
import stat
import textwrap
from pathlib import Path

import harness
import pytest

#: What nvda.pyw establishes before core.main, short of a configuration: translations installed as
#: builtins, appDir, and a record of every warning and error logged. The working directory stays the child's own,
#: so that nothing found relative to it can pass for something found under appDir.
PRELUDE = f"""
import gettext
import logging
import os

gettext.NullTranslations().install(names=["pgettext", "npgettext", "ngettext"])
import globalVars

globalVars.appDir = {str(harness.SOURCE)!r}
globalVars.appPid = os.getpid()
from logHandler import log

logErrors = []
warnings = []


class Collector(logging.Handler):
	def emit(self, record):
		message = f"{{record.levelname}}: {{record.getMessage()}}"
		(logErrors if record.levelno >= logging.ERROR else warnings).append(message)


log.addHandler(Collector(level=logging.WARNING))


def startConfig():
	# core.main's own steps, from finding the configuration directory to loading it.
	import config
	from NVDAState import WritePaths

	if not WritePaths.configDir:
		WritePaths.configDir = config.getUserDefaultConfigPath()
	config.initConfigPath()
	config.initialize()
	return config


def startCore():
	# NVDA's modules import each other in one big cycle, which resolves only when entered where
	# core.main enters it.
	config = startConfig()
	import NVDAHelper  # noqa: F401

	return config
"""

#: Each child sees only the locale variables a test gives it, whatever the rig inherited.
NO_LOCALE = {name: "" for name in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LC_MEASUREMENT", "LANG")}


def run(tmpPath: Path, body: str, env: dict[str, str] | None = None) -> harness.ChildResult:
	code = PRELUDE + textwrap.dedent(body) + "\nresult = {'value': result, 'logErrors': logErrors}\n"
	done = harness.runChild(tmpPath, code, prepare="bootstrap", env=env)
	if done.error is None:
		done.logErrors = done.value["logErrors"]
		done.value = done.value["value"]
	return done


def xdgEnv(tmpPath: Path) -> dict[str, str]:
	return {"XDG_CONFIG_HOME": str(tmpPath / "xdgConfig"), "XDG_CONFIG_DIRS": str(tmpPath / "xdgSystem")}


def isLocaleInstalled(name: str) -> bool:
	saved = locale.setlocale(locale.LC_ALL)
	try:
		locale.setlocale(locale.LC_ALL, name)
		return True
	except locale.Error:
		return False
	finally:
		locale.setlocale(locale.LC_ALL, saved)


def test_firstRunCreatesConfigurationUnderXdgConfigHome(tmp_path):
	done = run(
		tmp_path,
		"""
		config = startConfig()
		from NVDAState import WritePaths

		with open(WritePaths.nvdaConfigFile, encoding="utf-8") as f:
			nvdaIni = f.read()
		result = {
			"configDir": WritePaths.configDir,
			"entries": sorted(os.listdir(WritePaths.configDir)),
			"nvdaIni": nvdaIni,
			"baseConfigError": config.conf.baseConfigError,
		}
		""",
		env=xdgEnv(tmp_path),
	)
	done.assertClean()
	assert done.value["configDir"] == str(tmp_path / "xdgConfig" / "basedreader")
	assert done.value["entries"] == ["addons", "nvda.ini", "profiles", "speechDicts"]
	assert done.value["nvdaIni"].startswith("schemaVersion = 25")
	assert done.value["baseConfigError"] is False


def test_defaultConfigurationIgnoresARelativeXdgConfigHome(tmp_path):
	done = run(
		tmp_path,
		"""
		import config

		result = config.getUserDefaultConfigPath()
		""",
		# The specification makes a relative path invalid, so ~/.config is used instead.
		env={"XDG_CONFIG_HOME": "relative/config"},
	)
	done.assertClean()
	assert done.value == os.path.join(os.path.expanduser("~"), ".config", "basedreader")
	assert not done.value.startswith(str(harness.SOURCE))


def test_changedSettingSurvivesSaveAndReload(tmp_path):
	env = xdgEnv(tmp_path)
	saved = run(
		tmp_path,
		"""
		config = startConfig()
		config.conf["keyboard"]["speakTypedWords"] = 2
		config.conf["speech"]["symbolLevel"] = 300
		config.conf.save()
		from NVDAState import WritePaths

		result = sorted(os.listdir(WritePaths.configDir))
		""",
		env=env,
	)
	saved.assertClean()
	assert not [name for name in saved.value if name.endswith(".tmp")], saved.value
	reloaded = run(
		tmp_path,
		"""
		config = startConfig()
		result = {
			"speakTypedWords": config.conf["keyboard"]["speakTypedWords"],
			"symbolLevel": config.conf["speech"]["symbolLevel"],
			"baseConfigError": config.conf.baseConfigError,
		}
		""",
		env=env,
	)
	reloaded.assertClean()
	assert reloaded.value == {"speakTypedWords": 2, "symbolLevel": 300, "baseConfigError": False}


def test_faultTolerantFileReplacesWholeOrNotAtAll(tmp_path):
	target = tmp_path / "nvda.ini"
	target.write_text("original\n", encoding="utf-8")
	done = run(
		tmp_path,
		f"""
		import stat
		from fileUtils import FaultTolerantFile

		target = {str(target)!r}
		synced = []
		realFsync = os.fsync


		def fsync(fd):
			fd = fd if isinstance(fd, int) else fd.fileno()
			synced.append("directory" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file")
			realFsync(fd)


		os.fsync = fsync
		try:
			with FaultTolerantFile(target) as f:
				f.write(b"half a fil")
				raise RuntimeError("interrupted by the test")
		except RuntimeError:
			pass
		with open(target, encoding="utf-8") as f:
			afterFailure = f.read()
		synced.clear()
		with FaultTolerantFile(target) as f:
			f.write(b"replacement\\n")
		with open(target, encoding="utf-8") as f:
			afterSuccess = f.read()
		result = {{"afterFailure": afterFailure, "afterSuccess": afterSuccess, "synced": synced}}
		""",
	)
	done.assertClean()
	assert done.value["afterFailure"] == "original\n"
	assert done.value["afterSuccess"] == "replacement\n"
	# The new content is on disk before it takes the old file's name, and the name is on disk after.
	assert done.value["synced"] == ["file", "directory"]
	assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_savingThroughALinkKeepsTheLink(tmp_path):
	configDir = tmp_path / "config"
	dotfiles = tmp_path / "dotfiles"
	readOnly = tmp_path / "readOnly"
	for directory in (configDir, dotfiles, readOnly):
		directory.mkdir()
	(dotfiles / "nvda.ini").write_text("original\n", encoding="utf-8")
	(configDir / "nvda.ini").symlink_to(dotfiles / "nvda.ini")
	(readOnly / "profile.ini").write_text("original\n", encoding="utf-8")
	(configDir / "profile.ini").symlink_to(readOnly / "profile.ini")
	readOnly.chmod(0o555)
	try:
		done = run(
			tmp_path,
			f"""
			from fileUtils import FaultTolerantFile

			with FaultTolerantFile({str(configDir / "nvda.ini")!r}) as f:
				f.write(b"replacement\\n")
			try:
				with FaultTolerantFile({str(configDir / "profile.ini")!r}) as f:
					f.write(b"replacement\\n")
				intoReadOnly = "saved"
			except PermissionError:
				intoReadOnly = "PermissionError"
			result = {{"intoReadOnly": intoReadOnly, "left": sorted(os.listdir({str(configDir)!r}))}}
			""",
		)
	finally:
		readOnly.chmod(0o755)
	done.assertClean()
	assert (configDir / "nvda.ini").is_symlink()
	assert (dotfiles / "nvda.ini").read_text(encoding="utf-8") == "replacement\n"
	# A link into a directory NVDA may not write fails as a read-only file does, and leaves no debris.
	assert done.value == {"intoReadOnly": "PermissionError", "left": ["nvda.ini", "profile.ini"]}
	assert (configDir / "profile.ini").is_symlink()
	assert (readOnly / "profile.ini").read_text(encoding="utf-8") == "original\n"


def test_saveSurvivesAFileSystemThatCannotSyncDirectories(tmp_path):
	target = tmp_path / "nvda.ini"
	target.write_text("original\n", encoding="utf-8")
	done = run(
		tmp_path,
		f"""
		import errno
		import stat
		from fileUtils import FaultTolerantFile

		realFsync = os.fsync


		def fsync(fd):
			fd = fd if isinstance(fd, int) else fd.fileno()
			if stat.S_ISDIR(os.fstat(fd).st_mode):
				raise OSError(errno.EINVAL, "Invalid argument")
			realFsync(fd)


		os.fsync = fsync
		with FaultTolerantFile({str(target)!r}) as f:
			f.write(b"replacement\\n")
		""",
	)
	done.assertClean()
	assert target.read_text(encoding="utf-8") == "replacement\n"


# Upgrading a schema 13 configuration looks the old speech output device up among the audio devices,
# which A5's Linux utils.mmdevice provides. Until then the upgrade fails and the configuration is reset.
@pytest.mark.xfail(strict=True, reason="needs a Linux utils.mmdevice.getOutputDevices, from A5")
def test_olderWindowsConfigurationUpgrades(tmp_path):
	configDir = tmp_path / "xdgConfig" / "basedreader"
	configDir.mkdir(parents=True)
	(configDir / "nvda.ini").write_text(
		"schemaVersion = 13\n[speech]\n\toutputDevice = Speakers (Realtek High Definition Audio)\n"
		"\tsymbolLevel = 300\n",
		encoding="utf-8",
	)
	done = run(
		tmp_path,
		"""
		config = startConfig()
		result = {
			"baseConfigError": config.conf.baseConfigError,
			"entries": sorted(os.listdir(config.conf.profiles[0].filename.rpartition("/")[0])),
			"symbolLevel": config.conf["speech"]["symbolLevel"],
			"outputDevice": config.conf["audio"]["outputDevice"],
		}
		""",
		env=xdgEnv(tmp_path),
	)
	done.assertClean()
	assert done.value == {
		"baseConfigError": False,
		"entries": ["addons", "nvda.ini", "profiles", "speechDicts"],
		"symbolLevel": 300,
		"outputDevice": "default",
	}


def test_profileIsCreatedActivatedAndSaved(tmp_path):
	env = xdgEnv(tmp_path)
	created = run(
		tmp_path,
		"""
		# Creating a profile registers a command to switch to it, from globalCommands.
		config = startCore()
		config.conf.createProfile("work")
		config.conf.manualActivateProfile("work")
		config.conf["speech"]["symbolLevel"] = 200
		inProfile = config.conf["speech"]["symbolLevel"]
		config.conf.manualActivateProfile(None)
		inBase = config.conf["speech"]["symbolLevel"]
		config.conf.save()
		from NVDAState import WritePaths

		result = {
			"inProfile": inProfile,
			"inBase": inBase,
			"profiles": sorted(os.listdir(WritePaths.profilesDir)),
			"listed": list(config.conf.listProfiles()),
		}
		""",
		env=env,
	)
	created.assertClean()
	assert created.value == {"inProfile": 200, "inBase": 100, "profiles": ["work.ini"], "listed": ["work"]}
	activated = run(
		tmp_path,
		"""
		config = startConfig()
		inBase = config.conf["speech"]["symbolLevel"]
		config.conf.manualActivateProfile("work")
		result = {"inBase": inBase, "inProfile": config.conf["speech"]["symbolLevel"]}
		""",
		env=env,
	)
	activated.assertClean()
	assert activated.value == {"inBase": 100, "inProfile": 200}


def test_polishCatalogueLoadsFromLang(tmp_path):
	done = run(
		tmp_path,
		"""
		import locale
		import languageHandler

		languageHandler.setLanguage("Windows")
		result = {
			"language": languageHandler.getLanguage(),
			"off": _("Off"),
			"withContext": pgettext("languageName", "Aragonese"),
			"cLocale": locale.setlocale(locale.LC_ALL),
			"described": languageHandler.getLanguageDescription("pt_BR"),
		}
		""",
		env={**NO_LOCALE, "LANG": "pl_PL.UTF-8"},
	)
	done.assertClean()
	# Named with its region, as NVDA on a Polish Windows names it; the catalogue is the plain "pl" one.
	assert done.value["language"] == "pl_PL"
	assert done.value["off"] == "Wyłączone"
	assert done.value["withContext"] == "Aragoński"
	assert done.value["cLocale"] == ("pl_PL.UTF-8" if isLocaleInstalled("pl_PL.UTF-8") else "C.UTF-8")
	# Named in the desktop's language, as Windows names languages in the user's display language.
	assert done.value["described"] == "portugalski (Brazylia)"


def test_interfaceLanguageFollowsPosixPrecedence(tmp_path):
	# Each case: the environment, what getWindowsLanguage reports, and the interface language that
	# setLanguage("Windows") then settles on.
	cases = [
		# A language is reported with its region, as Windows reports it, and its catalogue is found.
		({"LANG": "de_AT.UTF-8"}, "de_AT", "de_AT"),
		({"LANG": "pt_BR.UTF-8"}, "pt_BR", "pt_BR"),
		({"LANG": "en_GB.UTF-8"}, "en_GB", "en"),
		({"LANG": "sr_RS.UTF-8@latin"}, "sr_RS", "sr_RS"),
		({"LANG": "pl"}, "pl", "pl"),
		({"LANG": "en_GB.UTF-8", "LC_MESSAGES": "pl_PL.UTF-8"}, "pl_PL", "pl_PL"),
		({"LANG": "de_DE.UTF-8", "LC_ALL": "fr_FR.UTF-8", "LC_MESSAGES": "pl_PL.UTF-8"}, "fr_FR", "fr_FR"),
		({"LANG": "pl_PL.UTF-8", "LC_MESSAGES": "C"}, "en", "en"),
		# LANGUAGE is a list; an entry with no catalogue gives way to the next.
		({"LANG": "en_GB.UTF-8", "LANGUAGE": "eo:pt_BR:pl"}, "pt_BR", "pt_BR"),
		# A language listed alone finds a catalogue NVDA has only for a region: the language's usual
		# region where glibc's aliases name one, or the first by name.
		({"LANG": "en_US.UTF-8", "LANGUAGE": "nb"}, "nb_NO", "nb_NO"),
		({"LANG": "en_US.UTF-8", "LANGUAGE": "pt:pl"}, "pt_PT", "pt_PT"),
		({"LANG": "en_US.UTF-8", "LANGUAGE": "zh:pl"}, "zh_CN", "zh_CN"),
		({"LANG": "polish"}, "pl_PL", "pl_PL"),
		# glibc ignores LANGUAGE under the C locale, and so does NVDA.
		({"LANG": "C.UTF-8", "LANGUAGE": "pl"}, "en", "en"),
		({"LANG": "pl_PL.UTF-8", "LC_ALL": "C", "LANGUAGE": "de"}, "en", "en"),
		({}, "en", "en"),
		# A language NVDA has no translation for is still the user's language, as on Windows, and only the
		# interface falls back: to English, or to the language's usual region where gettext's own aliases
		# find one, as they do on Windows.
		({"LANG": "zh_SG.UTF-8"}, "zh_SG", "zh"),
		({"LANG": "xx_YY.UTF-8"}, "xx_YY", "en"),
	]
	done = run(
		tmp_path,
		f"""
		import languageHandler

		result = []
		for env, _reported, _interface in {cases!r}:
			for name in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
				os.environ.pop(name, None)
			os.environ.update(env)
			reported = languageHandler.getWindowsLanguage()
			languageHandler.setLanguage("Windows")
			result.append([env, reported, languageHandler.getLanguage()])
		""",
	)
	done.assertClean()
	assert done.value == [[env, reported, interface] for env, reported, interface in cases]


def test_localeFallsBackToCUtf8(tmp_path):
	done = run(
		tmp_path,
		"""
		import locale
		import languageHandler

		results = {}
		for language in ("sq", "en", "pl", "pt_BR"):
			languageHandler.setLocale(language)
			results[language] = locale.setlocale(locale.LC_ALL)
		result = results
		""",
		env={**NO_LOCALE, "LANG": "en_GB.UTF-8"},
	)
	done.assertClean()
	expected = {
		# The user's own locale is used for their own language.
		"en": "en_GB.UTF-8",
		"sq": "sq_AL.UTF-8",
		"pl": "pl_PL.UTF-8",
		"pt_BR": "pt_BR.UTF-8",
	}
	for language, name in expected.items():
		if not isLocaleInstalled(name):
			expected[language] = "C.UTF-8"
	assert done.value == expected


def test_everyListedLanguageIsDescribed(tmp_path):
	done = run(
		tmp_path,
		"""
		import languageHandler

		languageHandler.setLanguage("en")
		result = {
			"languages": languageHandler.getAvailableLanguages(presentational=True),
			"polish": languageHandler.getLanguageDescription("pl"),
			"northernKurdish": languageHandler.getLanguageDescription("kmr"),
			"unknown": languageHandler.getLanguageDescription("xx"),
			"imperial": languageHandler.useImperialMeasurements(),
			"lcidName": languageHandler.windowsLCIDToLocaleName(0x0415),
			"centralKurdish": languageHandler.windowsLCIDToLocaleName(0x0492),
			"lcid": languageHandler.localeNameToWindowsLCID("pl_PL"),
		}
		""",
		env={**NO_LOCALE, "LANG": "en_GB.UTF-8"},
	)
	done.assertClean()
	languages = dict(done.value["languages"])
	assert len(languages) > 50, languages
	assert languages.pop("Windows") == "User default"
	undescribed = [code for code, label in languages.items() if label == code]
	assert not undescribed
	assert languages["pl"] == "Polish, pl"
	assert languages["pt_BR"] == "Portuguese (Brazil), pt_BR"
	assert done.value["polish"] == "Polish"
	# NVDA's translators' own name wins over ICU's plain "Kurdish".
	assert done.value["northernKurdish"] == "Northern Kurdish"
	assert done.value["unknown"] is None
	assert done.value["imperial"] is False
	assert done.value["lcidName"] == "pl_PL"
	assert done.value["centralKurdish"] == "ckb"
	assert done.value["lcid"] == 0


def test_languagesWithoutIcuAreListedByCode(tmp_path):
	done = run(
		tmp_path,
		"""
		import languageHandler

		languageHandler.setLanguage("en")
		languages = dict(languageHandler.getAvailableLanguages(presentational=True))
		result = {
			"german": languageHandler.getLanguageDescription("de"),
			"germanListed": languages["de"],
			"northernKurdish": languageHandler.getLanguageDescription("kmr"),
			"warnings": [w for w in warnings if "ICU" in w],
		}
		""",
		env={
			**NO_LOCALE,
			"LANG": "en_GB.UTF-8",
			"BASEDREADER_ICU_LIB_DIR": "",
			"BASEDREADER_ICU_VERSION_MAJOR": "",
		},
	)
	done.assertClean()
	value = done.value
	# As upstream's getLanguageDescription does for a language nothing can name.
	assert value["german"] is None
	assert value["germanListed"] == "de"
	# NVDA's translators' own names need no ICU.
	assert value["northernKurdish"] == "Northern Kurdish"
	assert len(value["warnings"]) == 1, value


def test_badOptionIsReportedOnStandardError(tmp_path):
	done = run(
		tmp_path,
		"""
		import argsParsing

		try:
			argsParsing.getParser().parse_known_args(["--log-level", "loud"])
		except SystemExit as e:
			result = e.code
		""",
	)
	done.assertClean()
	assert done.value == 2
	assert "usage:" in done.stderr
	assert "error: argument -l/--log-level" in done.stderr
	assert done.stdout == ""


def test_helpGoesToStandardOutput(tmp_path):
	done = run(
		tmp_path,
		"""
		import argsParsing

		try:
			argsParsing.getParser().parse_known_args(["--help"])
		except SystemExit as e:
			result = e.code
		""",
	)
	done.assertClean()
	assert done.value == 0
	assert "--config-path" in done.stdout


def test_windowsOnlyOptionsStillParse(tmp_path):
	done = run(
		tmp_path,
		"""
		import argsParsing

		args, unknown = argsParsing.getParser().parse_known_args(
			[
				"--ease-of-access",
				"--no-sr-flag",
				"--install-silent",
				"--enable-start-on-logon",
				"False",
				"--portable-path",
				"/somewhere",
				"--lang",
				"Windows",
				"--some-unknown-flag",
			],
		)
		result = {
			"easeOfAccess": args.easeOfAccess,
			"changeScreenReaderFlag": args.changeScreenReaderFlag,
			"installSilent": args.installSilent,
			"enableStartOnLogon": args.enableStartOnLogon,
			"portablePath": args.portablePath,
			"language": args.language,
			"unknown": unknown,
		}
		""",
	)
	done.assertClean()
	assert done.value == {
		"easeOfAccess": True,
		"changeScreenReaderFlag": False,
		"installSilent": True,
		"enableStartOnLogon": False,
		"portablePath": "/somewhere",
		"language": "Windows",
		"unknown": ["--some-unknown-flag"],
	}


def test_registryQuestionsAnswerWithoutARegistry(tmp_path):
	done = run(
		tmp_path,
		"""
		import config
		import NVDAState
		import systemUtils
		import winVersion
		from utils.security import isRunningOnSecureDesktop
		from winAPI.sessionTracking import isLockScreenModeActive

		version = winVersion.getWinVer()
		result = {
			"isInstalledCopy": config.isInstalledCopy(),
			"installedUserConfigPath": config.getInstalledUserConfigPath(),
			"forceSecureMode": NVDAState._forceSecureModeEnabled(),
			"serviceDebug": NVDAState._serviceDebugEnabled(),
			"clockSeconds": systemUtils._isSystemClockSecondsVisible(),
			"syswow64": systemUtils.hasSyswow64Dir(),
			"uiAccess": systemUtils.hasUiAccess(),
			"secureDesktop": isRunningOnSecureDesktop(),
			"lockScreen": isLockScreenModeActive(),
			"version": [version.major, version.minor, version.build],
			"belowWindows10": version < winVersion.WIN10,
			"versionText": repr(version),
			"architecture": version.processorArchitecture,
		}
		systemUtils.resetThreadExecutionState()
		""",
		env={"XDG_SESSION_CLASS": "user"},
	)
	done.assertClean()
	value = done.value
	assert value.pop("versionText").startswith("Linux ")
	assert value.pop("architecture") in ("AMD64", "ARM64", os.uname().machine)
	assert value == {
		"isInstalledCopy": False,
		"installedUserConfigPath": None,
		"forceSecureMode": False,
		"serviceDebug": False,
		"clockSeconds": False,
		"syswow64": False,
		"uiAccess": True,
		"secureDesktop": False,
		"lockScreen": False,
		"version": [0, 0, 0],
		"belowWindows10": True,
	}


def test_loginScreenIsTheSecureDesktop(tmp_path):
	done = run(
		tmp_path,
		"""
		startCore()
		from utils.security import isRunningOnSecureDesktop
		from winAPI.sessionTracking import isLockScreenModeActive

		result = [isRunningOnSecureDesktop(), isLockScreenModeActive()]
		""",
		env={**xdgEnv(tmp_path), "XDG_SESSION_CLASS": "greeter"},
	)
	done.assertClean()
	assert done.value == [True, False]


def test_unbuiltServicesFailLoudly(tmp_path):
	done = run(
		tmp_path,
		"""
		import easeOfAccess

		try:
			easeOfAccess.setAutoStart(easeOfAccess.AutoStartContext.AFTER_LOGON, True)
			result = "returned"
		except NotImplementedError as e:
			result = type(e).__name__
		""",
	)
	assert done.error is None, done.error
	assert done.value == "NoLinuxImplementation"
	assert done.noLinuxImplementation == ["easeOfAccess.setAutoStart(AFTER_LOGON)"]
	assert not done.logErrors


def test_sayAllStartsReadingFromTheCaret(tmp_path):
	done = run(
		tmp_path,
		"""
		startCore()
		import api
		from speech import sayAll
		from speech.speech import SpeakTextInfoState
		from textInfos.offsets import OffsetsTextInfo


		class Info(OffsetsTextInfo):
			def _getStoryText(self):
				return "Say all reads on from the caret.\\n"

			def _getStoryLength(self):
				return len(self._getStoryText())

			def _getCaretOffset(self):
				return 4


		class Document:
			def makeTextInfo(self, position):
				return Info(self, position)


		document = Document()
		api.getCaretObject = lambda: document
		started = []
		# Reading a line is speech's part; the reader has started once it is asked for the first line.
		sayAll._CaretTextReader.next = lambda reader: started.append(reader)
		handler = sayAll.SayAllHandler = sayAll._SayAllHandler(None, None, None, SpeakTextInfoState)
		handler.readText(sayAll.CURSOR.CARET)
		handler.readText(sayAll.CURSOR.CARET)
		running = handler.isRunning()
		result = {
			"readers": [type(reader).__name__ for reader in started],
			"startOffset": started[0].reader._startOffset,
			"running": running,
			"idleWarnings": [w for w in warnings if "idle" in w],
		}
		for reader in started:
			reader.stop()
		""",
		env=xdgEnv(tmp_path),
	)
	done.assertClean()
	value = done.value
	assert value["readers"] == ["_CaretTextReader", "_CaretTextReader"]
	assert value["startOffset"] == 4
	assert value["running"] is True
	# Missing idle inhibition is said once in a run, not once for each say all.
	assert len(value["idleWarnings"]) == 1, value


def test_brailleKeyRunsItsScript(tmp_path):
	done = run(
		tmp_path,
		"""
		startCore()
		import api
		import inputCore
		import queueHandler
		import scriptHandler
		from braille.display.gesture import BrailleDisplayGesture
		from speech import sayAll


		class Focus:
			sleepMode = False
			SLEEP_FULL = "full"


		class NotReading:
			def isRunning(self):
				return False


		api.getFocusObject = lambda: Focus()
		sayAll.SayAllHandler = NotReading()
		queued = []
		queueHandler.queueFunction = lambda queue, func, *args, **kwargs: queued.append(func.__name__)
		scriptHandler.queueScript = lambda script, gesture: queued.append(script.__name__)


		def script_testDisplayKey(gesture):
			pass


		class Key(BrailleDisplayGesture):
			source = "testDisplay"
			model = ""
			id = "d1"

			def _get_script(self):
				return script_testDisplayKey


		manager = inputCore.InputManager.__new__(inputCore.InputManager)
		manager._captureFunc = None
		manager.lastModifierWasInSayAll = False
		manager.executeGesture(Key())
		manager.executeGesture(Key())
		idleWarnings = [w for w in warnings if "idle" in w]
		result = {"preventsIdle": Key.shouldPreventSystemIdle, "queued": queued, "idleWarnings": len(idleWarnings)}
		""",
		env=xdgEnv(tmp_path),
	)
	done.assertClean()
	assert done.value == {
		"preventsIdle": True,
		"queued": ["cancelSpeech", "script_testDisplayKey"] * 2,
		"idleWarnings": 1,
	}


def test_execAndPumpWaitsForItsWork(tmp_path):
	done = run(
		tmp_path,
		"""
		import threading
		import time
		import systemUtils

		finished = threading.Event()


		def work(a, b=0):
			time.sleep(0.3)
			finished.set()
			return a + b


		pump = systemUtils.ExecAndPump(work, 40, b=2)
		finishedOnReturn = finished.is_set()


		def fail():
			time.sleep(0.1)
			raise ValueError("the work failed")


		try:
			systemUtils.ExecAndPump(fail)
			raised = None
		except ValueError as e:
			raised = str(e)
		result = {"funcRes": pump.funcRes, "finishedOnReturn": finishedOnReturn, "raised": raised}
		""",
	)
	done.assertClean()
	assert done.value == {"funcRes": 42, "finishedOnReturn": True, "raised": "the work failed"}


def test_execAndPumpKeepsTheGuiResponsive(tmp_path):
	done = run(
		tmp_path,
		"""
		import threading
		import wx
		import systemUtils

		app = wx.App()
		handledWhileWaiting = threading.Event()
		wx.CallAfter(handledWhileWaiting.set)
		# The work only finishes promptly if wx handled the event while ExecAndPump waited for it.
		pump = systemUtils.ExecAndPump(lambda: handledWhileWaiting.wait(10))
		result = pump.funcRes
		""",
	)
	done.assertClean()
	assert done.value is True


def test_sessionTrackingAnnouncesLockChanges(tmp_path):
	done = run(
		tmp_path,
		"""
		startCore()
		import time
		from baseObject import AutoPropertyObject
		from NVDAState import _TrackNVDAInitialization
		from utils.security import post_sessionLockStateChanged
		from winAPI import sessionTracking

		announced = []


		# Extension points hold their handlers weakly, so this one is a named function that stays alive.
		def onLockStateChanged(isNowLocked):
			announced.append(isNowLocked)


		post_sessionLockStateChanged.register(onLockStateChanged)
		sessionTracking.initialize()
		sessionTracking.pumpAll()
		beforeStartUpEnds = sessionTracking.isLockScreenModeActive()
		_TrackNVDAInitialization.markInitializationComplete()
		sessionTracking.pumpAll()
		sessionTracking._querySessionLocked = lambda: True
		AutoPropertyObject.invalidateCaches()
		sessionTracking.pumpAll()
		sessionTracking.pumpAll()
		whileLocked = sessionTracking.isLockScreenModeActive()
		sessionTracking._querySessionLocked = lambda: False
		AutoPropertyObject.invalidateCaches()
		sessionTracking.pumpAll()
		count = 100000
		start = time.perf_counter()
		for _i in range(count):
			sessionTracking.isLockScreenModeActive()
		perCall = (time.perf_counter() - start) / count
		result = {
			"announced": announced,
			"beforeStartUpEnds": beforeStartUpEnds,
			"whileLocked": whileLocked,
			"warnings": len([w for w in warnings if "locked" in w]),
			"perCallMicroseconds": perCall * 1e6,
		}
		""",
		env={**xdgEnv(tmp_path), "XDG_SESSION_CLASS": "user"},
	)
	done.assertClean()
	value = done.value
	assert value["announced"] == [True, False]
	assert value["beforeStartUpEnds"] is False
	assert value["whileLocked"] is True
	assert value["warnings"] == 1
	# Asked on every object construction, so it must stay far below a millisecond.
	assert value["perCallMicroseconds"] < 50, value


def test_autostartFollowsXdgAutostartEntries(tmp_path):
	user = tmp_path / "xdgConfig" / "autostart"
	system = tmp_path / "xdgSystem" / "autostart"
	user.mkdir(parents=True)
	system.mkdir(parents=True)
	entry = "[Desktop Entry]\nType=Application\nName=Basedreader\nExec=basedreader\n"
	done = run(
		tmp_path,
		f"""
		import easeOfAccess

		user = {str(user)!r}
		system = {str(system)!r}
		context = easeOfAccess.AutoStartContext


		def write(directory, text):
			with open(os.path.join(directory, "basedreader.desktop"), "w", encoding="utf-8") as f:
				f.write(text)


		result = {{"none": easeOfAccess.willAutoStart(context.AFTER_LOGON)}}
		write(system, {entry!r})
		result["system"] = easeOfAccess.willAutoStart(context.AFTER_LOGON)
		write(user, {entry!r} + "Hidden=true\\n")
		result["hiddenByUser"] = easeOfAccess.willAutoStart(context.AFTER_LOGON)
		write(user, {entry!r} + "X-MATE-Autostart-enabled=false\\n")
		result["disabledForMate"] = easeOfAccess.willAutoStart(context.AFTER_LOGON)
		write(user, {entry!r} + "X-GNOME-Autostart-enabled=false\\n")
		result["disabledForGnome"] = easeOfAccess.willAutoStart(context.AFTER_LOGON)
		write(user, {entry!r} + "OnlyShowIn=GNOME;KDE;\\n")
		result["onlyForOthers"] = easeOfAccess.willAutoStart(context.AFTER_LOGON)
		write(user, {entry!r} + "OnlyShowIn=GNOME;MATE;\\n")
		result["onlyForMate"] = easeOfAccess.willAutoStart(context.AFTER_LOGON)
		write(user, {entry!r} + "NotShowIn=MATE;\\n")
		result["notForMate"] = easeOfAccess.willAutoStart(context.AFTER_LOGON)
		write(user, {entry!r} + "TryExec=/nonexistent/basedreader\\n")
		result["notInstalled"] = easeOfAccess.willAutoStart(context.AFTER_LOGON)
		write(user, {entry!r} + "TryExec=sh\\n")
		result["installed"] = easeOfAccess.willAutoStart(context.AFTER_LOGON)
		write(user, {entry!r})
		result["user"] = easeOfAccess.willAutoStart(context.AFTER_LOGON)
		result["logonScreen"] = easeOfAccess.willAutoStart(context.ON_LOGON_SCREEN)
		result["registered"] = easeOfAccess.isRegistered()
		easeOfAccess.notify(3)
		import config

		result["startAfterLogon"] = config.getStartAfterLogon()
		""",
		env={**xdgEnv(tmp_path), "XDG_CURRENT_DESKTOP": "MATE"},
	)
	done.assertClean()
	assert done.value == {
		"none": False,
		"system": True,
		"hiddenByUser": False,
		"disabledForMate": False,
		"disabledForGnome": True,
		"onlyForOthers": False,
		"onlyForMate": True,
		"notForMate": False,
		"notInstalled": False,
		"installed": True,
		"user": True,
		"logonScreen": False,
		"registered": False,
		"startAfterLogon": True,
	}


def test_configurationDirectoryOpensInTheDesktopsFileManager(tmp_path):
	# A stand-in for xdg-open that only records what it was asked to open.
	fakeBin = tmp_path / "bin"
	fakeBin.mkdir()
	opened = tmp_path / "opened"
	xdgOpen = fakeBin / "xdg-open"
	xdgOpen.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" >> {opened}\n', encoding="utf-8")
	xdgOpen.chmod(0o755)
	configDir = tmp_path / "config"
	done = run(
		tmp_path,
		f"""
		import time
		import systemUtils
		from NVDAState import WritePaths

		WritePaths.configDir = {str(configDir)!r}
		systemUtils.openUserConfigurationDirectory()
		systemUtils.openDefaultConfigurationDirectory()
		deadline = time.monotonic() + 10
		lines = []
		while time.monotonic() < deadline and len(lines) < 2:
			time.sleep(0.05)
			if os.path.exists({str(opened)!r}):
				with open({str(opened)!r}, encoding="utf-8") as f:
					lines = f.read().splitlines()
		result = lines
		""",
		env={**xdgEnv(tmp_path), "PATH": f"{fakeBin}:{os.environ['PATH']}"},
	)
	done.assertClean()
	assert done.value == [str(configDir), str(tmp_path / "xdgConfig" / "basedreader")]
	# Opening the default directory creates it first, as upstream does.
	assert (tmp_path / "xdgConfig" / "basedreader" / "profiles").is_dir()


def test_messageWindowNeedsNoWindow(tmp_path):
	done = run(
		tmp_path,
		"""
		startCore()
		import buildVersion
		from winAPI.messageWindow import _MessageWindow

		window = _MessageWindow(buildVersion.name)
		handle = window.handle
		window.destroy()
		del window
		result = handle
		""",
		env=xdgEnv(tmp_path),
	)
	done.assertClean()
	assert done.value is None
