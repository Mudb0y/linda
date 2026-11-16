"""NVDA's log, the watchdog and crash dumps work on Linux without sound, recursion or Windows calls.

nix develop . -c tests/linux/rig python3 -m pytest -p no:cacheprovider tests/linux/test_logging.py
"""

import json
import os
import re
import stat
import subprocess
import sys
import wave
from pathlib import Path

import harness

HERE = Path(__file__).resolve().parent


def startLog(logFileName: Path | None, debugLogging: bool = True) -> str:
	"""Code that starts NVDA's log the way nvda.pyw does, with -f when logFileName is given."""
	return f"""
		import globalVars
		import logHandler
		from logHandler import log

		globalVars.appArgs.logFileName = {None if logFileName is None else str(logFileName)!r}
		globalVars.appArgs.debugLogging = {debugLogging!r}
		logHandler.initialize()

		def logText():
			logHandler.logHandler.flush()
			with open(globalVars.appArgs.logFileName, encoding="utf-8") as f:
				return f.read()

		# An event set once a record whose message begins with start has been logged.
		def whenLogged(start):
			import logging
			import threading

			logged = threading.Event()

			class Watcher(logging.Handler):
				def emit(self, record):
					if record.getMessage().startswith(start):
						logged.set()

			log.addHandler(Watcher())
			return logged
	"""


TIME = r"\d\d:\d\d:\d\d\.\d\d\d"


def test_recordHasTimeAndCodePath(tmp_path: Path):
	done = harness.runChild(
		tmp_path,
		startLog(tmp_path / "nvda.log", debugLogging=False)
		+ """
		import logging
		import time
		import config

		def reportFromTest():
			log.info("reported by the test")

		reportFromTest()
		config.conf["general"]["loggingLevel"] = "NOT_A_LEVEL"
		logHandler.setLogLevelFromConfig()
		record = logging.LogRecord("nvda", logging.INFO, "test.py", 1, "", (), None)
		record.created = 1700000000.0
		record.msecs = 123.0
		result = {
			"log": logText(),
			"formatted": logHandler.logHandler.formatter.formatTime(record),
			"expected": time.strftime("%H:%M:%S", time.localtime(1700000000.0)) + ".123",
		}
		""",
	)
	done.assertClean()
	text = done.value["log"]
	# A function outside NVDA's code is external; one of NVDA's own modules is not.
	assert re.search(
		rf"^INFO - external:__harness__\.reportFromTest \({TIME}\) - MainThread \(\d+\):\n"
		r"reported by the test$",
		text,
		re.MULTILINE,
	), text
	assert re.search(
		rf"^WARNING - logHandler\.setLogLevelFromConfig \({TIME}\) - MainThread \(\d+\):\n"
		r"invalid setting for logging level: NOT_A_LEVEL$",
		text,
		re.MULTILINE,
	), text
	assert done.value["formatted"] == done.value["expected"]


def test_exceptionWithOSError(tmp_path: Path):
	done = harness.runChild(
		tmp_path,
		startLog(tmp_path / "nvda.log")
		+ """
		import comtypes
		import exceptions

		try:
			open("/nonexistent/basedreader-test")
		except OSError:
			log.exception("plain OSError")
		try:
			raise WindowsError(None, "timed out", None, 1460)
		except OSError:
			log.exception("Windows timeout")
		try:
			raise exceptions.CallCancelled()
		except exceptions.CallCancelled:
			log.exception("cancelled call")
		try:
			raise comtypes.COMError(logHandler.E_ACCESSDENIED, "access denied", None)
		except comtypes.COMError:
			log.exception("COM access denied")
		try:
			raise comtypes.COMError(-2147467259, "unspecified failure", None)
		except comtypes.COMError:
			log.exception("COM failure")
		result = {"log": logText()}
		""",
	)
	done.assertClean(ignoring={"logErrors"})
	assert done.logErrors == ["ERROR: plain OSError", "ERROR: COM failure"]
	text = done.value["log"]
	assert re.search(r"^ERROR - .*\nplain OSError\nTraceback", text, re.MULTILINE), text
	assert "FileNotFoundError: [Errno 2]" in text
	# Upstream's expected errors are logged below ERROR, as on Windows.
	assert re.search(r"^DEBUGWARNING - .*\nWindows timeout\n", text, re.MULTILINE), text
	assert re.search(r"^DEBUGWARNING - .*\ncancelled call\n", text, re.MULTILINE), text
	assert re.search(r"^DEBUGWARNING - .*\nCOM access denied\n", text, re.MULTILINE), text


def test_failingHandlerDoesNotRecurse(tmp_path: Path):
	done = harness.runChild(
		tmp_path,
		startLog(tmp_path / "nvda.log")
		+ """
		def brokenFormatTime(self, record, datefmt=None):
			raise ValueError("formatting broken by the test")

		logHandler.Formatter.formatTime = brokenFormatTime
		log.info("cannot be written")
		# Standard output is redirected into the same failing log.
		print("printed while the log is broken")
		result = "returned"
		""",
	)
	done.assertClean(ignoring={"stderr"})
	assert done.value == "returned"
	assert done.stderr.count("--- Logging error") == 2, done.stderr
	assert "ValueError: formatting broken by the test" in done.stderr
	assert "RecursionError" not in done.stderr


