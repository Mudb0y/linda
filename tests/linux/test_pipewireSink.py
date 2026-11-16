"""The PipeWire sink, heard through a private daemon in the rig.

	nix develop . -c env BASEDREADER_PWSINK_LIB=$PWD/engine/target/release/libpwsink.so \\
		BASEDREADER_PWMON=$PWD/engine/target/release/pwmon \\
		tests/linux/rig python3 -m pytest tests/linux/test_pipewireSink.py

Every case plays from a fresh NVDA process, through nvwave and the wasapi replacement or straight
into the sink, to the null sinks of tests/linux/pipewireFixture.py, and checks what their monitor
recorded: levels, timing, and what happens when PipeWire stops consuming or goes away. The sinks
follow the graph's driver, so the monitor hears each cycle in that cycle and a latency measured on
it is the sink's own.
"""

import json
import math
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
from pipewireFixture import RATE, PipewireFixture, runningPipewire  # noqa: F401

CRATE = harness.ROOT / "engine" / "crates" / "pwsink"
WAVES = harness.SOURCE / "waves"
MS = 1_000_000

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
	timeout: float = 300,
) -> object:
	"""Runs code after PRELUDE in a harness child, requires it to have been clean, and returns its
	result.

	The one place a child that maps libpipewire-0.3 is started, so that a harness that refuses sound
	libraries in children is told here that this one is expected.
	"""
	done = harness.runChild(
		tmpPath,
		PRELUDE + textwrap.dedent(code),
		env={**env, "TEST_PARAMS": json.dumps(params or {})},
		timeout=timeout,
	)
	done.assertClean()
	assert done.returnCode == 0, done.stderr[-3000:]
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
			result = dict(start=start, props=props)
			""",
			{**pipewire.childEnv(), **steps.env()},
		)
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

	At 48 kHz nothing is resampled and the bounds hold as they are. At 22050 Hz, eSpeak's rate,
	PipeWire's resampler delays every frame by the few it holds, which it reports, and they play
	after a stop as after anything else; the bounds are moved by that much."""
	pipewire.setQuantum(256)
	monitor = pipewire.monitor()
	actions = {f"quantum{q}": (lambda q=q: pipewire.setQuantum(q)) for q in (256, 512)}
	with Steps(tmp_path / "steps", actions) as steps:
		value = runPipewireChild(
			tmp_path,
			"""
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
					for i in range(30):
						start = mono()
						player.feed(chunk)
						time.sleep(random.uniform(0.05, 0.2))
						stop = mono()
						player.stop()
						rounds.append(dict(quantum=quantum, rate=rate, start=start, stop=stop, stopCall=mono() - stop))
						time.sleep(0.08)
					handle = wasapi._players[player._player]._stream.handle
					resampler[f"{quantum} {rate}"] = lib.pwsink_resampler_frames(handle)
			del players, player
			gc.collect()
			result = dict(rounds=rounds, resampler=resampler)
			""",
			{**pipewire.childEnv(), **steps.env()},
		)
	measured = {}
	failures = []
	for quantum in (256, 512):
		for rate in (48000, 22050):
			mine = [r for r in value["rounds"] if r["quantum"] == quantum and r["rate"] == rate]
			starts = [(monitor.soundAfter(r["start"]) - r["start"]) / MS for r in mine]
			stops = [
				(monitor.silenceAfter(r["stop"] - quantum * 1_000_000_000 // RATE) - r["stop"]) / MS
				for r in mine
			]
			assert min(stops) > -0.1, stops
			delay = value["resampler"][f"{quantum} {rate}"] / rate * 1000
			row = {
				"startToSound": summary(starts),
				"stopToSilence": summary(stops),
				"resamplerMs": round(delay, 2),
			}
			row["stopCall"] = summary([r["stopCall"] / MS for r in mine])
			measured[f"quantum {quantum}, {rate} Hz"] = row
			for name, bounds in (("startToSound", START_BOUNDS), ("stopToSilence", STOP_BOUNDS)):
				for statistic, bound in zip(("median", "p95", "max"), bounds[quantum]):
					if bound is not None and row[name][statistic] > bound + delay:
						failures.append(
							f"{name} {statistic} at {quantum}, {rate} Hz: {row[name][statistic]} ms"
						)
	report("stop and start", measured)
	assert value["resampler"]["512 48000"] == 0
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
	assert first < 500 + 300 + 150, first
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
		for i in range(40):
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
	assert value["inactiveBefore"] > 10, "the stream was rarely inactive, so this showed little"
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
	but whole, since the clock waits for the daemon before it discards anything."""
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
			data = tone(SPEECH, 0.12)
			frames = len(data) // SPEECH.blockAlign
			stream = sink.openStream("", SPEECH, lambda: None)
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
				start = mono()
				stream.write(data)
				stream.start()
				time.sleep(0.15)
				ask(f"thaw{i}")
				waitPlayed(stream, frames)
				sounds.append(dict(start=start, asleep=asleep))
			stream.close()
			result = sounds
			""",
			{**pipewire.childEnv(), **steps.env()},
		)
	heard = []
	for sound in value:
		start = monitor.soundAfter(sound["start"])
		heard.append(round((monitor.silenceAfter(start) - start) / MS, 2))
	report("120 ms sounds woken while the daemon was frozen for 150 ms, heard for ms", heard)
	assert all(sound["asleep"] for sound in value), "the stream was not asleep, so this showed nothing"
	assert all(ms > 120 - 1 for ms in heard), heard


def test_stopNeverWaitsWhileOtherStreamsOpenAndClose(pipewire: PipewireFixture, tmp_path: Path):
	"""stop() is one store: 10,000 calls while another thread keeps opening and closing streams,
	which keeps the library's thread busy in PipeWire, never take 0.2 ms.

	The calls go through ctypes.PyDLL, which keeps the GIL, so that the time measured is the
	library's and not the wait for the GIL after a call that released it."""
	value = runPipewireChild(
		tmp_path,
		"""
		fast = ctypes.PyDLL(os.environ["BASEDREADER_PWSINK_LIB"])
		fast.pwsink_stop.argtypes = [ctypes.c_void_p]
		fast.pwsink_stop.restype = None
		stream = sink.openStream("", SPEECH, lambda: None)
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
		handle = stream.handle
		took = []
		clock = time.perf_counter_ns
		for i in range(10000):
			start = clock()
			fast.pwsink_stop(handle)
			took.append(clock() - start)
			stream.start()
			time.sleep(0.0001)
		running = False
		churner.join()
		stream.close()
		took.sort()
		result = dict(median=took[len(took) // 2], worst=took[-1], churned=churned[0], attached=lib.pwsink_attached_streams())
		""",
		pipewire.childEnv(),
	)
	report("stop under contention, ns", value)
	assert value["churned"] > 100
	assert value["worst"] < 200_000


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
			lib.pwsink_block_loop(500)
			time.sleep(0.02)
			busyStart = mono()
			took = calls("busy")
			# The loop thread stays blocked for another 450 ms or so, in which the tone plays on.
			busyEnd = busyStart + 400_000_000
			time.sleep(0.6)
			ask("freeze")
			took.update(calls("frozen"))
			ask("thaw")
			waitPlayed(playing, len(tone(SPEECH, 0.9)) // SPEECH.blockAlign, timeout=10)
			playing.close()
			result = dict(took=took, busyStart=busyStart, busyEnd=busyEnd)
			""",
			{**pipewire.childEnv(), **steps.env()},
		)
	report("call times, ms", {k: round(v, 3) for k, v in value["took"].items()})
	slow = {name: ms for name, ms in value["took"].items() if ms > 2}
	assert not slow, slow
	sound = monitor.soundAfter(value["busyStart"] - 200 * MS)
	assert sound < value["busyStart"]
	assert not [
		e for e in monitor.eventsOf("silence") if value["busyStart"] <= e["graph_ns"] <= value["busyEnd"]
	], "audio already playing stopped while the library's thread was busy"


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
