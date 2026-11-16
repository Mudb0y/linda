"""Audio on Linux: the wasapi replacement, its sinks, and the modules above them.

nix develop . -c tests/linux/rig python -m pytest tests/linux/test_audio.py

Every case runs in a fresh interpreter through harness.runChild, whose clean run loads no sound
library, opens no sound device and reaches no sound server, and enters NVDA's import graph where
core.main does; the cases that play also check that playing mapped nothing new that could sound. The
timings asserted are loose enough for a loaded machine but tight enough to tell a player that paces
its feeders from one that does not.
"""

import math
import textwrap
import wave
from pathlib import Path

import harness

WAVES = harness.SOURCE / "waves"

#: Opens nvwave the way core.main does, and notes what could sound before any audio plays. The harness
#: fails a run that loads a sound library, opens a sound device or reaches a sound server; this keeps the
#: finer claim that playing maps nothing that could sound, not even what WebKitGTK links.
PRELUDE = """
import gc
import os
import sys
import threading
import time

# NVDAHelper first, as core.main imports it: NVDA's import graph resolves only when entered there.
import NVDAHelper  # noqa: E402, F401
import nvwave  # noqa: E402
import wasapi  # noqa: E402

nvwave.initialize()
_soundBeforeAudio = harness.soundReached(allowLinked=False)
events = []


def done(tag):
	def onDone():
		events.append((tag, threading.current_thread().name, time.monotonic()))

	return onDone
"""

#: Run last: what could sound before the audio, and what the audio added.
ISOLATION = """
isolation = {
	"soundAtStart": _soundBeforeAudio,
	"addedByAudio": sorted(set(harness.soundReached(allowLinked=False)) - set(_soundBeforeAudio)),
}
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
	env = {
		"BASEDREADER_AUDIO_SINK": None,
		"BASEDREADER_AUDIO_CAPTURE_DIR": str(captureDir) if captureDir else "",
	}
	done = harness.runChild(tmpPath, "result = None\n" + body, env=env)
	done.assertClean()
	isolation = done.value["isolation"]
	assert isolation["soundAtStart"] == []
	assert isolation["addedByAudio"] == []
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
		result = {
			"calls": calls,
			"sound": harness.soundReached(allowLinked=False),
			"wx": "wx" in sys.modules,
		}
		"""
	for env in (
		{"BASEDREADER_AUDIO_SINK": None, "BASEDREADER_AUDIO_CAPTURE_DIR": ""},
		{"BASEDREADER_AUDIO_SINK": None, "BASEDREADER_AUDIO_CAPTURE_DIR": str(captureDir)},
	):
		done = harness.runChild(
			tmp_path, "import sys\n" + textwrap.dedent(code), prepare="bootstrap", env=env
		)
		done.assertClean()
		assert done.value["calls"] == [0]
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
		waitedDuringFeed = []
		waitUntilNeeded = wasapi._Player._waitUntilNeeded

		def recordingWait(self, *args):
			# The number of feeds returned so far is the number of the feed now waiting.
			waitedDuringFeed.append(sum(1 for kind, _i, _at in order if kind == "returned"))
			waitUntilNeeded(self, *args)

		wasapi._Player._waitUntilNeeded = recordingWait
		start = time.monotonic()
		firstFed = None

		def feedAll():
			global firstFed
			firstFed = time.monotonic() - start
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
		result = {
			"order": order,
			"calls": calls,
			"merged": [e[:2] for e in merged],
			"firstFed": firstFed,
			"waitedDuringFeed": sorted(set(waitedDuringFeed)),
		}
		""",
	)
	calls = value["calls"]
	assert [tag for tag, _thread, _at in calls] == list(range(10))
	assert {thread for _tag, thread, _at in calls} == {"feeder"}
	for tag, _thread, at in calls:
		assert (tag + 1) * 0.1 - 0.005 <= at <= (tag + 1) * 0.1 + 0.4, (tag, at)
	returned = {i: at for kind, i, at in value["order"] if kind == "returned"}
	# Three chunks fit before the queue passes half of 400 ms; after that each feed waits 100 ms.
	# Being slowed down only lets more be played, so no load makes an earlier feed wait, and the
	# fourth cannot return before 100 ms have played from the first write.
	assert min(value["waitedDuringFeed"]) >= 3
	assert returned[3] - value["firstFed"] >= 0.08
	assert returned[3] <= 0.4
	assert 0.6 <= returned[9] <= 1.2
	for i, at in returned.items():
		# Playing began after the clock was read, so this overstates what was still queued, which a
		# feed that waits only while more than half of 400 ms is queued keeps to 300 ms at most.
		queued = (i + 1) * 0.1 - at
		assert queued <= 0.3 + 0.001, (i, queued)
	synced = value["order"][-1][2]
	assert 0.99 <= synced <= 1.6
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
	assert 0.08 <= value["idleTook"] <= 0.6


def test_emptyFeedAfterPlaybackCallsBackAtTheNextSync(tmp_path: Path):
	"""feed(None, 0, onDone) once everything has been heard, as sapi5 ends an utterance. Its callback
	is due at once, but must wait until the feed has returned and nvwave has filed it under its id,
	or it is lost."""
	value = runAudio(
		tmp_path,
		"""
		player = nvwave.WavePlayer(1, 8000, 16, wantDucking=False, purpose=nvwave.AudioPurpose.SOUNDS)
		player.feed(bytes(800), onDone=done("a"))  # 50 ms
		time.sleep(0.2)
		player.feed(None, 0, onDone=done("b"))
		firedByFeed = [tag for tag, _thread, _at in events]
		syncer = threading.Thread(target=player.sync, name="syncer")
		syncer.start()
		syncer.join()
		player.sync()
		result = {"firedByFeed": firedByFeed, "calls": [(tag, thread) for tag, thread, _at in events]}
		""",
	)
	assert value["firedByFeed"] == ["a"]
	assert value["calls"] == [["a", "MainThread"], ["b", "syncer"]]


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
	assert value["stopTook"] < 0.08
	# Under the 100 ms a feeder that stop did not wake would sleep out before it looked again.
	assert value["feederTook"] < 0.08
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
	assert 0.25 <= afterResume <= 0.9


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
	assert 0.164 <= value["syncTook"] <= 1.0
	assert value["asyncReturned"] < 0.3
	assert 0.3 <= value["asyncTook"] <= 1.2
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
		import ctypes

		import tones
		from NVDAHelper import localLib

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
	)
	assert value["beepReturned"] < 0.3
	assert 0.1 <= value["synced"] <= 0.8
	first, interrupted, last = (bytes.fromhex(b) for b in value["buffers"])
	files = captured(captureDir)
	assert [(f["channels"], f["rate"], f["width"]) for f in files] == [(2, 44100, 2)] * 3
	assert files[0]["frames"] == first
	assert 0.19 <= value["longReturned"] <= 0.6
	cut = files[1]["frames"]
	assert interrupted.startswith(cut)
	heard = len(cut) / 4 / 44100
	# Generating each beep in Python takes some milliseconds on either side of the measured span.
	assert value["heardBeforeCut"] - 0.1 <= heard <= value["heardBeforeCut"] + 0.1
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
	assert 0.08 <= value["calls"][1][1] <= 0.6


def test_streamThatCannotBeReopenedLeavesNothingPlaying(tmp_path: Path):
	"""A lost stream whose device has gone for good: the pending callback still runs, pause and
	resume do nothing, and playing works again once the device is back."""
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

		class GoneSink(NullSink):
			gone = False

			def openStream(self, deviceId, fmt, wake):
				if GoneSink.gone:
					raise audioOutput.NoDevice(2, "no device")
				return LosingStream(fmt)

		audioOutput._sink = GoneSink()
		player = nvwave.WavePlayer(1, 8000, 16, wantDucking=False, purpose=nvwave.AudioPurpose.SOUNDS)
		player.feed(bytes(8000), onDone=done("before"))  # 500 ms
		LosingStream.lose = GoneSink.gone = True
		player.feed(bytes(1600), onDone=done("lost"))
		firedByFeed = [tag for tag, _thread, _at in events]
		failures = []
		for name, call in (
			("pause", lambda: player.pause(True)),
			("resume", lambda: player.pause(False)),
			("sync", player.sync),
		):
			try:
				call()
			except Exception as e:
				failures.append((name, repr(e)))
		try:
			wasapi.wasPlay_feed(player._player, None, 0, None)
			reopenError = None
		except OSError as e:
			reopenError = (type(e).__name__, e.winerror)
		GoneSink.gone = False
		player.feed(bytes(1600), onDone=done("back"))
		# Bounded, because a player still counting frames from the lost stream would wait forever.
		syncer = threading.Thread(target=player.sync, name="syncer", daemon=True)
		syncer.start()
		syncer.join(3)
		result = {
			"firedByFeed": firedByFeed,
			"failures": failures,
			"reopenError": reopenError,
			"syncHung": syncer.is_alive(),
			"calls": [tag for tag, _thread, _at in events],
		}
		""",
	)
	assert value["firedByFeed"] == ["before"]
	assert value["failures"] == []
	assert not value["syncHung"]
	assert value["reopenError"] == ["WindowsError", -2147023728]
	# nvwave filed no callback for the feed that failed.
	assert value["calls"] == ["before", "back"]


