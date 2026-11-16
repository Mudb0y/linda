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
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import harness
import pytest

HERE = Path(__file__).resolve().parent
LAUNCHER = harness.SOURCE / "basedreader.py"
CONFIG_FIXTURE = HERE / "fixtures" / "bootConfig" / "nvda.ini"
#: What fixtures/globalPlugins/bootProbe.py says.
SENTENCE = "the boot probe has started"
MESSAGE_BOX_SENTENCE = "the boot probe's message box is open"
EXIT_DIALOG_SENTENCE = "the exit dialog is open"
#: Seconds from starting the process to the sentence; a start under the rig takes about four.
STARTUP_BOUND = 60.0
#: Seconds from asking NVDA to exit to the process being gone; it takes well under one.
SHUTDOWN_BOUND = 20.0


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
	# The development shell gives GTK no SVG image loader, so the theme's check marks and other
	# symbolic icons are not drawn. Nothing NVDA says or does depends on them.
	re.compile(
		r"\n?\(basedreader\.py:\d+\): Gtk-WARNING \*\*: [\d:.]+: Could not load a pixbuf from "
		r"/org/gtk/libgtk/theme/Adwaita/assets/[\w-]+\.svg\.\n"
		r"This may indicate that pixbuf loaders or the mime database could not be found\.\n",
	),
)


def unexpectedStandardError(text: str) -> str:
	"""What is left of standard error once the allowed lines are taken out."""
	for pattern in _allowedStandardError:
		text = pattern.sub("", text)
	return text


def errorRecords(log: str) -> list[str]:
	"""The records at ERROR or above in an NVDA log, or a line that looks like one."""
	return _errorRecord.findall(log)


class Launch:
	"""One run of the launcher, with its own log, transcript and record of NoLinuxImplementation."""

	def __init__(self, directory: Path, process: subprocess.Popen, started: float) -> None:
		self.directory = directory
		self.process = process
		self.started = started

	@property
	def logPath(self) -> Path:
		return self.directory / "nvda.log"

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

	def waitForExit(self, timeout: float = SHUTDOWN_BOUND) -> int:
		try:
			return self.process.wait(timeout)
		except subprocess.TimeoutExpired:
			pytest.fail(f"NVDA did not exit within {timeout} s\n{self.describe()}")

	def assertClean(self, logNames: tuple[str, ...] = ("nvda.log",)) -> None:
		"""The run raised no NoLinuxImplementation, even one upstream caught, logged no error, and wrote
		nothing unexpected to standard error."""
		recorded = self.directory / "noLinuxImplementation.jsonl"
		assert recorded.exists(), "the launcher did not record NoLinuxImplementation as asked"
		raised = [json.loads(line) for line in recorded.read_text(encoding="utf-8").splitlines()]
		assert not raised, "NoLinuxImplementation raised:\n" + "\n".join(
			r["what"] + "\n" + "".join(r["stack"]) for r in raised
		)
		for name in logNames:
			log = self.log(name)
			assert _recordHeader.search(log), f"{name} holds no records\n{self.describe()}"
			errors = errorRecords(log)
			assert not errors, f"{name} holds {len(errors)} records at ERROR or above\n{log[-8000:]}"
		stderr = self.log("stderr.txt")
		assert not unexpectedStandardError(stderr), f"standard error holds:\n{stderr[-4000:]}"


def _runningCopy() -> int | None:
	"""The process holding the lock of the rig's display."""
	lock = singleInstance.InstanceLock(singleInstance.lockFilePath())
	try:
		return lock.holder()
	finally:
		lock.close()


