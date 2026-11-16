"""A private PipeWire daemon, its session manager, and a recorder of what a sink plays, for tests
that run in tests/linux/rig.

The daemon has no devices. Two null sinks, rig-sink-a and rig-sink-b, follow a dummy driver, and
WirePlumber links streams to the default one as it would on a desktop, so nothing here can be
heard. The fixture starts nothing outside the rig or where the sandbox has sound devices, and it
signals only processes it started itself.

	with PipewireFixture(tmp_path / "pipewire") as pipewire:
		monitor = pipewire.monitor()
		done = harness.runChild(tmp_path, code, env=pipewire.childEnv())

or, as a pytest fixture, import runningPipewire from here and take a parameter named pipewire.
"""

import json
import os
import re
import signal
import subprocess
import threading
import time
from array import array
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Self

import harness
import pytest

HERE = Path(__file__).resolve().parent
CONFIG_DIR = HERE / "pipewire"
#: The graph's rate in the daemon's configuration, and the rate pwmon records at.
RATE = 48000

#: What has to be named for the fixture and the sink under test, and what names it.
VARIABLES = {
	"BASEDREADER_PIPEWIRE_BIN": "PipeWire's bin directory, which the dev shell names",
	"BASEDREADER_WIREPLUMBER_BIN": "WirePlumber's bin directory, which the dev shell names",
	"BASEDREADER_PWSINK_LIB": "libpwsink.so",
	"BASEDREADER_PWMON": "pwmon",
}


def missingVariables() -> str | None:
	"""A sentence saying what to set, or None when everything the fixture needs is named."""
	missing = [f"{name} to {what}" for name, what in VARIABLES.items() if not os.environ.get(name)]
	if not missing:
		return None
	return (
		"Set "
		+ ", ".join(missing)
		+ ". The two built files come from engine/crates/pwsink, for example: nix develop . -c env "
		"BASEDREADER_PWSINK_LIB=$PWD/engine/target/release/libpwsink.so "
		"BASEDREADER_PWMON=$PWD/engine/target/release/pwmon tests/linux/rig python3 -m pytest "
		"tests/linux/test_pipewireSink.py"
	)


def requireVariables() -> None:
	"""Fail the test, rather than skip it, when the fixture cannot run."""
	missing = missingVariables()
	if missing:
		pytest.fail(missing, pytrace=False)


def _refuseOutsideTheRig() -> None:
	why = harness.whyNotInRig()
	if why is not None:
		raise RuntimeError(f"refusing to start PipeWire outside the rig ({why})")
	if os.path.exists("/dev/snd"):
		raise RuntimeError("refusing to start PipeWire where /dev/snd exists")


