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
- leading silence is trimmed as SilenceDetect does it, with one silent frame put back;
- at interpreter exit every stream is closed and no other is opened, so that no thread is left
  inside a sink as the interpreter is torn down.

Failures are raised as the WindowsError the HRESULT would have become under ctypes, since nvwave
reads winerror.
"""

import atexit
import contextlib
import ctypes
import errno
import functools
import itertools
import threading
from collections.abc import Callable, Iterable, Iterator
from enum import Enum, auto
from typing import Any, NoReturn

from comtypes.hresult import E_INVALIDARG
from linuxPlatform import audioOutput, missingAttribute
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

#: How long the exit hook waits for a thread to come out of a call into a stream.
_EXIT_WAIT = 1.0

#: Set once the interpreter is exiting, after which no stream is opened.
_isExiting = False


class _PlayState(Enum):
	stopped = auto()
	playing = auto()
	# stop() was called; the thread that next feeds or idles finishes the job.
	stopping = auto()


def _value(arg: Any) -> Any:
	"""The Python value of an argument nvwave may pass as a ctypes simple instance."""
	return arg.value if isinstance(arg, ctypes._SimpleCData) else arg


@contextlib.contextmanager
def _asWindowsErrors() -> Iterator[None]:
	"""Raise a sink's OSError as the WindowsError nvwave expects, which carries winerror."""
	try:
		yield
	except WindowsError:
		raise
	except OSError as e:
		winerror = _E_NOTFOUND if isinstance(e, NoDevice) else _E_FAIL
		raise WindowsError(e.errno or errno.EIO, e.strerror or str(e), None, winerror) from e


def _entryPoint[**P, R](func: Callable[P, R]) -> Callable[P, R]:
	# The boundary where wasapi.cpp returns an HRESULT, which ctypes raises as an OSError with winerror.
	@functools.wraps(func)
	def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
		with _asWindowsErrors():
			return func(*args, **kwargs)

	return wrapper