def test_unwritableRecordIsNotReportedWhenLoggingSaysNotOrHasNowhereTo(tmp_path: Path):
	done = harness.runChild(
		tmp_path,
		startLog(tmp_path / "nvda.log")
		+ """
		import logging
		import sys

		def brokenFormatTime(self, record, datefmt=None):
			raise ValueError("formatting broken by the test")

		logHandler.Formatter.formatTime = brokenFormatTime
		logging.raiseExceptions = False
		log.info("not reported, as logging is told not to")
		logging.raiseExceptions = True
		realStderr = sys.__stderr__
		sys.__stderr__ = None
		try:
			log.info("not reported, as there is no standard error")
		finally:
			sys.__stderr__ = realStderr
		log.info("reported")
		result = "returned"
		""",
	)
	done.assertClean(ignoring={"stderr"})
	assert done.value == "returned"
	assert done.stderr.count("--- Logging error") == 1, done.stderr


def test_errorSoundThreadThatCannotStartIsTriedAgain(tmp_path: Path):
	done = harness.runChild(
		tmp_path,
		startLog(tmp_path / "nvda.log")
		+ """
		import threading
		import config

		config.conf["featureFlag"]["playErrorSound"] = 1
		played = threading.Event()

		def player():
			played.set()

		logHandler.getOnErrorSoundRequested().register(player)
		realStart = threading.Thread.start

		def startRefused(self, *args, **kwargs):
			# What threading does once the interpreter is shutting down.
			if self.name == "logHandler.errorSound":
				raise RuntimeError("can't create new thread at interpreter shutdown")
			return realStart(self, *args, **kwargs)

		threading.Thread.start = startRefused
		try:
			log.error("logged while no thread can start")
		finally:
			threading.Thread.start = realStart
		refused = {"played": played.is_set(), "logged": "logged while no thread can start" in logText()}
		log.error("logged once threads start again")
		result = {"refused": refused, "played": played.wait(60)}
		""",
	)
	done.assertClean(ignoring={"logErrors"})
	assert done.logErrors == [
		"ERROR: logged while no thread can start",
		"ERROR: logged once threads start again",
	]
	assert done.value == {"refused": {"played": False, "logged": True}, "played": True}


def test_errorSoundCannotRecurseOrBlock(tmp_path: Path):
	done = harness.runChild(
		tmp_path,
		startLog(tmp_path / "nvda.log")
		+ """
		import threading
		import config
		from linuxPlatform import logSupport

		config.conf["featureFlag"]["playErrorSound"] = 1
		calls = []
		entered = threading.Event()
		release = threading.Event()
		playerFailureLogged = whenLogged("Error running handler")

		def stuckFailingPlayer():
			calls.append(threading.current_thread().name)
			entered.set()
			# Released only by the thread that logs, so this is False if that thread waited for it.
			calls.append(release.wait(60))
			raise RuntimeError("player failed, as the test asks")

		logHandler.getOnErrorSoundRequested().register(stuckFailingPlayer)
		log.error("first error")
		entered.wait(60)
		log.error("second error")
		# Read while the player is still stuck, so the sound's thread cannot have taken a request.
		askedAgain = logSupport._errorSound._pending.is_set()
		release.set()
		playerFailureLogged.wait(60)
		result = {"calls": calls, "askedAgain": askedAgain}
		""",
	)
	done.assertClean(ignoring={"logErrors"})
	# The second error arrived while the sound was being requested and asks for nothing, and the
	# thread that logged it went on while the player was stuck. The player's own failure is logged
	# on the sound's thread, so nothing follows it either.
	assert done.value == {"calls": ["logHandler.errorSound", True], "askedAgain": False}
	assert done.logErrors[:2] == ["ERROR: first error", "ERROR: second error"]
	assert len(done.logErrors) == 3
	assert done.logErrors[2].startswith("ERROR: Error running handler")


def test_errorSoundStopsWhenItsPlaybackLogsErrors(tmp_path: Path):
	done = harness.runChild(
		tmp_path,
		startLog(tmp_path / "nvda.log")
		+ """
		import threading
		import time
		import config
		from linuxPlatform import logSupport

		config.conf["featureFlag"]["playErrorSound"] = 1
		# Far longer than a loaded machine takes to fail a playback, so every failure follows its sound.
		logSupport.ERROR_SOUND_FOLLOW_WINDOW = 30
		logSupport.ERROR_SOUND_QUIET_PERIOD = 0.5
		plays = []
		failing = True
		stopped = whenLogged("Errors followed the error sound")
		fourthPlay = threading.Event()

		def playerWhosePlaybackFails():
			plays.append(time.perf_counter())
			if len(plays) == 4:
				fourthPlay.set()

			def playback():
				if not failing:
					return
				# Feeding takes a moment, so the error comes after the sound was requested.
				time.sleep(0.05)
				try:
					raise RuntimeError("feed failed, as the test asks")
				except RuntimeError:
					log.exception("Error playing wave file")

			# As nvwave's playWaveFile does: the error comes from a thread of the player's own.
			threading.Thread(target=playback, daemon=True).start()

		logHandler.getOnErrorSoundRequested().register(playerWhosePlaybackFails)
		log.error("one error")
		stopped.wait(60)
		loopPlays = len(plays)
		failing = False
		# A sleep is never shorter than asked, so errors have been quiet for the quiet period.
		time.sleep(logSupport.ERROR_SOUND_QUIET_PERIOD + 0.1)
		log.error("an error while errors are quiet")
		fourthPlay.wait(60)
		result = {"loopPlays": loopPlays, "plays": len(plays), "log": logText()}
		""",
	)
	done.assertClean(ignoring={"logErrors"})
	# Each failing playback asks for the sound again; the third that follows its sound stops it.
	assert done.value["loopPlays"] == 3, done.value
	assert done.logErrors.count("ERROR: Error playing wave file") == 3, done.logErrors
	text = done.value["log"]
	assert len(re.findall(r"^WARNING - .*\nErrors followed the error sound 3 times", text, re.MULTILINE)) == 1
	# Once errors have been quiet for the quiet period, the sound comes back.
	assert done.value["plays"] == 4, done.value