@pytest.fixture
def launcher(tmp_path: Path) -> Iterator:
	"""Starts launches, and makes sure none outlives the test or wrote into the repository."""
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
	) -> Launch:
		directory = tmp_path / "launches" / name
		directory.mkdir(parents=True)
		if configDir is None:
			configDir = directory / "config"
		if probe and not configDir.exists():
			configDir.mkdir()
			shutil.copy(CONFIG_FIXTURE, configDir / "nvda.ini")
		env = {
			**os.environ,
			"BASEDREADER_TEST_HOOKS_DIR": str(hooks),
			"TRANSCRIPT_SYNTH_FILE": str(directory / "transcript.jsonl"),
			"BASEDREADER_NO_LINUX_IMPLEMENTATION_FILE": str(directory / "noLinuxImplementation.jsonl"),
			"TRANSCRIPT_SYNTH_MODE": "immediate",
		}
		if probe:
			env["BASEDREADER_BOOT_PROBE"] = probe
		else:
			env.pop("BASEDREADER_BOOT_PROBE", None)
		command = [sys.executable, "-I", "-B", str(LAUNCHER), *args]
		if probe:
			command += ["-c", str(configDir), "-f", str(directory / "nvda.log"), "--debug-logging"]
		with open(directory / "stdout.txt", "w") as stdout, open(directory / "stderr.txt", "w") as stderr:
			started = time.monotonic()
			process = subprocess.Popen(
				command,
				env=env,
				cwd=directory,
				stdout=stdout,
				stderr=stderr,
			)
		launch = Launch(directory, process, started)
		launches.append(launch)
		return launch

	# The rig runs its command as a background job, which starts with SIGINT ignored, and a launch
	# would inherit that. A handler is reset to the default action in a new program instead, which is
	# what NVDA started from a terminal gets.
	previousSigInt = signal.signal(signal.SIGINT, signal.default_int_handler)
	try:
		yield start
	finally:
		signal.signal(signal.SIGINT, previousSigInt)
	for launch in launches:
		if launch.process.poll() is None:
			launch.process.kill()
			launch.process.wait()
	# A copy that a launch started to restart itself is not this process's child, and is found by the
	# lock it holds.
	lock = singleInstance.InstanceLock(singleInstance.lockFilePath())
	try:
		singleInstance.terminateRunning(lock)
	finally:
		lock.close()
	after = harness.treeState()
	changed = sorted(path for path in before.keys() | after.keys() if before.get(path) != after.get(path))
	assert not changed, f"a launch changed the repository: {changed}"


def test_startsSpeaksAndExitsThroughTriggerNVDAExit(launcher):
	nvda = launcher("exit", "exit")
	toSpeech = nvda.waitForSentence()
	assert nvda.waitForExit() == 0, nvda.describe()
	nvda.assertClean()
	log = nvda.log()
	assert "_doShutdown has been queued" in log
	assert "Exiting on" not in log
	assert "NVDA exit" in log
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


def test_secondLaunchReplacesFirst(launcher, tmp_path: Path):
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


def test_restartStartsACopyThatTakesOver(launcher):
	nvda = launcher("restart", "restart")
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
	# NVDA's exit joins this thread, so anything it waits for delays every exit and restart.
	done = harness.runChild(
		tmp_path,
		"""
		import time
		from utils import schedule

		schedule.initialize()
		time.sleep(0.05)
		started = time.monotonic()
		schedule.terminate()
		result = [time.monotonic() - started, schedule.ScheduleThread.SLEEP_INTERVAL_SECS]
		""",
		prepare="bootstrap",
	)
	done.assertClean()
	took, interval = done.value
	assert took < interval / 2


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


def test_restartKeepsInterpreterOptions():
	script = "/nvda/source/basedreader.py"
	origArgv = ["/bin/python3", "-I", "-B", "-X", "dev", script, "-c", script, "--debug-logging"]
	assert singleInstance.interpreterOptions(origArgv, script) == ["-I", "-B", "-X", "dev"]
	assert singleInstance.interpreterOptions(["/bin/python3", script], script) == []
	assert singleInstance.interpreterOptions(["/bin/python3", "-c", "pass"], "-c") == []


@pytest.mark.parametrize(
	("display", "name"),
	[
		(":0", ":0"),
		(":0.1", ":0"),
		("unix:0", ":0"),
		("localhost:0.0", ":0"),
		("/tmp/launch-x/org.xquartz:0", ":0"),
		("otherhost:0", "otherhost:0"),
		(":10", ":10"),
	],
)
def test_oneLockNamePerDisplay(monkeypatch, display: str, name: str):
	monkeypatch.setenv("DISPLAY", display)
	assert singleInstance.displayName() == name


