"""The PipeWire sink, heard through a private daemon in the rig.

	nix develop . -c tests/linux/rig python3 -m pytest tests/linux/test_pipewireSink.py

The dev shell names the library the flake builds. To test a build of engine/crates/pwsink instead,
name it and its pwmon with BASEDREADER_PWSINK_LIB and BASEDREADER_PWMON, which the rig passes in.
BENCH_PWSINK_ROUNDS sets how many stops and starts are timed at each quantum and rate, 30 by
default.

Every case plays from a fresh NVDA process, through nvwave and the wasapi replacement or straight
into the sink, to the null sinks of tests/linux/pipewireFixture.py, and checks what their monitor
recorded: levels, timing, and what happens when PipeWire stops consuming or goes away. The sinks
follow the graph's driver, so the monitor hears each cycle in that cycle and a latency measured on
it is the sink's own.
"""

import json
import math
import os
import re
import signal
import statistics
import subprocess
import textwrap
import threading
import time
import wave
from array import array
from collections.abc import Callable
from pathlib import Path
from typing import Self

import harness
import pytest
from pipewireFixture import RATE, PipewireFixture, assertCleanThroughPipewire

CRATE = harness.ROOT / "engine" / "crates" / "pwsink"
WAVES = harness.SOURCE / "waves"
MS = 1_000_000
#: Stops and starts timed at each quantum and rate.
STOP_ROUNDS = int(os.environ.get("BENCH_PWSINK_ROUNDS", "30"))

#: Run in every child first: NVDA's audio as core.main brings it up, the sink, and helpers.
PRELUDE = '''
import array
import ctypes
import gc
import json
import math
import os
import random
import threading
import time

import NVDAHelper  # noqa: F401
import nvwave
import wasapi
from linuxPlatform import audioOutput
from linuxPlatform.audioOutput import StreamFormat

nvwave.initialize()
sink = audioOutput.getSink()
assert sink.kind == "pipewire", sink.kind
lib = sink.lib
mono = time.monotonic_ns
PARAMS = json.loads(os.environ.get("TEST_PARAMS", "{}"))
STEPS = os.environ.get("TEST_STEPS", "")


def ask(name, timeout=30):
	"""Has the test do something to the daemon, waits until it has, and returns what it said."""
	with open(os.path.join(STEPS, "ask-" + name), "w") as f:
		f.write(str(mono()))
	done = os.path.join(STEPS, "done-" + name)
	end = time.monotonic() + timeout
	while not os.path.exists(done):
		if time.monotonic() > end:
			raise TimeoutError(name)
		time.sleep(0.002)
	with open(done) as f:
		return json.load(f)


def encode(fmt, samples):
	bits = fmt.bitsPerSample
	if fmt.formatTag == 3:
		return array.array("f" if bits == 32 else "d", samples).tobytes()
	if bits == 8:
		return bytes(round(128 + s * 127) for s in samples)
	if bits == 24:
		return b"".join(round(s * 8388607).to_bytes(3, "little", signed=True) for s in samples)
	return array.array("h" if bits == 16 else "i", [round(s * (2 ** (bits - 1) - 1)) for s in samples]).tobytes()


def tone(fmt, seconds, *, amplitude=0.25, levels=None, hz=440.0):
	"""A sine in the format, each channel at its level of the amplitude."""
	levels = levels or [1.0] * fmt.channels
	step = 2 * math.pi * hz / fmt.samplesPerSec
	frames = int(fmt.samplesPerSec * seconds)
	return encode(fmt, [amplitude * math.sin(step * i) * level for i in range(frames) for level in levels])


def waitPlayed(stream, frames, timeout=5):
	end = time.monotonic() + timeout
	while stream.playedFrames() < frames:
		assert time.monotonic() < end, "the stream never finished"
		time.sleep(0.003)


SPEECH = StreamFormat(1, 1, 22050, 16)


def speechPlayer():
	return nvwave.WavePlayer(1, 22050, 16, wantDucking=False, purpose=nvwave.AudioPurpose.SOUNDS)
'''


def runPipewireChild(
	tmpPath: Path,
	code: str,
	env: dict[str, str],
	*,
	params: dict | None = None,
	timeout: float = 90,
) -> object:
	"""Runs code after PRELUDE in a harness child, requires it to have been clean, with PipeWire's
	client the only sound library it mapped, and returns its result."""
	done = harness.runChild(
		tmpPath,
		PRELUDE + textwrap.dedent(code),
		env={**env, "TEST_PARAMS": json.dumps(params or {})},
		timeout=timeout,
	)
	assertCleanThroughPipewire(done)
	return done.value


class Steps:
	"""Does to the daemon what a child asks for, by name, while harness.runChild waits for the child."""

	def __init__(self, directory: Path, actions: dict[str, Callable[[], object]]) -> None:
		self.directory = directory
		directory.mkdir(parents=True, exist_ok=True)
		self.actions = actions
		self.asked: dict[str, int] = {}
		self.done: dict[str, int] = {}
		self.errors: list[str] = []
		self._stopping = threading.Event()
		self._thread = threading.Thread(target=self._run, name="steps", daemon=True)

	def env(self) -> dict[str, str]:
		return {"TEST_STEPS": str(self.directory)}

	def __enter__(self) -> Self:
		self._thread.start()
		return self

	def __exit__(self, *exc: object) -> None:
		self._stopping.set()
		self._thread.join(10)
		assert not self.errors, self.errors

	def _run(self) -> None:
		while not self._stopping.is_set():
			for name, action in self.actions.items():
				request = self.directory / f"ask-{name}"
				if name in self.done or not request.exists():
					continue
				self.asked[name] = int(request.read_text() or 0)
				try:
					value = action()
				except Exception as e:  # noqa: BLE001
					# Reported when the test ends; the child is told and carries on.
					self.errors.append(f"{name}: {e!r}")
					value = {"error": repr(e)}
				self.done[name] = time.monotonic_ns()
				staged = self.directory / f".done-{name}"
				staged.write_text(json.dumps({"ns": self.done[name], "value": value}))
				staged.rename(self.directory / f"done-{name}")
			time.sleep(0.002)


def rms(samples: array) -> float:
	return math.sqrt(sum(x * x for x in samples) / max(1, len(samples)))


def energy(samples) -> float:
	return sum(x * x for x in samples)


def decibels(ratio: float) -> float:
	return 20 * math.log10(ratio) if ratio > 0 else -math.inf


def crossingsPerSecond(samples: array) -> float:
	sign = 0
	crossings = 0
	for x in samples:
		if x == 0:
			continue
		s = 1 if x > 0 else -1
		if sign and s != sign:
			crossings += 1
		sign = s
	return crossings / (len(samples) / RATE)


def summary(values: list[float]) -> dict[str, float]:
	ordered = sorted(values)
	p95 = ordered[min(len(ordered) - 1, math.ceil(0.95 * len(ordered)) - 1)]
	return {
		"n": len(ordered),
		"median": round(statistics.median(ordered), 2),
		"p95": round(p95, 2),
		"max": round(ordered[-1], 2),
	}


def quantumMs(frames: int) -> float:
	return frames / RATE * 1000


def ourNodes(fixture: PipewireFixture) -> list[dict]:
	return [n for n in fixture.nodes() if n["info"]["props"].get("application.id") == "basedreader"]


def report(name: str, value: object) -> None:
	"""Measurements, printed for pytest -s and kept with the test's output otherwise."""
	print(f"{name}: {json.dumps(value)}")


FORMATS = [(1, 8), (1, 16), (1, 24), (1, 32), (3, 32), (3, 64)]
RATES = [8000, 11025, 22050, 44100, 48000]


def test_everyFormatIsHeardAtItsLevel(pipewire: PipewireFixture, tmp_path: Path):
	"""Each sample format at each rate, mono and stereo, reaches the sink at the level written and
	the pitch written: a format declared wrongly plays as loud noise at the wrong pitch."""
	pipewire.setQuantum(512)
	monitor = pipewire.monitor()
	cases = runPipewireChild(
		tmp_path,
		"""
		cases = []
		for tag, bits in PARAMS["formats"]:
			for rate in PARAMS["rates"]:
				for channels in (1, 2):
					fmt = StreamFormat(tag, channels, rate, bits)
					levels = [1.0] if channels == 1 else [1.0, 0.5]
					data = tone(fmt, 0.25, levels=levels)
					stream = sink.openStream("", fmt, lambda: None)
					start = mono()
					stream.write(data)
					stream.start()
					waitPlayed(stream, len(data) // fmt.blockAlign)
					stream.stop()
					stream.close()
					cases.append(dict(tag=tag, bits=bits, rate=rate, levels=levels, start=start))
					time.sleep(0.06)
		result = cases
		""",
		pipewire.childEnv(),
		params={"formats": FORMATS, "rates": RATES},
	)
	failures = []
	for case in cases:
		label = f"format {case['tag']}/{case['bits']} at {case['rate']} Hz, {len(case['levels'])} channel(s)"
		sound = monitor.soundAfter(case["start"])
		silence = monitor.silenceAfter(sound)
		left, right = monitor.samples(sound + 40 * MS, silence - 40 * MS)
		for samples, level in ((left, case["levels"][0]), (right, case["levels"][-1])):
			expected = 0.25 * level
			off = decibels(rms(samples) / (expected / math.sqrt(2)))
			peak = max(abs(x) for x in samples)
			pitch = crossingsPerSecond(samples) / 2
			if abs(off) > 0.5 or peak > expected * 1.1 or abs(pitch - 440) > 440 * 0.02:
				failures.append(f"{label}: {off:+.2f} dB, peak {peak:.3f}, {pitch:.0f} Hz")
	assert len(cases) == len(FORMATS) * len(RATES) * 2
	assert not failures, failures