def test_errorSoundThroughNvwaveStops(tmp_path: Path):
	done = harness.runChild(
		tmp_path,
		startLog(tmp_path / "nvda.log")
		+ """
		import time
		import config
		import NVDAHelper
		import nvwave
		from linuxPlatform import logSupport

		config.conf["featureFlag"]["playErrorSound"] = 1
		# Far longer than a loaded machine takes to fail a playback, so every failure follows its sound.
		logSupport.ERROR_SOUND_FOLLOW_WINDOW = 30
		created = []
		stopped = whenLogged("Errors followed the error sound")

		class FailingPlayer:
			# Not an OSError, so nvwave's own error state never stops the replays.
			audioDeviceError_static = False

			def __init__(self, **kwargs):
				created.append(1)

			def feed(self, data):
				# A device's feed fails once playing has begun, after the request for the sound has
				# returned. An error logged before then is one logged while the sound was being
				# requested, which asks for nothing and would end the chain before the limit.
				deadline = time.monotonic() + 60
				while logSupport._errorSound._handling and time.monotonic() < deadline:
					time.sleep(0.001)
				raise RuntimeError("feed failed, as the test asks")

			def idle(self):
				pass

			def stop(self):
				pass

		def playErrorSoundOnceThePreviousHasEnded():
			# playWaveFile reads the global player again after checking it, so a playback thread that
			# clears it in between makes playWaveFile raise, which playErrorSound swallows, and ends the
			# chain early. That race is upstream's own and not what this test is about.
			if nvwave.fileWavePlayerThread is not None:
				nvwave.fileWavePlayerThread.join(60)
			nvwave.playErrorSound()

		nvwave.WavePlayer = FailingPlayer
		logHandler.getOnErrorSoundRequested().register(playErrorSoundOnceThePreviousHasEnded)
		log.error("one error")
		result = {"stopped": stopped.wait(60), "players": len(created)}
		""",
	)
	done.assertClean(ignoring={"logErrors"})
	assert done.value == {"stopped": True, "players": 3}
	assert done.logErrors == ["ERROR: one error"] + ["ERROR: Error playing wave file"] * 3


def test_errorWhileSoundThreadStartsDoesNotHang(tmp_path: Path):
	done = harness.runChild(
		tmp_path,
		startLog(tmp_path / "nvda.log")
		+ """
		import threading
		import config

		config.conf["featureFlag"]["playErrorSound"] = 1
		realStart = threading.Thread.start

		def startThatLogs(self, *args, **kwargs):
			# Stands in for a finaliser or a signal handler that runs on this thread and logs.
			if self.name == "logHandler.errorSound":
				log.error("logged while the sound thread starts")
			return realStart(self, *args, **kwargs)

		threading.Thread.start = startThatLogs
		log.error("first error")
		threading.Thread.start = realStart
		result = "returned"
		""",
		timeout=120,
	)
	done.assertClean(ignoring={"logErrors"})
	assert done.value == "returned"
	assert sorted(done.logErrors) == ["ERROR: first error", "ERROR: logged while the sound thread starts"]


def test_errorSoundRequestsStopBeforeTheExitSound(tmp_path: Path):
	done = harness.runChild(
		tmp_path,
		startLog(tmp_path / "nvda.log")
		+ """
		import threading
		import time
		import config
		from linuxPlatform import logSupport

		config.conf["featureFlag"]["playErrorSound"] = 1
		plays = []
		inPlayer = threading.Event()

		def playerThatTakesAMoment():
			inPlayer.set()
			plays.append("error sound starting")
			# Long enough that, without the wait, the exit sound would start in the middle.
			time.sleep(0.3)
			plays.append("error sound started")

		logHandler.getOnErrorSoundRequested().register(playerThatTakesAMoment)
		# Logged in the last steps of shutdown, as when a module fails to terminate.
		log.error("late error")
		inPlayer.wait(60)
		logSupport.stopErrorSoundRequests(timeout=60)
		plays.append("exit sound")
		log.error("logged after the requests stopped")
		result = {
			"plays": plays,
			"threads": [thread.name for thread in threading.enumerate()],
		}
		""",
	)
	done.assertClean(ignoring={"logErrors"})
	assert done.logErrors == ["ERROR: late error", "ERROR: logged after the requests stopped"]
	assert done.value["plays"] == ["error sound starting", "error sound started", "exit sound"]
	assert "logHandler.errorSound" not in done.value["threads"]


def test_stoppingErrorSoundRequestsDropsOneNotYetMadeAndNeverWaitsLong(tmp_path: Path):
	done = harness.runChild(
		tmp_path,
		startLog(tmp_path / "nvda.log")
		+ """
		import threading
		import config
		from linuxPlatform import logSupport

		config.conf["featureFlag"]["playErrorSound"] = 1
		calls = []
		entered = threading.Event()
		release = threading.Event()

		def stuckPlayer():
			calls.append("stuck")
			entered.set()
			release.wait(60)

		logHandler.getOnErrorSoundRequested().register(stuckPlayer)
		# Stopping before any error was logged starts nothing and lets nothing start later.
		logSupport.stopErrorSoundRequests()
		log.error("logged after the requests stopped")
		neverStarted = {
			"calls": list(calls),
			"thread": logSupport._errorSound._thread is not None,
		}
		logSupport._errorSound = logSupport._ErrorSoundRequester()
		log.error("asks for the stuck player")
		entered.wait(60)
		logSupport.stopErrorSoundRequests(timeout=0.2)
		# The wait is bounded: it returned while the player was still stuck.
		stuck = {"released": release.is_set(), "thread": logSupport._errorSound._thread.is_alive()}
		release.set()
		logSupport._errorSound._thread.join(60)
		result = {"neverStarted": neverStarted, "stuck": stuck, "calls": calls}
		""",
	)
	done.assertClean(ignoring={"logErrors"})
	assert done.logErrors == ["ERROR: logged after the requests stopped", "ERROR: asks for the stuck player"]
	assert done.value == {
		"neverStarted": {"calls": [], "thread": False},
		"stuck": {"released": False, "thread": True},
		"calls": ["stuck"],
	}


