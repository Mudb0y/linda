"""The say mode, linda say, as a process of its own: one text spoken through NVDA's whole speech
path into the capture sink, one line saying how it went, an exit status, and nothing left behind.

Each run starts source/linda.py through a small runner that holds it to harness's checks, as
harness.startLauncher does, and that also records, as the process exits, the sockets it still holds.
Every run has a user configuration, a state directory, a runtime directory and a temporary
directory of its own, and whatever way it ends, it must leave the configuration exactly as it was,
no temporary directory behind, and the launcher's own logs untouched. Run under the rig:

	nix develop . -c tests/linux/rig python3 -m pytest tests/linux/test_say.py
"""

import math
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import wave
from array import array
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import harness
import pytest
from pipewireFixture import PipewireFixture, assertCleanThroughPipewire

if TYPE_CHECKING:
	from gi.repository import Gio

HERE = Path(__file__).resolve().parent
TEMPORARY_PREFIX = "linda-say-"
#: Seconds a run may take; one takes about two, and eSpeak with the sounds about six.
RUN_BOUND = 120.0
#: Seconds a run that refuses its arguments may take. It stops before importing NVDA.
REFUSAL_BOUND = 30.0
TRANSCRIPT_CONFIG = "[speech]\nsynth = transcript\n"
#: The rate and the level of the audio the transcript synth feeds in player mode.
TRANSCRIPT_RATE = 22050
TRANSCRIPT_LEVEL = 1000
#: What the launcher's own log directory holds before each run, which no run may touch.
LAUNCHER_LOGS = {"nvda.log": "the launcher's last log\n", "nvda-old.log": "the launcher's log before that\n"}
SPOKE = re.compile(
	r"linda say: spoke with (?P<synth>\S+), voice (?P<voice>[^,]+), "
	r"(?:(?P<null>into the null sink, which plays nothing)\. Speech"
	r"|(?P<output>into the capture sink|through PipeWire)\. "
	r"The first audio came (?P<firstAudio>\d+) milliseconds? after asking, and speech)"
	r" finished after (?P<finished>\d+(?:\.\d)?) seconds?\.\n",
)
INTERRUPTED = "linda say: interrupted before speech finished.\n"
OPENEVV_DRIVER_DIR_VARIABLE = "LINDA_OPENEVV_DRIVER_DIR"

#: Runs the launcher with the checks harness.startLauncher puts on it. The say mode leaves through
#: os._exit, past atexit, so this records what the process holds and finishes the checks there too.
RUNNER = """
import atexit
import importlib.util
import os
import runpy
import signal
import sys

harnessPath, eventsPath, launcher, *arguments = sys.argv[1:]
spec = importlib.util.spec_from_file_location("harness", harnessPath)
harness = importlib.util.module_from_spec(spec)
spec.loader.exec_module(harness)
harness.requireRig()
record = harness.EventFile(eventsPath)
checks = harness.Checks(record)
record(
	"start",
	launcher=launcher,
	arguments=arguments,
	isSigintIgnored=signal.getsignal(signal.SIGINT) == signal.SIG_IGN,
)
exitProcess = os._exit
finished = []


def finish(status):
	if finished:
		return
	finished.append(status)
	sockets = []
	for fd in os.listdir("/proc/self/fd"):
		try:
			target = os.readlink(f"/proc/self/fd/{fd}")
		except OSError:
			continue
		if target.startswith("socket:"):
			sockets.append(target)
	record("exit", status=status, sockets=sockets, modules=sorted(sys.modules))
	checks.finish()


def exitChecked(status):
	finish(status)
	exitProcess(status)


os._exit = exitChecked
atexit.register(finish, None)
sys.argv[:] = [launcher, *arguments]
runpy.run_path(launcher, run_name="__main__")
"""


def snapshot(top: Path) -> dict[str, tuple]:
	"""Every entry under top with its type, mode, modification time and, for a file, its bytes."""
	state = {}
	for path in sorted([top, *top.rglob("*")]) if top.exists() else []:
		info = path.lstat()
		content = path.read_bytes() if path.is_file() and not path.is_symlink() else None
		state[str(path.relative_to(top))] = (info.st_mode, info.st_mtime_ns, content)
	return state


@dataclass
class Capture:
	name: str
	channels: int
	rate: int
	width: int
	samples: array

	@property
	def seconds(self) -> float:
		return len(self.samples) / self.channels / self.rate


def captured(directory: Path) -> list[Capture]:
	"""The capture sink's files, in the order their audio began."""
	found = []
	for path in sorted(directory.glob("*.wav")):
		with wave.open(str(path), "rb") as f:
			assert f.getsampwidth() == 2, path
			samples = array("h", f.readframes(f.getnframes()))
			found.append(Capture(path.name, f.getnchannels(), f.getframerate(), f.getsampwidth(), samples))
	return found


