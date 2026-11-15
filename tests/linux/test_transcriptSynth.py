"""The transcript synth, the test hooks that load it, and synthesizer selection on Linux.

The driver tests talk to the driver directly. The speech tests go through speech.speak and the
real speech manager, with the queue pumped by hand, as nothing runs NVDA's core loop here. Run under
the rig:

	nix develop . -c tests/linux/rig python3 -m pytest tests/linux/test_transcriptSynth.py
"""

import importlib.util
import textwrap
from pathlib import Path

import harness

HERE = Path(__file__).resolve().parent
WINDOWS_ONLY_DRIVERS = {"mssp", "oneCore", "sapi4_32", "sapi5", "sapi5_32"}

_spec = importlib.util.spec_from_file_location(
	"_transcriptRecords",
	HERE / "synthDrivers" / "_transcriptRecords.py",
)
transcriptRecords = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(transcriptRecords)

#: Brings speech up as core.main does, with the transcript synth configured in place of auto.
SPEECH = """
import core
# core.main's first import after config; entering NVDA's import cycle anywhere else fails.
import NVDAHelper
from linuxPlatform import testHooks
testHooks.install()
import characterProcessing
import config
import queueHandler
import speech
import speechDictHandler
import synthDriverHandler
import watchdog
from speech.commands import *
from speech.priorities import Spri

# STUB, owned by workstream A3 (watchdog): queueHandler calls watchdog.alive() before each queued
# function, and the Linux watchdog skeleton raises. Remove when A3's watchdog lands.
watchdog.alive = lambda: None

speechDictHandler.initialize()
characterProcessing.initialize()
config.conf["speech"]["synth"] = "transcript"
speech.initialize()
synth = synthDriverHandler.getSynth()


def pump():
	while not queueHandler.eventQueue.empty():
		queueHandler.pumpAll()


def spoken():
	from synthDrivers._transcriptRecords import spokenTexts

	return spokenTexts(synth.events)


def kinds():
	return [e["kind"] for e in synth.events]


class Heard:
	# extensionPoints holds handlers weakly, so these are bound methods of an object kept alive here.
	def __init__(self):
		import threading

		self.threading = threading
		self.notifications = []
		synthDriverHandler.synthIndexReached.register(self.onIndex)
		synthDriverHandler.synthDoneSpeaking.register(self.onDone)

	def onIndex(self, synth, index):
		self.notifications.append(("index", index, self.threading.current_thread().name))

	def onDone(self, synth):
		self.notifications.append(("done", None, self.threading.current_thread().name))

	def plain(self):
		return [(kind, index) for kind, index, _thread in self.notifications]
"""

END = """
synthDriverHandler.setSynth(None)
"""


def runSpeech(tmpPath: Path, code: str, mode: str = "immediate", **env: str) -> harness.ChildResult:
	return harness.runChild(
		tmpPath,
		SPEECH + textwrap.dedent(code) + END,
		env={"BASEDREADER_TEST_HOOKS_DIR": str(HERE), "TRANSCRIPT_SYNTH_MODE": mode, **env},
	)


def test_testHooksDoNothingWhenUnset(tmp_path: Path):
	done = harness.runChild(
		tmp_path,
		"""
		import os
		os.environ.pop("BASEDREADER_TEST_HOOKS_DIR", None)
		import globalPlugins
		import synthDrivers
		before = (list(synthDrivers.__path__), list(globalPlugins.__path__))
		from linuxPlatform import testHooks
		added = testHooks.install()
		result = {"added": added, "unchanged": before == (synthDrivers.__path__, globalPlugins.__path__)}
		""",
		prepare="bootstrap",
	)
	done.assertClean()
	assert done.value == {"added": [], "unchanged": True}


