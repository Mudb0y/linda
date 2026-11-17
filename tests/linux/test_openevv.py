"""openevv's NVDA driver, loaded from its own directory, speaking through NVDA into the capture sink.

The driver is not part of this tree. It lives in openevv's source, and a build lays it out in a
directory of its own with the engine library beside it, which LINDA_OPENEVV_DRIVER_DIR names:
openevv.py, _openevv.py and _openevv_dictionaries.py from openevv's nvda/addon/synthDrivers with the
series in nix/patches/openevv applied, and openevv_engine/libeci.so.1. The dev shell names the
directory nix/openevv-driver.nix builds. Every test but the first needs it, and fails without it. Run
under the rig:

	nix develop . -c tests/linux/rig python3 -m pytest tests/linux/test_openevv.py

A directory built some other way is named with env, and passed with --bind when it is outside the Nix
store, which the rig sees, and outside the checkout:

	nix develop . -c tests/linux/rig --bind DIR env LINDA_OPENEVV_DRIVER_DIR=DIR python3 -m pytest ...

The tests of cancelling, marks and settings each drive the case a driver of this shape gets wrong:
a cancel landing in the last wait for the player, a cancel landing between the engine callback's
check for one and what it does with a buffer or a mark, two marks with no audio between them, a mark
alone after a cancel, and a setting made on the driver rather than through NVDA's settings ring.
"""

import os
import textwrap
from pathlib import Path

import harness
import pytest
from pipewireFixture import PipewireFixture, assertSpeechHeard

DRIVER_DIR_VARIABLE = "LINDA_OPENEVV_DRIVER_DIR"


def requireDriverDirectory() -> None:
	"""Fail the test, rather than skip it, when there is no driver directory: the dev shell always names
	one, so a run without it has lost the voice the tests are for."""
	directory = os.environ.get(DRIVER_DIR_VARIABLE)
	if not directory:
		pytest.fail(
			f"{DRIVER_DIR_VARIABLE} is unset. Run the tests in the dev shell, which sets it to the directory "
			"nix/openevv-driver.nix builds, or set it to a directory holding openevv's three driver files "
			"with nix/patches/openevv applied, beside openevv_engine/libeci.so.1.",
			pytrace=False,
		)
	for name in ("openevv.py", "openevv_engine/libeci.so.1"):
		if not os.path.isfile(os.path.join(directory, name)):
			pytest.fail(f"{DRIVER_DIR_VARIABLE} is {directory}, which holds no {name}.", pytrace=False)


#: Brings speech up as core.main does with openevv configured, into the capture sink, and records
#: when the sink is written to and when the driver reports indexes and the end of an utterance.
SPEECH = """
import statistics
import threading
import time
import wave
from array import array

import core
# core.main's first import after config; entering NVDA's import cycle anywhere else fails.
import NVDAHelper
from linuxPlatform import synthDriverPaths

synthDriverPaths.install()
import characterProcessing
import config
import nvwave
import queueHandler
import speech
import speechDictHandler
import synthDriverHandler
import synthDrivers._openevv as engineModule
from linuxPlatform.audioOutput import captureSink
from speech.commands import IndexCommand

now = time.monotonic
writes = []
_write = captureSink.CaptureStream.write


def write(self, data):
	writes.append(now())
	_write(self, data)


captureSink.CaptureStream.write = write
# The capture sink keeps a frame, in a WAV file, once its clock says the frame was heard, and does so
# whenever the player asks the stream where it is or stops it. What it keeps is recorded here as it
# is kept, since a file being written is not complete on disk.
kept = []
_writeframes = wave.Wave_write.writeframes


def writeframes(self, data):
	kept.append((now(), bytes(data)))
	_writeframes(self, data)


wave.Wave_write.writeframes = writeframes
nvwave.initialize()
speechDictHandler.initialize()
characterProcessing.initialize()
config.conf["speech"]["synth"] = "openevv"
speech.initialize()
synth = synthDriverHandler.getSynth()
assert synth.name == "openevv", synth.name
engine = synth._engine


class Heard:
	# extensionPoints holds handlers weakly, so these are bound methods of an object kept alive here.
	def __init__(self):
		self.events = []
		synthDriverHandler.synthIndexReached.register(self.onIndex)
		synthDriverHandler.synthDoneSpeaking.register(self.onDone)

	def onIndex(self, synth, index):
		self.events.append(("index", index, now()))

	def onDone(self, synth):
		self.events.append(("done", None, now()))

	def since(self, t):
		return self.between(t, float("inf"))

	def between(self, start, end):
		return [(kind, index) for kind, index, at in self.events if start <= at < end]


heard = Heard()


def pump(seconds):
	end = now() + seconds
	while now() < end:
		queueHandler.pumpAll()
		time.sleep(0.002)


def waitFor(condition, timeout=20.0):
	end = now() + timeout
	while now() < end:
		queueHandler.pumpAll()
		if condition():
			return True
		time.sleep(0.001)
	return False


def speakAndWait(sequence):
	start = now()
	synth.speak(sequence)
	assert waitFor(lambda: ("done", None) in heard.since(start)), "the utterance never finished"
	return heard.since(start)


def heardSince(t):
	samples = array("h")
	for at, chunk in kept:
		if at >= t:
			samples.frombytes(chunk)
	return samples


def peak(samples):
	return max(max(samples), -min(samples)) if samples else 0


LONG = (
	"This is a long passage that goes on for a while so that a cancel lands in the middle of it, "
	"and it keeps going with more words after the first comma, then a second clause about nothing much, "
	"and a third one for good measure, all without a full stop until the very end of this rather long line."
)
"""

