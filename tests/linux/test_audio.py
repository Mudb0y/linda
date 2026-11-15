"""Audio on Linux: the wasapi replacement, its sinks, and the modules above them.

nix develop . -c tests/linux/rig python -m pytest tests/linux/test_audio.py

Every case runs in a fresh interpreter, enters NVDA's import graph where core.main does, and ends by
checking from the process's own maps and file descriptors that no sound device was opened and no
sound library loaded. The timings asserted are loose enough for a loaded machine but tight enough
to tell a player that paces its feeders from one that does not.
"""

import textwrap
import wave
from pathlib import Path

import harness

WAVES = harness.SOURCE / "waves"

#: Opens nvwave the way core.main does, and watches the process for anything that could make sound.
PRELUDE = """
import gc
import os
import sys
import threading
import time

_soundLibraries = (
	"libasound", "libpulse", "libpipewire", "libjack", "libportaudio", "libpcaudio", "libopenal",
	"libSDL", "libcanberra", "libao.", "libsndio", "libespeak", "libgst",
)
_soundOpens = []


def _isSound(path):
	return path.startswith("/dev/snd") or any(name in os.path.basename(path) for name in _soundLibraries)


def _watchForSound(event, args):
	if event == "open" and isinstance(args[0], (str, bytes)) and _isSound(os.fsdecode(args[0])):
		_soundOpens.append(os.fsdecode(args[0]))
	elif event == "ctypes.dlopen" and args[0] and _isSound(os.fsdecode(args[0])):
		_soundOpens.append(f"dlopen {args[0]}")


def _soundMappings():
	paths = set()
	with open("/proc/self/maps", encoding="utf-8") as maps:
		for line in maps:
			fields = line.split(None, 5)
			if len(fields) == 6:
				paths.add(fields[5].strip())
	for fd in os.listdir("/proc/self/fd"):
		try:
			paths.add(os.readlink(f"/proc/self/fd/{fd}"))
		except OSError:
			pass
	return len(paths), sorted(path for path in paths if _isSound(path))


sys.addaudithook(_watchForSound)

# NVDAHelper first, as core.main imports it: NVDA's import graph resolves only when entered there.
import NVDAHelper  # noqa: E402, F401
import nvwave  # noqa: E402
import wasapi  # noqa: E402

nvwave.initialize()
_soundBeforeAudio = _soundMappings()[1]
events = []


def done(tag):
	def onDone():
		events.append((tag, threading.current_thread().name, time.monotonic()))

	return onDone
"""

#: Run last: what in this process could have reached a sound device, and what the audio added.
ISOLATION = """
_mappedCount, _soundAfterAudio = _soundMappings()
isolation = {
	"mappedFiles": _mappedCount,
	"soundAtStart": _soundBeforeAudio,
	"addedByAudio": sorted(set(_soundAfterAudio) - set(_soundBeforeAudio)),
	"opened": _soundOpens,
}
"""

#: Sound libraries the import graph maps before any audio is played, none of them through audio
#: code: gui.message imports wx.html2, whose extension links WebKitGTK, which links GStreamer and,
#: through it, libpulse. Nothing calls them. Any other sound library, or any of these mapped by
#: playing, fails the test.
IMPORTED_WITH_WEBVIEW = ("libgst", "libpulse")

#: Workstream A4 owns NVDAHelper.localLib.generateBeep, whose Linux body has not landed and raises.
#: This stands in for that one function only: a port of nvdaHelper/local/beeps.cpp, same arguments,
#: same samples.
GENERATE_BEEP_STUB = """
import array
import ctypes
import math

from NVDAHelper import localLib


def generateBeep(buf, hz, length, left=50, right=50):
	sampleRate, amplitude = 44100, 14000
	samplesPerCycle = int(sampleRate / hz)
	totalSamples = int((length / 1000.0) / (1.0 / sampleRate))
	totalSamples += samplesPerCycle - (totalSamples % samplesPerCycle)
	if buf is None:
		return totalSamples * 4
	lpan, rpan = (left / 100.0) * amplitude, (right / 100.0) * amplitude
	sinFreq = (2.0 * math.pi) / (sampleRate / hz)
	out = array.array("h", bytes(totalSamples * 4))
	for n in range(totalSamples):
		sample = min(max(math.sin((n % sampleRate) * sinFreq) * 2.0, -1.0), 1.0)
		out[2 * n] = int(sample * lpan)
		out[2 * n + 1] = int(sample * rpan)
	ctypes.memmove(buf, out.tobytes(), totalSamples * 4)
	return totalSamples * 4


localLib.generateBeep = generateBeep
"""


