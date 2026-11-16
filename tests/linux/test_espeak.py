"""NVDA's eSpeak NG driver on Linux, speaking through NVDA into the capture sink.

The library and its data are the ones the dev shell names in BASEDREADER_ESPEAK_LIB and
BASEDREADER_ESPEAK_DATA_DIR: eSpeak NG at the commit NVDA pins, built without audio output. Run
under the rig:

	nix develop . -c tests/linux/rig python3 -m pytest tests/linux/test_espeak.py

A run that loads eSpeak maps a library whose name the harness counts among sound libraries, so those
runs ignore the audio check and then require that what it found is that library and nothing else.

The tests of cancelling each hold the driver's thread where a cancel used to slip past it: between
the callback's check for a cancel and its feed of the player, and in the wait for the player at the
end of an utterance.
"""

import itertools
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import harness
import rununittests

LIBRARY_VARIABLE = "BASEDREADER_ESPEAK_LIB"
DATA_DIR_VARIABLE = "BASEDREADER_ESPEAK_DATA_DIR"


def assertOnlyEspeakReached(done: harness.ChildResult) -> None:
	"""What the audio check found, in a run that loaded eSpeak and so ignored that check, is eSpeak NG's
	library and nothing else: the file BASEDREADER_ESPEAK_LIB names, built without audio output."""
	library = os.path.realpath(os.environ[LIBRARY_VARIABLE])
	# Each finding is "loaded PATH" or "mapped PATH".
	reached = {os.path.realpath(what.split(" ", 1)[1]) for what in done.audio}
	assert reached == {library}, done.audio


#: Brings speech up as core.main does with eSpeak configured, into the capture sink, and records when
#: the sink is written to, what it keeps as heard, and when the driver reports indexes and the end of
#: an utterance. LANGUAGE is the interface language the test sets.
SPEECH = """
import random
import statistics
import threading
import time
import wave
from array import array

import core
# core.main's first import after config; entering NVDA's import cycle anywhere else fails.
import NVDAHelper
import characterProcessing
import config
import languageHandler
import locale
import nvwave
import queueHandler
import speech
import speechDictHandler
import synthDriverHandler
import wasapi
from linuxPlatform.audioOutput import captureSink
from speech.commands import IndexCommand, LangChangeCommand
from synthDrivers import _espeak

now = time.monotonic
writes = []
_write = captureSink.CaptureStream.write


def write(self, data):
	writes.append((now(), bytes(data)))
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
languageHandler.setLanguage(LANGUAGE)
characterLocaleBefore = locale.setlocale(locale.LC_CTYPE)
nvwave.initialize()
speechDictHandler.initialize()
characterProcessing.initialize()
config.conf["speech"]["synth"] = "espeak"
speech.initialize()
characterLocaleAfter = locale.setlocale(locale.LC_CTYPE)
synth = synthDriverHandler.getSynth()
assert synth.name == "espeak", synth.name
sampleRate = _espeak.player.samplesPerSec


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
		return [(kind, index) for kind, index, at in self.events if at >= t]

	def timedSince(self, t):
		return [(kind, index, at - t) for kind, index, at in self.events if at >= t]


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


def spokenAudio(sequence):
	start = now()
	events = speakAndWait(sequence)
	return events, heardSince(start)


def peak(samples):
	return max(max(samples), -min(samples)) if samples else 0


def rms(samples):
	return (sum(s * s for s in samples) / len(samples)) ** 0.5 if samples else 0


def pitch(samples):
	# The median over voiced frames of the lag, between 60 and 300 Hz, at which a frame is most like
	# itself, at half the sample rate, which is plenty for a voice's fundamental.
	half = samples[::2]
	rate = sampleRate / 2
	shortest, longest = int(rate / 300), int(rate / 60)
	frame = 512
	found = []
	for start in range(0, len(half) - frame - longest, frame * 2):
		window = half[start : start + frame + longest]
		energy = sum(s * s for s in window[:frame])
		if energy < frame * 1000**2:
			continue
		best, bestLag = 0.0, 0
		for lag in range(shortest, longest):
			score = sum(window[i] * window[i + lag] for i in range(frame)) / energy
			if score > best:
				best, bestLag = score, lag
		if best > 0.5:
			found.append(rate / bestLag)
	return statistics.median(found) if found else 0


LONG = (
	"navigating the web with a screen reader means listening to a great deal of text "
	"and each line is announced as the cursor reaches it and the user interrupts it constantly "
	"so speech must stop the instant a key is pressed or the next announcement comes late "
	"which is why the time from a key press to silence matters more than anything else does"
)
"""

END = """
synthDriverHandler.setSynth(None)
"""


