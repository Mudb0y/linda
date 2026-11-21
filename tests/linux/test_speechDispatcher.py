"""Linda's Speech Dispatcher driver, against a private daemon started inside the rig.

The daemon is the dev shell's, LINDA_SPEECHD, started with its own package's configuration, which
it only reads, on a socket in the test's own runtime directory, with a configuration directory of the
test's own that stays empty. The child the driver runs in is given that runtime directory and that
socket as SPEECHD_ADDRESS, and before anything of NVDA is imported it installs an audit hook that
refuses, before it is made, every connection but one to that socket. So a test can fail, but it cannot
reach another daemon; the rig, which has an empty /run and none of the host's sockets, is the second
wall. Run under the rig:

	nix develop . -c tests/linux/rig python3 -m pytest tests/linux/test_speechDispatcher.py

The tests that time sound start the rig's private PipeWire with its PulseAudio server, which the stock
configuration's audio output reaches, and listen on the sink with pwmon. The others give the daemon no
sound server, and it falls back to libao, which plays nothing, so marks and ends come as fast as the
module synthesizes.
"""

import json
import os
import signal
import subprocess
import tempfile
import textwrap
import time
from pathlib import Path

import harness
import pytest
from pipewireFixture import RATE, PipewireFixture

SPEECHD_VARIABLE = "LINDA_SPEECHD"


def speechdBinary() -> Path:
	value = os.environ.get(SPEECHD_VARIABLE)
	if not value:
		pytest.fail(
			f"{SPEECHD_VARIABLE} is unset. Run the tests in the dev shell, which names the daemon.",
			pytrace=False,
		)
	return Path(value)


def speechdConfigDir() -> Path:
	return speechdBinary().parent.parent / "etc" / "speech-dispatcher"


class PrivateDaemon:
	"""A Speech Dispatcher of the test's own, which it alone started and alone stops."""

	def __init__(self, workDir: Path, *, pulseServer: str | None = None) -> None:
		harness.requireRig()
		self.workDir = workDir
		# The rig's own /tmp, as a socket's path may not be longer than 107 bytes, which the test's
		# directory under the scratch folder can be.
		self.runtime = Path(tempfile.mkdtemp(prefix="speechd-", dir="/tmp"))
		self.socket = self.runtime / "speech-dispatcher" / "speechd.sock"
		self.configHome = workDir / "config"
		self.log = workDir / "log"
		for directory in (self.socket.parent, self.configHome, self.log):
			directory.mkdir(parents=True, exist_ok=True)
		self.pulseServer = pulseServer
		self.process: subprocess.Popen | None = None

	def command(self) -> list[str]:
		return [
			str(speechdBinary()),
			"--run-single",
			"--timeout",
			"0",
			"--log-level",
			"3",
			"--log-dir",
			str(self.log),
			"--communication-method",
			"unix_socket",
			"--socket-path",
			str(self.socket),
			"--config-dir",
			str(speechdConfigDir()),
		]

	def environment(self) -> dict[str, str]:
		env = {**os.environ, "XDG_RUNTIME_DIR": str(self.runtime), "XDG_CONFIG_HOME": str(self.configHome)}
		if self.pulseServer:
			env["PULSE_SERVER"] = self.pulseServer
		return env

	def start(self) -> "PrivateDaemon":
		self.socket.unlink(missing_ok=True)
		self.process = subprocess.Popen(
			self.command(),
			env=self.environment(),
			stdin=subprocess.DEVNULL,
			stdout=open(self.workDir / "daemon.out", "a"),  # noqa: SIM115
			stderr=subprocess.STDOUT,
		)
		end = time.monotonic() + 30
		while not self.socket.exists():
			if self.process.poll() is not None or time.monotonic() > end:
				raise RuntimeError(f"the private Speech Dispatcher did not start: {self.tail()}")
			time.sleep(0.02)
		return self

	def tail(self) -> str:
		path = self.workDir / "daemon.out"
		return path.read_text(errors="replace")[-2000:] if path.exists() else ""

	def stop(self) -> None:
		if self.process is not None and self.process.poll() is None:
			self.process.terminate()
			try:
				self.process.wait(5)
			except subprocess.TimeoutExpired:
				self.process.kill()
				self.process.wait(5)

	def childEnv(self, **extra: str) -> dict[str, str]:
		return {
			"XDG_RUNTIME_DIR": str(self.runtime),
			"SPEECHD_ADDRESS": f"unix_socket:{self.socket}",
			"LINDA_TEST_SPEECHD_SOCKET": str(self.socket),
			**extra,
		}