END = """
synthDriverHandler.setSynth(None)
"""


def runOpenevv(tmpPath: Path, code: str) -> harness.ChildResult:
	requireDriverDirectory()
	capture = tmpPath / "capture"
	capture.mkdir()
	return harness.runChild(
		tmpPath,
		SPEECH + textwrap.dedent(code) + END,
		env={"LINDA_AUDIO_CAPTURE_DIR": str(capture)},
		timeout=600,
	)


def test_installAddsTheDriverDirectoryOnceAtTheEnd(tmp_path: Path):
	withDriver = tmp_path / "withDriver"
	withDriver.mkdir()
	(withDriver / "openevv.py").write_text("")
	withoutDriver = tmp_path / "withoutDriver"
	withoutDriver.mkdir()
	(withoutDriver / "_openevv.py").write_text("")
	link = tmp_path / "link"
	link.symlink_to(withDriver)
	other = tmp_path / "other"
	other.mkdir()
	(other / "openevv.py").write_text("")
	done = harness.runChild(
		tmp_path,
		f"""
		import os
		import synthDrivers
		from linuxPlatform import synthDriverPaths
		variable = synthDriverPaths.openevvDriverDirVariable
		before = list(synthDrivers.__path__)
		os.environ.pop(variable, None)
		unset = synthDriverPaths.install()
		os.environ[variable] = ""
		empty = synthDriverPaths.install()
		os.environ[variable] = {str(withoutDriver)!r}
		noDriver = synthDriverPaths.install()
		os.environ[variable] = {str(tmp_path / "absent")!r}
		absent = synthDriverPaths.install()
		unchanged = list(synthDrivers.__path__) == before
		os.environ[variable] = {str(withDriver)!r}
		first = synthDriverPaths.install()
		second = synthDriverPaths.install()
		os.environ[variable] = {str(link)!r}
		throughLink = synthDriverPaths.install()
		os.environ[variable] = {str(tmp_path) + "/./withDriver"!r}
		dotted = synthDriverPaths.install()
		after = list(synthDrivers.__path__)
		synthDrivers.__path__.append({str(tmp_path) + "/./other"!r})
		os.environ[variable] = {str(other)!r}
		alreadyThereDotted = synthDriverPaths.install()
		result = {{
			"variable": variable,
			"nothingAdded": [unset, empty, noDriver, absent],
			"unchanged": unchanged,
			"first": first,
			"second": second,
			"sameDirectory": [throughLink, dotted, alreadyThereDotted],
			"before": before,
			"after": after,
		}}
		""",
		prepare="bootstrap",
	)
	done.assertClean()
	value = done.value
	assert value["variable"] == DRIVER_DIR_VARIABLE
	assert value["nothingAdded"] == [[], [], [], []]
	assert value["unchanged"]
	assert value["first"] == [os.path.realpath(withDriver)]
	assert value["second"] == []
	assert value["sameDirectory"] == [[], [], []]
	# Last, so that NVDA's own drivers and an add-on's driver of the same name come first.
	assert value["after"] == value["before"] + [os.path.realpath(withDriver)]


