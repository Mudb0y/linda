"""A sink that plays through PulseAudio, by way of libpulse.so.0.

Provides an audio output sink for systems running PulseAudio (such as Ubuntu 22.04 LTS),
selected explicitly via LINDA_AUDIO_SINK=pulse (or pulseaudio) or configured via
installation settings.

Stopping speech is immediate: abort() flushes queued audio on the sound server and corks
playback so that no tail of speech lingers after a key press.
"""

import ctypes
import ctypes.util
import errno
import os
import threading
import time
from collections.abc import Callable
from pathlib import Path

from linuxPlatform import installSettings

from . import (
	BUFFER_MS,
	WAVE_FORMAT_IEEE_FLOAT,
	WAVE_FORMAT_PCM,
	NoDevice,
	Sink,
	SinkStream,
	StreamFormat,
	StreamLost,
	checkGain,
	pcm,
)

#: Environment variable to override libpulse library path if needed.
LIBRARY_VARIABLE = "LINDA_PULSE_LIB"

# PulseAudio sample formats
PA_SAMPLE_U8 = 0
PA_SAMPLE_ALAW = 1
PA_SAMPLE_ULAW = 2
PA_SAMPLE_S16LE = 3
PA_SAMPLE_S16BE = 4
PA_SAMPLE_FLOAT32LE = 5
PA_SAMPLE_FLOAT32BE = 6
PA_SAMPLE_S32LE = 7
PA_SAMPLE_S32BE = 8
PA_SAMPLE_S24LE = 9
PA_SAMPLE_S24BE = 10
PA_SAMPLE_S24_32LE = 11
PA_SAMPLE_S24_32BE = 12

# Context states
PA_CONTEXT_UNCONNECTED = 0
PA_CONTEXT_CONNECTING = 1
PA_CONTEXT_AUTHORIZING = 2
PA_CONTEXT_SETTING_NAME = 3
PA_CONTEXT_READY = 4
PA_CONTEXT_FAILED = 5
PA_CONTEXT_TERMINATED = 6

# Context flags
PA_CONTEXT_NOAUTOSPAWN = 0x0001

# Stream states
PA_STREAM_UNCONNECTED = 0
PA_STREAM_CREATING = 1
PA_STREAM_READY = 2
PA_STREAM_FAILED = 3
PA_STREAM_TERMINATED = 4

# Stream flags
PA_STREAM_START_CORKED = 0x0001
PA_STREAM_INTERPOLATE_TIMING = 0x0002
PA_STREAM_NOT_MONOTONIC = 0x0004
PA_STREAM_AUTO_TIMING_UPDATE = 0x0008
PA_STREAM_ADJUST_LATENCY = 0x2000

PA_SEEK_RELATIVE = 0


class pa_sample_spec(ctypes.Structure):
	_fields_ = [
		("format", ctypes.c_int),
		("rate", ctypes.c_uint32),
		("channels", ctypes.c_uint8),
	]


class pa_buffer_attr(ctypes.Structure):
	_fields_ = [
		("maxlength", ctypes.c_uint32),
		("tlength", ctypes.c_uint32),
		("prebuf", ctypes.c_uint32),
		("minreq", ctypes.c_uint32),
		("fragsize", ctypes.c_uint32),
	]


class _Silencers:
	"""pa_stream_cork and pa_stream_flush called through CDLL, releasing the GIL.

	Releasing the GIL while taking pa_threaded_mainloop_lock is essential: holding the GIL
	here would deadlock against Python callbacks (CFUNCTYPE) running on the PulseAudio
	thread that must acquire the GIL while the mainloop lock is held. Sending cork and flush
	to the PulseAudio daemon takes sub-millisecond time.
	"""

	def __init__(self, lib: ctypes.CDLL) -> None:
		self.lock = lib.pa_threaded_mainloop_lock
		self.unlock = lib.pa_threaded_mainloop_unlock
		self.cork = lib.pa_stream_cork
		self.flush = lib.pa_stream_flush
		self.op_unref = lib.pa_operation_unref

		for fn in (self.lock, self.unlock):
			fn.restype = None
			fn.argtypes = [ctypes.c_void_p]
		self.cork.restype = ctypes.c_void_p
		self.cork.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p]
		self.flush.restype = ctypes.c_void_p
		self.flush.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
		self.op_unref.restype = None
		self.op_unref.argtypes = [ctypes.c_void_p]

	def stop(self, mainloop: ctypes.c_void_p, stream: ctypes.c_void_p) -> None:
		if not stream or not mainloop:
			return
		self.lock(mainloop)
		try:
			op = self.cork(stream, 1, None, None)
			if op:
				self.op_unref(op)
		finally:
			self.unlock(mainloop)

	def abort(self, mainloop: ctypes.c_void_p, stream: ctypes.c_void_p) -> None:
		if not stream or not mainloop:
			return
		self.lock(mainloop)
		try:
			op1 = self.cork(stream, 1, None, None)
			if op1:
				self.op_unref(op1)
			op2 = self.flush(stream, None, None)
			if op2:
				self.op_unref(op2)
		finally:
			self.unlock(mainloop)


