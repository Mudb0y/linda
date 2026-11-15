"""Paths, saved configuration, language, the command line and the operating system's answers on Linux.

nix develop . -c tests/linux/rig python -m pytest tests/linux/test_osServices.py
"""

import locale
import os
import stat
import textwrap
from pathlib import Path

import harness

#: What nvda.pyw establishes before core.main, short of a configuration: translations installed as
#: builtins, appDir, and a record of every error logged. The working directory stays the child's own,
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


class Collector(logging.Handler):
	def emit(self, record):
		logErrors.append(f"{{record.levelname}}: {{record.getMessage()}}")


log.addHandler(Collector(level=logging.ERROR))


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
	assert done.value["language"] == "pl"
	assert done.value["off"] == "Wyłączone"
	assert done.value["withContext"] == "Aragoński"
	assert done.value["cLocale"] == ("pl_PL.UTF-8" if isLocaleInstalled("pl_PL.UTF-8") else "C.UTF-8")
	# Named in the desktop's language, as Windows names languages in the user's display language.
	assert done.value["described"] == "portugalski (Brazylia)"


def test_interfaceLanguageFollowsPosixPrecedence(tmp_path):
	cases = [
		({"LANG": "de_AT.UTF-8"}, "de"),
		({"LANG": "pt_BR.UTF-8"}, "pt_BR"),
		({"LANG": "en_GB.UTF-8"}, "en"),
		({"LANG": "sr_RS.UTF-8@latin"}, "sr"),
		({"LANG": "en_GB.UTF-8", "LC_MESSAGES": "pl_PL.UTF-8"}, "pl"),
		({"LANG": "de_DE.UTF-8", "LC_ALL": "fr_FR.UTF-8", "LC_MESSAGES": "pl_PL.UTF-8"}, "fr"),
		# LANGUAGE is a list; an entry with no catalogue gives way to the next.
		({"LANG": "en_GB.UTF-8", "LANGUAGE": "eo:pt_BR:pl"}, "pt_BR"),
		# glibc ignores LANGUAGE under the C locale, and so does NVDA.
		({"LANG": "C.UTF-8", "LANGUAGE": "pl"}, "en"),
		({}, "en"),
		({"LANG": "xx_YY.UTF-8"}, "en"),
	]
	done = run(
		tmp_path,
		f"""
		import languageHandler

		result = []
		for env, _expected in {cases!r}:
			for name in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
				os.environ.pop(name, None)
			os.environ.update(env)
			result.append(languageHandler.getWindowsLanguage())
		""",
	)
	done.assertClean()
	assert done.value == [expected for _env, expected in cases]


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
		import NVDAState
		import systemUtils

		result = []
		for attempt in (
			lambda: NVDAState.ReadPaths.coreArchLibPath,
			lambda: systemUtils.preventSystemIdle(),
			lambda: systemUtils.preventSystemIdle(persistent=True),
			lambda: easeOfAccess.setAutoStart(easeOfAccess.AutoStartContext.AFTER_LOGON, True),
		):
			try:
				attempt()
				result.append("returned")
			except NotImplementedError as e:
				result.append(type(e).__name__)
		""",
	)
	assert done.error is None, done.error
	assert done.value == ["NoLinuxImplementation"] * 4
	assert done.noLinuxImplementation == [
		"NVDAState.ReadPaths.coreArchLibPath",
		"systemUtils.preventSystemIdle",
		"systemUtils.preventSystemIdle",
		"easeOfAccess.setAutoStart(AFTER_LOGON)",
	]
	assert not done.logErrors


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
		warnings = []


		class WarningCollector(logging.Handler):
			def emit(self, record):
				warnings.append(record.getMessage())


		log.addHandler(WarningCollector(level=logging.WARNING))
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
			"warnings": len(warnings),
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
		write(user, {entry!r})
		result["user"] = easeOfAccess.willAutoStart(context.AFTER_LOGON)
		result["logonScreen"] = easeOfAccess.willAutoStart(context.ON_LOGON_SCREEN)
		result["registered"] = easeOfAccess.isRegistered()
		easeOfAccess.notify(3)
		import config

		result["startAfterLogon"] = config.getStartAfterLogon()
		""",
		env=xdgEnv(tmp_path),
	)
	done.assertClean()
	assert done.value == {
		"none": False,
		"system": True,
		"hiddenByUser": False,
		"disabledForMate": False,
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