def runEspeak(tmpPath: Path, code: str, language: str = "en") -> harness.ChildResult:
	capture = tmpPath / "capture"
	capture.mkdir()
	done = harness.runChild(
		tmpPath,
		f"LANGUAGE = {language!r}\n" + SPEECH + textwrap.dedent(code) + END,
		env={"BASEDREADER_AUDIO_CAPTURE_DIR": str(capture)},
		timeout=600,
	)
	done.assertClean(ignoring={"audio"})
	assertOnlyEspeakReached(done)
	return done


def test_filesAreWhereTheVariablesSay(tmp_path: Path):
	notData = tmp_path / "notData"
	notData.mkdir()
	done = harness.runChild(
		tmp_path,
		f"""
		import os
		from linuxPlatform import espeakFiles

		def outcomes(variable, function):
			found = []
			for value in ("", {str(notData)!r}, os.environ[variable]):
				os.environ[variable] = value
				try:
					found.append(function())
				except FileNotFoundError as e:
					found.append(str(e))
			return found

		result = {{
			"variables": [espeakFiles.libraryVariable, espeakFiles.dataDirVariable],
			"library": outcomes(espeakFiles.libraryVariable, espeakFiles.libraryPath),
			"data": outcomes(espeakFiles.dataDirVariable, espeakFiles.dataDir),
		}}
		""",
		prepare="bootstrap",
	)
	done.assertClean()
	value = done.value
	assert value["variables"] == [LIBRARY_VARIABLE, DATA_DIR_VARIABLE]
	assert value["library"] == [
		f"[Errno 2] {LIBRARY_VARIABLE} is not set, so eSpeak NG cannot be found",
		f"[Errno 2] {LIBRARY_VARIABLE} names no file: '{notData}'",
		os.environ[LIBRARY_VARIABLE],
	]
	assert value["data"] == [
		f"[Errno 2] {DATA_DIR_VARIABLE} is not set, so eSpeak NG cannot be found",
		f"[Errno 2] {DATA_DIR_VARIABLE} names no eSpeak NG data: '{notData}'",
		os.environ[DATA_DIR_VARIABLE],
	]


def test_speaksEnglishAndPolishWithEveryIndexInOrder(tmp_path: Path):
	done = runEspeak(
		tmp_path,
		"""
		english, englishAudio = spokenAudio(["Documents folder, 3 of 12,", IndexCommand(1), "OK button", IndexCommand(2)])
		first = {"voice": synth.voice, "language": synth.language}
		# The first voice was stored as eSpeak names it, with its directory; changing voice looks the
		# new one up among availableVoices, which are keyed without it.
		synth.voice = "pl"
		polish, polishAudio = spokenAudio(["Zażółć gęślą jaźń,", IndexCommand(3), "dzień dobry", IndexCommand(4)])
		second = {"voice": synth.voice, "language": synth.language}
		synth.voice = "en"
		mixed, mixedAudio = spokenAudio(
			["Hello,", IndexCommand(5), LangChangeCommand("pl"), "dzień dobry,", IndexCommand(6),
			LangChangeCommand(None), "and goodbye", IndexCommand(7)]
		)
		result = {
			"first": first,
			"second": second,
			"voices": len(synth.availableVoices),
			"english": english,
			"polish": polish,
			"mixed": mixed,
			"seconds": [len(a) / sampleRate for a in (englishAudio, polishAudio, mixedAudio)],
			"peaks": [peak(a) for a in (englishAudio, polishAudio, mixedAudio)],
		}
		""",
	)
	value = done.value
	assert value["first"] == {"voice": "en", "language": "en-gb"}
	assert value["second"] == {"voice": "pl", "language": "pl"}
	assert value["voices"] > 100
	assert value["english"] == [["index", 1], ["index", 2], ["done", None]]
	assert value["polish"] == [["index", 3], ["index", 4], ["done", None]]
	assert value["mixed"] == [["index", 5], ["index", 6], ["index", 7], ["done", None]]
	# Speech, not silence: a voice peaks far above the noise floor and lasts as long as the words.
	assert all(seconds > 0.8 for seconds in value["seconds"]), value
	assert all(p > 5000 for p in value["peaks"]), value


