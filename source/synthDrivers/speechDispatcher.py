"""Speech Dispatcher, the speech service of Linux desktops, as a synthesizer.

The driver speaks SSIP, Speech Dispatcher's text protocol, on the socket of the user's own daemon,
so that every output module and voice their distribution has set up can be chosen, and the sound
comes out of the daemon's own audio output. It is written against the protocol rather than libspeechd
or the speechd Python module because both start a missing daemon from the path they were built with,
which in a Nix build is a daemon from the Nix store with a stock configuration and none of the
distribution's voices; here a missing daemon is started as the distribution starts it, from
speech-dispatcher on the user's PATH.

Each output module's voices are offered together as the voice setting, named with their module, and
a voice's variants, such as eSpeak NG's, as the variant setting. NVDA's settings panel builds each
list once and rebuilds none when another setting changes, so a module setting of its own would leave
the voice list showing the previous module's voices.

Only this connection's own parameters are ever set, with SET SELF. Punctuation, capital letters and
spelling are switched off for the connection, because NVDA has already turned symbols into words and
marks capitals itself.

An NVDA sequence becomes one or more messages, one for each stretch with the same voice, language,
rate, pitch and volume, since those are set between messages, and one for each character spoken in
character mode, which goes as a CHAR command. Index marks go inside the messages as SSML marks.
Modules that report no marks, such as pico and flite, still report the end of each message, and every
mark of a message not reported by then is reported at its end. A module found to report none has its
speech split at each mark from then on, so that each mark is reported as the speech before it ends.

Pausing is done here rather than by the daemon, whose PAUSE stops only at its next mark, goes on
reporting marks while paused and can let speech queued after it play on: a pause cancels what is
speaking and keeps it, and a resume speaks it again from the last mark reported.
"""

import getpass
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from xml.sax.saxutils import escape

from logHandler import log
from speech.commands import (
	BaseProsodyCommand,
	BreakCommand,
	CharacterModeCommand,
	IndexCommand,
	LangChangeCommand,
	PitchCommand,
	RateCommand,
	VolumeCommand,
)
from speech.types import SpeechSequence
from synthDriverHandler import SynthDriver as BaseSynthDriver
from synthDriverHandler import VoiceInfo, synthDoneSpeaking, synthIndexReached

#: Speech Dispatcher's port when SPEECHD_ADDRESS names an inet_socket without one.
DEFAULT_PORT = 6560
#: How long a connection may take, and a reply. Listing eSpeak NG's fifteen thousand voices takes
#: about half a second.
CONNECT_TIMEOUT = 2.0
REPLY_TIMEOUT = 10.0
#: How long a daemon started here may take to answer on its socket. One loading every module it has
#: takes about two seconds.
SPAWN_TIMEOUT = 15.0
#: After a failed attempt to reach the daemon, how long speech is dropped before the next attempt.
RETRY_INTERVAL = 1.0

#: A variable of this process that would make the distribution's daemon, or a module it runs, load
#: code from Linda's Nix closure instead of its own.
SPAWN_ENVIRONMENT_CLEARED = (
	"LD_LIBRARY_PATH",
	"LD_PRELOAD",
	"LD_AUDIT",
	"PYTHONPATH",
	"PYTHONHOME",
	"PYTHONNOUSERSITE",
	"GI_TYPELIB_PATH",
	"GIO_EXTRA_MODULES",
	"GDK_PIXBUF_MODULE_FILE",
	"GTK_PATH",
	"GTK_MODULES",
	"GTK3_MODULES",
	"GST_PLUGIN_PATH",
	"GST_PLUGIN_SYSTEM_PATH",
	"GST_PLUGIN_SYSTEM_PATH_1_0",
)
SPAWN_ENVIRONMENT_CLEARED_PREFIXES = ("NIX_PYTHON",)

#: Characters XML does not allow, and line breaks, which would end an SSIP data line.
_unspeakable = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\r\n\ud800-\udfff￾￿]")
#: A client name part may hold only these.
_clientNameUnsafe = re.compile(r"[^A-Za-z0-9_-]")