def runAudio(tmpPath: Path, code: str, *, captureDir: Path | None = None, extra: str = "") -> dict:
	"""Run code after PRELUDE and return its result, with the isolation findings merged in."""
	body = (
		textwrap.dedent(PRELUDE)
		+ textwrap.dedent(extra)
		+ textwrap.dedent(code)
		+ textwrap.dedent(ISOLATION)
		+ "result = {'value': result, 'isolation': isolation}\n"
	)
	env = {"BASEDREADER_AUDIO_CAPTURE_DIR": str(captureDir) if captureDir else ""}
	done = harness.runChild(tmpPath, "result = None\n" + body, env=env)
	done.assertClean()
	isolation = done.value["isolation"]
	assert isolation["mappedFiles"] > 0
	assert isolation["opened"] == []
	assert isolation["addedByAudio"] == []
	for path in isolation["soundAtStart"]:
		assert not path.startswith("/dev/snd"), path
		assert Path(path).name.startswith(IMPORTED_WITH_WEBVIEW), path
	return done.value["value"]


def captured(captureDir: Path) -> list[dict]:
	files = []
	for path in sorted(captureDir.iterdir()):
		with wave.open(str(path), "rb") as w:
			files.append(
				{
					"name": path.name,
					"channels": w.getnchannels(),
					"rate": w.getframerate(),
					"width": w.getsampwidth(),
					"frames": w.readframes(w.getnframes()),
				},
			)
	return files


def test_backEndAloneMapsNothingThatCouldSound(tmp_path: Path):
	"""Without NVDA's GUI in the process, so that no sound library is there to start with, playing
	through the back end, to either sink, maps no sound library and opens no sound device."""
	captureDir = tmp_path / "capture"
	captureDir.mkdir()
	code = """
		import ctypes
		import os

		import wasapi
		from winBindings.mmeapi import WAVEFORMATEX

		calls = []
		callback = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_uint)(lambda h, i: calls.append(i))
		fmt = WAVEFORMATEX(wFormatTag=1, nChannels=1, nSamplesPerSec=8000, wBitsPerSample=16, nBlockAlign=2)
		wasapi.wasPlay_startup()
		player = wasapi.wasPlay_create("", fmt, callback)
		wasapi.wasPlay_open(player)
		feedId = ctypes.c_uint()
		data = bytes(range(256)) * 4
		wasapi.wasPlay_feed(player, ctypes.cast(data, ctypes.c_char_p), len(data), ctypes.byref(feedId))
		wasapi.wasPlay_sync(player)
		wasapi.wasPlay_destroy(player)
		mapped = set()
		with open("/proc/self/maps", encoding="utf-8") as maps:
			for line in maps:
				fields = line.split(None, 5)
				if len(fields) == 6:
					mapped.add(fields[5].strip())
		for fd in os.listdir("/proc/self/fd"):
			try:
				mapped.add(os.readlink(f"/proc/self/fd/{fd}"))
			except OSError:
				pass
		sound = (
			"/dev/snd", "libasound", "libpulse", "libpipewire", "libjack", "libportaudio", "libpcaudio",
			"libopenal", "libSDL", "libcanberra", "libsndio", "libgst",
		)
		result = {
			"calls": calls,
			"mapped": len(mapped),
			"sound": sorted(path for path in mapped if any(name in path for name in sound)),
			"wx": "wx" in sys.modules,
		}
		"""
	for env in ({"BASEDREADER_AUDIO_CAPTURE_DIR": ""}, {"BASEDREADER_AUDIO_CAPTURE_DIR": str(captureDir)}):
		done = harness.runChild(
			tmp_path, "import sys\n" + textwrap.dedent(code), prepare="bootstrap", env=env
		)
		done.assertClean()
		assert done.value["calls"] == [0]
		assert done.value["mapped"] > 0
		assert done.value["sound"] == []
		assert not done.value["wx"]
	[got] = captured(captureDir)
	assert got["frames"] == bytes(range(256)) * 4


