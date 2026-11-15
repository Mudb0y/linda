"""Where NVDA's audio goes on Linux: the sinks behind the wasapi replacement.

The player logic that nvwave relies on (blocking feeds, completion callbacks, stop, pause, leading
silence) lives in the wasapi replacement and is the same for every sink. A sink only has to
consume frames the way a sound device does and say how far it has got, much as WASAPI's
IAudioClient, IAudioRenderClient and IAudioClock do for upstream's player.

Which sink is used is decided once per process, and only by the environment. With
BASEDREADER_AUDIO_CAPTURE_DIR naming a directory, the capture sink also writes what was played
there. Otherwise the null sink is used. Neither opens a sound device or loads a sound library, and
there is no fallback from one sink to another: a sink that cannot be used fails.
"""

import os
import threading
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass

#: Names the directory the capture sink writes to; when unset or empty the null sink is used.
CAPTURE_DIR_VARIABLE = "BASEDREADER_AUDIO_CAPTURE_DIR"

#: The length of the queue a stream holds ahead of the listener, as WASAPI's shared-mode buffer
#: in upstream's player.
BUFFER_MS = 400

WAVE_FORMAT_PCM = 1
WAVE_FORMAT_IEEE_FLOAT = 3


class NoDevice(OSError):
	"""The sink has no output device to play to. nvwave treats this as having no audio at all."""


class StreamLost(OSError):
	"""The stream stopped working while open, as when a device goes away.

	The player opens a new stream and completes every pending callback, because a stranded
	end-of-utterance callback stalls speech.
	"""


@dataclass(frozen=True)
class StreamFormat:
	"""The format of a stream, as plain integers rather than Windows' WAVEFORMATEX.

	WAVEFORMATEX from winBindings has Linux field widths, 32 bytes rather than Windows' 18, so a
	native sink must never take that structure by layout.
	"""

	formatTag: int
	channels: int
	samplesPerSec: int
	bitsPerSample: int

	@property
	def blockAlign(self) -> int:
		return self.bitsPerSample // 8 * self.channels

	def isSupported(self) -> bool:
		if self.channels < 1 or self.samplesPerSec < 1:
			return False
		if self.formatTag == WAVE_FORMAT_PCM:
			return self.bitsPerSample in (8, 16, 24, 32)
		if self.formatTag == WAVE_FORMAT_IEEE_FLOAT:
			return self.bitsPerSample in (32, 64)
		return False

	def framesToSeconds(self, frames: int) -> float:
		return frames / self.samplesPerSec


class SinkStream(ABC):
	"""One player's stream on a sink.

	The player calls these with its own lock held, so a stream is never called from two threads at
	once. A sink that consumes on a thread of its own synchronises with that thread itself. Whenever
	it has made room or moved on, it calls the wake function it was given, from that thread and never
	from inside one of these calls, which would deadlock on the player's lock.
	"""

	#: How many frames the stream holds before write would overrun it.
	bufferFrames: int

	@abstractmethod
	def write(self, data: bytes) -> None:
		"""Queue whole frames to be played after those already queued. Never blocks.

		The player never writes more than bufferFrames less bufferedFrames().
		"""

	@abstractmethod
	def bufferedFrames(self) -> int:
		"""Frames written but not yet taken by the device. May raise StreamLost."""

	@abstractmethod
	def playedFrames(self) -> int:
		"""Frames the listener has heard since the stream was opened or last reset.

		Never more than the frames written. May raise StreamLost.
		"""

	@abstractmethod
	def start(self) -> None:
		"""Start or resume playing what is queued."""

	@abstractmethod
	def stop(self) -> None:
		"""Stop playing at once, keeping what is queued and the position."""

	@abstractmethod
	def reset(self) -> None:
		"""Discard everything queued and start counting frames from zero. Only called when stopped."""

	@abstractmethod
	def setChannelVolume(self, channel: int, level: float) -> None:
		"""Set the gain of one channel, from 0 to 1, for audio not yet played.

		Applied by the sink rather than by the sound server, so that it leaves no state behind in the
		user's audio set-up.
		"""

	@abstractmethod
	def close(self) -> None:
		"""Stop and release the stream. Nothing else is called on it afterwards."""


class Sink(ABC):
	"""Where every stream of the process goes."""

	#: Says in the log which sink is in use.
	description: str

	@abstractmethod
	def openStream(self, deviceId: str, fmt: StreamFormat, wake: Callable[[], None]) -> SinkStream:
		"""Open a stream on the device named by deviceId, or on the default device when that is empty
		or names no device that is present.

		May raise NoDevice, or another OSError when the stream cannot be opened.
		"""

	@abstractmethod
	def devices(self) -> list[tuple[str, str]]:
		"""The output devices that are present, as (identifier, friendly name), not counting the
		default device, which nvwave names itself."""

	@abstractmethod
	def keepAwakeFor(self, deviceId: str, ms: int, noiseVolume: float) -> None:
		"""Keep the device from suspending for ms milliseconds from now, playing silence, or white
		noise when noiseVolume is above 0, so that the start of the next sound is not lost while it
		wakes. A device that does not suspend has nothing to do."""

	@abstractmethod
	def stopKeepingAwake(self) -> None:
		"""Stop whatever keepAwakeFor started."""


_sink: Sink | None = None
_sinkLock = threading.Lock()


def getSink() -> Sink:
	"""The sink this process plays to, chosen on first use."""
	global _sink
	with _sinkLock:
		if _sink is None:
			_sink = _selectSink()
		return _sink


def _selectSink() -> Sink:
	captureDir = os.environ.get(CAPTURE_DIR_VARIABLE)
	if captureDir:
		from .captureSink import CaptureSink

		return CaptureSink(captureDir)
	from .nullSink import NullSink

	return NullSink()
