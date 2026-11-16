"""The launcher as a real process: NVDA starts, speaks through the transcript synth, and exits.

Each test starts source/basedreader.py with a configuration from fixtures/bootConfig, the transcript
synth, and the boot probe plugin from fixtures/globalPlugins, which says one sentence once NVDA has
started. Run under the rig, which gives every launch its display and its runtime directory, where the
single-instance lock is:

	nix develop . -c tests/linux/rig python3 -m pytest tests/linux/test_boot.py
"""

import importlib.util
import json
import os
import re
import select
import signal
import struct
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import configobj
import harness
import pytest

HERE = Path(__file__).resolve().parent
LAUNCHER = harness.SOURCE / "basedreader.py"
CONFIG_FIXTURE = HERE / "fixtures" / "bootConfig" / "nvda.ini"
#: What fixtures/globalPlugins/bootProbe.py says.
SENTENCE = "the boot probe has started"
DIALOG_SENTENCE = "the boot probe's dialog is open"
EXIT_DIALOG_SENTENCE = "the exit dialog is open"
#: The setting the probe changes before it opens a dialog or hangs, as section, key and value.
CHANGED_SETTING = ("keyboard", "speakTypedWords", "2")
#: Seconds from starting the process to the sentence; a start under the rig takes about three.
STARTUP_BOUND = 60.0
#: Seconds from asking NVDA to exit to the process being gone; it takes well under one.
SHUTDOWN_BOUND = 20.0
#: The display the rig starts, a number no live display has.
RIG_DISPLAY = ":99"
#: What wx's calls return, as the probe logs them.
WX_CANCEL = 16
WX_ID_CANCEL = 5101
WX_ID_CLOSE_ALL = 5021
#: linuxPlatform.modalDialogs.GIVEN_UP_STATUS: NVDA ended without finishing its exit.
GIVEN_UP_STATUS = 1


def _loadFile(name: str, path: Path) -> ModuleType:
	# Loaded on their own, as both need only the standard library, rather than by putting source/ on
	# this process's path, where NVDA's module names would shadow others.
	spec = importlib.util.spec_from_file_location(name, path)
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module


transcriptRecords = _loadFile("_transcriptRecords", HERE / "synthDrivers" / "_transcriptRecords.py")
singleInstance = _loadFile("singleInstance", harness.SOURCE / "linuxPlatform" / "singleInstance.py")

#: The first line of each log record: level, code path, time, thread. The code path can hold spaces:
#: logHandler writes "unhandled exception", "Exception ignored in: <...>" and "Python warning" there.
_recordHeader = re.compile(r"^(?P<level>[A-Z_]+) - .+? \(\d\d:\d\d:\d\d\.\d{3}\) - .*:$", re.MULTILINE)
#: Any line that starts as a record at ERROR or above does, whatever follows it.
_errorRecord = re.compile(r"^(?:ERROR|CRITICAL) - .*$", re.MULTILINE)
#: What the probe logs for each call that showed a dialog, once it has returned.
_answer = re.compile(r"^boot probe: (?P<name>\w+) answered (?P<answer>None|-?\d+)$", re.MULTILINE)
#: What standard error may hold after a clean run, each with the reason it is allowed.
_allowedStandardError = (
	# NVDAHighlighter keeps its settings in a class attribute and globalVars.settingsRing keeps the
	# last synth, both for the life of the process, so AutoSettings.__del__ runs only as the
	# interpreter takes config apart, and fails. Windows does the same, with no standard error to show
	# it.
	re.compile(
		r"Exception ignored in: <function AutoSettings\.__del__ at 0x[0-9a-f]+>\n"
		r"Traceback \(most recent call last\):\n"
		r"(?:  File \"[^\"\n]*/autoSettingsUtils/autoSettings\.py\", line \d+, in \w+\n(?:    .*\n)*)+"
		r"AttributeError: 'NoneType' object has no attribute 'unregister'\n",
	),
)
#: What a clean run may record as missing on Linux, because the code records the gap rather than
#: raising, each with where and why.
_notedGaps = (
	# gui.message.MessageDialog._playSound: Windows' system sound for an error, warning or question
	# box. Raising would stop the dialog before it focuses its default button.
	"winsound.MessageBeep",
)


def unexpectedStandardError(text: str, alsoAllowed: tuple[re.Pattern, ...] = ()) -> str:
	"""What is left of standard error once the allowed lines are taken out."""
	for pattern in (*_allowedStandardError, *alsoAllowed):
		text = pattern.sub("", text)
	return text


def errorRecords(log: str) -> list[str]:
	"""The records at ERROR or above in an NVDA log, or a line that looks like one."""
	return _errorRecord.findall(log)


def _gsettingsSchemaEnvironment() -> dict[str, str]:
	"""XDG_DATA_DIRS with the GSettings schemas of the GTK libraries the dev shell provides.

	GTK's file chooser aborts the process when it finds no schema for itself. The rig's data
	directories hold the development outputs of these libraries, which have none; the outputs whose
	typelibs are on GI_TYPELIB_PATH have them.
	"""
	directories = []
	for typelibs in os.environ.get("GI_TYPELIB_PATH", "").split(os.pathsep):
		share = Path(typelibs).parent.parent / "share"
		directories += [str(path) for path in sorted(share.glob("gsettings-schemas/*")) if path.is_dir()]
	return {"XDG_DATA_DIRS": os.pathsep.join([*directories, os.environ.get("XDG_DATA_DIRS", "")])}


#: GTK's file chooser, finding no icon theme at all: the rig's data directories hold none, not even
#: hicolor, the fallback every desktop has. Nothing NVDA says or does depends on the icons.
_noIconTheme = re.compile(
	r"\n?\(basedreader\.py:\d+\): Gtk-WARNING \*\*: [\d:.]+: Could not find the icon '[\w-]+'\. The "
	r"'hicolor' theme\nwas not found either, perhaps you need to install it\.\nYou can get a copy from:\n"
	r"\thttp://icon-theme\.freedesktop\.org/releases\n",
)