def test_feedPacesFeederAndCallsBackOnItsThread(tmp_path: Path):
	"""feed blocks while more than half the buffer is queued, and each callback runs on the feeding
	thread, in order, once its audio has played and only after its feed has returned."""
	value = runAudio(
		tmp_path,
		"""
		player = nvwave.WavePlayer(1, 8000, 16, wantDucking=False, purpose=nvwave.AudioPurpose.SOUNDS)
		chunk = bytes(1600)  # 100 ms
		order = []
		start = time.monotonic()

		def feedAll():
			for i in range(10):
				player.feed(chunk, onDone=done(i))
				order.append(("returned", i, time.monotonic() - start))
			player.sync()
			order.append(("synced", None, time.monotonic() - start))

		feeder = threading.Thread(target=feedAll, name="feeder")
		feeder.start()
		feeder.join()
		calls = [(tag, thread, at - start) for tag, thread, at in events]
		merged = sorted(order + [("done", tag, at) for tag, _thread, at in calls], key=lambda e: e[2])
		result = {"order": order, "calls": calls, "merged": [e[:2] for e in merged]}
		""",
	)
	calls = value["calls"]
	assert [tag for tag, _thread, _at in calls] == list(range(10))
	assert {thread for _tag, thread, _at in calls} == {"feeder"}
	for tag, _thread, at in calls:
		assert (tag + 1) * 0.1 - 0.005 <= at <= (tag + 1) * 0.1 + 0.15, (tag, at)
	returned = {i: at for kind, i, at in value["order"] if kind == "returned"}
	# Three chunks fit before the queue passes half of 400 ms; after that each feed waits 100 ms.
	assert returned[2] < 0.05
	assert 0.6 <= returned[9] <= 0.85
	synced = value["order"][-1][2]
	assert 0.99 <= synced <= 1.25
	merged = value["merged"]
	for i in range(10):
		assert merged.index(["returned", i]) < merged.index(["done", i])


def test_callbacksRunOnlyInsideFeedSyncOrIdle(tmp_path: Path):
	value = runAudio(
		tmp_path,
		"""
		player = nvwave.WavePlayer(1, 8000, 16, wantDucking=False, purpose=nvwave.AudioPurpose.SOUNDS)
		chunk = bytes(1600)  # 100 ms
		player.feed(chunk, onDone=done("a"))
		time.sleep(0.3)
		firedUnattended = list(events)
		syncer = threading.Thread(target=player.sync, name="syncer")
		syncer.start()
		syncer.join()
		player.feed(chunk, onDone=done("b"))
		idStored = list(player._doneCallbacks)
		start = time.monotonic()
		idler = threading.Thread(target=wasapi.wasPlay_idle, args=(player._player,), name="idler")
		idler.start()
		idler.join()
		idleTook = time.monotonic() - start
		# wasPlay_idle stops and resets the stream, so feed ids start again from 0.
		player.feed(chunk, onDone=done("c"))
		idAfterIdle = list(player._doneCallbacks)
		player.sync()
		result = {
			"firedUnattended": firedUnattended,
			"calls": [(tag, thread) for tag, thread, _at in events],
			"idStored": idStored,
			"idleTook": idleTook,
			"idAfterIdle": idAfterIdle,
		}
		""",
	)
	assert value["firedUnattended"] == []
	assert value["calls"] == [["a", "syncer"], ["b", "idler"], ["c", "MainThread"]]
	assert value["idStored"] == [1]
	assert value["idAfterIdle"] == [0]
	assert 0.08 <= value["idleTook"] <= 0.25


