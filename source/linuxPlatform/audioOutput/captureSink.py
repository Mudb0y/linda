"""A null sink that also keeps what was played, as WAV files in a directory, for tests to examine.

Each continuous stretch of a stream's playing, from its first frame until it is stopped and reset or
closed, goes to its own file, <stretch number>-stream<stream number>.wav, numbered in the order the
stretches began. A frame goes to the file only once the clock says it has been heard, at the gain
it was heard at, so audio discarded by stopping never appears, and a stretch stopped before any of
it was heard leaves no file and a gap in the numbering.
"""

import errno
import itertools
import os
import threading
import wave
from collections.abc import Callable

from linuxPlatform import NoLinuxImplementation

from . import WAVE_FORMAT_PCM, SinkStream, StreamFormat, pcm
from .nullSink import NullSink, NullStream


class CaptureStream(NullStream):
	def __init__(self, sink: "CaptureSink", streamNumber: int, fmt: StreamFormat) -> None:
		super().__init__(fmt)
		self._sink = sink
		self._streamNumber = streamNumber
		# Written but not yet heard.
		self._unheard = bytearray()
		self._heardFrames = 0
		self._stretchNumber: int | None = None
		self._file: wave.Wave_write | None = None

	def _keepHeard(self) -> None:
		heard = self._taken()
		frames = heard - self._heardFrames
		if frames <= 0:
			return
		size = frames * self.format.blockAlign
		data = pcm.applyGain(self.format, bytes(self._unheard[:size]), self.volumes)
		del self._unheard[:size]
		self._heardFrames = heard
		if self._file is None:
			self._file = self._sink._openFile(self._stretchNumber, self._streamNumber, self.format)
		# wave rewrites the header after every write, so the file is complete at every step.
		self._file.writeframes(data)

	def _endStretch(self) -> None:
		self._keepHeard()
		self._unheard.clear()
		self._heardFrames = 0
		self._stretchNumber = None
		if self._file is not None:
			self._file.close()
			self._file = None

	def write(self, data: bytes) -> None:
		if self._stretchNumber is None:
			self._stretchNumber = self._sink._nextStretchNumber()
		super().write(data)
		self._unheard += data

	def bufferedFrames(self) -> int:
		self._keepHeard()
		return super().bufferedFrames()

	def playedFrames(self) -> int:
		self._keepHeard()
		return super().playedFrames()

	def stop(self) -> None:
		super().stop()
		self._keepHeard()

	def reset(self) -> None:
		self._endStretch()
		super().reset()

	def setChannelVolume(self, channel: int, level: float) -> None:
		self._keepHeard()
		super().setChannelVolume(channel, level)

	def close(self) -> None:
		super().close()
		self._endStretch()


class CaptureSink(NullSink):
	def __init__(self, directory: str) -> None:
		if not os.path.isdir(directory):
			raise NotADirectoryError(errno.ENOTDIR, "the audio capture directory does not exist", directory)
		self.directory = directory
		self.description = f"capture sink: audio is timed as a device would play it and kept in {directory}"
		self._lock = threading.Lock()
		self._streamNumbers = itertools.count(1)
		self._stretchNumbers = itertools.count(1)

	def openStream(self, deviceId: str, fmt: StreamFormat, wake: Callable[[], None]) -> SinkStream:
		if fmt.formatTag != WAVE_FORMAT_PCM:
			# Python's wave module writes integer PCM only, and nvwave plays nothing else.
			raise NoLinuxImplementation(
				"linuxPlatform.audioOutput.captureSink.CaptureSink.openStream",
				f"the capture sink keeps integer PCM only, not wave format {fmt.formatTag}",
			)
		with self._lock:
			streamNumber = next(self._streamNumbers)
		return CaptureStream(self, streamNumber, fmt)

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