def test_settingsChangeTheSpeech(tmp_path: Path):
	done = runEspeak(
		tmp_path,
		"""
		TEXT = ["one two three four five six seven eight nine ten", IndexCommand(1)]

		def seconds(audio):
			return len(audio) / sampleRate

		_events, normal = spokenAudio(TEXT)
		synth.rate = 70
		_events, fast = spokenAudio(TEXT)
		synth.rate = 100
		_events, fastest = spokenAudio(TEXT)
		synth.rateBoost = True
		_events, boosted = spokenAudio(TEXT)
		synth.rateBoost = False
		synth.rate = 30
		synth.pitch = 20
		_events, low = spokenAudio(TEXT)
		synth.pitch = 80
		_events, high = spokenAudio(TEXT)
		synth.pitch = 40
		synth.variant = "klatt"
		_events, klatt = spokenAudio(TEXT)
		variant = synth.variant
		synth.variant = "max"
		_events, plain = spokenAudio(TEXT)
		result = {
			"seconds": {"normal": seconds(normal), "fast": seconds(fast), "fastest": seconds(fastest),
				"boosted": seconds(boosted)},
			"pitch": {"low": pitch(low), "high": pitch(high)},
			"variant": variant,
			"rms": {"klatt": rms(klatt), "max": rms(plain)},
		}
		""",
	)
	value = done.value
	seconds = value["seconds"]
	assert seconds["fast"] < seconds["normal"] * 0.7, value
	# Rate boost runs the audio through sonic, which goes well past eSpeak's own fastest rate.
	assert seconds["boosted"] < seconds["fastest"] * 0.7, value
	assert 0 < value["pitch"]["low"] < value["pitch"]["high"] * 0.8, value
	assert value["variant"] == "klatt"
	klatt, plain = value["rms"]["klatt"], value["rms"]["max"]
	assert abs(klatt - plain) > 0.2 * max(klatt, plain), value


def test_aMarkAfterAFullStopIsReportedWhereItStands(tmp_path: Path):
	# eSpeak NG ends a clause at a full stop followed by a tag only once it sees what comes after the
	# tag, and before anything but a lower-case letter it cut the clause at the full stop, dropping
	# what the tag put there. The build carries a patch that keeps it, so say-all hears each sentence's
	# mark as that sentence ends.
	done = runEspeak(
		tmp_path,
		"""
		start = now()
		events = speakAndWait([
			"Hello world.", IndexCommand(1), "This is the second sentence.", IndexCommand(2),
			"Mr.", IndexCommand(3), "Smith counted to 42.", IndexCommand(4), "(Then he stopped.)", IndexCommand(5),
		])
		result = heard.timedSince(start)
		""",
	)
	events = done.value
	assert [index for _kind, index, _at in events] == [1, 2, 3, 4, 5, None], events
	# Each mark comes as its own sentence ends, not with the next one's.
	times = [at for _kind, _index, at in events]
	assert all(later - earlier > 0.2 for earlier, later in itertools.pairwise(times[:5])), events


def test_theCharacterLocaleIsKept(tmp_path: Path):
	# espeak_Initialize sets LC_CTYPE for the whole process; in NVDA's process on Windows it touches
	# only the C runtime linked into espeak.dll.
	done = runEspeak(
		tmp_path,
		"""
		result = {"before": characterLocaleBefore, "after": characterLocaleAfter, "language": synth.language}
		""",
		language="pl",
	)
	assert done.value["before"].startswith("pl_PL"), done.value
	assert done.value["after"] == done.value["before"]
	assert done.value["language"] == "pl"


def test_noCancelledAudioPlaysAfterTheCancel(tmp_path: Path):
	# The callback checks for a cancel and then feeds the player. A cancel between the two used to
	# leave the feed to restart the stopped player with up to 300 ms of the cancelled speech, and with
	# a mark's callback that then reported speech NVDA had silenced. Holding the driver's thread just
	# before the feed makes the window as wide as the test needs, in every round.
	done = runEspeak(
		tmp_path,
		"""
		gate = {"armed": False, "inWindow": threading.Event(), "proceed": threading.Event()}
		_feed = wasapi.wasPlay_feed


		def feed(*args):
			if gate["armed"] and threading.current_thread() is _espeak.bgThread:
				gate["armed"] = False
				gate["inWindow"].set()
				gate["proceed"].wait(1.0)
			return _feed(*args)


		wasapi.wasPlay_feed = feed
		random.seed(7)
		rounds = []
		for number in range(100):
			if number % 2:
				# Marks between the clauses, so that the feed held is sometimes one with a mark's callback.
				clauses = LONG.split(" and ")
				sequence = [part for i, clause in enumerate(clauses) for part in (clause, IndexCommand(number * 10 + i))]
			else:
				sequence = [LONG, IndexCommand(number * 10)]
			cancelledMarks = {command.index for command in sequence if isinstance(command, IndexCommand)}
			start = now()
			synth.speak(sequence)
			assert waitFor(lambda: any(t > start for t, _data in writes)), "the long sentence never played"
			time.sleep(random.uniform(0, 0.4))
			gate["inWindow"].clear()
			gate["proceed"].clear()
			gate["armed"] = True
			assert gate["inWindow"].wait(2.0), "the driver never fed the player"
			synth.cancel()
			mark = now()
			gate["proceed"].set()
			pump(0.35)
			# Stopped again, so that whatever played since is written down too.
			synth.cancel()
			rounds.append({
				"heardFrames": len(heardSince(mark)),
				"lateMarks": [index for kind, index in heard.since(mark) if index in cancelledMarks],
				"lateDone": ("done", None) in heard.since(mark),
			})
			pump(0.05)
		wasapi.wasPlay_feed = _feed
		after = speakAndWait(["OK button", IndexCommand(9999)])
		result = {"rounds": rounds, "after": after}
		""",
	)
	rounds = done.value["rounds"]
	oneMillisecond = 22050 // 1000
	assert all(r["heardFrames"] <= oneMillisecond for r in rounds), [r["heardFrames"] for r in rounds]
	assert not any(r["lateMarks"] or r["lateDone"] for r in rounds), rounds
	assert done.value["after"] == [["index", 9999], ["done", None]]