def rms(samples: array) -> float:
	return math.sqrt(sum(s * s for s in samples) / len(samples)) if samples else 0.0


def peak(samples: array) -> int:
	return max(max(samples), -min(samples)) if samples else 0


class Home:
	"""What a user of the say mode has: a configuration, a state directory with the launcher's logs in
	it, a runtime directory and a temporary directory, all under one directory a test makes."""

	def __init__(self, directory: Path, config: str | None = TRANSCRIPT_CONFIG) -> None:
		self.directory = directory
		self.configDir = directory / "config" / "linda"
		self.stateDir = directory / "state" / "linda"
		self.sayDir = self.stateDir / "say"
		self.runtimeDir = directory / "run"
		self.temporaryDir = directory / "tmp"
		self.captureDir = directory / "capture"
		self.hooksDir = directory / "hooks"
		for path in (self.configDir, self.stateDir, self.temporaryDir, self.captureDir, self.hooksDir):
			path.mkdir(parents=True)
		self.runtimeDir.mkdir(mode=0o700)
		if config is not None:
			(self.configDir / "nvda.ini").write_text(config, encoding="utf-8")
		dictionaries = self.configDir / "speechDicts"
		dictionaries.mkdir()
		(dictionaries / "default.dic").write_text("Linda\tbased reader\t1\t0\n", encoding="utf-8")
		for name, text in LAUNCHER_LOGS.items():
			(self.stateDir / name).write_text(text, encoding="utf-8")
		(self.hooksDir / "synthDrivers").symlink_to(HERE / "synthDrivers")

	@property
	def transcriptPath(self) -> Path:
		return self.directory / "transcript.jsonl"

	def environment(self) -> dict[str, str]:
		return {
			"XDG_CONFIG_HOME": str(self.configDir.parent),
			"XDG_STATE_HOME": str(self.stateDir.parent),
			"XDG_RUNTIME_DIR": str(self.runtimeDir),
			"TMPDIR": str(self.temporaryDir),
			"LINDA_TEST_HOOKS_DIR": str(self.hooksDir),
			"LINDA_AUDIO_SINK": "capture",
			"LINDA_AUDIO_CAPTURE_DIR": str(self.captureDir),
			"LINDA_TRANSCRIPT_MODE": "player",
			"LINDA_TRANSCRIPT_FILE": str(self.transcriptPath),
		}

	def transcript(self) -> list[dict]:
		if not self.transcriptPath.exists():
			return []
		return harness.transcriptRecords().readRecords(self.transcriptPath)

	def captured(self) -> list[Capture]:
		return captured(self.captureDir)