def test_testHooksAddBothDirectoriesOnce(tmp_path: Path):
	hooks = tmp_path / "hooks"
	(hooks / "synthDrivers").mkdir(parents=True)
	(hooks / "globalPlugins").mkdir()
	done = harness.runChild(
		tmp_path,
		"""
		import globalPlugins
		import synthDrivers
		from linuxPlatform import testHooks
		first = testHooks.install()
		second = testHooks.install()
		result = {
			"first": first,
			"second": second,
			"synthDrivers": list(synthDrivers.__path__),
			"globalPlugins": list(globalPlugins.__path__),
		}
		""",
		prepare="bootstrap",
		env={"BASEDREADER_TEST_HOOKS_DIR": str(hooks)},
	)
	done.assertClean()
	synthDir, pluginDir = str(hooks / "synthDrivers"), str(hooks / "globalPlugins")
	assert done.value["first"] == [synthDir, pluginDir]
	assert done.value["second"] == []
	# Last, so that a test driver never takes the place of a built-in or an add-on's.
	assert done.value["synthDrivers"][-1] == synthDir
	assert done.value["synthDrivers"].count(synthDir) == 1
	assert done.value["globalPlugins"][-1] == pluginDir
	assert done.value["globalPlugins"].count(pluginDir) == 1


def test_testHooksSkipAMissingSubdirectory(tmp_path: Path):
	done = harness.runChild(
		tmp_path,
		"""
		from linuxPlatform import testHooks
		result = testHooks.install()
		""",
		prepare="bootstrap",
		env={"BASEDREADER_TEST_HOOKS_DIR": str(HERE)},
	)
	done.assertClean()
	assert done.value == [str(HERE / "synthDrivers")]


def test_testHooksRefuseAMissingDirectory(tmp_path: Path):
	done = harness.runChild(
		tmp_path,
		"""
		from linuxPlatform import testHooks
		testHooks.install()
		""",
		prepare="bootstrap",
		env={"BASEDREADER_TEST_HOOKS_DIR": str(tmp_path / "absent")},
	)
	assert done.error is not None and "FileNotFoundError" in done.error


def test_setSynthLoadsTranscriptByName(tmp_path: Path):
	done = runSpeech(
		tmp_path,
		"""
		loaded = synthDriverHandler.setSynth("transcript")
		synth = synthDriverHandler.getSynth()
		synth.rate = 70
		result = {
			"loaded": loaded,
			"name": synth.name,
			"configured": config.conf["speech"]["synth"],
			"commands": sorted(c.__name__ for c in synth.supportedCommands),
			"notifications": len(synth.supportedNotifications),
			"settings": [s.id for s in synth.supportedSettings],
			"values": [synth.voice, synth.rate, synth.pitch, synth.volume],
			"listed": "transcript" in dict(synthDriverHandler.getSynthList()),
		}
		""",
	)
	done.assertClean()
	assert done.value == {
		"loaded": True,
		"name": "transcript",
		"configured": "transcript",
		"commands": [
			"BreakCommand",
			"CharacterModeCommand",
			"IndexCommand",
			"LangChangeCommand",
			"PhonemeCommand",
			"PitchCommand",
			"RateCommand",
			"VolumeCommand",
		],
		"notifications": 2,
		"settings": ["voice", "rate", "pitch", "volume"],
		"values": ["transcript", 70, 50, 50],
		"listed": True,
	}