def daemonAddress() -> tuple[int, str | tuple[str, int]]:
	"""The address libspeechd would connect to, as a socket family and address: SPEECHD_ADDRESS when it
	is set, and otherwise speech-dispatcher/speechd.sock in the user's runtime directory."""
	address = os.environ.get("SPEECHD_ADDRESS", "").strip()
	method, _sep, rest = address.partition(":")
	if method == "inet_socket":
		host, _sep, port = rest.partition(":")
		return socket.AF_INET, (host or "127.0.0.1", int(port) if port.isdigit() else DEFAULT_PORT)
	if method == "unix_socket" and rest:
		return socket.AF_UNIX, rest
	if address and method != "unix_socket":
		log.warning(f"SPEECHD_ADDRESS is {address!r}, which is neither unix_socket nor inet_socket")
	runtime = os.environ.get("XDG_RUNTIME_DIR", "")
	if not os.path.isabs(runtime):
		# GLib's fallback for the runtime directory, which libspeechd uses.
		runtime = os.environ.get("XDG_CACHE_HOME", "") or os.path.expanduser("~/.cache")
	return socket.AF_UNIX, os.path.join(runtime, "speech-dispatcher", "speechd.sock")


def spawnEnvironment() -> dict[str, str]:
	return {
		name: value
		for name, value in os.environ.items()
		if name not in SPAWN_ENVIRONMENT_CLEARED and not name.startswith(SPAWN_ENVIRONMENT_CLEARED_PREFIXES)
	}


def spawnDaemon(socketPath: str) -> bool:
	"""Start the distribution's daemon as libspeechd's autospawn does, and say whether it was started.

	--spawn leaves the decision to the daemon's configuration, which can forbid autospawn.
	"""
	executable = shutil.which("speech-dispatcher")
	if not executable:
		log.debug("No speech-dispatcher on PATH to start")
		return False
	command = [
		executable,
		"--spawn",
		"--communication-method",
		"unix_socket",
		"--socket-path",
		socketPath,
	]
	log.info(f"Starting Speech Dispatcher: {' '.join(command)}")
	try:
		done = subprocess.run(
			command,
			env=spawnEnvironment(),
			stdin=subprocess.DEVNULL,
			stdout=subprocess.DEVNULL,
			stderr=subprocess.PIPE,
			timeout=SPAWN_TIMEOUT,
			# Its own session, so that a signal to Linda's process group is not one to the daemon.
			start_new_session=True,
			check=False,
		)
	except (OSError, subprocess.TimeoutExpired):
		log.warning("Could not start Speech Dispatcher", exc_info=True)
		return False
	if done.returncode != 0:
		log.warning(
			f"speech-dispatcher --spawn exited with status {done.returncode}: "
			f"{done.stderr.decode('utf-8', 'replace').strip()}",
		)
		return False
	return True


def fromPercent(value: int) -> int:
	"""NVDA's 0 to 100 as Speech Dispatcher's -100 to 100, 50 being the module's own default."""
	return max(-100, min(100, round(value * 2 - 100)))


def primaryLanguage(language: str | None) -> str:
	return (language or "").replace("_", "-").split("-")[0].lower()


class ConnectionLost(OSError):
	pass


class _Waiter:
	__slots__ = ("done", "lines", "onReply")

	def __init__(self, onReply: Callable[[list[str]], None] | None):
		self.done = threading.Event()
		self.lines: list[str] | None = None
		self.onReply = onReply


