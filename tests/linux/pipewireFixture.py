"""A private PipeWire daemon, its session manager, and a recorder of what a sink plays, for tests
that run in tests/linux/rig.

The daemon has no devices. Two null sinks, rig-sink-a and rig-sink-b, follow a dummy driver, and
WirePlumber links streams to the default one as it would on a desktop, so nothing here can be
heard. The fixture starts nothing outside the rig or where the sandbox has sound devices, and it
signals only processes it started itself.

	with PipewireFixture(tmp_path / "pipewire") as pipewire:
		monitor = pipewire.monitor()
		done = harness.runChild(tmp_path, code, env=pipewire.childEnv())
		assertCleanThroughPipewire(done)

or, as a pytest fixture, which tests/linux/conftest.py registers, take a parameter named pipewire.
"""

import json
import os
import re
import signal
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
CONFIG_DIR = HERE / "pipewire"
#: The graph's rate in the daemon's configuration, and the rate pwmon records at.
RATE = 48000

#: What has to be named for the fixture and the sink under test. The dev shell names each.
VARIABLES = {
	"BASEDREADER_PIPEWIRE_BIN": "PipeWire's bin directory",
	"BASEDREADER_WIREPLUMBER_BIN": "WirePlumber's bin directory",
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
		+ ". The dev shell names them all: nix develop . -c tests/linux/rig python3 -m pytest "
		"tests/linux/test_pipewireSink.py"
	)


def requireVariables() -> None:
	"""Fail the test, rather than skip it, when the fixture cannot run."""
	missing = missingVariables()
	if missing:
		pytest.fail(missing, pytrace=False)


def assertCleanThroughPipewire(done: harness.ChildResult, *, ignoring: Collection[str] = ()) -> None:
	"""A child that played through the PipeWire sink was clean, apart from the checks in ignoring.

	Playing maps PipeWire's client library and the client modules it loads, which the harness's sound
	check counts. That check is replaced here by a narrower one: the only sound libraries mapped are
	libpipewire-0.3 and its modules, from the PipeWire package the fixture's daemon comes from, and
	nothing was loaded as a sound library from Python, opened as a sound device or connected to as a
	sound server. Anything else of that package, such as its JACK or PulseAudio libraries, still counts.
	"""
	done.assertClean(ignoring={"audio", *ignoring})
	pipewireLib = Path(os.path.realpath(os.environ["BASEDREADER_PIPEWIRE_BIN"])).parent / "lib"
	unexpected = []
	for what in done.audio:
		how, _, mapped = what.partition(" ")
		path = Path(mapped)
		if how == "mapped" and (
			(path.parent == pipewireLib and path.name.startswith("libpipewire-0.3.so"))
			or (path.parent == pipewireLib / "pipewire-0.3" and path.name.startswith("libpipewire-module-"))
		):
			continue
		unexpected.append(what)
	assert not unexpected, "sound reached other than through PipeWire's client library:\n" + "\n".join(
		unexpected,
	)


#: Brings speech up as core.main does with the synthesizer SYNTH configured, speaks TEXT through
#: speech.speakMessage and the speech manager, and returns when the synthesizer reports it done.
SPEAK_MESSAGE = """
import time

import core
# core.main's first import after config; entering NVDA's import cycle anywhere else fails.
import NVDAHelper
from linuxPlatform import synthDriverPaths

synthDriverPaths.install()
import characterProcessing
import config
import languageHandler
import nvwave
import queueHandler
import speech
import speechDictHandler
import synthDriverHandler

languageHandler.setLanguage("en")
nvwave.initialize()
speechDictHandler.initialize()
characterProcessing.initialize()
config.conf["speech"]["synth"] = SYNTH
speech.initialize()
synth = synthDriverHandler.getSynth()
assert synth.name == SYNTH, synth.name


class Done:
	# extensionPoints holds handlers weakly, so this is a bound method of an object kept alive here.
	def __init__(self):
		self.at = []
		synthDriverHandler.synthDoneSpeaking.register(self.onDone)

	def onDone(self, synth):
		self.at.append(time.monotonic_ns())


done = Done()
asked = time.monotonic_ns()
speech.speakMessage(TEXT)
end = time.monotonic() + 30
while not done.at and time.monotonic() < end:
	queueHandler.pumpAll()
	time.sleep(0.002)
synthDriverHandler.setSynth(None)
result = {"asked": asked, "done": done.at}
"""


def assertSpeechHeard(fixture: "PipewireFixture", tmpPath: Path, synth: str, text: str) -> None:
	"""The synthesizer, loaded by NVDA and given text through speech.speakMessage, is heard at the
	monitor through the PipeWire sink, and reports the end of the utterance only after the last of it
	was heard."""
	monitor = fixture.monitor()
	done = harness.runChild(
		tmpPath,
		f"SYNTH = {synth!r}\nTEXT = {text!r}\n" + SPEAK_MESSAGE,
		env=fixture.childEnv(),
		timeout=120,
	)
	assertCleanThroughPipewire(done)
	asked = done.value["asked"]
	assert len(done.value["done"]) == 1, done.value
	finished = done.value["done"][0]
	first = monitor.soundAfter(asked)
	sounds = [e["graph_ns"] for e in monitor.eventsOf("sound") if e["graph_ns"] >= asked]
	last = monitor.silenceAfter(sounds[-1])
	assert (first - asked) / 1e6 < 1000, "the first sound came more than a second after asking"
	assert (last - first) / 1e9 > 1.0, "less than a second of speech was heard"
	assert finished >= last - 1_000_000, "the end of the utterance was reported before it was heard"
	assert not [ns for ns in sounds if ns > finished], "sound was heard after the utterance ended"
	left, right = monitor.samples(first, last)
	assert max(abs(s) for s in left) > 0.1 and max(abs(s) for s in right) > 0.1, "the speech was barely heard"


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
	"""A running daemon with WirePlumber and both sinks, stopped after the test, for every test that
	takes a parameter named pipewire."""
	with PipewireFixture(tmp_path / "pipewire") as fixture:
		yield fixture