class PipewireFixture:
	"""A daemon from tests/linux/pipewire, and WirePlumber's policy profile beside it."""

	def __init__(self, workDir: Path, *, sinks: bool = True) -> None:
		requireVariables()
		self.workDir = workDir
		workDir.mkdir(parents=True, exist_ok=True)
		self.config = "pipewire.conf" if sinks else "pipewire-nosinks.conf"
		self.sinks = sinks
		self.pipewireBin = Path(os.environ["BASEDREADER_PIPEWIRE_BIN"])
		self.wireplumberBin = Path(os.environ["BASEDREADER_WIREPLUMBER_BIN"])
		self.socket = Path(os.environ["XDG_RUNTIME_DIR"]) / "pipewire-0"
		#: WirePlumber's own state, which is where it remembers streams.
		self.stateDir = workDir / "wireplumber-state"
		self.daemon: subprocess.Popen | None = None
		self.sessionManager: subprocess.Popen | None = None
		self._started: list[subprocess.Popen] = []
		self._monitors: list[Monitor] = []
		self._logs: list = []

	def __enter__(self) -> Self:
		self.start()
		return self

	def __exit__(self, *exc: object) -> None:
		self.stop()

	def _log(self, name: str):
		f = open(self.workDir / f"{name}.log", "a", encoding="utf-8")  # noqa: SIM115
		self._logs.append(f)
		return f

	def _spawn(self, args: list, name: str, env: dict | None = None) -> subprocess.Popen:
		_refuseOutsideTheRig()
		process = subprocess.Popen(
			[str(a) for a in args],
			stdin=subprocess.DEVNULL,
			stdout=self._log(name),
			stderr=subprocess.STDOUT,
			env={**os.environ, **(env or {})},
		)
		self._started.append(process)
		return process

	def tool(self, name: str, *args: str, check: bool = True) -> str:
		"""Runs one of PipeWire's own tools against the daemon and returns what it printed."""
		_refuseOutsideTheRig()
		done = subprocess.run(
			[str(self.pipewireBin / name), *args],
			capture_output=True,
			text=True,
			timeout=10,
			check=False,
		)
		if check and done.returncode != 0:
			raise RuntimeError(f"{name} {' '.join(args)} failed: {done.stdout}{done.stderr}")
		return done.stdout

	def start(self, *, daemon: bool = True, sessionManager: bool = True) -> None:
		if daemon:
			self.startDaemon()
			if sessionManager:
				self.startSessionManager()

	def startDaemon(self) -> None:
		# A daemon killed earlier leaves its socket behind, which would look like a new one.
		for stale in (self.socket, self.socket.with_name("pipewire-0.lock")):
			stale.unlink(missing_ok=True)
		self.daemon = self._spawn(
			[self.pipewireBin / "pipewire", "-c", self.config],
			"pipewire",
			# Only the daemon reads this directory: its configuration, and none of the host's drop-ins.
			{"PIPEWIRE_CONFIG_DIR": str(CONFIG_DIR)},
		)
		self._waitFor(lambda: self.socket.exists(), "the daemon's socket")
		self._waitFor(
			lambda: self.tool("pw-metadata", "-n", "settings", check=False) != "",
			"the daemon answering",
		)

	def startSessionManager(self, *, waitForDefault: bool | None = None) -> None:
		self.stateDir.mkdir(exist_ok=True)
		self.sessionManager = self._spawn(
			[self.wireplumberBin / "wireplumber", "--profile", "policy"],
			"wireplumber",
			{"GIO_USE_VFS": "local", "XDG_STATE_HOME": str(self.stateDir)},
		)
		if waitForDefault if waitForDefault is not None else self.sinks:
			self.waitForDefaultSink()

	def defaultSink(self) -> str | None:
		out = self.tool("pw-metadata", "-n", "default", check=False)
		match = re.search(r"key:'default\.audio\.sink' value:'([^']*)'", out)
		return json.loads(match.group(1)).get("name") if match else None

	def waitForDefaultSink(self, name: str | None = None) -> None:
		self._waitFor(
			lambda: (sink := self.defaultSink()) is not None and (name is None or sink == name),
			"WirePlumber choosing a default sink",
		)

	def _waitFor(self, condition: Callable[[], bool], what: str, timeout: float = 5) -> None:
		end = time.monotonic() + timeout
		while not condition():
			if time.monotonic() > end:
				raise TimeoutError(f"{what} took longer than {timeout} s")
			time.sleep(0.02)

	def setQuantum(self, frames: int) -> None:
		"""Forces the graph's quantum, or frees it with 0."""
		self.tool("pw-metadata", "-n", "settings", "0", "clock.force-quantum", str(frames))

	def setDefaultQuantum(self, frames: int) -> None:
		"""The quantum the graph runs at when no stream asks for another."""
		self.tool("pw-metadata", "-n", "settings", "0", "clock.quantum", str(frames))

	def nodes(self) -> list[dict]:
		return [o for o in json.loads(self.tool("pw-dump")) if o.get("type") == "PipeWire:Interface:Node"]

	def nodesNamed(self, name: str) -> list[dict]:
		return [n for n in self.nodes() if n["info"]["props"].get("node.name") == name]

	def createSink(self, name: str) -> None:
		self.tool(
			"pw-cli",
			"create-node",
			"adapter",
			f"{{ factory.name = support.null-audio-sink node.name = {name} node.description = {name} "
			"media.class = Audio/Sink audio.position = [ FL FR ] monitor.channel-volumes = true "
			"monitor.passthrough = true node.driver = false object.linger = true }",
		)
		self._waitFor(lambda: bool(self.nodesNamed(name)), f"the sink {name}")

	def removeSink(self, name: str) -> None:
		for node in self.nodesNamed(name):
			self.tool("pw-cli", "destroy", str(node["id"]))

	def killDaemon(self) -> None:
		"""SIGKILL to the daemon. WirePlumber exits by itself when it loses the daemon."""
		self._end(self.daemon, signal.SIGKILL)
		self.daemon = None
		if self.sessionManager is not None:
			try:
				self.sessionManager.wait(3)
			except subprocess.TimeoutExpired:
				self._end(self.sessionManager, signal.SIGKILL)
			self.sessionManager = None

	def freezeDaemon(self) -> None:
		self.daemon.send_signal(signal.SIGSTOP)

	def thawDaemon(self) -> None:
		self.daemon.send_signal(signal.SIGCONT)

	def restartDaemon(self) -> None:
		"""The daemon and WirePlumber started again, as after a crash or a fixaudio."""
		if self.daemon is not None:
			self.killDaemon()
		self.startDaemon()
		self.startSessionManager()

	def killSessionManager(self) -> None:
		self._end(self.sessionManager, signal.SIGKILL)
		self.sessionManager = None

	def monitor(self, sink: str = "rig-sink-a", *, passive: bool = False) -> "Monitor":
		"""Starts pwmon on a sink and waits until it is recording."""
		monitor = Monitor(self, sink, passive=passive)
		self._monitors.append(monitor)
		return monitor

	def childEnv(self, **extra: str) -> dict[str, str]:
		"""The environment a harness child needs to play to this daemon."""
		return {"BASEDREADER_AUDIO_SINK": "pipewire", **extra}

	def _end(self, process: subprocess.Popen | None, how: int = signal.SIGTERM) -> None:
		if process is None or process.poll() is not None:
			return
		# A frozen process cannot act on SIGTERM.
		process.send_signal(signal.SIGCONT)
		process.send_signal(how)
		try:
			process.wait(3)
		except subprocess.TimeoutExpired:
			process.kill()
			process.wait(3)

	def stop(self) -> None:
		for monitor in self._monitors:
			monitor.close()
		for process in reversed(self._started):
			self._end(process)
		for f in self._logs:
			f.close()