def test_stopWakesFeederAndDiscardsPendingCallbacks(tmp_path: Path):
	value = runAudio(
		tmp_path,
		"""
		player = nvwave.WavePlayer(1, 8000, 16, wantDucking=False, purpose=nvwave.AudioPurpose.SOUNDS)
		chunk = bytes(1600)  # 100 ms
		stopping = threading.Event()
		feeds = []

		def feedAll():
			for i in range(10):
				player.feed(chunk, onDone=done(i))
				feeds.append(i)
				if stopping.is_set():
					break

		feeder = threading.Thread(target=feedAll, name="feeder")
		feeder.start()
		time.sleep(0.35)
		stopping.set()
		stopCalled = time.monotonic()
		player.stop()
		stopTook = time.monotonic() - stopCalled
		feeder.join()
		feederTook = time.monotonic() - stopCalled
		time.sleep(0.5)
		player.sync()
		firedBeforeNew = [tag for tag, _thread, _at in events]
		lateCalls = [tag for tag, _thread, at in events if at > stopCalled]
		player.feed(chunk, onDone=done("new"))
		player.sync()
		result = {
			"stopTook": stopTook,
			"feederTook": feederTook,
			"fed": len(feeds),
			"firedBeforeNew": firedBeforeNew,
			"lateCalls": lateCalls,
			"calls": [(tag, thread) for tag, thread, _at in events],
		}
		""",
	)
	assert value["stopTook"] < 0.02
	assert value["feederTook"] < 0.05
	fired = value["firedBeforeNew"]
	# The chunks that had played by the time of the stop, about three, and nothing after it.
	assert fired == list(range(len(fired))) and 2 <= len(fired) <= 4
	assert value["fed"] < 10
	assert value["lateCalls"] == []
	assert value["calls"][-1] == ["new", "MainThread"]
	assert [tag for tag, _thread in value["calls"]].count("new") == 1


def test_pauseHoldsPlaybackAndResumeContinuesIt(tmp_path: Path):
	value = runAudio(
		tmp_path,
		"""
		player = nvwave.WavePlayer(1, 8000, 16, wantDucking=False, purpose=nvwave.AudioPurpose.SOUNDS)
		player.feed(bytes(4800), onDone=done("end"))  # 300 ms
		player.pause(True)
		syncer = threading.Thread(target=player.sync, name="syncer")
		syncer.start()
		time.sleep(0.5)
		blockedWhilePaused = syncer.is_alive()
		firedWhilePaused = list(events)
		player.pause(False)
		resumed = time.monotonic()
		syncer.join(3)
		result = {
			"blockedWhilePaused": blockedWhilePaused,
			"firedWhilePaused": firedWhilePaused,
			"calls": [(tag, thread, at - resumed) for tag, thread, at in events],
		}
		""",
	)
	assert value["blockedWhilePaused"]
	assert value["firedWhilePaused"] == []
	[(tag, thread, afterResume)] = value["calls"]
	assert (tag, thread) == ("end", "syncer")
	assert 0.25 <= afterResume <= 0.45


def test_waveFilesPlayToCaptureFrameForFrame(tmp_path: Path):
	"""playWaveFile, synchronous and asynchronous, through the back end to the capture sink."""
	captureDir = tmp_path / "capture"
	captureDir.mkdir()
	syncFile, asyncFile = WAVES / "browseMode.wav", WAVES / "clipboardPush.wav"
	value = runAudio(
		tmp_path,
		f"""
		start = time.monotonic()
		nvwave.playWaveFile({str(syncFile)!r}, asynchronous=False)
		syncTook = time.monotonic() - start
		gc.collect()
		start = time.monotonic()
		nvwave.playWaveFile({str(asyncFile)!r}, asynchronous=True)
		asyncReturned = time.monotonic() - start
		nvwave.fileWavePlayerThread.join(5)
		asyncTook = time.monotonic() - start
		gc.collect()
		result = {{"syncTook": syncTook, "asyncReturned": asyncReturned, "asyncTook": asyncTook}}
		""",
		captureDir=captureDir,
	)
	assert 0.164 <= value["syncTook"] <= 0.4
	assert value["asyncReturned"] < 0.1
	assert 0.3 <= value["asyncTook"] <= 0.6
	files = captured(captureDir)
	assert len(files) == 2
	for source, got in zip((syncFile, asyncFile), files):
		with wave.open(str(source), "rb") as w:
			expected = (w.getnchannels(), w.getframerate(), w.getsampwidth(), w.readframes(w.getnframes()))
		assert (got["channels"], got["rate"], got["width"]) == expected[:3]
		assert got["frames"] == expected[3], source.name


