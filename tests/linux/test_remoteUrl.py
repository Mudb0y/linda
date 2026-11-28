"""Opening nvdaremote:// URLs: desktop entry scheme registration, CLI handling, and controlSocket IPC
(issue 62).

Run under the rig:
	nix develop . -c tests/linux/rig python3 -m pytest -q tests/linux/test_remoteUrl.py
"""

import importlib.util
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import harness
import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
CONFIG_FIXTURE = HERE / "fixtures" / "bootConfig" / "nvda.ini"
SENTENCE = "the boot probe has started"
STARTUP_BOUND = 60.0
SHUTDOWN_BOUND = 20.0
RIG_DISPLAY = ":99"
VALID_URL = "nvdaremote://example.com?key=testkey&mode=master"
VALID_FOLLOWER_URL = "nvdaremote://example.com:7000?key=testkey&mode=slave"

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


def _rigLockPath(display: str = RIG_DISPLAY) -> str:
	why = harness.whyNotInRig()
	assert why is None, f"refusing to touch a display's lock outside the rig: {why}"
	assert os.environ.get("DISPLAY") == RIG_DISPLAY, f"DISPLAY is {os.environ.get('DISPLAY')!r}"
	path = singleInstance.lockFilePath(singleInstance.APP_NAME, display)
	runtime = os.environ["XDG_RUNTIME_DIR"]
	assert path.startswith(runtime + os.sep) and not runtime.startswith("/run/"), path
	return path


def _startCopy(
	launches,
	tmp_path: Path,
	name: str,
	*,
	enableRemote: bool = False,
	extraArgs: tuple[str, ...] = (),
) -> harness.Launch:
	_rigLockPath()
	directory = tmp_path / "launches" / name
	configDir = directory / "config"
	configDir.mkdir(parents=True)
	text = CONFIG_FIXTURE.read_text(encoding="utf-8")
	text = text.replace("[general]\n", "[general]\n\tshowWelcomeDialogAtStartup = False\n", 1)
	if enableRemote:
		text += "\n[remote]\n\tenabled = True\n"
	(configDir / "nvda.ini").write_text(text, encoding="utf-8")
	nvda = launches(
		name,
		*extraArgs,
		"-c",
		str(configDir),
		"-f",
		str(directory / "nvda.log"),
		"--debug-logging",
		env={"LINDA_BOOT_PROBE": "wait", "WAYLAND_DISPLAY": None},
	)
	nvda.waitForSpeech(SENTENCE, STARTUP_BOUND)
	return nvda


def _runLauncher(*arguments: str) -> subprocess.CompletedProcess[str]:
	_rigLockPath()
	merged = {**os.environ, "WAYLAND_DISPLAY": None}
	return subprocess.run(
		[sys.executable, "-I", "-B", str(harness.LAUNCHER), *arguments],
		capture_output=True,
		check=False,
		text=True,
		timeout=60,
		stdin=subprocess.DEVNULL,
		env={name: value for name, value in merged.items() if value is not None},
	)


def _quit(nvda: harness.Launch) -> None:
	nvda.process.send_signal(signal.SIGTERM)
	assert nvda.waitForExit(SHUTDOWN_BOUND) == 0, nvda.describe()
	nvda.assertClean(logNames=("nvda.log",))


def test_desktopEntryDeclaresNvdaremoteScheme():
	desktopFile = ROOT / "linux" / "linda.desktop"
	lines = desktopFile.read_text(encoding="utf-8").splitlines()
	assert "Exec=linda %U" in lines, lines
	assert "MimeType=application/x-nvda-addon;x-scheme-handler/nvdaremote;" in lines, lines
	validator = shutil.which("desktop-file-validate")
	if validator is not None:
		done = subprocess.run([validator, str(desktopFile)], capture_output=True, text=True, check=False)
		assert done.returncode == 0, done.stdout + done.stderr


def test_openRemoteUrlInRunningCopyViaIpc(launches, tmp_path: Path):
	nvda = _startCopy(launches, tmp_path, "running", enableRemote=True)

	done = _runLauncher(VALID_URL)
	assert done.returncode == 0, done.stderr
	assert nvda.process.poll() is None
	nvda.waitForLog("Requesting Remote Access connection from nvdaremote URL", 10.0)
	# Remote Access shows its confirmation dialog with the parsed server address.
	records = harness.transcriptRecords()
	deadline = time.monotonic() + 10.0
	while not any(
		"Remote Access Connection Request" in records.spokenText(r) and "6837" in records.spokenText(r)
		for r in nvda.spoken()
	):
		assert time.monotonic() < deadline, nvda.describe()
		time.sleep(0.05)

	# Invalid URLs sent directly over the control socket are rejected with failed\n.
	sockPath = controlSocket.socketPath(_rigLockPath())
	with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as caller:
		caller.settimeout(5.0)
		caller.connect(sockPath)
		caller.sendall(b"open-remote-url nvdaremote://missing-params\n")
		assert caller.recv(16) == b"failed\n"

	_quit(nvda)


def test_openRemoteUrlFreshStartupWhenRemoteDisabled(launches, tmp_path: Path):
	nvda = _startCopy(launches, tmp_path, "fresh", enableRemote=False, extraArgs=(VALID_FOLLOWER_URL,))
	nvda.waitForLog("Requesting Remote Access connection from nvdaremote URL", 10.0)
	nvda.waitForLog("Remote Access is disabled; ignoring nvdaremote URL", 10.0)
	_quit(nvda)


@pytest.mark.parametrize(
	"url",
	[
		"nvdaremote://",
		"nvdaremote://example.com?mode=master",
		"nvdaremote://example.com?key=secret",
		"nvdaremote://example.com?key=secret&mode=bogus",
	],
)
def test_invalidRemoteUrlsRejected(url: str):
	done = _runLauncher(url)
	assert done.returncode == 1
	assert "invalid Remote Access URL" in done.stderr


def test_newlineAndConflictingOptionsRejected():
	doneNewline = _runLauncher("nvdaremote://example.com?key=a\nb&mode=master")
	assert doneNewline.returncode == 1
	assert "Remote Access URL cannot contain newline" in doneNewline.stderr

	doneQuit = _runLauncher("--quit", VALID_URL)
	assert doneQuit.returncode == 1
	assert "--quit cannot be combined with a Remote Access URL" in doneQuit.stderr

	doneCheck = _runLauncher("--check-running", VALID_URL)
	assert doneCheck.returncode == 1
	assert "--check-running cannot be combined with a Remote Access URL" in doneCheck.stderr