class Launch:
	"""One run of the launcher, with its own log, transcript and record of NoLinuxImplementation."""

	def __init__(self, directory: Path, process: subprocess.Popen, started: float, configDir: Path) -> None:
		self.directory = directory
		self.process = process
		self.started = started
		self.configDir = configDir

	def log(self, name: str = "nvda.log") -> str:
		path = self.directory / name
		return path.read_text(encoding="utf-8") if path.exists() else ""

	def sentences(self, text: str = SENTENCE) -> list[dict]:
		return [r for r in self.spoken() if transcriptRecords.spokenText(r) == text]

	def spoken(self) -> list[dict]:
		path = self.directory / "transcript.jsonl"
		if not path.exists():
			return []
		return [r for r in transcriptRecords.readRecords(path) if r["kind"] == "speak"]

	def recorded(self) -> list[str]:
		"""What each NoLinuxImplementation made in the run names, raised or not."""
		path = self.directory / "noLinuxImplementation.jsonl"
		lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
		return [json.loads(line)["what"] for line in lines]

	def answers(self) -> dict[str, int | None]:
		"""What each call that showed one of the probe's dialogs got back."""
		return {
			m["name"]: None if m["answer"] == "None" else int(m["answer"])
			for m in _answer.finditer(self.log())
		}

	def savedSetting(self) -> str | None:
		"""The value nvda.ini on disk holds for the setting the probe changes."""
		section, key, _value = CHANGED_SETTING
		path = self.configDir / "nvda.ini"
		return configobj.ConfigObj(str(path)).get(section, {}).get(key) if path.exists() else None

	def describe(self) -> str:
		parts = [f"exit status {self.process.poll()}"]
		for name in ("stdout.txt", "stderr.txt", "noLinuxImplementation.jsonl"):
			path = self.directory / name
			text = path.read_text(encoding="utf-8") if path.exists() else ""
			if text:
				parts.append(f"{name}:\n{text[-3000:]}")
		parts.append(f"log tail:\n{self.log()[-6000:]}")
		return "\n".join(parts)

	def waitForSentence(self, timeout: float = STARTUP_BOUND, text: str = SENTENCE) -> float:
		"""Seconds from starting the process to the sentence being spoken."""
		deadline = self.started + timeout
		while time.monotonic() < deadline:
			if sentences := self.sentences(text):
				# The synth stamps records with the same monotonic clock, which is system wide.
				return sentences[0]["t"] - self.started
			if self.process.poll() is not None:
				break
			time.sleep(0.05)
		pytest.fail(f"{text!r} was not spoken within {timeout} s\n{self.describe()}")

	def waitForLog(self, text: str, timeout: float = SHUTDOWN_BOUND) -> None:
		deadline = time.monotonic() + timeout
		while text not in self.log():
			if time.monotonic() >= deadline or self.process.poll() is not None:
				pytest.fail(f"{text!r} was not logged within {timeout} s\n{self.describe()}")
			time.sleep(0.05)

	def waitForExit(self, timeout: float = SHUTDOWN_BOUND) -> int:
		try:
			return self.process.wait(timeout)
		except subprocess.TimeoutExpired:
			pytest.fail(f"NVDA did not exit within {timeout} s\n{self.describe()}")

	def assertClean(
		self,
		logNames: tuple[str, ...] = ("nvda.log",),
		alsoAllowed: tuple[re.Pattern, ...] = (),
	) -> None:
		"""The run raised no NoLinuxImplementation, even one upstream caught, logged no error, and wrote
		nothing unexpected to standard error.

		:param alsoAllowed: What this run's standard error may hold besides what every run's may.
		"""
		recorded = self.directory / "noLinuxImplementation.jsonl"
		assert recorded.exists(), "the launcher did not record NoLinuxImplementation as asked"
		raised = [json.loads(line) for line in recorded.read_text(encoding="utf-8").splitlines()]
		raised = [r for r in raised if r["what"] not in _notedGaps]
		assert not raised, "NoLinuxImplementation raised:\n" + "\n".join(
			r["what"] + "\n" + "".join(r["stack"]) for r in raised
		)
		for name in logNames:
			log = self.log(name)
			assert _recordHeader.search(log), f"{name} holds no records\n{self.describe()}"
			errors = errorRecords(log)
			assert not errors, f"{name} holds {len(errors)} records at ERROR or above\n{log[-8000:]}"
		stderr = self.log("stderr.txt")
		assert not unexpectedStandardError(stderr, alsoAllowed), f"standard error holds:\n{stderr[-4000:]}"


def _rigLockPath() -> str:
	"""The lock file of the rig's display.

	Tests signal whatever holds it. Outside the rig it would be the live session's display, and its
	holder the screen reader the machine's owner is using.
	"""
	why = harness.whyNotInRig()
	assert why is None, f"refusing to touch a display's lock outside the rig: {why}"
	assert os.environ.get("DISPLAY") == RIG_DISPLAY, f"DISPLAY is {os.environ.get('DISPLAY')!r}"
	path = singleInstance.lockFilePath()
	runtime = os.environ["XDG_RUNTIME_DIR"]
	assert path.startswith(runtime + os.sep) and not runtime.startswith("/run/"), path
	return path


def _rigLock() -> "singleInstance.InstanceLock":
	return singleInstance.InstanceLock(_rigLockPath())


def _runningCopy() -> int | None:
	"""The process holding the lock of the rig's display."""
	lock = _rigLock()
	try:
		return lock.holder()
	finally:
		lock.close()


def _readyCopy() -> int | None:
	"""The process recorded in the rig's lock file as acting on SIGTERM."""
	lock = _rigLock()
	try:
		return lock.recordedReady()
	finally:
		lock.close()


