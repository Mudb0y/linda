"""linda --take-keyboard: the running copy asked from outside to take the keyboard back, through the
control socket beside its lock, and what the caller says when it cannot be asked or does not answer.

Run under the rig, which gives every launch its display and its runtime directory:

	nix develop . -c tests/linux/rig python3 -m pytest tests/linux/test_takeKeyboard.py
"""

import importlib.util
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import harness
import pytest

HERE = Path(__file__).resolve().parent
CONFIG_FIXTURE = HERE / "fixtures" / "bootConfig" / "nvda.ini"
#: What fixtures/globalPlugins/bootProbe.py says once NVDA has started.
SENTENCE = "the boot probe has started"
STARTUP_BOUND = 60.0
SHUTDOWN_BOUND = 20.0
#: The rig's X display; the lock of any other would be a live session's.
RIG_DISPLAY = ":99"
#: Seconds within which a caller must have given up on a copy that does not answer.
GIVE_UP_BOUND = 3.0

singleInstance = harness.singleInstance()


def _loadControlSocket():
	spec = importlib.util.spec_from_file_location(
		"controlSocket", harness.SOURCE / "linuxPlatform" / "controlSocket.py"
	)
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module


controlSocket = _loadControlSocket()

pytestmark = pytest.mark.rigSession("x11")


def _rigLockPath() -> str:
	why = harness.whyNotInRig()
	assert why is None, f"refusing to touch a display's lock outside the rig: {why}"
	assert os.environ.get("DISPLAY") == RIG_DISPLAY, f"DISPLAY is {os.environ.get('DISPLAY')!r}"
	path = singleInstance.lockFilePath()
	runtime = os.environ["XDG_RUNTIME_DIR"]
	assert path.startswith(runtime + os.sep) and not runtime.startswith("/run/"), path
	return path


def _startCopy(launches, tmp_path: Path, name: str) -> harness.Launch:
	"""A copy that runs until it is signalled, once it has spoken."""
	_rigLockPath()
	directory = tmp_path / "launches" / name
	configDir = directory / "config"
	configDir.mkdir(parents=True)
	text = CONFIG_FIXTURE.read_text(encoding="utf-8")
	text = text.replace("[general]\n", "[general]\n\tshowWelcomeDialogAtStartup = False\n", 1)
	(configDir / "nvda.ini").write_text(text, encoding="utf-8")
	nvda = launches(
		name,
		"-c",
		str(configDir),
		"-f",
		str(directory / "nvda.log"),
		"--debug-logging",
		env={"LINDA_BOOT_PROBE": "wait"},
	)
	nvda.waitForSpeech(SENTENCE, STARTUP_BOUND)
	return nvda


def _takeKeyboard(*arguments: str) -> tuple[int, str, str, float]:
	"""Run linda --take-keyboard as a user would: exit status, standard output and error, and seconds."""
	_rigLockPath()
	started = time.monotonic()
	done = subprocess.run(
		[sys.executable, "-I", "-B", str(harness.LAUNCHER), "--take-keyboard", *arguments],
		capture_output=True,
		check=False,
		text=True,
		timeout=60,
		stdin=subprocess.DEVNULL,
	)
	return done.returncode, done.stdout, done.stderr, time.monotonic() - started


def _quit(nvda: harness.Launch) -> None:
	nvda.process.send_signal(signal.SIGTERM)
	assert nvda.waitForExit(SHUTDOWN_BOUND) == 0, nvda.describe()
	nvda.assertClean(logNames=("nvda.log",))


def test_runningCopyIsAskedAndAnswers(launches, tmp_path: Path):
	nvda = _startCopy(launches, tmp_path, "running")
	socketPath = controlSocket.socketPath(_rigLockPath())
	assert Path(socketPath).is_socket()
	# Something that is not the request is turned away without an answer, and nothing is done.
	with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stranger:
		stranger.settimeout(SHUTDOWN_BOUND)
		stranger.connect(socketPath)
		stranger.sendall(b"junk\n")
		assert stranger.recv(64) == b""
	status, stdout, stderr, seconds = _takeKeyboard()
	assert (status, stdout, stderr) == (0, controlSocket._SAID[controlSocket.ANSWERED] + "\n", "")
	log = nvda.text("nvda.log")
	assert log.count("through the control socket, to take the keyboard back") == 1, nvda.describe()
	assert nvda.process.poll() is None
	print(f"asked and answered in {seconds:.2f} s")
	_quit(nvda)
	# Gone with the copy, so that the next caller hears that nothing runs.
	assert not Path(socketPath).exists()
	status, stdout, _stderr, _seconds = _takeKeyboard()
	assert (status, stdout) == (1, controlSocket._SAID[controlSocket.NOT_RUNNING] + "\n")


def test_copyWhoseMainLoopIsStoppedDoesNotAnswer(launches, tmp_path: Path):
	nvda = _startCopy(launches, tmp_path, "stopped")
	os.kill(nvda.process.pid, signal.SIGSTOP)
	try:
		status, stdout, stderr, seconds = _takeKeyboard()
	finally:
		os.kill(nvda.process.pid, signal.SIGCONT)
	assert (status, stdout, stderr) == (1, controlSocket._SAID[controlSocket.NO_ANSWER] + "\n", ""), seconds
	assert seconds < GIVE_UP_BOUND, f"gave up after {seconds:.2f} s"
	print(f"gave up after {seconds:.2f} s")
	# The request it took while stopped is acted on once it runs again, to nobody, and it goes on.
	nvda.waitForLog("through the control socket, to take the keyboard back", SHUTDOWN_BOUND)
	assert nvda.process.poll() is None
	_quit(nvda)


def test_copyWithNoSocketIsNeverSignalled(launches, tmp_path: Path):
	# As a copy from before the control socket holds its lock: ready, and ended by any signal.
	holder = subprocess.Popen(
		[
			sys.executable,
			"-I",
			"-c",
			(
				"import fcntl, os, sys\n"
				f"fd = os.open({_rigLockPath()!r}, os.O_RDWR | os.O_CREAT, 0o600)\n"
				"fcntl.lockf(fd, fcntl.LOCK_EX)\n"
				"os.ftruncate(fd, 0)\n"
				"os.pwrite(fd, f'{os.getpid()}\\nready\\n'.encode(), 0)\n"
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
		status, stdout, stderr, _seconds = _takeKeyboard()
		assert (status, stdout, stderr) == (1, controlSocket._SAID[controlSocket.UNREACHABLE] + "\n", "")
		time.sleep(0.5)
		assert holder.poll() is None
	finally:
		holder.stdin.close()
		assert holder.wait(SHUTDOWN_BOUND) == 0


def test_nothingRunning():
	_rigLockPath()
	status, stdout, stderr, _seconds = _takeKeyboard()
	assert (status, stdout, stderr) == (1, controlSocket._SAID[controlSocket.NOT_RUNNING] + "\n", "")


def test_takesNoOtherArgument():
	status, stdout, stderr, _seconds = _takeKeyboard("--debug-logging")
	assert (status, stdout) == (2, "")
	assert stderr == "linda: --take-keyboard takes no other argument, so nothing was done\n"