def test_speaksEnglishAndPolishWithEveryIndexInOrder(tmp_path: Path):
	done = runOpenevv(
		tmp_path,
		"""
		listed = [name for name, _description in synthDriverHandler.getSynthList()]
		mark = now()
		english = speakAndWait(["Documents folder, 3 of 12.", IndexCommand(1), "OK button.", IndexCommand(2)])
		englishAudio = heardSince(mark)
		englishLanguage = synth.language
		polishVoice = next(v.id for v in synth.availableVoices.values() if v.language == "pl_PL")
		synth.voice = polishVoice
		assert waitFor(lambda: synth.language == "pl_PL", 5)
		mark = now()
		polish = speakAndWait(["Zażółć gęślą jaźń.", IndexCommand(3), "Dzień dobry.", IndexCommand(4)])
		polishAudio = heardSince(mark)
		result = {
			"listed": "openevv" in listed,
			"english": english,
			"englishLanguage": englishLanguage,
			"englishSeconds": len(englishAudio) / engine.sampleRate,
			"englishPeak": peak(englishAudio),
			"polish": polish,
			"polishSeconds": len(polishAudio) / engine.sampleRate,
			"polishPeak": peak(polishAudio),
		}
		""",
	)
	done.assertClean()
	value = done.value
	assert value["listed"]
	assert value["englishLanguage"] == "en_US"
	assert value["english"] == [["index", 1], ["index", 2], ["done", None]]
	assert value["polish"] == [["index", 3], ["index", 4], ["done", None]]
	# Speech, not silence: a voice peaks far above the noise floor and lasts as long as the words.
	assert value["englishPeak"] > 5000 and value["englishSeconds"] > 1.0
	assert value["polishPeak"] > 5000 and value["polishSeconds"] > 1.0


def test_cancelIsReadyAgainQuicklyAtAnySampleRate(tmp_path: Path):
	# Ready is when the engine's thread clears the cancel, which it can do only once the engine has
	# let go of the cancelled utterance. Answering eciDataAbort lets it go without making the rest of
	# the audio, which otherwise costs the time to make it at the sample rate in force.
	done = runOpenevv(
		tmp_path,
		"""
		resumed = []
		_resume = engineModule.Engine._resume


		def resume(self):
			resumed.append(now())
			_resume(self)


		engineModule.Engine._resume = resume


		def cancelToReady(rounds, busySeconds):
			times = []
			for _round in range(rounds):
				start = now()
				synth.speak([LONG, IndexCommand(1)])
				assert waitFor(lambda: any(t > start for t in writes)), "the long sentence never played"
				time.sleep(0.25)
				cancelled = now()
				synth.cancel()
				synth.speak(["OK.", IndexCommand(2)])
				busyUntil = now() + busySeconds
				while now() < busyUntil:
					pass
				assert waitFor(lambda: ("index", 2) in heard.since(cancelled)), "the next item never played"
				times.append(min(t for t in resumed if t >= cancelled) - cancelled)
				pump(0.05)
			return round(statistics.median(times) * 1000, 1)


		medians = {}
		for rate in ("11025", "48000"):
			synth.samplerate = rate
			assert waitFor(lambda: engine.sampleRate == int(rate), 5)
			medians[rate] = {"idle": cancelToReady(20, 0), "busy": cancelToReady(20, 0.05)}
		result = medians
		""",
	)
	done.assertClean()
	# Busy is NVDA's main thread holding the GIL for 50 ms after the cancel, as handling a key press
	# does; making the cancelled audio needs the GIL for every buffer, aborting does not. Medians in
	# milliseconds, by sample rate. At 48 kHz making the rest of the sentence instead costs more than
	# twice the bound; at 11,025 Hz there is too little of it to tell the two apart, and the bound only
	# holds the cancel to what a key press can wait. Aborting takes about half the bound, which leaves
	# room for a machine that is busy with other work.
	assert all(median < 150 for medians in done.value.values() for median in medians.values()), done.value