@pytest.fixture
def launcher(tmp_path: Path) -> Iterator:
	"""Starts launches, and makes sure none outlives the test or wrote into the repository."""
	_rigLockPath()
	launches: list[Launch] = []
	before = harness.treeState()
	# The test hooks take one directory holding both packages' directories.
	hooks = tmp_path / "hooks"
	hooks.mkdir()
	(hooks / "synthDrivers").symlink_to(HERE / "synthDrivers")
	(hooks / "globalPlugins").symlink_to(HERE / "fixtures" / "globalPlugins")

	def start(
		name: str,
		probe: str | None = None,
		*args: str,
		configDir: Path | None = None,
		welcome: bool = True,
		env: dict[str, str | None] | None = None,
	) -> Launch:
		"""Start the launcher in a directory of its own.

		:param probe: What the boot probe does, and with it a fixture configuration, a log and debug
			logging; None for a launch that only acts on another copy.
		:param welcome: Whether the configuration leaves the welcome dialog of a first start on.
		:param env: Variables to set, or to unset where the value is None.
		"""
		directory = tmp_path / "launches" / name
		directory.mkdir(parents=True)
		if configDir is None:
			configDir = directory / "config"
		if probe and not configDir.exists():
			configDir.mkdir()
			text = CONFIG_FIXTURE.read_text(encoding="utf-8")
			if not welcome:
				text = text.replace("[general]\n", "[general]\n\tshowWelcomeDialogAtStartup = False\n", 1)
			(configDir / "nvda.ini").write_text(text, encoding="utf-8")
		environment = {
			**os.environ,
			"BASEDREADER_TEST_HOOKS_DIR": str(hooks),
			"BASEDREADER_TRANSCRIPT_FILE": str(directory / "transcript.jsonl"),
			"BASEDREADER_NO_LINUX_IMPLEMENTATION_FILE": str(directory / "noLinuxImplementation.jsonl"),
			"BASEDREADER_TRANSCRIPT_MODE": "immediate",
		}
		environment.pop("BASEDREADER_BOOT_PROBE", None)
		if probe:
			environment["BASEDREADER_BOOT_PROBE"] = probe
		for variable, value in (env or {}).items():
			if value is None:
				environment.pop(variable, None)
			else:
				environment[variable] = value
		command = [sys.executable, "-I", "-B", str(LAUNCHER), *args]
		if probe:
			command += ["-c", str(configDir), "-f", str(directory / "nvda.log"), "--debug-logging"]
		with open(directory / "stdout.txt", "w") as stdout, open(directory / "stderr.txt", "w") as stderr:
			started = time.monotonic()
			process = subprocess.Popen(
				command,
				env=environment,
				cwd=directory,
				stdout=stdout,
				stderr=stderr,
			)
		launch = Launch(directory, process, started, configDir)
		launches.append(launch)
		return launch

	yield start
	for launch in launches:
		if launch.process.poll() is None:
			launch.process.kill()
			launch.process.wait()
	# A copy that a launch started to restart itself is not this process's child, and is found by the
	# lock it holds.
	lock = _rigLock()
	try:
		singleInstance.terminateRunning(lock)
	finally:
		lock.close()
	after = harness.treeState()
	changed = sorted(path for path in before.keys() | after.keys() if before.get(path) != after.get(path))
	assert not changed, f"a launch changed the repository: {changed}"


def _holdLock(lockPath: str, record: str, ignoreTerm: bool = False) -> subprocess.Popen:
	"""A process that is not NVDA but holds the display's lock, with record written in the file."""
	holder = subprocess.Popen(
		[
			sys.executable,
			"-I",
			"-c",
			(
				"import fcntl, os, signal, sys\n"
				f"if {ignoreTerm!r}:\n"
				"	signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
				f"fd = os.open({lockPath!r}, os.O_RDWR | os.O_CREAT, 0o600)\n"
				"fcntl.lockf(fd, fcntl.LOCK_EX)\n"
				f"if {record!r}:\n"
				"	os.ftruncate(fd, 0)\n"
				f"	os.pwrite(fd, {record!r}.replace('PID', str(os.getpid())).encode(), 0)\n"
				"print('held', flush=True)\n"
				"sys.stdin.read()\n"
			),
		],
		stdin=subprocess.PIPE,
		stdout=subprocess.PIPE,
		text=True,
	)
	assert holder.stdout.readline() == "held\n"
	return holder


def test_startsSpeaksAndExitsThroughTriggerNVDAExit(launcher):
	nvda = launcher("exit", "exit")
	toSpeech = nvda.waitForSentence()
	assert nvda.waitForExit() == 0, nvda.describe()
	nvda.assertClean()
	log = nvda.log()
	assert "_doShutdown has been queued" in log
	assert "Exiting on" not in log
	assert "NVDA exit" in log
	# Java applications are reached through AT-SPI, so the missing Windows bridge is no warning.
	assert "Java Access Bridge not available" not in log
	assert "No Java Access Bridge on Linux" in log
	print(f"start-up to speech: {toSpeech:.2f} s")


@pytest.mark.parametrize("signalNumber", [signal.SIGTERM, signal.SIGHUP, signal.SIGINT])
def test_exitsOnSignal(launcher, signalNumber: signal.Signals):
	nvda = launcher("wait", "wait")
	nvda.waitForSentence()
	# Idle by now: nothing but the signal can wake the main loop.
	time.sleep(0.5)
	asked = time.monotonic()
	nvda.process.send_signal(signalNumber)
	assert nvda.waitForExit() == 0, nvda.describe()
	toExit = time.monotonic() - asked
	nvda.assertClean()
	assert f"Exiting on {signalNumber.name}" in nvda.log()
	print(f"{signalNumber.name} to exit: {toExit:.2f} s")


@pytest.mark.parametrize("signalNumber", [signal.SIGHUP, signal.SIGINT])
def test_signalIgnoredAtStartStaysIgnored(launcher, signalNumber: signal.Signals):
	# As nohup starts a program, so that it outlives the terminal it was started from.
	previous = signal.signal(signalNumber, signal.SIG_IGN)
	try:
		nvda = launcher("ignoring", "wait")
	finally:
		signal.signal(signalNumber, previous)
	nvda.waitForSentence()
	status = Path(f"/proc/{nvda.process.pid}/status").read_text(encoding="ascii")
	ignored = int(re.search(r"^SigIgn:\s*([0-9a-f]+)$", status, re.MULTILINE)[1], 16)
	assert ignored & (1 << (signalNumber - 1)), status
	nvda.process.send_signal(signalNumber)
	nvda.process.send_signal(signal.SIGTERM)
	assert nvda.waitForExit() == 0, nvda.describe()
	nvda.assertClean()
	log = nvda.log()
	assert f"Exiting on {signalNumber.name}" not in log
	assert "Exiting on SIGTERM" in log


def test_repeatedQuitSignalsDuringExit(launcher):
	# The probe's terminate takes two seconds, so that these arrive while the main loop still runs.
	nvda = launcher("repeated", "slowExit", welcome=False)
	nvda.waitForSentence()
	nvda.process.send_signal(signal.SIGTERM)
	nvda.waitForLog("Exiting on SIGTERM")
	# What the end of a session sends after SIGTERM, and a second request to quit.
	nvda.process.send_signal(signal.SIGHUP)
	nvda.process.send_signal(signal.SIGTERM)
	assert nvda.waitForExit() == 0, nvda.describe()
	nvda.assertClean()
	log = nvda.log()
	assert log.count("Exiting on SIGTERM") == 2
	assert "Exiting on SIGHUP" in log
	assert "NVDA exit" in log


def test_secondSignalEndsAnExitThatHangs(launcher):
	# The probe leaves a thread running that NVDA's exit waits for after the main loop.
	nvda = launcher("hang", "hangAtExit", welcome=False)
	nvda.waitForSentence()
	nvda.process.send_signal(signal.SIGTERM)
	nvda.waitForLog("Waiting on <Thread(bootProbeHang")
	nvda.process.send_signal(signal.SIGTERM)
	assert nvda.waitForExit() == -signal.SIGTERM, nvda.describe()
	assert nvda.savedSetting() == CHANGED_SETTING[2]


