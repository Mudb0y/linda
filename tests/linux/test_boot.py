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
#: Seconds from starting the process to the sentence; a start under the rig takes about five.
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

#: The first line of each log record: level, code path, time, thread.
_recordHeader = re.compile(r"^(?P<level>[A-Z_]+) - \S+ \(\d\d:\d\d:\d\d\.\d{3}\) - .*:$", re.MULTILINE)


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

	def sentences(self) -> list[dict]:
		return [r for r in self.spoken() if transcriptRecords.spokenText(r) == SENTENCE]

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

	def waitForSentence(self, timeout: float = STARTUP_BOUND) -> float:
		"""Seconds from starting the process to the sentence being spoken."""
		deadline = self.started + timeout
		while time.monotonic() < deadline:
			if sentences := self.sentences():
				# The synth stamps records with the same monotonic clock, which is system wide.
				return sentences[0]["t"] - self.started
			if self.process.poll() is not None:
				break
			time.sleep(0.05)
		pytest.fail(f"the sentence was not spoken within {timeout} s\n{self.describe()}")

	def waitForExit(self, timeout: float = SHUTDOWN_BOUND) -> int:
		try:
			return self.process.wait(timeout)
		except subprocess.TimeoutExpired:
			pytest.fail(f"NVDA did not exit within {timeout} s\n{self.describe()}")

	def assertClean(self, logNames: tuple[str, ...] = ("nvda.log",)) -> None:
		"""The run raised no NoLinuxImplementation, even one upstream caught, and logged no error."""
		recorded = self.directory / "noLinuxImplementation.jsonl"
		assert recorded.exists(), "the launcher did not record NoLinuxImplementation as asked"
		raised = [json.loads(line) for line in recorded.read_text(encoding="utf-8").splitlines()]
		assert not raised, "NoLinuxImplementation raised:\n" + "\n".join(
			r["what"] + "\n" + "".join(r["stack"]) for r in raised
		)
		for name in logNames:
			log = self.log(name)
			levels = [m["level"] for m in _recordHeader.finditer(log)]
			assert levels, f"{name} holds no records\n{self.describe()}"
			errors = [level for level in levels if level in ("ERROR", "CRITICAL")]
			assert not errors, f"{name} holds {len(errors)} records at ERROR or above\n{log[-8000:]}"


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
			process = subprocess.Popen(command, env=env, cwd=directory, stdout=stdout, stderr=stderr)
		launch = Launch(directory, process, started)
		launches.append(launch)
		return launch

	yield start
	for launch in launches:
		if launch.process.poll() is None:
			launch.process.kill()
			launch.process.wait()
	# A copy that a launch started to restart itself is not this process's child, and is found by the
	# lock it holds.
	lock = singleInstance.InstanceLock(singleInstance.lockFilePath())
	try:
		if not lock.tryAcquire():
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
	deadline = time.monotonic() + STARTUP_BOUND
	while len(nvda.sentences()) < 2:
		assert time.monotonic() < deadline, f"the new copy did not speak\n{nvda.describe()}"
		time.sleep(0.05)
	# Not this process's child, so it is waited for through the process ID it recorded in the lock.
	lock = singleInstance.InstanceLock(singleInstance.lockFilePath())
	try:
		newCopy = lock.holder()
	finally:
		lock.close()
	assert newCopy is not None and newCopy != nvda.process.pid
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