@pytest.fixture
def daemon(tmp_path: Path):
	private = PrivateDaemon(tmp_path / "speechd").start()
	yield private
	private.stop()
	assert not list(private.configHome.iterdir()), "something wrote configuration for the daemon"


@pytest.fixture
def heardDaemon(tmp_path: Path):
	"""A private daemon playing into the rig's PipeWire through its PulseAudio server, and a monitor on
	the sink it plays to."""
	pipewire = PipewireFixture(tmp_path / "pipewire")
	pipewire.start()
	try:
		pulse = pipewire._spawn(
			[pipewire.pipewireBin / "pipewire-pulse"],
			"pipewire-pulse",
			# Its package's own configuration, and none of the host's drop-ins.
			{"PIPEWIRE_CONFIG_DIR": str(pipewire.pipewireBin.parent / "share" / "pipewire")},
		)
		native = Path(os.environ["XDG_RUNTIME_DIR"]) / "pulse" / "native"
		pipewire._waitFor(lambda: native.exists() or pulse.poll() is not None, "PipeWire's PulseAudio server")
		private = PrivateDaemon(tmp_path / "speechd", pulseServer=f"unix:{native}").start()
		try:
			yield private, pipewire.monitor()
		finally:
			private.stop()
	finally:
		pipewire.stop()


#: The child's first lines: no connection but to the private daemon's socket, then NVDA's speech with
#: the driver loaded, a record of what it sent and of what it reported, and helpers.
PRELUDE = """
import os
import socket
import sys
import time

ALLOWED = os.environ["LINDA_TEST_SPEECHD_SOCKET"]


def refuseOtherSockets(event, args):
	if event != "socket.connect":
		return
	sock, address = args[0], args[1]
	if sock.family == socket.AF_UNIX and isinstance(address, (str, bytes)) and os.fsdecode(address) == ALLOWED:
		return
	raise RuntimeError(f"the test refuses a connection to {address!r}; only {ALLOWED} may be reached")


sys.addaudithook(refuseOtherSockets)

import core
# core.main's first import after config; entering NVDA's import cycle anywhere else fails.
import NVDAHelper
import characterProcessing
import config
import languageHandler
import queueHandler
import speech
import speechDictHandler
import synthDriverHandler
import synthDrivers.speechDispatcher as driver
from speech.commands import (
	BreakCommand,
	CharacterModeCommand,
	IndexCommand,
	LangChangeCommand,
	PitchCommand,
	RateCommand,
	VolumeCommand,
)

assert driver.daemonAddress() == (socket.AF_UNIX, ALLOWED), driver.daemonAddress()
now = time.monotonic_ns
sent = []
_send = driver._Connection._send


def send(self, data, onReply, **options):
	reply = _send(self, data, onReply, **options)
	sent.append([now(), data.split("\\r\\n")[0], reply[-1]])
	return reply


driver._Connection._send = send
languageHandler.setLanguage("en")
speechDictHandler.initialize()
characterProcessing.initialize()
#: Twenty sentences of about two seconds each, with an index after each.
LONG = []
for i in range(1, 21):
	LONG += [f"This is sentence number {i}, which goes on for a little while.", IndexCommand(i)]


class Heard:
	# extensionPoints holds handlers weakly, so these are bound methods of an object kept alive here.
	def __init__(self):
		self.events = []
		synthDriverHandler.synthIndexReached.register(self.onIndex)
		synthDriverHandler.synthDoneSpeaking.register(self.onDone)

	def onIndex(self, synth, index):
		self.events.append(["index", index, now()])

	def onDone(self, synth):
		self.events.append(["done", None, now()])

	def since(self, t):
		return [[kind, index] for kind, index, at in self.events if at >= t]


heard = Heard()


def waitFor(condition, timeout=20.0):
	end = time.monotonic() + timeout
	while time.monotonic() < end:
		queueHandler.pumpAll()
		if condition():
			return True
		time.sleep(0.001)
	return False


def speakAndWait(sequence, timeout=20.0):
	start = now()
	synth.speak(sequence)
	assert waitFor(lambda: ["done", None] in heard.since(start), timeout), (sequence, heard.since(start))
	return heard.since(start)


def sentSince(t):
	return [[line, reply] for at, line, reply in sent if at >= t]


def useSpeechDispatcher(module="espeak-ng"):
	\"\"\"Speech Dispatcher as NVDA's synth, with the module's first American English voice, as the
	stock configuration's default module is pico, which reports no marks.\"\"\"
	config.conf["speech"]["synth"] = "speechDispatcher"
	speech.initialize()
	synth = synthDriverHandler.getSynth()
	assert synth.name == "speechDispatcher", synth.name
	if module:
		voice = next(
			v for v, info in synth.availableVoices.items() if v.startswith(f"{module}/") and info.language == "en_US"
		)
		synthDriverHandler.changeVoice(synth, voice)
	return synth
"""

