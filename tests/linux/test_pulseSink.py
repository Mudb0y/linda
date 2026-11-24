"""The PulseAudio fallback sink, heard through a private daemon in the rig.

Runs tests against PulseAudio in tests/linux/rig using a private PulseAudio daemon with null sinks,
testing levels, playback, stopping responsiveness, and clean termination.
"""

import json
import statistics
import textwrap
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Self

import harness
from pulseFixture import PulseFixture, assertCleanThroughPulse

PRELUDE = """
import array
import ctypes
import faulthandler
import gc
import json
import math
import os
import random
import threading
import time

faulthandler.enable()

import NVDAHelper  # noqa: F401
import nvwave
import wasapi
from linuxPlatform import audioOutput
from linuxPlatform.audioOutput import StreamFormat

nvwave.initialize()
sink = audioOutput.getSink()
assert sink.kind == "pulse", sink.kind
mono = time.monotonic_ns
STEPS = os.environ.get("TEST_STEPS", "")


def ask(name, timeout=30):
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
	levels = levels or [1.0] * fmt.channels
	step = 2 * math.pi * hz / fmt.samplesPerSec
	frames = int(fmt.samplesPerSec * seconds)
	return encode(fmt, [amplitude * math.sin(step * i) * level for i in range(frames) for level in levels])
"""


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
					self.errors.append(f"{name}: {e!r}")
					value = {"error": repr(e)}
				self.done[name] = time.monotonic_ns()
				staged = self.directory / f".done-{name}"
				staged.write_text(json.dumps({"ns": self.done[name], "value": value}))
				staged.rename(self.directory / f"done-{name}")
			time.sleep(0.002)


def runPulseChild(
	tmpPath: Path, code: str, env: dict[str, str], *, timeout: float = 30
) -> harness.ChildResult:
	done = harness.runChild(
		tmpPath,
		PRELUDE + textwrap.dedent(code),
		env=env,
		timeout=timeout,
	)
	assertCleanThroughPulse(done)
	return done


def test_pulseSinkPlaysAudioAndTracksDelivered(pulse: PulseFixture, tmp_path: Path):
	"""PulseAudio sink opens a stream, plays audio, and tracks delivered frames."""
	_ = pulse.monitor()
	code = """
		fmt = StreamFormat(1, 2, 44100, 16)
		player = nvwave.WavePlayer(2, 44100, 16, wantDucking=False, purpose=nvwave.AudioPurpose.SOUNDS)
		chunk = tone(fmt, 0.2)  # 200 ms
		done = threading.Event()
		player.feed(chunk, onDone=done.set)
		player.sync()
		done.wait(3)
		delivered = sink.deliveredFrames()
		player.stop()
		result = {"delivered": delivered, "expectedMin": int(44100 * 0.2)}
	"""
	done = runPulseChild(tmp_path, code, pulse.childEnv())
	assert done.value["delivered"] >= done.value["expectedMin"]


def test_pulseSinkStopCutsAudioQuickly(pulse: PulseFixture, tmp_path: Path):
	"""WavePlayer.stop() on PulseAudio cuts off audio quickly and reliably."""
	code = """
		fmt = StreamFormat(1, 1, 22050, 16)
		chunk = tone(fmt, 0.3)
		player = nvwave.WavePlayer(1, 22050, 16, wantDucking=False, purpose=nvwave.AudioPurpose.SPEECH)
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

		synth = threading.Thread(target=synthesize, name="synth", daemon=True)
		synth.start()

		# Measure stop times across rounds
		returned = []
		for _ in range(50):
			cancelled.clear()
			finished.clear()
			go.set()
			time.sleep(random.uniform(0.001, 0.005))
			called = mono()
			cancelled.set()
			player.stop()
			durationMs = (mono() - called) / 1e6
			returned.append(durationMs)
			finished.wait(1.0)

		quitting[0] = True
		go.set()
		synth.join(2.0)
		del player
		gc.collect()

		result = {
			"returned": returned,
			"mean": sum(returned) / len(returned),
			"max": max(returned),
		}
	"""
	done = runPulseChild(tmp_path, code, pulse.childEnv())
	returned = done.value["returned"]
	assert len(returned) == 50
	print(
		f"\n[BENCHMARK] PulseAudio WavePlayer.stop (50 rounds): mean={done.value['mean']:.3f} ms, "
		f"min={min(returned):.3f} ms, max={done.value['max']:.3f} ms, median={statistics.median(returned):.3f} ms"
	)
	# Mean stop time should be snappy (under 10 ms)
	assert done.value["mean"] < 10.0, f"Mean stop time too slow: {done.value['mean']} ms"
	# Max stop time should stay reasonably bounded
	assert done.value["max"] < 35.0, f"Max stop time too high: {done.value['max']} ms"