class _Connection:
	"""One SSIP connection. Commands are answered in the order sent, one exchange at a time, and the
	daemon's events go to onEvent on the reader thread, as the reply before them has been handled."""

	def __init__(
		self,
		family: int,
		address: str | tuple[str, int],
		onEvent: Callable[["_Connection", list[str]], None],
		onLost: Callable[["_Connection"], None],
	):
		self._socket = socket.socket(family, socket.SOCK_STREAM)
		try:
			self._socket.settimeout(CONNECT_TIMEOUT)
			self._socket.connect(address)
			self._socket.settimeout(None)
		except OSError:
			self._socket.close()
			raise
		self._onEvent = onEvent
		self._onLost = onLost
		self._exchange = threading.Lock()
		self._waiter: _Waiter | None = None
		self._waiterLock = threading.Lock()
		self.closed = False
		#: The parameters this connection was last set to, by SSIP name; a name missing is unknown.
		self.parameters: dict[str, str] = {}
		self._reader = threading.Thread(target=self._read, name="speechDispatcher reader", daemon=True)
		self._reader.start()

	def _read(self) -> None:
		lines: list[str] = []
		try:
			with self._socket.makefile("rb") as stream:
				for raw in stream:
					line = raw.decode("utf-8", "replace").rstrip("\r\n")
					lines.append(line)
					# A reply ends with the line whose code is followed by a space rather than a hyphen.
					if len(line) < 4 or line[3] != " ":
						continue
					reply, lines = lines, []
					if reply[-1].startswith("7"):
						self._onEvent(self, reply)
						continue
					with self._waiterLock:
						waiter, self._waiter = self._waiter, None
					if waiter is None:
						log.debugWarning(f"Speech Dispatcher replied when nothing was asked: {reply}")
						continue
					if waiter.onReply:
						waiter.onReply(reply)
					waiter.lines = reply
					waiter.done.set()
		except OSError:
			pass
		except Exception:
			log.exception("Reading from Speech Dispatcher")
		self._lose()

	def _lose(self) -> None:
		with self._waiterLock:
			if self.closed:
				return
			self.closed = True
			waiter, self._waiter = self._waiter, None
		if waiter:
			waiter.done.set()
		try:
			self._socket.shutdown(socket.SHUT_RDWR)
		except OSError:
			pass
		self._socket.close()
		self._onLost(self)

	def close(self) -> None:
		# A goodbye only when no exchange is waiting on a daemon that may have stopped answering.
		if not self.closed and self._exchange.acquire(timeout=1.0):
			try:
				self._send("QUIT\r\n", None, timeout=1.0)
			except OSError:
				pass
			finally:
				self._exchange.release()
		self._lose()

	def _send(
		self,
		data: str,
		onReply: Callable[[list[str]], None] | None,
		*,
		timeout: float = REPLY_TIMEOUT,
	) -> list[str]:
		waiter = _Waiter(onReply)
		with self._waiterLock:
			if self.closed:
				raise ConnectionLost("the connection to Speech Dispatcher is closed")
			self._waiter = waiter
		try:
			self._socket.sendall(data.encode("utf-8"))
		except OSError:
			self._lose()
			raise
		if not waiter.done.wait(timeout):
			log.warning(f"Speech Dispatcher did not answer {data.splitlines()[0]!r} within {timeout} s")
			self._lose()
		if waiter.lines is None:
			raise ConnectionLost("the connection to Speech Dispatcher was lost")
		return waiter.lines

	def command(self, line: str, onReply: Callable[[list[str]], None] | None = None) -> list[str]:
		with self._exchange:
			return self._send(line + "\r\n", onReply)

	def speak(self, data: str, onReply: Callable[[list[str]], None]) -> list[str]:
		with self._exchange:
			reply = self._send("SPEAK\r\n", None)
			if not reply[-1].startswith("230"):
				return reply
			# Data never holds a line break, so no line of it can be the terminating dot.
			return self._send(data + "\r\n.\r\n", onReply)

	def checked(self, line: str) -> list[str]:
		"""The lines of a successful reply, without their codes or the last one."""
		reply = self.command(line)
		if not reply[-1].startswith("2"):
			raise RuntimeError(f"Speech Dispatcher answered {line!r} with {reply[-1]!r}")
		return [item[4:] for item in reply[:-1]]