def test_eachCommandIsRecorded(tmp_path: Path):
	transcript = tmp_path / "transcript.jsonl"
	done = runSpeech(
		tmp_path,
		"""
		conf = config.conf["speech"]["transcript"]
		conf["rate"], conf["pitch"], conf["volume"] = 40, 60, 80


		class Unknown(SynthCommand):
			def __repr__(self):
				return "Unknown()"


		heard = Heard()
		synth.speak([
			LangChangeCommand("pl"),
			"Dzień dobry",
			IndexCommand(1),
			CharacterModeCommand(True),
			"a",
			CharacterModeCommand(False),
			BreakCommand(250),
			RateCommand(offset=10),
			PitchCommand(multiplier=0.5),
			VolumeCommand(),
			PhonemeCommand("ˈtoʊmɑːtoʊ", text="tomato"),
			LangChangeCommand(None),
			Unknown(),
			IndexCommand(2),
		])
		result = {"events": synth.events, "heard": heard.plain()}
		""",
		TRANSCRIPT_SYNTH_FILE=str(transcript),
	)
	done.assertClean()
	expected = [
		{"lang": "pl"},
		"Dzień dobry",
		{"index": 1},
		{"charMode": True},
		"a",
		{"charMode": False},
		{"break": 250},
		{"rate": 50},
		{"pitch": 30},
		{"volume": 80},
		{"ipa": "ˈtoʊmɑːtoʊ", "text": "tomato"},
		{"lang": None},
		{"unknown": "Unknown()"},
		{"index": 2},
	]
	# The result is read after the child's last line, setSynth(None), which cancels before terminating.
	events = done.value["events"]
	assert [(e["kind"], e["data"]) for e in events] == [("speak", expected), ("cancel", None)]
	# Setting the synth up spoke nothing; the file holds exactly what the driver kept in memory.
	assert transcriptRecords.readRecords(transcript) == events
	assert transcriptRecords.spokenTexts(events) == ["Dzień dobry a"]
	assert done.value["heard"] == [["index", 1], ["index", 2], ["done", None]]


def test_manualModeReleasesOnlyOnAdvance(tmp_path: Path):
	done = runSpeech(
		tmp_path,
		"""
		heard = Heard()
		synth.speak(["one", IndexCommand(1), "two", IndexCommand(2)])
		steps = {"afterSpeak": (heard.plain(), synth.pendingNotifications)}
		steps["advanced"] = synth.advance()
		steps["afterOne"] = heard.plain()
		steps["advancedRest"] = synth.advance(5)
		steps["afterAll"] = heard.plain()
		steps["advancedNothing"] = synth.advance()
		synth.speak(["three", IndexCommand(3)])
		steps["pendingBeforeCancel"] = synth.pendingNotifications
		synth.cancel()
		steps["pendingAfterCancel"] = synth.pendingNotifications
		steps["advancedAfterCancel"] = synth.advance()
		steps["final"] = heard.plain()
		steps["kinds"] = kinds()
		result = steps
		""",
		mode="manual",
	)
	done.assertClean()
	steps = done.value
	assert steps["afterSpeak"] == [[], [1, 2, None]]
	assert steps["advanced"] == 1
	assert steps["afterOne"] == [["index", 1]]
	assert steps["advancedRest"] == 2
	assert steps["afterAll"] == [["index", 1], ["index", 2], ["done", None]]
	assert steps["advancedNothing"] == 0
	assert steps["pendingBeforeCancel"] == [3, None]
	assert steps["pendingAfterCancel"] == []
	assert steps["advancedAfterCancel"] == 0
	assert steps["final"] == steps["afterAll"]
	assert steps["kinds"] == ["speak", "speak", "cancel"]


def test_timedModeReportsFromItsOwnThreadAndHonoursPause(tmp_path: Path):
	done = runSpeech(
		tmp_path,
		"""
		import threading
		import time

		heard = Heard()
		finished = threading.Event()
		synthDriverHandler.synthDoneSpeaking.register(finished.set)
		started = time.monotonic()
		synth.speak(["x" * 20, IndexCommand(1), "y" * 20, IndexCommand(2)])
		finished.wait(10)
		elapsed = time.monotonic() - started
		firstRun = list(heard.notifications)

		finished.clear()
		synth.speak(["z" * 10, IndexCommand(3)])
		synth.pause(True)
		time.sleep(0.5)
		duringPause = len(heard.notifications) - len(firstRun)
		synth.pause(False)
		finished.wait(10)
		afterPause = heard.plain()[len(firstRun):]

		synth.speak(["w" * 100, IndexCommand(4)])
		synth.cancel()
		time.sleep(0.5)
		result = {
			"elapsed": elapsed,
			"firstRun": firstRun,
			"duringPause": duringPause,
			"afterPause": afterPause,
			"afterCancel": heard.plain()[len(firstRun) + len(afterPause):],
			"kinds": kinds(),
		}
		""",
		mode="timed",
		TRANSCRIPT_SYNTH_MS_PER_CHAR="10",
	)
	done.assertClean()
	result = done.value
	assert [[kind, index] for kind, index, _thread in result["firstRun"]] == [
		["index", 1],
		["index", 2],
		["done", None],
	]
	assert {thread for _kind, _index, thread in result["firstRun"]} == {"transcriptSynth"}
	# 40 characters at 10 ms each.
	assert result["elapsed"] >= 0.4
	assert result["duringPause"] == 0
	assert result["afterPause"] == [["index", 3], ["done", None]]
	assert result["afterCancel"] == []
	assert result["kinds"] == ["speak", "speak", "pause", "pause", "speak", "cancel"]