def _findLibpulse() -> str:
	path = installSettings.get(LIBRARY_VARIABLE) or os.environ.get(LIBRARY_VARIABLE)
	if path and os.path.exists(path):
		return path
	if "LINDA_PULSEAUDIO_BIN" in os.environ:
		binDir = Path(os.environ["LINDA_PULSEAUDIO_BIN"])
		candidate = binDir.parent / "lib" / "libpulse.so.0"
		if candidate.exists():
			return str(candidate)
	found = ctypes.util.find_library("pulse")
	if found:
		return found
	for candidate in [
		"/usr/lib/x86_64-linux-gnu/libpulse.so.0",
		"/usr/lib64/libpulse.so.0",
		"/usr/lib/libpulse.so.0",
		"/lib/x86_64-linux-gnu/libpulse.so.0",
		"/lib64/libpulse.so.0",
	]:
		if os.path.exists(candidate):
			return candidate
	return "libpulse.so.0"


def _loadLibrary() -> ctypes.CDLL:
	path = _findLibpulse()
	try:
		lib = ctypes.CDLL(path)
	except OSError as e:
		raise OSError(errno.ENOENT, f"could not load PulseAudio client library ({path}): {e}") from e

	handle = ctypes.c_void_p

	# Mainloop & Context
	lib.pa_threaded_mainloop_new.restype = handle
	lib.pa_threaded_mainloop_new.argtypes = []
	lib.pa_threaded_mainloop_free.restype = None
	lib.pa_threaded_mainloop_free.argtypes = [handle]
	lib.pa_threaded_mainloop_start.restype = ctypes.c_int
	lib.pa_threaded_mainloop_start.argtypes = [handle]
	lib.pa_threaded_mainloop_stop.restype = None
	lib.pa_threaded_mainloop_stop.argtypes = [handle]
	lib.pa_threaded_mainloop_lock.restype = None
	lib.pa_threaded_mainloop_lock.argtypes = [handle]
	lib.pa_threaded_mainloop_unlock.restype = None
	lib.pa_threaded_mainloop_unlock.argtypes = [handle]
	lib.pa_threaded_mainloop_wait.restype = None
	lib.pa_threaded_mainloop_wait.argtypes = [handle]
	lib.pa_threaded_mainloop_signal.restype = None
	lib.pa_threaded_mainloop_signal.argtypes = [handle, ctypes.c_int]
	lib.pa_threaded_mainloop_get_api.restype = handle
	lib.pa_threaded_mainloop_get_api.argtypes = [handle]

	lib.pa_context_new.restype = handle
	lib.pa_context_new.argtypes = [handle, ctypes.c_char_p]
	lib.pa_context_unref.restype = None
	lib.pa_context_unref.argtypes = [handle]
	lib.pa_context_set_state_callback.restype = None
	lib.pa_context_set_state_callback.argtypes = [handle, ctypes.c_void_p, ctypes.c_void_p]
	lib.pa_context_connect.restype = ctypes.c_int
	lib.pa_context_connect.argtypes = [handle, ctypes.c_char_p, ctypes.c_int, ctypes.c_void_p]
	lib.pa_context_disconnect.restype = None
	lib.pa_context_disconnect.argtypes = [handle]
	lib.pa_context_get_state.restype = ctypes.c_int
	lib.pa_context_get_state.argtypes = [handle]

	# Stream
	lib.pa_stream_new.restype = handle
	lib.pa_stream_new.argtypes = [handle, ctypes.c_char_p, ctypes.POINTER(pa_sample_spec), ctypes.c_void_p]
	lib.pa_stream_unref.restype = None
	lib.pa_stream_unref.argtypes = [handle]
	lib.pa_stream_set_state_callback.restype = None
	lib.pa_stream_set_state_callback.argtypes = [handle, ctypes.c_void_p, ctypes.c_void_p]
	lib.pa_stream_set_write_callback.restype = None
	lib.pa_stream_set_write_callback.argtypes = [handle, ctypes.c_void_p, ctypes.c_void_p]
	lib.pa_stream_connect_playback.restype = ctypes.c_int
	lib.pa_stream_connect_playback.argtypes = [
		handle,
		ctypes.c_char_p,
		ctypes.POINTER(pa_buffer_attr),
		ctypes.c_int,
		ctypes.c_void_p,
		ctypes.c_void_p,
	]
	lib.pa_stream_disconnect.restype = ctypes.c_int
	lib.pa_stream_disconnect.argtypes = [handle]
	lib.pa_stream_get_state.restype = ctypes.c_int
	lib.pa_stream_get_state.argtypes = [handle]
	lib.pa_stream_write.restype = ctypes.c_int
	lib.pa_stream_write.argtypes = [
		handle,
		ctypes.c_void_p,
		ctypes.c_size_t,
		ctypes.c_void_p,
		ctypes.c_int64,
		ctypes.c_int,
	]
	lib.pa_stream_writable_size.restype = ctypes.c_size_t
	lib.pa_stream_writable_size.argtypes = [handle]
	lib.pa_stream_cork.restype = handle
	lib.pa_stream_cork.argtypes = [handle, ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p]
	lib.pa_stream_flush.restype = handle
	lib.pa_stream_flush.argtypes = [handle, ctypes.c_void_p, ctypes.c_void_p]
	lib.pa_stream_get_time.restype = ctypes.c_int
	lib.pa_stream_get_time.argtypes = [handle, ctypes.POINTER(ctypes.c_uint64)]
	lib.pa_operation_unref.restype = None
	lib.pa_operation_unref.argtypes = [handle]

	return lib