class _Message:
	"""What goes to the daemon as one message: its parameters and its items, each one of
	("text", str), ("spell", str), ("char", str), ("break", int) or ("mark", int)."""

	__slots__ = ("items", "markedByDaemon", "msgId", "parameters", "reported")

	def __init__(self, parameters: dict[str, str]):
		self.parameters = parameters
		self.items: list[tuple[str, object]] = []
		#: How many of its marks have been reported.
		self.reported = 0
		self.msgId: str | None = None
		#: Whether the daemon has reported any of its marks itself.
		self.markedByDaemon = False

	@property
	def marks(self) -> list[int]:
		return [value for kind, value in self.items if kind == "mark"]

	@property
	def char(self) -> str | None:
		return next((value for kind, value in self.items if kind == "char"), None)

	def hasSound(self) -> bool:
		return any(kind != "mark" for kind, _value in self.items)

	def hasMarkInsideSound(self) -> bool:
		"""Whether a mark of it has sound before and after it, so that a module reporting marks would
		have reported it before the message ended. Leading marks are reported at the beginning anyway."""
		kinds = [kind for kind, _value in self.items][self.leadingMarks() :]
		if "mark" not in kinds:
			return False
		return any(kind != "mark" for kind in kinds[kinds.index("mark") :])

	def leadingMarks(self) -> int:
		count = 0
		for kind, _value in self.items:
			if kind != "mark":
				break
			count += 1
		return count

	def dropReported(self) -> None:
		"""Keep only what follows the last mark reported, for speaking again after a pause."""
		seen = 0
		for position, (kind, _value) in enumerate(self.items):
			if seen == self.reported:
				self.items = self.items[position:]
				break
			if kind == "mark":
				seen += 1
		else:
			self.items = []
		self.reported = 0

	def ssml(self) -> str:
		parts = ["<speak>"]
		for kind, value in self.items:
			if kind == "text":
				parts.append(escape(value))
			elif kind == "spell":
				parts.append(f'<say-as interpret-as="characters">{escape(value)}</say-as>')
			elif kind == "break":
				parts.append(f'<break time="{value}ms"/>')
			elif kind == "mark":
				parts.append(f'<mark name="{value}"/>')
		parts.append("</speak>")
		return "".join(parts)


#: The job that cancels what the daemon is speaking for this connection.
_CANCEL = object()


