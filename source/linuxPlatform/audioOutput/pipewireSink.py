"""A sink that plays through PipeWire, by way of libpwsink from engine/crates/pwsink.

libpwsink keeps one PipeWire connection for the process and gives each player a stream, which its
real-time callback fills from a ring on PipeWire's own thread; no Python ever runs there. Nothing
called from here waits for PipeWire, its daemon or the library's own thread, so a player's stop
silences it within one graph cycle even while another thread is blocked in a feed, and opening a
stream returns at once, from a pool of closed streams when one of the same format is waiting.

The library keeps time by the wall clock whenever the graph stops consuming a stream that holds
audio: when no session manager links it, its only sink is removed, or the daemon freezes or dies.
So a feed, a sync, and the idle check nvwave runs on NVDA's main thread take no longer than the
audio would have, and every completion callback fires. It also connects again by itself, every
second, while the daemon is away. A missing daemon is therefore never a missing device: nvwave
drops the callbacks of a feed that fails, and a stranded end-of-utterance callback stalls speech.
deliveredFrames is how a caller tells audio heard from audio only timed.
"""

import ctypes
import errno
import os
from collections.abc import Callable

from . import BUFFER_MS, Sink, SinkStream, StreamFormat

#: Names the libpwsink the sink loads.
LIBRARY_VARIABLE = "BASEDREADER_PWSINK_LIB"


def _load() -> ctypes.CDLL:
	path = os.environ.get(LIBRARY_VARIABLE, "")
	if not path:
		raise OSError(errno.ENOENT, f"{LIBRARY_VARIABLE} does not name libpwsink")
	# CDLL rather than PyDLL, so that the GIL is released during every call: the calls never wait,
	# and keeping the GIL through them was measured to gain nothing.
	lib = ctypes.CDLL(path)
	handle = ctypes.c_void_p
	lib.pwsink_init.restype = ctypes.c_int
	lib.pwsink_init.argtypes = []
	lib.pwsink_open.restype = handle
	lib.pwsink_open.argtypes = [ctypes.c_uint32] * 4 + [ctypes.c_char_p, ctypes.POINTER(ctypes.c_int)]
	lib.pwsink_write.restype = ctypes.c_uint64
	lib.pwsink_write.argtypes = [handle, ctypes.c_char_p, ctypes.c_size_t]
	for name in ("buffered", "played", "process_calls", "resampler_frames"):
		function = getattr(lib, f"pwsink_{name}")
		function.restype = ctypes.c_uint64
		function.argtypes = [handle]
	for name in ("start", "stop", "reset", "close"):
		function = getattr(lib, f"pwsink_{name}")
		function.restype = None
		function.argtypes = [handle]
	lib.pwsink_is_active.restype = ctypes.c_int
	lib.pwsink_is_active.argtypes = [handle]
	lib.pwsink_set_volume.restype = ctypes.c_int
	lib.pwsink_set_volume.argtypes = [handle, ctypes.c_uint32, ctypes.c_float]
	lib.pwsink_set_gain.restype = ctypes.c_int
	lib.pwsink_set_gain.argtypes = [ctypes.c_float]
	for name in ("delivered", "pool_size", "attached_streams"):
		function = getattr(lib, f"pwsink_{name}")
		function.restype = ctypes.c_uint64
		function.argtypes = []
	lib.pwsink_connected.restype = ctypes.c_int
	lib.pwsink_connected.argtypes = []
	lib.pwsink_keep_awake.restype = None
	lib.pwsink_keep_awake.argtypes = [ctypes.c_uint64]
	lib.pwsink_set_pool_limits.restype = None
	lib.pwsink_set_pool_limits.argtypes = [ctypes.c_uint64, ctypes.c_uint64]
	lib.pwsink_block_loop.restype = None
	lib.pwsink_block_loop.argtypes = [ctypes.c_uint64]
	return lib


class PipewireStream(SinkStream):
	def __init__(self, lib: ctypes.CDLL, handle: int, fmt: StreamFormat) -> None:
		self._lib = lib
		#: libpwsink's handle, None once closed.
		self.handle: int | None = handle
		self.format = fmt
		self.bufferFrames = fmt.samplesPerSec * BUFFER_MS // 1000

	def write(self, data: bytes) -> None:
		frames = len(data) // self.format.blockAlign
		if self._lib.pwsink_write(self.handle, data, len(data)) != frames:
			raise OSError(errno.ENOBUFS, "the stream's ring is full")

	def bufferedFrames(self) -> int:
		return self._lib.pwsink_buffered(self.handle)

	def playedFrames(self) -> int:
		return self._lib.pwsink_played(self.handle)

	def start(self) -> None:
		self._lib.pwsink_start(self.handle)

	def stop(self) -> None:
		self._lib.pwsink_stop(self.handle)

	def reset(self) -> None:
		self._lib.pwsink_reset(self.handle)

	def setChannelVolume(self, channel: int, level: float) -> None:
		if self._lib.pwsink_set_volume(self.handle, channel, level) < 0:
			raise OSError(errno.EINVAL, f"no volume {level} for channel {channel} of {self.format}")

	def close(self) -> None:
		handle, self.handle = self.handle, None
		if handle:
			self._lib.pwsink_close(handle)


class PipewireSink(Sink):
	kind = "pipewire"
	description = "PipeWire, through libpwsink"

	def __init__(self) -> None:
		self.lib = _load()
		result = self.lib.pwsink_init()
		if result < 0:
			raise OSError(-result, f"libpwsink could not start: {os.strerror(-result)}")
		self._noiseNoted = False

	def openStream(self, deviceId: str, fmt: StreamFormat, wake: Callable[[], None]) -> SinkStream:
		# wake is never needed: the library keeps every stream's position moving in real time, so
		# the player's own timed waits always end.
		error = ctypes.c_int(0)
		handle = self.lib.pwsink_open(
			fmt.formatTag,
			fmt.channels,
			fmt.samplesPerSec,
			fmt.bitsPerSample,
			deviceId.encode() or None,
			ctypes.byref(error),
		)
		if not handle:
			raise OSError(-error.value, f"PipeWire is not given {fmt}: {os.strerror(-error.value)}")
		return PipewireStream(self.lib, handle, fmt)

	def devices(self) -> list[tuple[str, str]]:
		# Only the default device, which nvwave lists itself. WirePlumber moves the streams when the
		# default changes.
		return []

	def keepAwakeFor(self, deviceId: str, ms: int, noiseVolume: float) -> None:
		if noiseVolume > 0 and not self._noiseNoted:
			from logHandler import log

			log.debug("The PipeWire sink keeps the device awake with silence, not white noise")
			self._noiseNoted = True
		self.lib.pwsink_keep_awake(max(0, int(ms)))

	def stopKeepingAwake(self) -> None:
		self.lib.pwsink_keep_awake(0)

	def setGain(self, level: float) -> None:
		if self.lib.pwsink_set_gain(level) < 0:
			raise ValueError(f"a gain of {level} is not between 0 and 1")

	def deliveredFrames(self) -> int:
		return self.lib.pwsink_delivered()