END = """
synthDriverHandler.setSynth(None)
"""


def runDriver(
	tmpPath: Path, daemon: PrivateDaemon, code: str, *, env: dict | None = None
) -> harness.ChildResult:
	done = harness.runChild(
		tmpPath,
		PRELUDE + textwrap.dedent(code) + END,
		env={**daemon.childEnv(), **(env or {})},
		timeout=300,
		recordLevel=harness.LOG_ERROR - 10,
	)
	reached = [what for what in done.reached if what.startswith("socket.connect")]
	assert all(what == f"socket.connect {daemon.socket}" for what in reached), reached
	return done


def lastSoundBefore(monitor, start: int, end: int) -> int | None:
	"""The graph time just after the last frame above pwmon's threshold from start to end."""
	left, right = monitor.samples(start, end)
	for i in range(len(left) - 1, -1, -1):
		if abs(left[i]) > 0.001 or abs(right[i]) > 0.001:
			return start + (i + 1) * 1_000_000_000 // RATE
	return None


def test_notListedWhenNoDaemonRunsOrCanBeStarted(tmp_path: Path):
	absent = PrivateDaemon(tmp_path / "absent")
	done = runDriver(
		tmp_path,
		absent,
		"""
		import shutil

		result = {
			"onPath": shutil.which("speech-dispatcher"),
			"names": dict(synthDriverHandler.getSynthList()),
		}
		""",
	)
	done.assertClean()
	assert done.value["onPath"] is None
	assert "speechDispatcher" not in done.value["names"]
	assert "espeak" in done.value["names"]


def test_listedAndLoadedWhenTheDaemonRuns(tmp_path: Path, daemon: PrivateDaemon):
	done = runDriver(
		tmp_path,
		daemon,
		"""
		names = dict(synthDriverHandler.getSynthList())
		synth = useSpeechDispatcher(module=None)
		result = {"names": names, "voice": synth.voice, "said": speakAndWait(["Hello.", IndexCommand(1)])}
		""",
	)
	done.assertClean()
	assert done.value["names"]["speechDispatcher"] == "Speech Dispatcher"
	# The daemon's default module, which the stock configuration makes pico, with its voice for NVDA's
	# language.
	assert done.value["voice"] == "pico/samantha"
	assert done.value["said"] == [["index", 1], ["done", None]]


