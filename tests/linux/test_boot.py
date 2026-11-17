"""The launcher as a real process: NVDA starts, speaks through the transcript synth, and exits.

Each test starts source/linda.py through harness.startLauncher, with a configuration from
fixtures/bootConfig, the transcript synth, and the boot probe plugin from fixtures/globalPlugins,
which says one sentence once NVDA has started. Run under the rig, which gives every launch its display
and its runtime directory, where the single-instance lock is:

	nix develop . -c tests/linux/rig python3 -m pytest tests/linux/test_boot.py
"""

import json
import os
import re
import select
import signal
import struct
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

import configobj
import harness
import pytest

HERE = Path(__file__).resolve().parent
LAUNCHER = harness.LAUNCHER
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


singleInstance = harness.singleInstance()
#: What the probe logs for each call that showed a dialog, once it has returned.
_answer = re.compile(r"^boot probe: (?P<name>\w+) answered (?P<answer>None|-?\d+)$", re.MULTILINE)

#: start(name, probe, *arguments, configDir=None, welcome=True, env=None) starts the launcher in a
#: directory of its own; see the start fixture.
Start = Callable[..., harness.Launch]


def configDirOf(launch: harness.Launch) -> Path:
	"""The configuration directory start gives a launch that is given none."""
	return launch.directory / "config"


def answers(launch: harness.Launch) -> dict[str, int | None]:
	"""What each call that showed one of the probe's dialogs got back."""
	return {
		m["name"]: None if m["answer"] == "None" else int(m["answer"])
		for m in _answer.finditer(launch.text("nvda.log"))
	}


def savedSetting(launch: harness.Launch) -> str | None:
	"""The value the launch's nvda.ini on disk holds for the setting the probe changes."""
	section, key, _value = CHANGED_SETTING
	path = configDirOf(launch) / "nvda.ini"
	return configobj.ConfigObj(str(path)).get(section, {}).get(key) if path.exists() else None


def spokenCount(launch: harness.Launch, text: str = SENTENCE) -> int:
	return launch.spokenTexts().count(text)


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


def _catches(processID: int, signalNumber: signal.Signals) -> bool:
	"""Whether the process has a handler of its own for the signal, from the kernel's view of it."""
	status = Path(f"/proc/{processID}/status").read_text(encoding="ascii")
	caught = int(re.search(r"^SigCgt:\s*([0-9a-f]+)$", status, re.MULTILINE)[1], 16)
	return bool(caught & (1 << (signalNumber - 1)))


def waitUntilIdle(launch: harness.Launch, timeout: float = STARTUP_BOUND) -> None:
	"""Wait until NVDA's main thread is blocked in the main loop's poll, where only an event, a timer
	or a signal can wake it."""
	# The main thread's ID is the process's.
	wchan = Path(f"/proc/{launch.process.pid}/wchan")
	deadline = time.monotonic() + timeout
	while "poll" not in (where := wchan.read_text(encoding="ascii")):
		assert time.monotonic() < deadline and launch.process.poll() is None, (
			f"the main thread was not idle within {timeout} s, last in {where!r}\n{launch.describe()}"
		)
		time.sleep(0.01)