def test_nothingOfOneSoundIsHeldBackForTheNext(pipewire: PipewireFixture, tmp_path: Path):
	"""A sound ends when its last frame is heard, and none of it is heard at the start of the next
	sound on the same stream, whether it ran out, was stopped, or its player was closed and the
	stream reused from the pool.

	PipeWire's converter keeps input that does not fill a cycle until more comes, so a sink that
	stops handing over buffers once its ring is empty plays the end of each sound at the start of
	the next. The loud sounds here end at a peak, partway through a cycle; the quiet ones that
	follow would show any of it."""
	pipewire.setQuantum(512)
	monitor = pipewire.monitor()
	value = runPipewireChild(
		tmp_path,
		"""
		loud = tone(SPEECH, 0.1017, amplitude=0.5, hz=1000.0)
		quiet = tone(SPEECH, 0.2, amplitude=0.02)
		marks = []

		def play(player, data, stopAfter=None):
			start = mono()
			if stopAfter is None:
				player.feed(data)
				player.idle()
			else:
				player.feed(data)
				time.sleep(stopAfter)
				player.stop()
			return start

		player = speechPlayer()
		for i in range(4):
			loudStart = play(player, loud)
			time.sleep(0.1)
			marks.append(dict(how="ran out", loud=loudStart, quiet=play(player, quiet)))
			time.sleep(0.1)
		for i in range(4):
			loudStart = play(player, tone(SPEECH, 0.4, amplitude=0.5, hz=1000.0), stopAfter=0.1 + i * 0.013)
			time.sleep(0.1)
			marks.append(dict(how="stopped", loud=loudStart, quiet=play(player, quiet)))
			time.sleep(0.1)
		for i in range(4):
			loudStart = play(player, loud)
			del player
			gc.collect()
			time.sleep(0.1)
			player = speechPlayer()
			marks.append(dict(how="pooled", loud=loudStart, quiet=play(player, quiet)))
			time.sleep(0.1)
		del player
		gc.collect()
		result = dict(marks=marks, loudFrames=len(loud) // 2)
		""",
		pipewire.childEnv(),
	)
	failures = []
	loudSeconds = value["loudFrames"] / 22050
	durations = []
	for mark in value["marks"]:
		quietStart = monitor.soundAfter(mark["quiet"])
		left, _ = monitor.samples(quietStart, quietStart + 30 * MS)
		peak = max(abs(x) for x in left)
		if peak > 0.02 * 1.2:
			failures.append(f"{mark['how']}: the next sound began at {peak:.3f}")
		if mark["how"] != "stopped":
			loudStart = monitor.soundAfter(mark["loud"])
			loudEnd = monitor.silenceAfter(loudStart)
			durations.append(round((loudEnd - loudStart) / MS - loudSeconds * 1000, 2))
	report("loud sounds' heard length against their own, ms", durations)
	assert not failures, failures
	# The resampler's filter rings for a fraction of a millisecond after a sound cut off at its peak.
	assert all(-0.5 < d < 1.5 for d in durations), durations


def test_channelVolumesAndTheProcessGainApplyInTheSinkOnly(pipewire: PipewireFixture, tmp_path: Path):
	"""Channel volumes and the process gain scale what is heard, while PipeWire's own volumes on
	Basedreader's node stay at 1, so that nothing is left for WirePlumber to remember."""
	monitor = pipewire.monitor()
	nodesBefore = {
		n["info"]["props"]["node.name"]: n["info"]["params"].get("Props") for n in pipewire.nodes()
	}

	def inspect() -> list:
		return [n["info"]["params"].get("Props") for n in ourNodes(pipewire)]

	with Steps(tmp_path / "steps", {"inspect": inspect}) as steps:
		value = runPipewireChild(
			tmp_path,
			"""
			fmt = StreamFormat(1, 2, 48000, 16)
			stream = sink.openStream("", fmt, lambda: None)
			stream.setChannelVolume(0, 0.5)
			stream.setChannelVolume(1, 0.25)
			sink.setGain(0.5)
			data = tone(fmt, 0.8)
			start = mono()
			stream.write(data)
			stream.start()
			time.sleep(0.3)
			props = ask("inspect")["value"]
			waitPlayed(stream, len(data) // fmt.blockAlign)
			stream.close()
			result = dict(start=start, props=props, delivered=sink.deliveredFrames())
			""",
			{**pipewire.childEnv(), **steps.env()},
		)
	assert value["delivered"] >= 48000 * 0.8, f"the graph took {value['delivered']} of 38400 frames"
	sound = monitor.soundAfter(value["start"])
	silence = monitor.silenceAfter(sound)
	left, right = monitor.samples(sound + 50 * MS, silence - 50 * MS)
	expected = 0.25 / math.sqrt(2)
	assert abs(decibels(rms(left) / (expected * 0.5 * 0.5))) < 0.5
	assert abs(decibels(rms(right) / (expected * 0.25 * 0.5))) < 0.5
	assert value["props"], "Basedreader's node was not found"
	for props in value["props"]:
		(params,) = props
		assert params["volume"] == 1.0
		assert params["mute"] is False
		assert params["channelVolumes"] == [1.0] * len(params["channelVolumes"])
	nodesAfter = {n["info"]["props"]["node.name"]: n["info"]["params"].get("Props") for n in pipewire.nodes()}
	for name in ("rig-sink-a", "rig-sink-b"):
		assert nodesAfter[name] == nodesBefore[name], name


EXPECTED_PROPERTIES = {
	"application.name": "Basedreader",
	"application.id": "basedreader",
	"media.name": "Basedreader",
	"node.name": "basedreader",
	"node.description": "Basedreader",
	"media.type": "Audio",
	"media.category": "Playback",
	"media.role": "Accessibility",
	# pw-dump writes these two as JSON booleans.
	"state.restore-props": False,
	"state.restore-target": False,
	"node.latency": "512/48000",
}


def test_streamsCarryBasedreadersPropertiesAndNeverForceTheQuantum(pipewire: PipewireFixture, tmp_path: Path):
	"""The node is named as Basedreader's own, asks for a quantum of 512 without forcing it, and
	plays to a configured device by target.object.

	A forced quantum stays as forced while Basedreader plays. A free one, 1024 here as in PipeWire's
	own configuration, comes down to the 512 Basedreader asks for while its stream is active, which
	halves the time to silence, and goes back once the stream is deactivated."""
	pipewire.setQuantum(256)
	monitor = pipewire.monitor()
	monitorB = pipewire.monitor("rig-sink-b")
	seen: dict[str, list] = {}

	def nodes(step: str) -> Callable[[], None]:
		def record() -> None:
			seen[step] = [n["info"]["props"] for n in ourNodes(pipewire)]

		return record

	def free() -> None:
		pipewire.setQuantum(0)
		pipewire.setDefaultQuantum(1024)
		monitor.waitFor("quantum", lambda e: e["frames"] == 1024, timeout=5)

	actions = {"forced": nodes("forced"), "free": free, "targeted": nodes("targeted")}
	with Steps(tmp_path / "steps", actions) as steps:
		value = runPipewireChild(
			tmp_path,
			"""
			player = speechPlayer()
			marks = {"forcedStart": mono()}
			player.feed(tone(SPEECH, 0.5))
			ask("forced")
			player.idle()
			sink.stopKeepingAwake()
			time.sleep(0.3)
			ask("free")
			marks["freeStart"] = mono()
			player.feed(tone(SPEECH, 0.5))
			player.idle()
			marks["freeEnd"] = mono()
			sink.stopKeepingAwake()
			time.sleep(0.5)
			marks["deactivated"] = mono()
			targeted = sink.openStream("rig-sink-b", SPEECH, lambda: None)
			data = tone(SPEECH, 0.4)
			marks["targetedStart"] = mono()
			targeted.write(data)
			targeted.start()
			time.sleep(0.2)
			ask("targeted")
			waitPlayed(targeted, len(data) // SPEECH.blockAlign)
			targeted.close()
			marks["targetedEnd"] = mono()
			del player
			gc.collect()
			result = marks
			""",
			{**pipewire.childEnv(), **steps.env()},
		)
	quanta = monitor.eventsOf("quantum")

	def quantaBetween(start: int, end: int) -> list[int]:
		before = [e["frames"] for e in quanta if e["graph_ns"] <= start]
		return before[-1:] + [e["frames"] for e in quanta if start < e["graph_ns"] <= end]

	assert quantaBetween(value["forcedStart"], value["freeStart"] - 400 * MS) == [256]
	playing = quantaBetween(value["freeStart"], value["freeEnd"])
	assert playing[0] == 1024 and playing[-1] == 512, playing
	assert quantaBetween(value["deactivated"], value["targetedStart"])[-1] == 1024
	report("quantum while free", quantaBetween(value["freeStart"], value["deactivated"]))
	(speech,) = seen["forced"]
	assert len(seen["targeted"]) == 2
	for props in seen["forced"] + seen["targeted"]:
		for key, expected in EXPECTED_PROPERTIES.items():
			assert props.get(key) == expected, (key, props.get(key))
		for forbidden in ("node.force-quantum", "node.force-rate", "node.force-latency"):
			assert forbidden not in props
	assert "target.object" not in speech
	assert [p.get("target.object") for p in seen["targeted"]].count("rig-sink-b") == 1
	heardOnB = monitorB.soundAfter(value["targetedStart"])
	assert heardOnB < value["targetedEnd"]
	assert not [
		e
		for e in monitor.eventsOf("sound")
		if value["targetedStart"] <= e["graph_ns"] <= value["targetedEnd"]
	], "the targeted stream played to the default sink"