def test_firstSignalAfterTheMainLoopIsNotFatal(launcher):
	# A copy that this one started to restart NVDA sends SIGTERM as it takes over, whenever that is.
	nvda = launcher("hang", "hangAtExit+exit", welcome=False)
	nvda.waitForLog("Waiting on <Thread(bootProbeHang", timeout=STARTUP_BOUND)
	nvda.process.send_signal(signal.SIGTERM)
	nvda.waitForLog("SIGTERM while exiting")
	assert nvda.process.poll() is None
	nvda.process.send_signal(signal.SIGTERM)
	assert nvda.waitForExit() == -signal.SIGTERM, nvda.describe()


@pytest.mark.parametrize("signalNumber", [signal.SIGTERM, signal.SIGINT])
def test_repeatedSignalEndsAnExitHeldInC(launcher, signalNumber: signal.Signals):
	# No signal started this exit, so the first after the main loop is let through, as a copy started
	# to restart NVDA sends one. The probe then holds the main thread in C with the GIL, where no
	# Python signal handler can ever run.
	nvda = launcher("heldInC", "hangInC+exit", welcome=False)
	nvda.waitForLog("boot probe: blocking in C", timeout=STARTUP_BOUND)
	nvda.process.send_signal(signalNumber)
	with pytest.raises(subprocess.TimeoutExpired):
		nvda.process.wait(1)
	nvda.process.send_signal(signalNumber)
	assert nvda.waitForExit() == -signalNumber, nvda.describe()
	assert nvda.savedSetting() == CHANGED_SETTING[2]


def test_signalSavesTheConfigurationFirst(launcher):
	# The probe's terminate never returns, so the exit never reaches the save after the main loop.
	nvda = launcher("hangInTerminate", "hangInTerminate", welcome=False)
	nvda.waitForSentence()
	nvda.process.send_signal(signal.SIGTERM)
	deadline = time.monotonic() + SHUTDOWN_BOUND
	while nvda.savedSetting() != CHANGED_SETTING[2]:
		assert time.monotonic() < deadline, f"the setting was not saved\n{nvda.describe()}"
		time.sleep(0.05)
	assert nvda.process.poll() is None


def test_secondLaunchReplacesFirst(launcher, tmp_path: Path):
	# Left by a copy with a longer process ID, which a shorter one must not leave a tail of.
	Path(_rigLockPath()).write_text("4194303\nready\n", encoding="ascii")
	sharedConfig = tmp_path / "config"
	first = launcher("first", "wait", configDir=sharedConfig)
	first.waitForSentence()
	second = launcher("second", "exit", configDir=sharedConfig)
	# The second asks the first to quit, and only kills it if it is still there after four seconds.
	assert first.waitForExit() == 0, first.describe()
	second.waitForSentence()
	assert second.waitForExit() == 0, second.describe()
	first.assertClean()
	second.assertClean()
	assert "Exiting on SIGTERM" in first.log()
	firstEnd = first.spoken()[-1]["t"]
	secondStart = second.spoken()[0]["t"]
	assert firstEnd < secondStart


def test_copyStillStartingIsGivenTimeToStart(launcher):
	# The probe makes the first copy's start take longer than the QUIT_TIMEOUT a ready copy gets.
	first = launcher("slow", "slowStart", welcome=False)
	deadline = time.monotonic() + STARTUP_BOUND
	while _runningCopy() != first.process.pid:
		assert time.monotonic() < deadline and first.process.poll() is None, first.describe()
		time.sleep(0.01)
	assert _readyCopy() is None
	second = launcher("second", "exit", welcome=False)
	assert first.waitForExit(timeout=STARTUP_BOUND) == 0, first.describe()
	second.waitForSentence()
	assert second.waitForExit() == 0, second.describe()
	first.assertClean()
	second.assertClean()
	assert "Exiting on SIGTERM" in first.log()


def test_hungCopyIsKilled(launcher):
	holder = _holdLock(_rigLockPath(), "PID\nready\n", ignoreTerm=True)
	try:
		nvda = launcher("takeover", "exit")
		limit = singleInstance.QUIT_TIMEOUT + singleInstance.KILL_TIMEOUT + SHUTDOWN_BOUND
		assert holder.wait(limit) == -signal.SIGKILL
		nvda.waitForSentence()
		assert nvda.waitForExit() == 0, nvda.describe()
		nvda.assertClean()
	finally:
		if holder.poll() is None:
			holder.kill()
		holder.wait()


def test_restartStartsACopyThatTakesOver(launcher):
	# The new copy's command line is quoted as Windows quotes it and split again here.
	nvda = launcher('restart in "a dir\\" with \\\\ quotes', "restart")
	nvda.waitForSentence()
	assert nvda.waitForExit() == 0, nvda.describe()
	# Not this process's child, so it is found and waited for through the lock it holds, which it
	# takes long before it speaks.
	deadline = time.monotonic() + STARTUP_BOUND
	while (newCopy := _runningCopy()) is None:
		assert time.monotonic() < deadline, f"the new copy did not start\n{nvda.describe()}"
		time.sleep(0.01)
	assert newCopy != nvda.process.pid
	# Started with the first copy's interpreter options, not just with the interpreter core names.
	command = Path(f"/proc/{newCopy}/cmdline").read_bytes().split(b"\0")
	assert command[1:3] == [b"-I", b"-B"], command
	assert os.fsencode(nvda.configDir) in command, command
	while len(nvda.sentences()) < 2:
		assert time.monotonic() < deadline, f"the new copy did not speak\n{nvda.describe()}"
		time.sleep(0.05)
	try:
		pidfd = os.pidfd_open(newCopy)
	except ProcessLookupError:
		pass
	else:
		try:
			poller = select.poll()
			poller.register(pidfd, select.POLLIN)
			assert poller.poll(SHUTDOWN_BOUND * 1000), f"the new copy did not exit\n{nvda.describe()}"
		finally:
			os.close(pidfd)
	# The new copy moved the first copy's log aside as it started.
	nvda.assertClean(logNames=("nvda-old.log", "nvda.log"))
	assert "Starting new NVDA instance" in nvda.log("nvda-old.log")
	assert "NVDA exit" in nvda.log("nvda.log")


