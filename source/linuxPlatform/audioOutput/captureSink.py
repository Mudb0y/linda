"""A null sink that also keeps what was played, as WAV files in a directory, for tests to examine.

Each continuous stretch of a stream's playing, from its first frame until it is stopped and reset or
closed, goes to its own file, <stretch number>-stream<stream number>.wav, numbered in the order the
stretches began. A frame goes to the file only once the clock says it has been heard, at the gain
it was heard at, so audio discarded by stopping never appears, and a stretch stopped before any of
it was heard leaves no file and a gap in the numbering. Whatever was heard is in the files by the
time stop returns.
"""

import errno
import itertools
import os
import threading
import wave
import weakref
from collections.abc import Callable

from linuxPlatform import NoLinuxImplementation

from . import WAVE_FORMAT_PCM, SinkStream, StreamFormat, checkGain, pcm
from .nullSink import NullSink, NullStream


class CaptureStream(NullStream):
	def __init__(self, sink: "CaptureSink", streamNumber: int, fmt: StreamFormat) -> None:
		super().__init__(fmt)
		self._sink = sink
		self._streamNumber = streamNumber
		# stop runs alongside the other methods, and writes what was heard as they do.
		self._lock = threading.Lock()
		# Written but not yet heard.
		self._unheard = bytearray()
		self._heardFrames = 0
		self._stretchNumber: int | None = None
		self._file: wave.Wave_write | None = None
		self._closed = False

	def _keepHeard(self) -> None:
		heard = self._taken()
		frames = heard - self._heardFrames
		if frames <= 0:
			return
		size = frames * self.format.blockAlign
		gain = self._sink.gain
		data = pcm.applyGain(self.format, bytes(self._unheard[:size]), [v * gain for v in self.volumes])
		del self._unheard[:size]
		self._heardFrames = heard
		if self._file is None:
			self._file = self._sink._openFile(self._stretchNumber, self._streamNumber, self.format)
		# wave rewrites the header after every write, so the file is complete at every step.
		self._file.writeframes(data)
		self._sink._noteDelivered(frames)

	def keepHeard(self) -> None:
		"""Write out everything heard so far."""
		with self._lock:
			if not self._closed:
				self._keepHeard()

	def _endStretch(self) -> None:
		self._keepHeard()
		self._unheard.clear()
		self._heardFrames = 0
		self._stretchNumber = None
		if self._file is not None:
			self._file.close()
			self._file = None

	def write(self, data: bytes) -> None:
		with self._lock:
			if self._stretchNumber is None:
				self._stretchNumber = self._sink._nextStretchNumber()
			super().write(data)
			self._unheard += data

	def bufferedFrames(self) -> int:
		with self._lock:
			self._keepHeard()
			return super().bufferedFrames()

	def playedFrames(self) -> int:
		with self._lock:
			self._keepHeard()
			return super().playedFrames()

	def start(self) -> None:
		with self._lock:
			super().start()

	def stop(self) -> None:
		# The clock stops at the call, before the lock is taken, so that waiting for it cannot let
		# more be heard.
		super().stop()
		self.keepHeard()

	def reset(self) -> None:
		with self._lock:
			self._endStretch()
			super().reset()

	def setChannelVolume(self, channel: int, level: float) -> None:
		with self._lock:
			self._keepHeard()
			super().setChannelVolume(channel, level)

	def close(self) -> None:
		self._sink._forget(self)
		with self._lock:
			super().close()
			self._endStretch()
			self._closed = True


class CaptureSink(NullSink):
	kind = "capture"

	def __init__(self, directory: str) -> None:
		super().__init__()
		if not os.path.isdir(directory):
			raise NotADirectoryError(errno.ENOTDIR, "the audio capture directory does not exist", directory)
		self.directory = directory
		self.description = f"capture sink: audio is timed as a device would play it and kept in {directory}"
		self._lock = threading.Lock()
		self._streamNumbers = itertools.count(1)
		self._stretchNumbers = itertools.count(1)
		self._streams: weakref.WeakSet[CaptureStream] = weakref.WeakSet()
		self._delivered = 0
		# Held while the gain changes, so that what was heard before it is kept at the gain it was
		# heard at.
		self._gainLock = threading.Lock()

	def openStream(self, deviceId: str, fmt: StreamFormat, wake: Callable[[], None]) -> SinkStream:
		if fmt.formatTag != WAVE_FORMAT_PCM:
			# Python's wave module writes integer PCM only, and nvwave plays nothing else.
			raise NoLinuxImplementation(
				"linuxPlatform.audioOutput.captureSink.CaptureSink.openStream",
				f"the capture sink keeps integer PCM only, not wave format {fmt.formatTag}",
			)
		with self._lock:
			stream = CaptureStream(self, next(self._streamNumbers), fmt)
			self._streams.add(stream)
		return stream

	def _openStreams(self) -> list[CaptureStream]:
		with self._lock:
			return list(self._streams)

	def _forget(self, stream: CaptureStream) -> None:
		with self._lock:
			self._streams.discard(stream)

	def setGain(self, level: float) -> None:
		checkGain(level)
		with self._gainLock:
			for stream in self._openStreams():
				stream.keepHeard()
			self.gain = level

	def deliveredFrames(self) -> int:
		"""The frames written to the files, counting all that has been heard by now."""
		for stream in self._openStreams():
			stream.keepHeard()
		with self._lock:
			return self._delivered

	def _noteDelivered(self, frames: int) -> None:
		with self._lock:
			self._delivered += frames

	def _nextStretchNumber(self) -> int:
		with self._lock:
			return next(self._stretchNumbers)

	def _openFile(self, stretchNumber: int, streamNumber: int, fmt: StreamFormat) -> wave.Wave_write:
		path = os.path.join(self.directory, f"{stretchNumber:05d}-stream{streamNumber:03d}.wav")
		# Kept open for the whole stretch and closed when it ends.
		f = wave.open(path, "wb")  # noqa: SIM115
		f.setnchannels(fmt.channels)
		f.setsampwidth(fmt.bitsPerSample // 8)
		f.setframerate(fmt.samplesPerSec)
		return f