def test_beepReachesCapture(tmp_path: Path):
	"""tones.beep returns at once; the player's sync waits for it; a beep cut off by the next keeps
	only what was heard."""
	captureDir = tmp_path / "capture"
	captureDir.mkdir()
	value = runAudio(
		tmp_path,
		"""
		import tones

		tones.initialize()
		start = time.monotonic()
		tones.beep(440, 100)
		beepReturned = time.monotonic() - start
		tones.player.sync()
		synced = time.monotonic() - start
		longStarted = time.monotonic()
		# Returns once the last of the beep is queued: the queue takes 400 ms at once, and feed waits
		# while more than half of it is full, so after 200 ms the remaining 100 ms fits.
		tones.beep(440, 500)
		longReturned = time.monotonic() - longStarted
		time.sleep(0.05)
		heardBeforeCut = time.monotonic() - longStarted
		tones.beep(880, 50, left=100, right=0)
		tones.player.sync()
		tones.terminate()
		gc.collect()
		buffers = []
		for hz, length, left, right in ((440, 100, 50, 50), (440, 500, 50, 50), (880, 50, 100, 0)):
			buf = ctypes.create_string_buffer(localLib.generateBeep(None, hz, length, left, right))
			localLib.generateBeep(buf, hz, length, left, right)
			buffers.append(buf.raw.hex())
		result = {
			"beepReturned": beepReturned,
			"synced": synced,
			"longReturned": longReturned,
			"heardBeforeCut": heardBeforeCut,
			"buffers": buffers,
		}
		""",
		captureDir=captureDir,
		extra=GENERATE_BEEP_STUB,
	)
	assert value["beepReturned"] < 0.08
	assert 0.1 <= value["synced"] <= 0.3
	first, interrupted, last = (bytes.fromhex(b) for b in value["buffers"])
	files = captured(captureDir)
	assert [(f["channels"], f["rate"], f["width"]) for f in files] == [(2, 44100, 2)] * 3
	assert files[0]["frames"] == first
	assert 0.19 <= value["longReturned"] <= 0.3
	cut = files[1]["frames"]
	assert interrupted.startswith(cut)
	heard = len(cut) / 4 / 44100
	# Generating each beep in Python takes some milliseconds on either side of the measured span.
	assert value["heardBeforeCut"] - 0.05 <= heard <= value["heardBeforeCut"] + 0.05
	assert heard < 0.5
	assert files[2]["frames"] == last


def test_leadingSilenceIsTrimmedWithOneSilentFrameKept(tmp_path: Path):
	captureDir = tmp_path / "capture"
	captureDir.mkdir()
	runAudio(
		tmp_path,
		"""
		import struct

		# A speech player: trimming is on by default.
		player = nvwave.WavePlayer(1, 8000, 16, wantDucking=False)
		tone = struct.pack("<800h", *([1000] * 800))
		player.feed(bytes(1600) + tone)
		player.sync()
		player.stop()
		# Stopping re-arms trimming: a chunk of silence alone still sends one silent frame.
		player.feed(bytes(1600))
		player.feed(tone)
		player.sync()
		del player
		gc.collect()
		result = None
		""",
		captureDir=captureDir,
	)
	files = captured(captureDir)
	silentFrame = bytes(2)
	tone = (1000).to_bytes(2, "little", signed=True) * 800
	assert [f["frames"] for f in files] == [silentFrame + tone, silentFrame + silentFrame + tone]


def test_volumeIsAppliedToWhatIsHeard(tmp_path: Path):
	captureDir = tmp_path / "capture"
	captureDir.mkdir()
	runAudio(
		tmp_path,
		"""
		import struct

		# A speech player, because a sounds player puts the configured sound volume back on every feed.
		player = nvwave.WavePlayer(2, 8000, 16, wantDucking=False)
		player.enableTrimmingLeadingSilence(False)
		player.setVolume(left=0.5, right=0.25)
		player.feed(struct.pack("<1600h", *([1000, -1000] * 800)))
		player.sync()
		del player
		gc.collect()
		result = None
		""",
		captureDir=captureDir,
	)
	[got] = captured(captureDir)
	frame = (500).to_bytes(2, "little", signed=True) + (-250).to_bytes(2, "little", signed=True)
	assert got["frames"] == frame * 800