class _Player:
	def __init__(
		self,
		handle: int,
		deviceId: str,
		fmt: StreamFormat,
		callback: Callable[[int, int], None],
	) -> None:
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

	def _open(self) -> None:
		if self._stream is not None or _isExiting:
			return
		if not self.format.isSupported():
			raise WindowsError(
				errno.EINVAL, f"unsupported format {self.format}", None, _AUDCLNT_E_UNSUPPORTED_FORMAT
			)
		stream = audioOutput.getSink().openStream(self.deviceId, self.format, self._wake)
		try:
			for channel, level in enumerate(self._volumes):
				stream.setChannelVolume(channel, level)
		except OSError:
			# The open failed, so the next feed opens a new stream; this one would never be closed.
			with contextlib.suppress(OSError):
				stream.close()
			raise
		self._stream = stream
		self._state = _PlayState.stopped

	def _reopenAfterLoss(self) -> None:
		lost, self._stream = self._stream, None
		# Stopped before reopening, so that if no stream can be opened, pause, resume and sync find
		# nothing playing rather than a missing stream.
		self._state = _PlayState.stopped
		pending, self._feedEnds = self._feedEnds, []
		self._sentFrames = 0
		try:
			# The stream is gone already; failing to release it changes nothing.
			with contextlib.suppress(OSError):
				lost.close()
			self._open()
		finally:
			# A callback stranded with the old stream would never be called, and a stranded end of
			# utterance stalls speech, so they are all due now, even when no new stream opened.
			self._call(feedId for feedId, _end in pending)

	def _bufferedFrames(self) -> int | None:
		"""The frames still to be played, or None when the stream was lost and the player has none.

		That happens at interpreter exit, when no new stream is opened, or when the exit hook closed
		the new one while a stranded callback had the lock released.
		"""
		try:
			return self._stream.bufferedFrames()
		except StreamLost:
			self._reopenAfterLoss()
			if self._stream is None:
				return None
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
			if self._stream is None:
				# The interpreter is exiting, and nothing more is played.
				return
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
				if paddingFrames is None:
					return
				bufferFrames = self._stream.bufferFrames
				if paddingFrames > bufferFrames // 2:
					self._waitUntilNeeded(fmt.framesToSeconds(paddingFrames - bufferFrames // 2))
					if not self._continueUnlessStopping():
						return
					paddingFrames = self._bufferedFrames()
					if paddingFrames is None:
						return
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
		sentFrames = self._sentFrames
		# Checked on every pass, as a callback releases the lock, and stop or the exit hook may run.
		while self._stream is not None and self._state is _PlayState.playing:
			playPos = self._playPos()
			if playPos >= sentFrames:
				# A callback right at the end of the stream.
				self._maybeFireCallbacks()
				return
			self._maybeFireCallbacks()
			self._waitUntilNeeded(self.format.framesToSeconds(sentFrames - playPos))

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
			if self._stream is not None:
				self._stream.setChannelVolume(channel, level)

	def startTrimmingLeadingSilence(self, start: bool) -> None:
		with self._lock:
			self._isTrimmingLeadingSilence = start

	def destroy(self) -> None:
		with self._lock:
			if self._stream is not None:
				self._stream.close()
				self._stream = None

	def closeForExit(self) -> None:
		"""Close the stream and leave any waiting feed or sync to return, without waiting for long.

		A daemon thread, such as nvwave's wave file player, can still be playing at interpreter exit,
		and must not be inside a sink, which may be native code, as the interpreter is torn down.
		"""
		if not self._lock.acquire(timeout=_EXIT_WAIT):
			# A thread is stuck inside the stream; calling it from here too would break the sink's
			# contract.
			return
		try:
			stream, self._stream = self._stream, None
			# Stopping, so that a feed or sync now waiting returns as it does after stop().
			self._state = _PlayState.stopping
			# A sync that a callback left with the lock released would otherwise wait on the next end,
			# asking a stream that is gone how far it has played.
			self._feedEnds = []
			self._wakePending = True
			self._wakeCondition.notify_all()
			if stream is not None:
				with contextlib.suppress(OSError):
					stream.close()
		finally:
			self._lock.release()


def _storeFeedId(idOut: Any, feedId: int) -> None:
	# nvwave passes byref(c_uint()); a pointer() would do as well.
	target = idOut._obj if hasattr(idOut, "_obj") else idOut.contents
	target.value = feedId


# No lock guards this: wasPlay_destroy runs from WavePlayer.__del__, so from wherever the collector
# runs, including inside wasPlay_create on the same thread, and a lock taken there could be held by
# the thread itself. Each access is a single dict operation, which the GIL makes atomic.
_players: dict[int, _Player] = {}
# Starting at 1: nvwave keys its players by the handle, and a handle of 0 would become None in the
# callback's c_void_p argument.
_handles = itertools.count(1)
_silenceDeviceId: str | None = None


def _player(handle: Any) -> _Player:
	return _players[_value(handle)]


@_entryPoint
def wasPlay_startup() -> None:
	sink: Sink = audioOutput.getSink()
	log.info(f"Audio output: {sink.description}")


@_entryPoint
def wasPlay_create(endpointId: str, fmt: Any, callback: Callable[[int, int], None]) -> int:
	# Copied now, as the C++ takes the structure by value.
	streamFormat = StreamFormat(
		formatTag=fmt.wFormatTag,
		channels=fmt.nChannels,
		samplesPerSec=fmt.nSamplesPerSec,
		bitsPerSample=fmt.wBitsPerSample,
	)
	handle = next(_handles)
	_players[handle] = _Player(handle, _value(endpointId) or "", streamFormat, callback)
	return handle


@_entryPoint
def wasPlay_destroy(player: Any) -> None:
	_players.pop(_value(player)).destroy()


@_entryPoint
def wasPlay_open(player: Any) -> None:
	_player(player).open()


@_entryPoint
def wasPlay_feed(player: Any, data: Any, size: Any, id: Any) -> None:
	size = _value(size)
	address = ctypes.cast(data, ctypes.c_void_p).value if data is not None else None
	_player(player).feed(ctypes.string_at(address, size) if address else None, size, id)


@_entryPoint
def wasPlay_stop(player: Any) -> None:
	_player(player).stop()


@_entryPoint
def wasPlay_sync(player: Any) -> None:
	_player(player).sync()


@_entryPoint
def wasPlay_idle(player: Any) -> None:
	_player(player).idle()


@_entryPoint
def wasPlay_pause(player: Any) -> None:
	_player(player).pause()


@_entryPoint
def wasPlay_resume(player: Any) -> None:
	_player(player).resume()


@_entryPoint
def wasPlay_setChannelVolume(player: Any, channel: Any, level: Any) -> None:
	_player(player).setChannelVolume(_value(channel), _value(level))


@_entryPoint
def wasPlay_startTrimmingLeadingSilence(player: Any, start: Any) -> None:
	_player(player).startTrimmingLeadingSilence(bool(_value(start)))


@_entryPoint
def wasSilence_init(endpointId: str) -> None:
	global _silenceDeviceId
	_silenceDeviceId = _value(endpointId) or ""


@_entryPoint
def wasSilence_playFor(ms: Any, volume: Any) -> None:
	if _silenceDeviceId is not None:
		audioOutput.getSink().keepAwakeFor(_silenceDeviceId, _value(ms), _value(volume))


@_entryPoint
def wasSilence_terminate() -> None:
	global _silenceDeviceId
	_silenceDeviceId = None
	audioOutput.getSink().stopKeepingAwake()


@atexit.register
def _closeForExit() -> None:
	global _isExiting, _silenceDeviceId
	_isExiting = True
	for player in list(_players.values()):
		player.closeForExit()
	if _silenceDeviceId is not None:
		_silenceDeviceId = None
		with contextlib.suppress(OSError):
			audioOutput.getSink().stopKeepingAwake()


def __getattr__(name: str) -> NoReturn:
	missingAttribute(__name__, name)
