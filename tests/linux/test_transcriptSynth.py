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
from speech.commands import *
from speech.priorities import Spri

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
		env={"BASEDREADER_TEST_HOOKS_DIR": str(HERE), "BASEDREADER_TRANSCRIPT_MODE": mode, **env},
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


def test_testHooksDoNothingInSecureMode(tmp_path: Path):
	done = harness.runChild(
		tmp_path,
		"""
		import globalPlugins
		import globalVars
		import synthDrivers
		globalVars.appArgs.secure = True
		before = (list(synthDrivers.__path__), list(globalPlugins.__path__))
		from linuxPlatform import testHooks
		added = testHooks.install()
		result = {"added": added, "unchanged": before == (synthDrivers.__path__, globalPlugins.__path__)}
		""",
		prepare="bootstrap",
		env={"BASEDREADER_TEST_HOOKS_DIR": str(HERE)},
	)
	done.assertClean()
	assert done.value == {"added": [], "unchanged": True}


def test_testHooksSurviveReloadingPlugins(tmp_path: Path):
	plugins = tmp_path / "hooks" / "globalPlugins"
	plugins.mkdir(parents=True)
	(plugins / "hookedPlugin.py").write_text(
		"import globalPluginHandler\n\n\nclass GlobalPlugin(globalPluginHandler.GlobalPlugin):\n\tpass\n",
		encoding="utf-8",
	)
	done = harness.runChild(
		tmp_path,
		"""
		import core
		import NVDAHelper
		import globalVars
		# As tests launch NVDA; it also keeps add-ons and the scratchpad out of the reload.
		globalVars.appArgs.disableAddons = True
		from linuxPlatform import testHooks
		testHooks.install()
		import globalPluginHandler


		def running():
			return sorted(type(plugin).__module__ for plugin in globalPluginHandler.runningPlugins)


		globalPluginHandler.initialize()
		before = running()
		# Removes globalPlugins from sys.modules and imports it again, with a fresh search path.
		globalPluginHandler.reloadGlobalPlugins()
		result = {"before": before, "after": running()}
		globalPluginHandler.terminate()
		""",
		env={"BASEDREADER_TEST_HOOKS_DIR": str(tmp_path / "hooks")},
	)
	done.assertClean()
	assert done.value == {"before": ["globalPlugins.hookedPlugin"], "after": ["globalPlugins.hookedPlugin"]}


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
		BASEDREADER_TRANSCRIPT_FILE=str(transcript),
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


def test_manualModeHoldsNotificationsWhilePaused(tmp_path: Path):
	done = runSpeech(
		tmp_path,
		"""
		heard = Heard()
		synth.speak(["one", IndexCommand(1)])
		synth.pause(True)
		steps = {"advancedWhilePaused": synth.advance(), "heardWhilePaused": heard.plain()}
		steps["pendingWhilePaused"] = synth.pendingNotifications
		synth.pause(False)
		steps["advancedAfterResume"] = synth.advance(2)
		steps["heard"] = heard.plain()
		result = steps
		""",
		mode="manual",
	)
	done.assertClean()
	assert done.value == {
		"advancedWhilePaused": 0,
		"heardWhilePaused": [],
		"pendingWhilePaused": [1, None],
		"advancedAfterResume": 2,
		"heard": [["index", 1], ["done", None]],
	}