def test_checkRunningAndQuit(launcher):
	before = launcher("checkBefore", None, "--check-running")
	assert before.waitForExit() == 1, before.describe()
	nvda = launcher("wait", "wait")
	nvda.waitForSentence()
	running = launcher("checkRunning", None, "--check-running")
	assert running.waitForExit() == 0, running.describe()
	assert nvda.process.poll() is None
	quitter = launcher("quit", None, "--quit")
	assert quitter.waitForExit() == 0, quitter.describe()
	assert nvda.waitForExit() == 0, nvda.describe()
	nvda.assertClean()
	after = launcher("checkAfter", None, "--check-running")
	assert after.waitForExit() == 1, after.describe()


def test_coreFailureIsReportedOnStandardError(launcher):
	# wx raises SystemExit when there is no display to open.
	nvda = launcher("noDisplay", "exit", env={"DISPLAY": None})
	assert nvda.waitForExit() == 1, nvda.describe()
	assert re.search(r"^CRITICAL - __main__\.main .*:\ncore failure$", nvda.log(), re.MULTILINE), (
		nvda.describe()
	)
	# One line, and nothing from a hook of logHandler's that the interpreter took apart as it exited.
	assert unexpectedStandardError(nvda.log("stderr.txt")) == (
		"basedreader: core failure, SystemExit: Unable to access the X Display, is $DISPLAY set "
		f"properly?; the log is {nvda.directory / 'nvda.log'}\n"
	)


def test_launcherOptionIsRefused(launcher):
	nvda = launcher("launcherOption", "exit", "--launcher")
	assert nvda.waitForExit() == 1, nvda.describe()
	assert nvda.log("stderr.txt").startswith("basedreader: --launcher ")
	assert not nvda.log()


def test_lockFailureIsReportedOnStandardError(tmp_path: Path):
	done = harness.runChild(
		tmp_path,
		f"""
		import errno
		import fcntl
		import sys

		def refuse(*args):
			raise OSError(errno.ENOLCK, "No locks available")

		fcntl.lockf = refuse
		sys.argv = [{str(LAUNCHER)!r}]
		import basedreader

		basedreader.main()
		""",
		prepare="none",
	)
	assert done.error is not None and done.error.rstrip().endswith("SystemExit: 1"), done.error
	assert done.stderr.startswith("basedreader: cannot take "), done.stderr
	assert done.stderr.rstrip().endswith("No locks available"), done.stderr


def test_greeterCopyTakesNoTestHooks(launcher):
	# On a display manager's login screen NVDA runs in secure mode, which turns logging off as well.
	nvda = launcher("greeter", "exit", env={"XDG_SESSION_CLASS": "greeter"})
	deadline = time.monotonic() + STARTUP_BOUND
	while _readyCopy() != nvda.process.pid:
		assert time.monotonic() < deadline and nvda.process.poll() is None, nvda.describe()
		time.sleep(0.05)
	nvda.process.send_signal(signal.SIGTERM)
	assert nvda.waitForExit() == 0, nvda.describe()
	assert not nvda.spoken()
	assert not nvda.log()
	recorded = (nvda.directory / "noLinuxImplementation.jsonl").read_text(encoding="utf-8")
	assert not recorded, recorded
	assert not unexpectedStandardError(nvda.log("stderr.txt")), nvda.describe()


def test_recordSeesNoLinuxImplementationThatUpstreamCaught(tmp_path: Path):
	recordPath = tmp_path / "noLinuxImplementation.jsonl"
	done = harness.runChild(
		tmp_path,
		f"""
		import ctypes
		import basedreader
		basedreader._recordNoLinuxImplementation({str(recordPath)!r})
		try:
			ctypes.windll.user32.GetForegroundWindow()
		except NotImplementedError:
			pass
		""",
		prepare="bootstrap",
	)
	assert done.error is None, done.error
	records = [json.loads(line) for line in recordPath.read_text(encoding="utf-8").splitlines()]
	assert [r["what"] for r in records] == ["user32.GetForegroundWindow"]
	assert records[0]["thread"] == "MainThread"
	stack = records[0]["stack"]
	assert "windowsCtypes.py" in stack[-1]
	assert "GetForegroundWindow()" in stack[-2]


def test_findingTheCallersModuleReadsNoSource(tmp_path: Path):
	# Every module with deprecated names asks this as it is imported, deep in an import stack; reading
	# the source of each frame on that stack cost more than a second of every start.
	done = harness.runChild(
		tmp_path,
		"""
		import linecache
		import sys
		import types

		module = types.ModuleType("deprecationProbe")
		sys.modules[module.__name__] = module
		linecache.clearcache()
		code = "from utils import _deprecate\\ncaller = _deprecate._getCallerModule()\\n"
		exec(compile(code, "<deprecationProbe>", "exec"), module.__dict__)
		result = [module.caller.__name__, sorted(linecache.cache)]
		""",
		prepare="bootstrap",
	)
	done.assertClean()
	assert done.value == ["deprecationProbe", []]


def test_taskSchedulerStopsAtOnce(tmp_path: Path):
	# NVDA's exit joins this thread, so anything it waits for delays every exit and restart. An interval
	# far longer than upstream's half second makes waiting for it unmistakable.
	done = harness.runChild(
		tmp_path,
		"""
		import time
		from utils import schedule

		schedule.ScheduleThread.SLEEP_INTERVAL_SECS = 30
		schedule.initialize()
		time.sleep(0.05)
		started = time.monotonic()
		schedule.terminate()
		result = time.monotonic() - started
		""",
		prepare="bootstrap",
	)
	done.assertClean()
	assert done.value < 10


def test_guiLeavesWebKitUnloaded(tmp_path: Path):
	# wx.html2's extension links WebKitGTK, which brings GStreamer, PulseAudio and a speech
	# synthesiser into the process; nothing needs it until an HTML message is shown.
	done = harness.runChild(
		tmp_path,
		"""
		import sys

		# In the order NVDA's own imports reach gui; importing it first is circular.
		import appModuleHandler
		import gui

		def mapped() -> list[str]:
			with open("/proc/self/maps", encoding="utf-8") as f:
				names = {line.split()[-1].rsplit("/", 1)[-1] for line in f if "/" in line}
			prefixes = ("libwebkit", "libjavascriptcore", "libgst", "libpulse")
			return sorted(name for name in names if name.startswith(prefixes))

		before = [mapped(), "wx.html2" in sys.modules]
		import wx.html2

		result = [*before, gui.message.HtmlMessageDialog._webViewBackend == wx.html2.WebViewBackendIE]
		""",
	)
	done.assertClean()
	assert done.value == [[], False, True]


