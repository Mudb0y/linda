"""A MATE sign-in in the rig: mate-session started as a display manager starts it, with a home, an
X server, a session bus and an accessibility bus of its own, and Linda started from its autostart
entries, with what Linda spoke and logged.

The session gets its own X server and buses rather than the rig's because ending a session stops the
clients of its session manager, and the accessibility bus launcher is one: the rig's would not
outlive the first logout, and every later test in the run would find no accessibility bus.

Inside the rig only. Every process the session starts carries a variable with a token of its own,
which is how close finds and stops them all, mate-session's children and those orphaned when it exits
alike, by their process IDs; while a session runs, this process is their subreaper, so that the exit
status of a Linda that outlives mate-session can be read.
"""

import ctypes
import os
import re
import select
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Self

import harness

#: The variable whose value marks every process of one session.
MARKER = "LINDA_SESSION_TEST"
#: The configuration Linda starts with, as desktopSession's sessions have it, with debug logging.
SESSION_CONFIG = harness.HERE / "fixtures" / "sessionConfig" / "nvda.ini"
RIG_SESSION_CONF = harness.HERE / "rig-session.conf"
#: Seconds to wait for the X server, the buses and mate-session's first phase.
SERVICE_TIMEOUT = 30.0
#: Seconds processes get to exit on SIGTERM before they are killed.
STOP_TIMEOUT = 10.0
#: The first display number tried; the rig's own is 99.
FIRST_DISPLAY = 120
#: What mate-session's --debug says of its phases and of the programs it starts and sees end.
_PHASE = re.compile(r"GsmManager: (?P<what>starting|ending) phase (?P<phase>[A-Z_]+)")
_STARTED = re.compile(r"GsmAutostartApp: started pid:(?P<pid>\d+)")
_DONE = re.compile(r"GsmAutostartApp: \(pid:(?P<pid>\d+)\) done \((?P<how>\w+):(?P<code>-?\d+)\)")
_PR_SET_CHILD_SUBREAPER = 36
#: The program in a desktop entry's Exec that stands in for the installed launcher, PREFIX/bin/linda.
LAUNCHER_NAME = "linda"


def _setSubreaper(on: bool) -> None:
	libc = ctypes.CDLL(None, use_errno=True)
	if libc.prctl(_PR_SET_CHILD_SUBREAPER, int(on), 0, 0, 0) != 0:
		raise OSError(ctypes.get_errno(), "prctl(PR_SET_CHILD_SUBREAPER)")


def _isAlive(pid: int) -> bool:
	try:
		with open(f"/proc/{pid}/stat", encoding="ascii", errors="replace") as f:
			state = f.read().rpartition(")")[2].split()[0]
	except (OSError, IndexError):
		return False
	return state not in ("Z", "X")


def _markedProcesses(token: str) -> list[int]:
	needle = f"{MARKER}={token}".encode()
	found = []
	for entry in os.listdir("/proc"):
		if not entry.isdigit():
			continue
		try:
			with open(f"/proc/{entry}/environ", "rb") as f:
				variables = f.read().split(b"\0")
		except OSError:
			continue
		if needle in variables:
			found.append(int(entry))
	return found


def lindaConfig(directory: Path) -> Path:
	"""An nvda.ini like SESSION_CONFIG with debug logging, in directory."""
	text = SESSION_CONFIG.read_text(encoding="utf-8")
	text = text.replace("[general]\n", "[general]\n\tloggingLevel = DEBUG\n", 1)
	directory.mkdir(parents=True, exist_ok=True)
	path = directory / "nvda.ini"
	path.write_text(text, encoding="utf-8")
	return path


class SessionProcess:
	"""A process mate-session started, as much of subprocess.Popen as harness.Launch uses.

	Its exit status is what mate-session saw, or, once mate-session is gone and the process with it
	orphaned, what waiting on it as its subreaper gives.
	"""

	def __init__(self, session: "MateSession", pid: int) -> None:
		self.session = session
		self.pid = pid
		self.returncode: int | None = None

	def poll(self) -> int | None:
		if self.returncode is not None:
			return self.returncode
		try:
			waited, status = os.waitpid(self.pid, os.WNOHANG)
		except ChildProcessError:
			waited = 0
		if waited:
			self.returncode = os.waitstatus_to_exitcode(status)
			return self.returncode
		if not _isAlive(self.pid):
			# Reaped by mate-session, which says so a moment later.
			self.returncode = self.session.exitStatus(self.pid)
		return self.returncode

	def wait(self, timeout: float | None = None) -> int:
		deadline = None if timeout is None else time.monotonic() + timeout
		while self.poll() is None:
			if deadline is not None and time.monotonic() >= deadline:
				raise subprocess.TimeoutExpired(f"process {self.pid}", timeout)
			time.sleep(0.05)
		return self.returncode

	def send_signal(self, signalNumber: int) -> None:
		if self.poll() is None:
			os.kill(self.pid, signalNumber)

	def kill(self) -> None:
		self.send_signal(signal.SIGKILL)


