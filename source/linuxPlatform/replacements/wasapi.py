"""Linux replacement for wasapi, the audio player nvwave drives.

Upstream binds the player in nvdaHelperLocal.dll, nvdaHelper/local/wasapi.cpp. This keeps its
entry points and their behaviour, which nvwave and every synth driver depend on, and plays to a
linuxPlatform.audioOutput sink instead of a WASAPI endpoint:
- feed blocks while more than half the stream's buffer is still to be played, and returns long
  before its audio has been heard;
- a completion callback runs only on a thread that is inside feed, sync or idle, once playback has
  passed the end of its audio, and never before the feed that registered it has returned;
- stop may be called from any thread, returns at once, wakes a blocked feeder and discards pending
  callbacks without calling them;
- pause and resume keep what is queued and its position;
- leading silence is trimmed as SilenceDetect does it, with one silent frame put back.

Failures are raised as the WindowsError the HRESULT would have become under ctypes, since nvwave
reads winerror.
"""

import ctypes
import errno
import itertools
import threading
from collections.abc import Callable, Iterable
from enum import Enum, auto
from typing import Any

from comtypes.hresult import E_INVALIDARG
from linuxPlatform import audioOutput
from linuxPlatform.audioOutput import NoDevice, Sink, SinkStream, StreamFormat, StreamLost, pcm
from linuxPlatform.bootstrap import WindowsError
from logHandler import log

# HRESULT_FROM_WIN32(ERROR_NOT_FOUND), which nvwave.playWaveFile takes to mean there is no device.
_E_NOTFOUND = -2147023728
_E_FAIL = -2147467259
_AUDCLNT_E_UNSUPPORTED_FORMAT = -2004287480

#: The shortest sleep of a blocked call, so that a callback due within a millisecond is not waited
#: for by spinning.
_MIN_WAIT = 0.001


class _PlayState(Enum):
	stopped = auto()
	playing = auto()
	# stop() was called; the thread that next feeds, syncs or idles finishes the job.
	stopping = auto()


def _value(arg: Any) -> Any:
	"""The Python value of an argument nvwave may pass as a ctypes simple instance."""
	return arg.value if isinstance(arg, ctypes._SimpleCData) else arg


def _deviceError(e: OSError) -> WindowsError:
	winerror = _E_NOTFOUND if isinstance(e, NoDevice) else _E_FAIL
	return WindowsError(e.errno or errno.EIO, e.strerror or str(e), None, winerror)


