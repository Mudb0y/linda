"""NVDA's log, the watchdog and crash dumps work on Linux without sound, recursion or Windows calls.

nix develop . -c tests/linux/rig python3 -m pytest -p no:cacheprovider tests/linux/test_logging.py
"""

import re
import stat
from pathlib import Path

import harness


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
		import sys
		import exceptions

		standIn = sys.modules.pop("comtypes")
		try:
			try:
				open("/nonexistent/basedreader-test")
			except OSError:
				log.exception("plain OSError")
			comtypesImported = "comtypes" in sys.modules
		finally:
			sys.modules["comtypes"] = standIn
		try:
			raise WindowsError(None, "timed out", None, 1460)
		except OSError:
			log.exception("Windows timeout")
		try:
			raise exceptions.CallCancelled()
		except exceptions.CallCancelled:
			log.exception("cancelled call")
		result = {"log": logText(), "comtypesImported": comtypesImported}
		""",
	)
	assert done.error is None, done.error
	assert not done.noLinuxImplementation, done.noLinuxImplementation
	assert done.logErrors == ["ERROR: plain OSError"]
	assert done.value["comtypesImported"] is False
	text = done.value["log"]
	assert re.search(r"^ERROR - .*\nplain OSError\nTraceback", text, re.MULTILINE), text
	assert "FileNotFoundError: [Errno 2]" in text
	# Upstream's expected errors are logged below ERROR, as on Windows.
	assert re.search(r"^DEBUGWARNING - .*\nWindows timeout\n", text, re.MULTILINE), text
	assert re.search(r"^DEBUGWARNING - .*\ncancelled call\n", text, re.MULTILINE), text


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
	done.assertClean()
	assert done.value == "returned"
	assert done.stderr.count("--- Logging error") == 2, done.stderr
	assert "ValueError: formatting broken by the test" in done.stderr
	assert "RecursionError" not in done.stderr


def test_errorSoundCannotRecurseOrBlock(tmp_path: Path):
	done = harness.runChild(
		tmp_path,
		startLog(tmp_path / "nvda.log")
		+ """
		import threading
		import time
		import config

		config.conf["featureFlag"]["playErrorSound"] = 1
		calls = []
		release = threading.Event()

		def stuckFailingPlayer():
			calls.append(threading.current_thread().name)
			release.wait(5)
			raise RuntimeError("player failed, as the test asks")

		logHandler.getOnErrorSoundRequested().register(stuckFailingPlayer)
		started = time.perf_counter()
		log.error("first error")
		log.error("second error")
		elapsed = time.perf_counter() - started
		time.sleep(0.3)
		release.set()
		time.sleep(1)
		result = {"elapsed": elapsed, "calls": calls}
		""",
	)
	assert done.error is None, done.error
	assert not done.noLinuxImplementation, done.noLinuxImplementation
	# The logging thread never waits for the player.
	assert done.value["elapsed"] < 0.5, done.value
	# The second error arrived while the sound was being requested and asks for nothing. The
	# player's own failure is logged on the sound's thread, so nothing follows it either.
	assert done.value["calls"] == ["logHandler.errorSound"], done.value
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
		logSupport.ERROR_SOUND_QUIET_PERIOD = 0.5
		plays = []
		failing = True

		def playerWhosePlaybackFails():
			plays.append(time.perf_counter())

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
		time.sleep(2)
		loopPlays = len(plays)
		failing = False
		log.error("an error while errors are quiet")
		time.sleep(0.3)
		result = {"loopPlays": loopPlays, "plays": len(plays), "log": logText()}
		""",
	)
	assert done.error is None, done.error
	assert not done.noLinuxImplementation, done.noLinuxImplementation
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

		config.conf["featureFlag"]["playErrorSound"] = 1
		created = []

		class FailingPlayer:
			# Not an OSError, so nvwave's own error state never stops the replays.
			audioDeviceError_static = False

			def __init__(self, **kwargs):
				created.append(1)

			def feed(self, data):
				raise RuntimeError("feed failed, as the test asks")

			def idle(self):
				pass

			def stop(self):
				pass

		nvwave.WavePlayer = FailingPlayer
		logHandler.getOnErrorSoundRequested().register(nvwave.playErrorSound)
		log.error("one error")
		time.sleep(2)
		result = {"players": len(created)}
		""",
	)
	assert done.error is None, done.error
	assert not done.noLinuxImplementation, done.noLinuxImplementation
	# Upstream's playWaveFile sometimes ends the chain sooner, through a race of its own.
	assert 1 <= done.value["players"] <= 3, done.value


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
		timeout=30,
	)
	assert done.error is None, done.error
	assert done.value == "returned"
	assert sorted(done.logErrors) == ["ERROR: first error", "ERROR: logged while the sound thread starts"]


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
	assert done.error is None, done.error
	assert done.logErrors == ["ERROR: an error with the sound off"]
	assert "logHandler.errorSound" not in done.value


def test_criticalMakesNoSound(tmp_path: Path):
	done = harness.runChild(
		tmp_path,
		startLog(tmp_path / "nvda.log")
		+ """
		import time
		import config

		config.conf["featureFlag"]["playErrorSound"] = 1
		calls = []

		def player():
			calls.append(1)

		logHandler.getOnErrorSoundRequested().register(player)
		log.critical("critical by the test")
		time.sleep(0.5)
		result = {"calls": len(calls), "log": logText()}
		""",
	)
	assert done.error is None, done.error
	assert not done.noLinuxImplementation, done.noLinuxImplementation
	assert done.logErrors == ["CRITICAL: critical by the test"]
	assert done.value["calls"] == 0
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
	assert done.error is None, done.error
	assert not done.noLinuxImplementation, done.noLinuxImplementation
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
		result = {"crashed": crashed, "dump": dump, "enabled": faulthandler.is_enabled()}
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
	# The next start keeps the dump as the old one and starts an empty file.
	assert (logDir / "nvda_crash-old.log").read_text() == dump
	assert (logDir / "nvda_crash.log").read_text() == ""


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
	assert done.error is None, done.error
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
"""


