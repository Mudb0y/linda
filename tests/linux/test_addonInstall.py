"""Opening .nvda-addon files: MIME association, CLI argument handling, and controlSocket IPC in the rig.

Run under the rig:
	nix develop . -c tests/linux/rig python3 -m pytest tests/linux/test_addonInstall.py
"""

import importlib.util
import os
import signal
import socket
import subprocess
import sys
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Iterator
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
WAYLAND_NAME = "wayland-addon"

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


@pytest.fixture
def wayland() -> Iterator[dict[str, str]]:
	_rigLockPath()
	path = Path(os.environ["XDG_RUNTIME_DIR"]) / WAYLAND_NAME
	with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as compositor:
		compositor.bind(str(path))
		try:
			yield {"WAYLAND_DISPLAY": WAYLAND_NAME, "GDK_BACKEND": "x11"}
		finally:
			path.unlink()


def _makeAddonBundle(directory: Path, name: str = "sample") -> Path:
	bundle = directory / f"{name}.nvda-addon"
	addonDir = directory / name
	addonDir.mkdir(parents=True, exist_ok=True)
	manifest = (
		f"name = {name}\nsummary = {name}\ndescription = Test add-on\nauthor = A test\nversion = 1.0\n"
		"minimumNVDAVersion = 2024.1\nlastTestedNVDAVersion = 2026.3\n"
		"platforms = linux\n"
	)
	(addonDir / "manifest.ini").write_text(manifest, encoding="utf-8")
	with zipfile.ZipFile(bundle, "w") as archive:
		for path in addonDir.rglob("*"):
			archive.write(path, path.relative_to(addonDir))
	return bundle


def _startCopy(launches, tmp_path: Path, name: str, env: dict[str, str | None]) -> harness.Launch:
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
		env={"LINDA_BOOT_PROBE": "wait", **env},
	)
	nvda.waitForSpeech(SENTENCE, STARTUP_BOUND)
	return nvda


def _runLauncher(
	*arguments: str, env: dict[str, str | None] | None = None
) -> subprocess.CompletedProcess[str]:
	_rigLockPath()
	merged = {**os.environ, **(env or {})}
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
	nvda.assertClean(ignoring=("logErrors",), logNames=("nvda.log",))


def test_mimePackageXml():
	mimePath = ROOT / "linux" / "linda-mime.xml"
	assert mimePath.is_file(), f"MIME file not found at {mimePath}"
	tree = ET.parse(mimePath)
	root = tree.getroot()
	assert root.tag == "{http://www.freedesktop.org/standards/shared-mime-info}mime-info"
	mimeTypeElem = root.find("{http://www.freedesktop.org/standards/shared-mime-info}mime-type")
	assert mimeTypeElem is not None
	assert mimeTypeElem.attrib.get("type") == "application/x-nvda-addon"
	subclassElem = mimeTypeElem.find("{http://www.freedesktop.org/standards/shared-mime-info}sub-class-of")
	assert subclassElem is not None
	assert subclassElem.attrib.get("type") == "application/zip"
	globElem = mimeTypeElem.find("{http://www.freedesktop.org/standards/shared-mime-info}glob")
	assert globElem is not None
	assert globElem.attrib.get("pattern") == "*.nvda-addon"


def test_installAddonInRunningCopyViaIpc(launches, tmp_path: Path):
	nvda = _startCopy(launches, tmp_path, "running", {"WAYLAND_DISPLAY": None})
	bundle = _makeAddonBundle(tmp_path, "probe")

	# Pass via positional CLI argument
	done = _runLauncher(str(bundle))
	assert done.returncode == 0, done.stderr
	assert nvda.process.poll() is None
	nvda.waitForLog(f"Requesting installation of add-on from {bundle.resolve()}", 10.0)

	# Pass via file:// URI
	bundle2 = _makeAddonBundle(tmp_path, "probe2")
	doneUri = _runLauncher(f"file://{bundle2.resolve()}")
	assert doneUri.returncode == 0, doneUri.stderr
	assert nvda.process.poll() is None
	nvda.waitForLog(f"Requesting installation of add-on from {bundle2.resolve()}", 10.0)

	_quit(nvda)


def test_installAddonFreshStartup(launches, tmp_path: Path):
	_rigLockPath()
	bundle = _makeAddonBundle(tmp_path, "freshprobe")
	directory = tmp_path / "launches" / "fresh"
	configDir = directory / "config"
	configDir.mkdir(parents=True)
	text = CONFIG_FIXTURE.read_text(encoding="utf-8")
	text = text.replace("[general]\n", "[general]\n\tshowWelcomeDialogAtStartup = False\n", 1)
	(configDir / "nvda.ini").write_text(text, encoding="utf-8")
	nvda = launches(
		"fresh",
		str(bundle),
		"-c",
		str(configDir),
		"-f",
		str(directory / "nvda.log"),
		"--debug-logging",
		env={"LINDA_BOOT_PROBE": "wait", "WAYLAND_DISPLAY": None},
	)
	nvda.waitForSpeech(SENTENCE, STARTUP_BOUND)
	assert f"Requesting installation of add-on from {bundle.resolve()}" in nvda.text("nvda.log")
	_quit(nvda)


def test_concurrentTakeKeyboardAndInstallAddon(launches, tmp_path: Path, wayland: dict[str, str]):
	nvda = _startCopy(launches, tmp_path, "concurrent", wayland)
	bundle = _makeAddonBundle(tmp_path, "queuedaddon")
	socketPath = controlSocket.socketPath(_rigLockPath(WAYLAND_NAME))

	os.kill(nvda.process.pid, signal.SIGSTOP)
	s_take1 = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
	s_take1.settimeout(5.0)
	s_addon = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
	s_addon.settimeout(5.0)
	s_take2 = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
	s_take2.settimeout(5.0)
	try:
		s_take1.connect(socketPath)
		s_take1.sendall(b"take-keyboard\n")
		s_addon.connect(socketPath)
		s_addon.sendall(f"install-addon {bundle.resolve()}\n".encode())
		s_take2.connect(socketPath)
		s_take2.sendall(b"take-keyboard\n")
	finally:
		os.kill(nvda.process.pid, signal.SIGCONT)

	try:
		reply1 = s_take1.recv(16)
		assert reply1 == b"taken\n", reply1
		replyAddon = s_addon.recv(16)
		assert replyAddon == b"ok\n", replyAddon
		reply2 = s_take2.recv(16)
		assert reply2 == b"taken\n", reply2
	finally:
		s_take1.close()
		s_addon.close()
		s_take2.close()

	nvda.waitForLog(f"Requesting installation of add-on from {bundle.resolve()}", 10.0)
	assert nvda.process.poll() is None
	_quit(nvda)


def test_conflictingOptionsWithAddon(tmp_path: Path):
	bundle = _makeAddonBundle(tmp_path, "conflict")
	doneQuit = _runLauncher("--quit", str(bundle))
	assert doneQuit.returncode == 1
	assert "--quit cannot be combined with add-on installation" in doneQuit.stderr

	doneCheck = _runLauncher("--check-running", str(bundle))
	assert doneCheck.returncode == 1
	assert "--check-running cannot be combined with add-on installation" in doneCheck.stderr


def test_newlineInPathRejected():
	done = _runLauncher("bad\nname.nvda-addon")
	assert done.returncode == 1
	assert "add-on path cannot contain newline" in done.stderr


def test_nonexistentAddonRejected():
	done = _runLauncher("/nonexistent/path/missing.nvda-addon")
	assert done.returncode == 1
	assert "add-on package not found" in done.stderr