#: A global plugin that fails the task scheduler's terminate, the last step of core.main before the
#: exit sound, and writes to LATE_ERROR_PROBE_RESULT what became of the error sound.
LATE_ERROR_PROBE = """
import atexit
import json
import os
import sys
import threading
import time

import core
import globalPluginHandler
import nvwave
from linuxPlatform import logSupport

#: The longest the probe waits for anything; only an NVDA that is stuck reaches it.
WAIT = 60
_plays = []
_failedAtExit = threading.Event()
_release = threading.Event()
#: What let the held error sound go on: the exit sound starting, or the requests being stopped.
_releasedBy = []


def _releaseHeldSound(why):
	_releasedBy.append(why)
	_release.set()


_playWaveFile = nvwave.playWaveFile


def playWaveFile(fileName, *args, **kwargs):
	name = os.path.basename(fileName)
	if name == "exit.wav":
		_releaseHeldSound("exit sound")
	elif name == "error.wav" and _failedAtExit.is_set():
		# The error sound's thread may be slow to get to the player. Holding it until shutdown has
		# gone on to the exit sound makes it as slow as it can be every time, rather than in the few
		# runs where the race goes that way.
		_release.wait(WAIT)
		if _releasedBy[0] == "exit sound":
			deadline = time.monotonic() + WAIT
			while nvwave.fileWavePlayer is None and time.monotonic() < deadline:
				time.sleep(0.001)
	_plays.append([name, threading.current_thread().name])
	return _playWaveFile(fileName, *args, **kwargs)


nvwave.playWaveFile = playWaveFile
_closeRequests = logSupport._errorSound.close


def closeRequests(timeout):
	_releaseHeldSound("requests stopped")
	_closeRequests(timeout)


logSupport._errorSound.close = closeRequests


@atexit.register
def _report():
	result = {
		"plays": _plays,
		"releasedBy": _releasedBy,
		"errorSoundThreadAlive": any(
			t.name == "logHandler.errorSound" and t.is_alive() for t in threading.enumerate()
		),
	}
	with open(os.environ["LATE_ERROR_PROBE_RESULT"], "w", encoding="utf-8") as f:
		json.dump(result, f)


class GlobalPlugin(globalPluginHandler.GlobalPlugin):
	def __init__(self):
		super().__init__()
		# postNvdaStartup holds its handlers weakly; globalPluginHandler keeps this plugin alive.
		core.postNvdaStartup.register(self._onNvdaStarted)

	def _onNvdaStarted(self):
		schedule = sys.modules["utils.schedule"]
		terminate = schedule.terminate

		def failingTerminate():
			terminate()
			_failedAtExit.set()
			raise RuntimeError("the task scheduler fails to terminate, as the test asks")

		schedule.terminate = failingTerminate
		core.triggerNVDAExit()
"""


class LateErrorSoundHeard(AssertionError):
	"""The sound for an error logged late in shutdown cut off the exit sound, or its thread outlived it."""


def test_lateErrorLeavesTheExitSoundWhole(tmp_path: Path):
	"""An error logged in the last step of shutdown cuts off neither the exit sound nor outlives it.

	nvwave plays one file at a time, and the error sound is asked for from a thread of its own, so
	unless shutdown stops those requests first, the sound for a late error can start in the middle
	of the exit sound and stop it.
	"""
	hooks = tmp_path / "hooks"
	(hooks / "globalPlugins").mkdir(parents=True)
	(hooks / "synthDrivers").symlink_to(HERE / "synthDrivers")
	(hooks / "globalPlugins" / "lateErrorProbe.py").write_text(LATE_ERROR_PROBE, encoding="utf-8")
	configDir = tmp_path / "config"
	configDir.mkdir()
	config = (HERE / "fixtures" / "bootConfig" / "nvda.ini").read_text(encoding="utf-8")
	for setting, value in (("playStartAndExitSounds", "True"), ("playErrorSound", "1")):
		config, replaced = re.subn(rf"^(\t{setting} = ).*$", rf"\g<1>{value}", config, flags=re.MULTILINE)
		assert replaced == 1, setting
	(configDir / "nvda.ini").write_text(config, encoding="utf-8")
	capture = tmp_path / "capture"
	capture.mkdir()
	resultPath = tmp_path / "result.json"
	logPath = tmp_path / "nvda.log"
	env = {
		**os.environ,
		"BASEDREADER_TEST_HOOKS_DIR": str(hooks),
		"BASEDREADER_AUDIO_CAPTURE_DIR": str(capture),
		"BASEDREADER_TRANSCRIPT_FILE": str(tmp_path / "transcript.jsonl"),
		"LATE_ERROR_PROBE_RESULT": str(resultPath),
	}
	env.pop("BASEDREADER_BOOT_PROBE", None)
	before = harness.treeState()
	done = subprocess.run(
		[
			sys.executable,
			"-I",
			"-B",
			str(harness.SOURCE / "basedreader.py"),
			*("-c", str(configDir), "-f", str(logPath), "--debug-logging"),
		],
		env=env,
		cwd=tmp_path,
		capture_output=True,
		text=True,
		timeout=600,
		check=False,
	)
	after = harness.treeState()
	assert before == after, "NVDA changed the repository"
	log = logPath.read_text(encoding="utf-8") if logPath.exists() else ""
	assert done.returncode == 0, f"exit status {done.returncode}\n{done.stderr[-3000:]}\n{log[-6000:]}"
	errors = re.findall(r"^(ERROR|CRITICAL) - (\S+) .*\n(.*)$", log, re.MULTILINE)
	assert errors == [("ERROR", "core._terminate", "Error terminating task scheduler")], log[-6000:]
	result = json.loads(resultPath.read_text(encoding="utf-8"))
	# The late error did ask for the sound, so the test exercised what it is about.
	assert ["error.wav", "logHandler.errorSound"] in result["plays"], result
	with wave.open(str(harness.SOURCE / "waves" / "exit.wav"), "rb") as w:
		exitSound = w.readframes(w.getnframes())
	heard = {}
	for path in sorted(capture.iterdir()):
		with wave.open(str(path), "rb") as w:
			heard[path.name] = w.readframes(w.getnframes())
	if not any(frames == exitSound for frames in heard.values()):
		lengths = {name: len(frames) for name, frames in heard.items()}
		raise LateErrorSoundHeard(
			f"no whole exit sound of {len(exitSound)} bytes among {lengths}; "
			f"the held error sound was let go by {result['releasedBy']}",
		)
	if result["errorSoundThreadAlive"]:
		raise LateErrorSoundHeard("the error sound's thread was still running at exit")


