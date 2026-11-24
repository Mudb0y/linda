"""A private PulseAudio daemon and a recorder of what a sink plays, for tests that run in tests/linux/rig.

The daemon has no physical sound devices. Two null sinks, rig-sink-a and rig-sink-b, consume audio
in real-time and discard it, with their monitors recorded by parec or the fixture monitor.

	with PulseFixture(tmp_path / "pulse") as pulse:
		monitor = pulse.monitor()
		done = harness.runChild(tmp_path, code, env=pulse.childEnv())
		assertCleanThroughPulse(done)
"""

import os
import shutil
import subprocess
import threading
import time
from array import array
from collections.abc import Callable, Collection, Iterator
from pathlib import Path
from typing import Self

import harness
import pytest

HERE = Path(__file__).resolve().parent
CONFIG_TEMPLATE = HERE / "pulse" / "default.pa.in"
RATE = 48000


def _findPulseBin() -> str:
	if "LINDA_PULSEAUDIO_BIN" in os.environ:
		p = os.environ["LINDA_PULSEAUDIO_BIN"]
		if os.path.isdir(p):
			candidate = os.path.join(p, "pulseaudio")
			if os.path.exists(candidate):
				return candidate
		elif os.path.exists(p):
			return p
	found = shutil.which("pulseaudio")
	if found:
		return found
	for candidate in ["/usr/bin/pulseaudio", "/bin/pulseaudio", "/usr/local/bin/pulseaudio"]:
		if os.path.exists(candidate) and os.access(candidate, os.X_OK):
			return candidate
	return "pulseaudio"


def _findParecBin(pulseBin: str | None = None) -> str:
	if "LINDA_PAREC_BIN" in os.environ:
		return os.environ["LINDA_PAREC_BIN"]
	if pulseBin:
		parecNeighbor = Path(pulseBin).parent / "parec"
		if parecNeighbor.exists() and os.access(parecNeighbor, os.X_OK):
			return str(parecNeighbor)
	found = shutil.which("parec")
	if found:
		return found
	for candidate in ["/usr/bin/parec", "/bin/parec", "/usr/local/bin/parec"]:
		if os.path.exists(candidate) and os.access(candidate, os.X_OK):
			return candidate
	return "parec"


def _refuseOutsideTheRig() -> None:
	why = harness.whyNotInRig()
	if why is not None:
		raise RuntimeError(f"refusing to start PulseAudio outside the rig ({why})")
	if os.path.exists("/dev/snd"):
		raise RuntimeError("refusing to start PulseAudio where /dev/snd exists")


def assertCleanThroughPulse(done: harness.ChildResult, *, ignoring: Collection[str] = ()) -> None:
	"""A child that played through the PulseAudio sink was clean, apart from mapped PulseAudio libraries and socket."""
	done.assertClean(ignoring={"audio", *ignoring})
	unexpected = []
	for what in done.audio:
		how, _, target = what.partition(" ")
		targetLower = target.lower()
		name = os.path.basename(target).lower()
		if how == "mapped" and (name.startswith("libpulse") or "pulse" in name):
			continue
		if how == "loaded" and ("pulse" in name or "pulse" in targetLower):
			continue
		if how == "connected" and ("pulse" in targetLower):
			continue
		unexpected.append(what)
	assert not unexpected, "sound reached other than through PulseAudio's client library:\n" + "\n".join(
		unexpected,
	)


class PulseFixture:
	"""A private PulseAudio daemon started in the sandbox with two null sinks."""

	def __init__(self, workDir: Path) -> None:
		self.workDir = workDir
		workDir.mkdir(parents=True, exist_ok=True)
		runtimeDir = os.environ.get("XDG_RUNTIME_DIR", str(workDir))
		self.socketDir = Path(runtimeDir) / "pulse"
		self.socketDir.mkdir(parents=True, exist_ok=True)
		self.socket = self.socketDir / "native"
		self.configFile = workDir / "default.pa"
		self.daemon: subprocess.Popen | None = None
		self._pulseBin: str = ""
		self._monitors: list[PulseMonitor] = []
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

	def start(self) -> None:
		_refuseOutsideTheRig()
		self.socket.unlink(missing_ok=True)

		pulseBin = _findPulseBin()
		if not (os.path.exists(pulseBin) or shutil.which(pulseBin)):
			pytest.skip(f"PulseAudio daemon binary ({pulseBin}) not found")
		self._pulseBin = pulseBin

		# Render config file with socket path
		configText = CONFIG_TEMPLATE.read_text(encoding="utf-8")
		configText = configText.replace("@SOCKET@", str(self.socket))
		self.configFile.write_text(configText, encoding="utf-8")

		env = dict(os.environ)
		env["PULSE_RUNTIME_PATH"] = str(self.socketDir)

		self.daemon = subprocess.Popen(
			[
				pulseBin,
				"-n",
				"-F",
				str(self.configFile),
				"--daemonize=no",
				"--exit-idle-time=-1",
				"--use-pid-file=no",
				"--disallow-exit",
				"--disallow-module-loading",
				"--realtime=no",
				"--high-priority=no",
			],
			stdin=subprocess.DEVNULL,
			stdout=self._log("pulseaudio"),
			stderr=subprocess.STDOUT,
			env=env,
		)

		# Wait for socket to become active
		self._waitFor(lambda: self.socket.exists(), "PulseAudio daemon socket")
		time.sleep(0.1)

	def _waitFor(self, condition: Callable[[], bool], what: str, timeout: float = 5.0) -> None:
		end = time.monotonic() + timeout
		while not condition():
			if time.monotonic() > end:
				raise TimeoutError(f"{what} took longer than {timeout} s")
			time.sleep(0.02)

	def childEnv(self, **extra: str) -> dict[str, str]:
		"""The environment a child needs to play to this private PulseAudio daemon."""
		return {
			"LINDA_AUDIO_SINK": "pulse",
			"PULSE_SERVER": f"unix:{self.socket}",
			**extra,
		}

	def monitor(self, sink: str = "rig-sink-a") -> "PulseMonitor":
		monitor = PulseMonitor(self, sink)
		self._monitors.append(monitor)
		return monitor

	def killDaemon(self) -> None:
		"""SIGKILL to the daemon."""
		if self.daemon is not None and self.daemon.poll() is None:
			self.daemon.kill()
			self.daemon.wait(3)
		self.daemon = None
		self.socket.unlink(missing_ok=True)

	def restartDaemon(self) -> None:
		"""The daemon started again, as after a crash or pulseaudio -k."""
		if self.daemon is not None:
			self.killDaemon()
		self.start()

	def stop(self) -> None:
		for m in self._monitors:
			m.close()
		if self.daemon is not None and self.daemon.poll() is None:
			self.daemon.terminate()
			try:
				self.daemon.wait(3)
			except subprocess.TimeoutExpired:
				self.daemon.kill()
				self.daemon.wait(3)
		self.daemon = None
		for f in self._logs:
			f.close()


