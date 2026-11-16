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
	assert 0.08 <= returned[3] <= 0.15
	assert 0.6 <= returned[9] <= 0.85
	for i, at in returned.items():
		# Playing began after the clock was read, so this overstates what was still queued, which a
		# feed that waits only while more than half of 400 ms is queued keeps to 300 ms at most.
		queued = (i + 1) * 0.1 - at
		assert queued <= 0.3 + 0.001, (i, queued)
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
					self._running = True

			def stop(self):
				with self._lock:
					self._running = False

			def reset(self):
				with self._lock:
					self._written = self._taken = 0
					self._dueAt = None

			def setChannelVolume(self, channel, level):
				pass

			def close(self):
				self._closed.set()

		class RushingSink(audioOutput.Sink):
			description = "a sink whose device runs ahead"

			def openStream(self, deviceId, fmt, wake):
				return RushingStream(fmt, wake)

			def devices(self):
				return []

			def keepAwakeFor(self, deviceId, ms, noiseVolume):
				pass

			def stopKeepingAwake(self):
				pass

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
	# 100 ms seven times; woken, each wait lasts about 20 ms.
	assert value["took"] < 0.4, value["took"]
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

		def feedLong():
			feeding.feed(bytes(32000), onDone=done("fed"))  # 2 s, five times the buffer
			feeding.idle()

		feeder = threading.Thread(target=feedLong, name="feeder")
		feeder.start()
		syncing.feed(bytes(16000), onDone=done("synced"))  # 1 s
		syncer = threading.Thread(target=syncing.idle, name="syncer")
		syncer.start()
		time.sleep(0.1)
		start = time.monotonic()
		wasapi._closeForExit()
		hookTook = time.monotonic() - start
		feeder.join(3)
		syncer.join(3)
		late = nvwave.WavePlayer(1, 8000, 16, wantDucking=False, purpose=nvwave.AudioPurpose.SOUNDS)
		late.feed(bytes(1600), onDone=done("late"))
		late.sync()
		result = {
			"hookTook": hookTook,
			"alive": [thread.name for thread in (feeder, syncer) if thread.is_alive()],
			"closed": closed,
			"opened": RecordingSink.opened,
			"calls": [tag for tag, _thread, _at in events],
		}
		""",
	)
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
		import time

		import NVDAHelper
		import nvwave
		from linuxPlatform import audioOutput
		from linuxPlatform.audioOutput.nullSink import NullSink, NullStream


		def note(what):
			with open({str(record)!r}, "a", encoding="utf-8") as f:
				f.write(what + "\\n")


		class RecordingStream(NullStream):
			def write(self, data):
				note("write")
				super().write(data)

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
		time.sleep(0.3)
		result = "exiting"
		""",
	)
	done.assertClean()
	assert done.returnCode == 0, done.stderr
	assert "Traceback" not in done.stderr, done.stderr
	calls = record.read_text(encoding="utf-8").split()
	assert calls[0] == "open"
	assert "write" in calls
	assert calls[-1] == "close" and calls.count("close") == 1, calls


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