def test_noSoundThreadWhenSoundIsOff(tmp_path: Path):
	done = harness.runChild(
		tmp_path,
		startLog(tmp_path / "nvda.log")
		+ """
		import threading
		import config

		config.conf["featureFlag"]["playErrorSound"] = 2
		log.error("an error with the sound off")
		result = [thread.name for thread in threading.enumerate()]
		""",
	)
	done.assertClean(ignoring={"logErrors"})
	assert done.logErrors == ["ERROR: an error with the sound off"]
	assert "logHandler.errorSound" not in done.value


def test_criticalMakesNoSound(tmp_path: Path):
	done = harness.runChild(
		tmp_path,
		startLog(tmp_path / "nvda.log")
		+ """
		import config
		from linuxPlatform import logSupport

		config.conf["featureFlag"]["playErrorSound"] = 1
		calls = []

		def player():
			calls.append(1)

		logHandler.getOnErrorSoundRequested().register(player)
		log.critical("critical by the test")
		# A request starts the sound's thread before the logging call returns.
		result = {
			"calls": len(calls),
			"requested": logSupport._errorSound._thread is not None,
			"log": logText(),
		}
		""",
	)
	done.assertClean(ignoring={"logErrors"})
	assert done.logErrors == ["CRITICAL: critical by the test"]
	assert done.value["calls"] == 0
	assert done.value["requested"] is False
	assert "critical by the test" in done.value["log"]


def test_defaultLogPathHonoursXdgStateHome(tmp_path: Path):
	state = tmp_path / "state"
	done = harness.runChild(
		tmp_path,
		startLog(None)
		+ """
		log.info("in the default place")
		result = globalVars.appArgs.logFileName
		""",
		env={"XDG_STATE_HOME": str(state)},
	)
	done.assertClean()
	logDir = state / "basedreader"
	assert done.value == str(logDir / "nvda.log")
	assert "in the default place" in (logDir / "nvda.log").read_text(encoding="utf-8")
	assert (logDir / "nvda_crash.log").exists()
	assert stat.S_IMODE(logDir.stat().st_mode) == 0o700


def test_logFilesAreTheUsersAlone(tmp_path: Path):
	state = tmp_path / "state"
	# Left open by something else, or by an earlier build.
	(state / "basedreader").mkdir(parents=True, mode=0o755)
	(state / "basedreader").chmod(0o755)
	chosen = tmp_path / "chosen"
	chosen.mkdir()
	(chosen / "shared.log").write_text("")
	(chosen / "shared.log").chmod(0o644)
	done = harness.runChild(
		tmp_path,
		startLog(None)
		+ f"""
		import logging
		import os

		log.info("in the default place")
		logHandler.logHandler.close()
		log.root.removeHandler(logHandler.logHandler)
		globalVars.appArgs.logFileName = {str(chosen / "my.log")!r}
		logHandler.initialize()
		shared = logHandler.FileHandler({str(chosen / "shared.log")!r}, mode="w", encoding="utf-8")
		shared.close()
		result = "returned"
		""",
		env={"XDG_STATE_HOME": str(state)},
	)
	done.assertClean()

	def mode(path: Path) -> int:
		return stat.S_IMODE(path.stat().st_mode)

	assert mode(state / "basedreader") == 0o700
	assert mode(state / "basedreader" / "nvda.log") == 0o600
	assert mode(state / "basedreader" / "nvda_crash.log") == 0o600
	assert mode(chosen / "my.log") == 0o600
	assert mode(chosen / "nvda_crash.log") == 0o600
	# A file that exists keeps the mode it was given.
	assert mode(chosen / "shared.log") == 0o644


def test_defaultLogPathFallsBackToLocalState(tmp_path: Path):
	home = tmp_path / "home"
	home.mkdir()
	done = harness.runChild(
		tmp_path,
		startLog(None)
		+ """
		log.info("in the fallback place")
		result = globalVars.appArgs.logFileName
		""",
		# A relative value is invalid under the base directory specification.
		env={"XDG_STATE_HOME": "relative/state", "HOME": str(home)},
	)
	done.assertClean()
	logFile = home / ".local" / "state" / "basedreader" / "nvda.log"
	assert done.value == str(logFile)
	assert "in the fallback place" in logFile.read_text(encoding="utf-8")


