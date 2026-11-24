"""linda say: speaks one text through NVDA's whole speech path, waits until it has been heard,
and exits.

The text goes through speech.speakMessage, so through the speech manager, dictionaries, symbols,
the configured synthesizer's driver, nvwave, the wasapi replacement and the audio sink, exactly as
anything NVDA says does. Nothing else of NVDA starts: no wx application and no window, no X
connection, no session or accessibility bus, no input capture, no add-ons, no braille and no
single-instance lock, so it runs beside any screen reader already speaking, Linda included,
and takes nothing from it.

The user's configuration is never written: nvda.ini and speechDicts are copied into a temporary
directory, which NVDA is given as its configuration directory, and which is removed on every way out
but a fatal signal. The log, the crash dump and whatever a library writes to the standard output or
error at the C level go to a directory of their own under Linda's state directory, never
beside the launcher's log, whose last two runs NVDA would otherwise rotate away.

What it prints is read aloud by a screen reader, so nothing is printed until the end, and then one
line on the original standard error: what was spoken, through what, how soon the first audio came
and when speech finished, or, for a failure, one sentence saying what went wrong and naming the log.
The exit status says how it ended: see the EXIT_ constants.
"""

import argparse
import logging
import math
import os
import shutil
import signal
import sys
import tempfile
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, NoReturn, TextIO

if TYPE_CHECKING:
	from synthDriverHandler import SynthDriver

#: Spoken and played to the end.
EXIT_SPOKEN = 0
#: An unexpected error, or an ERROR record logged although speech was played.
EXIT_ERROR = 1
#: A wrong or missing argument, an unknown voice or a setting the synthesizer lacks.
EXIT_USAGE = 2
#: The synthesizer asked for did not load, or only silence did.
EXIT_NO_SYNTH = 3
#: The audio output could not be set up, is the null sink without --check, or nothing reached it.
EXIT_NO_AUDIO = 4
#: The synthesizer did not finish within the timeout.
EXIT_TIMEOUT = 5
#: Stopped by SIGINT, SIGTERM or SIGHUP.
EXIT_INTERRUPTED = 6

PROGRAM = "linda say"
INTERRUPTED_SENTENCE = "interrupted before speech finished."
#: The directory under Linda's state directory that holds this mode's log and output.
STATE_SUBDIRECTORY = "say"
LOG_FILE_NAME = "say.log"
#: Receives whatever is written to the standard output or error by file descriptor, by C code
#: included, once the mode has started.
OUTPUT_FILE_NAME = "output.log"
TEMPORARY_PREFIX = "linda-say-"

DEFAULT_TIMEOUT = 30.0
#: How often NVDA's queue is pumped while speech goes on, as core's pump does it.
_PUMP_INTERVAL = 0.005
#: How often the output is asked whether audio has reached it, until it has.
_FIRST_AUDIO_INTERVAL = 0.001
_BEEP_HZ = 440
_BEEP_MS = 100
_STOP_SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)

_HELP = """\
linda say speaks the text after its options through Linda's speech and audio, waits
until it has been heard, and exits.
It reads the synthesizer and its settings from your Linda configuration and never changes it.
--synth followed by a name chooses the synthesizer, such as espeak or openevv.
--voice followed by an identifier chooses the voice, as --list-voices names it.
--rate, --pitch and --volume, each followed by a whole number from 0 to 100, set the synthesizer's own settings.
--gain followed by a number from 0 to 1 scales every sound it makes, and is 1 when not given.
--check plays to an output that makes no sound, to check everything else.
--list-voices lists the voices of the synthesizer, one to a line, and speaks nothing.
--sounds plays Linda's start sound, a short beep and its error sound after the text.
--factory ignores your configuration, and --config-path followed by a directory reads that one instead.
--timeout followed by a number of seconds says how long to wait for speech to finish, 30 when not given.
It exits with 0 when the text was heard, 1 for an unexpected error, 2 for a mistake in what it was given,
3 when the synthesizer did not load, 4 when nothing could be heard, 5 when speech did not finish in time,
and 6 when stopped by Control C.
"""


class _UsageError(Exception):
	"""A mistake in the command line, said as the sentence to print."""


class _Outcome(Exception):
	"""Ends the run with an exit status and the sentence to print, or None to print nothing."""

	def __init__(self, status: int, sentence: str | None) -> None:
		super().__init__(sentence)
		self.status = status
		self.sentence = sentence


class _Interrupted(BaseException):
	"""Raised in the main thread by the first stop signal. A BaseException, as KeyboardInterrupt is,
	so that NVDA's handlers of Exception let it through."""