class SynthDriver(BaseSynthDriver):
	name = "speechDispatcher"
	description = "Speech Dispatcher"

	supportedSettings = (
		BaseSynthDriver.VoiceSetting(),
		BaseSynthDriver.VariantSetting(),
		BaseSynthDriver.RateSetting(),
		BaseSynthDriver.PitchSetting(),
		BaseSynthDriver.VolumeSetting(),
	)
	supportedCommands = frozenset(
		{
			IndexCommand,
			CharacterModeCommand,
			LangChangeCommand,
			BreakCommand,
			PitchCommand,
			RateCommand,
			VolumeCommand,
		},
	)
	supportedNotifications = frozenset({synthIndexReached, synthDoneSpeaking})

	@classmethod
	def check(cls) -> bool:
		if sys.platform == "win32":
			return False
		family, address = daemonAddress()
		try:
			with socket.socket(family, socket.SOCK_STREAM) as probe:
				probe.settimeout(CONNECT_TIMEOUT)
				probe.connect(address)
			return True
		except OSError:
			pass
		# A daemon not running yet is started when the driver loads, as any client of libspeechd would.
		return family == socket.AF_UNIX and shutil.which("speech-dispatcher") is not None

	def __init__(self) -> None:
		super().__init__()
		# Held only for short changes of state and never while waiting on the daemon. Reentrant, so
		# that a handler of a notification may call cancel on the same thread.
		self._lock = threading.RLock()
		self._wake = threading.Condition(self._lock)
		#: Messages given to the daemon or still to be, in order, until each ends or is cancelled.
		self._outstanding: deque[_Message] = deque()
		self._byId: dict[str, _Message] = {}
		self._jobs: deque = deque()
		#: Changed by every cancel and pause, so that a job or reply from before one is not acted on.
		self._epoch = 0
		self._paused = False
		self._terminating = False
		self._sentSinceCancel = False
		self._lastFailure = 0.0
		self._connection: _Connection | None = None
		self._rate = 50
		self._pitch = 50
		self._volume = 100
		self._variant = "none"
		#: Output modules found to report no marks, such as pico and flite. Their speech is split into a
		#: message for each mark, so that each is reported as the speech before it ends rather than all
		#: at the end, which say all and the cursor following speech would otherwise wait for.
		self._modulesWithoutMarks: set[str] = set()
		self._connection = self._connect()
		try:
			self._loadVoices(self._connection)
		except Exception:
			self._connection.close()
			raise
		self._voice = self._defaultVoice()
		self._worker = threading.Thread(target=self._run, name="speechDispatcher", daemon=True)
		self._worker.start()

	def _connect(self) -> _Connection:
		family, address = daemonAddress()
		try:
			connection = _Connection(family, address, self._onEvent, self._onLost)
		except OSError:
			if family != socket.AF_UNIX or not spawnDaemon(address):
				raise
			deadline = time.monotonic() + SPAWN_TIMEOUT
			while True:
				try:
					connection = _Connection(family, address, self._onEvent, self._onLost)
					break
				except OSError:
					if time.monotonic() > deadline:
						raise
					time.sleep(0.05)
		try:
			user = _clientNameUnsafe.sub("_", getpass.getuser()) or "user"
		except (KeyError, OSError):
			user = "user"
		try:
			for line in (
				f"SET SELF CLIENT_NAME {user}:linda:main",
				"SET SELF NOTIFICATION ALL on",
				# Queued in order, neither interrupting itself nor being dropped for a later message.
				"SET SELF PRIORITY message",
				"SET SELF SSML_MODE on",
				"SET SELF PUNCTUATION none",
				"SET SELF CAP_LET_RECOGN none",
				"SET SELF SPELLING off",
			):
				connection.checked(line)
		except Exception:
			connection.close()
			raise
		log.debug(f"Connected to Speech Dispatcher at {address}")
		return connection

	def _loadVoices(self, connection: _Connection) -> None:
		# What the daemon's configuration chooses for a client that sets neither.
		self._defaultModule = self._get(connection, "OUTPUT_MODULE")
		self._defaultLanguage = self._get(connection, "LANGUAGE") or "en"
		voices: OrderedDict[str, VoiceInfo] = OrderedDict()
		#: For each voice, its name for SET SELF SYNTHESIS_VOICE, or None for the module's own choice.
		self._voiceNames: dict[str, str | None] = {}
		self._voiceVariants: dict[str, set[str]] = {}
		variants: set[str] = set()
		for module in connection.checked("LIST OUTPUT_MODULES"):
			connection.checked(f"SET SELF OUTPUT_MODULE {module}")
			reply = connection.command("LIST SYNTHESIS_VOICES")
			entries = []
			for line in reply[:-1] if reply[-1].startswith("2") else ():
				name, _sep, rest = line[4:].partition("\t")
				language, _sep, variant = rest.partition("\t")
				entries.append((name, language or None, variant or "none"))
			names = {name for name, _language, _variant in entries}
			for name, language, variant in entries:
				base = name.removesuffix(f"+{variant}")
				if variant != "none" and base != name and base in names:
					self._voiceVariants.setdefault(f"{module}/{base}", set()).add(variant)
					variants.add(variant)
					continue
				voiceId = f"{module}/{name}"
				voices[voiceId] = VoiceInfo(
					voiceId,
					f"{name}, {module}",
					language.replace("-", "_") if language else None,
				)
				self._voiceNames[voiceId] = name
			if not entries:
				voiceId = f"{module}/"
				voices[voiceId] = VoiceInfo(voiceId, module, None)
				self._voiceNames[voiceId] = None
		connection.parameters.clear()
		if not voices:
			raise RuntimeError("Speech Dispatcher has no output module")
		self._availableVoices = voices
		self._availableVariants = OrderedDict(
			(variant, VoiceInfo(variant, variant)) for variant in ["none", *sorted(variants, key=str.lower)]
		)

	@staticmethod
	def _get(connection: _Connection, name: str) -> str | None:
		reply = connection.command(f"GET {name}")
		return reply[0][4:] if reply[-1].startswith("2") and len(reply) > 1 else None

	def _defaultVoice(self) -> str:
		"""The default module's voice for NVDA's language, or for the daemon's, or its first."""
		import languageHandler

		module = [v for v in self._availableVoices if v.partition("/")[0] == self._defaultModule]
		candidates = module or list(self._availableVoices)
		for wanted in (languageHandler.getLanguage(), self._defaultLanguage):
			if not wanted:
				continue
			for exact in (True, False):
				for voiceId in candidates:
					language = self._availableVoices[voiceId].language
					if not language:
						continue
					if (
						language.lower() == wanted.replace("-", "_").lower()
						if exact
						else primaryLanguage(language) == primaryLanguage(wanted)
					):
						return voiceId
		return candidates[0]

	def _getAvailableVoices(self) -> OrderedDict[str, VoiceInfo]:
		return self._availableVoices

	def _getAvailableVariants(self) -> OrderedDict[str, VoiceInfo]:
		return self._availableVariants

	def _get_voice(self) -> str:
		return self._voice

	def _set_voice(self, value: str) -> None:
		if value not in self._availableVoices:
			raise LookupError(value)
		self._voice = value

	def _get_variant(self) -> str:
		return self._variant

	def _set_variant(self, value: str) -> None:
		self._variant = value if value in self._availableVariants else "none"

	def _get_rate(self) -> int:
		return self._rate

	def _set_rate(self, value: int) -> None:
		self._rate = value

	def _get_pitch(self) -> int:
		return self._pitch

	def _set_pitch(self, value: int) -> None:
		self._pitch = value

	def _get_volume(self) -> int:
		return self._volume

	def _set_volume(self, value: int) -> None:
		self._volume = value

	def _voiceParameters(self, voiceId: str) -> dict[str, str]:
		module = voiceId.partition("/")[0]
		parameters = {"OUTPUT_MODULE": module}
		language = self._availableVoices[voiceId].language
		if language:
			parameters["LANGUAGE"] = language.replace("_", "-")
		name = self._voiceNames[voiceId]
		if name is not None:
			if self._variant in self._voiceVariants.get(voiceId, ()):
				name = f"{name}+{self._variant}"
			parameters["SYNTHESIS_VOICE"] = name
		return parameters

	def _languageParameters(self, language: str | None) -> dict[str, str]:
		"""The voice of the current module that speaks a language NVDA switches to: the current voice
		for its own language or one with no voice for it, and otherwise the first that speaks it
		exactly, or else its primary language."""
		if not language or primaryLanguage(language) == primaryLanguage(self.language):
			return self._voiceParameters(self._voice)
		module = self._voice.partition("/")[0]
		wanted = language.replace("-", "_").lower()
		candidates = [
			v
			for v in self._availableVoices
			if v.partition("/")[0] == module and self._availableVoices[v].language
		]
		for match in (
			lambda v: self._availableVoices[v].language.lower() == wanted,
			lambda v: primaryLanguage(self._availableVoices[v].language) == primaryLanguage(language),
		):
			for voiceId in candidates:
				if match(voiceId):
					return self._voiceParameters(voiceId)
		return self._voiceParameters(self._voice)

	def _messages(self, speechSequence: SpeechSequence) -> list[_Message]:
		voice = self._voiceParameters(self._voice)
		prosody = {
			"RATE": str(fromPercent(self._rate)),
			"PITCH": str(fromPercent(self._pitch)),
			"VOLUME": str(fromPercent(self._volume)),
		}
		base = {**voice, **prosody}
		parameters = dict(base)
		charMode = False
		messages: list[_Message] = []

		def add(kind: str, value: object) -> None:
			"""Add an item to the last message, or to a new one when it cannot share the last: when the
			parameters differ, or either is a character, which is a command of its own."""
			last = messages[-1] if messages else None
			if last is None:
				last = _Message(dict(parameters))
				messages.append(last)
			elif kind != "mark" and not last.hasSound():
				# Marks alone so far, which are reached as the sound after them begins.
				last.parameters = dict(parameters)
			elif kind != "mark" and (
				kind == "char"
				or last.char is not None
				or last.parameters != parameters
				or (last.items[-1][0] == "mark" and parameters["OUTPUT_MODULE"] in self._modulesWithoutMarks)
			):
				last = _Message(dict(parameters))
				messages.append(last)
			last.items.append((kind, value))

		for item in speechSequence:
			if isinstance(item, str):
				text = _unspeakable.sub(" ", item)
				if charMode and len(text) == 1:
					add("char", text)
				elif charMode and text.strip():
					add("spell", text.strip())
				elif text:
					add("text", text)
			elif isinstance(item, IndexCommand):
				add("mark", item.index)
			elif isinstance(item, CharacterModeCommand):
				charMode = item.state
			elif isinstance(item, BreakCommand):
				add("break", max(0, int(item.time)))
			elif isinstance(item, LangChangeCommand):
				for name in ("OUTPUT_MODULE", "LANGUAGE", "SYNTHESIS_VOICE"):
					parameters.pop(name, None)
				parameters.update(self._languageParameters(item.lang))
			elif isinstance(item, BaseProsodyCommand) and type(item) in _PROSODY:
				name = _PROSODY[type(item)]
				if item.isDefault:
					parameters[name] = base[name]
				else:
					parameters[name] = str(fromPercent(max(0, min(100, item.newValue))))
			else:
				log.debugWarning(f"Unsupported speech command: {item!r}")
		if not messages:
			messages.append(_Message(dict(parameters)))
		return messages

	def speak(self, speechSequence: SpeechSequence) -> None:
		messages = self._messages(speechSequence)
		with self._lock:
			self._outstanding.extend(messages)
			if not self._paused:
				self._jobs.extend((self._epoch, message) for message in messages)
				self._wake.notify()

	def cancel(self) -> None:
		with self._lock:
			self._epoch += 1
			self._outstanding.clear()
			self._byId.clear()
			self._jobs.clear()
			# NVDA ends a pause by cancelling, without calling pause(False).
			self._paused = False
			if self._sentSinceCancel:
				self._jobs.append(_CANCEL)
				self._wake.notify()

	def pause(self, switch: bool) -> None:
		with self._lock:
			if switch == self._paused:
				return
			self._paused = switch
			self._epoch += 1
			self._jobs.clear()
			if switch:
				self._byId.clear()
				for message in self._outstanding:
					message.msgId = None
				if self._sentSinceCancel:
					self._jobs.append(_CANCEL)
			else:
				for message in self._outstanding:
					message.dropReported()
				self._jobs.extend((self._epoch, message) for message in self._outstanding)
			self._wake.notify()

	def _run(self) -> None:
		while True:
			with self._lock:
				while not self._jobs and not self._terminating:
					self._wake.wait()
				if self._terminating:
					return
				job = self._jobs.popleft()
			try:
				if job is _CANCEL:
					self._sendCancel()
				else:
					self._send(*job)
			except Exception as error:
				if not isinstance(error, OSError):
					log.exception("Speaking through Speech Dispatcher")
				self._abandon(f"Speech Dispatcher could not be reached: {error}")

	def _ensureConnection(self) -> _Connection:
		connection = self._connection
		if connection is not None and not connection.closed:
			return connection
		if time.monotonic() - self._lastFailure < RETRY_INTERVAL:
			raise ConnectionLost("Speech Dispatcher was not reachable a moment ago")
		try:
			connection = self._connect()
		except OSError:
			self._lastFailure = time.monotonic()
			raise
		with self._lock:
			self._connection = connection
		return connection

	def _sendCancel(self) -> None:
		connection = self._connection
		if connection is None or connection.closed:
			return
		with self._lock:
			self._sentSinceCancel = False
		connection.command("CANCEL self")

	def _send(self, epoch: int, message: _Message) -> None:
		with self._lock:
			if epoch != self._epoch:
				return
		connection = self._ensureConnection()
		self._setParameters(connection, message.parameters)

		def register(reply: list[str]) -> None:
			# On the reader thread, before any event of the message can be read.
			if not reply[-1].startswith("225") or len(reply) < 2:
				return
			with self._lock:
				if epoch == self._epoch:
					message.msgId = reply[0][4:]
					self._byId[message.msgId] = message

		with self._lock:
			if epoch != self._epoch:
				return
			self._sentSinceCancel = True
		char = message.char
		if char is not None:
			reply = connection.command(f"CHAR {'space' if char == ' ' else char}", register)
		else:
			reply = connection.speak(message.ssml(), register)
		if not reply[-1].startswith("225"):
			log.warning(f"Speech Dispatcher refused a message: {reply[-1]}")
			with self._lock:
				if epoch == self._epoch:
					self._finish(message)

	def _setParameters(self, connection: _Connection, parameters: dict[str, str]) -> None:
		known = connection.parameters
		for name in ("OUTPUT_MODULE", "LANGUAGE", "SYNTHESIS_VOICE", "RATE", "PITCH", "VOLUME"):
			value = parameters.get(name)
			if value is None or known.get(name) == value:
				continue
			reply = connection.command(f"SET SELF {name} {value}")
			if not reply[-1].startswith("2"):
				log.warning(f"Speech Dispatcher refused SET SELF {name} {value}: {reply[-1]}")
				known.pop(name, None)
				continue
			known[name] = value
			# A module chooses its voice again for a new language, and a voice may bring its language.
			if name == "OUTPUT_MODULE":
				known.pop("LANGUAGE", None)
				known.pop("SYNTHESIS_VOICE", None)
			elif name == "LANGUAGE":
				known.pop("SYNTHESIS_VOICE", None)

	def _onEvent(self, connection: _Connection, reply: list[str]) -> None:
		code = reply[-1][:3]
		msgId = reply[0][4:]
		with self._lock:
			message = self._byId.get(msgId)
			if message is None or connection is not self._connection:
				return
			if code == "700" and len(reply) >= 4:
				self._reportMark(message, reply[2][4:])
			elif code == "701":
				self._reportMarks(message, message.leadingMarks())
			elif code == "702":
				module = message.parameters.get("OUTPUT_MODULE")
				if not message.markedByDaemon and message.hasMarkInsideSound() and module:
					if module not in self._modulesWithoutMarks:
						log.debug(
							f"Speech Dispatcher's {module} reports no marks; its speech is split at each"
						)
					self._modulesWithoutMarks.add(module)
				self._finish(message)
			elif code == "703":
				# Not a cancel of this driver's, which forgets its messages first: another client's
				# CANCEL all, or the daemon's. Going on keeps say all from waiting for ever.
				log.debugWarning(f"Speech Dispatcher cancelled message {msgId} itself")
				self._finish(message)

	def _reportMark(self, message: _Message, name: str) -> None:
		try:
			index = int(name)
		except ValueError:
			return
		marks = message.marks
		if index in marks:
			# Counted even when reported already, as leading marks are at the message's beginning.
			message.markedByDaemon = True
		if index in marks[message.reported :]:
			self._reportMarks(message, marks.index(index, message.reported) + 1)

	def _reportMarks(self, message: _Message, upTo: int) -> None:
		marks = message.marks
		while message.reported < min(upTo, len(marks)):
			index = marks[message.reported]
			message.reported += 1
			synthIndexReached.notify(synth=self, index=index)

	def _finish(self, message: _Message) -> None:
		"""The message has ended: report its marks and those of any message before it, which the daemon
		has dropped, and done once nothing is left."""
		if message not in self._outstanding:
			return
		while self._outstanding:
			first = self._outstanding.popleft()
			self._reportMarks(first, len(first.items))
			if first.msgId is not None:
				self._byId.pop(first.msgId, None)
			if first is message:
				break
		if not self._outstanding and not self._paused:
			synthDoneSpeaking.notify(synth=self)

	def _onLost(self, connection: _Connection) -> None:
		with self._lock:
			if connection is not self._connection:
				return
		self._abandon("The connection to Speech Dispatcher was lost")

	def _abandon(self, why: str) -> None:
		"""Drop everything outstanding, which will never end, and say speech is done, so that nothing
		in NVDA waits on it."""
		with self._lock:
			hadSpeech = bool(self._outstanding)
			self._epoch += 1
			self._outstanding.clear()
			self._byId.clear()
			self._jobs.clear()
			self._paused = False
			self._sentSinceCancel = False
			if hadSpeech:
				log.warning(why)
				synthDoneSpeaking.notify(synth=self)

	def terminate(self) -> None:
		with self._lock:
			self._terminating = True
			self._epoch += 1
			self._outstanding.clear()
			self._byId.clear()
			self._jobs.clear()
			self._wake.notify()
			connection, self._connection = self._connection, None
		if self._worker is not threading.current_thread():
			self._worker.join(REPLY_TIMEOUT)
		if connection is not None:
			connection.close()
		super().terminate()


_PROSODY = {PitchCommand: "PITCH", RateCommand: "RATE", VolumeCommand: "VOLUME"}