def test_aModuleReportingNoMarksIsSplitAtEachMark(tmp_path: Path, daemon: PrivateDaemon):
	done = runDriver(
		tmp_path,
		daemon,
		"""
		synth = useSpeechDispatcher(module="pico")
		rounds = []
		for first in (10, 20):
			start = now()
			said = speakAndWait(["One.", IndexCommand(first + 1), "Two.", IndexCommand(first + 2), "Three.", IndexCommand(first + 3)])
			rounds.append({"said": said, "spoken": [line for line, reply in sentSince(start) if line.startswith("<speak>")]})
		english = next(v for v, info in synth.availableVoices.items() if v.startswith("espeak-ng/") and info.language == "en_US")
		synthDriverHandler.changeVoice(synth, english)
		# A leading mark is reported as the message begins, before eSpeak NG reports it too.
		for first in (30, 40):
			start = now()
			said = speakAndWait([IndexCommand(first + 1), "One.", IndexCommand(first + 2), "Two.", IndexCommand(first + 3)])
			rounds.append({"said": said, "spoken": [line for line, reply in sentSince(start) if line.startswith("<speak>")]})
		result = rounds
		""",
	)
	done.assertClean()
	learning, split, *espeak = done.value
	for first, entry in zip((30, 40), espeak):
		assert entry["said"] == [
			["index", first + 1],
			["index", first + 2],
			["index", first + 3],
			["done", None],
		]
		assert entry["spoken"] == [
			f'<speak><mark name="{first + 1}"/>One.<mark name="{first + 2}"/>Two.<mark name="{first + 3}"/></speak>'
		]
	assert learning["said"] == [["index", 11], ["index", 12], ["index", 13], ["done", None]]
	assert learning["spoken"] == [
		'<speak>One.<mark name="11"/>Two.<mark name="12"/>Three.<mark name="13"/></speak>'
	]
	assert split["said"] == [["index", 21], ["index", 22], ["index", 23], ["done", None]]
	assert split["spoken"] == [
		'<speak>One.<mark name="21"/></speak>',
		'<speak>Two.<mark name="22"/></speak>',
		'<speak>Three.<mark name="23"/></speak>',
	]


#: Stands in for the distribution's speech-dispatcher on PATH: it records how it was started, then
#: runs the dev shell's daemon with its package's configuration.
SPAWN_WRAPPER = """#!{bash}
printf '%s\\n' "$@" > {record}/arguments
env > {record}/environment
exec {speechd} --config-dir {config} "$@"
"""


def test_startsTheDaemonOnPathWhenNoneRuns(tmp_path: Path):
	absent = PrivateDaemon(tmp_path / "spawned")
	bin = tmp_path / "bin"
	bin.mkdir()
	record = tmp_path / "record"
	record.mkdir()
	wrapper = bin / "speech-dispatcher"
	wrapper.write_text(
		SPAWN_WRAPPER.format(
			bash=os.environ["LINDA_BASH"],
			record=record,
			speechd=speechdBinary(),
			config=speechdConfigDir(),
		),
	)
	wrapper.chmod(0o755)
	try:
		done = runDriver(
			tmp_path,
			absent,
			"""
			names = dict(synthDriverHandler.getSynthList())
			synth = useSpeechDispatcher()
			result = {"names": names, "said": speakAndWait(["Started for me.", IndexCommand(1)])}
			""",
			env={
				"PATH": f"{bin}:{os.environ['PATH']}",
				"XDG_CONFIG_HOME": str(absent.configHome),
				# Linda's wrapper clears these; set here, they must not reach the daemon either way.
				"LD_LIBRARY_PATH": str(tmp_path / "nothing"),
				"GI_TYPELIB_PATH": str(tmp_path / "nothing"),
				"NIX_PYTHONPATH": str(tmp_path / "nothing"),
			},
		)
	finally:
		# The daemon detached itself; its process ID is where it writes it, under the runtime directory.
		for pidFile in absent.runtime.glob("speech-dispatcher/**/*.pid"):
			try:
				os.kill(int(pidFile.read_text().split()[0]), signal.SIGTERM)
			except (ValueError, ProcessLookupError):
				pass
	done.assertClean()
	assert done.value["names"]["speechDispatcher"] == "Speech Dispatcher"
	assert done.value["said"] == [["index", 1], ["done", None]]
	assert (record / "arguments").read_text().split("\n")[:5] == [
		"--spawn",
		"--communication-method",
		"unix_socket",
		"--socket-path",
		str(absent.socket),
	]
	environment = (record / "environment").read_text()
	for name in ("LD_LIBRARY_PATH", "GI_TYPELIB_PATH", "NIX_PYTHONPATH"):
		assert f"\n{name}=" not in f"\n{environment}", name
	assert not list(absent.configHome.iterdir())


