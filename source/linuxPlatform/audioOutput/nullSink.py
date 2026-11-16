"""A sink that takes audio at the rate a sound device would and discards it.

It has no thread and opens nothing: how far a stream has played is worked out from the clock, so
that the player blocks, paces its feeders and completes its callbacks exactly as it would in front
of a device.
"""

import collections
import time
from collections.abc import Callable

from . import BUFFER_MS, Sink, SinkStream, StreamFormat, checkGain


class NullStream(SinkStream):
	def __init__(self, fmt: StreamFormat) -> None:
		self.format = fmt
		self.bufferFrames = fmt.samplesPerSec * BUFFER_MS // 1000
		self.volumes = [1.0] * fmt.channels
		self._written = 0
		# Frames taken as of _markTime, and whether frames are being taken since then.
		self._takenAtMark = 0
		self._markTime = time.monotonic()
		self._running = False
		# When stop was called, for the next of the other methods to apply. stop runs alongside them,
		# and appending to a deque is the one thing it does, so that it never waits for the feeder.
		self._stops: collections.deque[float] = collections.deque()

	def _settle(self) -> None:
		"""Apply each stop called since the last look, at the time it was called."""
		while self._stops:
			at = self._stops.popleft()
			if self._running:
				self._mark(at)
				self._running = False

	def _taken(self) -> int:
		self._settle()
		if not self._running:
			return self._takenAtMark
		elapsed = time.monotonic() - self._markTime
		# A device that has run out of audio waits for more, so the count never runs ahead of what
		# was written.
		return min(self._written, self._takenAtMark + int(elapsed * self.format.samplesPerSec))

	def _mark(self, now: float) -> None:
		if not self._running:
			self._markTime = now
			return
		rate = self.format.samplesPerSec
		# A stop timed before the last mark, by a thread that was held up between reading the clock
		# and recording it, stops the count where it stands.
		elapsedFrames = max(0, int((now - self._markTime) * rate))
		if self._takenAtMark + elapsedFrames >= self._written:
			# Ran dry: the device has been waiting since then, so it carries on from now.
			self._takenAtMark = self._written
			self._markTime = max(now, self._markTime)
		else:
			self._takenAtMark += elapsedFrames
			# Moved on by whole frames only, so that the part of a frame already under way is not
			# dropped at every write, which slows the clock in proportion to how often it is fed.
			self._markTime += elapsedFrames / rate

	def write(self, data: bytes) -> None:
		self._settle()
		# Marking first means that after an underrun the new frames play from now, not from when
		# the queue ran dry.
		self._mark(time.monotonic())
		self._written += len(data) // self.format.blockAlign

	def bufferedFrames(self) -> int:
		return self._written - self._taken()

	def playedFrames(self) -> int:
		return self._taken()

	def start(self) -> None:
		self._settle()
		if not self._running:
			self._markTime = time.monotonic()
			self._running = True

	def stop(self) -> None:
		self._stops.append(time.monotonic())

	def reset(self) -> None:
		self._settle()
		self._written = 0
		self._takenAtMark = 0
		self._markTime = time.monotonic()

	def setChannelVolume(self, channel: int, level: float) -> None:
		self.volumes[channel] = level

	def close(self) -> None:
		self._stops.append(time.monotonic())
		self._settle()


class NullSink(Sink):
	kind = "null"
	description = "null sink: audio is timed as a device would play it, then discarded"

	def __init__(self) -> None:
		self.gain = 1.0

	def setGain(self, level: float) -> None:
		checkGain(level)
		self.gain = level

	def deliveredFrames(self) -> int:
		# Nothing reaches an output.
		return 0

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