class Say:
	"""One run of linda say."""

	def __init__(
		self,
		home: Home,
		launcherArguments: tuple[str, ...],
		env: dict[str, str | None] | None = None,
		prefix: tuple[str, ...] = (),
	) -> None:
		""":param launcherArguments: The launcher's arguments, the word say first for the say mode.
		:param prefix: A command that runs the rest, as env does.
		"""
		self.home = home
		self.directory = Path(tempfile.mkdtemp(prefix="say-", dir=home.directory))
		runner = self.directory / "runner.py"
		runner.write_text(RUNNER, encoding="utf-8")
		self.overlay = self.directory / "overlay"
		self.eventsPath = self.directory / "events.jsonl"
		self.configBefore = snapshot(home.configDir)
		self.launcherLogsBefore = {name: (home.stateDir / name).read_bytes() for name in LAUNCHER_LOGS}
		self.treeBefore = harness.treeState()
		command = harness.isolatedCommand(
			self.overlay,
			[
				*prefix,
				sys.executable,
				"-I",
				"-B",
				str(runner),
				str(HERE / "harness.py"),
				str(self.eventsPath),
				str(harness.LAUNCHER),
				*launcherArguments,
			],
		)
		environment = {**os.environ, **home.environment(), **(env or {})}
		self.started = time.monotonic()
		self.process = subprocess.Popen(
			command,
			stdout=subprocess.PIPE,
			stderr=subprocess.PIPE,
			text=True,
			env={name: value for name, value in environment.items() if value is not None},
			cwd=self.directory,
		)
		self.stdout = ""
		self.stderr = ""

	def wait(self, timeout: float = RUN_BOUND) -> int:
		"""Wait for the run to end, and check what every way of ending must leave as it was."""
		try:
			self.stdout, self.stderr = self.process.communicate(timeout=timeout)
		except subprocess.TimeoutExpired:
			self.process.kill()
			self.stdout, self.stderr = self.process.communicate()
			raise AssertionError(f"linda say did not end within {timeout} s\n{self.describe()}") from None
		self.ended = time.monotonic()
		assert snapshot(self.home.configDir) == self.configBefore, "the user's configuration changed"
		leftOver = [
			path.name for path in self.home.temporaryDir.iterdir() if path.name.startswith(TEMPORARY_PREFIX)
		]
		assert not leftOver, f"temporary directories were left behind: {leftOver}"
		for name, content in self.launcherLogsBefore.items():
			assert (self.home.stateDir / name).read_bytes() == content, f"the launcher's {name} changed"
		changed = harness.changedPaths(self.treeBefore, harness.treeState())
		assert not changed, f"the run changed the repository: {changed}"
		return self.process.returncode

	@property
	def events(self) -> list[dict]:
		return harness.readEvents(self.eventsPath)

	def exitRecord(self) -> dict:
		records = [event for event in self.events if event["kind"] == "exit"]
		assert records, f"the run recorded no exit\n{self.describe()}"
		return records[0]

	def result(self) -> harness.ChildResult:
		return harness.ChildResult.fromEvents(
			self.events,
			self.overlay,
			returnCode=self.process.returncode,
			stdout=self.stdout,
			stderr=self.stderr,
		)

	def log(self) -> str:
		path = self.home.sayDir / "say.log"
		return path.read_text(encoding="utf-8") if path.exists() else ""

	def describe(self) -> str:
		parts = [
			harness.describeExit(self.process.returncode),
			f"stdout: {self.stdout!r}",
			f"stderr: {self.stderr!r}",
		]
		output = self.home.sayDir / "output.log"
		if output.exists():
			parts.append(f"output.log: {output.read_text(encoding='utf-8', errors='replace')[-3000:]!r}")
		if log := self.log():
			parts.append(f"log tail:\n{log[-6000:]}")
		return "\n".join(parts)

	def assertClean(self, *, ignoring: set[str] = frozenset()) -> None:
		"""harness's checks, but for standard error, which tests compare whole, and the say mode's own
		output, which must be empty."""
		self.result().assertClean(ignoring={"stderr", "returnCode", *ignoring})
		output = self.home.sayDir / "output.log"
		if output.exists():
			assert not output.read_text(encoding="utf-8", errors="replace"), self.describe()
		assert self.exitRecord()["sockets"] == [], self.describe()

	def assertSpoke(self) -> re.Match:
		"""The run ended as a success does: status 0, nothing on standard output and the one line."""
		assert self.process.returncode == 0, self.describe()
		assert self.stdout == "", self.describe()
		match = SPOKE.fullmatch(self.stderr)
		assert match, self.describe()
		return match

	def assertFailed(self, status: int, sentence: str) -> None:
		"""The run ended with status and one line that holds sentence and, for a failure after the log is
		open, names the log."""
		assert self.process.returncode == status, self.describe()
		assert self.stdout == "", self.describe()
		assert self.stderr.endswith("\n") and self.stderr.count("\n") == 1, self.describe()
		assert self.stderr.startswith("linda say: "), self.describe()
		assert sentence in self.stderr, self.describe()
		if self.log():
			assert self.stderr.endswith(f", and the log is {self.home.sayDir / 'say.log'}\n"), self.describe()


def say(
	home: Home,
	*arguments: str,
	env: dict[str, str | None] | None = None,
	timeout: float = RUN_BOUND,
) -> Say:
	run = Say(home, ("say", *arguments), env)
	run.wait(timeout)
	return run


@pytest.fixture
def home(tmp_path: Path) -> Home:
	return Home(tmp_path / "home")


def requireOpenevvDriverDirectory() -> None:
	"""Fail, rather than skip, without openevv's driver directory: the dev shell always names one."""
	directory = os.environ.get(OPENEVV_DRIVER_DIR_VARIABLE)
	if not directory or not os.path.isfile(os.path.join(directory, "openevv.py")):
		pytest.fail(
			f"{OPENEVV_DRIVER_DIR_VARIABLE} names no openevv driver directory. Run the tests in the dev "
			"shell, which sets it to the directory nix/openevv-driver.nix builds.",
			pytrace=False,
		)


def test_speaksThroughTheWholeSpeechPath(home: Home):
	run = say(home, "Hello from the say mode.")
	spoke = run.assertSpoke()
	run.assertClean()
	assert spoke["synth"] == "transcript" and spoke["voice"] == "transcript"
	assert int(spoke["firstAudio"]) < 2000
	spoken = harness.transcriptRecords().spokenTexts(home.transcript())
	assert spoken == ["Hello from the say mode."]
	audio = home.captured()
	assert [(c.channels, c.rate) for c in audio] == [(1, TRANSCRIPT_RATE)]
	# The transcript synth feeds 10 ms of audio for every character it is given, the separator NVDA
	# puts after each chunk included, and all of it must have been heard.
	(record,) = [r for r in home.transcript() if r["kind"] == "speak"]
	characters = sum(len(item) for item in record["data"] if isinstance(item, str))
	assert audio[0].seconds == pytest.approx(characters * 0.01, abs=0.002)
	assert peak(audio[0].samples) == TRANSCRIPT_LEVEL
	log = run.log()
	assert "Loaded synthDriver transcript" in log
	assert spoke.group(0).removeprefix("linda say: ").strip() in log