def test_sequenceCommandsBecomeThisConnectionsParameters(tmp_path: Path, daemon: PrivateDaemon):
	done = runDriver(
		tmp_path,
		daemon,
		"""
		synth = useSpeechDispatcher()
		english = next(v for v, info in synth.availableVoices.items() if v.startswith("espeak-ng/") and info.language == "en_US")
		polish = next(v for v, info in synth.availableVoices.items() if v.startswith("espeak-ng/") and info.language == "pl")
		synth.voice = english
		synth.variant = "none"
		synth.rate = 50
		synth.pitch = 50
		synth.volume = 100
		config.conf["speech"]["speechDispatcher"]["rate"] = 50
		config.conf["speech"]["speechDispatcher"]["pitch"] = 50
		config.conf["speech"]["speechDispatcher"]["volume"] = 100
		start = now()
		said = speakAndWait([
			"Hello",
			IndexCommand(1),
			PitchCommand(offset=30),
			CharacterModeCommand(True),
			"A",
			IndexCommand(2),
			CharacterModeCommand(False),
			PitchCommand(),
			"world",
			BreakCommand(250),
			"a < b & c",
			IndexCommand(3),
			RateCommand(multiplier=0.5),
			"slow",
			RateCommand(),
			VolumeCommand(offset=-40),
			"quiet",
			VolumeCommand(),
			LangChangeCommand("pl_PL"),
			"Dzień dobry",
			IndexCommand(4),
			LangChangeCommand(None),
			CharacterModeCommand(True),
			"xyz",
			CharacterModeCommand(False),
			"line\\none",
			IndexCommand(5),
		])
		lines = sentSince(start)
		markOnly = speakAndWait([IndexCommand(9)])
		result = {
			"said": said,
			"markOnly": markOnly,
			"lines": lines,
			"english": synth._voiceNames[english],
			"polish": synth._voiceNames[polish],
		}
		""",
	)
	done.assertClean()
	value = done.value
	assert value["said"] == [
		["index", 1],
		["index", 2],
		["index", 3],
		["index", 4],
		["index", 5],
		["done", None],
	]
	assert value["markOnly"] == [["index", 9], ["done", None]]
	refused = [[line, reply] for line, reply in value["lines"] if not reply.startswith("2")]
	assert not refused, refused
	english, polish = value["english"], value["polish"]
	commands = [line for line, _reply in value["lines"] if line != "SPEAK"]
	assert commands == [
		"SET SELF OUTPUT_MODULE espeak-ng",
		"SET SELF LANGUAGE en-US",
		f"SET SELF SYNTHESIS_VOICE {english}",
		"SET SELF RATE 0",
		"SET SELF PITCH 0",
		"SET SELF VOLUME 100",
		'<speak>Hello<mark name="1"/></speak>',
		"SET SELF PITCH 60",
		"CHAR A",
		"SET SELF PITCH 0",
		'<speak>world<break time="250ms"/>a &lt; b &amp; c<mark name="3"/></speak>',
		"SET SELF RATE -50",
		"<speak>slow</speak>",
		"SET SELF RATE 0",
		"SET SELF VOLUME 20",
		"<speak>quiet</speak>",
		"SET SELF LANGUAGE pl",
		f"SET SELF SYNTHESIS_VOICE {polish}",
		"SET SELF VOLUME 100",
		'<speak>Dzień dobry<mark name="4"/></speak>',
		"SET SELF LANGUAGE en-US",
		f"SET SELF SYNTHESIS_VOICE {english}",
		'<speak><say-as interpret-as="characters">xyz</say-as>line one<mark name="5"/></speak>',
	]