def test_noDoneAfterACancelInTheLastWait(tmp_path: Path):
	# At the end of synthesis the driver waits for the player to finish the utterance and then reports
	# it done. A cancel during that wait releases it, and used to be followed by that report all the
	# same, for speech NVDA had cancelled.
	done = runEspeak(
		tmp_path,
		"""
		gate = {"armed": False, "inWindow": threading.Event(), "proceed": threading.Event()}
		_idle = _espeak.player.idle


		def idle():
			if gate["armed"]:
				gate["armed"] = False
				gate["inWindow"].set()
				gate["proceed"].wait(1.0)
			return _idle()


		_espeak.player.idle = idle
		doneAfterCancel = 0
		for number in range(20):
			gate["inWindow"].clear()
			gate["proceed"].clear()
			gate["armed"] = True
			synth.speak(["Documents folder, 3 of 12", IndexCommand(number)])
			assert gate["inWindow"].wait(10.0), "the utterance never ended"
			synth.cancel()
			cancelled = now()
			gate["proceed"].set()
			pump(0.3)
			doneAfterCancel += ("done", None) in heard.since(cancelled)
		after = speakAndWait(["OK button", IndexCommand(99)])
		result = {"doneAfterCancel": doneAfterCancel, "after": after}
		""",
	)
	assert done.value == {"doneAfterCancel": 0, "after": [["index", 99], ["done", None]]}


def runUnitTestRunner(tmpPath: Path, test: str, env: dict[str, str]) -> subprocess.CompletedProcess:
	"""The unit-test runner, on a package of one probe test module."""
	package = tmpPath / "top" / "probeEspeak"
	package.mkdir(parents=True)
	(package / "__init__.py").write_text("")
	(package / "test_probe.py").write_text(textwrap.dedent(test), encoding="utf-8")
	exclusions = tmpPath / "exclusions.txt"
	exclusions.write_text("")
	tmpDir = tmpPath / "tmp"
	tmpDir.mkdir()
	return subprocess.run(
		[
			sys.executable,
			rununittests.__file__,
			"--top",
			str(tmpPath / "top"),
			"--package",
			"probeEspeak",
			"--exclusions",
			str(exclusions),
		],
		capture_output=True,
		text=True,
		timeout=300,
		env={**os.environ, "TMPDIR": str(tmpDir), **env},
		check=False,
	)


def test_unitTestRunnerAllowsThisEspeakAndNoOther(tmp_path: Path):
	# Paths that do not exist, so that nothing would load even if the runner let them through.
	done = runUnitTestRunner(
		tmp_path,
		"""
		import ctypes
		import os
		import unittest

		class TestLoads(unittest.TestCase):
			def test_theNamedEspeak(self):
				ctypes.CDLL(os.environ["BASEDREADER_ESPEAK_LIB"])

			def test_otherLibraries(self):
				for path in ("/nonexistent/libespeak-ng.so.1", "/nonexistent/libspa-pipewire-probe.so"):
					with self.assertRaises(OSError):
						ctypes.CDLL(path)
		""",
		{},
	)
	output = done.stdout + done.stderr
	assert done.returncode == 1, output
	refusals = sorted(line for line in output.splitlines() if line.startswith("rununittests: refused "))
	assert len(refusals) == 2, output
	assert refusals[0].startswith(
		"rununittests: refused load of the audio library /nonexistent/libespeak-ng.so.1"
	)
	assert refusals[1].startswith(
		"rununittests: refused load of the audio library /nonexistent/libspa-pipewire-probe.so"
	)
	assert "rununittests: 2 tests ran: 2 passed" in output, output


def test_unitTestRunnerRefusesToStartWithoutEspeak(tmp_path: Path):
	done = runUnitTestRunner(
		tmp_path,
		"""
		import unittest

		class TestNothing(unittest.TestCase):
			def test_passes(self):
				pass
		""",
		{LIBRARY_VARIABLE: ""},
	)
	assert done.returncode == 2, done.stdout + done.stderr
	assert f"rununittests: {LIBRARY_VARIABLE} is unset or names no file" in done.stderr
	assert "tests ran" not in done.stdout