@pytest.fixture
def start(launches, tmp_path: Path) -> Start:
	"""Starts launches through conftest's launches fixture, which makes sure none outlives the test or
	wrote into the repository."""
	_rigLockPath()

	def start(
		name: str,
		probe: str | None = None,
		*args: str,
		configDir: Path | None = None,
		welcome: bool = True,
		env: dict[str, str | None] | None = None,
	) -> harness.Launch:
		"""Start the launcher in a directory of its own.

		:param probe: What the boot probe does, and with it a fixture configuration, a log and debug
			logging; None for a launch that only acts on another copy.
		:param configDir: The configuration directory, which is made from the fixture unless it exists;
			by default configDirOf the launch.
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
		if probe:
			args = (*args, "-c", str(configDir), "-f", str(directory / "nvda.log"), "--debug-logging")
		return launches(name, *args, env={"LINDA_BOOT_PROBE": probe, **(env or {})})

	return start


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


def test_startsSpeaksAndExitsThroughTriggerNVDAExit(start: Start):
	nvda = start("exit", "exit")
	toSpeech = nvda.waitForSpeech(SENTENCE, STARTUP_BOUND)
	assert nvda.waitForExit(SHUTDOWN_BOUND) == 0, nvda.describe()
	nvda.assertClean(logNames=("nvda.log",))
	log = nvda.text("nvda.log")
	assert "_doShutdown has been queued" in log
	assert "Exiting on" not in log
	assert "NVDA exit" in log
	# Java applications are reached through AT-SPI, so the missing Windows bridge is no warning.
	assert "Java Access Bridge not available" not in log
	assert "No Java Access Bridge on Linux" in log
	print(f"start-up to speech: {toSpeech:.2f} s")


def test_remoteAccessSwitchedOnStartsAndExits(start: Start, tmp_path: Path):
	# What the Enable Remote Access checkbox of the settings leaves in the configuration.
	configDir = tmp_path / "remoteConfig"
	configDir.mkdir()
	(configDir / "nvda.ini").write_text(
		CONFIG_FIXTURE.read_text(encoding="utf-8") + "[remote]\n\tenabled = True\n", encoding="utf-8"
	)
	nvda = start("remote", "exit", configDir=configDir)
	nvda.waitForSpeech(SENTENCE, STARTUP_BOUND)
	assert nvda.waitForExit(SHUTDOWN_BOUND) == 0, nvda.describe()
	nvda.assertClean(logNames=("nvda.log",))
	assert "Initializing NVDA Remote client" in nvda.text("nvda.log")


def test_remoteAccessConnectsToAServerOfItsOwn(start: Start, tmp_path: Path):
	# Hosting the control server here takes the relay server, its certificate, a TLS connection to it
	# and the session, with no second machine.
	configDir = tmp_path / "remoteConfig"
	configDir.mkdir()
	(configDir / "nvda.ini").write_text(
		CONFIG_FIXTURE.read_text(encoding="utf-8")
		+ "[remote]\n\tenabled = True\n\t[[controlServer]]\n\t\tautoconnect = True\n"
		"\t\tselfHosted = True\n\t\tconnectionMode = 0\n\t\tport = 16837\n\t\tkey = loopback\n",
		encoding="utf-8",
	)
	nvda = start("remote", "wait", configDir=configDir)
	nvda.waitForSpeech(SENTENCE, STARTUP_BOUND)
	deadline = time.monotonic() + STARTUP_BOUND
	while "Control connector connected" not in (log := nvda.text("nvda.log")):
		assert time.monotonic() < deadline and nvda.process.poll() is None, nvda.describe()
		time.sleep(0.05)
	assert "Client 1 authenticated successfully" in log
	nvda.process.send_signal(signal.SIGTERM)
	assert nvda.waitForExit(SHUTDOWN_BOUND) == 0, nvda.describe()
	nvda.assertClean(logNames=("nvda.log",))
	assert "Shutting down NVDA Remote relay server" in nvda.text("nvda.log")


def test_addOnMarkedForRemovalIsRemovedAtStart(start: Start, tmp_path: Path):
	# What removing an add-on in the add-on store leaves for the next start to carry out.
	configDir = tmp_path / "addOnConfig"
	addOn = configDir / "addons" / "leaving"
	addOn.mkdir(parents=True)
	(configDir / "nvda.ini").write_text(CONFIG_FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
	(addOn / "manifest.ini").write_text(
		"name = leaving\n"
		'summary = "An add-on on its way out"\n'
		'description = "Does nothing"\n'
		'author = "A test"\n'
		"version = 1.0\n"
		"minimumNVDAVersion = 2024.1\n"
		"lastTestedNVDAVersion = 2026.3\n",
		encoding="utf-8",
	)
	(addOn / "globalPlugins").mkdir()
	(addOn / "globalPlugins" / "leaving.py").write_text("raise RuntimeError('loaded')\n", encoding="utf-8")
	stateFile = configDir / "addonsState.json"
	stateFile.write_text(
		json.dumps(
			{
				"pendingRemovesSet": ["leaving"],
				"pendingInstallsSet": [],
				"disabledAddons": [],
				"pendingEnableSet": [],
				"pendingDisableSet": [],
				"overrideCompatibility": [],
				"blocked": [],
				"PENDING_OVERRIDE_COMPATIBILITY": [],
				"backCompatToAPIVersion": [2026, 1, 0],
			}
		),
		encoding="utf-8",
	)
	nvda = start("addOn", "exit", configDir=configDir)
	nvda.waitForSpeech(SENTENCE, STARTUP_BOUND)
	assert nvda.waitForExit(SHUTDOWN_BOUND) == 0, nvda.describe()
	nvda.assertClean(logNames=("nvda.log",))
	assert os.listdir(configDir / "addons") == []
	assert (
		not stateFile.exists() or json.loads(stateFile.read_text(encoding="utf-8"))["pendingRemovesSet"] == []
	)


@pytest.mark.parametrize("signalNumber", [signal.SIGTERM, signal.SIGHUP, signal.SIGINT])
def test_exitsOnSignal(start: Start, signalNumber: signal.Signals):
	nvda = start("wait", "wait")
	nvda.waitForSpeech(SENTENCE, STARTUP_BOUND)
	# Nothing but the signal is to wake the main loop.
	waitUntilIdle(nvda)
	asked = time.monotonic()
	nvda.process.send_signal(signalNumber)
	assert nvda.waitForExit(SHUTDOWN_BOUND) == 0, nvda.describe()
	toExit = time.monotonic() - asked
	nvda.assertClean(logNames=("nvda.log",))
	assert f"Exiting on {signalNumber.name}" in nvda.text("nvda.log")
	print(f"{signalNumber.name} to exit: {toExit:.2f} s")


@pytest.mark.parametrize("signalNumber", [signal.SIGHUP, signal.SIGINT])
def test_signalIgnoredAtStartStaysIgnored(start: Start, signalNumber: signal.Signals):
	# As nohup starts a program, so that it outlives the terminal it was started from.
	previous = signal.signal(signalNumber, signal.SIG_IGN)
	try:
		nvda = start("ignoring", "wait")
	finally:
		signal.signal(signalNumber, previous)
	nvda.waitForSpeech(SENTENCE, STARTUP_BOUND)
	status = Path(f"/proc/{nvda.process.pid}/status").read_text(encoding="ascii")
	ignored = int(re.search(r"^SigIgn:\s*([0-9a-f]+)$", status, re.MULTILINE)[1], 16)
	assert ignored & (1 << (signalNumber - 1)), status
	nvda.process.send_signal(signalNumber)
	nvda.process.send_signal(signal.SIGTERM)
	assert nvda.waitForExit(SHUTDOWN_BOUND) == 0, nvda.describe()
	nvda.assertClean(logNames=("nvda.log",))
	log = nvda.text("nvda.log")
	assert f"Exiting on {signalNumber.name}" not in log
	assert "Exiting on SIGTERM" in log


def test_repeatedQuitSignalsDuringExit(start: Start):
	# The probe's terminate takes two seconds, so that these arrive while the main loop still runs.
	nvda = start("repeated", "slowExit", welcome=False)
	nvda.waitForSpeech(SENTENCE, STARTUP_BOUND)
	nvda.process.send_signal(signal.SIGTERM)
	nvda.waitForLog("Exiting on SIGTERM", SHUTDOWN_BOUND)
	# What the end of a session sends after SIGTERM, and a second request to quit.
	nvda.process.send_signal(signal.SIGHUP)
	nvda.process.send_signal(signal.SIGTERM)
	assert nvda.waitForExit(SHUTDOWN_BOUND) == 0, nvda.describe()
	nvda.assertClean(logNames=("nvda.log",))
	log = nvda.text("nvda.log")
	assert log.count("Exiting on SIGTERM") == 2
	assert "Exiting on SIGHUP" in log
	assert "NVDA exit" in log


def test_secondSignalEndsAnExitThatHangs(start: Start):
	# The probe leaves a thread running that NVDA's exit waits for after the main loop.
	nvda = start("hang", "hangAtExit", welcome=False)
	nvda.waitForSpeech(SENTENCE, STARTUP_BOUND)
	nvda.process.send_signal(signal.SIGTERM)
	nvda.waitForLog("Waiting on <Thread(bootProbeHang", SHUTDOWN_BOUND)
	nvda.process.send_signal(signal.SIGTERM)
	assert nvda.waitForExit(SHUTDOWN_BOUND) == -signal.SIGTERM, nvda.describe()
	assert savedSetting(nvda) == CHANGED_SETTING[2]
	nvda.assertClean(ignoring={"returnCode"}, logNames=("nvda.log",))


def test_firstSignalAfterTheMainLoopIsNotFatal(start: Start):
	# A copy that this one started to restart NVDA sends SIGTERM as it takes over, whenever that is.
	nvda = start("hang", "hangAtExit+exit", welcome=False)
	nvda.waitForLog("Waiting on <Thread(bootProbeHang", STARTUP_BOUND)
	nvda.process.send_signal(signal.SIGTERM)
	nvda.waitForLog("SIGTERM while exiting", SHUTDOWN_BOUND)
	assert nvda.process.poll() is None
	nvda.process.send_signal(signal.SIGTERM)
	assert nvda.waitForExit(SHUTDOWN_BOUND) == -signal.SIGTERM, nvda.describe()
	nvda.assertClean(ignoring={"returnCode"}, logNames=("nvda.log",))


@pytest.mark.parametrize("signalNumber", [signal.SIGTERM, signal.SIGINT])
def test_repeatedSignalEndsAnExitHeldInC(start: Start, signalNumber: signal.Signals):
	# No signal started this exit, so the first after the main loop is let through, as a copy started
	# to restart NVDA sends one. The probe then holds the main thread in C with the GIL, where no
	# Python signal handler can ever run.
	nvda = start("heldInC", "hangInC+exit", welcome=False)
	nvda.waitForLog("boot probe: blocking in C", STARTUP_BOUND)
	assert _catches(nvda.process.pid, signalNumber), nvda.describe()
	nvda.process.send_signal(signalNumber)
	# The kernel puts the signal back to its default action as it delivers it, and the process lives on.
	deadline = time.monotonic() + SHUTDOWN_BOUND
	while nvda.process.poll() is None and _catches(nvda.process.pid, signalNumber):
		assert time.monotonic() < deadline, f"the first signal was not delivered\n{nvda.describe()}"
		time.sleep(0.01)
	assert nvda.process.poll() is None, nvda.describe()
	nvda.process.send_signal(signalNumber)
	assert nvda.waitForExit(SHUTDOWN_BOUND) == -signalNumber, nvda.describe()
	assert savedSetting(nvda) == CHANGED_SETTING[2]
	nvda.assertClean(ignoring={"returnCode"}, logNames=("nvda.log",))


def test_signalSavesTheConfigurationFirst(start: Start):
	# The probe's terminate never returns, so the exit never reaches the save after the main loop.
	nvda = start("hangInTerminate", "hangInTerminate", welcome=False)
	nvda.waitForSpeech(SENTENCE, STARTUP_BOUND)
	nvda.process.send_signal(signal.SIGTERM)
	deadline = time.monotonic() + SHUTDOWN_BOUND
	while savedSetting(nvda) != CHANGED_SETTING[2]:
		assert time.monotonic() < deadline, f"the setting was not saved\n{nvda.describe()}"
		time.sleep(0.05)
	assert nvda.process.poll() is None


def test_secondLaunchReplacesFirst(start: Start, tmp_path: Path):
	# Left by a copy with a longer process ID, which a shorter one must not leave a tail of.
	Path(_rigLockPath()).write_text("4194303\nready\n", encoding="ascii")
	sharedConfig = tmp_path / "config"
	first = start("first", "wait", configDir=sharedConfig)
	first.waitForSpeech(SENTENCE, STARTUP_BOUND)
	second = start("second", "exit", configDir=sharedConfig)
	# The second asks the first to quit, and only kills it if it is still there after four seconds.
	assert first.waitForExit(SHUTDOWN_BOUND) == 0, first.describe()
	second.waitForSpeech(SENTENCE, STARTUP_BOUND)
	assert second.waitForExit(SHUTDOWN_BOUND) == 0, second.describe()
	first.assertClean(logNames=("nvda.log",))
	second.assertClean(logNames=("nvda.log",))
	assert "Exiting on SIGTERM" in first.text("nvda.log")
	firstEnd = first.spoken()[-1]["t"]
	secondStart = second.spoken()[0]["t"]
	assert firstEnd < secondStart


def test_copyStillStartingIsGivenTimeToStart(start: Start):
	# The probe makes the first copy's start take longer than the QUIT_TIMEOUT a ready copy gets.
	first = start("slow", "slowStart", welcome=False)
	deadline = time.monotonic() + STARTUP_BOUND
	while _runningCopy() != first.process.pid:
		assert time.monotonic() < deadline and first.process.poll() is None, first.describe()
		time.sleep(0.01)
	assert _readyCopy() is None
	second = start("second", "exit", welcome=False)
	assert first.waitForExit(STARTUP_BOUND) == 0, first.describe()
	second.waitForSpeech(SENTENCE, STARTUP_BOUND)
	assert second.waitForExit(SHUTDOWN_BOUND) == 0, second.describe()
	first.assertClean(logNames=("nvda.log",))
	second.assertClean(logNames=("nvda.log",))
	assert "Exiting on SIGTERM" in first.text("nvda.log")


def test_hungCopyIsKilled(start: Start):
	holder = _holdLock(_rigLockPath(), "PID\nready\n", ignoreTerm=True)
	try:
		nvda = start("takeover", "exit")
		limit = singleInstance.QUIT_TIMEOUT + singleInstance.KILL_TIMEOUT + SHUTDOWN_BOUND
		assert holder.wait(limit) == -signal.SIGKILL
		nvda.waitForSpeech(SENTENCE, STARTUP_BOUND)
		assert nvda.waitForExit(SHUTDOWN_BOUND) == 0, nvda.describe()
		nvda.assertClean(logNames=("nvda.log",))
	finally:
		if holder.poll() is None:
			holder.kill()
		holder.wait()


def test_restartStartsACopyThatTakesOver(start: Start):
	# The new copy's command line is quoted as Windows quotes it and split again here.
	nvda = start('restart in "a dir\\" with \\\\ quotes', "restart")
	nvda.waitForSpeech(SENTENCE, STARTUP_BOUND)
	assert nvda.waitForExit(SHUTDOWN_BOUND) == 0, nvda.describe()
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
	assert os.fsencode(configDirOf(nvda)) in command, command
	while spokenCount(nvda) < 2:
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
	assert "Starting new NVDA instance" in nvda.text("nvda-old.log")
	assert "NVDA exit" in nvda.text("nvda.log")


def test_checkRunningAndQuit(start: Start):
	before = start("checkBefore", None, "--check-running")
	assert before.waitForExit(SHUTDOWN_BOUND) == 1, before.describe()
	before.assertClean(ignoring={"returnCode"})
	nvda = start("wait", "wait")
	nvda.waitForSpeech(SENTENCE, STARTUP_BOUND)
	running = start("checkRunning", None, "--check-running")
	assert running.waitForExit(SHUTDOWN_BOUND) == 0, running.describe()
	running.assertClean()
	assert nvda.process.poll() is None
	quitter = start("quit", None, "--quit")
	assert quitter.waitForExit(SHUTDOWN_BOUND) == 0, quitter.describe()
	quitter.assertClean()
	assert nvda.waitForExit(SHUTDOWN_BOUND) == 0, nvda.describe()
	nvda.assertClean(logNames=("nvda.log",))
	after = start("checkAfter", None, "--check-running")
	assert after.waitForExit(SHUTDOWN_BOUND) == 1, after.describe()
	after.assertClean(ignoring={"returnCode"})


def test_coreFailureIsReportedOnStandardError(start: Start):
	# wx raises SystemExit when there is no display to open.
	nvda = start("noDisplay", "exit", env={"DISPLAY": None})
	assert nvda.waitForExit(SHUTDOWN_BOUND) == 1, nvda.describe()
	assert re.search(r"^CRITICAL - __main__\.main .*:\ncore failure$", nvda.text("nvda.log"), re.MULTILINE), (
		nvda.describe()
	)
	# One line, and nothing from a hook of logHandler's that the interpreter took apart as it exited.
	assert harness.unexpectedStandardError(nvda.text("stderr.txt")) == (
		"linda: core failure, SystemExit: Unable to access the X Display, is $DISPLAY set "
		f"properly?; the log is {nvda.directory / 'nvda.log'}\n"
	)
	nvda.assertClean(ignoring={"returnCode", "logErrors", "stderr"})


def test_launcherOptionIsRefused(start: Start):
	nvda = start("launcherOption", "exit", "--launcher")
	assert nvda.waitForExit(SHUTDOWN_BOUND) == 1, nvda.describe()
	assert nvda.text("stderr.txt").startswith("linda: --launcher ")
	assert not nvda.text("nvda.log")
	nvda.assertClean(ignoring={"returnCode", "stderr"})


@pytest.mark.parametrize(
	"option",
	["--install", "--install-silent", "--create-portable", "--create-portable-silent"],
)
def test_installerOptionsAreRefused(start: Start, option: str):
	nvda = start(option.lstrip("-"), "exit", option)
	assert nvda.waitForExit(SHUTDOWN_BOUND) == 1, nvda.describe()
	assert nvda.text("stderr.txt").startswith(f"linda: {option} uses NVDA's Windows installer")
	assert not nvda.text("nvda.log")
	nvda.assertClean(ignoring={"returnCode", "stderr"})


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
		import linda

		linda.main()
		""",
		prepare="none",
	)
	assert done.error is not None and done.error.rstrip().endswith("SystemExit: 1"), done.error
	assert done.stderr.startswith("linda: cannot take "), done.stderr
	assert done.stderr.rstrip().endswith("No locks available"), done.stderr
	done.assertClean(ignoring={"error", "stderr"})


