"""The harness reports what a child did, including what upstream would have swallowed.

nix develop . -c tests/linux/rig python -m pytest tests/linux/test_harness.py
"""

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import conftest
import harness
import pytest

BOOT_CONFIG = harness.HERE / "fixtures" / "bootConfig" / "nvda.ini"
#: What fixtures/globalPlugins/bootProbe.py says once NVDA has started.
BOOT_SENTENCE = "the boot probe has started"


def problemsBesides(done: harness.ChildResult, *names: str) -> list[str]:
	"""The checks that fail, other than names, by name."""
	return [
		name
		for name in harness.CLEAN_CHECKS
		if name not in names and done.problems(set(harness.CLEAN_CHECKS) - {name})
	]


def test_valueComesBack(tmp_path: Path):
	done = harness.runChild(tmp_path, "result = {'sum': 1 + 2}", prepare="none")
	done.assertClean()
	assert done.value == {"sum": 3}


def test_exceptionIsReported(tmp_path: Path):
	done = harness.runChild(tmp_path, "raise ValueError('expected by the test')", prepare="none")
	assert done.error is not None and "expected by the test" in done.error
	assert problemsBesides(done) == ["error"]


def test_swallowedWindowsCallIsReported(tmp_path: Path):
	done = harness.runChild(
		tmp_path,
		"""
		import ctypes
		try:
			ctypes.windll.user32.GetForegroundWindow()
		except NotImplementedError:
			pass
		result = "carried on"
		""",
		prepare="bootstrap",
	)
	assert done.error is None, done.error
	assert done.value == "carried on"
	assert done.noLinuxImplementation == ["user32.GetForegroundWindow"]
	with pytest.raises(AssertionError, match=r"GetForegroundWindow\(\)"):
		# The stack names the line that made the call.
		done.assertClean()
	assert problemsBesides(done) == ["noLinuxImplementation"]


@pytest.mark.parametrize("prepare", harness.PREPARE_LEVELS)
def test_errorOnAnyLoggerIsReportedAtEveryLevel(tmp_path: Path, prepare: str):
	done = harness.runChild(
		tmp_path,
		"""
		import logging

		logging.getLogger("comtypes.client").error("expected by the test")
		logging.getLogger("comtypes.client").warning("below the level kept")
		""",
		prepare=prepare,
	)
	assert done.logErrors == ["ERROR: expected by the test"]
	assert done.logRecords == done.logErrors
	assert problemsBesides(done) == ["logErrors"]


def test_loggedErrorIsReported(tmp_path: Path):
	done = harness.runChild(
		tmp_path,
		"""
		from logHandler import log
		log.error("expected by the test")
		log.warning("kept when asked for")
		""",
		recordLevel=30,
	)
	assert done.logErrors == ["ERROR: expected by the test"]
	assert done.logRecords == ["ERROR: expected by the test", "WARNING: kept when asked for"]
	assert problemsBesides(done) == ["logErrors"]


def test_threadExceptionIsReportedOnce(tmp_path: Path):
	done = harness.runChild(
		tmp_path,
		"""
		import threading

		def fail():
			raise RuntimeError("expected by the test")

		thread = threading.Thread(target=fail, name="failing")
		thread.start()
		thread.join()
		""",
		prepare="none",
	)
	assert len(done.threadExceptions) == 1
	assert "in thread failing" in done.threadExceptions[0]
	assert "RuntimeError: expected by the test" in done.threadExceptions[0]
	assert problemsBesides(done) == ["threadExceptions"]


def test_threadExceptionUnderNVDAsOwnHookIsOneLogError(tmp_path: Path):
	# logHandler.initialize puts NVDA's hooks in place, which log the exception as an ERROR record.
	done = harness.runChild(
		tmp_path,
		f"""
		import threading

		import globalVars
		import logHandler

		globalVars.appArgs.logFileName = {str(tmp_path / "nvda.log")!r}
		logHandler.initialize()

		def fail():
			raise RuntimeError("expected by the test")

		thread = threading.Thread(target=fail, name="failing")
		thread.start()
		thread.join()
		""",
	)
	assert done.logErrors == ["ERROR: Exception in thread failing:\n"]
	assert "RuntimeError: expected by the test" in done.events[-1]["exception"]
	assert done.threadExceptions == []
	assert problemsBesides(done) == ["logErrors"]