def test_watchdogLogsStackOfFrozenCore(tmp_path: Path):
	done = harness.runChild(
		tmp_path,
		startLog(tmp_path / "nvda.log")
		+ WATCHDOG_FAST
		+ """
		import exceptions

		watchdog.initialize()

		import select

		fd = watchdog.cancelCallFd()

		def readable():
			return bool(select.select([fd], [], [], 0)[0])

		before = {"cancelling": watchdog.isCancellingCalls(), "readable": readable()}

		def coreStuckInTest():
			watchdog.alive()
			time.sleep(1.5)
			try:
				watchdog.cancellableExecute(lambda: "ran")
				refused = False
			except exceptions.CallCancelled:
				refused = True
			return {
				"recovering": watchdog.isAttemptingRecovery,
				"refused": refused,
				"cancelling": watchdog.isCancellingCalls(),
				"readable": readable(),
			}

		during = coreStuckInTest()
		watchdog.asleep()
		time.sleep(0.2)
		after = {
			"recovering": watchdog.isAttemptingRecovery,
			"cancelling": watchdog.isCancellingCalls(),
			"readable": readable(),
		}

		def coreStuckAgainInTest():
			watchdog.alive()
			time.sleep(0.6)
			return watchdog.isCancellingCalls()

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
	assert done.error is None, done.error
	assert not done.noLinuxImplementation, done.noLinuxImplementation
	assert done.logErrors, "no frozen core was reported"
	assert all(record.startswith("ERROR: Core frozen in stack!") for record in done.logErrors), done.logErrors
	assert done.value["before"] == {"cancelling": False, "readable": False}
	assert done.value["during"] == {"recovering": True, "refused": True, "cancelling": True, "readable": True}
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
		+ WATCHDOG_FAST
		+ """
		import threading

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
		# Asleep for longer than every threshold: an idle core is not a frozen one.
		time.sleep(0.6)
		with watchdog.Suspender():
			time.sleep(0.3)
		value = watchdog.cancellableExecute(lambda a, b=0: a + b, 1, b=2)
		recovering = watchdog.WatchdogObserver().isAttemptingRecovery
		count = 100000
		started = time.perf_counter()
		for _ in range(count):
			watchdog.alive()
			watchdog.asleep()
		perPump = (time.perf_counter() - started) / count
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
	assert "freeze" not in done.value["log"]
	# alive() and asleep() run around every queued function; a pair costs about a microsecond.
	assert done.value["perPump"] < 20e-6, done.value["perPump"]


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
	assert done.error is None, done.error
	assert done.noLinuxImplementation == ["watchdog.cancellableSendMessage"]
	assert not done.logErrors
	assert "watchdog.cancellableSendMessage" in done.value