class MateSession:
	"""One sign-in: its home, its X server and buses, mate-session, and the Linda it starts.

	Set up the home first, with writeAutostart, lindaEntry and settings, then start.
	"""

	def __init__(self, directory: Path) -> None:
		harness.requireRig()
		self.directory = directory
		self.home = directory / "home"
		self.configHome = self.home / ".config"
		self.stateHome = self.home / ".local" / "state"
		# Not under directory: atk-bridge's socket in it would have a longer path than a Unix socket can.
		self.runtimeDir = Path(tempfile.mkdtemp(prefix="session-"))
		#: Where the system's autostart entries would be, empty unless a test puts one there.
		self.systemConfig = directory / "xdg"
		#: Applications the session can find by name, as in /usr/share/applications.
		self.dataDir = directory / "share"
		for path in (self.configHome / "autostart", self.stateHome, self.systemConfig, self.dataDir):
			path.mkdir(parents=True, exist_ok=True)
		self.token = uuid.uuid4().hex
		self._processes: list[subprocess.Popen] = []
		self._lines: list[tuple[float, str]] = []
		self._linesLock = threading.Lock()
		self._reader: threading.Thread | None = None
		self.manager: subprocess.Popen | None = None
		self.display: str | None = None
		self.busAddress: str | None = None
		self._subreaper = False
		#: Variables the sign-in adds to the session's environment, such as LINDA_BOOT_PROBE.
		self.variables: dict[str, str] = {}
		self._hooks = directory / "testHooks"
		self._hooks.mkdir(exist_ok=True)
		for name, target in (
			("synthDrivers", harness.HERE / "synthDrivers"),
			("globalPlugins", harness.HERE / "fixtures" / "globalPlugins"),
		):
			(self._hooks / name).symlink_to(target)
		self.settings(
			"org/mate/desktop/session",
			# The window manager alone of MATE's required components: the panel and the file manager
			# would only slow the session down, and none of them is what the tests are about.
			{
				"required-components-list": "['windowmanager']",
				"default-session": "@as []",
				"gnome-compat-startup": "@as []",
			},
		)
		self.settings("org/mate/desktop/session/required-components", {"windowmanager": "'marco'"})
		lindaConfig(self.configHome / "linda")

	def __enter__(self) -> Self:
		return self

	def __exit__(self, *exception: object) -> None:
		self.close()

	# The home.

	def settings(self, path: str, values: dict[str, str]) -> None:
		"""Set GSettings keys at path, as GVariant text, in the key file the session reads them from."""
		keyfile = self.configHome / "glib-2.0" / "settings" / "keyfile"
		keyfile.parent.mkdir(parents=True, exist_ok=True)
		text = keyfile.read_text(encoding="utf-8") if keyfile.exists() else ""
		lines = [f"[{path}]", *(f"{key}={value}" for key, value in values.items())]
		keyfile.write_text(text + "\n".join(lines) + "\n\n", encoding="utf-8")

	@property
	def launcher(self) -> Path:
		"""A stand-in for the installed launcher, which starts Linda from the checkout under the
		harness, with the transcript synth and the test plugins, and records each run in a directory
		of its own under runs."""
		path = self.directory / "bin" / LAUNCHER_NAME
		if path.exists():
			return path
		path.parent.mkdir(exist_ok=True)
		runs = self.directory / "runs"
		runs.mkdir(exist_ok=True)
		launch = harness.isolatedCommand(
			Path("$run/overlay"),
			[
				sys.executable,
				"-I",
				"-B",
				str(Path(harness.__file__).resolve()),
				"launch",
				"$run/events.jsonl",
				str(harness.LOG_ERROR),
				str(harness.LAUNCHER),
			],
		)
		# The run directory is the shell's to fill in, and mktemp names it with no character to quote.
		command = " ".join(f'"{part}"' if part.startswith("$run/") else shlex.quote(part) for part in launch)
		path.write_text(
			"#!/bin/sh\n"
			f"run=$(mktemp -d {shlex.quote(str(runs))}/run.XXXXXX)\n"
			'echo $$ >"$run/pid"\n'
			'env >"$run/environment"\n'
			# Where NVDA logs by default, and where a copy it restarts as moves the log aside.
			f'ln -s {shlex.quote(str(self.stateHome / "linda" / "nvda.log"))} "$run/nvda.log"\n'
			f'ln -s {shlex.quote(str(self.stateHome / "linda" / "nvda-old.log"))} "$run/nvda-old.log"\n'
			f"export LINDA_TEST_HOOKS_DIR={shlex.quote(str(self._hooks))}\n"
			'export LINDA_TRANSCRIPT_FILE="$run/transcript.jsonl" LINDA_TRANSCRIPT_MODE=immediate\n'
			'exec >"$run/stdout.txt" 2>"$run/stderr.txt"\n'
			f'exec {command} "$@"\n',
			encoding="utf-8",
		)
		path.chmod(0o755)
		return path

	def writeAutostart(self, name: str, lines: list[str]) -> Path:
		"""Write the user's autostart entry name from the lines after its group header."""
		path = self.configHome / "autostart" / name
		path.write_text("\n".join(["[Desktop Entry]", *lines]) + "\n", encoding="utf-8")
		return path

	def lindaEntry(self, phase: str | None = None) -> Path:
		"""An autostart entry that starts the launcher, in phase or, with none, among the applications."""
		lines = ["Type=Application", "Name=Linda", f"Exec={self.launcher}"]
		if phase:
			lines.append(f"X-MATE-Autostart-Phase={phase}")
		return self.writeAutostart("linda.desktop", lines)

	@property
	def environment(self) -> dict[str, str]:
		"""The environment of the sign-in, as a display manager gives it to mate-session."""
		env = {
			name: value
			for name, value in os.environ.items()
			if name
			not in (
				"DISPLAY",
				"DBUS_SESSION_BUS_ADDRESS",
				"DBUS_STARTER_ADDRESS",
				"DBUS_STARTER_BUS_TYPE",
				"XDG_CURRENT_DESKTOP",
				"PYTEST_CURRENT_TEST",
			)
		}
		env.update(
			{
				MARKER: self.token,
				"HOME": str(self.home),
				"XDG_CONFIG_HOME": str(self.configHome),
				"XDG_STATE_HOME": str(self.stateHome),
				"XDG_CACHE_HOME": str(self.home / ".cache"),
				"XDG_DATA_HOME": str(self.home / ".local" / "share"),
				"XDG_CONFIG_DIRS": str(self.systemConfig),
				"XDG_DATA_DIRS": f"{self.dataDir}:{os.environ.get('XDG_DATA_DIRS', '')}",
				"XDG_RUNTIME_DIR": str(self.runtimeDir),
				"GSETTINGS_BACKEND": "keyfile",
			}
		)
		env.update(self.variables)
		if self.display:
			env["DISPLAY"] = self.display
		if self.busAddress:
			env["DBUS_SESSION_BUS_ADDRESS"] = self.busAddress
		return env

	# Starting and watching.

	def _spawn(self, command: list[str], logName: str, **kwargs) -> subprocess.Popen:
		with open(self.directory / logName, "w") as log:
			process = subprocess.Popen(
				command,
				env=self.environment,
				stdin=subprocess.DEVNULL,
				stdout=kwargs.pop("stdout", log),
				stderr=log,
				**kwargs,
			)
		self._processes.append(process)
		return process

	def _readLine(self, process: subprocess.Popen, stream, timeout: float) -> str:
		deadline = time.monotonic() + timeout
		buffer = b""
		while not buffer.endswith(b"\n"):
			remaining = deadline - time.monotonic()
			if remaining <= 0 or process.poll() is not None:
				return ""
			readable, _, _ = select.select([stream], [], [], min(remaining, 0.5))
			if readable:
				chunk = os.read(stream.fileno(), 256)
				if not chunk:
					return ""
				buffer += chunk
		return buffer.decode().strip()

	def _startDisplay(self) -> None:
		for number in range(FIRST_DISPLAY, FIRST_DISPLAY + 20):
			read, write = os.pipe()
			try:
				server = self._spawn(
					["Xvfb", f":{number}", "-displayfd", str(write), "-screen", "0", "1280x1024x24"]
					+ ["-nolisten", "tcp", "-noreset"],
					"xvfb.log",
					pass_fds=(write,),
				)
			finally:
				os.close(write)
			with os.fdopen(read, "rb") as stream:
				shown = self._readLine(server, stream, SERVICE_TIMEOUT)
			if shown:
				self.display = f":{shown}"
				self.xserver = server
				return
			server.kill()
			server.wait()
			self._processes.remove(server)
		raise AssertionError(f"no X server would start\n{(self.directory / 'xvfb.log').read_text()}")

	def _startBus(self) -> None:
		bus = self._spawn(
			["dbus-daemon", f"--config-file={RIG_SESSION_CONF}", "--nofork", "--print-address=1"],
			"bus.log",
			stdout=subprocess.PIPE,
		)
		self.bus = bus
		self.busAddress = self._readLine(bus, bus.stdout, SERVICE_TIMEOUT)
		assert self.busAddress, f"the session bus did not start\n{(self.directory / 'bus.log').read_text()}"

	def busCall(self, *arguments: str, timeout: float = 10) -> subprocess.CompletedProcess:
		"""dbus-send on the session's bus, with its reply printed."""
		return subprocess.run(
			["dbus-send", "--session", "--print-reply", *arguments],
			env=self.environment,
			capture_output=True,
			text=True,
			timeout=timeout,
			check=False,
		)

	def _hasOwner(self, name: str) -> bool:
		reply = self.busCall(
			"--dest=org.freedesktop.DBus",
			"/org/freedesktop/DBus",
			"org.freedesktop.DBus.NameHasOwner",
			f"string:{name}",
		)
		return "boolean true" in reply.stdout

	def _startAccessibilityBus(self) -> None:
		# As the rig starts its own, and as D-Bus activation starts it on a desktop, before the session
		# manager, which the launcher then registers with.
		launcher = self._spawn([os.environ["AT_SPI_BUS_LAUNCHER"], "--launch-immediately"], "a11y.log")
		deadline = time.monotonic() + SERVICE_TIMEOUT
		while not self._hasOwner("org.a11y.Bus"):
			assert launcher.poll() is None and time.monotonic() < deadline, (
				f"the accessibility bus did not start\n{(self.directory / 'a11y.log').read_text()}"
			)
			time.sleep(0.05)
		for name in ("IsEnabled", "ScreenReaderEnabled"):
			self.busCall(
				"--dest=org.a11y.Bus",
				"/org/a11y/bus",
				"org.freedesktop.DBus.Properties.Set",
				"string:org.a11y.Status",
				f"string:{name}",
				"variant:boolean:true",
			)

	def _readManager(self) -> None:
		with open(self.directory / "session.log", "w", encoding="utf-8") as log:
			for line in self.manager.stderr:
				now = time.monotonic()
				with self._linesLock:
					self._lines.append((now, line))
				log.write(f"{now:.3f} {line}")
				log.flush()

	def start(self, *arguments: str) -> None:
		"""Start the X server, the buses and mate-session, which starts the session's programs."""
		_setSubreaper(True)
		self._subreaper = True
		self._startDisplay()
		self._startBus()
		self._startAccessibilityBus()
		self.started = time.monotonic()
		self.manager = subprocess.Popen(
			["mate-session", "--debug", "--disable-acceleration-check", *arguments],
			env=self.environment,
			stdin=subprocess.DEVNULL,
			stdout=subprocess.DEVNULL,
			stderr=subprocess.PIPE,
			text=True,
			errors="replace",
		)
		self._processes.append(self.manager)
		self._reader = threading.Thread(target=self._readManager, name="mateSessionLog", daemon=True)
		self._reader.start()
		self.waitForPhase("INITIALIZATION", "starting", SERVICE_TIMEOUT)

	def lines(self) -> list[tuple[float, str]]:
		"""What mate-session has said so far, each line with the time it was read."""
		with self._linesLock:
			return list(self._lines)

	def log(self) -> str:
		return "".join(f"{when - self.started:8.3f} {line}" for when, line in self.lines())

	def phaseTime(self, phase: str, what: str = "starting") -> float | None:
		"""When mate-session said it was starting or ending phase, or None if it has not."""
		for when, line in self.lines():
			found = _PHASE.search(line)
			if found and found["phase"] == phase and found["what"] == what:
				return when
		return None

	def waitForPhase(self, phase: str, what: str = "starting", timeout: float = 60) -> float:
		deadline = time.monotonic() + timeout
		while (when := self.phaseTime(phase, what)) is None:
			exited = self.manager.poll() is not None
			if exited or time.monotonic() >= deadline:
				# The reader may not have caught up with the last lines.
				if (when := self.phaseTime(phase, what)) is not None:
					return when
				raise AssertionError(
					f"mate-session did not reach {what} phase {phase} within {timeout} s, "
					f"{harness.describeExit(self.manager.poll())}\n{self.log()[-12000:]}"
				)
			time.sleep(0.05)
		return when

	def exitStatus(self, pid: int) -> int | None:
		"""The exit status mate-session gave for a process it started, as Popen gives it."""
		for _when, line in self.lines():
			found = _DONE.search(line)
			if found and int(found["pid"]) == pid:
				code = int(found["code"])
				return code if found["how"] == "status" else -code
		return None

	def startedTime(self, pid: int) -> float | None:
		for when, line in self.lines():
			found = _STARTED.search(line)
			if found and int(found["pid"]) == pid:
				return when
		return None

	def runs(self) -> list[Path]:
		"""The run directories of each Linda the launcher started, oldest first."""
		runs = self.directory / "runs"
		found = [run for run in runs.iterdir() if (run / "pid").exists()] if runs.exists() else []
		return sorted(found, key=lambda run: (run / "pid").stat().st_mtime_ns)

	def waitForLinda(self, timeout: float = 60, index: int = 0) -> harness.Launch:
		"""The Linda the launcher started index-th, as a harness.Launch, once it has started."""
		deadline = time.monotonic() + timeout
		while True:
			runs = self.runs()
			if len(runs) > index and (pid := (runs[index] / "pid").read_text().strip()):
				break
			assert time.monotonic() < deadline, (
				f"the session did not start Linda within {timeout} s\n{self.log()[-12000:]}"
			)
			time.sleep(0.05)
		run = runs[index]
		pid = int(pid)
		started = self.startedTime(pid) or time.monotonic()
		return harness.Launch(run, SessionProcess(self, pid), started, run / "overlay")

	def press(self, *chords: str, gap: float = 0.15) -> None:
		for chord in chords:
			subprocess.run(["xdotool", "key", chord], env=self.environment, check=True)
			time.sleep(gap)

	def logout(self) -> None:
		"""Ask mate-session to end the session without asking the user, as the panel's Log Out does once
		its dialog has been answered."""
		reply = self.busCall(
			"--dest=org.gnome.SessionManager",
			"/org/gnome/SessionManager",
			"org.gnome.SessionManager.Logout",
			"uint32:1",
		)
		assert reply.returncode == 0, reply.stderr

	def closeDisplay(self) -> None:
		"""Close the X server, as a display manager does once the session has ended."""
		self.xserver.terminate()
		self.xserver.wait(STOP_TIMEOUT)

	def marked(self) -> list[int]:
		"""The session's processes still running."""
		return [pid for pid in _markedProcesses(self.token) if _isAlive(pid)]

	def close(self) -> None:
		"""Stop every process of the session, by its process ID: SIGTERM, then SIGKILL for those that
		do not exit in time."""
		try:
			for signalNumber in (signal.SIGTERM, signal.SIGKILL):
				for pid in self.marked():
					try:
						os.kill(pid, signalNumber)
					except ProcessLookupError:
						pass
				deadline = time.monotonic() + STOP_TIMEOUT
				while self.marked() and time.monotonic() < deadline:
					self._reap()
					time.sleep(0.05)
			for process in self._processes:
				try:
					process.wait(STOP_TIMEOUT)
				except subprocess.TimeoutExpired:
					process.kill()
					process.wait()
			self._reap()
			if self._reader is not None:
				self._reader.join(STOP_TIMEOUT)
		finally:
			shutil.rmtree(self.runtimeDir, ignore_errors=True)
			if self._subreaper:
				_setSubreaper(False)
				self._subreaper = False

	def _reap(self) -> None:
		"""Wait on the session's orphans that have exited, which only this process can."""
		for pid in _markedProcesses(self.token):
			if _isAlive(pid):
				continue
			try:
				os.waitpid(pid, os.WNOHANG)
			except ChildProcessError:
				pass