def test_failedFinaliserIsReported(tmp_path: Path):
	done = harness.runChild(
		tmp_path,
		"""
		class Failing:
			def __del__(self):
				raise RuntimeError("expected by the test")

		Failing()
		""",
		prepare="none",
	)
	assert len(done.unraisable) == 1
	assert "RuntimeError: expected by the test" in done.unraisable[0]
	assert problemsBesides(done) == ["unraisable"]


def test_finaliserFailureThatUpstreamHasAtExitIsAllowed():
	# The interpreter takes config apart before the synth settings whose finaliser unregisters from it.
	atExit = (
		"Exception ignored in: <function AutoSettings.__del__ at 0x7fffdb3b1a80>\n"
		"Traceback (most recent call last):\n"
		'  File "/x/source/autoSettingsUtils/autoSettings.py", line 43, in __del__\n'
		'  File "/x/source/autoSettingsUtils/autoSettings.py", line 52, in _unregisterConfigSaveAction\n'
		"AttributeError: 'NoneType' object has no attribute 'unregister'\n"
	)
	assert harness.ChildResult(returnCode=0, unraisable=[atExit]).problems() == []
	other = atExit.replace("'unregister'", "'register'")
	assert len(harness.ChildResult(returnCode=0, unraisable=[atExit, other]).problems()) == 1


def test_exitStatusIsChecked(tmp_path: Path):
	done = harness.runChild(
		tmp_path,
		"""
		import atexit
		import os

		atexit.register(os._exit, 3)
		""",
		prepare="none",
	)
	assert done.error is None
	assert done.returnCode == 3
	assert problemsBesides(done) == ["returnCode"]


def test_childThatKillsItselfStillReportsWhatItDid(tmp_path: Path):
	done = harness.runChild(
		tmp_path,
		"""
		import os
		import signal

		from logHandler import log

		log.error("expected by the test")
		os.kill(os.getpid(), signal.SIGKILL)
		""",
		prepare="bootstrap",
	)
	assert done.returnCode == -9
	assert "ended without reporting a result: killed by SIGKILL" in done.error
	assert done.logErrors == ["ERROR: expected by the test"]
	assert problemsBesides(done) == ["error", "logErrors", "returnCode"]


def test_hungChildEndsWithEveryStack(tmp_path: Path):
	done = harness.runChild(tmp_path, "import time\ntime.sleep(60)\n", prepare="none", timeout=1)
	assert "ended without reporting a result: exit status 1" in done.error
	assert "Timeout" in done.stderr
	assert "code.py" in done.stderr


@pytest.mark.parametrize(
	"write",
	[
		"open(os.path.join(SOURCE, 'linuxPlatform', 'harnessTest.tmp'), 'w')",
		"os.open(os.path.join(SOURCE, 'harnessTest.tmp'), os.O_WRONLY | os.O_CREAT)",
		"os.mkdir(os.path.join(SOURCE, 'harnessTest'))",
		"os.rename(os.path.join(SOURCE, 'basedreader.py'), os.path.join(SOURCE, 'moved.py'))",
		"os.utime(os.path.join(SOURCE, 'basedreader.py'))",
		"shutil.copyfile(os.path.join(SOURCE, 'basedreader.py'), os.path.join(UNIT, 'copy.py'))",
		"os.symlink('/nowhere', os.path.join(UNIT, 'link'))",
		# Through a symbolic link that lives outside the protected trees.
		"os.symlink(SOURCE, 'outside') or open(os.path.join('outside', 'harnessTest.tmp'), 'w')",
		# Relative to a directory descriptor, which the open event does not carry; the os.mkdir event does.
		"os.mkdir('harnessTest', dir_fd=os.open(SOURCE, os.O_RDONLY))",
	],
)
def test_writeUnderSourceIsReportedEvenWhenSwallowed(tmp_path: Path, write: str):
	done = harness.runChild(
		tmp_path,
		f"""
		import os
		import shutil

		SOURCE = {str(harness.SOURCE)!r}
		UNIT = {str(harness.ROOT / "tests" / "unit")!r}
		try:
			{write}
		except PermissionError as e:
			result = str(e)
		""",
		prepare="none",
	)
	assert "nothing may write under source/ or tests/unit" in done.value
	assert len(done.writes) == 1
	assert problemsBesides(done) == ["writes"]