def test_wirePlumberRemembersNothingOfBasedreader(pipewire: PipewireFixture, tmp_path: Path):
	"""WirePlumber stores no entry for Basedreader's stream even after a mixer changes its volume,
	though it does for another stream treated the same way."""
	stateFiles = pipewire.stateDir / "wireplumber"
	control = tmp_path / "control.wav"
	with wave.open(str(control), "wb") as w:
		w.setnchannels(1)
		w.setsampwidth(2)
		w.setframerate(48000)
		w.writeframes(
			b"".join(
				int(3000 * math.sin(i / 10)).to_bytes(2, "little", signed=True) for i in range(48000 * 3)
			)
		)
	player = subprocess.Popen(
		[
			str(pipewire.pipewireBin / "pw-play"),
			"--properties",
			"{ application.id = rig-control application.name = RigControl }",
			str(control),
		],
		stdout=subprocess.DEVNULL,
		stderr=subprocess.DEVNULL,
	)

	def stored() -> str:
		return "".join(f.read_text(errors="replace") for f in stateFiles.glob("*") if f.is_file())

	def setVolume(applicationId: str) -> None:
		(node,) = [n for n in pipewire.nodes() if n["info"]["props"].get("application.id") == applicationId]
		channels = len(node["info"]["params"]["Props"][0]["channelVolumes"])
		volumes = ", ".join(["0.3"] * channels)
		pipewire.tool("pw-cli", "set-param", str(node["id"]), "Props", f"{{ channelVolumes: [ {volumes} ] }}")

	try:
		pipewire._waitFor(
			lambda: any(n["info"]["props"].get("application.id") == "rig-control" for n in pipewire.nodes()),
			"the control stream",
		)
		setVolume("rig-control")
		pipewire._waitFor(
			lambda: "rig-control" in stored(), "WirePlumber storing the control stream", timeout=10
		)
	finally:
		player.send_signal(signal.SIGTERM)
		player.wait(5)

	with Steps(tmp_path / "steps", {"volume": lambda: setVolume("basedreader")}) as steps:
		runPipewireChild(
			tmp_path,
			"""
			player = speechPlayer()
			player.feed(tone(SPEECH, 0.4))
			ask("volume")
			player.feed(tone(SPEECH, 2.5))
			player.idle()
			del player
			gc.collect()
			""",
			{**pipewire.childEnv(), **steps.env()},
		)
	contents = stored()
	assert "rig-control" in contents
	assert "basedreader" not in contents.lower()


#: Bounds in milliseconds, by quantum, on the time from a call to the first or last sounding frame:
#: (median, 95th percentile, maximum), None where none applies.
STOP_BOUNDS = {512: (8, 12, 30), 256: (4, 7, 30)}
START_BOUNDS = {512: (12, 16, 30), 256: (8, None, 30)}


def test_stopIsSilentAndStartIsHeardWithinTheQuantum(pipewire: PipewireFixture, tmp_path: Path):
	"""Stopping a player is silent at the sink once the cycle in progress has played, and a new feed
	is heard from the next cycle, at the quanta of 256 and 512.

	WavePlayer.stop reaches the sink as the stream's abort, once per stop and within a millisecond of
	the call; the time to silence is counted from there. At the graph's rate nothing is resampled and
	STOP_BOUNDS apply unchanged. At 22050 Hz, eSpeak's rate, PipeWire's resampler holds a few frames,
	about 1.3 ms, which it reports for the stream. They play out after a stop, since dropping them
	would take a flush, so each stop bound there is STOP_BOUNDS plus that delay. A start is counted
	from the first feed after a stop, which completes that stop, and keeps START_BOUNDS at every
	rate."""
	pipewire.setQuantum(256)
	monitor = pipewire.monitor()
	actions = {f"quantum{q}": (lambda q=q: pipewire.setQuantum(q)) for q in (256, 512)}
	with Steps(tmp_path / "steps", actions) as steps:
		value = runPipewireChild(
			tmp_path,
			"""
			from linuxPlatform.audioOutput.pipewireSink import PipewireStream

			aborts = []
			abort = PipewireStream.abort

			def timedAbort(self):
				aborts.append(mono())
				abort(self)

			PipewireStream.abort = timedAbort
			random.seed(1)
			players = {}
			for rate in (48000, 22050):
				players[rate] = nvwave.WavePlayer(1, rate, 16, wantDucking=False, purpose=nvwave.AudioPurpose.SOUNDS)
				# Each stream is made and linked before anything is timed.
				players[rate].feed(tone(StreamFormat(1, 1, rate, 16), 0.05))
				players[rate].idle()
			rounds = []
			resampler = {}
			for quantum in (256, 512):
				ask(f"quantum{quantum}")
				time.sleep(0.2)
				for rate, player in players.items():
					chunk = tone(StreamFormat(1, 1, rate, 16), 0.3)
					for i in range(PARAMS["rounds"]):
						start = mono()
						player.feed(chunk)
						time.sleep(random.uniform(0.05, 0.2))
						del aborts[:]
						stop = mono()
						player.stop()
						returned = mono()
						rounds.append(
							dict(quantum=quantum, rate=rate, start=start, stop=stop, stopCall=returned - stop, aborts=list(aborts))
						)
						time.sleep(0.08)
					handle = wasapi._players[player._player]._stream.handle
					resampler[f"{quantum} {rate}"] = lib.pwsink_resampler_frames(handle)
			del players, player
			gc.collect()
			result = dict(rounds=rounds, resampler=resampler)
			""",
			{**pipewire.childEnv(), **steps.env()},
			params={"rounds": STOP_ROUNDS},
		)
	measured = {}
	failures = []
	for quantum in (256, 512):
		for rate in (48000, 22050):
			mine = [r for r in value["rounds"] if r["quantum"] == quantum and r["rate"] == rate]
			assert all(len(r["aborts"]) == 1 for r in mine), "a stop did not reach the stream's abort once"
			reached = [(r["aborts"][0] - r["stop"]) / MS for r in mine]
			assert max(reached) < 1, f"the abort was reached up to {max(reached):.2f} ms after the stop"
			starts, stops = [], []
			for r in mine:
				sound = monitor.soundAfter(r["start"])
				starts.append((sound - r["start"]) / MS)
				silence = monitor.silenceAfter(sound)
				# An abort that lands after the cycle in progress began but before the stream's
				# callback ran in it silences that whole cycle, up to a quantum before the call. A
				# silence earlier than that is a graph starved of the processor running dry, and
				# says nothing about stop.
				if silence >= r["aborts"][0] - quantumMs(quantum) * MS:
					stops.append((silence - r["aborts"][0]) / MS)
			assert len(stops) >= len(mine) * 2 // 3, (
				f"only {len(stops)} rounds were still sounding at the stop"
			)
			delay = value["resampler"][f"{quantum} {rate}"] / rate * 1000
			row = {
				"startToSound": summary(starts),
				"stopToSilence": summary(stops),
				"resamplerMs": round(delay, 2),
			}
			row["stopCall"] = summary([r["stopCall"] / MS for r in mine])
			row["stopToAbort"] = summary(reached)
			measured[f"quantum {quantum}, {rate} Hz"] = row
			for name, bounds, allowance in (
				("startToSound", START_BOUNDS, 0),
				("stopToSilence", STOP_BOUNDS, delay),
			):
				for statistic, bound in zip(("median", "p95", "max"), bounds[quantum]):
					if bound is not None and row[name][statistic] > bound + allowance:
						failures.append(
							f"{name} {statistic} at {quantum}, {rate} Hz: {row[name][statistic]} ms "
							f"against {bound + allowance:.2f}"
						)
	report("stop and start", measured)
	assert value["resampler"]["256 48000"] == value["resampler"]["512 48000"] == 0, value["resampler"]
	# More would mean the stream is held back by something other than the resampler's filter.
	assert all(
		frames / 22050 * 1000 < 2 for key, frames in value["resampler"].items() if key.endswith(" 22050")
	)
	assert not failures, failures


def test_indexCallbacksFireJustAfterTheirAudioIsHeard(pipewire: PipewireFixture, tmp_path: Path):
	"""A feed's completion callback, which speech indexes ride on, fires within 2 ms after the
	monitor hears the last of its audio, never before."""
	monitor = pipewire.monitor()
	actions = {f"quantum{q}": (lambda q=q: pipewire.setQuantum(q)) for q in (256, 512)}
	with Steps(tmp_path / "steps", actions) as steps:
		value = runPipewireChild(
			tmp_path,
			"""
			player = speechPlayer()
			chunk = tone(SPEECH, 0.15)
			gap = bytes(SPEECH.blockAlign * int(SPEECH.samplesPerSec * 0.06))
			player.feed(gap)
			player.idle()
			runs = {}
			for quantum in (256, 512):
				ask(f"quantum{quantum}")
				time.sleep(0.2)
				fired = []
				start = mono()
				for i in range(6):
					player.feed(chunk, onDone=lambda i=i: fired.append(mono()))
					player.feed(gap)
				player.idle()
				runs[quantum] = dict(start=start, fired=fired)
			del player
			gc.collect()
			result = runs
			""",
			{**pipewire.childEnv(), **steps.env()},
		)
	lateness = {}
	for quantum, run in value.items():
		ends = []
		after = run["start"]
		for _ in range(6):
			sound = monitor.soundAfter(after)
			after = monitor.silenceAfter(sound)
			ends.append(after)
		lateness[quantum] = [round((fired - end) / MS, 3) for fired, end in zip(run["fired"], ends)]
	report("callback after the end of its audio, ms", lateness)
	for quantum, late in lateness.items():
		assert len(late) == 6, quantum
		# The sine ends at a zero crossing, where the resampler's interpolation decides which of its
		# last few frames clear the monitor's -60 dB threshold: a quarter of a millisecond, five
		# frames at 22050 Hz, is as close as this can place the end of the audio.
		assert all(-0.25 <= ms <= 2 for ms in late), (quantum, late)