def test_logFileOptionAndRedirection(tmp_path: Path):
	logDir = tmp_path / "chosen"
	logDir.mkdir()
	(logDir / "my.log").write_text("the previous run\n", encoding="utf-8")
	done = harness.runChild(
		tmp_path,
		f"""
		import argsParsing
		import globalVars
		import logHandler

		options, _ = argsParsing.getParser().parse_known_args(["-f", {str(logDir / "my.log")!r}])
		globalVars.appArgs.logFileName = options.logFileName
		logHandler.initialize()

		def logText():
			logHandler.logHandler.flush()
			with open(globalVars.appArgs.logFileName, encoding="utf-8") as f:
				return f.read()
		"""
		+ """
		import sys

		print("printed by the test")
		sys.stderr.write("written to standard error by the test\\n")
		result = logText()
		""",
	)
	done.assertClean(ignoring={"logErrors"})
	assert done.logErrors == ["ERROR: written to standard error by the test"]
	text = done.value
	assert re.search(
		rf"^WARNING - stdout \({TIME}\) - MainThread \(\d+\):\nprinted by the test$", text, re.MULTILINE
	)
	assert re.search(
		rf"^ERROR - stderr \({TIME}\) - MainThread \(\d+\):\nwritten to standard error by the test$",
		text,
		re.MULTILINE,
	)
	assert (logDir / "nvda-old.log").read_text(encoding="utf-8") == "the previous run\n"
	assert "printed by the test" not in done.stdout


def test_crashDumpBesideLog(tmp_path: Path):
	logDir = tmp_path / "logs"
	logDir.mkdir()
	done = harness.runChild(
		tmp_path,
		startLog(logDir / "nvda.log")
		+ """
		import ctypes
		import faulthandler
		import os
		from linuxPlatform import logSupport

		PR_SET_DUMPABLE = 4

		def crashHere():
			faulthandler._sigsegv()

		pid = os.fork()
		if pid == 0:
			# Not dumpable, so the kernel writes no core file and starts no core handler.
			ctypes.CDLL(None).prctl(PR_SET_DUMPABLE, 0, 0, 0, 0)
			crashHere()
			os._exit(3)
		_, status = os.waitpid(pid, 0)
		crashed = os.WIFSIGNALED(status) and os.WTERMSIG(status) == 11
		with open(os.path.join(os.path.dirname(globalVars.appArgs.logFileName), "nvda_crash.log")) as f:
			dump = f.read()
		logSupport.enableCrashDump(globalVars.appArgs.logFileName)
		with open(os.path.join(os.path.dirname(globalVars.appArgs.logFileName), "nvda_crash-old.log")) as f:
			oldAfterNextStart = f.read()
		# A start after that, with no crash in between, must not replace the old dump with an empty one.
		logSupport.enableCrashDump(globalVars.appArgs.logFileName)
		result = {
			"crashed": crashed,
			"dump": dump,
			"oldAfterNextStart": oldAfterNextStart,
			"enabled": faulthandler.is_enabled(),
			"dumpFileOpen": not logSupport._crashDumpFile.closed,
		}
		""",
	)
	done.assertClean()
	assert done.value["crashed"]
	dump = done.value["dump"]
	assert dump.startswith("Fatal Python error: Segmentation fault"), dump
	assert re.search(
		r"Current thread 0x[0-9a-f]+ .*\n(  File .*\n)*  File .*, line \d+ in crashHere\n", dump
	), dump
	assert done.value["enabled"]
	assert done.value["dumpFileOpen"]
	# The next start keeps the dump as the old one and starts an empty file, and so does every start
	# after it until the next crash.
	assert done.value["oldAfterNextStart"] == dump
	assert (logDir / "nvda_crash-old.log").read_text() == dump
	assert (logDir / "nvda_crash.log").read_text() == ""


def test_emptyCrashDumpLeavesTheOldOneAlone(tmp_path: Path):
	logDir = tmp_path / "logs"
	logDir.mkdir()
	(logDir / "nvda_crash.log").write_text("")
	(logDir / "nvda_crash-old.log").write_text("the last crash\n")
	done = harness.runChild(
		tmp_path,
		startLog(logDir / "nvda.log")
		+ """
		import faulthandler

		result = faulthandler.is_enabled()
		""",
	)
	done.assertClean()
	assert done.value is True
	assert (logDir / "nvda_crash-old.log").read_text() == "the last crash\n"
	assert (logDir / "nvda_crash.log").read_text() == ""


def test_crashDumpsOffWhenNothingCanTakeThem(tmp_path: Path):
	logDir = tmp_path / "logs"
	logDir.mkdir()
	for name in ("nvda_crash.log", "nvda_crash-old.log"):
		(logDir / name).mkdir()
		(logDir / name / "occupied").write_text("")
	done = harness.runChild(
		tmp_path,
		startLog(logDir / "nvda.log")
		+ """
		import faulthandler
		import sys
		from linuxPlatform import logSupport

		faulthandler.disable()
		# As under a service manager that gives the process no standard error.
		realStderr = sys.__stderr__
		sys.__stderr__ = None
		try:
			logSupport.enableCrashDump(globalVars.appArgs.logFileName)
		finally:
			sys.__stderr__ = realStderr
		result = {"enabled": faulthandler.is_enabled(), "log": logText()}
		""",
	)
	done.assertClean()
	assert done.value["enabled"] is False
	assert re.search(
		r"^WARNING - linuxPlatform\.logSupport\.enableCrashDump .*\n"
		r"Crash dumps are off: there is no standard error$",
		done.value["log"],
		re.MULTILINE,
	), done.value["log"]