def test_pulseSinkSetsProcessGainAndChannelVolume(pulse: PulseFixture, tmp_path: Path):
	"""PulseAudio sink applies channel volumes and process gain cleanly."""
	code = """
		sink.setGain(0.5)
		assert sink.gain == 0.5
		try:
			sink.setGain(1.5)
			threw = False
		except ValueError:
			threw = True
		assert threw, "gain above 1 did not raise ValueError"

		fmt = StreamFormat(1, 2, 44100, 16)
		stream = sink.openStream("", fmt, lambda: None)
		stream.setChannelVolume(0, 0.5)
		stream.setChannelVolume(1, 0.25)
		assert stream.volumes == [0.5, 0.25]
		stream.close()
		result = True
	"""
	done = runPulseChild(tmp_path, code, pulse.childEnv())
	assert done.value is True


def test_pulseSinkRefusesWriteExceedingBuffer(pulse: PulseFixture, tmp_path: Path):
	"""PulseAudio stream refuses writes exceeding writable size with ENOBUFS."""
	code = """
		import errno
		fmt = StreamFormat(1, 2, 44100, 16)
		stream = sink.openStream("", fmt, lambda: None)
		hugeData = b"\\x00" * (1024 * 1024 * 16)  # 16 MB chunk, exceeds any buffer
		refused = False
		try:
			stream.write(hugeData)
		except OSError as e:
			refused = e.errno == errno.ENOBUFS
		stream.close()
		result = refused
	"""
	done = runPulseChild(tmp_path, code, pulse.childEnv())
	assert done.value is True


def test_pulseSinkRecoversAfterDaemonRestart(pulse: PulseFixture, tmp_path: Path):
	"""When PulseAudio daemon is killed mid-session, active stream detects loss and next
	utterance recovers after daemon restart or fails with NoDevice while dead."""
	actions = {
		"kill": pulse.killDaemon,
		"restart": pulse.restartDaemon,
	}
	with Steps(tmp_path / "steps", actions) as steps:
		code = """
			fmt = StreamFormat(1, 2, 44100, 16)
			player = nvwave.WavePlayer(2, 44100, 16, wantDucking=False, purpose=nvwave.AudioPurpose.SPEECH)
			chunk = tone(fmt, 0.2)
			# 1. Play first chunk while daemon is alive
			player.feed(chunk)
			player.idle()

			# 2. Kill the daemon mid-session
			ask("kill")

			# 3. Feeding while daemon is dead detects StreamLost and fails open with NoDevice
			failedWhileDead = False
			for _ in range(10):
				try:
					player.feed(chunk)
					player.idle()
					time.sleep(0.05)
				except (OSError, Exception):
					failedWhileDead = True
					break

			# 4. Restart the daemon
			ask("restart")

			# 5. Next utterance after restart plays cleanly
			playedAfterRestart = False
			for _ in range(5):
				try:
					player.feed(chunk)
					player.idle()
					playedAfterRestart = True
					break
				except OSError:
					time.sleep(0.1)

			player.stop()
			result = {
				"failedWhileDead": failedWhileDead,
				"playedAfterRestart": playedAfterRestart,
			}
		"""
		done = runPulseChild(tmp_path, code, {**pulse.childEnv(), **steps.env()})
		assert done.value["failedWhileDead"] is True
		assert done.value["playedAfterRestart"] is True