def test_speaksWithEspeak(home: Home):
	run = say(home, "--synth", "espeak", "This is Linda, speaking through eSpeak.")
	spoke = run.assertSpoke()
	run.assertClean()
	assert spoke["synth"] == "espeak" and spoke["voice"] == "en"
	audio = home.captured()
	assert [(c.channels, c.rate) for c in audio] == [(1, 22050)]
	assert audio[0].seconds > 1.5
	assert peak(audio[0].samples) > 5000


def test_speaksWithOpenevv(home: Home):
	requireOpenevvDriverDirectory()
	run = say(home, "--synth", "openevv", "This is openevv inside Linda.")
	spoke = run.assertSpoke()
	run.assertClean()
	assert spoke["synth"] == "openevv"
	audio = home.captured()
	assert len(audio) == 1 and audio[0].channels == 1
	assert audio[0].seconds > 1.0
	assert peak(audio[0].samples) > 5000


def test_gainOfAHalfIsSixDecibelsDown(tmp_path: Path):
	levels = []
	for gain in ((), ("--gain", "0.5")):
		home = Home(tmp_path / f"home{len(levels)}")
		run = say(home, "--synth", "espeak", *gain, "One two three four five.")
		run.assertSpoke()
		run.assertClean()
		(audio,) = home.captured()
		levels.append(rms(audio.samples))
	assert 20 * math.log10(levels[1] / levels[0]) == pytest.approx(-6.02, abs=0.3)


def test_soundsFollowTheText(home: Home):
	run = say(home, "--sounds", "Sounds next.")
	run.assertSpoke()
	run.assertClean()
	audio = home.captured()
	waves = HERE.parent.parent / "source" / "waves"
	expected = []
	for name in ("start.wav", "error.wav"):
		with wave.open(str(waves / name), "rb") as f:
			expected.append((f.getnchannels(), f.getframerate(), f.getnframes() / f.getframerate()))
	described = [(c.channels, c.rate, round(c.seconds, 2)) for c in audio]
	assert len(audio) == 4, described
	speech, start, beep, error = audio
	assert (speech.channels, speech.rate) == (1, TRANSCRIPT_RATE)
	assert (start.channels, start.rate) == expected[0][:2] and start.seconds == pytest.approx(
		expected[0][2], abs=0.01
	)
	# tones.beep: 100 ms at 440 Hz through its own stereo player.
	assert (beep.channels, beep.rate) == (2, 44100) and beep.seconds == pytest.approx(0.1, abs=0.01)
	assert (error.channels, error.rate) == expected[1][:2] and error.seconds == pytest.approx(
		expected[1][2], abs=0.01
	)


@pytest.mark.parametrize("sinkName", ["capture", "pipewire", "bogus"])
def test_checkPlaysToTheNullSinkWhateverTheOutput(home: Home, sinkName: str):
	run = say(home, "--check", "Checking.", env={"LINDA_AUDIO_SINK": sinkName})
	spoke = run.assertSpoke()
	run.assertClean()
	assert spoke["null"] and spoke["synth"] == "transcript"
	assert harness.transcriptRecords().spokenTexts(home.transcript()) == ["Checking."]
	assert home.captured() == []
	# Not even the module of another sink is loaded, nor a library of one.
	modules = run.exitRecord()["modules"]
	assert "linuxPlatform.audioOutput.nullSink" in modules
	assert not [
		m for m in modules if m.startswith("linuxPlatform.audioOutput.") and "Sink" in m and "null" not in m
	]
	loaded = [what for what in run.result().reached if what.startswith("ctypes.dlopen")]
	assert not [what for what in loaded if "pipewire" in what or "pwsink" in what], loaded


def test_nullSinkWithoutCheckIsRefused(home: Home):
	run = say(home, "Nobody would hear this.", env={"LINDA_AUDIO_SINK": "null"})
	run.assertFailed(4, "the audio output is the null sink, which nobody hears, and only --check plays to it")
	run.assertClean()
	assert home.transcript() == []