def test_noDoneAfterACancelInTheLastWait(tmp_path: Path):
	done = runOpenevv(
		tmp_path,
		"""
		lastWait = []
		_flush = engineModule.Engine._flush


		def flush(self, last=False):
			if last:
				lastWait.append(now())
			return _flush(self, last)


		engineModule.Engine._flush = flush
		landed = doneAfterCancel = 0
		for number in range(30):
			start = now()
			synth.speak(["Documents folder, 3 of 12.", IndexCommand(number)])
			assert waitFor(lambda: any(t > start for t in lastWait), 10), "the utterance never ended"
			time.sleep(0.05)
			cancelled = now()
			synth.cancel()
			pump(0.4)
			if ("done", None) not in heard.between(start, cancelled):
				# Synthesis was over and the end not yet reported, so the cancel landed in the wait.
				landed += 1
				doneAfterCancel += ("done", None) in heard.since(cancelled)
		after = speakAndWait(["OK button.", IndexCommand(99)])
		result = {"landed": landed, "doneAfterCancel": doneAfterCancel, "after": after}
		""",
	)
	done.assertClean()
	value = done.value
	assert value["landed"] >= 20, value
	assert value["doneAfterCancel"] == 0, value
	assert value["after"] == [["index", 99], ["done", None]]


def test_noCancelledAudioPlaysAfterTheCancel(tmp_path: Path):
	# The engine's callback checks for a cancel and then feeds the player. A cancel between the two
	# used to leave the feed to restart the stopped player with a buffer of the cancelled speech.
	# Holding the engine's thread there makes the window as wide as the test needs.
	done = runOpenevv(
		tmp_path,
		"""
		gate = {"armed": False, "inWindow": threading.Event(), "proceed": threading.Event()}
		_flush = engineModule.Engine._flush


		def flush(self, last=False):
			if gate["armed"] and not last:
				gate["armed"] = False
				gate["inWindow"].set()
				gate["proceed"].wait(1.0)
			return _flush(self, last)


		engineModule.Engine._flush = flush
		heardAfterCancel = []
		for number in range(10):
			start = now()
			synth.speak([LONG, IndexCommand(number)])
			assert waitFor(lambda: any(t > start for t in writes)), "the long sentence never played"
			time.sleep(0.3 + 0.05 * (number % 5))
			gate["inWindow"].clear()
			gate["proceed"].clear()
			gate["armed"] = True
			assert gate["inWindow"].wait(2.0), "the engine never fed the player"
			synth.cancel()
			# Stopping the player kept everything heard up to the cancel.
			mark = now()
			gate["proceed"].set()
			pump(0.6)
			# Stopped again, so that whatever played since is written down too.
			synth.cancel()
			heardAfterCancel.append(len(heardSince(mark)))
			pump(0.1)
		result = {"heardAfterCancel": heardAfterCancel, "sampleRate": engine.sampleRate}
		""",
	)
	done.assertClean()
	oneMillisecond = done.value["sampleRate"] // 1000
	assert all(frames <= oneMillisecond for frames in done.value["heardAfterCancel"]), done.value


def test_noMarkOfACancelledUtteranceIsReported(tmp_path: Path):
	# The same window at a mark with no audio in hand in front of it, which the callback either hangs
	# on the audio already fed, when there is some, or reports at once. A mark reported after the
	# cancel tells NVDA that speech it silenced was reached, and one left with the player is reported
	# in the next utterance.
	done = runOpenevv(
		tmp_path,
		"""
		gate = {"armed": False, "inWindow": threading.Event(), "proceed": threading.Event()}
		_flush = engineModule.Engine._flush


		def flush(self, last=False):
			if gate["armed"] and not last and not self._held and self._pendingIndexes:
				gate["armed"] = False
				gate["inWindow"].set()
				gate["proceed"].wait(1.0)
			return _flush(self, last)


		engineModule.Engine._flush = flush
		rounds = []
		for number in range(8):
			cancelledMarks = {100 + number * 10, 101 + number * 10}
			first, second = sorted(cancelledMarks)
			if number % 2:
				# The first mark comes before any audio of the utterance has been fed.
				sequence = [IndexCommand(first), LONG, IndexCommand(second)]
			else:
				# The second of two adjacent marks, after the audio in front of the first was fed.
				sequence = ["Hello world, this is the first part.", IndexCommand(first), IndexCommand(second), LONG]
			gate["inWindow"].clear()
			gate["proceed"].clear()
			gate["armed"] = True
			synth.speak(sequence)
			assert gate["inWindow"].wait(5.0), "the engine never reached a mark with nothing in hand"
			cancelled = now()
			synth.cancel()
			gate["proceed"].set()
			pump(0.3)
			following = speakAndWait(["OK button.", IndexCommand(900 + number)])
			pump(0.2)
			rounds.append({
				"cancelledReported": [index for _kind, index in heard.since(cancelled) if index in cancelledMarks],
				"following": following,
				"number": number,
			})
		result = rounds
		""",
	)
	done.assertClean()
	for value in done.value:
		assert value["cancelledReported"] == [], done.value
		assert value["following"] == [["index", 900 + value["number"]], ["done", None]], done.value