def test_sinkFailuresReachNvwaveAsWindowsErrors(tmp_path: Path):
	"""nvwave reads winerror from what wasapi raises, so a sink's plain OSError must arrive as a
	WindowsError carrying E_FAIL, with its own errno, and never as an AttributeError."""
	value = runAudio(
		tmp_path,
		"""
		from linuxPlatform import audioOutput
		from linuxPlatform.audioOutput.nullSink import NullSink, NullStream

		class FailingStream(NullStream):
			failIn = set()

			def write(self, data):
				if "write" in FailingStream.failIn:
					raise OSError(5, "Input/output error")
				super().write(data)

			def setChannelVolume(self, channel, level):
				if "setChannelVolume" in FailingStream.failIn:
					raise OSError(5, "Input/output error")
				super().setChannelVolume(channel, level)

		class FailingSink(NullSink):
			def openStream(self, deviceId, fmt, wake):
				return FailingStream(fmt)

		def described(e):
			return [type(e).__name__, e.errno, e.winerror, type(e.__cause__).__name__]

		audioOutput._sink = FailingSink()
		FailingStream.failIn = {"setChannelVolume"}
		try:
			nvwave.playWaveFile(os.path.join(globalVars.appDir, "waves", "start.wav"), asynchronous=False)
			waveFileError = None
		except Exception as e:
			waveFileError = described(e)
		FailingStream.failIn = {"write"}
		player = nvwave.WavePlayer(1, 8000, 16, wantDucking=False, purpose=nvwave.AudioPurpose.SOUNDS)
		data = bytes(1600)
		try:
			wasapi.wasPlay_feed(player._player, ctypes.cast(data, ctypes.c_char_p), len(data), None)
			feedError = None
		except Exception as e:
			feedError = described(e)
		# nvwave's feed logs the failure and returns, as for an endpoint lost on Windows.
		player.feed(data, onDone=done("failed"))
		FailingStream.failIn = set()
		player.feed(data, onDone=done("recovered"))
		player.sync()
		result = {
			"waveFileError": waveFileError,
			"feedError": feedError,
			"calls": [tag for tag, _thread, _at in events],
		}
		""",
		extra="""
		import ctypes

		import globalVars
		""",
	)
	failed = ["WindowsError", 5, -2147467259, "OSError"]
	assert value == {"waveFileError": failed, "feedError": failed, "calls": ["recovered"]}


def test_destroyRunByTheCollectorInsideCreateDoesNotHang(tmp_path: Path):
	"""WavePlayer.__del__ destroys the player wherever a collection happens to run, which can be in
	the middle of wasapi creating another player on the same thread."""
	value = runAudio(
		tmp_path,
		"""
		class Holder:
			pass

		# A dead player that only the cycle collector can free.
		gc.disable()
		dead = nvwave.WavePlayer(1, 8000, 16, wantDucking=False, purpose=nvwave.AudioPurpose.SOUNDS)
		deadHandle = dead._player
		holder = Holder()
		holder.player = dead
		dead.holder = holder
		del dead, holder

		class CollectingPlayer(wasapi._Player):
			def __init__(self, *args, **kwargs):
				# Stands for a collection that the allocations here happen to set off.
				gc.collect()
				super().__init__(*args, **kwargs)

		wasapi._Player = CollectingPlayer
		created = []

		def create():
			created.append(nvwave.WavePlayer(1, 8000, 16, wantDucking=False, purpose=nvwave.AudioPurpose.SOUNDS))

		creator = threading.Thread(target=create, name="creator", daemon=True)
		creator.start()
		creator.join(5)
		hung = creator.is_alive()
		gc.enable()
		result = {
			"hung": hung,
			"created": len(created),
			"deadDestroyed": deadHandle not in wasapi._players,
		}
		""",
	)
	assert value == {"hung": False, "created": 1, "deadDestroyed": True}


def test_nullClockKeepsTimeWhenFedInSmallPieces(tmp_path: Path):
	"""The null sink's clock against a clock the test moves: no time is lost to writes, however small
	and frequent, and after the queue has run dry new frames play from when they were written."""
	done = harness.runChild(
		tmp_path,
		"""
		from linuxPlatform.audioOutput import StreamFormat, nullSink

		class Clock:
			now = 1000.0

			@staticmethod
			def monotonic():
				return Clock.now

		nullSink.time = Clock
		rate = 44100
		stream = nullSink.NullStream(StreamFormat(1, 1, rate, 16))
		stream.write(bytes(2 * rate * 2))
		stream.start()
		# Ten and a half frames of time pass between writes of ten frames, 4200 times: one second.
		for _ in range(4200):
			Clock.now += 10.5 / rate
			stream.write(bytes(20))
		fedInPieces = stream.playedFrames()
		dry = nullSink.NullStream(StreamFormat(1, 1, rate, 16))
		dry.start()
		dry.write(bytes(200))
		Clock.now += 1.0
		dry.write(bytes(200))
		Clock.now += 50.5 / rate
		afterRunningDry = dry.playedFrames()
		result = {"fedInPieces": fedInPieces, "afterRunningDry": afterRunningDry}
		""",
		prepare="bootstrap",
	)
	done.assertClean()
	assert abs(done.value["fedInPieces"] - 44100) <= 1
	assert done.value["afterRunningDry"] == 150