class _Player:
	def __init__(
		self,
		handle: int,
		deviceId: str,
		fmt: StreamFormat,
		callback: Callable[[int, int], None],
	):
		self.handle = handle
		self.deviceId = deviceId
		self.format = fmt
		self.callback = callback
		self._lock = threading.Lock()
		self._wakeCondition = threading.Condition(self._lock)
		# Set by a wake that came while nobody waited, so the next wait returns at once.
		self._wakePending = False
		self._stream: SinkStream | None = None
		self._state = _PlayState.stopped
		# Each pending callback's feed id and the frame its audio ends at.
		self._feedEnds: list[tuple[int, int]] = []
		self._sentFrames = 0
		self._nextFeedId = 0
		self._isTrimmingLeadingSilence = False
		self._volumes = [1.0] * fmt.channels

	def _wake(self) -> None:
		with self._lock:
			self._wakePending = True
			self._wakeCondition.notify_all()

	def _open(self, force: bool = False) -> None:
		if self._stream is not None and not force:
			return
		if not self.format.isSupported():
			raise WindowsError(
				errno.EINVAL, f"unsupported format {self.format}", None, _AUDCLNT_E_UNSUPPORTED_FORMAT
			)
		if self._stream is not None:
			self._stream.close()
			self._stream = None
		try:
			stream = audioOutput.getSink().openStream(self.deviceId, self.format, self._wake)
		except OSError as e:
			raise _deviceError(e) from e
		for channel, level in enumerate(self._volumes):
			stream.setChannelVolume(channel, level)
		self._stream = stream
		self._state = _PlayState.stopped

	def _reopenAfterLoss(self) -> None:
		self._open(force=True)
		# A callback stranded with the old stream would never be called, and a stranded end of
		# utterance stalls speech, so they are all due now.
		pending, self._feedEnds = self._feedEnds, []
		self._call(feedId for feedId, _end in pending)
		self._sentFrames = 0

	def _bufferedFrames(self) -> int:
		try:
			return self._stream.bufferedFrames()
		except StreamLost:
			self._reopenAfterLoss()
			return self._stream.bufferedFrames()

	def _playPos(self) -> int:
		try:
			return self._stream.playedFrames()
		except StreamLost:
			# Treated as finished, so that nothing waits for it and its callbacks are not stranded.
			return self._sentFrames

	def _call(self, feedIds: Iterable[int]) -> None:
		feedIds = list(feedIds)
		if not feedIds:
			return
		# Released so that a callback, or another thread meanwhile, can call stop without waiting.
		self._lock.release()
		try:
			for feedId in feedIds:
				self.callback(self.handle, feedId)
		finally:
			self._lock.acquire()

	def _maybeFireCallbacks(self) -> None:
		playPos = self._playPos()
		due = [feedId for feedId, end in self._feedEnds if playPos >= end]
		if due:
			self._feedEnds = [(feedId, end) for feedId, end in self._feedEnds if playPos < end]
			self._call(due)

	def _waitUntilNeeded(self, maxWait: float) -> None:
		if self._feedEnds:
			untilCallback = self.format.framesToSeconds(self._feedEnds[0][1] - self._playPos())
			maxWait = min(maxWait, untilCallback)
		if not self._wakePending:
			self._wakeCondition.wait(max(maxWait, _MIN_WAIT))
		self._wakePending = False

	def _completeStop(self) -> None:
		if self._stream is not None:
			self._stream.reset()
		self._nextFeedId = 0
		self._sentFrames = 0
		self._feedEnds = []
		self._state = _PlayState.stopped

	def _continueUnlessStopping(self) -> bool:
		if self._state is _PlayState.stopping:
			self._completeStop()
			return False
		return True

	def open(self) -> None:
		with self._lock:
			self._open()

	def feed(self, data: bytes | None, size: int, idOut: Any) -> None:
		with self._lock:
			self._open()
			if self._state is _PlayState.stopping:
				# stop() was called after the previous feed returned.
				self._completeStop()
			fmt = self.format
			view = memoryview(data) if data is not None else None
			remainingFrames = size // fmt.blockAlign
			shouldInsertSilentFrame = False
			if self._isTrimmingLeadingSilence and view is not None and size > 0:
				silenceSize = pcm.leadingSilenceSize(fmt, data)
				if silenceSize >= size:
					# The whole chunk is silence. One silent frame still goes out, so that the rest of
					# the feed runs as usual and the device keeps being fed.
					shouldInsertSilentFrame = True
					remainingFrames = 1
				else:
					view = view[silenceSize:]
					size -= silenceSize
					remainingFrames = size // fmt.blockAlign
					self._isTrimmingLeadingSilence = False
					# Without a silent frame before it, the start of the audio can be cut off
					# (NVDA discussion 17697).
					shouldInsertSilentFrame = True
					remainingFrames += 1
			while remainingFrames > 0:
				if not self._continueUnlessStopping():
					return
				paddingFrames = self._bufferedFrames()
				bufferFrames = self._stream.bufferFrames
				if paddingFrames > bufferFrames // 2:
					self._waitUntilNeeded(fmt.framesToSeconds(paddingFrames - bufferFrames // 2))
					if not self._continueUnlessStopping():
						return
					paddingFrames = self._bufferedFrames()
				sendFrames = min(remainingFrames, bufferFrames - paddingFrames)
				if sendFrames <= 0:
					continue
				silentFrames = 1 if shouldInsertSilentFrame else 0
				sendBytes = (sendFrames - silentFrames) * fmt.blockAlign
				if view is not None:
					chunk = pcm.silence(fmt, silentFrames) + bytes(view[:sendBytes])
				else:
					# No data means silence.
					chunk = pcm.silence(fmt, sendFrames)
				shouldInsertSilentFrame = False
				self._stream.write(chunk)
				if self._state is _PlayState.stopped:
					self._stream.start()
					self._state = _PlayState.playing
				self._maybeFireCallbacks()
				if view is not None:
					view = view[sendBytes:]
				size -= sendBytes
				remainingFrames -= sendFrames
				self._sentFrames += sendFrames
			if self._state is _PlayState.playing:
				self._maybeFireCallbacks()
			if idOut is not None:
				# Taken only after the callbacks above, so that this one cannot run before the caller
				# has the id and has filed its callback under it.
				feedId = self._nextFeedId
				self._nextFeedId += 1
				_storeFeedId(idOut, feedId)
				self._feedEnds.append((feedId, self._sentFrames))

	def stop(self) -> None:
		with self._lock:
			if self._stream is not None:
				self._stream.stop()
			self._state = _PlayState.stopping
			self._wakePending = True
			self._wakeCondition.notify_all()

	def sync(self) -> None:
		with self._lock:
			self._syncLocked()

	def _syncLocked(self) -> None:
		if self._stream is None:
			return
		sentFrames = self._sentFrames
		while (playPos := self._playPos()) < sentFrames:
			if self._state is not _PlayState.playing:
				return
			self._maybeFireCallbacks()
			self._waitUntilNeeded(self.format.framesToSeconds(sentFrames - playPos))
		# A callback right at the end of the stream.
		if self._state is _PlayState.playing:
			self._maybeFireCallbacks()

	def idle(self) -> None:
		self.sync()
		self.stop()
		with self._lock:
			self._completeStop()

	def pause(self) -> None:
		with self._lock:
			if self._state is _PlayState.playing:
				self._stream.stop()

	def resume(self) -> None:
		with self._lock:
			if self._state is _PlayState.playing:
				self._stream.start()

	def setChannelVolume(self, channel: int, level: float) -> None:
		with self._lock:
			self._open()
			if not 0 <= channel < self.format.channels or not 0.0 <= level <= 1.0:
				# As IAudioStreamVolume::SetChannelVolume; nvwave relies on it for mono streams.
				raise WindowsError(errno.EINVAL, "The parameter is incorrect", None, E_INVALIDARG)
			self._volumes[channel] = level
			self._stream.setChannelVolume(channel, level)

	def startTrimmingLeadingSilence(self, start: bool) -> None:
		with self._lock:
			self._isTrimmingLeadingSilence = start

	def destroy(self) -> None:
		with self._lock:
			if self._stream is not None:
				self._stream.close()
				self._stream = None


def _storeFeedId(idOut: Any, feedId: int) -> None:
	# nvwave passes byref(c_uint()); a pointer() would do as well.
	target = idOut._obj if hasattr(idOut, "_obj") else idOut.contents
	target.value = feedId


_players: dict[int, _Player] = {}
_playersLock = threading.Lock()
# Starting at 1: nvwave keys its players by the handle, and a handle of 0 would become None in the
# callback's c_void_p argument.
_handles = itertools.count(1)
_silenceDeviceId: str | None = None


def _player(handle: Any) -> _Player:
	return _players[_value(handle)]


def wasPlay_startup() -> None:
	sink: Sink = audioOutput.getSink()
	log.info(f"Audio output: {sink.description}")


def wasPlay_create(endpointId: str, fmt: Any, callback: Callable[[int, int], None]) -> int:
	# Copied now, as the C++ takes the structure by value.
	streamFormat = StreamFormat(
		formatTag=fmt.wFormatTag,
		channels=fmt.nChannels,
		samplesPerSec=fmt.nSamplesPerSec,
		bitsPerSample=fmt.wBitsPerSample,
	)
	with _playersLock:
		handle = next(_handles)
		_players[handle] = _Player(handle, _value(endpointId) or "", streamFormat, callback)
	return handle


def wasPlay_destroy(player: Any) -> None:
	with _playersLock:
		p = _players.pop(_value(player))
	p.destroy()


def wasPlay_open(player: Any) -> None:
	_player(player).open()


def wasPlay_feed(player: Any, data: Any, size: Any, id: Any) -> None:
	size = _value(size)
	address = ctypes.cast(data, ctypes.c_void_p).value if data is not None else None
	_player(player).feed(ctypes.string_at(address, size) if address else None, size, id)


def wasPlay_stop(player: Any) -> None:
	_player(player).stop()


def wasPlay_sync(player: Any) -> None:
	_player(player).sync()


def wasPlay_idle(player: Any) -> None:
	_player(player).idle()


def wasPlay_pause(player: Any) -> None:
	_player(player).pause()


def wasPlay_resume(player: Any) -> None:
	_player(player).resume()


def wasPlay_setChannelVolume(player: Any, channel: Any, level: Any) -> None:
	_player(player).setChannelVolume(_value(channel), _value(level))


def wasPlay_startTrimmingLeadingSilence(player: Any, start: Any) -> None:
	_player(player).startTrimmingLeadingSilence(bool(_value(start)))


def wasSilence_init(endpointId: str) -> None:
	global _silenceDeviceId
	_silenceDeviceId = _value(endpointId) or ""


def wasSilence_playFor(ms: Any, volume: Any) -> None:
	if _silenceDeviceId is not None:
		audioOutput.getSink().keepAwakeFor(_silenceDeviceId, _value(ms), _value(volume))


def wasSilence_terminate() -> None:
	global _silenceDeviceId
	_silenceDeviceId = None
	audioOutput.getSink().stopKeepingAwake()