def _stall(fixture: PipewireFixture, case: str) -> None:
	if case == "sinkRemoved":
		fixture.removeSink("rig-only")
	elif case == "sessionManagerKilled":
		fixture.killSessionManager()
	elif case == "daemonFrozen":
		fixture.freezeDaemon()
	elif case == "daemonKilled":
		fixture.killDaemon()


def _restore(fixture: PipewireFixture, case: str) -> str:
	"""Undoes the stall and returns the sink to listen to."""
	if case == "sinkRemoved":
		fixture.createSink("rig-only")
		fixture.waitForDefaultSink("rig-only")
		return "rig-only"
	if case in ("noSessionManager", "sessionManagerKilled"):
		fixture.startSessionManager()
	elif case == "daemonFrozen":
		fixture.thawDaemon()
	elif case == "daemonKilled":
		fixture.startDaemon()
		fixture.startSessionManager()
	return "rig-sink-a"


@pytest.mark.parametrize(
	"case",
	["noSessionManager", "sinkRemoved", "sessionManagerKilled", "daemonFrozen", "daemonKilled"],
)
def test_aGraphThatStopsConsumingNeverHoldsUpNvda(tmp_path: Path, case: str):
	"""When nothing consumes the stream, feeds keep returning in real time, idle returns, every
	callback fires and nothing raises; once PipeWire is back, the same stream is heard again within
	1.5 s with nothing reopened in Python."""
	fixture = PipewireFixture(tmp_path / "pipewire", sinks=case != "sinkRemoved")
	monitors = {}
	try:
		fixture.startDaemon()
		if case == "sinkRemoved":
			fixture.createSink("rig-only")
		if case != "noSessionManager":
			fixture.startSessionManager()
			fixture.waitForDefaultSink()
			monitors["before"] = fixture.monitor("rig-only" if case == "sinkRemoved" else "rig-sink-a")

		def restore() -> int:
			sinkName = _restore(fixture, case)
			restored = time.monotonic_ns()
			monitors["after"] = fixture.monitor(sinkName)
			return restored

		actions = {"stall": lambda: _stall(fixture, case), "restore": restore}
		with Steps(tmp_path / "steps", actions) as steps:
			value = runPipewireChild(
				tmp_path,
				"""
				opened = []
				openStream = sink.openStream
				sink.openStream = lambda *a: opened.append(a) or openStream(*a)
				player = speechPlayer()
				chunk = tone(SPEECH, 0.3)
				fired = []
				feeds = []

				def feed(phase, count):
					for i in range(count):
						start = mono()
						player.feed(chunk, onDone=lambda: fired.append(phase))
						feeds.append((phase, (mono() - start) / 1e6))

				if PARAMS["case"] != "noSessionManager":
					feed("before", 2)
				stream = wasapi._players[player._player]._stream
				ask("stall")
				feed("during", 6)
				start = mono()
				player.idle()
				idleMs = (mono() - start) / 1e6
				restored = ask("restore")["value"]
				feed("after", 6)
				player.idle()
				sameStream = wasapi._players[player._player]._stream is stream
				del player, stream
				gc.collect()
				result = dict(
					idleMs=idleMs,
					restored=restored,
					fired=fired,
					feeds=feeds,
					opened=len(opened),
					sameStream=sameStream,
					deviceError=nvwave.WavePlayer.audioDeviceError_static,
				)
				""",
				{**fixture.childEnv(), **steps.env()},
				params={"case": case},
				# A stall the library fails to see hangs the child, which otherwise ends within ten seconds.
				timeout=40,
			)
		heard = monitors["after"].soundAfter(value["restored"], timeout=5)
	finally:
		fixture.stop()
	recovery = (heard - value["restored"]) / MS
	report(
		f"{case}",
		{
			"idleMs": round(value["idleMs"], 1),
			"recoveryMs": round(recovery, 1),
			"slowestFeedMs": round(max(ms for _, ms in value["feeds"]), 1),
		},
	)
	before = 0 if case == "noSessionManager" else 2
	assert value["fired"] == ["before"] * before + ["during"] * 6 + ["after"] * 6
	assert value["idleMs"] < 600
	# A feed waits at most until half the 400 ms buffer has played, and the clock takes over within
	# its bound: 500 ms for a stream that has never been scheduled.
	assert all(ms < 800 for _, ms in value["feeds"]), value["feeds"]
	assert value["opened"] == 1 and value["sameStream"]
	assert not value["deviceError"]
	assert recovery < 1500


def test_withNoDaemonAStreamOpensKeepsTimeAndIsHeardOnceOneStarts(tmp_path: Path):
	"""With no daemon, opening succeeds, feeds keep real time and nothing counts as delivered; once
	a daemon and its session manager start, the same player is heard and deliveredFrames rises."""
	fixture = PipewireFixture(tmp_path / "pipewire")
	monitors = {}

	def startDaemon() -> int:
		fixture.startDaemon()
		fixture.startSessionManager()
		ready = time.monotonic_ns()
		monitors["after"] = fixture.monitor()
		return ready

	try:
		with Steps(tmp_path / "steps", {"daemon": startDaemon}) as steps:
			value = runPipewireChild(
				tmp_path,
				"""
				player = speechPlayer()
				chunk = tone(SPEECH, 0.25)
				fired = []
				start = mono()
				for i in range(8):
					player.feed(chunk, onDone=lambda: fired.append("before"))
				player.idle()
				took = (mono() - start) / 1e6
				deliveredBefore = sink.deliveredFrames()
				connectedBefore = lib.pwsink_connected()
				ready = ask("daemon")["value"]
				for i in range(8):
					player.feed(chunk, onDone=lambda: fired.append("after"))
				player.idle()
				del player
				gc.collect()
				result = dict(
					took=took,
					deliveredBefore=deliveredBefore,
					connectedBefore=connectedBefore,
					deliveredAfter=sink.deliveredFrames(),
					ready=ready,
					fired=fired,
				)
				""",
				{**fixture.childEnv(), **steps.env()},
				timeout=40,
			)
		heard = monitors["after"].soundAfter(value["ready"], timeout=5)
	finally:
		fixture.stop()
	report(
		"no daemon",
		{
			"twoSecondsTookMs": round(value["took"], 1),
			"heardAfterDaemonMs": round((heard - value["ready"]) / MS, 1),
		},
	)
	assert 1950 <= value["took"] <= 2250
	assert value["deliveredBefore"] == 0 and value["connectedBefore"] == 0
	assert value["fired"] == ["before"] * 8 + ["after"] * 8
	assert (heard - value["ready"]) / MS < 1500
	assert value["deliveredAfter"] > 0


def test_aStreamNobodyLinksWaitsForLinkingOnlyOnce(tmp_path: Path):
	"""With no session manager, a new stream waits once, for as long as linking may take, before the
	clock takes over; every utterance after that, started anew after a stop as nvwave does between
	utterances, takes its own length and no more."""
	fixture = PipewireFixture(tmp_path / "pipewire")
	try:
		fixture.startDaemon()
		value = runPipewireChild(
			tmp_path,
			"""
			player = speechPlayer()
			chunk = tone(SPEECH, 0.3)
			took = []
			for i in range(5):
				start = mono()
				player.feed(chunk)
				player.idle()
				took.append((mono() - start) / 1e6)
				player.stop()
				time.sleep(0.1)
			handle = wasapi._players[player._player]._stream.handle
			result = dict(took=took, calls=lib.pwsink_process_calls(handle))
			""",
			fixture.childEnv(),
			timeout=40,
		)
	finally:
		fixture.stop()
	report("300 ms utterances on a stream nobody links, ms", [round(ms, 1) for ms in value["took"]])
	assert value["calls"] == 0, "the stream was consumed, so this showed nothing"
	first, *rest = value["took"]
	# The 500 ms wait for the graph, once for this pw_stream, then the utterance's own 300 ms, with
	# room for nvwave's polling.
	assert first < 500 + 300 + 150, first
	# Every later utterance is idle well within 600 ms: it takes its own length and no more.
	assert all(ms < 300 + 60 for ms in rest), rest