def test_voicesAndVariantsAreTheDaemonsAndSettingsAreMapped(tmp_path: Path, daemon: PrivateDaemon):
	done = runDriver(
		tmp_path,
		daemon,
		"""
		synth = useSpeechDispatcher()
		voices = {v: [info.displayName, info.language] for v, info in synth.availableVoices.items()}
		variants = list(synth.availableVariants)
		english = next(v for v, info in synth.availableVoices.items() if v.startswith("espeak-ng/") and info.language == "en_US")
		lines = {}
		for name, voice, variant, rate, pitch, volume in (
			("pico", "pico/samantha", "Adam", 0, 100, 0),
			("espeakAdam", english, "Adam", 100, 0, 50),
			("espeakNone", english, "none", 25, 75, 100),
		):
			synthDriverHandler.changeVoice(synth, voice)
			synth.variant = variant
			synth.rate = rate
			synth.pitch = pitch
			synth.volume = volume
			start = now()
			speakAndWait(["Settings."])
			lines[name] = [line for line, reply in sentSince(start) if line.startswith("SET")]
		synth.saveSettings()
		synthDriverHandler.setSynth(None)
		synthDriverHandler.setSynth("speechDispatcher")
		synth = synthDriverHandler.getSynth()
		reloaded = [synth.voice, synth.variant, synth.rate, synth.pitch, synth.volume]
		result = {"voices": voices, "variants": variants, "lines": lines, "english": english, "reloaded": reloaded}
		""",
	)
	done.assertClean()
	value = done.value
	voices = value["voices"]
	assert voices["pico/samantha"] == ["samantha, pico", "en_US"]
	assert voices["flite/kal16"] == ["kal16, flite", "en"]
	assert any(v.startswith("espeak-ng-mbrola/") for v in voices)
	# eSpeak NG lists every variant of every voice as a voice of its own, which are variants here.
	espeak = [v for v in voices if v.startswith("espeak-ng/")]
	assert 100 < len(espeak) < 400, len(espeak)
	assert not [v for v in espeak if "+" in v]
	assert value["variants"][0] == "none" and "Adam" in value["variants"]
	english = value["english"].partition("/")[2]
	lines = value["lines"]
	assert lines["pico"] == [
		"SET SELF OUTPUT_MODULE pico",
		"SET SELF LANGUAGE en-US",
		"SET SELF SYNTHESIS_VOICE samantha",
		"SET SELF RATE -100",
		"SET SELF PITCH 100",
		"SET SELF VOLUME -100",
	]
	assert lines["espeakAdam"] == [
		"SET SELF OUTPUT_MODULE espeak-ng",
		"SET SELF LANGUAGE en-US",
		f"SET SELF SYNTHESIS_VOICE {english}+Adam",
		"SET SELF RATE 100",
		"SET SELF PITCH -100",
		"SET SELF VOLUME 0",
	]
	assert lines["espeakNone"] == [
		f"SET SELF SYNTHESIS_VOICE {english}",
		"SET SELF RATE -50",
		"SET SELF PITCH 50",
		"SET SELF VOLUME 100",
	]
	assert value["reloaded"] == [value["english"], "none", 25, 75, 100]


def test_onlyThisConnectionsParametersAndNvdaDoesTheSymbols(tmp_path: Path, daemon: PrivateDaemon):
	done = runDriver(
		tmp_path,
		daemon,
		"""
		synth = useSpeechDispatcher()
		start = now()
		speech.speakMessage("Save 50% & more!")
		assert waitFor(lambda: ["done", None] in heard.since(start))
		result = {"lines": [line for at, line, reply in sent], "spoken": [line for line, reply in sentSince(start) if line.startswith("<speak>")]}
		""",
	)
	done.assertClean()
	lines = done.value["lines"]
	sets = [line for line in lines if line.startswith("SET ")]
	assert sets and all(line.startswith("SET SELF ") for line in sets), sets
	for line in ("SET SELF PUNCTUATION none", "SET SELF CAP_LET_RECOGN none", "SET SELF SPELLING off"):
		assert line in lines
	spoken = " ".join(done.value["spoken"])
	# NVDA's symbol processing at its default level, which leaves nothing for the daemon to name.
	assert "percent" in spoken and "and" in spoken, spoken
	assert "%" not in spoken and "&amp;" not in spoken, spoken


