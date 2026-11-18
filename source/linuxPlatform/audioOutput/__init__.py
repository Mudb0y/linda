"""Where NVDA's audio goes on Linux: the sinks behind the wasapi replacement.

The player logic that nvwave relies on (blocking feeds, completion callbacks, stop, pause, leading
silence) lives in the wasapi replacement and is the same for every sink. A sink only has to
consume frames the way a sound device does and say how far it has got, much as WASAPI's
IAudioClient, IAudioRenderClient and IAudioClock do for upstream's player.

Which sink is used is decided once per process, by the environment or else by what the installation
fixed (linuxPlatform.installSettings). LINDA_AUDIO_SINK names it: null, capture or pipewire. Unset or empty, the capture sink is used when
LINDA_AUDIO_CAPTURE_DIR names a directory, and the null sink otherwise. The capture sink writes
what was played to that directory; neither it nor the null sink opens a sound device or loads a
sound library. There is no fallback from one sink to another: a sink that cannot be used fails.
"""

import errno
import os
import threading
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass

from linuxPlatform import installSettings

#: Names the sink: null, capture or pipewire.
SINK_VARIABLE = "LINDA_AUDIO_SINK"
#: Names the directory the capture sink writes to.
CAPTURE_DIR_VARIABLE = "LINDA_AUDIO_CAPTURE_DIR"

#: The length of the queue a stream holds ahead of the listener, as WASAPI's shared-mode buffer
#: in upstream's player.
BUFFER_MS = 400

WAVE_FORMAT_PCM = 1
WAVE_FORMAT_IEEE_FLOAT = 3


class NoDevice(OSError):
	"""The sink has no output device to play to. nvwave treats this as having no audio at all."""


class StreamLost(OSError):
	"""The stream stopped working while open, as when a device goes away.

	Any method of SinkStream may raise it, and the player never passes it on to nvwave. From then on
	the player calls nothing on that stream but close, and takes every frame written to it as played,
	so that nothing waits on a clock that has stopped. The next feed to find the stream lost, which is
	the feed in progress when write, start or bufferedFrames raised it, closes it, opens another,
	completes every pending callback, because a stranded end-of-utterance callback stalls speech, and
	plays the rest of the feed on the new stream, the frames of a failed write included. A loss found
	anywhere else waits for that next feed, so that callbacks still run only where nvwave expects
	them. When the stream that replaced a lost one is lost too before it has taken anything, the sink
	cannot play for now: the rest of that feed is taken as played, so that its callback still runs,
	and the feed after it tries a new stream again. A stop that finds the stream lost has nothing left
	to silence.
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

	The player calls write, bufferedFrames, playedFrames, start, reset, setChannelVolume and close with
	its own lock held, so none of them is ever called from two threads at once. abort and stop are
	called without that lock, so that silencing never waits for a thread that is feeding, which can be
	held up for whole switch intervals winning the GIL back after each call into C while other threads
	run Python. Either may therefore run on any thread at the same time as any other method, each
	other included, but never at the same time as close, and never after it. A sink that consumes on
	a thread of its own synchronises with that thread itself.

	The player times its waits from the format and the frame counts, which is all a sink whose
	device keeps real time needs. A sink whose device may run ahead of that estimate calls the wake
	function it was given whenever it has made room or moved on, so that a waiting feed or sync
	looks again at once. wake holds a lock of the player's only for a moment, and never one these
	methods are called with, but it runs Python, so it must be called from an ordinary thread that
	can take the GIL, never from a real-time audio thread, which must not wait on Python. A real-time
	callback hands the event to a helper thread instead, through an eventfd or a pipe. wake is
	harmless after close.

	Any method may raise OSError when the sink fails. The player reports it to nvwave as a failed
	HRESULT, E_FAIL for most errors and HRESULT_FROM_WIN32(ERROR_NOT_FOUND) for NoDevice, except from
	close, which nvwave has no way to be told of. StreamLost is handled by the player itself.
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
		"""Frames written but not yet taken by the device."""

	@abstractmethod
	def playedFrames(self) -> int:
		"""Frames the listener has heard since the stream was opened or last reset.

		Never more than the frames written. Keeps advancing while the stream is started and holds
		frames, even when the device or server stops consuming them: a sink whose output stalls holds
		the position for at most half a second, which is time for a new stream to be linked, and then
		discards in real time, so that feeds, syncs and the callbacks of NVDA's speech never wait on a
		device that is not there.
		"""

	@abstractmethod
	def start(self) -> None:
		"""Start or resume playing what is queued, unless abort has been called since the last reset."""

	@abstractmethod
	def stop(self) -> None:
		"""Stop playing at once, keeping what is queued and the position, until the next start.

		Takes effect within one device period, and never waits on anything a real-time thread holds,
		nor on the thread that is feeding.
		"""

	@abstractmethod
	def abort(self) -> None:
		"""Stop playing at once as stop does, and stay silent, whatever start is called meanwhile, until
		the next reset.

		This is how the player silences a stream while another thread may be feeding it: the feeder
		can be about to start the stream as the abort lands, and only the sink can see which came
		first, without a lock that would make the abort wait for the feeder.
		"""

	@abstractmethod
	def reset(self) -> None:
		"""Discard everything queued, start counting frames from zero, and end an abort. Only called
		after stop, so that the stream is silent until the next start."""

	@abstractmethod
	def setChannelVolume(self, channel: int, level: float) -> None:
		"""Set the gain of one channel, from 0 to 1, for audio not yet played.

		Applied by the sink rather than by the sound server, so that it leaves no state behind in the
		user's audio set-up.
		"""

	@abstractmethod
	def close(self) -> None:
		"""Stop and release the stream. Nothing else is called on it afterwards.

		The collector can call this, through nvwave.WavePlayer.__del__, on a thread that is inside a
		method of the sink or of this stream, so it must not wait for a lock that such a call could be
		holding.
		"""


class Sink(ABC):
	"""Where every stream of the process goes."""

	#: Which sink this is: "null", "capture" or "pipewire".
	kind: str
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

	@abstractmethod
	def setGain(self, level: float) -> None:
		"""Set a gain from 0 to 1 for every stream of the process, applied to audio not yet played,
		after each stream's channel volumes and before samples are clamped to their range. It is 1
		until set. Applied by the sink, never as a volume of the sound server.

		:raise ValueError: When level is outside 0 to 1.
		"""

	@abstractmethod
	def deliveredFrames(self) -> int:
		"""The frames, summed over all streams, that have reached a real output since the sink was
		made, which tells audio that was heard from audio that was only timed."""


def checkGain(level: float) -> None:
	"""Refuse a process gain outside 0 to 1, as Sink.setGain promises."""
	if not 0.0 <= level <= 1.0:
		raise ValueError(f"a gain of {level} is not between 0 and 1")


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
	kind = installSettings.get(SINK_VARIABLE) or ""
	captureDir = os.environ.get(CAPTURE_DIR_VARIABLE, "")
	if kind == "":
		kind = "capture" if captureDir else "null"
	if kind == "null":
		from .nullSink import NullSink

		return NullSink()
	if kind == "capture":
		if not captureDir:
			raise OSError(
				errno.ENOENT,
				f"{SINK_VARIABLE} is capture, but {CAPTURE_DIR_VARIABLE} names no directory to capture to",
			)
		from .captureSink import CaptureSink

		return CaptureSink(captureDir)
	if kind == "pipewire":
		from .pipewireSink import PipewireSink

		return PipewireSink()
	raise OSError(errno.EINVAL, f"{SINK_VARIABLE} must be null, capture or pipewire, not {kind!r}")