class _ArgumentParser(argparse.ArgumentParser):
	def error(self, message: str) -> NoReturn:
		# argparse would print its usage and exit; the sentence is printed with everything else, at the end.
		prefix, separator, detail = message.partition(": ")
		if prefix.startswith("argument ") and separator:
			option = prefix.removeprefix("argument ").split("/")[0]
			if detail == "expected one argument":
				raise _UsageError(f"{option} needs a value after it")
			if detail.startswith("not allowed with argument "):
				other = detail.removeprefix("not allowed with argument ")
				raise _UsageError(f"{option} and {other} cannot be given together")
			raise _UsageError(detail)
		raise _UsageError(f"its arguments could not be understood, as Python's parser says {message}")


def _wholeSetting(name: str) -> Callable[[str], int]:
	def parse(text: str) -> int:
		try:
			value = int(text)
		except ValueError:
			value = None
		if value is None or not 0 <= value <= 100:
			raise argparse.ArgumentTypeError(f"the {name} must be a whole number from 0 to 100, not {text}")
		return value

	return parse


def _gain(text: str) -> float:
	try:
		value = float(text)
	except ValueError:
		value = math.nan
	if not 0.0 <= value <= 1.0:
		raise argparse.ArgumentTypeError(f"the gain must be a number from 0 to 1, not {text}")
	return value


def _timeout(text: str) -> float:
	try:
		value = float(text)
	except ValueError:
		value = math.nan
	if not 0.0 < value < math.inf:
		raise argparse.ArgumentTypeError(f"the timeout must be a number of seconds above 0, not {text}")
	return value


def _parser() -> _ArgumentParser:
	parser = _ArgumentParser(prog=PROGRAM, add_help=False, allow_abbrev=False)
	parser.add_argument("--help", "-h", action="store_true")
	parser.add_argument("--synth")
	parser.add_argument("--voice")
	parser.add_argument("--rate", type=_wholeSetting("rate"))
	parser.add_argument("--pitch", type=_wholeSetting("pitch"))
	parser.add_argument("--volume", type=_wholeSetting("volume"))
	parser.add_argument("--gain", type=_gain, default=1.0)
	parser.add_argument("--check", action="store_true")
	parser.add_argument("--list-voices", action="store_true")
	parser.add_argument("--sounds", action="store_true")
	configuration = parser.add_mutually_exclusive_group()
	configuration.add_argument("--factory", action="store_true")
	configuration.add_argument("--config-path")
	parser.add_argument("--timeout", type=_timeout, default=DEFAULT_TIMEOUT)
	parser.add_argument("text", nargs="*")
	return parser


def parseArguments(arguments: list[str]) -> argparse.Namespace:
	""":raise _UsageError: With the sentence that says what is wrong."""
	options, unknown = _parser().parse_known_intermixed_args(arguments)
	if unknown:
		first = unknown[0]
		if first.startswith("-"):
			raise _UsageError(f"{first} is not one of its options, which {PROGRAM} --help lists")
		raise _UsageError(f"{first} could not be understood")
	if options.help:
		return options
	if options.text and not " ".join(options.text).strip():
		raise _UsageError("the text to speak is empty")
	if not options.text and not options.list_voices:
		raise _UsageError(f"it needs the text to speak after its options, as in {PROGRAM} Hello")
	if options.config_path is not None:
		if not os.path.isdir(options.config_path):
			raise _UsageError(f"the configuration directory {options.config_path} does not exist")
		if not os.path.isfile(os.path.join(options.config_path, "nvda.ini")):
			raise _UsageError(f"the configuration directory {options.config_path} holds no nvda.ini")
	return options


def _plural(number: int, unit: str) -> str:
	return f"{number} {unit}" if number == 1 else f"{number} {unit}s"


def spokenMilliseconds(seconds: float) -> str:
	return _plural(round(seconds * 1000), "millisecond")