def test_aPooledStreamOpensAtOnceAndIsHeardInTheNextCycle(pipewire: PipewireFixture, tmp_path: Path):
	"""A closed stream is kept, so the next player of its format opens in under 1 ms and is heard
	within one quantum and 5 ms of its first feed; the pool keeps at most 8 and lets them expire."""
	pipewire.setQuantum(512)
	monitor = pipewire.monitor()
	value = runPipewireChild(
		tmp_path,
		"""
		data = tone(SPEECH, 0.1)
		first = speechPlayer()
		first.feed(data)
		first.idle()
		del first
		gc.collect()
		time.sleep(0.2)
		opens, plays = [], []
		for i in range(8):
			start = mono()
			player = speechPlayer()
			opens.append((mono() - start) / 1e6)
			start = mono()
			player.feed(data)
			player.idle()
			plays.append(start)
			del player
			gc.collect()
			time.sleep(0.1)
		streamOpens = []
		for i in range(20):
			start = mono()
			stream = sink.openStream("", SPEECH, lambda: None)
			streamOpens.append((mono() - start) / 1e6)
			stream.close()
		poolBefore = lib.pwsink_pool_size()
		formats = [StreamFormat(1, channels, rate, 16) for channels in (1, 2) for rate in (8000, 11025, 16000, 22050, 32000, 44100)]
		streams = [sink.openStream("", fmt, lambda: None) for fmt in formats]
		for stream in streams:
			stream.close()
		time.sleep(0.3)
		poolFull = lib.pwsink_pool_size()
		attachedFull = lib.pwsink_attached_streams()
		lib.pwsink_set_pool_limits(8, 300)
		time.sleep(0.6)
		result = dict(
			opens=opens,
			streamOpens=streamOpens,
			plays=plays,
			poolBefore=poolBefore,
			poolFull=poolFull,
			attachedFull=attachedFull,
			poolExpired=lib.pwsink_pool_size(),
			attachedExpired=lib.pwsink_attached_streams(),
		)
		""",
		pipewire.childEnv(),
	)
	heard = [(monitor.soundAfter(start) - start) / MS for start in value["plays"]]
	report(
		"pool",
		{
			"playerOpenMs": summary(value["opens"]),
			"sinkOpenMs": summary(value["streamOpens"]),
			"heardAfterFirstFeedMs": summary(heard),
		},
	)
	assert max(value["streamOpens"]) < 1
	assert max(heard) < quantumMs(512) + 5
	assert value["poolBefore"] == 1
	assert value["poolFull"] == 8 and value["attachedFull"] == 8
	assert value["poolExpired"] == 0 and value["attachedExpired"] == 0
	assert not ourNodes(pipewire)


def test_aPooledStreamIsReusedOnlyForItsOwnDevice(pipewire: PipewireFixture, tmp_path: Path):
	"""The pool is keyed by device as well as format: a player asking for another device than the
	one a pooled stream of its format plays to gets a stream of its own, and is heard there."""
	pipewire.setQuantum(512)
	monitorA = pipewire.monitor()
	monitorB = pipewire.monitor("rig-sink-b")
	value = runPipewireChild(
		tmp_path,
		"""
		data = tone(SPEECH, 0.15)

		def play(deviceId):
			start = mono()
			stream = sink.openStream(deviceId, SPEECH, lambda: None)
			stream.write(data)
			stream.start()
			waitPlayed(stream, len(data) // SPEECH.blockAlign)
			stream.stop()
			stream.close()
			end = mono()
			time.sleep(0.15)
			return dict(start=start, end=end, pool=lib.pwsink_pool_size())

		result = [play(""), play("rig-sink-b"), play(""), play("rig-sink-b")]
		""",
		pipewire.childEnv(),
	)
	for play, sinkName in zip(value, ("a", "b", "a", "b")):
		heard, silent = (monitorA, monitorB) if sinkName == "a" else (monitorB, monitorA)
		assert heard.soundAfter(play["start"]) < play["end"], f"not heard on rig-sink-{sinkName}"
		assert not [e for e in silent.eventsOf("sound") if play["start"] <= e["graph_ns"] <= play["end"]], (
			f"a player asking for rig-sink-{sinkName} was heard on the other sink"
		)
	assert [play["pool"] for play in value] == [1, 2, 2, 2]


def test_streamsStayActiveUntilTheKeepAwakeDeadlineAndWakeAtOnce(pipewire: PipewireFixture, tmp_path: Path):
	"""A stream keeps being scheduled, which keeps the device awake, until the keep-awake deadline,
	is deactivated once it passes with nothing to play, and is heard within a quantum and 5 ms of the
	next feed."""
	pipewire.setQuantum(512)
	monitor = pipewire.monitor()
	value = runPipewireChild(
		tmp_path,
		"""
		player = speechPlayer()
		player.feed(tone(SPEECH, 0.2))
		player.idle()
		handle = wasapi._players[player._player]._stream.handle
		sink.keepAwakeFor("", 800, 0.0)
		start = mono()
		samples = []
		while mono() - start < 1_400_000_000:
			samples.append((mono() - start, lib.pwsink_process_calls(handle), lib.pwsink_is_active(handle)))
			time.sleep(0.02)
		feed = mono()
		player.feed(tone(SPEECH, 0.2))
		player.idle()
		del player
		gc.collect()
		result = dict(samples=samples, feed=feed)
		""",
		pipewire.childEnv(),
	)
	samples = value["samples"]
	inactive = [t for t, _calls, active in samples if not active]
	assert inactive, "the stream was never deactivated"
	deactivatedMs = inactive[0] / MS
	assert 800 <= deactivatedMs <= 900, deactivatedMs
	activeCalls = [calls for t, calls, active in samples if t < 780 * MS]
	assert activeCalls[-1] - activeCalls[0] >= 0.7 * 780 / quantumMs(512)
	settled = [calls for t, calls, active in samples if t > deactivatedMs * MS + 50 * MS]
	assert settled[0] == settled[-1], "process calls continued after deactivation"
	heard = (monitor.soundAfter(value["feed"]) - value["feed"]) / MS
	report("keep awake", {"deactivatedAfterMs": round(deactivatedMs, 1), "heardAfterWakeMs": round(heard, 2)})
	assert heard < quantumMs(512) + 5


def test_aStreamDeactivatedBetweenSoundsWakesForEachOne(pipewire: PipewireFixture, tmp_path: Path):
	"""With no keep-awake time, a stream is deactivated as soon as it has drained, often just as the
	next sound is written to it. Every sound is still heard, promptly: either the library's thread
	sees the new frames before it deactivates, or the write sees the stream inactive and wakes it.

	The player stays in its playing state between the sounds, as between the chunks of an
	utterance, so only the write can wake the stream."""
	pipewire.setQuantum(512)
	monitor = pipewire.monitor()
	value = runPipewireChild(
		tmp_path,
		"""
		import config

		config.conf["audio"]["audioAwakeTime"] = 0
		sink.stopKeepingAwake()
		random.seed(2)
		player = speechPlayer()
		beep = tone(SPEECH, 0.03)
		player.feed(beep)
		player.sync()
		handle = wasapi._players[player._player]._stream.handle
		feeds, inactiveBefore = [], 0
		for i in range(60):
			time.sleep(random.uniform(0.025, 0.08))
			inactiveBefore += not lib.pwsink_is_active(handle)
			feeds.append(mono())
			player.feed(beep)
			player.sync()
		player.idle()
		del player
		gc.collect()
		result = dict(feeds=feeds, inactiveBefore=inactiveBefore)
		""",
		pipewire.childEnv(),
	)
	heard = []
	for start in value["feeds"]:
		sound = monitor.soundAfter(start)
		heard.append(round((sound - start) / MS, 2))
		monitor.silenceAfter(sound)
	report("sound after each write to a stream deactivated or deactivating, ms", summary(heard))
	assert value["inactiveBefore"] >= 10, "the stream was rarely inactive, so this showed little"
	assert max(heard) < quantumMs(512) + 5, heard


def test_theStartOfASoundIsNeverLost(pipewire: PipewireFixture, tmp_path: Path):
	"""Every sound is heard from its first frame, on a stream just made and on one just woken, while
	another thread keeps the library's thread and the daemon busy making and destroying streams.

	A pw_stream is called to process as soon as the daemon starts its node, but the sink waits for
	it, and so mixes what it hands over, only from when the daemon has put the node into the graph,
	a round trip later; a cycle handed over in between is lost. And a stream the library's thread
	has yet to give a pw_stream, or to wake, must wait for it rather than let the clock discard its
	start."""
	pipewire.setQuantum(256)
	monitor = pipewire.monitor()
	value = runPipewireChild(
		tmp_path,
		"""
		running = [True]

		def churn():
			rates = (8000, 11025, 12000, 16000, 22050, 24000, 32000, 44100, 48000, 96000)
			formats = [StreamFormat(1, 1, rate, 16) for rate in rates]
			while running[0]:
				for fmt in formats:
					other = sink.openStream("", fmt, lambda: None)
					other.write(bytes(fmt.blockAlign * 64))
					other.start()
					other.close()

		churner = threading.Thread(target=churn, name="churn")
		churner.start()
		time.sleep(0.3)
		sink.stopKeepingAwake()
		sounds = []
		for i in range(20):
			fmt = StreamFormat(1, 1, 17000 + i * 499, 16)
			data = tone(fmt, 0.12)
			stream = sink.openStream("", fmt, lambda: None)
			start = mono()
			stream.write(data)
			stream.start()
			waitPlayed(stream, len(data) // fmt.blockAlign)
			stream.stop()
			stream.close()
			sounds.append(dict(kind="new", start=start))
			time.sleep(0.15)
		# Kept open between sounds, as nvwave's speech player is, so that the churn cannot take it
		# from the pool: it sleeps once it has drained, and each sound wakes it.
		fmt = StreamFormat(1, 1, 16001, 16)
		data = tone(fmt, 0.12)
		stream = sink.openStream("", fmt, lambda: None)
		woken = 0
		for i in range(21):
			woken += not lib.pwsink_is_active(stream.handle)
			start = mono()
			stream.write(data)
			stream.start()
			waitPlayed(stream, len(data) // fmt.blockAlign)
			stream.stop()
			stream.reset()
			if i:
				sounds.append(dict(kind="woken", start=start))
			time.sleep(0.4)
		stream.close()
		running[0] = False
		churner.join()
		result = dict(sounds=sounds, woken=woken)
		""",
		pipewire.childEnv(),
	)
	lengths = {"new": [], "woken": []}
	for sound in value["sounds"]:
		start = monitor.soundAfter(sound["start"])
		lengths[sound["kind"]].append(round((monitor.silenceAfter(start) - start) / MS, 2))
	report("heard length of 120 ms sounds, ms", {kind: summary(ms) for kind, ms in lengths.items()})
	assert value["woken"] >= 15, "the reused stream was rarely asleep, so this showed little"
	for kind, heard in lengths.items():
		short = [ms for ms in heard if ms < 120 - 1]
		assert not short, (kind, heard)