def test_indexesArriveInOrder(tmp_path: Path):
	done = runOpenevv(
		tmp_path,
		"""
		def timed(sequence):
			start = now()
			speakAndWait(sequence)
			return [(kind, index, round((at - start) * 1000)) for kind, index, at in heard.events if at >= start]


		adjacent = timed([
			"Hello world, this is the first part.",
			IndexCommand(1),
			IndexCommand(2),
			"And this is the second part.",
			IndexCommand(3),
		])
		first = speakAndWait([IndexCommand(4), "OK button.", IndexCommand(5)])
		# After a cancel the player holds nothing and has stopped, so a mark hung on what it holds
		# would never be reached.
		start = now()
		synth.speak([LONG, IndexCommand(7)])
		assert waitFor(lambda: any(t > start for t in writes)), "the long sentence never played"
		synth.cancel()
		alone = speakAndWait([IndexCommand(6)])
		result = {"adjacent": adjacent, "first": first, "alone": alone}
		""",
	)
	done.assertClean()
	value = done.value
	adjacent = value["adjacent"]
	assert [index for _kind, index, _at in adjacent] == [1, 2, 3, None], adjacent
	# Two marks with no audio between them belong to one place in the speech, after the first part.
	assert adjacent[1][2] - adjacent[0][2] < 50, adjacent
	assert value["first"] == [["index", 4], ["index", 5], ["done", None]]
	# An utterance with no audio at all still reports its mark and its end, and nothing of the
	# cancelled one.
	assert value["alone"] == [["index", 6], ["done", None]]


def test_settingsReachTheConfiguration(tmp_path: Path):
	done = runOpenevv(
		tmp_path,
		"""
		import synthSettingsRing

		section = config.conf["speech"]["openevv"]
		ring = synthSettingsRing.SynthSettingsRing(synth)
		next(s for s in ring.settings if s.setting.id == "rate").value = 60
		assert waitFor(lambda: synth.rate == 60, 5)
		config.pre_configSave.notify()
		fromRing = section["rate"]
		# What the voice settings dialog does while it is open, and what an add-on or the Python
		# console does: the driver's setting changes and the configuration is not written.
		synth.rate = 70
		assert waitFor(lambda: synth.rate == 70, 5)
		config.pre_configSave.notify()
		onSave = section["rate"]
		# The engine takes a setting between utterances, so a save during a long one comes before
		# the engine has the setting the ring just wrote.
		start = now()
		synth.speak([LONG, IndexCommand(1)])
		assert waitFor(lambda: any(t > start for t in writes)), "the long sentence never played"
		next(s for s in ring.settings if s.setting.id == "rate").value = 40
		config.pre_configSave.notify()
		duringSpeech = section["rate"]
		synth.cancel()
		synth.rate = 30
		assert waitFor(lambda: synth.rate == 30, 5)
		synthDriverHandler.setSynth(None)
		onSwitch = section["rate"]
		result = {"fromRing": fromRing, "onSave": onSave, "duringSpeech": duringSpeech, "onSwitch": onSwitch}
		""",
	)
	done.assertClean()
	assert done.value == {"fromRing": 60, "onSave": 70, "duringSpeech": 40, "onSwitch": 30}


def test_anUnknownCommandIsNoError(tmp_path: Path):
	# An error record plays NVDA's error sound in source builds, for a command the driver can skip.
	done = runOpenevv(
		tmp_path,
		"""
		from speech.commands import PhonemeCommand

		result = speakAndWait(["OK", PhonemeCommand("bʌtən", text="button"), IndexCommand(1)])
		""",
	)
	done.assertClean()
	assert done.value == [["index", 1], ["done", None]]


def test_isHeardThroughPipewire(pipewire: PipewireFixture, tmp_path: Path):
	requireDriverDirectory()
	assertSpeechHeard(
		pipewire, tmp_path, "openevv", "This is openevv, speaking through Linda and PipeWire."
	)