def test_abortHoldsUntilResetWhateverStartIsCalled(tmp_path: Path):
	"""The null stream's abort, against a clock the test moves: it stops the clock at the call, a
	start before the next reset leaves it stopped, and reset ends it, where a stop ends at the next
	start. The capture stream, built on it, keeps nothing played after an abort."""
	captureDir = tmp_path / "capture"
	captureDir.mkdir()
	done = harness.runChild(
		tmp_path,
		f"""
		from linuxPlatform.audioOutput import StreamFormat, captureSink, nullSink

		class Clock:
			now = 1000.0

			@staticmethod
			def monotonic():
				return Clock.now

		nullSink.time = Clock
		fmt = StreamFormat(1, 1, 8000, 16)
		sink = captureSink.CaptureSink({str(captureDir)!r})
		played = {{}}
		for name, stream in (("null", nullSink.NullStream(fmt)), ("capture", sink.openStream("", fmt, lambda: None))):
			steps = []
			stream.write(bytes(16000))  # 1 s
			stream.start()
			Clock.now += 0.1
			stream.abort()
			Clock.now += 0.1
			steps.append(stream.playedFrames())
			stream.start()
			Clock.now += 0.1
			steps.append(stream.playedFrames())
			stream.stop()
			stream.reset()
			stream.write(bytes(16000))
			stream.start()
			Clock.now += 0.1
			steps.append(stream.playedFrames())
			stream.stop()
			Clock.now += 0.1
			stream.start()
			Clock.now += 0.1
			steps.append(stream.playedFrames())
			stream.close()
			played[name] = steps
		result = {{"played": played, "delivered": sink.deliveredFrames()}}
		""",
		prepare="bootstrap",
	)
	done.assertClean()
	# 100 ms before the abort, nothing after it despite the start, then 100 ms after the reset, and
	# 100 ms more after a stop and start.
	assert done.value["played"] == {"null": [800, 800, 800, 1600], "capture": [800, 800, 800, 1600]}
	assert done.value["delivered"] == 800 + 1600
	assert sorted(len(f["frames"]) // 2 for f in captured(captureDir)) == [800, 1600]


def test_wakeFromASinkThreadEndsWaitsEarly(tmp_path: Path):
	"""A sink whose device runs far ahead of the player's estimate wakes the waiting feeder and
	syncer from a thread of its own; callbacks still run only on the thread feeding or syncing."""
	value = runAudio(
		tmp_path,
		"""
		from linuxPlatform import audioOutput

		class RushingStream(audioOutput.SinkStream):
			# Takes everything written 20 ms after the last write, while the player expects real time.
			def __init__(self, fmt, wake):
				self.bufferFrames = fmt.samplesPerSec * audioOutput.BUFFER_MS // 1000
				self._blockAlign = fmt.blockAlign
				self._wake = wake
				self._lock = threading.Lock()
				self._written = self._taken = 0
				self._dueAt = None
				self._running = False
				# Cleared by abort until the next reset.
				self._isRunnable = True
				self._closed = threading.Event()
				threading.Thread(target=self._consume, name="sinkThread", daemon=True).start()

			def _consume(self):
				while not self._closed.wait(0.002):
					with self._lock:
						due = self._running and self._dueAt is not None and time.monotonic() >= self._dueAt
						if due:
							self._taken = self._written
							self._dueAt = None
					if due:
						wakes.append(threading.current_thread().name)
						self._wake()

			def write(self, data):
				with self._lock:
					self._written += len(data) // self._blockAlign
					self._dueAt = time.monotonic() + 0.02

			def bufferedFrames(self):
				with self._lock:
					return self._written - self._taken

			def playedFrames(self):
				with self._lock:
					return self._taken

			def start(self):
				with self._lock:
					self._running = self._isRunnable

			def stop(self):
				with self._lock:
					self._running = False

			def abort(self):
				with self._lock:
					self._running = self._isRunnable = False

			def reset(self):
				with self._lock:
					self._written = self._taken = 0
					self._dueAt = None
					self._isRunnable = True

			def setChannelVolume(self, channel, level):
				pass

			def close(self):
				self._closed.set()

		class RushingSink(audioOutput.Sink):
			kind = "rushing"
			description = "a sink whose device runs ahead"

			def openStream(self, deviceId, fmt, wake):
				return RushingStream(fmt, wake)

			def devices(self):
				return []

			def keepAwakeFor(self, deviceId, ms, noiseVolume):
				pass

			def stopKeepingAwake(self):
				pass

			def setGain(self, level):
				pass

			def deliveredFrames(self):
				return 0

		wakes = []
		audioOutput._sink = RushingSink()
		player = nvwave.WavePlayer(1, 8000, 16, wantDucking=False, purpose=nvwave.AudioPurpose.SOUNDS)
		start = time.monotonic()
		for i in range(20):
			player.feed(bytes(1600), onDone=done(i))  # 100 ms each
		player.sync()
		took = time.monotonic() - start
		result = {
			"took": took,
			"wakeThreads": sorted(set(wakes)),
			"calls": [(tag, thread) for tag, thread, _at in events],
		}
		""",
	)
	# Two seconds of audio. Timed by the estimate alone, the feeds and the sync would wait about
	# 100 ms seven times, 0.7 s in all; woken, each wait lasts about 20 ms.
	assert value["took"] < 0.5, value["took"]
	assert value["wakeThreads"] == ["sinkThread"]
	assert value["calls"] == [[i, "MainThread"] for i in range(20)]


def test_exitClosesEveryStreamWithoutWaitingForItsAudio(tmp_path: Path):
	"""The exit hook, called directly: a feeder blocked in feed and a syncer blocked in sync both
	return, every stream is closed, no callback runs, and nothing opens a stream afterwards."""
	value = runAudio(
		tmp_path,
		"""
		from linuxPlatform import audioOutput
		from linuxPlatform.audioOutput.nullSink import NullSink, NullStream

		closed = []

		class RecordingStream(NullStream):
			def close(self):
				closed.append(threading.current_thread().name)
				super().close()

		class RecordingSink(NullSink):
			opened = 0

			def openStream(self, deviceId, fmt, wake):
				RecordingSink.opened += 1
				return RecordingStream(fmt)

		audioOutput._sink = RecordingSink()
		feeding = nvwave.WavePlayer(1, 8000, 16, wantDucking=False, purpose=nvwave.AudioPurpose.SOUNDS)
		syncing = nvwave.WavePlayer(1, 8000, 16, wantDucking=False, purpose=nvwave.AudioPurpose.SOUNDS)
		# Feed and sync both wait here, holding the player's lock until the wait gives it up, which the
		# exit hook needs.
		waiting = {"feeder": threading.Event(), "syncer": threading.Event()}
		waitUntilNeeded = wasapi._Player._waitUntilNeeded

		def noteWaiting(self, *args):
			if (name := threading.current_thread().name) in waiting:
				waiting[name].set()
			waitUntilNeeded(self, *args)

		wasapi._Player._waitUntilNeeded = noteWaiting

		def feedLong():
			feeding.feed(bytes(32000), onDone=done("fed"))  # 2 s, five times the buffer
			feeding.idle()

		feeder = threading.Thread(target=feedLong, name="feeder")
		feeder.start()
		syncing.feed(bytes(16000), onDone=done("synced"))  # 1 s
		syncer = threading.Thread(target=syncing.idle, name="syncer")
		syncer.start()
		blocked = all(event.wait(30) for event in waiting.values())
		start = time.monotonic()
		wasapi._closeForExit()
		hookTook = time.monotonic() - start
		feeder.join(3)
		syncer.join(3)
		late = nvwave.WavePlayer(1, 8000, 16, wantDucking=False, purpose=nvwave.AudioPurpose.SOUNDS)
		late.feed(bytes(1600), onDone=done("late"))
		late.sync()
		result = {
			"blocked": blocked,
			"hookTook": hookTook,
			"alive": [thread.name for thread in (feeder, syncer) if thread.is_alive()],
			"closed": closed,
			"opened": RecordingSink.opened,
			"calls": [tag for tag, _thread, _at in events],
		}
		""",
	)
	assert value["blocked"]
	assert value["alive"] == []
	# Neither stream's audio was waited for.
	assert value["hookTook"] < 0.5
	assert value["closed"] == ["MainThread", "MainThread"]
	assert value["opened"] == 2
	assert value["calls"] == []


def test_aPlayerStillPlayingAtExitIsClosedBeforeTheInterpreterGoes(tmp_path: Path):
	"""A daemon thread playing at exit, as nvwave's wave file player can be: the stream it is
	feeding is closed at exit, and nothing reaches the stream after that."""
	record = tmp_path / "streamCalls.txt"
	done = harness.runChild(
		tmp_path,
		f"""
		import threading

		import NVDAHelper
		import nvwave
		from linuxPlatform import audioOutput
		from linuxPlatform.audioOutput.nullSink import NullSink, NullStream


		def note(what):
			with open({str(record)!r}, "a", encoding="utf-8") as f:
				f.write(what + "\\n")


		wrote = threading.Event()


		class RecordingStream(NullStream):
			def write(self, data):
				note("write")
				super().write(data)
				wrote.set()

			def close(self):
				note("close")
				super().close()


		class RecordingSink(NullSink):
			def openStream(self, deviceId, fmt, wake):
				note("open")
				return RecordingStream(fmt)


		nvwave.initialize()
		audioOutput._sink = RecordingSink()
		player = nvwave.WavePlayer(1, 8000, 16, wantDucking=False, purpose=nvwave.AudioPurpose.SOUNDS)


		def play():
			player.feed(bytes(160000))  # 10 s
			player.idle()


		threading.Thread(target=play, name="player", daemon=True).start()
		result = "exiting while playing" if wrote.wait(30) else "nothing played"
		""",
	)
	done.assertClean()
	assert done.value == "exiting while playing"
	assert "Traceback" not in done.stderr, done.stderr
	calls = record.read_text(encoding="utf-8").split()
	assert calls[0] == "open"
	assert "write" in calls
	assert calls[-1] == "close" and calls.count("close") == 1, calls


def test_streamLostWhileTheExitHookRunsEndsTheFeed(tmp_path: Path):
	"""A stream lost after the exit hook has begun, so that no new stream is opened, and one lost
	before it, whose stranded callback runs the exit hook and so closes the new stream: either way
	the feed returns quietly with nothing left to play, and the stranded callback still runs."""
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

		def lose(whileExiting):
			LosingSink.opened = 0
			player = nvwave.WavePlayer(1, 8000, 16, wantDucking=False, purpose=nvwave.AudioPurpose.SOUNDS)

			def stranded():
				events.append(("stranded", threading.current_thread().name, time.monotonic()))
				if not whileExiting:
					wasapi._closeForExit()

			player.feed(bytes(8000), onDone=stranded)  # 500 ms
			wasapi._isExiting = whileExiting
			LosingStream.lose = True
			try:
				player.feed(bytes(1600))
				error = None
			except BaseException as e:
				error = repr(e)
			wasapi._isExiting = False
			calls = [tag for tag, _thread, _at in events]
			events.clear()
			return {"error": error, "opened": LosingSink.opened, "calls": calls}

		audioOutput._sink = LosingSink()
		result = {"whileExiting": lose(True), "beforeExiting": lose(False)}
		""",
	)
	assert value == {
		"whileExiting": {"error": None, "opened": 1, "calls": ["stranded"]},
		"beforeExiting": {"error": None, "opened": 2, "calls": ["stranded"]},
	}


def test_exitHookRunFromACallbackInsideSyncEndsTheSync(tmp_path: Path):
	"""A callback releases the player's lock, so the exit hook can close the stream while sync still
	has a later end of audio to wait for. sync returns without touching the closed stream, and the
	later callback is discarded."""
	value = runAudio(
		tmp_path,
		"""
		player = nvwave.WavePlayer(1, 8000, 16, wantDucking=False, purpose=nvwave.AudioPurpose.SOUNDS)

		def first():
			events.append(("first", threading.current_thread().name, time.monotonic()))
			wasapi._closeForExit()

		# Short enough that neither feed waits, so the first callback comes due inside sync.
		player.feed(bytes(800), onDone=first)  # 50 ms
		player.feed(bytes(1600), onDone=done("second"))  # 100 ms
		try:
			player.sync()
			error = None
		except BaseException as e:
			error = repr(e)
		result = {"error": error, "calls": [tag for tag, _thread, _at in events]}
		""",
	)
	assert value["error"] is None
	assert value["calls"] == ["first"]


def test_lostStreamFoundByPlayedFramesCountsAsHeard(tmp_path: Path):
	"""A stream that reports its loss from playedFrames: what was written counts as played, so the
	waiting sync returns and its callback runs rather than waiting on a clock that has stopped."""
	value = runAudio(
		tmp_path,
		"""
		from linuxPlatform import audioOutput
		from linuxPlatform.audioOutput.nullSink import NullSink, NullStream

		class LosingStream(NullStream):
			lose = False

			def playedFrames(self):
				if LosingStream.lose:
					raise audioOutput.StreamLost("device went away")
				return super().playedFrames()

		class LosingSink(NullSink):
			def openStream(self, deviceId, fmt, wake):
				return LosingStream(fmt)

		audioOutput._sink = LosingSink()
		player = nvwave.WavePlayer(1, 8000, 16, wantDucking=False, purpose=nvwave.AudioPurpose.SOUNDS)
		player.feed(bytes(16000), onDone=done("lost"))  # 1 s
		LosingStream.lose = True
		start = time.monotonic()
		player.sync()
		result = {"syncTook": time.monotonic() - start, "calls": [tag for tag, _thread, _at in events]}
		""",
	)
	assert value["calls"] == ["lost"]
	# The second of audio was not waited for.
	assert value["syncTook"] < 0.5


def test_aLostStreamIsReplacedWhicheverCallFindsIt(tmp_path: Path):
	"""A stream that reports its loss from any of its methods, once or from then on, as a device that
	went away would: nothing fails in nvwave, the stream is replaced, and every callback that was not
	cancelled by a stop still runs, the end of the next utterance among them, for speech and for
	sounds, whose players set their volume on every feed."""
	value = runAudio(
		tmp_path,
		"""
		from linuxPlatform import audioOutput
		from linuxPlatform.audioOutput.nullSink import NullSink, NullStream

		armed = {"method": None, "persists": False}

		def losing(name, base):
			def method(self, *args):
				if self.lost or armed["method"] == name:
					self.lost = self.lost or armed["persists"]
					armed["method"] = None
					raise audioOutput.StreamLost(5, "device went away")
				return base(self, *args)

			return method

		class LosingStream(NullStream):
			lost = False

		for name in (
			"write",
			"bufferedFrames",
			"playedFrames",
			"start",
			"stop",
			"abort",
			"reset",
			"setChannelVolume",
			"close",
		):
			setattr(LosingStream, name, losing(name, getattr(NullStream, name)))

		class LosingSink(NullSink):
			def __init__(self):
				super().__init__()
				self.opened = 0

			def openStream(self, deviceId, fmt, wake):
				self.opened += 1
				return LosingStream(fmt)

		# Each case: what the player is asked to do once the stream is to be lost, and the stream
		# method that reports it.
		cases = {
			"write": ("feed", "write"),
			"start": ("feed after idle", "start"),
			"stop": ("stop", "abort"),
			"setChannelVolume": ("setVolume", "setChannelVolume"),
			"pause": ("pause and resume", "stop"),
			"resume": ("pause and resume", "start"),
			"reset": ("stop", "reset"),
		}
		outcomes = {}
		for case, (action, method) in cases.items():
			for persists in (False, True):
				for purpose in (nvwave.AudioPurpose.SPEECH, nvwave.AudioPurpose.SOUNDS):
					sink = audioOutput._sink = LosingSink()
					armed.update(method=None, persists=persists)
					events.clear()
					player = nvwave.WavePlayer(1, 8000, 16, wantDucking=False, purpose=purpose)
					player.enableTrimmingLeadingSilence(False)
					raised = []

					def attempt(name, call):
						try:
							call()
						except Exception as e:
							raised.append((name, repr(e)))

					attempt("first", lambda: player.feed(bytes(3200), onDone=done("first")))  # 200 ms
					if action == "feed after idle":
						attempt("idle", lambda: wasapi.wasPlay_idle(player._player))
					armed["method"] = method
					if action == "stop":
						attempt("stop", player.stop)
					elif action == "setVolume":
						attempt("setVolume", lambda: player.setVolume(all=0.5))
					elif action == "pause and resume":
						attempt("pause", lambda: player.pause(True))
						attempt("resume", lambda: player.pause(False))
					attempt("next", lambda: player.feed(bytes(800), onDone=done("next")))  # 50 ms
					syncer = threading.Thread(target=lambda: attempt("sync", player.sync), daemon=True)
					syncer.start()
					syncer.join(3)
					outcomes[f"{case} {'from then on' if persists else 'once'} {purpose.name}"] = {
						"lostReported": armed["method"] is None,
						"opened": sink.opened,
						"calls": [tag for tag, _thread, _at in events],
						"raised": raised,
						"hung": syncer.is_alive(),
					}
					del player
					gc.collect()
		result = outcomes
		""",
	)
	assert len(value) == 28
	for case, outcome in value.items():
		# A stop cancels what was playing; nothing else may lose a callback.
		calls = ["next"] if case.split()[0] in ("stop", "reset") else ["first", "next"]
		expected = {"lostReported": True, "opened": 2, "calls": calls, "raised": [], "hung": False}
		assert outcome == expected, case


#: Stand-in sinks for the stop tests: each stream records when an abort reached it on the main
#: thread, which is where nvwave stops speech on a key press. Other threads keep the GIL busy.
TIMED_SINKS = """
import math
import random
import struct

from linuxPlatform import audioOutput
from linuxPlatform.audioOutput.captureSink import CaptureSink
from linuxPlatform.audioOutput.nullSink import NullSink, NullStream

reached = []


def timedAbort(base):
	class TimedAbort(base):
		def abort(self):
			if threading.current_thread() is threading.main_thread():
				reached.append(time.monotonic())
			super().abort()

	return TimedAbort


libc = ctypes.CDLL(None)


class NativeStream(NullStream):
	# Each call goes into C once, as a binding to a native sink does, and ctypes lets go of the GIL
	# there, which the calling thread must then win back while it holds whatever locks it holds.
	def write(self, data):
		libc.getpid()
		super().write(data)

	def bufferedFrames(self):
		libc.getpid()
		return super().bufferedFrames()

	def playedFrames(self):
		libc.getpid()
		return super().playedFrames()

	def start(self):
		libc.getpid()
		super().start()

	def stop(self):
		libc.getpid()
		super().stop()

	def abort(self):
		libc.getpid()
		super().abort()

	def reset(self):
		libc.getpid()
		super().reset()

	def setChannelVolume(self, channel, level):
		libc.getpid()
		super().setChannelVolume(channel, level)


NativeStream = timedAbort(NativeStream)


class NativeSink(NullSink):
	def openStream(self, deviceId, fmt, wake):
		return NativeStream(fmt)


class TimedCaptureSink(CaptureSink):
	# The capture sink writes files as the feeder calls it, which lets go of the GIL in the same way.
	def openStream(self, deviceId, fmt, wake):
		stream = super().openStream(deviceId, fmt, wake)
		stream.__class__ = timedAbort(type(stream))
		return stream


running = True


def busy():
	x = 0
	while running:
		for i in range(1000):
			x += i * i


for _ in range(BUSY_THREADS):
	threading.Thread(target=busy, daemon=True).start()
# eSpeak's 300 ms chunk at 22050 Hz.
tone = struct.pack("<6615h", *(int(8000 * math.sin(i * 0.2)) for i in range(6615)))
"""

#: Plays speech in eSpeak's chunks and stops it at random moments well into it.
STOP_UNDER_LOAD = """
measured = {}
for name, sink, purpose in (
	("native speech", NativeSink(), nvwave.AudioPurpose.SPEECH),
	("native sounds", NativeSink(), nvwave.AudioPurpose.SOUNDS),
	("capture speech", TimedCaptureSink(captureDir), nvwave.AudioPurpose.SPEECH),
):
	audioOutput._sink = sink
	player = nvwave.WavePlayer(1, 22050, 16, wantDucking=False, purpose=purpose)
	silenced, returned = [], []
	for _ in range(ROUNDS):
		stopping = threading.Event()

		def speak():
			while not stopping.is_set():
				player.feed(tone, onDone=lambda: None)
			player.idle()

		feeder = threading.Thread(target=speak, name="feeder")
		feeder.start()
		time.sleep(random.uniform(0.1, 0.4))
		stopping.set()
		reached.clear()
		called = time.monotonic()
		player.stop()
		returned.append(time.monotonic() - called)
		feeder.join(10)
		silenced.append(min((at for at in reached if at >= called), default=math.inf) - called)
	measured[name] = {"silenced": silenced, "returned": returned}
	del player
	gc.collect()
running = False
result = measured
"""


def test_stopSilencesAtOnceWhileOtherThreadsRunPython(tmp_path: Path):
	"""WavePlayer.stop, as speech is cancelled on a key press, reaches the sink at once, however long
	the feeding thread is held up waiting for the GIL, and returns at once too, the volume a sounds
	player sets on stop included. A stop that waited for the feeder's lock was measured silencing
	speech up to 0.6 s late through a sink that calls into C, and up to 0.8 s late through the capture
	sink."""
	captureDir = tmp_path / "capture"
	captureDir.mkdir()
	value = runAudio(
		tmp_path,
		TIMED_SINKS + STOP_UNDER_LOAD,
		extra=f"""
		import ctypes

		captureDir = {str(captureDir)!r}
		BUSY_THREADS = 3
		ROUNDS = 15
		""",
	)
	for name, measured in value.items():
		silenced, returned = sorted(measured["silenced"]), sorted(measured["returned"])
		# The main thread holds the GIL from the call on, unless the system takes the processor away.
		assert silenced[len(silenced) // 2] < 0.001 and silenced[-1] < 0.01, (name, silenced)
		if name.startswith("capture"):
			# The capture sink's stop writes out what was heard, which waits while the feeder writes.
			continue
		assert returned[len(returned) // 2] < 0.002 and returned[-1] < 0.02, (name, returned)


#: Rapid arrowing: each round a synthesizer thread begins an utterance and the main thread cancels
#: it within a few milliseconds, which is when the feeder completes the previous stop and starts the
#: stream, each a call into C after which it must win the GIL back.
STOP_NEAR_START = """
measured = {}
for purpose in (nvwave.AudioPurpose.SPEECH, nvwave.AudioPurpose.SOUNDS):
	audioOutput._sink = NativeSink()
	player = nvwave.WavePlayer(1, 22050, 16, wantDucking=False, purpose=purpose)
	player.enableTrimmingLeadingSilence(False)
	go, cancelled, finished = threading.Event(), threading.Event(), threading.Event()
	quitting = False

	def synthesize():
		while True:
			go.wait()
			go.clear()
			if quitting:
				return
			for _ in range(3):
				if cancelled.is_set():
					break
				player.feed(tone, onDone=lambda: None)
			player.idle()
			finished.set()

	synth = threading.Thread(target=synthesize, name="synth", daemon=True)
	synth.start()
	silenced, returned, hung = [], [], 0
	for _ in range(ROUNDS):
		cancelled.clear()
		finished.clear()
		go.set()
		time.sleep(random.uniform(0, 0.004))
		reached.clear()
		called = time.monotonic()
		cancelled.set()
		player.stop()
		returned.append(time.monotonic() - called)
		silenced.append(min((at for at in reached if at >= called), default=math.inf) - called)
		if not finished.wait(5):
			hung += 1
			break
	quitting = True
	go.set()
	synth.join(5)
	measured[purpose.name] = {"silenced": silenced, "returned": returned, "hung": hung}
	del player
	gc.collect()
running = False
result = measured
"""


def test_stopNearAnUtterancesStartSilencesAtOnce(tmp_path: Path):
	"""WavePlayer.stop landing as the feeder starts an utterance reaches the sink at once, through a
	sink whose start, stop and reset call into C as libpwsink's do, while three other threads run
	Python. A player whose feeder held a lock that stop needs around those calls was measured
	silencing up to 1.06 s late, with up to 17 stops in 300 more than 10 ms late."""
	value = runAudio(
		tmp_path,
		TIMED_SINKS + STOP_NEAR_START,
		extra="""
		import ctypes

		BUSY_THREADS = 3
		ROUNDS = 300
		""",
	)
	for name, measured in value.items():
		assert measured["hung"] == 0, name
		silenced = sorted(measured["silenced"])
		assert silenced[len(silenced) // 2] < 0.001, (name, silenced)
		# The main thread holds the GIL from the call on, unless the system takes the processor away.
		assert sum(1 for s in silenced if s > 0.005) <= 1 and silenced[-1] < 0.02, (name, silenced[-10:])


def test_aStopRacingTheFeedersResetNeitherPlaysOnNorHangs(tmp_path: Path):
	"""stop and the feeder meet only in the stream's abort and the stop flag, and the feeder's reset
	of the stream ends an abort. Two orders are forced here, each with a feed under way while the
	stop is: the abort lands between the feeder taking the flag down and its reset, or after its
	reset. In the first the feeder must still see the flag before it starts the stream; in the second
	the abort keeps the stream silent, and the flag raised after it must end the wait of a feeder
	whose stream will now never play. Either way a feed after the stop plays as usual."""
	value = runAudio(
		tmp_path,
		"""
		from linuxPlatform import audioOutput
		from linuxPlatform.audioOutput.nullSink import NullSink, NullStream

		hooks = {}
		phase = {"name": None}
		calls = []

		def hook(name):
			hooks.get(name, lambda: None)()

		class RacingStream(NullStream):
			def write(self, data):
				calls.append((phase["name"], "write"))
				super().write(data)

			def start(self):
				calls.append((phase["name"], "start"))
				super().start()
				hook("afterStart")

			def reset(self):
				hook("beforeReset")
				super().reset()

			def abort(self):
				hook("beforeAbort")
				super().abort()
				hook("afterAbort")

		class RacingSink(NullSink):
			def openStream(self, deviceId, fmt, wake):
				return RacingStream(fmt)

		audioOutput._sink = RacingSink()
		outcomes = {}
		for case in ("abort before the reset", "abort after the reset"):
			player = nvwave.WavePlayer(1, 8000, 16, wantDucking=False, purpose=nvwave.AudioPurpose.SPEECH)
			player.enableTrimmingLeadingSilence(False)
			events.clear()
			calls.clear()
			hooks.clear()
			phase["name"] = "first"
			player.feed(bytes(8000), onDone=done("first"))  # 500 ms
			ready, go = threading.Event(), threading.Event()
			if case == "abort before the reset":
				# A stop the next feed will complete before it plays.
				player.stop()
				hooks["beforeReset"] = lambda: (ready.set(), go.wait(2))
				# Held up after the abort, before stop raises the flag again.
				hooks["afterAbort"] = lambda: (go.set(), time.sleep(0.2))
			else:
				# The feeder takes the flag down, resets the stream and starts it before the abort.
				hooks["beforeAbort"] = lambda: (ready.set(), go.wait(2))
				hooks["afterStart"] = go.set

			def speak():
				phase["name"] = "during"
				started = time.monotonic()
				player.feed(bytes(8000), onDone=done("during"))  # 500 ms
				player.sync()
				took.append(time.monotonic() - started)

			took = []
			feeder = threading.Thread(target=speak, name="feeder", daemon=True)
			stopper = threading.Thread(target=player.stop, name="stopper", daemon=True)
			first, second = (feeder, stopper) if case == "abort before the reset" else (stopper, feeder)
			first.start()
			ready.wait(2)
			second.start()
			feeder.join(3)
			stopper.join(3)
			hung = [t.name for t in (feeder, stopper) if t.is_alive()]
			hooks.clear()
			phase["name"] = "after"
			player.feed(bytes(1600), onDone=done("after"))  # 100 ms
			player.sync()
			outcomes[case] = {
				"hung": hung,
				"calls": [tag for tag, _thread, _at in events],
				"duringStreamCalls": sorted({call for name, call in calls if name == "during"}),
				"took": took[0] if took else None,
			}
			del player
			gc.collect()
		result = outcomes
		""",
	)
	before, after = value["abort before the reset"], value["abort after the reset"]
	for outcome in (before, after):
		assert outcome["hung"] == [] and outcome["calls"] == ["after"], outcome
	# The flag was up before the feeder looked, so it neither wrote nor started; it went on only
	# once the stopper, held up after its abort, let it, and returned at once.
	assert before["duringStreamCalls"] == [] and before["took"] < 0.4, before
	# The feeder found the flag down and started the stream, which the abort then silenced; the flag
	# raised again ended the feed long before its 500 ms could have played.
	assert after["duringStreamCalls"] == ["start", "write"] and after["took"] < 0.3, after


def test_unsupportedFormatFailsToOpenAsWasapiDoes(tmp_path: Path):
	done = harness.runChild(
		tmp_path,
		"""
		import ctypes

		import wasapi
		from linuxPlatform import audioOutput
		from linuxPlatform.audioOutput.nullSink import NullSink
		from winBindings.mmeapi import WAVEFORMATEX

		class CountingSink(NullSink):
			opened = 0

			def openStream(self, deviceId, fmt, wake):
				CountingSink.opened += 1
				return super().openStream(deviceId, fmt, wake)

		audioOutput._sink = CountingSink()
		callback = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_uint)(lambda h, i: None)
		fmt = WAVEFORMATEX(wFormatTag=1, nChannels=1, nSamplesPerSec=8000, wBitsPerSample=12, nBlockAlign=2)
		player = wasapi.wasPlay_create("", fmt, callback)
		try:
			wasapi.wasPlay_open(player)
			error = None
		except OSError as e:
			error = [type(e).__name__, e.winerror]
		wasapi.wasPlay_destroy(player)
		result = {"error": error, "opened": CountingSink.opened}
		""",
		prepare="bootstrap",
	)
	done.assertClean()
	# AUDCLNT_E_UNSUPPORTED_FORMAT, and no stream asked of the sink.
	assert done.value == {"error": ["WindowsError", -2004287480], "opened": 0}


def test_pcmHandlesEveryFormatThePlayerAccepts(tmp_path: Path):
	"""Leading silence and gain for 8-, 16-, 24- and 32-bit integer and 32- and 64-bit float
	samples, the refusal of a format with no samples to scale, and the byte swap a big-endian
	machine needs."""
	done = harness.runChild(
		tmp_path,
		"""
		import struct
		import types

		from linuxPlatform.audioOutput import StreamFormat, pcm

		def ints24(*samples):
			return b"".join(s.to_bytes(3, "little", signed=True) for s in samples)

		def fromInts24(data):
			return [int.from_bytes(data[i : i + 3], "little", signed=True) for i in range(0, len(data), 3)]

		pcm8 = StreamFormat(1, 1, 8000, 8)
		pcm16 = StreamFormat(1, 1, 8000, 16)
		pcm24 = StreamFormat(1, 2, 8000, 24)
		pcm32 = StreamFormat(1, 1, 8000, 32)
		float32 = StreamFormat(3, 1, 8000, 32)
		float64 = StreamFormat(3, 2, 8000, 64)
		adpcm = StreamFormat(2, 1, 8000, 4)
		result = {
			"silence8": pcm.silence(pcm8, 3).hex(),
			# 8-bit audio's threshold is 0, so only the zero point, 128, is silent.
			"leading8": pcm.leadingSilenceSize(pcm8, bytes([128, 128, 129, 128])),
			"leading16": pcm.leadingSilenceSize(pcm16, struct.pack("<4h", 0, 31, -31, 32)),
			# Stereo: whole frames only, and a loud sample in either channel ends the silence.
			"leading24": pcm.leadingSilenceSize(pcm24, ints24(0, 8191, -8191, 0, 0, 8192)),
			"leading32": pcm.leadingSilenceSize(pcm32, struct.pack("<3i", 2097151, -2097151, 2097152)),
			"leadingFloat32": pcm.leadingSilenceSize(float32, struct.pack("<3f", 0.0, 0.0009, -0.001)),
			"leadingFloat64": pcm.leadingSilenceSize(float64, struct.pack("<4d", 0.0, -0.0009, 0.5, 0.0)),
			"allSilent": pcm.leadingSilenceSize(pcm16, bytes(8)),
			"unknownHasNone": pcm.leadingSilenceSize(adpcm, bytes(8)),
			"gain8": list(pcm.applyGain(pcm8, bytes([0, 128, 255]), [0.5])),
			"gain24": fromInts24(pcm.applyGain(pcm24, ints24(1000, -2000, 8388607, -8388608), [0.5, 0.25])),
			"gainFloat32": list(struct.unpack("<2f", pcm.applyGain(float32, struct.pack("<2f", 0.5, -1.0), [0.5]))),
			"gainFloat64": list(
				struct.unpack("<2d", pcm.applyGain(float64, struct.pack("<2d", 0.5, -1.0), [0.5, 0.25])),
			),
			"unityLeavesUnknownAlone": pcm.applyGain(adpcm, b"abc", [1.0]) == b"abc",
		}
		try:
			pcm.applyGain(adpcm, b"abc", [0.5])
			result["gainUnknown"] = None
		except ValueError as e:
			result["gainUnknown"] = str(e)
		# A big-endian machine reads the little-endian samples swapped and swaps them back. Pretending
		# to be one here, the swap shows as the reverse, and writing swaps them back again.
		pcm.sys = types.SimpleNamespace(byteorder="big")
		data16 = struct.pack("<2h", 1, -2)
		dataFloat = struct.pack("<f", 0.25)
		result["swapped16"] = list(pcm._samples(pcm16, data16))
		result["roundTrip16"] = pcm._toBytes(pcm16, pcm._samples(pcm16, data16)) == data16
		result["roundTripFloat"] = pcm._toBytes(float32, pcm._samples(float32, dataFloat)) == dataFloat
		""",
		prepare="bootstrap",
	)
	done.assertClean()
	assert done.value == {
		"silence8": "808080",
		"leading8": 2,
		"leading16": 6,
		"leading24": 12,
		"leading32": 8,
		"leadingFloat32": 8,
		"leadingFloat64": 16,
		"allSilent": 8,
		"unknownHasNone": 0,
		"gain8": [64, 128, 192],
		"gain24": [500, -500, 4194304, -2097152],
		"gainFloat32": [0.25, -0.5],
		"gainFloat64": [0.25, -0.25],
		"unityLeavesUnknownAlone": True,
		"gainUnknown": "no gain for format StreamFormat(formatTag=2, channels=1, samplesPerSec=8000, bitsPerSample=4)",
		"swapped16": [256, -257],
		"roundTrip16": True,
		"roundTripFloat": True,
	}


def test_soundSplitRefusesAnyStateButOff(tmp_path: Path):
	done = harness.runChild(
		tmp_path,
		"""
		import NVDAHelper
		import audio

		refused = []
		for state in audio.SoundSplitState:
			if state is audio.SoundSplitState.OFF:
				continue
			try:
				audio._setSoundSplitState(state)
			except NotImplementedError as e:
				refused.append(str(e))
		result = refused
		""",
	)
	done.assertClean(ignoring={"noLinuxImplementation"})
	assert done.value == ["audio.soundSplit._setSoundSplitState has no Linux implementation"] * 7
	assert done.noLinuxImplementation == ["audio.soundSplit._setSoundSplitState"] * 7


def test_outputDevicesListTheSinksDevicesWhenActiveOnesAreAsked(tmp_path: Path):
	value = runAudio(
		tmp_path,
		"""
		from linuxPlatform import audioOutput
		from linuxPlatform.audioOutput.nullSink import NullSink
		from pycaw.constants import DEVICE_STATE
		from utils import mmdevice

		class ListingSink(NullSink):
			def devices(self):
				return [("alsa_output.pci-0000_00_1f.3.analog-stereo", "Built-in Audio Analog Stereo")]

		audioOutput._sink = ListingSink()
		result = {
			"active": list(mmdevice.getOutputDevices()),
			"withDefault": list(mmdevice.getOutputDevices(includeDefault=True)),
			"unplugged": list(mmdevice.getOutputDevices(stateMask=DEVICE_STATE.UNPLUGGED)),
		}
		""",
	)
	device = ["alsa_output.pci-0000_00_1f.3.analog-stereo", "Built-in Audio Analog Stereo"]
	assert value == {
		"active": [device],
		"withDefault": [["default", "Default output device"], device],
		"unplugged": [],
	}


def test_missingCaptureDirectoryFailsAtStart(tmp_path: Path):
	done = harness.runChild(
		tmp_path,
		"""
		import NVDAHelper
		import nvwave

		nvwave.initialize()
		""",
		env={"BASEDREADER_AUDIO_SINK": None, "BASEDREADER_AUDIO_CAPTURE_DIR": str(tmp_path / "absent")},
	)
	assert done.error is not None and "NotADirectoryError" in done.error
	done.assertClean(ignoring={"error"})


def test_theEnvironmentChoosesTheSink(tmp_path: Path):
	"""BASEDREADER_AUDIO_SINK names the sink, unset it falls back to the capture directory, and any
	other value, or the capture sink with nowhere to capture to, fails at the first getSink with the
	variable named. The PipeWire sink is reached by importing its module, which a stand-in replaces
	here so that nothing is loaded that could sound."""
	captureDir = tmp_path / "capture"
	captureDir.mkdir()
	done = harness.runChild(
		tmp_path,
		f"""
		import os
		import sys
		import types

		from linuxPlatform import audioOutput

		class StandIn:
			kind = "pipewire"

		standIn = types.ModuleType("linuxPlatform.audioOutput.pipewireSink")
		standIn.PipewireSink = StandIn
		sys.modules[standIn.__name__] = standIn
		captureDir = {str(captureDir)!r}
		chosen = {{}}
		for sinkName in (None, "", "null", "capture", "pipewire", "alsa", "Null"):
			for directory in (None, captureDir):
				for name, value in (("BASEDREADER_AUDIO_SINK", sinkName), ("BASEDREADER_AUDIO_CAPTURE_DIR", directory)):
					if value is None:
						os.environ.pop(name, None)
					else:
						os.environ[name] = value
				audioOutput._sink = None
				try:
					sink = audioOutput.getSink()
					outcome = [type(sink).__name__, sink.kind, getattr(sink, "directory", None)]
				except OSError as e:
					outcome = [type(e).__name__, str(e)]
				chosen[f"{{sinkName}} {{'with' if directory else 'without'}} directory"] = outcome
		import wasapi

		os.environ["BASEDREADER_AUDIO_SINK"] = "alsa"
		audioOutput._sink = None
		try:
			wasapi.wasPlay_startup()
			startup = None
		except OSError as e:
			startup = [type(e).__name__, e.winerror, str(e)]
		result = {{"chosen": chosen, "startup": startup}}
		""",
		prepare="bootstrap",
		env={"BASEDREADER_AUDIO_SINK": None, "BASEDREADER_AUDIO_CAPTURE_DIR": None},
	)
	done.assertClean()
	null = ["NullSink", "null", None]
	capture = ["CaptureSink", "capture", str(captureDir)]
	noDirectory = [
		"FileNotFoundError",
		(
			"[Errno 2] BASEDREADER_AUDIO_SINK is capture, but BASEDREADER_AUDIO_CAPTURE_DIR names no "
			"directory to capture to"
		),
	]

	def unknown(name: str) -> list:
		return [
			"OSError",
			f"[Errno 22] BASEDREADER_AUDIO_SINK must be null, capture or pipewire, not {name!r}",
		]

	assert done.value["chosen"] == {
		"None without directory": null,
		"None with directory": capture,
		" without directory": null,
		" with directory": capture,
		"null without directory": null,
		"null with directory": null,
		"capture without directory": noDirectory,
		"capture with directory": capture,
		"pipewire without directory": ["StandIn", "pipewire", None],
		"pipewire with directory": ["StandIn", "pipewire", None],
		"alsa without directory": unknown("alsa"),
		"alsa with directory": unknown("alsa"),
		"Null without directory": unknown("Null"),
		"Null with directory": unknown("Null"),
	}
	assert done.value["startup"] == [
		"WindowsError",
		-2147467259,
		"[Errno 22] BASEDREADER_AUDIO_SINK must be null, capture or pipewire, not 'alsa'",
	]


def test_captureSinkKeepsAudioAtTheProcessGain(tmp_path: Path):
	"""Sink.setGain on the capture sink: 0.5 is -6.0 dB on 16- and 24-bit audio, full scale stays
	in range, audio heard before a change keeps the gain it was heard at, and a gain outside 0 to 1
	is refused by both sinks."""
	captureDir = tmp_path / "capture"
	captureDir.mkdir()
	value = runAudio(
		tmp_path,
		"""
		import math
		import struct

		from linuxPlatform import audioOutput
		from linuxPlatform.audioOutput.nullSink import NullSink

		def tone(bits, frames):
			peak = 2 ** (bits - 1) - 1
			samples = [round(0.5 * peak * math.sin(2 * math.pi * 440 * i / 8000)) for i in range(frames)]
			return b"".join(s.to_bytes(bits // 8, "little", signed=True) for s in samples)

		def fullScale(bits):
			return b"".join(
				s.to_bytes(bits // 8, "little", signed=True)
				for s in (2 ** (bits - 1) - 1, -(2 ** (bits - 1))) * 400
			)

		def play(bits, data):
			player = nvwave.WavePlayer(1, 8000, bits, wantDucking=False)
			player.enableTrimmingLeadingSilence(False)
			player.feed(data)
			player.sync()
			del player
			gc.collect()

		sink = audioOutput.getSink()
		gainAtStart = sink.gain
		sources = []
		for gain in (1.0, 0.5):
			sink.setGain(gain)
			for bits in (16, 24):
				for data in (tone(bits, 4000), fullScale(bits)):
					play(bits, data)
					sources.append([gain, bits, data.hex()])
		# A second at gain 1, which halves while the end of it is still to be heard.
		sink.setGain(1.0)
		player = nvwave.WavePlayer(1, 8000, 16, wantDucking=False)
		player.enableTrimmingLeadingSilence(False)
		started = time.monotonic()
		# Returns with 400 ms still to be heard.
		player.feed(struct.pack("<8000h", *([10000] * 8000)))
		time.sleep(0.05)
		sink.setGain(0.5)
		changedAt = time.monotonic() - started
		player.sync()
		del player
		gc.collect()
		refused = []
		for refusing in (sink, NullSink()):
			for level in (-0.01, 1.01, float("nan")):
				try:
					refusing.setGain(level)
				except ValueError as e:
					refused.append(str(e))
		result = {"gainAtStart": gainAtStart, "sources": sources, "changedAt": changedAt, "refused": refused}
		""",
		captureDir=captureDir,
	)
	assert value["gainAtStart"] == 1.0
	files = captured(captureDir)
	assert len(files) == len(value["sources"]) + 1

	def samples(data: bytes, width: int) -> list[int]:
		return [
			int.from_bytes(data[i : i + width], "little", signed=True) for i in range(0, len(data), width)
		]

	def rms(values: list[int]) -> float:
		return math.sqrt(sum(v * v for v in values) / len(values))

	levels = {}
	for (gain, bits, source), got in zip(value["sources"], files):
		assert (got["channels"], got["width"]) == (1, bits // 8)
		heard, written = samples(got["frames"], bits // 8), samples(bytes.fromhex(source), bits // 8)
		assert len(heard) == len(written)
		limit = 2 ** (bits - 1)
		assert all(-limit <= s < limit for s in heard)
		if gain == 1.0:
			assert heard == written
		levels.setdefault(bits, []).append(rms(heard))
	for bits, (tone, fullScale, halfTone, halfFullScale) in levels.items():
		assert abs(20 * math.log10(halfTone / tone) + 6.0) < 0.1, bits
		assert abs(20 * math.log10(halfFullScale / fullScale) + 6.0) < 0.1, bits
	changed = samples(files[-1]["frames"], 2)
	assert len(changed) == 8000
	firstHalved = changed.index(5000)
	assert set(changed[:firstHalved]) == {10000} and set(changed[firstHalved:]) == {5000}
	# The clock says when each frame was heard; the call was made within a few milliseconds.
	assert abs(firstHalved / 8000 - value["changedAt"]) < 0.02
	assert (
		value["refused"]
		== [f"a gain of {level} is not between 0 and 1" for level in ("-0.01", "1.01", "nan")] * 2
	)


def test_deliveredFramesCountWhatReachedAnOutput(tmp_path: Path):
	"""The capture sink counts the frames it kept, as they are heard, and only those; the null sink,
	which keeps nothing, counts none."""
	captureDir = tmp_path / "capture"
	captureDir.mkdir()
	code = """
		from linuxPlatform import audioOutput

		sink = audioOutput.getSink()
		counts = {"atStart": sink.deliveredFrames()}
		player = nvwave.WavePlayer(1, 8000, 16, wantDucking=False, purpose=nvwave.AudioPurpose.SOUNDS)
		player.feed(bytes(1600))  # 100 ms
		player.sync()
		counts["afterOne"] = sink.deliveredFrames()
		started = time.monotonic()
		# 1 s, which returns with 400 ms still to be heard, cut off by the stop.
		player.feed(bytes(16000))
		time.sleep(0.05)
		counts["whilePlaying"] = sink.deliveredFrames()
		counts["playingFor"] = time.monotonic() - started
		player.stop()
		counts["afterStop"] = sink.deliveredFrames()
		del player
		gc.collect()
		counts["atEnd"] = sink.deliveredFrames()
		result = {"kind": sink.kind, "counts": counts}
		"""
	null = runAudio(tmp_path, code)
	assert null == {
		"kind": "null",
		"counts": dict.fromkeys(("atStart", "afterOne", "whilePlaying", "afterStop", "atEnd"), 0)
		| {"playingFor": null["counts"]["playingFor"]},
	}
	capture = runAudio(tmp_path, code, captureDir=captureDir)
	assert capture["kind"] == "capture"
	counts = capture["counts"]
	keptFrames = sum(len(f["frames"]) // 2 for f in captured(captureDir))
	assert counts["atStart"] == 0
	assert counts["afterOne"] == 800
	playedFor = counts["playingFor"]
	assert 800 + (playedFor - 0.05) * 8000 <= counts["whilePlaying"] <= 800 + (playedFor + 0.01) * 8000
	assert counts["whilePlaying"] <= counts["afterStop"] == counts["atEnd"] == keptFrames < 800 + 8000


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
