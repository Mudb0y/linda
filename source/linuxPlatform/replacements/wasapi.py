"""Linux replacement for wasapi, the audio player nvwave drives.

Upstream binds the player in nvdaHelperLocal.dll, nvdaHelper/local/wasapi.cpp. This keeps its
entry points and their behaviour, which nvwave and every synth driver depend on, and plays to a
linuxPlatform.audioOutput sink instead of a WASAPI endpoint:
- feed blocks while more than half the stream's buffer is still to be played, and returns long
  before its audio has been heard;
- a completion callback runs only on a thread that is inside feed, sync or idle, once playback has
  passed the end of its audio, and never before the feed that registered it has returned;
- stop may be called from any thread, silences the stream and returns at once, without waiting for
  a thread that is feeding, wakes a blocked feeder and discards pending callbacks without calling
  them; a feed that runs while a stop is under way may be cut by it;
- pause and resume keep what is queued and its position;
- a channel volume set while another thread feeds is applied by that thread as soon as it lets go
  of the player, so that nvwave.WavePlayer.stop, which sets the volume of a sounds player, does not
  wait for it either;
- leading silence is trimmed as SilenceDetect does it, with one silent frame put back;
- a stream the sink reports lost is replaced, and no callback is stranded with it;
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
	"""One nvwave player and the sink stream it plays to.

	Three locks, always taken in this order and never held across a wait or a callback:
	- _lock, held by whichever thread feeds, syncs, idles, resumes or gives the stream its volumes,
	  through everything it does but its waits and the callbacks it calls;
	- _runLock, held by stop and pause while they silence the stream, and by a thread taking the
	  stream away to close it, for that moment only, so that nothing silences a closed stream. A
	  feeding thread never holds it while it calls the stream, so stop does not wait for one, which
	  may be held up for whole switch intervals by the GIL whenever other threads run Python;
	- the lock of _wakeCondition, held only to count a wake and to start waiting for one.

	stop and the feeding thread meet only in the stream, whose abort holds until the feeder resets
	the stream, and in _isStopRequested (see stop).
	"""

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
		self._runLock = threading.Lock()
		self._wakeCondition = threading.Condition(threading.Lock())
		# Counts every wake, by the sink or by stop, so that one that comes while a waiter is looking
		# at the stream, before it waits, still ends that wait.
		self._wakeCount = 0
		self._stream: SinkStream | None = None
		self._state = _PlayState.stopped
		# Set by stop for the thread that next feeds, syncs or idles to finish the job.
		self._isStopRequested = False
		# The stream, once a call has found it lost; the next feed replaces it.
		self._lostStream: SinkStream | None = None
		# Each pending callback's feed id and the frame its audio ends at.
		self._feedEnds: list[tuple[int, int]] = []
		self._sentFrames = 0
		self._nextFeedId = 0
		self._isTrimmingLeadingSilence = False
		self._volumes = [1.0] * fmt.channels
		# Set when _volumes changed while another thread held _lock, which then gives them to the stream.
		self._areVolumesPending = False

	@contextlib.contextmanager
	def _locked(self) -> Iterator[None]:
		self._lock.acquire()
		try:
			yield
		finally:
			self._unlock()

	def _unlock(self) -> None:
		self._lock.release()
		self._applyVolumesWhenFree(raising=False)

	def _applyVolumesWhenFree(self, *, raising: bool) -> None:
		"""Give the stream the volumes now, unless another thread holds _lock, which does it as it lets
		go: each thread that lets go looks again, so a volume set at any moment is applied.

		:param raising: Pass a failure on, as the thread that set the volume does; any other thread
			only logs it, since its own call did not fail.
		"""
		while self._areVolumesPending and self._lock.acquire(blocking=False):
			try:
				self._applyVolumes()
			except OSError:
				if raising:
					raise
				log.debugWarning("Couldn't set the volume of the stream", exc_info=True)
			finally:
				self._lock.release()

	def _applyVolumes(self) -> None:
		self._areVolumesPending = False
		stream = self._stream
		if stream is None or self._isLost():
			# A stream is given every volume as it opens.
			return
		try:
			for channel, level in enumerate(self._volumes):
				stream.setChannelVolume(channel, level)
		except StreamLost:
			self._noteLost(stream)

	def _wake(self) -> None:
		with self._wakeCondition:
			self._wakeCount += 1
			self._wakeCondition.notify_all()

	def _open(self) -> None:
		if self._stream is not None or _isExiting:
			return
		if not self.format.isSupported():
			raise WindowsError(
				errno.EINVAL, f"unsupported format {self.format}", None, _AUDCLNT_E_UNSUPPORTED_FORMAT
			)
		stream = audioOutput.getSink().openStream(self.deviceId, self.format, self._wake)
		isLost = False
		try:
			for channel, level in enumerate(self._volumes):
				stream.setChannelVolume(channel, level)
		except StreamLost:
			# Kept, for a feed to replace as it replaces any lost stream.
			isLost = True
		except OSError:
			# The open failed, so the next feed opens a new stream; this one would never be closed.
			with contextlib.suppress(OSError):
				stream.close()
			raise
		# Marked lost before stop can reach it.
		self._lostStream = stream if isLost else None
		self._state = _PlayState.stopped
		self._stream = stream

	def _takeStream(self) -> SinkStream | None:
		"""Take the stream away from the player, so that stop can no longer reach it, and leave the
		player stopped."""
		with self._runLock:
			stream, self._stream = self._stream, None
			self._lostStream = None
			self._state = _PlayState.stopped
		return stream

	def _reopenAfterLoss(self) -> None:
		# Stopped before reopening, so that if no stream can be opened, pause, resume and sync find
		# nothing playing rather than a missing stream.
		lost = self._takeStream()
		pending, self._feedEnds = self._feedEnds, []
		self._sentFrames = 0
		try:
			# The stream is gone already; failing to release it changes nothing.
			if lost is not None:
				with contextlib.suppress(OSError):
					lost.close()
			self._open()
		finally:
			# A callback stranded with the old stream would never be called, and a stranded end of
			# utterance stalls speech, so they are all due now, even when no new stream opened.
			self._call(feedId for feedId, _end in pending)

	def _noteLost(self, stream: SinkStream) -> None:
		if stream is self._stream:
			self._lostStream = stream

	def _isLost(self) -> bool:
		return self._lostStream is not None and self._lostStream is self._stream

	def _bufferedFrames(self) -> int | None:
		"""The frames still to be played, or None when the stream is found lost."""
		try:
			return self._stream.bufferedFrames()
		except StreamLost:
			self._noteLost(self._stream)
			return None

	def _playPos(self) -> int:
		if not self._isLost():
			try:
				return self._stream.playedFrames()
			except StreamLost:
				self._noteLost(self._stream)
		# Treated as finished, so that nothing waits for it and its callbacks are not stranded.
		return self._sentFrames

	def _call(self, feedIds: Iterable[int]) -> None:
		feedIds = list(feedIds)
		if not feedIds:
			return
		# Released so that a callback, or another thread meanwhile, can call stop without waiting.
		self._unlock()
		try:
			for feedId in feedIds:
				if self._isStopRequested:
					# Discarded, as nvwave discards them on stop.
					break
				self.callback(self.handle, feedId)
		finally:
			self._lock.acquire()

	def _maybeFireCallbacks(self) -> None:
		if self._isStopRequested:
			return
		playPos = self._playPos()
		due = [feedId for feedId, end in self._feedEnds if playPos >= end]
		if due:
			self._feedEnds = [(feedId, end) for feedId, end in self._feedEnds if playPos < end]
			self._call(due)

	def _waitUntilNeeded(self, maxWait: float, wakesSeen: int) -> None:
		"""Wait up to maxWait, or until the next callback is due, unless the stream was woken or stop
		was called since the waiter read wakesSeen from _wakeCount, before it looked at the stream."""
		if self._feedEnds:
			untilCallback = self.format.framesToSeconds(self._feedEnds[0][1] - self._playPos())
			maxWait = min(maxWait, untilCallback)
		self._unlock()
		try:
			with self._wakeCondition:
				if self._wakeCount == wakesSeen:
					self._wakeCondition.wait(max(maxWait, _MIN_WAIT))
		finally:
			self._lock.acquire()

	def _completeStop(self) -> None:
		# The flag comes down before the reset that ends any abort, so that a stop whose abort lands
		# after that reset has raised the flag again after it too (see stop).
		self._isStopRequested = False
		stream = self._stream
		if stream is not None and not self._isLost():
			for name, step in (("stop", stream.stop), ("reset", stream.reset)):
				try:
					step()
				except StreamLost:
					self._lostStream = stream
					break
				except OSError:
					# As upstream's completeStop: a stream that could not be reset is unusable or
					# empty anyway, and the player must still start again from nothing.
					log.debugWarning(f"Couldn't {name} the stream", exc_info=True)
		self._state = _PlayState.stopped
		self._nextFeedId = 0
		self._sentFrames = 0
		self._feedEnds = []

	def _continueUnlessStopping(self) -> bool:
		if self._isStopRequested:
			self._completeStop()
			return False
		return True

	def _startUnlessStopping(self) -> bool:
		"""Start the stream if the player is stopped, unless stop has been called meanwhile.

		A stop that lands after the look at the flag aborts the stream, which keeps it silent
		whether the abort comes before the start or after it, and raises the flag that the second
		look, or the next, sees.
		"""
		if not self._isStopRequested and self._state is _PlayState.stopped:
			self._stream.start()
			self._state = _PlayState.playing
		return self._continueUnlessStopping()

	def _takeAsPlayed(self, frames: int) -> None:
		"""Count frames as sent and played on a stream that is lost, so that the callbacks of what
		was fed run at the next feed, sync or idle rather than being stranded."""
		self._sentFrames += frames
		# A lost stream's position is what was sent, so sync finds everything due at once.
		self._state = _PlayState.playing

	def open(self) -> None:
		with self._locked():
			self._open()

	def feed(self, data: bytes | None, size: int, idOut: Any) -> None:
		with self._locked():
			self._open()
			if self._stream is None:
				# The interpreter is exiting, and nothing more is played.
				return
			if self._isStopRequested:
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
			mayReplace = True
			while remainingFrames > 0:
				if not self._continueUnlessStopping():
					return
				if self._isLost():
					if not mayReplace:
						# The stream that replaced a lost one was lost before it took anything.
						self._takeAsPlayed(remainingFrames)
						break
					mayReplace = False
					self._reopenAfterLoss()
					if self._stream is None:
						# The interpreter is exiting.
						return
					continue
				wakesSeen = self._wakeCount
				paddingFrames = self._bufferedFrames()
				if paddingFrames is None:
					continue
				bufferFrames = self._stream.bufferFrames
				if paddingFrames > bufferFrames // 2:
					self._waitUntilNeeded(fmt.framesToSeconds(paddingFrames - bufferFrames // 2), wakesSeen)
					if not self._continueUnlessStopping():
						return
					if self._isLost() or (paddingFrames := self._bufferedFrames()) is None:
						continue
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
				try:
					self._stream.write(chunk)
					if not self._startUnlessStopping():
						return
				except StreamLost:
					# The chunk goes again, whole, to the stream that replaces this one.
					self._noteLost(self._stream)
					continue
				mayReplace = True
				shouldInsertSilentFrame = False
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
		# The flag goes up before the abort, so that a feed about to start the stream sees it, and
		# again after, because the feeder may have taken it down and reset the stream in between,
		# which ends the abort; the feeder must then still find the flag up, rather than play on.
		self._isStopRequested = True
		try:
			with self._runLock:
				stream = self._stream
				if stream is not None and not self._isLost():
					try:
						stream.abort()
					except StreamLost:
						# Nothing is left to silence, and the next feed replaces the stream.
						self._lostStream = stream
			self._isStopRequested = True
		finally:
			self._wake()

	def sync(self) -> None:
		with self._locked():
			self._syncLocked()

	def _syncLocked(self) -> None:
		sentFrames = self._sentFrames
		# Checked on every pass, as a callback releases the lock, and stop or the exit hook may run.
		while self._stream is not None and self._state is _PlayState.playing and not self._isStopRequested:
			wakesSeen = self._wakeCount
			playPos = self._playPos()
			if playPos >= sentFrames:
				# A callback right at the end of the stream.
				self._maybeFireCallbacks()
				return
			self._maybeFireCallbacks()
			self._waitUntilNeeded(self.format.framesToSeconds(sentFrames - playPos), wakesSeen)

	def idle(self) -> None:
		self.sync()
		self.stop()
		with self._locked():
			self._completeStop()

	def pause(self) -> None:
		# Without the player's lock, as it silences the stream as stop does. As in wasapi.cpp, a
		# player not yet playing is left alone, and so is one whose feeder starts it meanwhile.
		with self._runLock:
			stream = self._stream
			if stream is not None and self._state is _PlayState.playing and not self._isLost():
				try:
					stream.stop()
				except StreamLost:
					self._lostStream = stream

	def resume(self) -> None:
		with self._locked():
			stream = self._stream
			if (
				stream is not None
				and self._state is _PlayState.playing
				and not self._isStopRequested
				and not self._isLost()
			):
				try:
					stream.start()
				except StreamLost:
					self._lostStream = stream

	def setChannelVolume(self, channel: int, level: float) -> None:
		if not 0 <= channel < self.format.channels or not 0.0 <= level <= 1.0:
			# As IAudioStreamVolume::SetChannelVolume; nvwave relies on it for mono streams.
			raise WindowsError(errno.EINVAL, "The parameter is incorrect", None, E_INVALIDARG)
		self._volumes[channel] = level
		self._areVolumesPending = True
		self._applyVolumesWhenFree(raising=True)

	def startTrimmingLeadingSilence(self, start: bool) -> None:
		# Without the lock, as upstream's is a plain store: nvwave.WavePlayer.stop calls this right
		# after stop, and must not then wait for the feeding thread.
		self._isTrimmingLeadingSilence = start

	def destroy(self) -> None:
		with self._locked():
			wasLost = self._isLost()
			stream = self._takeStream()
			if stream is None:
				return
			try:
				stream.close()
			except OSError:
				# Raised here it would reach WavePlayer.__del__, which can only print it. A lost stream
				# failing to close is expected; any other is worth a note.
				if not wasLost:
					log.debugWarning("Couldn't close the stream", exc_info=True)

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
			stream = self._takeStream()
			# Stopping, so that a feed or sync now waiting returns as it does after stop().
			self._isStopRequested = True
			# A sync that a callback left with the lock released would otherwise wait on the next end,
			# asking a stream that is gone how far it has played.
			self._feedEnds = []
			self._wake()
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