def test_endingDialogsUsesTheGtkThatWxLoaded(tmp_path: Path):
	# A second GTK in the process, or GTK 4, would hold windows and grabs that wx's GTK cannot see.
	done = harness.runChild(
		tmp_path,
		"""
		import wx

		app = wx.App()

		def gtk() -> list[str]:
			with open("/proc/self/maps", encoding="utf-8") as f:
				return sorted({line.split()[-1] for line in f if "/libgtk-" in line})

		before = gtk()
		from linuxPlatform import modalDialogs

		result = [before, gtk(), modalDialogs.isOneWxDialogAtMost()]
		""",
		prepare="bootstrap",
	)
	done.assertClean()
	before, after, isOneAtMost = done.value
	assert len(before) == 1 and "/libgtk-3.so" in before[0], before
	assert after == before
	assert isOneAtMost


def test_desktopIsOneObjectWithNothingUnderIt(tmp_path: Path):
	done = harness.runChild(
		tmp_path,
		"""
		# In the order NVDA's own imports reach NVDAObjects; importing it first is circular.
		import appModuleHandler
		from linuxPlatform.desktop import Desktop

		desktop = Desktop()
		result = [
			desktop == Desktop(),
			desktop.processID,
			desktop.objectWithFocus(),
			desktop.objectInForeground(),
			desktop.objectFromPoint(10, 10),
		]
		""",
	)
	done.assertClean()
	assert done.value == [True, None, None, None, None]


def test_restartKeepsInterpreterOptions():
	script = "/nvda/source/basedreader.py"
	origArgv = ["/bin/python3", "-I", "-B", "-X", "dev", script, "-c", script, "--debug-logging"]
	assert singleInstance.interpreterOptions(origArgv, script) == ["-I", "-B", "-X", "dev"]
	assert singleInstance.interpreterOptions(["/bin/python3", script], script) == []
	assert singleInstance.interpreterOptions(["/bin/python3", "-c", "pass"], "-c") == []


@pytest.mark.parametrize(
	"arguments",
	[
		["plain", "two words", ""],
		['a "quoted" word', 'ends with a quote"'],
		["back\\slash", "trailing backslash\\", "two\\\\", "\\\\server\\share"],
		['backslash before a quote\\"', 'three\\\\\\"', "tab\tinside"],
		["-c", '/home/u/dir with "quotes" and \\\\\\"', "--debug-logging"],
	],
)
def test_restartCommandLineSplitsAsWindowsQuotesIt(arguments: list[str]):
	assert singleInstance.splitWindowsCommandLine(subprocess.list2cmdline(arguments)) == arguments


@pytest.mark.parametrize(
	("display", "name"),
	[
		(":0", ":0"),
		(":0.1", ":0"),
		("unix:0", ":0"),
		("/tmp/launch-x/org.xquartz:0", ":0"),
		# Through TCP, as ssh forwards a display to a proxy for another machine's server.
		("localhost:10.0", "localhost:10"),
		("127.0.0.1:0", "127.0.0.1:0"),
		("otherhost:0", "otherhost:0"),
		(":10", ":10"),
	],
)
def test_oneLockNamePerDisplay(monkeypatch, display: str, name: str):
	monkeypatch.setenv("DISPLAY", display)
	assert singleInstance.displayName() == name


def test_oneLockNamePerWaylandDisplay(monkeypatch, tmp_path: Path):
	monkeypatch.delenv("DISPLAY")
	monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
	assert singleInstance.displayName() == "none"
	monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
	monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-1")
	assert singleInstance.displayName() == "wayland-1"
	monkeypatch.setenv("WAYLAND_DISPLAY", str(tmp_path / "wayland-1"))
	assert singleInstance.displayName() == "wayland-1"
	monkeypatch.setenv("WAYLAND_DISPLAY", "/elsewhere/wayland-1")
	assert singleInstance.displayName() == "_elsewhere_wayland-1"


def test_lockDirectoryMustBePrivate(monkeypatch, tmp_path: Path):
	monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
	(tmp_path / "basedreader").mkdir(mode=0o755)
	(tmp_path / "basedreader").chmod(0o755)
	with pytest.raises(PermissionError):
		singleInstance.lockFilePath()
	(tmp_path / "basedreader").chmod(0o700)
	assert singleInstance.lockFilePath().startswith(str(tmp_path / "basedreader") + os.sep)


def test_lockWithoutRuntimeDirectoryIsInTheUsersCache(monkeypatch, tmp_path: Path):
	# As under su or sudo. No other user can make this directory first, as one could in /tmp.
	monkeypatch.delenv("XDG_RUNTIME_DIR")
	monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
	assert singleInstance.lockFilePath().startswith(str(tmp_path / "cache" / "basedreader") + os.sep)
	monkeypatch.delenv("XDG_CACHE_HOME")
	monkeypatch.setenv("HOME", str(tmp_path / "home"))
	assert singleInstance.lockFilePath().startswith(
		str(tmp_path / "home" / ".cache" / "basedreader") + os.sep
	)


@pytest.mark.parametrize(
	("content", "processID", "readyProcessID"),
	[
		(b"1234\n", 1234, None),
		(b"1234\nready\n", 1234, 1234),
		(b"", None, None),
		(b"1234", None, None),
		(b"1234\n303\nready\n", None, None),
		(b"0\n", None, None),
		(b"-5\n", None, None),
		(b"\xff\n", None, None),
		(b"1234\nready\n" + b"x" * 40, None, None),
	],
)
def test_lockFileRecord(tmp_path: Path, content: bytes, processID: int | None, readyProcessID: int | None):
	path = tmp_path / "display.lock"
	path.write_bytes(content)
	lock = singleInstance.InstanceLock(str(path))
	try:
		assert lock.recorded() == processID
		assert lock.recordedReady() == readyProcessID
	finally:
		lock.close()


def test_lockHeldFromAnotherPidNamespaceIsNotSignalled(monkeypatch, tmp_path: Path):
	lock = singleInstance.InstanceLock(str(tmp_path / "display.lock"))
	# What F_GETLK reports for a holder whose process ID means nothing in this namespace.
	held = struct.pack(singleInstance._FLOCK, singleInstance.fcntl.F_WRLCK, os.SEEK_SET, 0, 0, 0)
	monkeypatch.setattr(singleInstance.fcntl, "fcntl", lambda *args: held)
	try:
		with pytest.raises(OSError, match="cannot see"):
			lock.holder()
	finally:
		lock.close()