def test_aWokenStreamWaitsForASlowDaemonRatherThanLoseItsStart(pipewire: PipewireFixture, tmp_path: Path):
	"""A stream asleep after a sound is woken by the next one, which the daemon has to schedule
	again; when the daemon takes 150 ms to answer, as a loaded desktop's can, the sound plays late
	but whole, since the clock waits for the daemon before it discards anything.

	The stream runs at the graph's rate: after a daemon has been stopped and continued, PipeWire's
	resampler sometimes drops a few milliseconds of what it is handed, which would hide whether the
	library handed it all over."""
	pipewire.setQuantum(256)
	monitor = pipewire.monitor()
	actions = {}
	for i in range(3):
		actions[f"freeze{i}"] = pipewire.freezeDaemon
		actions[f"thaw{i}"] = pipewire.thawDaemon
	with Steps(tmp_path / "steps", actions) as steps:
		value = runPipewireChild(
			tmp_path,
			"""
			sink.stopKeepingAwake()
			fmt = StreamFormat(1, 1, 48000, 16)
			data = tone(fmt, 0.12)
			frames = len(data) // fmt.blockAlign
			stream = sink.openStream("", fmt, lambda: None)
			stream.write(data)
			stream.start()
			waitPlayed(stream, frames)
			sounds = []
			for i in range(3):
				stream.stop()
				stream.reset()
				# A stream first scheduled only after the library's thread last looked at it is
				# looked at again a second later.
				time.sleep(1.2 if i == 0 else 0.4)
				asleep = not lib.pwsink_is_active(stream.handle)
				ask(f"freeze{i}")
				before = sink.deliveredFrames()
				start = mono()
				stream.write(data)
				stream.start()
				time.sleep(0.15)
				ask(f"thaw{i}")
				waitPlayed(stream, frames)
				time.sleep(0.05)
				sounds.append(dict(start=start, asleep=asleep, delivered=sink.deliveredFrames() - before))
			stream.close()
			result = dict(sounds=sounds, frames=frames)
			""",
			{**pipewire.childEnv(), **steps.env()},
		)
	heard = []
	for sound in value["sounds"]:
		# Counted in frames recorded rather than between graph times: the driver, stopped with the
		# daemon, may run its first cycles after the thaw closer together than a quantum.
		left, _ = monitor.samples(sound["start"], sound["start"] + 600 * MS)
		heard.append(round(sum(1 for x in left if abs(x) > 0.001) / (RATE / 1000), 2))
	report("120 ms sounds woken while the daemon was frozen for 150 ms, heard for ms", heard)
	assert all(sound["asleep"] for sound in value["sounds"]), (
		"the stream was not asleep, so this showed nothing"
	)
	# Every frame was handed to the graph: the clock discarded none while the daemon was away.
	assert [sound["delivered"] for sound in value["sounds"]] == [value["frames"]] * 3
	# Once in a few dozen wakes after the daemon has been stopped and continued, one cycle handed
	# over is not heard; in hundreds of wakes from a daemon that kept running it never was.
	assert all(ms > 120 - quantumMs(256) - 1 for ms in heard), heard


def test_stopAndAbortNeverWaitWhileOtherStreamsOpenAndClose(pipewire: PipewireFixture, tmp_path: Path):
	"""stop and abort are each one store: 10,000 calls of each, while another thread keeps opening
	and closing streams, which keeps the library's thread busy in PipeWire, never wait for anything
	and never take 0.2 ms.

	They are timed as the stream calls them, through foreign functions that keep the GIL, with the
	collector held off, so that what is timed is the library. A call that waited for a lock, or for
	the GIL, would show as a voluntary context switch of the calling thread, of which there must be
	none. A call during which the system took the processor away from the thread, which a loaded
	machine does now and then, says nothing about the library and is not timed."""
	value = runPipewireChild(
		tmp_path,
		"""
		stream = sink.openStream("", SPEECH, lambda: None)
		chunk = tone(SPEECH, 0.005)
		stream.write(tone(SPEECH, 0.9))
		stream.start()
		running = True
		churned = [0]

		def churn():
			formats = [StreamFormat(1, 1, rate, 16) for rate in (8000, 11025, 12000, 16000, 22050, 24000, 32000, 44100, 48000, 96000)]
			while running:
				for fmt in formats:
					other = sink.openStream("", fmt, lambda: None)
					other.write(bytes(fmt.blockAlign * 64))
					other.start()
					other.close()
					churned[0] += 1

		churner = threading.Thread(target=churn, name="churn")
		churner.start()
		time.sleep(0.2)
		import resource

		took = {"stop": [], "abort": []}
		waited = {"stop": 0, "abort": 0}
		preempted = {"stop": 0, "abort": 0}
		clock = time.perf_counter_ns
		usage = resource.getrusage
		thread = resource.RUSAGE_THREAD
		silencers = stream._silencers
		handle = stream.handle
		gc.disable()
		for i in range(20000):
			name = "abort" if i % 2 else "stop"
			call = silencers.abort if i % 2 else silencers.stop
			before = usage(thread)
			start = clock()
			call(handle)
			end = clock()
			after = usage(thread)
			waited[name] += after.ru_nvcsw - before.ru_nvcsw
			if after.ru_nivcsw == before.ru_nivcsw:
				took[name].append(end - start)
			else:
				preempted[name] += 1
			if name == "abort":
				# As the player completes a stop: only a reset ends an abort.
				stream.stop()
				stream.reset()
				stream.write(chunk)
			stream.start()
			time.sleep(0.0001)
		gc.enable()
		running = False
		churner.join()
		stream.close()
		result = dict(churned=churned[0])
		for name, times in took.items():
			times.sort()
			result[name] = dict(
				n=len(times), median=times[len(times) // 2], worst=times[-1], waited=waited[name], preempted=preempted[name]
			)
		""",
		pipewire.childEnv(),
	)
	report("stop and abort under contention, ns", value)
	assert value["churned"] > 100
	for name in ("stop", "abort"):
		assert value[name]["waited"] == 0, name
		assert value[name]["n"] >= 9900, name
		assert value[name]["worst"] < 200_000, name