def test_speechReachesTheDriver(tmp_path: Path):
	transcript = tmp_path / "transcript.jsonl"
	done = runSpeech(
		tmp_path,
		"""
		called = []
		speech.speakMessage("Hello world.")
		speech.speak(["first", CallbackCommand(lambda: called.append("callback")), "second"])
		pump()
		result = {
			"events": synth.events,
			"called": called,
			"speaking": speech.isSpeaking(),
			"lastIndex": synth.events[-1]["data"][-1],
		}
		""",
		TRANSCRIPT_SYNTH_FILE=str(transcript),
	)
	done.assertClean()
	speaks = [e["data"] for e in done.value["events"]]
	assert speaks[0] == [{"lang": "en"}, "Hello world.  ", {"index": 1}]
	assert speaks[1] == [{"lang": "en"}, "first  ", {"index": 2}, "second  ", {"index": 3}]
	assert done.value["called"] == ["callback"]
	assert done.value["speaking"] is False
	records = transcriptRecords.readRecords(transcript)
	assert transcriptRecords.spokenTexts(records) == ["Hello world.", "first second"]


def test_speechQueuesBehindTheUtteranceBeingSpoken(tmp_path: Path):
	done = runSpeech(
		tmp_path,
		"""
		steps = {}
		for text in ("one", "two", "three"):
			speech.speakMessage(text)
		steps["sent"] = spoken()
		synth.advance(2)
		steps["afterFirstEnd"] = (pump(), spoken())[1]
		synth.advance(2)
		steps["afterSecondEnd"] = (pump(), spoken())[1]
		synth.advance(2)
		pump()
		steps["speaking"] = speech.isSpeaking()
		result = steps
		""",
		mode="manual",
	)
	done.assertClean()
	assert done.value["sent"] == ["one"]
	assert done.value["afterFirstEnd"] == ["one", "two"]
	assert done.value["afterSecondEnd"] == ["one", "two", "three"]
	assert done.value["speaking"] is False


def test_nowInterruptsAndTheInterruptedUtteranceResumes(tmp_path: Path):
	done = runSpeech(
		tmp_path,
		"""
		speech.speakMessage("normal one")
		speech.speakMessage("normal two")
		speech.speakMessage("urgent", priority=Spri.NOW)
		steps = {"kinds": kinds(), "sent": spoken()}
		synth.advance(2)
		pump()
		steps["afterUrgent"] = spoken()
		synth.advance(2)
		pump()
		steps["afterResume"] = spoken()
		result = steps
		""",
		mode="manual",
	)
	done.assertClean()
	assert done.value["kinds"] == ["speak", "cancel", "speak"]
	assert done.value["sent"] == ["normal one", "urgent"]
	assert done.value["afterUrgent"] == ["normal one", "urgent", "normal one"]
	assert done.value["afterResume"] == ["normal one", "urgent", "normal one", "normal two"]