def test_assertCleanSeesEveryErrorRecord(tmp_path: Path):
	# Each of these code paths holds a space, which a header pattern must still match.
	for codePath in ("unhandled exception", "Exception ignored in: <function f at 0x1>", "Python warning"):
		launch = Launch(tmp_path, subprocess.Popen(["true"]), time.monotonic(), tmp_path)
		launch.process.wait()
		(tmp_path / "noLinuxImplementation.jsonl").write_text("", encoding="utf-8")
		(tmp_path / "nvda.log").write_text(
			"INFO - core.main (12:00:00.000) - MainThread (1):\nstarting\n"
			f"ERROR - {codePath} (12:00:01.000) - MainThread (1):\n"
			"Traceback (most recent call last):\nValueError: x\n",
			encoding="utf-8",
		)
		with pytest.raises(AssertionError, match="1 records at ERROR"):
			launch.assertClean()


def test_assertCleanSeesUnexpectedStandardError(tmp_path: Path):
	launch = Launch(tmp_path, subprocess.Popen(["true"]), time.monotonic(), tmp_path)
	launch.process.wait()
	(tmp_path / "noLinuxImplementation.jsonl").write_text("", encoding="utf-8")
	(tmp_path / "nvda.log").write_text(
		"INFO - core.main (12:00:00.000) - MainThread (1):\nx\n", encoding="utf-8"
	)
	allowed = (
		"Exception ignored in: <function AutoSettings.__del__ at 0x7fffe85965c0>\n"
		"Traceback (most recent call last):\n"
		'  File "/x/source/autoSettingsUtils/autoSettings.py", line 43, in __del__\n'
		'  File "/x/source/autoSettingsUtils/autoSettings.py", line 52, in _unregisterConfigSaveAction\n'
		"AttributeError: 'NoneType' object has no attribute 'unregister'\n"
	)
	(tmp_path / "stderr.txt").write_text(allowed * 2, encoding="utf-8")
	launch.assertClean()
	(tmp_path / "stderr.txt").write_text(
		allowed + "(python3:1): Gtk-WARNING **: something\n", encoding="utf-8"
	)
	with pytest.raises(AssertionError, match="standard error"):
		launch.assertClean()


def test_assertCleanAllowsOnlyNotedGaps(tmp_path: Path):
	launch = Launch(tmp_path, subprocess.Popen(["true"]), time.monotonic(), tmp_path)
	launch.process.wait()
	(tmp_path / "nvda.log").write_text(
		"INFO - core.main (12:00:00.000) - MainThread (1):\nx\n", encoding="utf-8"
	)
	record = {"what": "winsound.MessageBeep", "thread": "MainThread", "stack": []}
	(tmp_path / "noLinuxImplementation.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")
	launch.assertClean()
	record["what"] = "winsound.PlaySound"
	(tmp_path / "noLinuxImplementation.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")
	with pytest.raises(AssertionError, match="winsound.PlaySound"):
		launch.assertClean()


def test_startsFromUpstreamDefaults(launcher):
	# The fixture leaves the welcome dialog, the question at exit and every other first-start dialog
	# at upstream's defaults; the welcome dialog is open while the probe speaks.
	assert "showWelcomeDialogAtStartup" not in CONFIG_FIXTURE.read_text(encoding="utf-8")
	nvda = launcher("defaults", "wait")
	nvda.waitForSentence()
	nvda.waitForLog("Did context help binding for WelcomeDialog", timeout=STARTUP_BOUND)
	nvda.process.send_signal(signal.SIGTERM)
	assert nvda.waitForExit() == 0, nvda.describe()
	nvda.assertClean()
	assert "ending modal <gui.startupDialogs.WelcomeDialog" in nvda.log()


def test_exitCommandAsksFirst(launcher):
	nvda = launcher("exitDialog", "exitDialog")
	nvda.waitForSentence(text=EXIT_DIALOG_SENTENCE)
	nvda.process.send_signal(signal.SIGTERM)
	assert nvda.waitForExit() == 0, nvda.describe()
	nvda.assertClean()


@pytest.mark.parametrize(
	("probe", "answer"),
	[
		("messageBox", WX_CANCEL),
		# A yes or no question has no Cancel button, so Cancel is the one answer that means neither.
		("question", WX_CANCEL),
		("okCancel", WX_CANCEL),
		("confirm", WX_ID_CANCEL),
		# MessageDialog.alert returns nothing, so all this shows is that the box was closed.
		("alert", None),
	],
)
def test_exitsOnSignalWithMessageBoxOpen(launcher, probe: str, answer: int | None):
	nvda = launcher(probe, probe)
	nvda.waitForSentence(text=DIALOG_SENTENCE)
	asked = time.monotonic()
	nvda.process.send_signal(signal.SIGTERM)
	# Well within the four seconds after which a copy replacing this one would kill it.
	assert nvda.waitForExit(timeout=singleInstance.QUIT_TIMEOUT) == 0, nvda.describe()
	toExit = time.monotonic() - asked
	nvda.assertClean()
	log = nvda.log()
	assert "Exiting on SIGTERM" in log
	assert "_doShutdown has been queued" in log
	assert nvda.answers() == {probe: answer}
	assert nvda.savedSetting() == CHANGED_SETTING[2]
	if probe == "messageBox":
		# An error box, whose sound Linux does not have.
		assert nvda.recorded() == ["winsound.MessageBeep"]
	print(f"SIGTERM to exit with {probe} open: {toExit:.2f} s")


@pytest.mark.parametrize("route", ["signal", "triggerNVDAExit"])
@pytest.mark.parametrize(
	("probe", "welcome", "answers"),
	[
		# GTK's own dialog, inside the welcome dialog.
		("nativeMessageBox", True, {"nativeMessageBox": WX_CANCEL}),
		("fileDialog", False, {"fileDialog": WX_ID_CANCEL}),
		("nestedDialogs", False, {"inner": WX_ID_CLOSE_ALL, "outer": WX_ID_CLOSE_ALL}),
		("dialogInWelcome", True, {"inner": WX_ID_CLOSE_ALL}),
	],
)
def test_exitsWithModalDialogsOpen(
	launcher,
	probe: str,
	welcome: bool,
	answers: dict,
	route: str,
):
	isFileDialog = probe == "fileDialog"
	env = _gsettingsSchemaEnvironment() if isFileDialog else None
	nvda = launcher(probe, probe if route == "signal" else f"{probe}+exit", welcome=welcome, env=env)
	nvda.waitForSentence(text=DIALOG_SENTENCE)
	if route == "signal":
		nvda.process.send_signal(signal.SIGTERM)
	assert nvda.waitForExit(timeout=singleInstance.QUIT_TIMEOUT) == 0, nvda.describe()
	nvda.assertClean(alsoAllowed=(_noIconTheme,) if isFileDialog else ())
	assert nvda.answers() == answers
	assert nvda.savedSetting() == CHANGED_SETTING[2]
	assert "NVDA exit" in nvda.log()