def _toPulseFormat(fmt: StreamFormat) -> int:
	if fmt.formatTag == WAVE_FORMAT_PCM:
		if fmt.bitsPerSample == 8:
			return PA_SAMPLE_U8
		if fmt.bitsPerSample == 16:
			return PA_SAMPLE_S16LE
		if fmt.bitsPerSample == 24:
			return PA_SAMPLE_S24LE
		if fmt.bitsPerSample == 32:
			return PA_SAMPLE_S32LE
	elif fmt.formatTag == WAVE_FORMAT_IEEE_FLOAT:
		if fmt.bitsPerSample == 32:
			return PA_SAMPLE_FLOAT32LE
	raise ValueError(f"PulseAudio sink does not support {fmt}")


class PulseStream(SinkStream):
	def __init__(
		self,
		sink: "PulseSink",
		lib: ctypes.CDLL,
		silencers: _Silencers,
		streamHandle: ctypes.c_void_p,
		fmt: StreamFormat,
		wake: Callable[[], None],
	) -> None:
		self._sink = sink
		self._lib = lib
		self._silencers = silencers
		self.handle = streamHandle
		self.format = fmt
		self._wake = wake
		self.bufferFrames = fmt.samplesPerSec * BUFFER_MS // 1000
		self.volumes = [1.0] * fmt.channels

		self._writtenFrames = 0
		self._playedHigh = 0
		self._playBaseUsec: int | None = None
		self._isStarted = False
		self._isAborted = False
		self._startTime: float | None = None
		self._closed = False

	def _checkLive(self) -> ctypes.c_void_p:
		if self._closed or not self.handle:
			raise OSError(errno.EBADF, "the stream is closed")
		if not self._sink.mainloop:
			raise StreamLost("PulseAudio mainloop is not running")
		self._lib.pa_threaded_mainloop_lock(self._sink.mainloop)
		try:
			if self._sink.context:
				ctxState = self._lib.pa_context_get_state(self._sink.context)
				if ctxState in (PA_CONTEXT_FAILED, PA_CONTEXT_TERMINATED):
					raise StreamLost("PulseAudio context failed or terminated")
			state = self._lib.pa_stream_get_state(self.handle)
			if state in (PA_STREAM_FAILED, PA_STREAM_TERMINATED):
				raise StreamLost("PulseAudio stream terminated or failed")
		finally:
			self._lib.pa_threaded_mainloop_unlock(self._sink.mainloop)
		return self.handle

	def write(self, data: bytes) -> None:
		handle = self._checkLive()
		if self._isAborted:
			return
		frames = len(data) // self.format.blockAlign
		# Scale channel volumes and process gain
		effectiveVolumes = [v * self._sink.gain for v in self.volumes]
		data = pcm.applyGain(self.format, data, effectiveVolumes)

		self._lib.pa_threaded_mainloop_lock(self._sink.mainloop)
		try:
			if self._isAborted:
				return
			writable = self._lib.pa_stream_writable_size(handle)
			if writable == ctypes.c_size_t(-1).value:
				raise StreamLost("PulseAudio stream is disconnected or failed")
			if len(data) > writable:
				raise OSError(errno.ENOBUFS, "the stream's buffer is full")
			result = self._lib.pa_stream_write(handle, data, len(data), None, 0, PA_SEEK_RELATIVE)
			if result < 0:
				raise StreamLost(f"pa_stream_write failed with code {result}")
		finally:
			self._lib.pa_threaded_mainloop_unlock(self._sink.mainloop)

		self._writtenFrames += frames
		self._sink._noteDelivered(frames)

	def bufferedFrames(self) -> int:
		self._checkLive()
		return max(0, self._writtenFrames - self.playedFrames())

	def playedFrames(self) -> int:
		if self._closed or not self.handle:
			return self._playedHigh
		self._checkLive()
		if not self._isStarted:
			return self._playedHigh

		usec = ctypes.c_uint64(0)
		self._lib.pa_threaded_mainloop_lock(self._sink.mainloop)
		try:
			res = self._lib.pa_stream_get_time(self.handle, ctypes.byref(usec))
		finally:
			self._lib.pa_threaded_mainloop_unlock(self._sink.mainloop)

		if res == 0:
			if self._playBaseUsec is None:
				if self._startTime is not None:
					elapsedSec = time.monotonic() - self._startTime
					self._playBaseUsec = max(0, usec.value - int(elapsedSec * 1_000_000))
				else:
					self._playBaseUsec = usec.value
			elapsedUsec = max(0, usec.value - self._playBaseUsec)
			frames = int(elapsedUsec * self.format.samplesPerSec / 1_000_000)
			self._playedHigh = min(self._writtenFrames, max(self._playedHigh, frames))
		elif self._startTime is not None:
			# Interpolate by monotonic clock if time query is still settling
			elapsed = time.monotonic() - self._startTime
			elapsedFrames = int(elapsed * self.format.samplesPerSec)
			self._playedHigh = min(self._writtenFrames, max(self._playedHigh, elapsedFrames))

		return self._playedHigh

	def start(self) -> None:
		handle = self._checkLive()
		if self._isAborted or self._isStarted:
			return
		self._lib.pa_threaded_mainloop_lock(self._sink.mainloop)
		try:
			if self._isAborted:
				return
			op = self._lib.pa_stream_cork(handle, 0, None, None)
			if op:
				self._lib.pa_operation_unref(op)
			usec = ctypes.c_uint64(0)
			if self._lib.pa_stream_get_time(handle, ctypes.byref(usec)) == 0:
				self._playBaseUsec = usec.value
			else:
				self._playBaseUsec = None
			self._isStarted = True
			self._startTime = time.monotonic()
		finally:
			self._lib.pa_threaded_mainloop_unlock(self._sink.mainloop)

	def stop(self) -> None:
		self._isStarted = False
		handle = self.handle
		if handle and not self._closed and self._sink.mainloop:
			self._silencers.stop(self._sink.mainloop, handle)

	def abort(self) -> None:
		self._isAborted = True
		self._isStarted = False
		handle = self.handle
		if handle and not self._closed and self._sink.mainloop:
			self._silencers.abort(self._sink.mainloop, handle)

	def reset(self) -> None:
		handle = self.handle
		if handle and not self._closed and self._sink.mainloop:
			self._silencers.abort(self._sink.mainloop, handle)
		self._isAborted = False
		self._isStarted = False
		self._writtenFrames = 0
		self._playedHigh = 0
		self._playBaseUsec = None
		self._startTime = None

	def setChannelVolume(self, channel: int, level: float) -> None:
		if not 0 <= channel < len(self.volumes):
			raise OSError(errno.EINVAL, f"invalid channel {channel}")
		self.volumes[channel] = level

	def close(self) -> None:
		if self._closed:
			return
		self._closed = True
		handle, self.handle = self.handle, None
		if handle and self._sink.mainloop:
			self._silencers.abort(self._sink.mainloop, handle)
			self._lib.pa_threaded_mainloop_lock(self._sink.mainloop)
			try:
				self._lib.pa_stream_set_state_callback(handle, None, None)
				self._lib.pa_stream_disconnect(handle)
				self._lib.pa_stream_unref(handle)
			finally:
				self._lib.pa_threaded_mainloop_unlock(self._sink.mainloop)


