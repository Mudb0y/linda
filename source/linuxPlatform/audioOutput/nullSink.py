"""A sink that takes audio at the rate a sound device would and discards it.

It has no thread and opens nothing: how far a stream has played is worked out from the clock, so
that the player blocks, paces its feeders and completes its callbacks exactly as it would in front
of a device.
"""

import time
from collections.abc import Callable

from . import BUFFER_MS, Sink, SinkStream, StreamFormat


class NullStream(SinkStream):
	def __init__(self, fmt: StreamFormat):
		self.format = fmt
		self.bufferFrames = fmt.samplesPerSec * BUFFER_MS // 1000
		self.volumes = [1.0] * fmt.channels
		self._written = 0
		# Frames taken as of _markTime, and whether frames are being taken since then.
		self._takenAtMark = 0
		self._markTime = time.monotonic()
		self._running = False

	def _taken(self) -> int:
		if not self._running:
			return self._takenAtMark
		elapsed = time.monotonic() - self._markTime
		# A device that has run out of audio waits for more, so the count never runs ahead of what
		# was written.
		return min(self._written, self._takenAtMark + int(elapsed * self.format.samplesPerSec))

	def _mark(self) -> None:
		self._takenAtMark = self._taken()
		self._markTime = time.monotonic()

	def write(self, data: bytes) -> None:
		# Marking first means that after an underrun the new frames play from now, not from when
		# the queue ran dry.
		self._mark()
		self._written += len(data) // self.format.blockAlign

	def bufferedFrames(self) -> int:
		return self._written - self._taken()

	def playedFrames(self) -> int:
		return self._taken()

	def start(self) -> None:
		if not self._running:
			self._markTime = time.monotonic()
			self._running = True

	def stop(self) -> None:
		if self._running:
			self._mark()
			self._running = False

	def reset(self) -> None:
		self._written = 0
		self._takenAtMark = 0
		self._markTime = time.monotonic()

	def setChannelVolume(self, channel: int, level: float) -> None:
		self.volumes[channel] = level

	def close(self) -> None:
		self.stop()


class NullSink(Sink):
	description = "null sink: audio is timed as a device would play it, then discarded"

	def openStream(self, deviceId: str, fmt: StreamFormat, wake: Callable[[], None]) -> SinkStream:
		return NullStream(fmt)

	def devices(self) -> list[tuple[str, str]]:
		# Only the default device, which nvwave lists itself.
		return []

	def keepAwakeFor(self, deviceId: str, ms: int, noiseVolume: float) -> None:
		# There is no device that could suspend.
		pass

	def stopKeepingAwake(self) -> None:
		pass