def spokenSeconds(seconds: float) -> str:
	tenths = round(seconds * 10)
	if tenths % 10 == 0:
		return _plural(tenths // 10, "second")
	return f"{tenths / 10:.1f} seconds"


class _Run:
	"""One run of the say mode, and what has to be undone on the way out, however it goes."""

	def __init__(self) -> None:
		self.result: TextIO | None = None
		self.listing: TextIO | None = None
		self.temporaryDir: str | None = None
		self.logPath: str | None = None
		self.errors: list[str] = []
		self.isSpeechStarted = False
		self.interruptions = 0
		self.isEnding = False

	def installSignals(self) -> None:
		# The handlers are set whatever the dispositions were: a background job of a shell without job
		# control starts with SIGINT ignored, and Control C would then let speech play on to the end.
		# SIGHUP is left ignored when it is, as nohup asks.
		for signalNumber in _STOP_SIGNALS:
			if signalNumber == signal.SIGHUP and signal.getsignal(signalNumber) == signal.SIG_IGN:
				continue
			signal.signal(signalNumber, self.onStopSignal)

	def onStopSignal(self, signalNumber: int, frame: object) -> None:
		self.interruptions += 1
		if self.isEnding:
			return
		if self.interruptions == 1:
			raise _Interrupted
		# The first one did not end the run, as when it came while C code held the main thread or a
		# handler swallowed it: this one does, without waiting for anything.
		self.end(EXIT_INTERRUPTED, INTERRUPTED_SENTENCE)

	def keepOutputs(self) -> None:
		"""Keep the original standard output and error for the listing and the result line."""
		self.result = os.fdopen(os.dup(2), "w", encoding="utf-8", errors="replace", closefd=True)
		self.listing = os.fdopen(os.dup(1), "w", encoding="utf-8", errors="replace", closefd=True)

	def failure(self, status: int, what: str) -> _Outcome:
		if self.logPath:
			return _Outcome(status, f"{what}, and the log is {self.logPath}")
		return _Outcome(status, f"{what}.")

	def end(self, status: int, sentence: str | None) -> NoReturn:
		"""Remove the temporary directory, print the line and exit, whatever happens meanwhile."""
		self.isEnding = True
		try:
			signal.pthread_sigmask(signal.SIG_BLOCK, _STOP_SIGNALS)
			if self.temporaryDir:
				shutil.rmtree(self.temporaryDir, ignore_errors=True)
			for handler in logging.getLogger().handlers:
				handler.flush()
			if sentence is not None and self.result is not None:
				self.result.write(f"{PROGRAM}: {sentence}\n")
				self.result.flush()
		except BaseException:  # noqa: BLE001, S110
			# Nothing is left to report to: the standard error is where reports go, and it is what failed.
			pass
		finally:
			# Not sys.exit: the interpreter's teardown runs NVDA's finalisers, some of which fail and print,
			# and joins threads a synthesizer may still hold.
			os._exit(status)


def main(arguments: list[str]) -> NoReturn:
	"""Run the say mode with its command-line arguments, those after the word say, and exit.

	The launcher hands over to this before NVDA's own argument parser sees anything, once
	linuxPlatform.bootstrap.install() has run. It never returns: every way through it, a mistake in
	the arguments included, ends the process with an exit status of its own, so nothing can go on to
	start the screen reader.
	"""
	run = _Run()
	status, sentence = EXIT_ERROR, "it stopped before it could say why."
	try:
		run.installSignals()
		run.keepOutputs()
		status, sentence = _speak(run, arguments)
	except _Interrupted:
		_silence(run)
		status, sentence = EXIT_INTERRUPTED, INTERRUPTED_SENTENCE
	except _Outcome as outcome:
		status, sentence = outcome.status, outcome.sentence
	except BaseException as e:  # noqa: BLE001
		if run.interruptions:
			_silence(run)
			status, sentence = EXIT_INTERRUPTED, INTERRUPTED_SENTENCE
		else:
			_logException()
			outcome = run.failure(EXIT_ERROR, f"it stopped on an unexpected error, {type(e).__name__}: {e}")
			status, sentence = outcome.status, outcome.sentence
	finally:
		run.end(status, sentence)


def _logException() -> None:
	try:
		from logHandler import log

		log.exception("say mode failed")
	except BaseException:  # noqa: BLE001, S110
		# The log may not be open yet, and the line printed at the end says what happened anyway.
		pass


def _silence(run: _Run) -> None:
	"""Stop speech and NVDA's sounds at once, so that the sink falls silent within a device period."""
	if not run.isSpeechStarted:
		return
	try:
		import nvwave
		import speech
		import tones

		speech.cancelSpeech()
		for player in (tones.player, nvwave.fileWavePlayer):
			if player is not None:
				player.stop()
	except BaseException:  # noqa: BLE001
		_logException()


class _ErrorRecords(logging.Handler):
	"""Keeps the first line of every record at ERROR or above, from any logger."""

	def __init__(self, into: list[str]) -> None:
		super().__init__(level=logging.ERROR)
		self.into = into

	def emit(self, record: logging.LogRecord) -> None:
		try:
			self.into.append(record.getMessage().splitlines()[0] if record.getMessage() else record.levelname)
		except Exception:  # noqa: BLE001
			self.into.append(record.levelname)


class _Done:
	"""Whether the synthesizer has reported the end of what it was given."""

	def __init__(self) -> None:
		self.isDone = False

	def onDoneSpeaking(self, synth: object = None, **kwargs: object) -> None:
		self.isDone = True


def _check(run: _Run) -> None:
	# A stop signal that NVDA code swallowed on its way still ends the run.
	if run.interruptions:
		raise _Interrupted


def _captureOutput(path: str) -> None:
	"""Send whatever is written to file descriptors 1 and 2 from now on to path."""
	for stream in (sys.stdout, sys.stderr):
		if stream is not None:
			stream.flush()
	fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_CLOEXEC, 0o600)
	try:
		os.dup2(fd, 1)
		os.dup2(fd, 2)
	finally:
		os.close(fd)