def test_anAbortFromAnotherThreadHoldsUntilTheFeedersReset(pipewire: PipewireFixture, tmp_path: Path):
	"""abort and stop may land on any thread while the feeding thread is inside any other call on the
	same stream, as when NVDA's player is stopped while its feeder is writing and starting.

	In each round one thread keeps the stream topped up, starting it and asking its position and
	volume without pause, as a feeder that has not yet seen the stop does, while another aborts and
	stops it at a random moment. The feeder goes on writing and starting for 60 ms after the abort
	before it stops and resets the stream. The abort is silent at the sink within one quantum and
	5 ms, and nothing is heard again before the reset. Then both threads call everything at random
	for a second and a half, and the stream still plays a sound whole."""
	pipewire.setQuantum(256)
	monitor = pipewire.monitor()
	value = runPipewireChild(
		tmp_path,
		"""
		random.seed(3)
		fmt = StreamFormat(1, 1, 48000, 16)
		chunk = tone(fmt, 0.02)
		chunkFrames = len(chunk) // fmt.blockAlign
		stream = sink.openStream("", fmt, lambda: None)
		# Linked before the first round, whose abort could otherwise land before anything is heard.
		stream.write(chunk)
		stream.start()
		waitPlayed(stream, chunkFrames)
		stream.stop()
		stream.reset()
		time.sleep(0.1)

		def feedUntil(done):
			while not done():
				if stream.bufferedFrames() < 5 * chunkFrames:
					stream.write(chunk)
				stream.start()
				stream.playedFrames()
				stream.setChannelVolume(0, 1.0)
				time.sleep(0.0005)

		go = threading.Event()
		quitting = [False]
		abortedAt = []

		def stopper():
			while True:
				go.wait()
				go.clear()
				if quitting[0]:
					return
				time.sleep(random.uniform(0.03, 0.15))
				abortedAt.append(mono())
				stream.abort()
				stream.stop()

		stopping = threading.Thread(target=stopper, name="stopper")
		stopping.start()
		rounds = []
		for i in range(30):
			start = mono()
			before = len(abortedAt)
			go.set()
			feedUntil(lambda: len(abortedAt) > before)
			hold = mono() + 60_000_000
			feedUntil(lambda: mono() > hold)
			reset = mono()
			stream.stop()
			stream.reset()
			rounds.append(dict(start=start, abort=abortedAt[-1], reset=reset))
			time.sleep(0.1)
		quitting[0] = True
		go.set()
		stopping.join()

		running = [True]
		silenced = [0]

		def hammer():
			while running[0]:
				random.choice((stream.abort, stream.stop))()
				silenced[0] += 1
				time.sleep(random.uniform(0, 0.001))

		hammering = threading.Thread(target=hammer, name="hammer")
		hammering.start()
		calls = 0
		end = mono() + 1_500_000_000
		while mono() < end:
			step = random.randrange(5)
			if step == 0 and stream.bufferedFrames() < 5 * chunkFrames:
				stream.write(chunk)
			elif step == 1:
				stream.start()
			elif step == 2:
				stream.stop()
				stream.reset()
			elif step == 3:
				stream.playedFrames()
			else:
				stream.setChannelVolume(0, random.choice((0.5, 1.0)))
			calls += 1
			time.sleep(random.uniform(0, 0.0005))
		running[0] = False
		hammering.join()
		stream.stop()
		stream.reset()
		stream.setChannelVolume(0, 1.0)
		time.sleep(0.2)
		data = tone(fmt, 0.2)
		before = sink.deliveredFrames()
		final = mono()
		stream.write(data)
		stream.start()
		waitPlayed(stream, len(data) // fmt.blockAlign)
		played = dict(start=final, ms=(mono() - final) / 1e6, taken=sink.deliveredFrames() - before)
		stream.close()
		result = dict(rounds=rounds, final=played, calls=calls, silenced=silenced[0])
		""",
		pipewire.childEnv(),
	)
	bound = quantumMs(256) + 5
	final = value["final"]
	# Taken whole by the graph rather than discarded by the clock.
	assert final["taken"] == 9600, f"after the hammering, the graph took {final}"
	sound = monitor.soundAfter(final["start"])
	heard = (monitor.silenceAfter(sound) - sound) / MS
	edges = sorted((e["graph_ns"], kind) for kind in ("sound", "silence") for e in monitor.eventsOf(kind))
	toSilence, failures, leftOut = [], [], []
	for i, r in enumerate(value["rounds"]):
		end = r["reset"] + 50 * MS
		timeline = ", ".join(
			f"{kind} at {(ns - r['abort']) / MS:+.1f} ms" for ns, kind in edges if r["start"] <= ns <= end
		)
		prior = [(ns, kind) for ns, kind in edges if r["start"] <= ns < r["abort"]]
		later = [(ns, kind) for ns, kind in edges if r["abort"] <= ns <= end]
		if prior[-1:] and prior[-1][1] == "silence" and prior[-1][0] >= r["abort"] - quantumMs(256) * MS:
			# The abort landed after the cycle in progress began but before the stream's callback ran
			# in it, so nothing was handed over for that cycle, which is heard as silence from its
			# start.
			silence, rest = prior[-1][0], later
		elif prior[-1:] and prior[-1][1] == "sound":
			if not later or later[0][1] != "silence":
				failures.append(f"round {i}, never silent before the reset: {timeline}")
				continue
			silence, rest = later[0][0], later[1:]
		else:
			# Nothing was being heard as the abort landed, as when a graph starved of the processor
			# has run dry; such a round says nothing about the abort.
			leftOut.append(f"round {i}: {timeline}")
			continue
		toSilence.append((silence - r["abort"]) / MS)
		if toSilence[-1] > bound or rest:
			failures.append(f"round {i}: {timeline}")
	report(
		"abort from another thread",
		{
			"toSilenceMs": summary(toSilence),
			"leftOut": leftOut,
			"hammerCalls": value["calls"],
			"hammerSilences": value["silenced"],
		},
	)
	assert not failures, failures
	assert len(toSilence) >= 20, f"only {len(toSilence)} rounds were sounding at the abort: {leftOut}"
	assert value["calls"] > 500 and value["silenced"] > 500
	assert heard > 200 - 1, f"a 200 ms sound after the hammering was heard for {heard:.1f} ms"


#: Run in a child: three threads that do nothing but run Python, as NVDA's own can.
BUSY_THREADS = """
busyRunning = [True]


def busy():
	x = 0
	while busyRunning[0]:
		for i in range(1000):
			x += i * i


busyThreads = [threading.Thread(target=busy, name=f"busy {i}") for i in range(3)]
for thread in busyThreads:
	thread.start()


def stopBusyThreads():
	busyRunning[0] = False
	for thread in busyThreads:
		thread.join()
"""


def test_aPlayersStopReturnsAtOnceWhileOtherThreadsRunPython(pipewire: PipewireFixture, tmp_path: Path):
	"""WavePlayer.stop, landing as an utterance starts, returns at once while three other threads run
	Python, for speech and for sounds.

	It reaches the sink as the stream's abort, which, like the stream's stop, is a foreign function
	that keeps the GIL, and the test checks that of both. One that let it go would have the thread
	stopping speech win it back from the busy threads afterwards. That mostly happens at once and
	now and then takes a switch interval for each of them, so the times alone would show it only
	sometimes."""
	pipewire.setQuantum(512)
	value = runPipewireChild(
		tmp_path,
		BUSY_THREADS
		+ textwrap.dedent("""
		random.seed(4)
		chunk = tone(SPEECH, 0.3)
		measured = {}
		for purpose in (nvwave.AudioPurpose.SPEECH, nvwave.AudioPurpose.SOUNDS):
			player = nvwave.WavePlayer(1, 22050, 16, wantDucking=False, purpose=purpose)
			player.enableTrimmingLeadingSilence(False)
			go, cancelled, finished = threading.Event(), threading.Event(), threading.Event()
			quitting = [False]

			def synthesize():
				while True:
					go.wait()
					go.clear()
					if quitting[0]:
						return
					for _ in range(3):
						if cancelled.is_set():
							break
						player.feed(chunk, onDone=lambda: None)
					player.idle()
					finished.set()

			synth = threading.Thread(target=synthesize, name="synth")
			synth.start()
			silencers = wasapi._players[player._player]._stream._silencers
			keepsGil = [bool(type(f)._flags_ & ctypes._FUNCFLAG_PYTHONAPI) for f in (silencers.stop, silencers.abort)]
			returned, hung = [], 0
			for _ in range(300):
				cancelled.clear()
				finished.clear()
				go.set()
				time.sleep(random.uniform(0, 0.004))
				called = mono()
				cancelled.set()
				player.stop()
				returned.append((mono() - called) / 1e6)
				if not finished.wait(5):
					hung += 1
					break
			quitting[0] = True
			go.set()
			synth.join()
			measured[purpose.name] = dict(returned=returned, hung=hung, keepsGil=keepsGil)
			del player
			gc.collect()
		stopBusyThreads()
		result = measured
		"""),
		pipewire.childEnv(),
	)
	report(
		"WavePlayer.stop with three busy threads, ms",
		{name: summary(m["returned"]) for name, m in value.items()},
	)
	for name, measured in value.items():
		returned = measured["returned"]
		assert measured["keepsGil"] == [True, True], "the stream's stop or abort lets go of the GIL"
		assert measured["hung"] == 0 and len(returned) == 300, name
		# The stopping thread holds the GIL from the call on, unless the system takes the processor
		# away from it.
		assert sum(1 for ms in returned if ms > 5) <= 1 and max(returned) < 20, (name, sorted(returned)[-10:])


def test_noCallFromPythonWaitsForTheLibrarysThreadOrTheDaemon(pipewire: PipewireFixture, tmp_path: Path):
	"""Every call NVDA's player and nvwave make returns at once while the library's own thread is
	busy, and while the daemon is frozen; audio already flowing keeps flowing meanwhile."""
	monitor = pipewire.monitor()
	actions = {"freeze": pipewire.freezeDaemon, "thaw": pipewire.thawDaemon}
	with Steps(tmp_path / "steps", actions) as steps:
		value = runPipewireChild(
			tmp_path,
			"""
			def calls(tag):
				took = {}

				def timed(name, call):
					start = time.perf_counter_ns()
					value = call()
					took[f"{tag} {name}"] = (time.perf_counter_ns() - start) / 1e6
					return value

				fmt = StreamFormat(1, 2, 32000 if tag == "busy" else 24000, 16)
				data = tone(fmt, 0.1)
				fresh = timed("open new", lambda: sink.openStream("", fmt, lambda: None))
				timed("write", lambda: fresh.write(data))
				timed("start", fresh.start)
				timed("played", fresh.playedFrames)
				timed("buffered", fresh.bufferedFrames)
				timed("volume", lambda: fresh.setChannelVolume(1, 0.5))
				timed("stop", fresh.stop)
				timed("reset", fresh.reset)
				timed("close", fresh.close)
				again = timed("open pooled", lambda: sink.openStream("", fmt, lambda: None))
				timed("close again", again.close)
				timed("keep awake", lambda: sink.keepAwakeFor("", 30000, 0.0))
				timed("stop keeping awake", sink.stopKeepingAwake)
				timed("gain", lambda: sink.setGain(1.0))
				timed("delivered", sink.deliveredFrames)
				return took

			playing = sink.openStream("", SPEECH, lambda: None)
			playing.write(tone(SPEECH, 0.9))
			playing.start()
			time.sleep(0.1)
			attachedBefore = lib.pwsink_attached_streams()
			lib.pwsink_block_loop(500)
			time.sleep(0.02)
			busyStart = mono()
			took = calls("busy")
			attachedWhileBusy = lib.pwsink_attached_streams()
			# The loop thread stays blocked for another 450 ms or so, in which the tone plays on.
			busyEnd = busyStart + 400_000_000
			time.sleep(0.6)
			attachedAfter = lib.pwsink_attached_streams()
			ask("freeze")
			took.update(calls("frozen"))
			ask("thaw")
			waitPlayed(playing, len(tone(SPEECH, 0.9)) // SPEECH.blockAlign, timeout=10)
			playing.close()
			result = dict(
				took=took,
				busyStart=busyStart,
				busyEnd=busyEnd,
				attached=[attachedBefore, attachedWhileBusy, attachedAfter],
			)
			""",
			{**pipewire.childEnv(), **steps.env(), "BASEDREADER_PWSINK_TEST_HOOKS": "1"},
		)
	report("call times, ms", {k: round(v, 3) for k, v in value["took"].items()})
	before, whileBusy, after = value["attached"]
	assert whileBusy == before < after, "the library's thread was not held up while the calls were made"
	slow = {name: ms for name, ms in value["took"].items() if ms > 2}
	assert not slow, slow
	sound = monitor.soundAfter(value["busyStart"] - 200 * MS)
	assert sound < value["busyStart"]
	assert not [
		e for e in monitor.eventsOf("silence") if value["busyStart"] <= e["graph_ns"] <= value["busyEnd"]
	], "audio already playing stopped while the library's thread was busy"