def test_indexesAndDoneFollowTheSound(tmp_path: Path, heardDaemon):
	daemon, monitor = heardDaemon
	done = runDriver(
		tmp_path,
		daemon,
		"""
		synth = useSpeechDispatcher()
		speakAndWait(["Warm up."])
		time.sleep(0.5)
		asked = now()
		synth.speak(["The first part.", IndexCommand(1), "The second part.", IndexCommand(2), "The end.", IndexCommand(3)])
		assert waitFor(lambda: ["done", None] in heard.since(asked))
		time.sleep(0.5)
		result = {"asked": asked, "events": [e for e in heard.events if e[2] >= asked]}
		""",
	)
	done.assertClean()
	asked = done.value["asked"]
	events = done.value["events"]
	assert [[kind, index] for kind, index, _at in events] == [
		["index", 1],
		["index", 2],
		["index", 3],
		["done", None],
	]
	first = monitor.soundAfter(asked)
	finished = events[-1][2]
	last = lastSoundBefore(monitor, first, finished + 300_000_000)
	print(
		f"first sound {(first - asked) / 1e6:.1f} ms after speak; done {(finished - last) / 1e6:.1f} ms after the last sound"
	)
	assert (first - asked) / 1e6 < 500, "the first sound came more than half a second after speaking"
	# Each index is reported while the speech it ends is heard, not as it is synthesized.
	for _kind, index, at in events[:2]:
		assert first + 300_000_000 < at < last, (index, (at - first) / 1e6, (last - first) / 1e6)
	assert events[0][2] + 300_000_000 < events[1][2]
	assert last - 50_000_000 <= finished, "done was reported before the speech was heard"
	assert lastSoundBefore(monitor, finished + 50_000_000, finished + 300_000_000) is None, "sound after done"


def test_cancelSilencesAtOnceAndTheNextUtteranceSpeaks(tmp_path: Path, heardDaemon):
	daemon, monitor = heardDaemon
	done = runDriver(
		tmp_path,
		daemon,
		"""
		synth = useSpeechDispatcher()
		speakAndWait(["Warm up."])
		rounds = []
		for round in range(6):
			start = now()
			synth.speak(LONG)
			assert waitFor(lambda: ["index", 1] in heard.since(start))
			time.sleep(0.3 + round * 0.11)
			cancelled = now()
			synth.cancel()
			if round % 2:
				# As a key press does: a cancel, and at once what the key says.
				nextAt = cancelled
			else:
				time.sleep(0.6)
				nextAt = now()
			synth.speak(["Next.", IndexCommand(100)])
			assert waitFor(lambda: ["done", None] in heard.since(nextAt))
			afterCancel = [e for e in heard.since(cancelled) if e != ["index", 100]]
			rounds.append({"cancelled": cancelled, "next": nextAt, "afterCancel": afterCancel})
			time.sleep(0.6)
		result = rounds
		""",
	)
	done.assertClean()
	stops, starts = [], []
	for entry in done.value:
		# Only the next utterance's own index and done came after the cancel.
		assert entry["afterCancel"] == [["done", None]], entry
		cancelled, nextAt = entry["cancelled"], entry["next"]
		if nextAt == cancelled:
			# The cancelled speech and the next are not told apart by ear; these rounds show only
			# that nothing of the cancelled one is reported.
			continue
		stop = lastSoundBefore(monitor, cancelled, nextAt)
		assert stop is not None, "the cancel landed in silence"
		stops.append(round((stop - cancelled) / 1e6, 1))
		starts.append(round((monitor.soundAfter(nextAt) - nextAt) / 1e6, 1))
	print(f"cancel to silence, ms: {stops}; new utterance to sound, ms: {starts}")
	assert max(stops) < 250, stops
	assert max(starts) < 300, starts