def _copyConfiguration(source: str, destination: str) -> None:
	"""Copy what the say mode reads of a configuration directory: nvda.ini and the speech dictionaries."""
	if os.path.isfile(os.path.join(source, "nvda.ini")):
		shutil.copyfile(os.path.join(source, "nvda.ini"), os.path.join(destination, "nvda.ini"))
	if os.path.isdir(os.path.join(source, "speechDicts")):
		shutil.copytree(os.path.join(source, "speechDicts"), os.path.join(destination, "speechDicts"))


def _applySettings(run: _Run, synth: "SynthDriver", options: argparse.Namespace) -> None:
	import synthDriverHandler

	if options.voice is not None:
		if not synth.isSupported("voice") or options.voice not in synth.availableVoices:
			raise run.failure(
				EXIT_USAGE,
				f"{synth.name} has no voice {options.voice}, and {PROGRAM} --synth {synth.name} --list-voices "
				"lists those it has",
			)
		synthDriverHandler.changeVoice(synth, options.voice)
	for name in ("rate", "pitch", "volume"):
		value = getattr(options, name)
		if value is None:
			continue
		if not synth.isSupported(name):
			raise run.failure(EXIT_USAGE, f"{synth.name} has no {name} setting")
		setattr(synth, name, value)


def _playSounds(appDir: str) -> None:
	import nvwave
	import tones

	nvwave.playWaveFile(os.path.join(appDir, "waves", "start.wav"), asynchronous=False)
	tones.beep(_BEEP_HZ, _BEEP_MS)
	if tones.player is not None:
		tones.player.idle()
	nvwave.playWaveFile(os.path.join(appDir, "waves", "error.wav"), asynchronous=False)


def _terminate() -> None:
	import nvwave
	import speech
	import tones

	speech.terminate()
	tones.terminate()
	nvwave.terminate()


def _outputName(kind: str) -> str:
	if kind == "pipewire":
		return "PipeWire"
	if kind == "pulse":
		return "PulseAudio"
	return f"the {kind} sink"


def _outputPhrase(kind: str) -> str:
	if kind == "pipewire":
		return "through PipeWire"
	if kind == "pulse":
		return "through PulseAudio"
	return f"into the {kind} sink"