@pytest.mark.parametrize(
	("env", "sentence"),
	[
		(
			{"LINDA_AUDIO_SINK": "capture", "LINDA_AUDIO_CAPTURE_DIR": "/nonexistent"},
			"the audio output could not be set up, [Errno 20] the audio capture directory does not exist",
		),
		(
			{"LINDA_AUDIO_SINK": "bogus"},
			(
				"the audio output could not be set up, [Errno 22] LINDA_AUDIO_SINK must be null, capture "
				"or pipewire, not 'bogus'"
			),
		),
	],
)
def test_audioOutputThatCannotBeSetUpExits4(home: Home, env: dict[str, str], sentence: str):
	run = say(home, "Hello.", env=env)
	run.assertFailed(4, sentence)
	# The failure is logged, with its traceback, for whoever reads the log.
	run.assertClean(ignoring={"logErrors"})
	assert run.result().logErrors == ["ERROR: The audio output could not be set up"]


def test_nothingReachingTheOutputExits4(home: Home):
	# In immediate mode the transcript synth reports the end of speech at once and plays nothing.
	run = say(home, "Silent.", env={"LINDA_TRANSCRIPT_MODE": "immediate"})
	run.assertFailed(4, "nothing reached the capture sink, which may not be running")
	run.assertClean()


def test_synthesizerThatDoesNotLoadExits3(home: Home):
	run = say(home, "--synth", "nosuchsynth", "Hello.")
	run.assertFailed(3, "the synthesizer nosuchsynth did not load")
	run.assertClean(ignoring={"logErrors"})
	assert run.result().logErrors == ["ERROR: setSynth failed for nosuchsynth"]
	assert home.captured() == []


def test_unknownVoiceExits2(home: Home):
	run = say(home, "--voice", "klingon", "Hello.")
	run.assertFailed(
		2,
		"transcript has no voice klingon, and linda say --synth transcript --list-voices lists those it has",
	)
	run.assertClean()
	assert home.transcript() == []


def test_stalledSynthesizerExits5(home: Home):
	# In manual mode the transcript synth never reports the end of speech by itself.
	run = say(home, "--timeout", "1", "Stalled.", env={"LINDA_TRANSCRIPT_MODE": "manual"})
	run.assertFailed(5, "the synthesizer transcript did not finish within 1 second")
	run.assertClean()
	assert run.ended - run.started < 1 + 30
	assert [r["kind"] for r in home.transcript()] == ["speak", "cancel"]


def test_errorLoggedAlongsideSpeechExits1(tmp_path: Path):
	# NVDA cannot parse this, logs an error, starts from defaults and moves the file aside, which it
	# does to the copy: the user's file stays as it is.
	home = Home(tmp_path / "home", config="[speech\nsynth = transcript\n")
	run = say(home, "--synth", "transcript", "Spoken despite the error.")
	run.assertFailed(
		1, "spoke with transcript, voice transcript, into the capture sink, but NVDA logged an error"
	)
	run.assertClean(ignoring={"logErrors"})
	assert [e.split(";")[0] for e in run.result().logErrors] == ["ERROR: Error loading base configuration"]
	assert harness.transcriptRecords().spokenTexts(home.transcript()) == ["Spoken despite the error."]


def test_configurationComesFromWhereItIsAskedFor(tmp_path: Path):
	home = Home(tmp_path / "home", config="[speech]\nsynth = nosuchsynth\n")
	other = tmp_path / "other"
	other.mkdir()
	(other / "nvda.ini").write_text(TRANSCRIPT_CONFIG, encoding="utf-8")
	otherBefore = snapshot(other)
	fromOther = say(home, "--config-path", str(other), "From the other configuration.")
	assert fromOther.assertSpoke()["synth"] == "transcript"
	fromOther.assertClean()
	assert snapshot(other) == otherBefore
	# Upstream's defaults: synth auto, which is eSpeak.
	fromDefaults = say(home, "--factory", "From the defaults.")
	assert fromDefaults.assertSpoke()["synth"] == "espeak"
	fromDefaults.assertClean()


def test_configurationUnderTheFormerNameIsReadUntilAStartMovesIt(tmp_path: Path):
	home = Home(tmp_path / "home")
	former = home.configDir.parent / "basedreader"
	home.configDir.rename(former)
	run = say(home, "From the former name's configuration.")
	assert run.assertSpoke()["synth"] == "transcript"
	run.assertClean()
	# Only read: a copy started under the former name may still be running from it.
	assert former.is_dir()
	assert not home.configDir.exists()


def test_listsTheVoices(home: Home):
	run = say(home, "--synth", "espeak", "--list-voices")
	run.assertClean()
	assert run.process.returncode == 0 and run.stderr == "", run.describe()
	lines = run.stdout.splitlines()
	assert len(lines) > 100
	assert "en, English (Great Britain)" in lines and "pl, Polish" in lines, lines[:20]
	assert home.captured() == []


def test_helpIsPlainSentences(home: Home):
	run = say(home, "--help")
	assert run.process.returncode == 0 and run.stderr == "", run.describe()
	lines = run.stdout.splitlines()
	assert lines[0].startswith("linda say speaks the text")
	# Read aloud by a screen reader: no brackets, bars or other symbols standing for words.
	assert not re.search(r"[\[\]{}<>|=]", run.stdout), run.stdout
	assert not run.log()