def test_pauseKeepsSpeechAndResumeSpeaksTheRest(tmp_path: Path, heardDaemon):
	daemon, monitor = heardDaemon
	done = runDriver(
		tmp_path,
		daemon,
		"""
		synth = useSpeechDispatcher()
		speakAndWait(["Warm up."])
		start = now()
		synth.speak(LONG[:8])
		assert waitFor(lambda: ["index", 1] in heard.since(start))
		time.sleep(0.5)
		paused = now()
		synth.pause(True)
		time.sleep(1.5)
		resumed = now()
		whilePaused = heard.since(paused)
		synth.pause(False)
		assert waitFor(lambda: ["done", None] in heard.since(resumed), 30)
		result = {"paused": paused, "resumed": resumed, "whilePaused": whilePaused, "afterResume": heard.since(resumed)}
		""",
	)
	done.assertClean()
	value = done.value
	assert value["whilePaused"] == []
	assert value["afterResume"] == [["index", 2], ["index", 3], ["index", 4], ["done", None]]
	stop = lastSoundBefore(monitor, value["paused"], value["resumed"])
	assert stop is not None
	resumedSound = monitor.soundAfter(value["resumed"])
	print(
		f"pause to silence {(stop - value['paused']) / 1e6:.1f} ms; resume to sound {(resumedSound - value['resumed']) / 1e6:.1f} ms"
	)
	assert (stop - value["paused"]) / 1e6 < 250
	assert (resumedSound - value["resumed"]) / 1e6 < 300


def test_aLostDaemonEndsSpeechAndANewOneIsUsed(tmp_path: Path, daemon: PrivateDaemon):
	done = runDriver(
		tmp_path,
		daemon,
		"""
		import json
		import signal
		import subprocess

		synth = useSpeechDispatcher()
		start = now()
		synth.speak(LONG)
		assert waitFor(lambda: ["index", 1] in heard.since(start))
		killed = now()
		# The daemon the test started, whose process ID it passed.
		os.kill(int(os.environ["LINDA_TEST_SPEECHD_PID"]), signal.SIGKILL)
		ended = waitFor(lambda: ["done", None] in heard.since(killed), 5)
		lost = heard.since(killed)
		again = subprocess.Popen(json.loads(os.environ["LINDA_TEST_SPEECHD_COMMAND"]), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
		try:
			# The killed daemon's socket file is still there until the new one replaces it.
			end = time.monotonic() + 30
			while True:
				try:
					with socket.socket(socket.AF_UNIX) as probe:
						probe.connect(ALLOWED)
					break
				except OSError:
					assert time.monotonic() < end, "the daemon started again never answered"
					time.sleep(0.05)
			# Past the driver's wait after a failed attempt, should the speech above have made one.
			time.sleep(driver.RETRY_INTERVAL + 0.1)
			said = speakAndWait(["Back again.", IndexCommand(50)])
		finally:
			synthDriverHandler.setSynth(None)
			again.terminate()
			again.wait(5)
		result = {"ended": ended, "lost": lost, "said": said}
		""",
		env={
			"LINDA_TEST_SPEECHD_PID": str(daemon.process.pid),
			"LINDA_TEST_SPEECHD_COMMAND": json.dumps(daemon.command()),
		},
	)
	done.assertClean()
	assert done.value["ended"], done.value
	assert done.value["lost"][-1] == ["done", None]
	assert done.value["said"] == [["index", 50], ["done", None]]


def test_cancelledSpeechReportsNothingLate(tmp_path: Path, daemon: PrivateDaemon):
	"""With nothing played, the daemon reports marks as fast as it synthesizes, so a cancel always lands
	while old marks are still on their way."""
	done = runDriver(
		tmp_path,
		daemon,
		"""
		synth = useSpeechDispatcher()
		late = []
		for round in range(30):
			start = now()
			synth.speak(LONG)
			time.sleep(round * 0.002)
			cancelled = now()
			synth.cancel()
			nextAt = now()
			said = speakAndWait(["Next.", IndexCommand(1000 + round)])
			if said != [["index", 1000 + round], ["done", None]]:
				late.append(said)
		result = late
		""",
	)
	done.assertClean()
	assert done.value == []