def test_lockDirectoryMustBePrivate(monkeypatch, tmp_path: Path):
	monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
	(tmp_path / "basedreader").mkdir(mode=0o755)
	(tmp_path / "basedreader").chmod(0o755)
	with pytest.raises(PermissionError):
		singleInstance.lockFilePath()
	(tmp_path / "basedreader").chmod(0o700)
	assert singleInstance.lockFilePath().startswith(str(tmp_path / "basedreader") + os.sep)


def test_assertCleanSeesEveryErrorRecord(tmp_path: Path):
	# Each of these code paths holds a space, which a header pattern must still match.
	for codePath in ("unhandled exception", "Exception ignored in: <function f at 0x1>", "Python warning"):
		launch = Launch(tmp_path, subprocess.Popen(["true"]), time.monotonic())
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
	launch = Launch(tmp_path, subprocess.Popen(["true"]), time.monotonic())
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


def test_startsFromUpstreamDefaults(launcher):
	# The fixture leaves the welcome dialog, the question at exit and every other first-start dialog
	# at upstream's defaults; the welcome dialog is open while the probe speaks.
	assert "showWelcomeDialogAtStartup" not in CONFIG_FIXTURE.read_text(encoding="utf-8")
	nvda = launcher("defaults", "wait")
	nvda.waitForSentence()
	deadline = time.monotonic() + STARTUP_BOUND
	while "Did context help binding for WelcomeDialog" not in nvda.log():
		assert time.monotonic() < deadline and nvda.process.poll() is None, nvda.describe()
		time.sleep(0.05)
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


@pytest.mark.parametrize("probe", ["messageBox", "question", "alert"])
def test_exitsOnSignalWithMessageBoxOpen(launcher, probe: str):
	nvda = launcher(probe, probe)
	nvda.waitForSentence(text=MESSAGE_BOX_SENTENCE)
	asked = time.monotonic()
	nvda.process.send_signal(signal.SIGTERM)
	# Well within the four seconds after which a copy replacing this one would kill it.
	assert nvda.waitForExit(timeout=singleInstance.QUIT_TIMEOUT) == 0, nvda.describe()
	toExit = time.monotonic() - asked
	nvda.assertClean()
	log = nvda.log()
	assert "Exiting on SIGTERM" in log
	assert "_doShutdown has been queued" in log
	print(f"SIGTERM to exit with {probe} open: {toExit:.2f} s")


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
	lockPath = singleInstance.lockFilePath()
	Path(lockPath).write_text(f"{bystander.pid}\n", encoding="ascii")
	holder = subprocess.Popen(
		[
			sys.executable,
			"-I",
			"-c",
			(
				"import fcntl, os, sys\n"
				f"fd = os.open({lockPath!r}, os.O_RDWR)\n"
				"fcntl.lockf(fd, fcntl.LOCK_EX)\n"
				"print('held', flush=True)\n"
				"sys.stdin.read()\n"
			),
		],
		stdin=subprocess.PIPE,
		stdout=subprocess.PIPE,
		text=True,
	)
	try:
		assert holder.stdout.readline() == "held\n"
		assert _runningCopy() == holder.pid
		running = launcher("checkRunning", None, "--check-running")
		assert running.waitForExit() == 0, running.describe()
		quitter = launcher("quit", None, "--quit")
		assert quitter.waitForExit() == 0, quitter.describe()
		assert "has not recorded itself" in quitter.log("stderr.txt")
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
		Path(singleInstance.lockFilePath()).write_text(f"{bystander.pid}\n", encoding="ascii")
		quitter = launcher("quit", None, "--quit")
		assert quitter.waitForExit() == 0, quitter.describe()
		assert bystander.poll() is None
	finally:
		bystander.kill()
		bystander.wait()