def test_theLoopBlockingHookDoesNothingOutsideTests(pipewire: PipewireFixture, tmp_path: Path):
	"""pwsink_block_loop holds up every stream's connecting and waking, so it acts only where the
	tests ask for it by name."""
	value = runPipewireChild(
		tmp_path,
		"""
		lib.pwsink_block_loop(3000)
		time.sleep(0.02)
		before = lib.pwsink_attached_streams()
		stream = sink.openStream("", StreamFormat(1, 2, 44100, 16), lambda: None)
		start = mono()
		while lib.pwsink_attached_streams() == before and mono() - start < 2_000_000_000:
			time.sleep(0.005)
		stream.close()
		result = (mono() - start) / 1e6
		""",
		pipewire.childEnv(),
	)
	assert value < 500, f"a new stream waited {value:.0f} ms for the library's thread"


def test_theCrateNeverFlushesSetsControlsOrTouchesNodes():
	"""Stopping must stay a store, and no volume or parameter may be set on any node, where
	WirePlumber would remember it."""
	sources = sorted(CRATE.rglob("*.rs"))
	assert sources
	for path in sources:
		text = path.read_text(encoding="utf-8")
		for name in ("pw_stream_flush", "pw_stream_set_control", "pw_node_set_param"):
			assert name not in text, f"{path.relative_to(harness.ROOT)} uses {name}"


@pytest.mark.parametrize("library", ["", "/nonexistent/libpwsink.so"])
def test_aLibraryThatCannotBeLoadedFailsAtOnce(tmp_path: Path, library: str):
	"""A broken build says so when nvwave starts, rather than falling back to another sink."""
	done = harness.runChild(
		tmp_path,
		"""
		import NVDAHelper  # noqa: F401
		import nvwave

		try:
			nvwave.initialize()
			result = "started"
		except OSError as e:
			result = str(e)
		""",
		env={"BASEDREADER_AUDIO_SINK": "pipewire", "BASEDREADER_PWSINK_LIB": library},
	)
	done.assertClean()
	assert done.value != "started"
	assert re.search("BASEDREADER_PWSINK_LIB|libpwsink", done.value), done.value


def test_aStreamFailsCleanlyWhenMisused(pipewire: PipewireFixture, tmp_path: Path):
	"""A write that does not fit leaves nothing queued, and a closed stream raises rather than
	handing the library a handle it has freed, which would crash NVDA and its speech with it."""
	value = runPipewireChild(
		tmp_path,
		"""
		import errno

		stream = sink.openStream("", SPEECH, lambda: None)
		try:
			stream.write(bytes(SPEECH.blockAlign * (SPEECH.samplesPerSec + 1)))
			tooMuch = "accepted"
		except OSError as e:
			tooMuch = errno.errorcode[e.errno]
		queuedAfter = stream.bufferedFrames()
		stream.write(tone(SPEECH, 0.1))
		stream.close()
		stream.close()
		closed = {}
		for name, call in (
			("write", lambda: stream.write(bytes(2))),
			("bufferedFrames", stream.bufferedFrames),
			("playedFrames", stream.playedFrames),
			("start", stream.start),
			("stop", stream.stop),
			("abort", stream.abort),
			("reset", stream.reset),
			("setChannelVolume", lambda: stream.setChannelVolume(0, 0.5)),
		):
			try:
				call()
				closed[name] = "returned"
			except OSError as e:
				closed[name] = errno.errorcode[e.errno]
		result = dict(tooMuch=tooMuch, queuedAfter=queuedAfter, closed=closed)
		""",
		pipewire.childEnv(),
	)
	assert value["tooMuch"] == "ENOBUFS" and value["queuedAfter"] == 0, value
	assert set(value["closed"].values()) == {"EBADF"}, value["closed"]


def _fileLevels(path: Path) -> tuple[list[float], float, int]:
	"""Each channel's energy at 48 kHz, the peak, and the frames before the first sound."""
	with wave.open(str(path), "rb") as w:
		channels, width, rate = w.getnchannels(), w.getsampwidth(), w.getframerate()
		raw = w.readframes(w.getnframes())
	if width == 2:
		values = array("h")
		values.frombytes(raw)
		scale = 32768
	else:
		values = [int.from_bytes(raw[i : i + 3], "little", signed=True) for i in range(0, len(raw), 3)]
		scale = 8388608
	samples = [v / scale for v in values]
	perChannel = [samples[c::channels] for c in range(channels)]
	energies = [energy(s) * RATE / rate for s in perChannel]
	if channels == 1:
		energies *= 2
	lead = next((i // channels for i, v in enumerate(samples) if abs(v) > 0.001), 0)
	return energies, max(abs(v) for v in samples), round(lead * RATE / rate)


def test_nvdasSoundsAreHeardAtTheirLevelAndARepeatStartsAtOnce(pipewire: PipewireFixture, tmp_path: Path):
	"""All of NVDA's wave files and a beep reach the sink at the level they were made at, and a
	sound played again, on the stream its previous player left in the pool, starts within one
	quantum and 5 ms."""
	pipewire.setQuantum(512)
	monitor = pipewire.monitor()
	value = runPipewireChild(
		tmp_path,
		"""
		import tones
		from NVDAHelper import localLib

		tones.initialize()
		plays = []
		for name in sorted(os.listdir(PARAMS["waves"])):
			start = mono()
			nvwave.playWaveFile(os.path.join(PARAMS["waves"], name), asynchronous=False)
			plays.append(dict(name=name, start=start, end=mono()))
			time.sleep(0.05)
		repeats = []
		for i in range(6):
			start = mono()
			nvwave.playWaveFile(os.path.join(PARAMS["waves"], "error.wav"), asynchronous=False)
			repeats.append(start)
			time.sleep(0.1)
		start = mono()
		tones.beep(440, 200)
		tones.player.sync()
		beep = dict(start=start, end=mono())
		size = localLib.generateBeep(None, 440, 200, 50, 50)
		buffer = ctypes.create_string_buffer(size)
		localLib.generateBeep(buffer, 440, 200, 50, 50)
		beep["samples"] = list(array.array("h", buffer.raw))
		tones.terminate()
		gc.collect()
		result = dict(plays=plays, repeats=repeats, beep=beep)
		""",
		pipewire.childEnv(),
		params={"waves": str(WAVES)},
	)
	failures = []
	levels = {}
	for play in value["plays"]:
		energies, peak, _lead = _fileLevels(WAVES / play["name"])
		left, right = monitor.samples(play["start"], play["end"] + 30 * MS)
		for name, heard, expected in (("left", left, energies[0]), ("right", right, energies[-1])):
			off = 10 * math.log10(energy(heard) / expected)
			levels[f"{play['name']} {name}"] = round(off, 2)
			heardPeak = max(abs(x) for x in heard)
			# A wrong gain or format is 6 dB out or noise. Within a decibel leaves room for what
			# PipeWire's resampler filters out above about 10 kHz of a 22050 Hz file: textError.wav,
			# a third of whose energy lies above 9 kHz, comes out half a decibel down.
			if abs(off) > 1 or heardPeak > peak * 1.12 + 0.01:
				failures.append(
					f"{play['name']} {name}: {off:+.2f} dB, peak {heardPeak:.3f} against {peak:.3f}"
				)
	assert len(value["plays"]) == 16
	beep = value["beep"]
	beepLeft = [s / 32768 for s in beep["samples"][0::2]]
	expected = energy(beepLeft) * RATE / 44100
	left, right = monitor.samples(beep["start"], beep["end"] + 30 * MS)
	for name, heard in (("left", left), ("right", right)):
		off = 10 * math.log10(energy(heard) / expected)
		levels[f"beep {name}"] = round(off, 2)
		if abs(off) > 0.5:
			failures.append(f"beep {name}: {off:+.2f} dB")
	report("level heard against the source, dB", levels)
	assert not failures, failures
	_energies, _peak, lead = _fileLevels(WAVES / "error.wav")
	leadNs = lead * 1_000_000_000 // RATE
	starts = [(monitor.soundAfter(start) - start - leadNs) / MS for start in value["repeats"]]
	report("repeated error.wav, call to sound, ms", summary(starts[1:]))
	assert max(starts[1:]) < quantumMs(512) + 5