@pytest.mark.parametrize("route", ["signal", "triggerNVDAExit"])
def test_exitsWhileTheCallInsideADialogGoesOn(launcher, route: str):
	# Once its dialog is answered, the call that showed it handles events and then asks a question
	# inside the outer dialog. Ended before that call returns, the outer dialog either never ends or is
	# destroyed under the question.
	nvda = launcher("followUp", "followUp" if route == "signal" else "followUp+exit", welcome=False)
	nvda.waitForSentence(text=DIALOG_SENTENCE)
	if route == "signal":
		nvda.process.send_signal(signal.SIGTERM)
	assert nvda.waitForExit(timeout=singleInstance.QUIT_TIMEOUT) == 0, nvda.describe()
	nvda.assertClean()
	# In the order the calls returned.
	assert list(nvda.answers().items()) == [
		("inner", WX_ID_CLOSE_ALL),
		("followUp", WX_CANCEL),
		("outer", WX_ID_CLOSE_ALL),
	]
	assert nvda.savedSetting() == CHANGED_SETTING[2]


def test_callThatNeverReturnsInsideADialogEndsNVDA(launcher):
	# Once its dialog is answered, the call that showed it handles events for ever inside the outer
	# dialog, which can therefore never be ended.
	nvda = launcher("neverReturns", "neverReturns", welcome=False)
	nvda.waitForSentence(text=DIALOG_SENTENCE)
	nvda.process.send_signal(signal.SIGTERM)
	assert nvda.waitForExit(timeout=singleInstance.QUIT_TIMEOUT) == GIVEN_UP_STATUS, nvda.describe()
	log = nvda.log()
	assert re.search(
		r"^Gave up ending modal dialogs: the call that showed .* has not returned after [\d.]+ s; "
		r"ending NVDA without finishing its exit$",
		log,
		re.MULTILINE,
	), log[-4000:]
	assert nvda.answers() == {"inner": WX_ID_CLOSE_ALL}
	assert nvda.savedSetting() == CHANGED_SETTING[2]


@pytest.mark.parametrize("route", ["signal", "triggerNVDAExit"])
def test_dialogReopenedForEverEndsNVDA(launcher, route: str):
	# Code that shows its dialog again each time it is ended would otherwise keep NVDA half shut down
	# for ever, its plugins terminated and its main loop never returning.
	nvda = launcher("reopening", "reopening" if route == "signal" else "reopening+exit", welcome=False)
	nvda.waitForSentence(text=DIALOG_SENTENCE)
	if route == "signal":
		nvda.process.send_signal(signal.SIGTERM)
	assert nvda.waitForExit(timeout=singleInstance.QUIT_TIMEOUT) == GIVEN_UP_STATUS, nvda.describe()
	log = nvda.log()
	given = re.search(
		r"^Gave up ending modal dialogs: .* is still open after ending (\d+); ending NVDA without "
		r"finishing its exit$",
		log,
		re.MULTILINE,
	)
	assert given, log[-4000:]
	assert len(errorRecords(log)) == 1, log[-4000:]
	assert log.count("ending modal <Gtk.MessageDialog") == int(given[1])
	assert nvda.savedSetting() == CHANGED_SETTING[2]
	assert f"basedreader: {given[0]}\n" in nvda.log("stderr.txt")


def test_magnifierToggleSaysItCannotStart(launcher):
	nvda = launcher("magnifier", "magnifier")
	nvda.waitForSentence()
	assert nvda.waitForExit() == 0, nvda.describe()
	nvda.assertClean()
	spoken = [transcriptRecords.spokenText(r) for r in nvda.spoken()]
	assert "Cannot start magnifier. NVDA's magnifier is not available on Linux." in spoken
	assert "Magnifier enabled" not in spoken


def test_exitsOnSignalAsSoonAsItHoldsTheLock(launcher):
	# A copy replacing this one can send SIGTERM from the moment this one holds the lock.
	nvda = launcher("early", "wait")
	deadline = time.monotonic() + STARTUP_BOUND
	while _runningCopy() != nvda.process.pid:
		assert time.monotonic() < deadline and nvda.process.poll() is None, nvda.describe()
		time.sleep(0.001)
	nvda.process.send_signal(signal.SIGTERM)
	assert nvda.waitForExit(timeout=STARTUP_BOUND) == 0, nvda.describe()
	nvda.assertClean()
	assert "Exiting on SIGTERM" in nvda.log()


def test_lockHolderThatRecordedNothingIsNeverSignalled(launcher):
	# A process can hold the lock while the file still names the copy before it, which may be gone
	# and its process ID reused by anything.
	bystander = subprocess.Popen(["sleep", "60"])
	lockPath = _rigLockPath()
	Path(lockPath).write_text(f"{bystander.pid}\n", encoding="ascii")
	holder = _holdLock(lockPath, "")
	try:
		assert _runningCopy() == holder.pid
		running = launcher("checkRunning", None, "--check-running")
		assert running.waitForExit() == 0, running.describe()
		quitter = launcher("quit", None, "--quit")
		assert quitter.waitForExit() == 0, quitter.describe()
		assert "has not recorded itself" in quitter.log("stderr.txt")
		# A copy that would start waits its while for the lock, as nvda.pyw waits for the mutex, and
		# gives up.
		starter = launcher("start", None)
		assert starter.waitForExit() == 1, starter.describe()
		assert "has not recorded itself" in starter.log("stderr.txt")
		assert f"basedreader: another NVDA still holds {lockPath}\n" in starter.log("stderr.txt")
		assert bystander.poll() is None
		assert holder.poll() is None
	finally:
		holder.stdin.close()
		holder.wait()
		bystander.kill()
		bystander.wait()


def test_unlockedRecordIsNeverSignalled(launcher):
	# A process ID left in the lock file by a copy that has gone may since belong to anything.
	bystander = subprocess.Popen(["sleep", "60"])
	try:
		Path(_rigLockPath()).write_text(f"{bystander.pid}\nready\n", encoding="ascii")
		quitter = launcher("quit", None, "--quit")
		assert quitter.waitForExit() == 0, quitter.describe()
		assert bystander.poll() is None
	finally:
		bystander.kill()
		bystander.wait()