def test_crashDumpKeptWhenItCannotBeMoved(tmp_path: Path):
	logDir = tmp_path / "logs"
	logDir.mkdir()
	(logDir / "nvda_crash.log").write_text("the last crash\n")
	# A directory in the old dump's place makes the move fail.
	(logDir / "nvda_crash-old.log").mkdir()
	(logDir / "nvda_crash-old.log" / "occupied").write_text("")
	done = harness.runChild(
		tmp_path,
		startLog(logDir / "nvda.log")
		+ """
		import faulthandler

		result = faulthandler.is_enabled()
		""",
	)
	done.assertClean()
	assert done.value is True
	assert (logDir / "nvda_crash.log").read_text() == "the last crash\n"


def test_crashDumpWithoutLogFile(tmp_path: Path):
	state = tmp_path / "state"
	done = harness.runChild(
		tmp_path,
		startLog(tmp_path / "missing" / "nvda.log")
		+ """
		import faulthandler

		result = {"enabled": faulthandler.is_enabled(), "logFileName": globalVars.appArgs.logFileName}
		""",
		env={"XDG_STATE_HOME": str(state)},
	)
	done.assertClean(ignoring={"logErrors"})
	assert done.logErrors == ["ERROR: Faile to open log file, redirecting to standard output"]
	assert done.value == {"enabled": True, "logFileName": None}
	assert (state / "basedreader" / "nvda_crash.log").exists()


def test_crashDumpThatCannotBeOpenedIsReported(tmp_path: Path):
	logDir = tmp_path / "logs"
	logDir.mkdir()
	# Directories in both places: the dump can neither be moved aside nor opened.
	for name in ("nvda_crash.log", "nvda_crash-old.log"):
		(logDir / name).mkdir()
		(logDir / name / "occupied").write_text("")
	done = harness.runChild(
		tmp_path,
		startLog(logDir / "nvda.log")
		+ """
		import faulthandler

		result = {"enabled": faulthandler.is_enabled(), "log": logText()}
		""",
	)
	done.assertClean()
	# The real standard error takes the dump instead.
	assert done.value["enabled"] is True
	assert re.search(
		r"^WARNING - linuxPlatform\.logSupport\.enableCrashDump .*\nCannot write crash dumps to "
		+ re.escape(str(logDir)),
		done.value["log"],
		re.MULTILINE,
	), done.value["log"]


WATCHDOG_FAST = """
		import time
		import watchdog

		watchdog.MIN_CORE_ALIVE_TIMEOUT = 0.05
		watchdog.NORMAL_CORE_ALIVE_TIMEOUT = 0.2
		watchdog.FROZEN_WARNING_TIMEOUT = 0.3
		watchdog.RECOVER_ATTEMPT_INTERVAL = 0.01

		def waitFor(condition, bound=60):
			deadline = time.monotonic() + bound
			while not condition() and time.monotonic() < deadline:
				time.sleep(0.01)
			return condition()
"""


def test_watchdogLogsStackOfFrozenCore(tmp_path: Path):
	done = harness.runChild(
		tmp_path,
		startLog(tmp_path / "nvda.log")
		+ WATCHDOG_FAST
		+ """
		import select
		import threading

		import exceptions

		frozenReported = whenLogged("Core frozen in stack!")
		watchdog.initialize()
		fd = watchdog.cancelCallFd()

		def readable():
			return bool(select.select([fd], [], [], 0)[0])

		before = {"cancelling": watchdog.isCancellingCalls(), "readable": readable()}

		def tryCall():
			try:
				return watchdog.cancellableExecute(lambda: "ran")
			except exceptions.CallCancelled:
				return "refused"

		def coreStuckInTest():
			watchdog.alive()
			# Frozen until recovery is cancelling calls and the core has been reported stuck.
			waitFor(watchdog.isCancellingCalls)
			frozenReported.wait(60)
			fromWorker = []
			worker = threading.Thread(target=lambda: fromWorker.append(tryCall()))
			worker.start()
			worker.join(60)
			return {
				"recovering": watchdog.isAttemptingRecovery,
				"mainThread": tryCall(),
				"otherThread": fromWorker,
				"cancelling": watchdog.isCancellingCalls(),
				"readable": readable(),
			}

		during = coreStuckInTest()
		watchdog.asleep()
		waitFor(lambda: not watchdog.isAttemptingRecovery)
		after = {
			"recovering": watchdog.isAttemptingRecovery,
			"cancelling": watchdog.isCancellingCalls(),
			"readable": readable(),
		}

		def coreStuckAgainInTest():
			watchdog.alive()
			return waitFor(watchdog.isCancellingCalls)

		# Frozen again, and the screen reader stops before the core recovers.
		frozenAgain = coreStuckAgainInTest()
		watchdog.terminate()
		terminated = {"cancelling": watchdog.isCancellingCalls(), "readable": readable()}
		result = {
			"before": before,
			"during": during,
			"after": after,
			"frozenAgain": frozenAgain,
			"terminated": terminated,
			"sameFd": watchdog.cancelCallFd() == fd,
			"log": logText(),
		}
		""",
	)
	done.assertClean(ignoring={"logErrors"})
	assert done.logErrors, "no frozen core was reported"
	assert all(record.startswith("ERROR: Core frozen in stack!") for record in done.logErrors), done.logErrors
	assert done.value["before"] == {"cancelling": False, "readable": False}
	# Only the frozen main thread gives up; a call on any other thread is not what froze the core.
	assert done.value["during"] == {
		"recovering": True,
		"mainThread": "refused",
		"otherThread": ["ran"],
		"cancelling": True,
		"readable": True,
	}
	assert done.value["after"] == {"recovering": False, "cancelling": False, "readable": False}
	assert done.value["frozenAgain"] is True
	assert done.value["terminated"] == {"cancelling": False, "readable": False}
	assert done.value["sameFd"] is True
	text = done.value["log"]
	assert "Starting freeze recovery after" in text
	assert "Recovered from freeze after" in text
	listings = re.findall(
		r"^INFO - watchdog\.waitForFreezeRecovery .*\nListing stacks for Python threads:\n(.*?)(?=^\w+ - )",
		text,
		re.MULTILINE | re.DOTALL,
	)
	assert listings, text
	mainStacks = [
		stack
		for listing in listings
		for stack in listing.split("Python stack for thread ")
		if "(MainThread):" in stack
	]
	assert mainStacks and all(
		"in coreStuckInTest" in stack or "in coreStuckAgainInTest" in stack for stack in mainStacks
	), listings