def test_greeterCopyTakesNoTestHooks(start: Start):
	# On a display manager's login screen NVDA runs in secure mode, which turns logging off as well.
	nvda = start("greeter", "exit", env={"XDG_SESSION_CLASS": "greeter"})
	deadline = time.monotonic() + STARTUP_BOUND
	while _readyCopy() != nvda.process.pid:
		assert time.monotonic() < deadline and nvda.process.poll() is None, nvda.describe()
		time.sleep(0.05)
	nvda.process.send_signal(signal.SIGTERM)
	assert nvda.waitForExit(SHUTDOWN_BOUND) == 0, nvda.describe()
	assert not nvda.spoken()
	assert not nvda.text("nvda.log")
	# Without the test hooks it speaks through NVDA's default synthesizer, eSpeak, and reaches for
	# nothing that could sound; the rig has no sound device for it to reach.
	reached = nvda.result().reached
	assert any(
		what.startswith("ctypes.dlopen ") and harness.isNamedEspeakLibrary(what.split(" ", 1)[1])
		for what in reached
	), reached
	nvda.assertClean()


def test_configuredDriverFromOutsideTheTreeLoads(start: Start, tmp_path: Path):
	# openevv's driver is built into a directory of its own, which LINDA_OPENEVV_DRIVER_DIR names.
	configDir = tmp_path / "openevvConfig"
	configDir.mkdir()
	text = CONFIG_FIXTURE.read_text(encoding="utf-8").replace("synth = transcript", "synth = openevv")
	(configDir / "nvda.ini").write_text(text, encoding="utf-8")
	nvda = start("openevv", "exit", configDir=configDir)
	assert nvda.waitForExit(STARTUP_BOUND + SHUTDOWN_BOUND) == 0, nvda.describe()
	nvda.assertClean(logNames=("nvda.log",))
	assert "Loaded synthDriver openevv" in nvda.text("nvda.log")


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

		result = [before, gtk(), modalDialogs.areNoneOpen()]
		""",
		prepare="bootstrap",
	)
	done.assertClean()
	before, after, areNoneOpen = done.value
	assert len(before) == 1 and "/libgtk-3.so" in before[0], before
	assert after == before
	assert areNoneOpen


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
	script = "/nvda/source/linda.py"
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
	(tmp_path / "linda").mkdir(mode=0o755)
	(tmp_path / "linda").chmod(0o755)
	with pytest.raises(PermissionError):
		singleInstance.lockFilePath()
	(tmp_path / "linda").chmod(0o700)
	assert singleInstance.lockFilePath().startswith(str(tmp_path / "linda") + os.sep)


def test_lockWithoutRuntimeDirectoryIsInTheUsersCache(monkeypatch, tmp_path: Path):
	# As under su or sudo. No other user can make this directory first, as one could in /tmp.
	monkeypatch.delenv("XDG_RUNTIME_DIR")
	monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
	assert singleInstance.lockFilePath().startswith(str(tmp_path / "cache" / "linda") + os.sep)
	monkeypatch.delenv("XDG_CACHE_HOME")
	monkeypatch.setenv("HOME", str(tmp_path / "home"))
	assert singleInstance.lockFilePath().startswith(str(tmp_path / "home" / ".cache" / "linda") + os.sep)


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


def test_startsFromUpstreamDefaults(start: Start):
	# The fixture leaves the welcome dialog, the question at exit and every other first-start dialog
	# at upstream's defaults; the welcome dialog is open while the probe speaks.
	assert "showWelcomeDialogAtStartup" not in CONFIG_FIXTURE.read_text(encoding="utf-8")
	nvda = start("defaults", "wait")
	nvda.waitForSpeech(SENTENCE, STARTUP_BOUND)
	nvda.waitForLog("Did context help binding for WelcomeDialog", STARTUP_BOUND)
	nvda.process.send_signal(signal.SIGTERM)
	assert nvda.waitForExit(SHUTDOWN_BOUND) == 0, nvda.describe()
	nvda.assertClean(logNames=("nvda.log",))
	assert "ending modal <gui.startupDialogs.WelcomeDialog" in nvda.text("nvda.log")


def test_exitCommandAsksFirst(start: Start):
	nvda = start("exitDialog", "exitDialog")
	nvda.waitForSpeech(EXIT_DIALOG_SENTENCE, STARTUP_BOUND)
	nvda.process.send_signal(signal.SIGTERM)
	assert nvda.waitForExit(SHUTDOWN_BOUND) == 0, nvda.describe()
	nvda.assertClean(logNames=("nvda.log",))


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
def test_exitsOnSignalWithMessageBoxOpen(start: Start, probe: str, answer: int | None):
	nvda = start(probe, probe)
	nvda.waitForSpeech(DIALOG_SENTENCE, STARTUP_BOUND)
	asked = time.monotonic()
	nvda.process.send_signal(signal.SIGTERM)
	# Well within the four seconds after which a copy replacing this one would kill it.
	assert nvda.waitForExit(singleInstance.QUIT_TIMEOUT) == 0, nvda.describe()
	toExit = time.monotonic() - asked
	nvda.assertClean(logNames=("nvda.log",))
	log = nvda.text("nvda.log")
	assert "Exiting on SIGTERM" in log
	assert "_doShutdown has been queued" in log
	assert answers(nvda) == {probe: answer}
	assert savedSetting(nvda) == CHANGED_SETTING[2]
	if probe == "messageBox":
		# An error box, whose sound Linux does not have.
		assert nvda.result().noLinuxImplementation == ["winsound.MessageBeep"]
	print(f"SIGTERM to exit with {probe} open: {toExit:.2f} s")


@pytest.mark.parametrize("route", ["signal", "triggerNVDAExit"])
@pytest.mark.parametrize(
	("probe", "welcome", "answered"),
	[
		# GTK's own dialog, inside the welcome dialog.
		("nativeMessageBox", True, {"nativeMessageBox": WX_CANCEL}),
		("fileDialog", False, {"fileDialog": WX_ID_CANCEL}),
		("nestedDialogs", False, {"inner": WX_ID_CLOSE_ALL, "outer": WX_ID_CLOSE_ALL}),
		("dialogInWelcome", True, {"inner": WX_ID_CLOSE_ALL}),
	],
)
def test_exitsWithModalDialogsOpen(
	start: Start,
	probe: str,
	welcome: bool,
	answered: dict,
	route: str,
):
	nvda = start(probe, probe if route == "signal" else f"{probe}+exit", welcome=welcome)
	nvda.waitForSpeech(DIALOG_SENTENCE, STARTUP_BOUND)
	if route == "signal":
		nvda.process.send_signal(signal.SIGTERM)
	assert nvda.waitForExit(singleInstance.QUIT_TIMEOUT) == 0, nvda.describe()
	nvda.assertClean(logNames=("nvda.log",))
	assert answers(nvda) == answered
	assert savedSetting(nvda) == CHANGED_SETTING[2]
	assert "NVDA exit" in nvda.text("nvda.log")


@pytest.mark.parametrize("route", ["signal", "triggerNVDAExit"])
def test_exitsWhileTheCallInsideADialogGoesOn(start: Start, route: str):
	# Once its dialog is answered, the call that showed it handles events and then asks a question
	# inside the outer dialog. Ended before that call returns, the outer dialog either never ends or is
	# destroyed under the question.
	nvda = start("followUp", "followUp" if route == "signal" else "followUp+exit", welcome=False)
	nvda.waitForSpeech(DIALOG_SENTENCE, STARTUP_BOUND)
	if route == "signal":
		nvda.process.send_signal(signal.SIGTERM)
	assert nvda.waitForExit(singleInstance.QUIT_TIMEOUT) == 0, nvda.describe()
	nvda.assertClean(logNames=("nvda.log",))
	# In the order the calls returned.
	assert list(answers(nvda).items()) == [
		("inner", WX_ID_CLOSE_ALL),
		("followUp", WX_CANCEL),
		("outer", WX_ID_CLOSE_ALL),
	]
	assert savedSetting(nvda) == CHANGED_SETTING[2]


def test_callThatNeverReturnsInsideADialogEndsNVDA(start: Start):
	# Once its dialog is answered, the call that showed it handles events for ever inside the outer
	# dialog, which can therefore never be ended.
	nvda = start("neverReturns", "neverReturns", welcome=False)
	nvda.waitForSpeech(DIALOG_SENTENCE, STARTUP_BOUND)
	nvda.process.send_signal(signal.SIGTERM)
	assert nvda.waitForExit(singleInstance.QUIT_TIMEOUT) == GIVEN_UP_STATUS, nvda.describe()
	log = nvda.text("nvda.log")
	given = re.search(
		r"^Gave up ending modal dialogs: the call that showed .* has not returned after [\d.]+ s; "
		r"ending NVDA without finishing its exit$",
		log,
		re.MULTILINE,
	)
	assert given, log[-4000:]
	assert answers(nvda) == {"inner": WX_ID_CLOSE_ALL}
	assert savedSetting(nvda) == CHANGED_SETTING[2]
	assert nvda.result().logErrors == [f"ERROR: {given[0]}"]
	nvda.assertClean(ignoring={"returnCode", "logErrors", "stderr"}, logNames=("nvda.log",))


@pytest.mark.parametrize("route", ["signal", "triggerNVDAExit"])
def test_dialogReopenedForEverEndsNVDA(start: Start, route: str):
	# Code that shows its dialog again each time it is ended would otherwise keep NVDA half shut down
	# for ever, its plugins terminated and its main loop never returning.
	nvda = start("reopening", "reopening" if route == "signal" else "reopening+exit", welcome=False)
	nvda.waitForSpeech(DIALOG_SENTENCE, STARTUP_BOUND)
	if route == "signal":
		nvda.process.send_signal(signal.SIGTERM)
	assert nvda.waitForExit(singleInstance.QUIT_TIMEOUT) == GIVEN_UP_STATUS, nvda.describe()
	log = nvda.text("nvda.log")
	given = re.search(
		r"^Gave up ending modal dialogs: .* is still open after ending (\d+); ending NVDA without "
		r"finishing its exit$",
		log,
		re.MULTILINE,
	)
	assert given, log[-4000:]
	assert len(harness.nvdaLogErrorRecords(log)) == 1, log[-4000:]
	assert log.count("ending modal <Gtk.MessageDialog") == int(given[1])
	assert savedSetting(nvda) == CHANGED_SETTING[2]
	assert f"linda: {given[0]}\n" in nvda.text("stderr.txt")
	assert nvda.result().logErrors == [f"ERROR: {given[0]}"]
	nvda.assertClean(ignoring={"returnCode", "logErrors", "stderr"}, logNames=("nvda.log",))


def test_magnifierToggleSaysItCannotStart(start: Start):
	nvda = start("magnifier", "magnifier")
	nvda.waitForSpeech(SENTENCE, STARTUP_BOUND)
	assert nvda.waitForExit(SHUTDOWN_BOUND) == 0, nvda.describe()
	nvda.assertClean(logNames=("nvda.log",))
	spoken = nvda.spokenTexts()
	assert "Cannot start magnifier. Linda's magnifier is not available on Linux." in spoken
	assert "Magnifier enabled" not in spoken


def test_exitsOnSignalAsSoonAsItHoldsTheLock(start: Start):
	# A copy replacing this one can send SIGTERM from the moment this one holds the lock.
	nvda = start("early", "wait")
	deadline = time.monotonic() + STARTUP_BOUND
	while _runningCopy() != nvda.process.pid:
		assert time.monotonic() < deadline and nvda.process.poll() is None, nvda.describe()
		time.sleep(0.001)
	nvda.process.send_signal(signal.SIGTERM)
	assert nvda.waitForExit(STARTUP_BOUND) == 0, nvda.describe()
	nvda.assertClean(logNames=("nvda.log",))
	assert "Exiting on SIGTERM" in nvda.text("nvda.log")


def test_lockHolderThatRecordedNothingIsNeverSignalled(start: Start):
	# A process can hold the lock while the file still names the copy before it, which may be gone
	# and its process ID reused by anything.
	bystander = subprocess.Popen(["sleep", "60"])
	lockPath = _rigLockPath()
	Path(lockPath).write_text(f"{bystander.pid}\n", encoding="ascii")
	holder = _holdLock(lockPath, "")
	try:
		assert _runningCopy() == holder.pid
		running = start("checkRunning", None, "--check-running")
		assert running.waitForExit(SHUTDOWN_BOUND) == 0, running.describe()
		running.assertClean()
		quitter = start("quit", None, "--quit")
		assert quitter.waitForExit(SHUTDOWN_BOUND) == 0, quitter.describe()
		assert "has not recorded itself" in quitter.text("stderr.txt")
		quitter.assertClean(ignoring={"stderr"})
		# A copy that would start waits its while for the lock, as nvda.pyw waits for the mutex, and
		# gives up.
		starter = start("start", None)
		assert starter.waitForExit(SHUTDOWN_BOUND) == 1, starter.describe()
		assert "has not recorded itself" in starter.text("stderr.txt")
		assert f"linda: another copy still holds {lockPath}\n" in starter.text("stderr.txt")
		starter.assertClean(ignoring={"returnCode", "stderr"})
		assert bystander.poll() is None
		assert holder.poll() is None
	finally:
		holder.stdin.close()
		holder.wait()
		bystander.kill()
		bystander.wait()


def test_unlockedRecordIsNeverSignalled(start: Start):
	# A process ID left in the lock file by a copy that has gone may since belong to anything.
	bystander = subprocess.Popen(["sleep", "60"])
	try:
		Path(_rigLockPath()).write_text(f"{bystander.pid}\nready\n", encoding="ascii")
		quitter = start("quit", None, "--quit")
		assert quitter.waitForExit(SHUTDOWN_BOUND) == 0, quitter.describe()
		quitter.assertClean()
		assert bystander.poll() is None
	finally:
		bystander.kill()
		bystander.wait()


def _formerLockPath() -> str:
	"""The lock file of the rig's display that a copy started under the program's former name holds."""
	path = singleInstance.lockFilePath(singleInstance.FORMER_APP_NAME)
	runtime = os.path.dirname(os.path.dirname(_rigLockPath()))
	assert path.startswith(os.path.join(runtime, singleInstance.FORMER_APP_NAME) + os.sep), path
	return path


def test_copyStartedUnderTheFormerNameIsReplaced(start: Start):
	holder = _holdLock(_formerLockPath(), "PID\nready\n")
	try:
		nvda = start("takeover", "wait")
		assert holder.wait(STARTUP_BOUND) == -signal.SIGTERM
		nvda.waitForSpeech(SENTENCE, STARTUP_BOUND)
		# It holds the former name's lock as well, so that a copy started under that name replaces it
		# as it replaces any copy.
		former = singleInstance.InstanceLock(_formerLockPath())
		try:
			deadline = time.monotonic() + STARTUP_BOUND
			while former.recordedReady() != nvda.process.pid:
				assert time.monotonic() < deadline and nvda.process.poll() is None, nvda.describe()
				time.sleep(0.01)
			assert former.holder() == nvda.process.pid
			singleInstance.terminateRunning(former)
		finally:
			former.close()
		assert nvda.waitForExit(SHUTDOWN_BOUND) == 0, nvda.describe()
		nvda.assertClean(logNames=("nvda.log",))
		assert "Exiting on SIGTERM" in nvda.text("nvda.log")
	finally:
		if holder.poll() is None:
			holder.kill()
		holder.wait()


def test_checkRunningAndQuitFindACopyUnderTheFormerName(start: Start):
	holder = _holdLock(_formerLockPath(), "PID\nready\n")
	try:
		running = start("checkRunning", None, "--check-running")
		assert running.waitForExit(SHUTDOWN_BOUND) == 0, running.describe()
		running.assertClean()
		assert holder.poll() is None
		quitter = start("quit", None, "--quit")
		assert quitter.waitForExit(SHUTDOWN_BOUND) == 0, quitter.describe()
		quitter.assertClean()
		assert holder.wait(SHUTDOWN_BOUND) == -signal.SIGTERM
	finally:
		if holder.poll() is None:
			holder.kill()
		holder.wait()


def test_settingsAndLogsUnderTheFormerNameCarryOver(launches, tmp_path: Path):
	_rigLockPath()
	configHome = tmp_path / "xdgConfig"
	stateHome = tmp_path / "xdgState"
	(configHome / "basedreader").mkdir(parents=True)
	(configHome / "basedreader" / "nvda.ini").write_text(
		CONFIG_FIXTURE.read_text(encoding="utf-8"), encoding="utf-8"
	)
	(stateHome / "basedreader").mkdir(parents=True)
	(stateHome / "basedreader" / "nvda.log").write_text("a log of the former name\n", encoding="utf-8")
	nvda = launches(
		"adopt",
		"-f",
		str(tmp_path / "launches" / "adopt" / "nvda.log"),
		"--debug-logging",
		env={
			"LINDA_BOOT_PROBE": "exit",
			"XDG_CONFIG_HOME": str(configHome),
			"XDG_STATE_HOME": str(stateHome),
		},
	)
	# Only the configuration it was moved with sets the transcript synth, which is what hears this.
	nvda.waitForSpeech(SENTENCE, STARTUP_BOUND)
	assert nvda.waitForExit(SHUTDOWN_BOUND) == 0, nvda.describe()
	nvda.assertClean(logNames=("nvda.log",))
	assert os.readlink(configHome / "basedreader") == "linda"
	assert (configHome / "linda" / "nvda.ini").is_file()
	assert os.readlink(stateHome / "basedreader") == "linda"
	assert (stateHome / "linda" / "nvda.log").read_text(encoding="utf-8") == "a log of the former name\n"