def test_timedModeReportsFromItsOwnThreadAndHonoursPause(tmp_path: Path):
	# At 10 ms a character, every wait that must see nothing outlasts the notification's due time by
	# half a second or more, and every step the main thread must take before a notification falls due
	# has a second, so neither kind of check depends on how loaded the machine is.
	done = runSpeech(
		tmp_path,
		"""
		import threading
		import time

		heard = Heard()
		finished = threading.Event()
		synthDriverHandler.synthDoneSpeaking.register(finished.set)


		def since(mark):
			return heard.plain()[mark:]


		steps = {}
		started = time.monotonic()
		synth.speak(["x" * 20, IndexCommand(1), "y" * 20, IndexCommand(2)])
		finished.wait(10)
		steps["elapsed"] = time.monotonic() - started
		steps["threads"] = sorted({thread for _kind, _index, thread in heard.notifications})
		steps["first"] = since(0)

		mark = len(heard.notifications)
		finished.clear()
		synth.pause(True)
		synth.speak(["z" * 10, IndexCommand(3)])
		time.sleep(0.6)
		steps["pausedBeforeStart"] = since(mark)
		synth.pause(False)
		finished.wait(10)
		steps["resumed"] = since(mark)

		# Due after 1 s; the pause holds it past that.
		mark = len(heard.notifications)
		finished.clear()
		started = time.monotonic()
		synth.speak(["v" * 100, IndexCommand(4)])
		synth.pause(True)
		time.sleep(1.5)
		steps["pausedMidway"] = since(mark)
		synth.pause(False)
		finished.wait(10)
		steps["resumedMidway"] = since(mark)
		steps["midwayElapsed"] = time.monotonic() - started

		# Cancelled once its first index is out; the second would be due 1 s after that.
		mark = len(heard.notifications)
		deadline = time.monotonic() + 10
		synth.speak(["w" * 10, IndexCommand(5), "w" * 100, IndexCommand(6)])
		while len(heard.notifications) == mark and time.monotonic() < deadline:
			time.sleep(0.01)
		synth.cancel()
		time.sleep(1.5)
		steps["cancelled"] = since(mark)

		# speech.speak cancels a paused synth and never unpauses it, so a cancel has to end the pause.
		mark = len(heard.notifications)
		finished.clear()
		synth.pause(True)
		synth.cancel()
		synth.speak(["u" * 10, IndexCommand(7)])
		finished.wait(10)
		steps["afterPauseAndCancel"] = since(mark)
		steps["kinds"] = kinds()
		result = steps
		""",
		mode="timed",
		BASEDREADER_TRANSCRIPT_MS_PER_CHAR="10",
	)
	done.assertClean()
	steps = done.value
	assert steps["first"] == [["index", 1], ["index", 2], ["done", None]]
	assert steps["threads"] == ["transcriptSynth"]
	# 40 characters at 10 ms each.
	assert steps["elapsed"] >= 0.4
	assert steps["pausedBeforeStart"] == []
	assert steps["resumed"] == [["index", 3], ["done", None]]
	assert steps["pausedMidway"] == []
	assert steps["resumedMidway"] == [["index", 4], ["done", None]]
	assert steps["midwayElapsed"] >= 1.5
	assert steps["cancelled"] == [["index", 5]]
	assert steps["afterPauseAndCancel"] == [["index", 7], ["done", None]]
	assert steps["kinds"] == [
		"speak",
		"pause",
		"speak",
		"pause",
		"speak",
		"pause",
		"pause",
		"speak",
		"cancel",
		"pause",
		"cancel",
		"speak",
	]


def test_speakingWhilePausedInTimedModeIsHeardToTheEnd(tmp_path: Path):
	done = runSpeech(
		tmp_path,
		"""
		import time

		speech.speakMessage("one")
		speech.pauseSpeech(True)
		speech.speakMessage("two")
		deadline = time.monotonic() + 10
		while speech.isSpeaking() and time.monotonic() < deadline:
			pump()
			time.sleep(0.01)
		result = {"speaking": speech.isSpeaking(), "sent": spoken(), "kinds": kinds()}
		""",
		mode="timed",
	)
	done.assertClean()
	assert done.value == {
		"speaking": False,
		"sent": ["one", "two"],
		"kinds": ["speak", "pause", "cancel", "speak"],
	}


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
		BASEDREADER_TRANSCRIPT_FILE=str(transcript),
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
		names = [name for name, _description in synthDriverHandler.getSynthList()]
		loaded = {}
		for name in names:
			loaded[name] = [synthDriverHandler.setSynth(name), synthDriverHandler.getSynth().name]
		result = {"names": names, "loaded": loaded, "priority": synthDriverHandler.defaultSynthPriorityList}
		""",
	)
	# Everything offered loads, with nothing logged at ERROR on the way.
	done.assertClean()
	assert done.value["names"] == ["transcript", "silence"]
	assert done.value["loaded"] == {"transcript": [True, "transcript"], "silence": [True, "silence"]}
	assert done.value["priority"] == ["espeak", "silence"]


def test_aDriverNamedLikeAWindowsOneIsListedWhenNotNVDAs(tmp_path: Path):
	addon = tmp_path / "addon" / "synthDrivers"
	addon.mkdir(parents=True)
	(addon / "sapi5.py").write_text(
		"from synthDrivers.silence import SynthDriver as Silence\n\n\n"
		"class SynthDriver(Silence):\n\tname = 'sapi5'\n\tdescription = 'Not Windows'\n",
		encoding="utf-8",
	)
	done = runSpeech(
		tmp_path,
		f"""
		import synthDrivers
		# Where Addon.addToPackagePath puts an add-on's drivers.
		synthDrivers.__path__.insert(0, {str(addon)!r})
		names = [name for name, _description in synthDriverHandler.getSynthList()]
		result = {{"names": names, "loaded": synthDriverHandler.setSynth("sapi5")}}
		""",
	)
	done.assertClean()
	assert done.value["names"] == ["sapi5", "transcript", "silence"]
	assert done.value["loaded"] is True


def test_autoFallsBackToSilenceAndStaysAuto(tmp_path: Path):
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
			# What config.conf.save() would write.
			"saved": config.conf.profiles[0].get("speech", {}).get("synth"),
		}
		synthDriverHandler.setSynth(None)
		""",
	)
	# Silence as a fallback is not saved, so eSpeak takes over by itself once it loads.
	assert done.value == {
		"configuredBefore": "auto",
		"synth": "silence",
		"configuredAfter": "auto",
		"saved": None,
	}
	# Until it does, the one error says why there is no voice. Remove it from here when eSpeak loads.
	assert done.error is None, done.error
	assert not done.noLinuxImplementation, done.noLinuxImplementation
	assert done.logErrors == ["ERROR: setSynth failed for espeak"]


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