def test_nextGoesBeforeQueuedNormalSpeechWithoutInterrupting(tmp_path: Path):
	done = runSpeech(
		tmp_path,
		"""
		speech.speakMessage("normal one")
		speech.speakMessage("normal two")
		speech.speakMessage("next", priority=Spri.NEXT)
		steps = {"kinds": kinds()}
		for _ in range(3):
			synth.advance(2)
			pump()
		steps["sent"] = spoken()
		steps["speaking"] = speech.isSpeaking()
		result = steps
		""",
		mode="manual",
	)
	done.assertClean()
	assert done.value["kinds"] == ["speak"]
	assert done.value["sent"] == ["normal one", "next", "normal two"]
	assert done.value["speaking"] is False


def test_cancelSpeechDiscardsQueuedSpeech(tmp_path: Path):
	done = runSpeech(
		tmp_path,
		"""
		speech.speakMessage("one")
		speech.speakMessage("two")
		speech.cancelSpeech()
		steps = {"kinds": kinds(), "pending": synth.pendingNotifications, "speaking": speech.isSpeaking()}
		speech.speakMessage("three")
		steps["sent"] = spoken()
		result = steps
		""",
		mode="manual",
	)
	done.assertClean()
	assert done.value["kinds"] == ["speak", "cancel"]
	assert done.value["pending"] == []
	assert done.value["speaking"] is False
	assert done.value["sent"] == ["one", "three"]


def test_speakingWhilePausedCancelsFirst(tmp_path: Path):
	done = runSpeech(
		tmp_path,
		"""
		speech.speakMessage("one")
		speech.pauseSpeech(True)
		speech.speakMessage("two")
		result = {"events": [(e["kind"], e["data"]) for e in synth.events], "sent": spoken()}
		""",
		mode="manual",
	)
	done.assertClean()
	assert [kind for kind, _data in done.value["events"]] == ["speak", "pause", "cancel", "speak"]
	assert done.value["events"][1] == ["pause", True]
	assert done.value["sent"] == ["one", "two"]


def test_synthListOnLinuxHasNoWindowsDriver(tmp_path: Path):
	done = runSpeech(
		tmp_path,
		"""
		result = {
			"names": [name for name, _description in synthDriverHandler.getSynthList()],
			"priority": synthDriverHandler.defaultSynthPriorityList,
		}
		""",
	)
	done.assertClean()
	names = done.value["names"]
	assert not WINDOWS_ONLY_DRIVERS & set(names)
	assert "transcript" in names
	assert names[-1] == "silence"
	assert done.value["priority"] == ["silence"]


def test_autoResolvesToSilence(tmp_path: Path):
	done = harness.runChild(
		tmp_path,
		"""
		import core
		import NVDAHelper
		import characterProcessing
		import config
		import speech
		import speechDictHandler
		import synthDriverHandler

		speechDictHandler.initialize()
		characterProcessing.initialize()
		configuredBefore = config.conf["speech"]["synth"]
		speech.initialize()
		result = {
			"configuredBefore": configuredBefore,
			"synth": synthDriverHandler.getSynth().name,
			"configuredAfter": config.conf["speech"]["synth"],
		}
		synthDriverHandler.setSynth(None)
		""",
	)
	done.assertClean()
	assert done.value == {"configuredBefore": "auto", "synth": "silence", "configuredAfter": "silence"}


def test_excludedDriversAreExactlyThoseThatCannotImport(tmp_path: Path):
	# The list in synthDriverHandler is written out by hand; this keeps it true as upstream adds,
	# removes or ports drivers.
	done = harness.runChild(
		tmp_path,
		"""
		import core
		import NVDAHelper
		import importlib
		import pkgutil
		import synthDriverHandler
		import synthDrivers

		failing = []
		for _loader, name, _isPkg in pkgutil.iter_modules(synthDrivers.__path__):
			if name.startswith("_"):
				continue
			try:
				importlib.import_module(f"synthDrivers.{name}")
			except Exception:
				failing.append(name)
		result = {"failing": sorted(failing), "excluded": sorted(synthDriverHandler._windowsOnlySynthDrivers)}
		""",
	)
	assert done.error is None, done.error
	assert done.value["failing"] == sorted(WINDOWS_ONLY_DRIVERS)
	assert done.value["excluded"] == sorted(WINDOWS_ONLY_DRIVERS)