def test_whatIsNotAWriteUnderSourceIsNotReported(tmp_path: Path):
	done = harness.runChild(
		tmp_path,
		f"""
		import os

		SOURCE = {str(harness.SOURCE)!r}
		with open(os.path.join(SOURCE, "basedreader.py"), encoding="utf-8") as f:
			f.read()
		with open("here.txt", "w") as f:
			f.write("outside")
		# A link outside that points in: removing it changes nothing under source/.
		os.symlink(SOURCE, "outside")
		os.remove("outside")
		# Aimed at nothing, so it changes nothing: the file system answers, not the check.
		try:
			os.remove(os.path.join(SOURCE, "absent.tmp"))
		except OSError as e:
			result = type(e).__name__
		""",
		prepare="none",
	)
	done.assertClean()
	assert done.value != "PermissionError"


def test_treeStateSeesAFileWrittenAndRemovedAgain(tmp_path: Path):
	(tmp_path / "directory").mkdir()
	before = harness.treeState(tmp_path)
	(tmp_path / "directory" / "brief.tmp").write_text("x", encoding="utf-8")
	(tmp_path / "directory" / "brief.tmp").unlink()
	assert harness.changedPaths(before, harness.treeState(tmp_path)) == [str(tmp_path / "directory")]


def test_treeStateCoversTheProtectedTreesAndSkipsUnwatched():
	state = harness.treeState()
	assert str(harness.SOURCE / "basedreader.py") in state
	assert str(harness.ROOT / "tests" / "unit" / "__init__.py") in state
	assert not any(path.startswith(str(harness.ROOT / "tests" / "linux")) for path in state)
	whole = harness.treeState(harness.ROOT)
	assert not any(path.startswith(tuple(top + os.sep for top in harness.UNWATCHED)) for path in whole)
	assert str(harness.ROOT / "tests" / "linux" / "harness.py") in whole


@pytest.mark.parametrize(
	"reach",
	[
		"ctypes.CDLL('libasound-harnessTest.so.2')",
		"ctypes.CDLL('libpulse-simple-harnessTest.so.0')",
		"open('/dev/snd/harnessTest', 'rb')",
		"socket.socket(socket.AF_UNIX).connect('/run/user/1/pulse/native')",
	],
)
def test_reachingForSoundIsReported(tmp_path: Path, reach: str):
	done = harness.runChild(
		tmp_path,
		f"""
		import ctypes
		import socket

		try:
			{reach}
		except OSError:
			pass
		""",
		prepare="none",
	)
	assert len(done.audio) == 1, done.audio
	assert problemsBesides(done) == ["audio"]


def test_audioLibrariesAreKnownByName():
	assert harness.isAudioLibrary("/nix/store/x-alsa-lib/lib/libasound.so.2")
	assert harness.isAudioLibrary("libespeak-ng.so.1")
	assert not harness.isAudioLibrary("/nix/store/x-icu/lib/libicuuc.so.76")
	assert harness.soundReachedAtExit() == []


def test_standardErrorIsChecked(tmp_path: Path):
	allowed = (
		"Exception ignored in: <function AutoSettings.__del__ at 0x7fffe85965c0>\n"
		"Traceback (most recent call last):\n"
		'  File "/x/source/autoSettingsUtils/autoSettings.py", line 43, in __del__\n'
		'  File "/x/source/autoSettingsUtils/autoSettings.py", line 52, in _unregisterConfigSaveAction\n'
		"AttributeError: 'NoneType' object has no attribute 'unregister'\n"
		"(basedreader.py:12): Gtk-WARNING **: 12:00:00.000: Could not load a pixbuf from "
		"/org/gtk/libgtk/theme/Adwaita/assets/check-symbolic.svg.\n"
		"This may indicate that pixbuf loaders or the mime database could not be found.\n"
	)
	assert harness.unexpectedStandardError(allowed * 2) == ""
	done = harness.runChild(
		tmp_path,
		f"import sys\nsys.stderr.write({allowed!r})\nsys.stderr.write('written by the test\\n')\n",
		prepare="none",
	)
	assert harness.unexpectedStandardError(done.stderr) == "written by the test\n"
	assert problemsBesides(done) == ["stderr"]


def test_ignoringOptsOutOfTheNamedCheckOnly(tmp_path: Path):
	done = harness.runChild(
		tmp_path,
		"""
		import logging
		import sys

		logging.getLogger("x").error("expected by the test")
		sys.stderr.write("written by the test\\n")
		""",
		prepare="none",
	)
	with pytest.raises(AssertionError, match="standard error"):
		done.assertClean(ignoring={"logErrors"})
	with pytest.raises(AssertionError, match="logged at ERROR"):
		done.assertClean(ignoring={"stderr"})
	done.assertClean(ignoring={"logErrors", "stderr"})
	with pytest.raises(AssertionError, match="no such check"):
		done.assertClean(ignoring={"everything"})