class PulseSink(Sink):
	kind = "pulse"
	description = "PulseAudio, through libpulse"

	def __init__(self) -> None:
		self.lib = None
		self.mainloop = None
		self.context = None
		self.api = None
		self._silencers = None
		self.gain = 1.0
		self._deliveredFrames = 0
		self._lock = threading.Lock()
		self._contextCallback = None

		self.lib = _loadLibrary()
		self._silencers = _Silencers(self.lib)

		self.mainloop = self.lib.pa_threaded_mainloop_new()
		if not self.mainloop:
			raise OSError(errno.ENOMEM, "could not create PulseAudio threaded mainloop")

		self.api = self.lib.pa_threaded_mainloop_get_api(self.mainloop)
		self._contextCallback = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p)(self._onContextState)

		if self.lib.pa_threaded_mainloop_start(self.mainloop) < 0:
			self._cleanup()
			raise OSError(errno.EIO, "could not start PulseAudio mainloop")

		self.lib.pa_threaded_mainloop_lock(self.mainloop)
		try:
			self._ensureReadyContext()
		except BaseException:
			self.lib.pa_threaded_mainloop_unlock(self.mainloop)
			self._cleanup()
			raise
		self.lib.pa_threaded_mainloop_unlock(self.mainloop)

	def _ensureReadyContext(self) -> None:
		if self.context:
			state = self.lib.pa_context_get_state(self.context)
			if state == PA_CONTEXT_READY:
				return
			self.lib.pa_context_set_state_callback(self.context, None, None)
			self.lib.pa_context_disconnect(self.context)
			self.lib.pa_context_unref(self.context)
			self.context = None

		self.context = self.lib.pa_context_new(self.api, b"Linda")
		if not self.context:
			raise OSError(errno.ENOMEM, "could not create PulseAudio context")

		self.lib.pa_context_set_state_callback(self.context, self._contextCallback, None)

		if self.lib.pa_context_connect(self.context, None, PA_CONTEXT_NOAUTOSPAWN, None) < 0:
			self.lib.pa_context_set_state_callback(self.context, None, None)
			self.lib.pa_context_unref(self.context)
			self.context = None
			raise NoDevice("could not connect to PulseAudio daemon")

		while True:
			state = self.lib.pa_context_get_state(self.context)
			if state == PA_CONTEXT_READY:
				break
			if state in (PA_CONTEXT_FAILED, PA_CONTEXT_TERMINATED):
				self.lib.pa_context_set_state_callback(self.context, None, None)
				self.lib.pa_context_disconnect(self.context)
				self.lib.pa_context_unref(self.context)
				self.context = None
				raise NoDevice("PulseAudio daemon connection failed or terminated")
			self.lib.pa_threaded_mainloop_wait(self.mainloop)

	def _onContextState(self, context: ctypes.c_void_p, userdata: ctypes.c_void_p) -> None:
		self.lib.pa_threaded_mainloop_signal(self.mainloop, 0)

	def _cleanup(self) -> None:
		if getattr(self, "mainloop", None) and self.lib:
			if getattr(self, "context", None):
				self.lib.pa_threaded_mainloop_lock(self.mainloop)
				try:
					self.lib.pa_context_set_state_callback(self.context, None, None)
					self.lib.pa_context_disconnect(self.context)
					self.lib.pa_context_unref(self.context)
					self.context = None
				finally:
					self.lib.pa_threaded_mainloop_unlock(self.mainloop)
			self.lib.pa_threaded_mainloop_stop(self.mainloop)
			self.lib.pa_threaded_mainloop_free(self.mainloop)
			self.mainloop = None
		elif getattr(self, "context", None) and self.lib:
			self.lib.pa_context_set_state_callback(self.context, None, None)
			self.lib.pa_context_disconnect(self.context)
			self.lib.pa_context_unref(self.context)
			self.context = None

	def openStream(self, deviceId: str, fmt: StreamFormat, wake: Callable[[], None]) -> SinkStream:
		pulseFormat = _toPulseFormat(fmt)
		spec = pa_sample_spec(format=pulseFormat, rate=fmt.samplesPerSec, channels=fmt.channels)

		targetSink = deviceId.encode() if deviceId else None

		streamHandle = None
		self.lib.pa_threaded_mainloop_lock(self.mainloop)
		try:
			self._ensureReadyContext()
			streamHandle = self.lib.pa_stream_new(self.context, b"speech", ctypes.byref(spec), None)
			if not streamHandle:
				raise OSError(errno.ENOMEM, "could not create PulseAudio stream")

			streamStateCb = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p)(self._onStreamState)
			self.lib.pa_stream_set_state_callback(streamHandle, streamStateCb, None)

			# Target latency and buffer match BUFFER_MS (400 ms); maxlength default (-1)
			tlengthBytes = fmt.samplesPerSec * BUFFER_MS // 1000 * fmt.blockAlign
			attr = pa_buffer_attr(
				maxlength=ctypes.c_uint32(-1).value,
				tlength=tlengthBytes,
				prebuf=0,
				minreq=ctypes.c_uint32(-1).value,
				fragsize=ctypes.c_uint32(-1).value,
			)

			flags = (
				PA_STREAM_START_CORKED
				| PA_STREAM_INTERPOLATE_TIMING
				| PA_STREAM_AUTO_TIMING_UPDATE
				| PA_STREAM_ADJUST_LATENCY
			)

			res = self.lib.pa_stream_connect_playback(
				streamHandle, targetSink, ctypes.byref(attr), flags, None, None
			)
			if res < 0:
				raise OSError(errno.EIO, f"PulseAudio connect playback failed: {res}")

			while True:
				state = self.lib.pa_stream_get_state(streamHandle)
				if state == PA_STREAM_READY:
					break
				if state in (PA_STREAM_FAILED, PA_STREAM_TERMINATED):
					raise NoDevice("PulseAudio stream failed to initialize")
				self.lib.pa_threaded_mainloop_wait(self.mainloop)
		except BaseException:
			if streamHandle:
				self.lib.pa_stream_set_state_callback(streamHandle, None, None)
				self.lib.pa_stream_disconnect(streamHandle)
				self.lib.pa_stream_unref(streamHandle)
			raise
		finally:
			self.lib.pa_threaded_mainloop_unlock(self.mainloop)

		stream = PulseStream(self, self.lib, self._silencers, streamHandle, fmt, wake)
		# Keep stream callback references alive on stream object
		stream._streamStateCb = streamStateCb
		return stream

	def _onStreamState(self, stream: ctypes.c_void_p, userdata: ctypes.c_void_p) -> None:
		self.lib.pa_threaded_mainloop_signal(self.mainloop, 0)

	def devices(self) -> list[tuple[str, str]]:
		# Default output device, which nvwave names itself.
		return []

	def keepAwakeFor(self, deviceId: str, ms: int, noiseVolume: float) -> None:
		pass

	def stopKeepingAwake(self) -> None:
		pass

	def setGain(self, level: float) -> None:
		checkGain(level)
		self.gain = level

	def deliveredFrames(self) -> int:
		with self._lock:
			return self._deliveredFrames

	def _noteDelivered(self, frames: int) -> None:
		with self._lock:
			self._deliveredFrames += frames

	def __del__(self) -> None:
		self._cleanup()