def _speak(run: _Run, arguments: list[str]) -> tuple[int, str | None]:
	try:
		options = parseArguments(arguments)
	except _UsageError as e:
		return EXIT_USAGE, f"{e}."
	if options.help:
		run.listing.write(_HELP)
		run.listing.flush()
		return EXIT_SPOKEN, None
	# Nothing is heard with these, so that a preflight, or a look at the voices, can make no sound.
	isSilent = options.check or options.list_voices

	from . import logSupport, xdg

	sayDir = os.path.join(logSupport.stateDirectory(), STATE_SUBDIRECTORY)
	os.makedirs(sayDir, mode=0o700, exist_ok=True)
	_captureOutput(os.path.join(sayDir, OUTPUT_FILE_NAME))
	logPath = os.path.join(sayDir, LOG_FILE_NAME)

	import globalVars
	import NVDAState
	from argsParsing import getParser

	appDir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
	os.chdir(appDir)
	globalVars.appDir = appDir
	globalVars.appPid = os.getpid()
	import config

	config.isAppX = False
	NVDAState._initializeStartTime()

	run.temporaryDir = tempfile.mkdtemp(prefix=TEMPORARY_PREFIX)
	if not options.factory:
		_copyConfiguration(options.config_path or xdg.readableUserConfigDir(), run.temporaryDir)
	# -m and -d keep out the start and exit sounds and add-ons, as they would in a full start.
	globalVars.appArgs, globalVars.unknownAppArgs = getParser().parse_known_args(
		["-m", "-d", "--no-sr-flag", "-c", run.temporaryDir, "-f", logPath, "--debug-logging"],
	)
	from . import synthDriverPaths, testHooks

	testHooks.install()
	synthDriverPaths.install()

	import logHandler
	from config.configFlags import PlayErrorSound
	from logHandler import log

	logHandler.initialize()
	run.logPath = globalVars.appArgs.logFileName
	logging.getLogger().addHandler(_ErrorRecords(run.errors))
	log.info(f"Say mode, with the arguments {arguments}")
	config.initConfigPath()
	config.initialize()
	# An error is reported in the line printed at the end; the error sound would play over the speech.
	config.conf["featureFlag"]["playErrorSound"] = PlayErrorSound.NO.value
	_check(run)

	import languageHandler

	languageHandler.setLanguage(config.conf["general"]["language"])
	import NVDAHelper

	NVDAHelper.initialize()
	_check(run)

	from .audioOutput import SINK_VARIABLE, getSink

	if isSilent:
		# Chosen through the variable, so that no other sink's module, nor any library of one, is loaded.
		os.environ[SINK_VARIABLE] = "null"
	try:
		sink = getSink()
	except Exception as e:
		log.exception("The audio output could not be set up")
		raise run.failure(EXIT_NO_AUDIO, f"the audio output could not be set up, {e}") from None
	if sink.kind == "null" and not isSilent:
		raise run.failure(
			EXIT_NO_AUDIO,
			"the audio output is the null sink, which nobody hears, and only --check plays to it",
		)
	sink.setGain(options.gain)

	import characterProcessing
	import nvwave
	import speech
	import speechDictHandler
	import synthDriverHandler
	import tones

	nvwave.initialize()
	logHandler.getOnErrorSoundRequested().unregister(nvwave.playErrorSound)
	tones.initialize()
	speechDictHandler.initialize()
	characterProcessing.initialize()
	if options.synth:
		config.conf["speech"]["synth"] = options.synth
	wanted = config.conf["speech"]["synth"]
	run.isSpeechStarted = True
	speech.initialize()
	_check(run)
	synth = synthDriverHandler.getSynth()
	# A fallback is not what was asked for, and silence is no synthesizer at all.
	if synth is None or synth.name == "silence" or (wanted != "auto" and synth.name != wanted):
		raise run.failure(EXIT_NO_SYNTH, f"the synthesizer {wanted} did not load")

	if options.list_voices:
		for voice in synth.availableVoices.values():
			run.listing.write(f"{voice.id}, {voice.displayName}\n")
		run.listing.flush()
		_terminate()
		return EXIT_SPOKEN, None
	_applySettings(run, synth, options)

	done = _Done()
	synthDriverHandler.synthDoneSpeaking.register(done.onDoneSpeaking)
	import queueHandler

	asked = time.monotonic()
	speech.speakMessage(" ".join(options.text))
	firstAudio = None
	deadline = asked + options.timeout
	while True:
		queueHandler.pumpAll()
		now = time.monotonic()
		if firstAudio is None and not isSilent and sink.deliveredFrames() > 0:
			firstAudio = now - asked
		if done.isDone and not speech.isSpeaking():
			break
		if now >= deadline:
			_silence(run)
			raise run.failure(
				EXIT_TIMEOUT,
				f"the synthesizer {synth.name} did not finish within {spokenSeconds(options.timeout)}",
			)
		time.sleep(_FIRST_AUDIO_INTERVAL if firstAudio is None and not isSilent else _PUMP_INTERVAL)
	finished = time.monotonic() - asked
	if options.sounds:
		_playSounds(globalVars.appDir)
	if not isSilent and sink.deliveredFrames() == 0:
		raise run.failure(
			EXIT_NO_AUDIO, f"nothing reached {_outputName(sink.kind)}, which may not be running"
		)
	spoke = f"spoke with {synth.name}, voice {synth.voice}, "
	_terminate()

	if isSilent:
		spoke += "into the null sink, which plays nothing"
		said = f"{spoke}. Speech finished after {spokenSeconds(finished)}."
	else:
		spoke += _outputPhrase(sink.kind)
		said = (
			f"{spoke}. The first audio came {spokenMilliseconds(firstAudio)} after asking, "
			f"and speech finished after {spokenSeconds(finished)}."
		)
	log.info(said)
	if run.errors:
		raise run.failure(EXIT_ERROR, f"{spoke}, but Linda logged an error")
	return EXIT_SPOKEN, said