def test_appDirLevelRunsFromAppDirAsTheLauncherDoes(tmp_path: Path):
	done = harness.runChild(
		tmp_path,
		"""
		import gettext
		import os

		import globalVars

		# upstream's languageHandler finds its catalogues relative to the working directory.
		translation = gettext.translation("nvda", localedir="locale", languages=["pl"])
		result = {
			"cwd": os.getcwd(),
			"appDir": globalVars.appDir,
			"translated": translation.gettext("Cancel") != "Cancel",
			"builtin": _("Cancel"),
		}
		""",
		prepare="appDir",
	)
	done.assertClean()
	assert done.value == {
		"cwd": str(harness.SOURCE),
		"appDir": str(harness.SOURCE),
		"translated": True,
		"builtin": "Cancel",
	}


def test_configLevelKeepsItsConfigurationAndTestsLinuxOffThePath(tmp_path: Path):
	done = harness.runChild(
		tmp_path,
		"""
		import os
		import sys

		import config
		import globalVars
		import synthDrivers

		result = {
			"configPath": globalVars.appArgs.configPath,
			"language": config.conf["general"]["language"],
			"synthDrivers": list(synthDrivers.__path__),
			"testsOnPath": any(os.path.realpath(p).endswith(os.path.join("tests", "linux")) for p in sys.path),
		}
		""",
	)
	done.assertClean()
	assert done.value["configPath"].startswith(str(tmp_path))
	assert done.value["language"] == "Windows"
	assert str(harness.SOURCE / "synthDrivers") in done.value["synthDrivers"]
	assert not any("tests" in path for path in done.value["synthDrivers"])
	assert done.value["testsOnPath"] is False


@pytest.mark.parametrize("sign", ["variable", "firstProcess", "runtimeDirectory"])
def test_eachSignOfBeingOutsideTheRigRefuses(monkeypatch, tmp_path: Path, sign: str):
	assert harness.whyNotInRig() is None
	if sign == "variable":
		monkeypatch.delenv(harness.RIG_VARIABLE)
	elif sign == "firstProcess":
		fake = tmp_path / "comm"
		fake.write_text("systemd\n", encoding="ascii")
		monkeypatch.setattr(harness, "FIRST_PROCESS_NAME", fake)
	else:
		monkeypatch.setattr(harness, "LIVE_RUNTIME_DIRECTORY", tmp_path)
	why = harness.whyNotInRig()
	assert why is not None
	with pytest.raises(SystemExit, match="refusing to run outside the rig"):
		harness.requireRig()
	with pytest.raises(pytest.exit.Exception, match="refusing to run outside the rig") as stopped:
		conftest.pytest_sessionstart(None)
	assert stopped.value.returncode == pytest.ExitCode.USAGE_ERROR


def test_childRefusesOutsideTheRig(tmp_path: Path):
	done = harness.runChild(tmp_path, "result = 'ran'", prepare="none", env={harness.RIG_VARIABLE: None})
	assert done.value is None
	assert "ended without reporting a result: exit status 1" in done.error
	assert "refusing to run outside the rig" in done.stderr


def test_testRunRefusesOutsideTheRig():
	env = {name: value for name, value in os.environ.items() if name != harness.RIG_VARIABLE}
	done = subprocess.run(
		[sys.executable, "-m", "pytest", "-p", "no:cacheprovider", str(Path(__file__))],
		capture_output=True,
		text=True,
		env=env,
		cwd=harness.ROOT,
		timeout=120,
		check=False,
	)
	assert done.returncode == pytest.ExitCode.USAGE_ERROR, done.stdout + done.stderr
	assert "refusing to run outside the rig" in done.stdout + done.stderr
	assert " passed" not in done.stdout


def startBootProbe(launches, tmp_path: Path, name: str, probe: str) -> harness.Launch:
	configDir = tmp_path / f"{name}Config"
	configDir.mkdir()
	shutil.copy(BOOT_CONFIG, configDir / "nvda.ini")
	launch = launches(
		name,
		"-c",
		str(configDir),
		"-f",
		str(tmp_path / "launches" / name / "nvda.log"),
		"--debug-logging",
		env={"BASEDREADER_BOOT_PROBE": probe},
	)
	return launch