class PulseMonitor:
	"""Records audio from a null sink's monitor using parec and tracks audio/silence events."""

	def __init__(self, fixture: PulseFixture, sink: str) -> None:
		_refuseOutsideTheRig()
		self.sink = sink
		self.source = f"{sink}.monitor"
		self.raw = fixture.workDir / f"monitor-{sink}-{time.monotonic_ns()}.raw"
		self.events: list[dict] = []
		self._changed = threading.Condition()
		self._stopping = threading.Event()

		parecBin = _findParecBin(fixture._pulseBin)
		env = dict(os.environ)
		env["PULSE_SERVER"] = f"unix:{fixture.socket}"

		self.process = subprocess.Popen(
			[
				parecBin,
				"-d",
				self.source,
				"--raw",
				f"--rate={RATE}",
				"--channels=2",
				"--format=float32le",
			],
			stdin=subprocess.DEVNULL,
			stdout=subprocess.PIPE,
			stderr=fixture._log(f"parec-{sink}"),
			env=env,
		)

		self._rawFile = open(self.raw, "wb")  # noqa: SIM115
		self._reader = threading.Thread(target=self._readStream, name=f"parec {sink}", daemon=True)
		self._reader.start()

		# Record start event
		startNs = time.monotonic_ns()
		with self._changed:
			self.events.append({"event": "start", "graph_ns": startNs})
			self._changed.notify_all()

	def _readStream(self) -> None:
		chunkFrames = 512
		chunkBytes = chunkFrames * 8  # 2 channels * 4 bytes
		sounding = False
		threshold = 0.001
		while not self._stopping.is_set():
			data = self.process.stdout.read(chunkBytes)
			now = time.monotonic_ns()
			if not data:
				break
			self._rawFile.write(data)
			samples = array("f")
			samples.frombytes(data)

			# Detect sound / silence transitions
			isLoud = any(abs(s) > threshold for s in samples)
			if isLoud and not sounding:
				sounding = True
				with self._changed:
					self.events.append({"event": "sound", "graph_ns": now})
					self._changed.notify_all()
			elif not isLoud and sounding:
				sounding = False
				with self._changed:
					self.events.append({"event": "silence", "graph_ns": now})
					self._changed.notify_all()

	def waitFor(
		self,
		kind: str,
		condition: Callable[[dict], bool] = lambda e: True,
		*,
		timeout: float = 3.0,
	) -> dict:
		end = time.monotonic() + timeout
		with self._changed:
			while True:
				for e in self.events:
					if e["event"] == kind and condition(e):
						return e
				left = end - time.monotonic()
				if left <= 0 or self.process.poll() is not None:
					raise TimeoutError(f"no {kind} event from pulse monitor within {timeout} s")
				self._changed.wait(left)

	def soundAfter(self, ns: int, timeout: float = 3.0) -> int:
		return self.waitFor("sound", lambda e: e["graph_ns"] >= ns, timeout=timeout)["graph_ns"]

	def silenceAfter(self, ns: int, timeout: float = 3.0) -> int:
		return self.waitFor("silence", lambda e: e["graph_ns"] >= ns, timeout=timeout)["graph_ns"]

	def eventsOf(self, kind: str) -> list[dict]:
		with self._changed:
			return [e for e in self.events if e["event"] == kind]

	def samples(self, fromNs: int, toNs: int) -> tuple[array, array]:
		self._rawFile.flush()
		start = self.waitFor("start")["graph_ns"]
		first = max(0, (fromNs - start) * RATE // 1_000_000_000)
		last = max(first, (toNs - start) * RATE // 1_000_000_000)
		with open(self.raw, "rb") as f:
			f.seek(first * 8)
			data = array("f")
			data.frombytes(f.read((last - first) * 8))
		return data[0::2], data[1::2]

	def close(self) -> None:
		self._stopping.set()
		if self.process.poll() is None:
			self.process.terminate()
			try:
				self.process.wait(2)
			except subprocess.TimeoutExpired:
				self.process.kill()
		self._reader.join(2)
		self._rawFile.close()


@pytest.fixture(name="pulse")
def runningPulse(tmp_path: Path) -> Iterator[PulseFixture]:
	with PulseFixture(tmp_path / "pulse") as fixture:
		yield fixture