@pytest.mark.parametrize(
	("arguments", "line"),
	[
		((), "linda say: it needs the text to speak after its options, as in linda say Hello.\n"),
		(("",), "linda say: the text to speak is empty.\n"),
		(
			("--bogus", "Hello."),
			"linda say: --bogus is not one of its options, which linda say --help lists.\n",
		),
		# NVDA's own parser knows these, and would start the screen reader on them.
		(
			("-r", "Hello."),
			"linda say: -r is not one of its options, which linda say --help lists.\n",
		),
		(
			("--replace", "Hello."),
			"linda say: --replace is not one of its options, which linda say --help lists.\n",
		),
		(
			("--install", "Hello."),
			"linda say: --install is not one of its options, which linda say --help lists.\n",
		),
		(("-q",), "linda say: -q is not one of its options, which linda say --help lists.\n"),
		(
			("--syn", "espeak", "Hello."),
			"linda say: --syn is not one of its options, which linda say --help lists.\n",
		),
		(
			("--rate", "300", "Hello."),
			"linda say: the rate must be a whole number from 0 to 100, not 300.\n",
		),
		(
			("--pitch", "high", "Hello."),
			"linda say: the pitch must be a whole number from 0 to 100, not high.\n",
		),
		(("--gain", "2", "Hello."), "linda say: the gain must be a number from 0 to 1, not 2.\n"),
		(("--gain", "nan", "Hello."), "linda say: the gain must be a number from 0 to 1, not nan.\n"),
		(
			("--timeout", "0", "Hello."),
			"linda say: the timeout must be a number of seconds above 0, not 0.\n",
		),
		(("--synth",), "linda say: --synth needs a value after it.\n"),
		(
			("--factory", "--config-path", "/", "Hello."),
			"linda say: --config-path and --factory cannot be given together.\n",
		),
		(
			("--config-path", "/nonexistent", "Hello."),
			"linda say: the configuration directory /nonexistent does not exist.\n",
		),
		(
			("--config-path", "/", "Hello."),
			"linda say: the configuration directory / holds no nvda.ini.\n",
		),
	],
)
def test_mistakesInTheArgumentsExit2(home: Home, arguments: tuple[str, ...], line: str):
	run = say(home, *arguments, timeout=REFUSAL_BOUND)
	assert (run.process.returncode, run.stdout, run.stderr) == (2, "", line), run.describe()
	assertNothingStarted(run)


@pytest.mark.parametrize(
	"arguments",
	[("--debug-logging", "say", "Hello."), ("-m", "say"), ("Hello.", "say")],
)
def test_sayAnywhereButFirstStartsNothing(home: Home, arguments: tuple[str, ...]):
	run = Say(home, arguments)
	run.wait(REFUSAL_BOUND)
	assert (run.process.returncode, run.stdout) == (2, ""), run.describe()
	assert run.stderr == (
		"linda: the word say starts the say mode only as the first argument, so nothing was started\n"
	), run.describe()
	assertNothingStarted(run)


def assertNothingStarted(run: Say) -> None:
	"""The run imported none of NVDA's core, took no lock, opened no log and connected to nothing."""
	modules = run.exitRecord()["modules"]
	assert not {"core", "config", "wx", "logHandler"} & set(modules), run.describe()
	assert not (run.home.runtimeDir / "linda").exists()
	assert not run.home.sayDir.exists()
	assert run.exitRecord()["sockets"] == []
	run.result().assertClean(ignoring={"stderr", "returnCode"})


def test_optionsMayFollowTheText(home: Home):
	run = say(home, "Checking", "--check", "again.")
	assert run.assertSpoke()["null"]
	run.assertClean()
	assert harness.transcriptRecords().spokenTexts(home.transcript()) == ["Checking again."]


def test_logsInADirectoryOfItsOwn(home: Home):
	first = say(home, "First.")
	first.assertSpoke()
	firstLog = (home.sayDir / "say.log").read_text(encoding="utf-8")
	second = say(home, "Second.")
	second.assertSpoke()
	second.assertClean()
	# NVDA keeps the previous log as nvda-old.log beside the new one, which is why the say mode's log is
	# not beside the launcher's.
	assert (home.sayDir / "nvda-old.log").read_text(encoding="utf-8") == firstLog
	assert "Second." in second.log() and "Second." not in firstLog
	assert sorted(path.name for path in home.stateDir.iterdir()) == ["nvda-old.log", "nvda.log", "say"]
	modes = (home.sayDir.stat().st_mode & 0o777, (home.sayDir / "say.log").stat().st_mode & 0o777)
	assert modes == (0o700, 0o600)