def test_launcherRunsCheckedAndIsHeard(launches, tmp_path: Path):
	nvda = startBootProbe(launches, tmp_path, "exit", "exit")
	assert nvda.waitForSpeech(BOOT_SENTENCE, timeout=60) > 0
	assert nvda.waitForExit(timeout=20) == 0, nvda.describe()
	nvda.assertClean(logNames=("nvda.log",))
	assert BOOT_SENTENCE in nvda.spokenTexts()
	assert [event["kind"] for event in nvda.result().events if event["kind"] == "start"] == ["start"]


def test_killedLaunchStillReportsWhatItDid(launches, tmp_path: Path):
	nvda = startBootProbe(launches, tmp_path, "killed", "wait")
	nvda.waitForSpeech(BOOT_SENTENCE, timeout=60)
	nvda.process.kill()
	assert nvda.waitForExit(timeout=20) == -9
	done = nvda.result()
	assert done.events[0]["kind"] == "start"
	assert problemsBesides(done) == ["returnCode"]
	with pytest.raises(AssertionError, match="killed by SIGKILL"):
		nvda.assertClean()


#: A global plugin that, once NVDA has started, does what a clean run must not, swallows each, and exits.
UNCLEAN_PLUGIN = """
import ctypes
import logging
import os

import core
import globalPluginHandler
import globalVars


class GlobalPlugin(globalPluginHandler.GlobalPlugin):
	def __init__(self):
		super().__init__()
		# postNvdaStartup holds its handlers weakly; globalPluginHandler keeps this plugin alive.
		core.postNvdaStartup.register(self._onNvdaStarted)

	def _onNvdaStarted(self):
		logging.getLogger("harnessTest").error("logged by the test plugin")
		try:
			open(os.path.join(globalVars.appDir, "harnessTest.tmp"), "w")
		except OSError:
			pass
		try:
			ctypes.windll.user32.GetForegroundWindow()
		except NotImplementedError:
			pass
		core.triggerNVDAExit()
"""


def test_launchIsHeldToTheSameChecks(launches, tmp_path: Path):
	hooks = tmp_path / "hooks"
	(hooks / "globalPlugins").mkdir(parents=True)
	(hooks / "globalPlugins" / "harnessTestPlugin.py").write_text(UNCLEAN_PLUGIN, encoding="utf-8")
	(hooks / "synthDrivers").symlink_to(harness.HERE / "synthDrivers")
	configDir = tmp_path / "config"
	configDir.mkdir()
	shutil.copy(BOOT_CONFIG, configDir / "nvda.ini")
	nvda = launches(
		"unclean",
		"-c",
		str(configDir),
		"-f",
		str(tmp_path / "launches" / "unclean" / "nvda.log"),
		env={"BASEDREADER_TEST_HOOKS_DIR": str(hooks)},
	)
	assert nvda.waitForExit(timeout=60) == 0, nvda.describe()
	done = nvda.result()
	assert done.logErrors == ["ERROR: logged by the test plugin"]
	assert done.writes == [f"open {harness.SOURCE / 'harnessTest.tmp'}"]
	assert done.noLinuxImplementation == ["user32.GetForegroundWindow"]
	assert problemsBesides(done) == ["noLinuxImplementation", "logErrors", "writes"]
	with pytest.raises(AssertionError, match="nvda.log holds 1 records at ERROR"):
		nvda.assertClean(ignoring={"noLinuxImplementation", "writes"}, logNames=("nvda.log",))


def test_copyThatRestartsItselfRunsCheckedToo(launches, tmp_path: Path):
	nvda = startBootProbe(launches, tmp_path, "restart", "restart")
	assert nvda.waitForExit(timeout=60) == 0, nvda.describe()
	singleInstance = harness.singleInstance()
	lock = singleInstance.InstanceLock(singleInstance.lockFilePath())
	try:
		deadline = time.monotonic() + 60
		while [text for text in nvda.spokenTexts() if text == BOOT_SENTENCE] != [BOOT_SENTENCE] * 2:
			assert time.monotonic() < deadline, nvda.describe()
			time.sleep(0.05)
		while lock.holder() is not None:
			assert time.monotonic() < deadline, nvda.describe()
			time.sleep(0.05)
	finally:
		lock.close()
	starts = [event for event in nvda.result().events if event["kind"] == "start"]
	assert len(starts) == 2
	assert starts[0]["pid"] == nvda.process.pid != starts[1]["pid"]
	nvda.assertClean(logNames=("nvda-old.log", "nvda.log"))