def test_watchdogQuietWhileCoreRuns(tmp_path: Path):
	done = harness.runChild(
		tmp_path,
		startLog(tmp_path / "nvda.log")
		+ """
		import threading
		import time
		import watchdog

		# Shorter than upstream's, so that the idle waits below outlast them, and still far longer
		# than a busy machine stretches the core's few milliseconds of work.
		watchdog.MIN_CORE_ALIVE_TIMEOUT = 0.25
		watchdog.NORMAL_CORE_ALIVE_TIMEOUT = 1.0
		watchdog.FROZEN_WARNING_TIMEOUT = 1.5
		outlastingEveryTimeout = 1.7

		# The core pump calls these whether or not the watchdog was started.
		watchdog.alive()
		watchdog.asleep()
		watchdog.initialize()
		deadline = time.perf_counter() + 1
		while time.perf_counter() < deadline:
			watchdog.alive()
			time.sleep(0.002)
			watchdog.asleep()
			time.sleep(0.005)
		# An idle core is not a frozen one.
		time.sleep(outlastingEveryTimeout)
		with watchdog.Suspender():
			time.sleep(outlastingEveryTimeout)
		value = watchdog.cancellableExecute(lambda a, b=0: a + b, 1, b=2)
		recovering = watchdog.WatchdogObserver().isAttemptingRecovery
		count = 100000
		# CPU time, which a loaded machine stretches far less than it does the clock.
		started = time.thread_time()
		for _ in range(count):
			watchdog.alive()
			watchdog.asleep()
		perPump = (time.thread_time() - started) / count
		watchdog.terminate()
		result = {
			"value": value,
			"recovering": recovering,
			"asleep": watchdog.isCoreAsleep(),
			"perPump": perPump,
			"watcherLeft": any(t.name == "watchdog" for t in threading.enumerate()),
			"log": logText(),
		}
		""",
	)
	done.assertClean()
	assert done.value["value"] == 3
	assert done.value["recovering"] is False
	assert done.value["asleep"] is True
	assert not done.value["watcherLeft"]
	assert "Starting freeze recovery" not in done.value["log"]
	# alive() and asleep() run around every queued function; a pair costs about a microsecond.
	assert done.value["perPump"] < 20e-6, done.value["perPump"]


def test_watchdogRunsCallsWhileSuspended(tmp_path: Path):
	done = harness.runChild(
		tmp_path,
		startLog(tmp_path / "nvda.log")
		+ """
		import exceptions
		import NVDAState
		import watchdog

		watchdog.initialize()
		try:
			watchdog.initialize()
			again = "started twice"
		except RuntimeError as e:
			again = str(e)
		with watchdog.Suspender():
			# The watcher can begin a recovery just as the core suspends it for a modal loop.
			watchdog._startCancellingCalls()
			cancelling = watchdog.isCancellingCalls()
			try:
				suspended = watchdog.cancellableExecute(lambda: "ran")
			except exceptions.CallCancelled:
				suspended = "refused"
		cancellingAfter = watchdog.isCancellingCalls()
		watchdog.terminate()
		NVDAState._allowDeprecatedAPI = lambda: True
		moved = watchdog.getFormattedStacksForAllThreads is logHandler.getFormattedStacksForAllThreads
		try:
			watchdog.noSuchAttribute
			missing = "found"
		except AttributeError as e:
			missing = [type(e).__name__, isinstance(e, NotImplementedError)]
		result = {
			"again": again,
			"cancelling": cancelling,
			"suspended": suspended,
			"cancellingAfter": cancellingAfter,
			"moved": moved,
			"missing": missing,
			"log": logText(),
		}
		""",
	)
	# A name the replacement lacks is a missing feature, which hasattr still reads as absent.
	assert done.noLinuxImplementation == ["watchdog.noSuchAttribute"]
	done.assertClean(ignoring={"noLinuxImplementation"})
	assert {key: value for key, value in done.value.items() if key != "log"} == {
		"again": "already running",
		"cancelling": True,
		"suspended": "ran",
		"cancellingAfter": False,
		"moved": True,
		"missing": ["NoLinuxAttribute", True],
	}
	assert "Importing getFormattedStacksForAllThreads from here is deprecated" in done.value["log"]


def test_watchdogSendMessageIsWindowsOnly(tmp_path: Path):
	done = harness.runChild(
		tmp_path,
		"""
		import watchdog

		try:
			watchdog.cancellableSendMessage(0, 0, 0, 0)
		except NotImplementedError as e:
			result = str(e)
		""",
	)
	done.assertClean(ignoring={"noLinuxImplementation"})
	assert done.noLinuxImplementation == ["watchdog.cancellableSendMessage"]
	assert "watchdog.cancellableSendMessage" in done.value