def test_lostStreamIsReopenedAndStrandsNoCallback(tmp_path: Path):
	"""What a sink that can lose its device, as PipeWire's will, gets from the player."""
	value = runAudio(
		tmp_path,
		"""
		from linuxPlatform import audioOutput
		from linuxPlatform.audioOutput.nullSink import NullSink, NullStream

		class LosingStream(NullStream):
			lose = False

			def bufferedFrames(self):
				if LosingStream.lose:
					LosingStream.lose = False
					raise audioOutput.StreamLost("device went away")
				return super().bufferedFrames()

		class LosingSink(NullSink):
			opened = 0

			def openStream(self, deviceId, fmt, wake):
				LosingSink.opened += 1
				return LosingStream(fmt)

		audioOutput._sink = LosingSink()
		player = nvwave.WavePlayer(1, 8000, 16, wantDucking=False, purpose=nvwave.AudioPurpose.SOUNDS)
		player.feed(bytes(8000), onDone=done("before"))  # 500 ms
		LosingStream.lose = True
		start = time.monotonic()
		player.feed(bytes(1600), onDone=done("after"))
		firedByFeed = [tag for tag, _thread, _at in events]
		player.sync()
		result = {
			"opened": LosingSink.opened,
			"firedByFeed": firedByFeed,
			"calls": [(tag, at - start) for tag, _thread, at in events],
		}
		""",
	)
	assert value["opened"] == 2
	assert value["firedByFeed"] == ["before"]
	assert [tag for tag, _at in value["calls"]] == ["before", "after"]
	assert 0.08 <= value["calls"][1][1] <= 0.25


def test_missingCaptureDirectoryFailsAtStart(tmp_path: Path):
	done = harness.runChild(
		tmp_path,
		"""
		import NVDAHelper
		import nvwave

		nvwave.initialize()
		""",
		env={"BASEDREADER_AUDIO_CAPTURE_DIR": str(tmp_path / "absent")},
	)
	assert done.error is not None and "NotADirectoryError" in done.error


def test_duckingReportsUnsupported(tmp_path: Path):
	value = runAudio(
		tmp_path,
		"""
		import audioDucking

		audioDucking.initialize()
		player = nvwave.WavePlayer(1, 8000, 16)
		result = {"supported": audioDucking.isAudioDuckingSupported(), "ducker": repr(player._audioDucker)}
		""",
	)
	assert value == {"supported": False, "ducker": "None"}


def test_soundSplitDegradesAndItsToggleSaysSo(tmp_path: Path):
	value = runAudio(
		tmp_path,
		"""
		import audio
		import config
		import globalCommands
		import ui

		spoken = []
		ui.message = spoken.append
		audio.soundSplit.initialize()
		stateBefore = config.conf["audio"]["soundSplitState"]
		globalCommands.commands.script_cycleSoundSplit(None)
		offResult = audio._setSoundSplitState(audio.SoundSplitState.OFF)
		audio.soundSplit.terminate()
		result = {
			"spoken": spoken,
			"stateUnchanged": config.conf["audio"]["soundSplitState"] == stateBefore,
			"offResult": offResult,
		}
		""",
	)
	assert value == {
		"spoken": ["Sound split not supported"],
		"stateUnchanged": True,
		"offResult": {"foundSessionWithNot2Channels": False},
	}


def test_outputDevicesAreTheDefaultOnly(tmp_path: Path):
	value = runAudio(
		tmp_path,
		"""
		from pycaw.constants import DEVICE_STATE
		from utils import mmdevice

		result = {
			"withDefault": list(mmdevice.getOutputDevices(includeDefault=True)),
			"withoutDefault": list(mmdevice.getOutputDevices()),
			"unplugged": list(mmdevice.getOutputDevices(includeDefault=False, stateMask=DEVICE_STATE.UNPLUGGED)),
		}
		""",
	)
	assert value == {
		"withDefault": [["default", "Default output device"]],
		"withoutDefault": [],
		"unplugged": [],
	}