class Monitor:
	"""pwmon recording one sink's monitor: its events as they come, and the recording itself."""

	def __init__(self, fixture: PipewireFixture, sink: str, *, passive: bool = False) -> None:
		_refuseOutsideTheRig()
		self.sink = sink
		self.raw = fixture.workDir / f"monitor-{sink}-{time.monotonic_ns()}.raw"
		args = [os.environ["BASEDREADER_PWMON"], sink, "--raw", str(self.raw)]
		if passive:
			args.append("--passive")
		self.process = subprocess.Popen(
			args,
			stdin=subprocess.DEVNULL,
			stdout=subprocess.PIPE,
			stderr=fixture._log(f"pwmon-{sink}"),
			text=True,
		)
		self.events: list[dict] = []
		self._changed = threading.Condition()
		self._reader = threading.Thread(target=self._read, name=f"pwmon {sink}", daemon=True)
		self._reader.start()
		self.waitFor("state", lambda e: e["new"] == "Streaming", timeout=5)

	def _read(self) -> None:
		for line in self.process.stdout:
			with self._changed:
				self.events.append(json.loads(line))
				self._changed.notify_all()

	def waitFor(
		self,
		kind: str,
		condition: Callable[[dict], bool] = lambda e: True,
		*,
		timeout: float = 3,
	) -> dict:
		"""The first event of a kind that meets the condition, waiting for it if need be."""
		end = time.monotonic() + timeout
		with self._changed:
			while True:
				for event in self.events:
					if event["event"] == kind and condition(event):
						return event
				left = end - time.monotonic()
				if left <= 0 or self.process.poll() is not None:
					raise TimeoutError(f"no {kind} event from pwmon on {self.sink} within {timeout} s")
				self._changed.wait(left)

	def soundAfter(self, ns: int, timeout: float = 3) -> int:
		"""The graph time of the first sound at or after ns."""
		return self.waitFor("sound", lambda e: e["graph_ns"] >= ns, timeout=timeout)["graph_ns"]

	def silenceAfter(self, ns: int, timeout: float = 3) -> int:
		"""The graph time just after the last sounding frame, for the first silence at or after ns."""
		return self.waitFor("silence", lambda e: e["graph_ns"] >= ns, timeout=timeout)["graph_ns"]

	def eventsOf(self, kind: str) -> list[dict]:
		with self._changed:
			return [e for e in self.events if e["event"] == kind]

	def samples(self, fromNs: int, toNs: int) -> tuple[array, array]:
		"""The left and right samples recorded from one graph time to another."""
		start = self.waitFor("start")["graph_ns"]
		first = max(0, (fromNs - start) * RATE // 1_000_000_000)
		last = max(first, (toNs - start) * RATE // 1_000_000_000)
		with open(self.raw, "rb") as f:
			f.seek(first * 8)
			data = array("f")
			data.frombytes(f.read((last - first) * 8))
		return data[0::2], data[1::2]

	def close(self) -> None:
		if self.process.poll() is None:
			self.process.terminate()
			try:
				self.process.wait(3)
			except subprocess.TimeoutExpired:
				self.process.kill()
				self.process.wait(3)
		self._reader.join(3)


@pytest.fixture(name="pipewire")
def runningPipewire(tmp_path: Path) -> Iterator[PipewireFixture]:
	"""A running daemon with WirePlumber and both sinks, stopped after the test. Imported into a
	test module, it gives tests that take a parameter named pipewire a PipewireFixture."""
	with PipewireFixture(tmp_path / "pipewire") as fixture:
		yield fixture
