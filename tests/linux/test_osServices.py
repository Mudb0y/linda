"""Paths, saved configuration, language, the command line and the operating system's answers on Linux.

nix develop . -c tests/linux/rig python -m pytest tests/linux/test_osServices.py
"""

import logging
import os
import stat
import textwrap
from pathlib import Path

import harness

#: The steps of core.main that the tests take one at a time, on top of what nvda.pyw establishes
#: before it, which runChild's appDir level sets up.
PRELUDE = """
import os

import globalVars


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

#: The rig's environment holds LANG=C.UTF-8 and no other locale variable. A test that gives a child
#: its own locale clears all of them first, so that only the variables it names decide.
NO_LOCALE = {name: "" for name in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LC_MEASUREMENT", "LANG")}


def run(tmpPath: Path, body: str, env: dict[str, str] | None = None) -> harness.ChildResult:
	"""Run body after PRELUDE, keeping every record logged at WARNING or above in logRecords."""
	return harness.runChild(
		tmpPath,
		PRELUDE + textwrap.dedent(body),
		prepare="appDir",
		env=env,
		recordLevel=logging.WARNING,
	)


def warnings(done: harness.ChildResult, about: str) -> list[str]:
	"""The warnings the run logged whose text holds about."""
	return [r for r in done.logRecords if r.startswith("WARNING: ") and about in r]


def xdgEnv(tmpPath: Path) -> dict[str, str]:
	return {"XDG_CONFIG_HOME": str(tmpPath / "xdgConfig"), "XDG_CONFIG_DIRS": str(tmpPath / "xdgSystem")}


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


# Upgrading a schema 13 configuration looks the old speech output device up among the audio devices.
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
	assert done.value["cLocale"] == "pl_PL.UTF-8"
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
		for language in ("sq", "en", "pl", "pt_BR", "tlh", "pl_XX"):
			languageHandler.setLocale(language)
			results[language] = locale.setlocale(locale.LC_ALL)
		result = results
		""",
		env={**NO_LOCALE, "LANG": "en_GB.UTF-8"},
	)
	done.assertClean()
	# The dev shell's locale archive holds every locale glibc has, so each that exists is found.
	assert done.value == {
		# The user's own locale is used for their own language.
		"en": "en_GB.UTF-8",
		# A language alone gets its usual region.
		"sq": "sq_AL.UTF-8",
		"pl": "pl_PL.UTF-8",
		"pt_BR": "pt_BR.UTF-8",
		# glibc has no Klingon, and no Polish for a region that does not exist.
		"tlh": "C.UTF-8",
		"pl_XX": "C.UTF-8",
	}


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
	assert len(warnings(done, "ICU")) == 1, done.logRecords


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
	done.assertClean(ignoring={"stderr"})
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
	done.assertClean(ignoring={"noLinuxImplementation"})
	assert done.value == "NoLinuxImplementation"
	assert done.noLinuxImplementation == ["easeOfAccess.setAutoStart(AFTER_LOGON)"]


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
	assert len(warnings(done, "idle")) == 1, done.logRecords


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
		result = {"preventsIdle": Key.shouldPreventSystemIdle, "queued": queued}
		""",
		env=xdgEnv(tmp_path),
	)
	done.assertClean()
	assert done.value == {
		"preventsIdle": True,
		"queued": ["cancelSpeech", "script_testDisplayKey"] * 2,
	}
	assert len(warnings(done, "idle")) == 1, done.logRecords


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
	assert len(warnings(done, "locked")) == 1, done.logRecords
	# Asked on every object construction, so it must stay far below a millisecond.
	assert value["perCallMicroseconds"] < 200, value


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
	# Neither call waits for xdg-open, so the two may record in either order.
	assert sorted(done.value) == sorted([str(configDir), str(tmp_path / "xdgConfig" / "basedreader")])
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


def test_measurementSystemFollowsTheMeasurementLocale(tmp_path):
	# Each case: the environment, and whether NVDA reports measurements in imperial units.
	cases = [
		({"LANG": "en_US.UTF-8"}, True),
		({"LANG": "en_GB.UTF-8"}, False),
		({"LANG": "en_GB.UTF-8", "LC_MEASUREMENT": "en_US.UTF-8"}, True),
		({"LANG": "en_US.UTF-8", "LC_MEASUREMENT": "pl_PL.UTF-8"}, False),
		({"LC_ALL": "en_US.UTF-8", "LC_MEASUREMENT": "en_GB.UTF-8"}, True),
		# A locale glibc does not have measures as the C locale does, in metric.
		({"LANG": "tlh_XX.UTF-8"}, False),
		({}, False),
	]
	done = run(
		tmp_path,
		f"""
		import languageHandler

		# NVDA sets the process's locale to its interface language; the user's units must not follow it.
		languageHandler.setLocale("pl")
		result = []
		for env, _imperial in {cases!r}:
			for name in ("LC_ALL", "LC_MEASUREMENT", "LANG"):
				os.environ.pop(name, None)
			os.environ.update(env)
			result.append([env, languageHandler.useImperialMeasurements()])
		""",
		env=NO_LOCALE,
	)
	done.assertClean()
	assert done.value == [[env, imperial] for env, imperial in cases]


def test_localeHelpersAnswerInEnglishFromIcu(tmp_path):
	done = run(
		tmp_path,
		"""
		import languageHandler
		from localesData import LANG_NAMES_TO_LOCALIZED_DESCS

		languageHandler.setLanguage("pl")
		codes = ("en", "de", "ne", "pt-BR", "pt_PT", "de_CH", "nb_NO", "Ne", "zh_HK", "xx", "pl_XX")
		result = {
			"languages": {code: languageHandler.englishLanguageNameFromNVDALocale(code) for code in codes},
			"countries": {code: languageHandler.englishCountryNameFromNVDALocale(code) for code in codes},
			# Upstream's unit tests ask this of every language NVDA names itself.
			"unnamed": [
				code
				for code in LANG_NAMES_TO_LOCALIZED_DESCS
				if languageHandler.englishLanguageNameFromNVDALocale(code) is None
			],
		}
		for name, ask in (
			("codePage", lambda: languageHandler.ansiCodePageFromNVDALocale("pl_PL")),
			("localeString", lambda: languageHandler.localeStringFromLocaleCode("pl_PL")),
		):
			try:
				result[name] = ask()
			except NotImplementedError as e:
				result[name] = type(e).__name__
		""",
		env={**NO_LOCALE, "LANG": "pl_PL.UTF-8"},
	)
	done.assertClean(ignoring={"noLinuxImplementation"})
	value = done.value
	# In English whatever the interface language, as Windows' SENGLISH names are.
	assert value["languages"] == {
		"en": "English",
		"de": "German",
		"ne": "Nepali",
		"pt-BR": "Portuguese",
		"pt_PT": "Portuguese",
		"de_CH": "German",
		"nb_NO": "Norwegian Bokmål",
		"Ne": "Nepali",
		"zh_HK": "Chinese",
		"xx": None,
		"pl_XX": "Polish",
	}
	# A language alone is taken in its likeliest country, as Windows takes it.
	assert value["countries"] == {
		"en": "United States",
		"de": "Germany",
		"ne": "Nepal",
		"pt-BR": "Brazil",
		"pt_PT": "Portugal",
		"de_CH": "Switzerland",
		"nb_NO": "Norway",
		"Ne": "Nepal",
		"zh_HK": "Hong Kong SAR China",
		"xx": None,
		"pl_XX": None,
	}
	assert value["unnamed"] == []
	# Windows' ANSI code pages exist only to name Windows C runtime locales.
	assert value["codePage"] == "NoLinuxImplementation"
	assert value["localeString"] == "NoLinuxImplementation"
	assert done.noLinuxImplementation == ["languageHandler.ansiCodePageFromNVDALocale"] * 2


def test_helpNamesTheLinuxLogFile(tmp_path):
	stateHome = tmp_path / "state%dir"
	done = run(
		tmp_path,
		"""
		import argsParsing
		import logHandler

		try:
			argsParsing.getParser().parse_known_args(["--help"])
		except SystemExit:
			pass
		result = logHandler._getDefaultLogFilePath()
		""",
		env={"XDG_STATE_HOME": str(stateHome)},
	)
	done.assertClean()
	assert done.value == str(stateHome / "basedreader" / "nvda.log")
	assert f'Default destination is "{done.value}".' in done.stdout
	assert "TEMP" not in done.stdout


def test_windowsOnlyNamesFailLoudly(tmp_path):
	done = run(
		tmp_path,
		"""
		import sys

		# Every module that imports the stand-in.
		startCore()
		import config.registry
		import installer
		import hwPortUtils
		import NVDAState
		import systemUtils
		import winVersion
		from _remoteClient import localMachine, urlHandler
		from gui.addonStoreGui.viewModels import store
		from linuxPlatform.windowsOnly import winreg

		key = winreg.HKEY_LOCAL_MACHINE
		combined = winreg.KEY_READ | winreg.KEY_WOW64_64KEY
		uses = {
			"function": lambda: winreg.OpenKey,
			"type": lambda: winreg.HKEYType,
			"call": lambda: key(),
			"truth": lambda: bool(key),
			"number": lambda: int(key),
			"comparison": lambda: key == 0x80000002,
			"text": lambda: f"{key}",
			"attribute": lambda: key.Close,
			"and": lambda: key & 0x100,
			"invert": lambda: ~key,
			"add": lambda: key + 1,
			"hasattr": lambda: hasattr(winreg, "OpenKey"),
			"getattr": lambda: getattr(winreg, "OpenKey", None),
			"combined": lambda: int(combined),
			"combinedWithNumber": lambda: int(winreg.KEY_READ | 0x100),
		}
		outcomes = {}
		for name, use in uses.items():
			try:
				use()
				outcomes[name] = "returned"
			except NotImplementedError as e:
				outcomes[name] = str(e)
		result = {
			"outcomes": outcomes,
			"sameEachTime": key is winreg.HKEY_LOCAL_MACHINE,
			"repr": repr(key),
			"probedUnderscore": hasattr(winreg, "__wrapped__"),
			# The standard library takes a winreg it finds there for Windows.
			"registered": [name for name in ("winreg", "msvcrt", "_winapi") if name in sys.modules],
		}
		""",
	)
	done.assertClean(ignoring={"noLinuxImplementation"})
	value = done.value
	assert value["outcomes"] == {
		"function": "winreg.OpenKey has no Linux implementation",
		"type": "winreg.HKEYType has no Linux implementation",
		"call": "winreg.HKEY_LOCAL_MACHINE has no Linux implementation",
		"truth": "winreg.HKEY_LOCAL_MACHINE has no Linux implementation",
		"number": "winreg.HKEY_LOCAL_MACHINE has no Linux implementation",
		"comparison": "winreg.HKEY_LOCAL_MACHINE has no Linux implementation",
		"text": "winreg.HKEY_LOCAL_MACHINE has no Linux implementation",
		"attribute": "winreg.HKEY_LOCAL_MACHINE.Close has no Linux implementation",
		"and": "winreg.HKEY_LOCAL_MACHINE has no Linux implementation",
		"invert": "winreg.HKEY_LOCAL_MACHINE has no Linux implementation",
		"add": "winreg.HKEY_LOCAL_MACHINE has no Linux implementation",
		"hasattr": "winreg.OpenKey has no Linux implementation",
		"getattr": "winreg.OpenKey has no Linux implementation",
		"combined": "winreg.KEY_READ combined with winreg.KEY_WOW64_64KEY has no Linux implementation",
		"combinedWithNumber": "winreg.KEY_READ combined with 256 has no Linux implementation",
	}
	assert value["sameEachTime"] is True
	assert value["repr"] == "<winreg.HKEY_LOCAL_MACHINE, which Linux does not have>"
	assert value["probedUnderscore"] is False
	assert value["registered"] == []
	assert len(done.noLinuxImplementation) == len(value["outcomes"])


def test_registryPathsRaiseNoLinuxImplementation(tmp_path):
	done = run(
		tmp_path,
		"""
		startCore()
		import config.registry
		import NVDAState
		from _remoteClient import urlHandler
		from _remoteClient.localMachine import LocalMachine, SoftwareSASGeneration

		paths = {
			# What a silent install asks first.
			"installDirX86": lambda: NVDAState.WritePaths._installDirX86,
			"startMenuFolder": lambda: NVDAState.WritePaths.startMenuFolder,
			"deleteKey": lambda: config.registry._deleteKeyAndSubkeys(0, "key"),
			"registerURLHandler": urlHandler.registerURLHandler,
		}
		result = {}
		for name, ask in paths.items():
			try:
				ask()
				result[name] = "returned"
			except NotImplementedError as e:
				result[name] = str(e)
			except Exception as e:
				result[name] = f"{type(e).__name__}: {e}"
		result["canSendSAS"] = LocalMachine._canSendSAS()
		result["sasKey"] = repr(SoftwareSASGeneration.KEY)
		""",
		env=xdgEnv(tmp_path),
	)
	done.assertClean(ignoring={"noLinuxImplementation"})
	assert done.value == {
		"installDirX86": "winreg.OpenKey has no Linux implementation",
		"startMenuFolder": "winreg.OpenKey has no Linux implementation",
		"deleteKey": "winreg.OpenKey has no Linux implementation",
		"registerURLHandler": "winreg.CreateKey has no Linux implementation",
		# Control+alt+delete is Windows' secure attention sequence; a remote request for it is refused.
		"canSendSAS": False,
		"sasKey": "<winreg.HKEY_LOCAL_MACHINE, which Linux does not have>",
	}
	assert done.noLinuxImplementation == ["winreg.OpenKey"] * 3 + ["winreg.CreateKey"]


def test_addonStoreOpensLinksWithTheDefaultApplication(tmp_path):
	fakeBin = tmp_path / "bin"
	fakeBin.mkdir()
	opened = tmp_path / "opened"
	xdgOpen = fakeBin / "xdg-open"
	xdgOpen.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" >> {opened}\n', encoding="utf-8")
	xdgOpen.chmod(0o755)
	done = run(
		tmp_path,
		f"""
		import pathlib
		import time

		startCore()
		from gui.addonStoreGui.viewModels import store


		def openedSoFar(count):
			deadline = time.monotonic() + 30
			lines = []
			while time.monotonic() < deadline and len(lines) < count:
				time.sleep(0.05)
				if os.path.exists({str(opened)!r}):
					with open({str(opened)!r}, encoding="utf-8") as f:
						lines = f.read().splitlines()
			return lines


		store.startfile("https://example.org/addon")
		openedSoFar(1)
		store.startfile(pathlib.Path("/somewhere/readme.txt"))
		try:
			store.startfile("/somewhere/readme.txt", "print")
			printed = "returned"
		except NotImplementedError as e:
			printed = str(e)
		result = {{"opened": openedSoFar(2), "printed": printed}}
		""",
		env={**xdgEnv(tmp_path), "PATH": f"{fakeBin}:{os.environ['PATH']}"},
	)
	done.assertClean(ignoring={"noLinuxImplementation"})
	assert done.value == {
		"opened": ["https://example.org/addon", "/somewhere/readme.txt"],
		"printed": "os.startfile(operation='print') has no Linux implementation",
	}
	assert done.noLinuxImplementation == ["os.startfile(operation='print')"]


def test_customWindowFailsAtItsFirstWindowsCall(tmp_path):
	done = run(
		tmp_path,
		"""
		startCore()
		import windowUtils


		class Window(windowUtils.CustomWindow):
			className = "test"

			def windowProc(self, hwnd, msg, wParam, lParam):
				return None


		try:
			Window()
			result = "created"
		except NotImplementedError as e:
			result = str(e)
		""",
	)
	done.assertClean(ignoring={"noLinuxImplementation"})
	assert done.value == "user32.RegisterClassExW is a Windows function with no Linux implementation"
	assert done.noLinuxImplementation == ["user32.RegisterClassExW"]


def test_chineseWordSegmentationIsUnavailableWithoutItsLibrary(tmp_path):
	done = run(
		tmp_path,
		"""
		startCore()
		from NVDAState import ReadPaths
		from textUtils._wordSeg.wordSegStrategy import ChineseWordSegmentationStrategy

		try:
			ReadPaths.cppjiebaDll
			raised = []
		except Exception as e:
			raised = [type(e).__name__, isinstance(e, NotImplementedError), isinstance(e, OSError)]
		# A user who sets Chinese word segmentation starts this with the library missing.
		ChineseWordSegmentationStrategy._initCppJieba(forceInit=True)
		result = {"raised": raised, "loaded": ChineseWordSegmentationStrategy._lib is not None}
		""",
		env=xdgEnv(tmp_path),
	)
	done.assertClean(ignoring={"noLinuxImplementation", "logErrors"})
	assert done.value == {"raised": ["NoWindowsLibrary", True, True], "loaded": False}
	assert done.noLinuxImplementation == ["NVDAState.ReadPaths.coreArchLibPath"] * 2
	# Upstream logs a library that will not load, on Windows too, and carries on without it.
	assert done.logErrors == ["ERROR: Failed to load cppjieba library"]


def test_lockedSessionWithholdsEverythingButTheDesktop(tmp_path):
	done = run(
		tmp_path,
		"""
		startCore()
		import api
		import controlTypes
		from baseObject import AutoPropertyObject
		from linuxPlatform.desktop import Desktop
		from NVDAObjects import NVDAObject
		from NVDAState import _TrackNVDAInitialization
		from utils.security import objectBelowLockScreenAndWindowsIsLocked as isWithheld
		from winAPI import sessionTracking


		class Mail(NVDAObject):
			processID = 4242
			role = controlTypes.Role.DOCUMENT
			name = "a private message"


		api.setDesktopObject(Desktop())
		sessionTracking.initialize()
		_TrackNVDAInitialization.markInitializationComplete()
		unlocked = [isWithheld(Mail()), isWithheld(Desktop())]
		sessionTracking._querySessionLocked = lambda: True
		AutoPropertyObject.invalidateCaches()
		locked = [isWithheld(Mail()), isWithheld(Desktop())]
		navigatorSet = api.setNavigatorObject(Mail())
		result = {"unlocked": unlocked, "locked": locked, "navigatorSet": navigatorSet}
		""",
		env={**xdgEnv(tmp_path), "XDG_SESSION_CLASS": "user"},
	)
	done.assertClean()
	assert done.value == {
		"unlocked": [False, False],
		# Nothing tells the screen locker's objects apart, so everything but the desktop is withheld.
		"locked": [True, False],
		"navigatorSet": False,
	}


def test_xdgDirectoriesFollowTheSpecification(tmp_path):
	done = run(
		tmp_path,
		"""
		from linuxPlatform import xdg

		result = {}
		os.environ["XDG_CONFIG_DIRS"] = "relative/xdg:/etc/one::/etc/two"
		result["configDirs"] = xdg.configDirs()
		os.environ["XDG_CONFIG_DIRS"] = "relative/xdg"
		result["onlyRelative"] = xdg.configDirs()
		os.environ["XDG_STATE_HOME"] = "relative/state"
		result["stateHome"] = xdg.stateHome()
		result["userStateDir"] = xdg.userStateDir()
		""",
	)
	done.assertClean()
	home = os.path.expanduser("~")
	assert done.value == {
		# The specification says relative entries are invalid and must be ignored.
		"configDirs": ["/etc/one", "/etc/two"],
		"onlyRelative": ["/etc/xdg"],
		"stateHome": os.path.join(home, ".local", "state"),
		"userStateDir": os.path.join(home, ".local", "state", "basedreader"),
	}