#: About six seconds of speech at 20 ms a character.
LONG_TEXT = " ".join(["counting slowly"] * 20)


@pytest.mark.parametrize(
	("signalNumber", "ignoredAtStart"),
	[(signal.SIGINT, False), (signal.SIGINT, True), (signal.SIGTERM, False), (signal.SIGHUP, False)],
)
def test_stopSignalSilencesSpeechAtOnce(home: Home, signalNumber: signal.Signals, ignoredAtStart: bool):
	prefix = ()
	if ignoredAtStart:
		# As a background job of a shell without job control starts.
		prefix = (shutil.which("env"), f"--ignore-signal={signalNumber.name.removeprefix('SIG')}")
	run = Say(home, ("say", LONG_TEXT), {"LINDA_TRANSCRIPT_MS_PER_CHAR": "20"}, prefix)
	deadline = time.monotonic() + RUN_BOUND
	while not (spoken := [r for r in home.transcript() if r["kind"] == "speak"]):
		assert time.monotonic() < deadline and run.process.poll() is None, run.describe()
		time.sleep(0.01)
	time.sleep(1.0)
	sentAt = time.monotonic()
	run.process.send_signal(signalNumber)
	assert run.wait() == 6, run.describe()
	assert (run.stdout, run.stderr) == ("", INTERRUPTED), run.describe()
	run.assertClean()
	start = next(e for e in run.events if e["kind"] == "start")
	assert start["isSigintIgnored"] == ignoredAtStart
	cancels = [r["t"] for r in home.transcript() if r["kind"] == "cancel"]
	assert cancels, home.transcript()
	assert cancels[0] - sentAt < 0.5, cancels[0] - sentAt
	assert run.ended - sentAt < 10
	heard = sum(c.seconds for c in home.captured())
	# Speech was playing when the signal came, and stopped there rather than playing to its end.
	assert 0.5 < heard < sentAt - spoken[0]["t"] + 0.2, heard


def throughPipewire(runtimeDir: str) -> dict[str, str]:
	"""The say mode's environment for PipeWire, whose client looks for the daemon's socket in
	runtimeDir rather than in the home's own runtime directory."""
	return {"LINDA_AUDIO_SINK": "pipewire", "PIPEWIRE_RUNTIME_DIR": runtimeDir}


def assertCleanThroughPipewireSay(run: Say) -> None:
	"""Say.assertClean for a run that played through PipeWire, whose client library it maps and whose
	connection it holds until it exits."""
	assertCleanThroughPipewire(run.result(), ignoring={"stderr", "returnCode"})
	output = run.home.sayDir / "output.log"
	if output.exists():
		assert not output.read_text(encoding="utf-8", errors="replace"), run.describe()


def test_isHeardThroughPipewire(home: Home, pipewire: PipewireFixture):
	monitor = pipewire.monitor()
	started = time.monotonic_ns()
	run = say(
		home,
		"--synth",
		"espeak",
		"This is Linda, speaking through PipeWire.",
		env=throughPipewire(os.environ["XDG_RUNTIME_DIR"]),
	)
	spoke = run.assertSpoke()
	assertCleanThroughPipewireSay(run)
	assert spoke["synth"] == "espeak" and spoke["output"] == "through PipeWire"
	first = monitor.soundAfter(started)
	sounds = [e["graph_ns"] for e in monitor.eventsOf("sound") if e["graph_ns"] >= started]
	last = monitor.silenceAfter(sounds[-1])
	assert (last - first) / 1e9 > 1.5, (first, last)


def test_aStopSignalSilencesPipewireAtOnce(home: Home, pipewire: PipewireFixture):
	monitor = pipewire.monitor()
	started = time.monotonic_ns()
	run = Say(home, ("say", "--synth", "espeak", LONG_TEXT), throughPipewire(os.environ["XDG_RUNTIME_DIR"]))
	monitor.soundAfter(started, timeout=RUN_BOUND)
	time.sleep(1.0)
	sentAt = time.monotonic_ns()
	run.process.send_signal(signal.SIGINT)
	assert run.wait() == 6, run.describe()
	assert (run.stdout, run.stderr) == ("", INTERRUPTED), run.describe()
	assertCleanThroughPipewireSay(run)
	# The monitor reports what the graph has played a cycle or two after it played it.
	time.sleep(0.2)
	edges = sorted((e["graph_ns"], kind) for kind in ("sound", "silence") for e in monitor.eventsOf(kind))
	before = [kind for ns, kind in edges if ns < sentAt]
	after = [(ns, kind) for ns, kind in edges if ns >= sentAt]
	if before[-1:] == ["sound"]:
		# One edge, to silence, and nothing heard after it.
		assert [kind for ns, kind in after] == ["silence"], after
		stoppedMs = (after[0][0] - sentAt) / 1e6
		assert stoppedMs < 50, stoppedMs
	else:
		# The signal came in a pause between words, and the next word never began.
		assert not after, after


def test_withNoPipewireItEndsByItselfWithStatus4(home: Home):
	run = say(home, "--synth", "espeak", "Nobody hears this.", env=throughPipewire(str(home.runtimeDir)))
	run.assertFailed(4, "nothing reached PipeWire, which may not be running")
	assertCleanThroughPipewireSay(run)


def _xServer() -> int:
	"""The process ID of the rig's X server."""
	for entry in Path("/proc").iterdir():
		if not entry.name.isdigit():
			continue
		try:
			if (entry / "comm").read_text(encoding="ascii").strip() == "Xvfb":
				return int(entry.name)
		except OSError:
			continue
	raise AssertionError("the rig's X server was not found")


def _socketsOf(processID: int) -> set[str]:
	found = set()
	for fd in Path(f"/proc/{processID}/fd").iterdir():
		try:
			target = os.readlink(fd)
		except OSError:
			continue
		if target.startswith("socket:"):
			found.add(target)
	return found


class SocketWatch(threading.Thread):
	"""Collects every socket a process holds, beyond those it held at first, until stopped. The X
	server holds one for each client connected to it, from the moment it accepts the connection."""

	def __init__(self, processID: int) -> None:
		super().__init__(daemon=True)
		self.processID = processID
		self.baseline = _socketsOf(processID)
		self.seen: set[str] = set()
		self._isStopping = threading.Event()
		self.start()

	def run(self) -> None:
		while not self._isStopping.is_set():
			self.seen |= _socketsOf(self.processID) - self.baseline
			time.sleep(0.001)

	def stopWatching(self) -> set[str]:
		self._isStopping.set()
		self.join()
		return self.seen


def _openBusConnection(address: str) -> "Gio.DBusConnection":
	from gi.repository import Gio

	return Gio.DBusConnection.new_for_address_sync(
		address,
		Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
		None,
		None,
	)


def _connectionNumber(address: str) -> int:
	"""Connect to the bus at address and return the number in the unique name the bus gave the
	connection, which a bus counts up by one for every connection made to it."""
	connection = _openBusConnection(address)
	try:
		return int(connection.get_unique_name().rsplit(".", 1)[1])
	finally:
		connection.close_sync(None)


def _busAddresses() -> dict[str, str]:
	from gi.repository import Gio, GLib

	session = os.environ["DBUS_SESSION_BUS_ADDRESS"]
	connection = _openBusConnection(session)
	try:
		reply = connection.call_sync(
			"org.a11y.Bus",
			"/org/a11y/bus",
			"org.a11y.Bus",
			"GetAddress",
			None,
			GLib.VariantType("(s)"),
			Gio.DBusCallFlags.NONE,
			-1,
			None,
		)
	finally:
		connection.close_sync(None)
	return {"session bus": session, "accessibility bus": reply.unpack()[0]}


def test_neverConnectsToTheDisplayOrTheBuses(home: Home):
	# The display and both buses are named to it, as they are on a desktop.
	assert os.environ.get("DISPLAY") and os.environ.get("DBUS_SESSION_BUS_ADDRESS")
	addresses = _busAddresses()
	xServer = _xServer()
	# Each way of watching sees a client that does connect.
	watch = SocketWatch(xServer)
	client = subprocess.Popen([shutil.which("xprop"), "-root", "-spy"], stdout=subprocess.DEVNULL)
	try:
		deadline = time.monotonic() + 10
		while not watch.seen:
			assert time.monotonic() < deadline, "the X server's sockets did not show a client connecting"
			time.sleep(0.01)
	finally:
		client.kill()
		client.wait()
		watch.stopWatching()
	for name, address in addresses.items():
		first = _connectionNumber(address)
		subprocess.run(
			[
				shutil.which("dbus-send"),
				f"--bus={address}",
				"--print-reply",
				"--dest=org.freedesktop.DBus",
				"/org/freedesktop/DBus",
				"org.freedesktop.DBus.GetId",
			],
			check=True,
			stdout=subprocess.DEVNULL,
		)
		assert _connectionNumber(address) == first + 2, f"the {name} does not number connections one by one"

	before = {name: _connectionNumber(address) for name, address in addresses.items()}
	watch = SocketWatch(xServer)
	run = say(home, "Nothing here reaches the desktop.")
	connectedToX = watch.stopWatching()
	after = {name: _connectionNumber(address) for name, address in addresses.items()}
	run.assertSpoke()
	run.assertClean()
	assert not connectedToX, connectedToX
	assert {name: after[name] - before[name] for name in addresses} == dict.fromkeys(addresses, 1)
