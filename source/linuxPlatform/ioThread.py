"""The background I/O thread of hwIo, for Linux.

Upstream's thread sleeps in an alertable wait and is woken by Windows to run asynchronous
procedure calls, which is how functions are queued to it and how its waitable timers fire. Linux
has neither, so this thread waits on a condition instead, sleeping until a function is queued or
the earliest timer falls due, and never spins. What it runs, and how, stays upstream's: each
function goes into upstream's store as a weak reference and is run through upstream's own
dispatcher, which skips functions that have died and logs those that raise.
"""

import threading
import time

from hwIo import ioThread as upstream
from logHandler import getFormattedStacksForAllThreads, log
from serial.win32 import OVERLAPPED

from linuxPlatform import NoLinuxImplementation


class WaitableTimer:
	"""A one-shot timer that runs a function on an IoThread, where Windows uses a waitable timer handle.

	It is armed by IoThread.setWaitableTimer. Arming it again replaces what was pending, as setting
	a Windows waitable timer again does.
	"""

	def __init__(self) -> None:
		self._thread: IoThread | None = None

	def cancel(self) -> bool:
		"""Drop the pending call, if any.

		A call the thread has already taken runs. Unlike on Windows, where a timer that has fallen due
		has already queued its APC, a call that is due but still waiting for a busy thread is dropped.

		:return: True, as CancelWaitableTimer returns non-zero for a timer that is not armed.
		"""
		thread = self._thread
		if thread is not None:
			thread._cancelTimer(self)
		return True

	def close(self) -> None:
		self.cancel()


class IoThread(upstream.IoThread):
	"""hwIo's background thread, with upstream's public behaviour, for Linux."""

	def __init__(self) -> None:
		super().__init__()
		self._wake = threading.Condition()
		#: Store keys of functions queued to run, in the order they were queued.
		self._queued: list[upstream.ApcIdT] = []
		#: For each armed timer, when it falls due on the monotonic clock and its store key.
		self._timers: dict[WaitableTimer, tuple[float, upstream.ApcIdT]] = {}

	def start(self) -> None:
		# Upstream opens a Windows handle to the thread here, which only queuing an APC needs.
		threading.Thread.start(self)

	def queueAsApc(self, func: upstream.ApcT, param: int = 0) -> None:
		"""Queue func to run on this thread with param, holding func only weakly, as upstream does."""
		internalParam = self._registerToCallAsApc(func, param)
		with self._wake:
			self._queued.append(internalParam)
			self._wake.notify()

	def setWaitableTimer(
		self,
		handle: WaitableTimer,
		dueTime: int,
		func: upstream.ApcT,
		param: int = 0,
	) -> None:
		"""Run func with param on this thread once dueTime milliseconds have passed.

		:param handle: A WaitableTimer; arming it again replaces the call it had pending.
		"""
		if not isinstance(handle, WaitableTimer):
			raise TypeError(f"a Linux waitable timer is a WaitableTimer, not {handle!r}")
		internalParam = self._registerToCallAsApc(func, param)
		due = time.monotonic() + max(dueTime, 0) / 1000
		previous = handle._thread
		if previous is not None and previous is not self:
			previous._cancelTimer(handle)
		with self._wake:
			replaced = self._timers.get(handle)
			if replaced is not None:
				upstream.IoThread._apcStore.pop(replaced[1], None)
			self._timers[handle] = (due, internalParam)
			handle._thread = self
			self._wake.notify()

	def _cancelTimer(self, handle: WaitableTimer) -> None:
		with self._wake:
			pending = self._timers.pop(handle, None)
			if handle._thread is self:
				handle._thread = None
		if pending is not None:
			upstream.IoThread._apcStore.pop(pending[1], None)

	def queueAsCompletionRoutine(self, func: upstream.CompletionRoutineT, overlapped: OVERLAPPED):
		# Overlapped I/O completion is how Windows finishes reads from serial and HID devices.
		raise NoLinuxImplementation("hwIo.ioThread.IoThread.queueAsCompletionRoutine")

	def stop(self, timeout: float | None = None) -> None:
		"""Stop the thread once it has run every function already queued; pending timers never fire."""
		if not self.is_alive():
			raise RuntimeError("Thread is not running")
		with self._wake:
			self.exit = True
			self._wake.notify()
		self.join(timeout)
		self.exit = False

	def _takeReady(self) -> list[upstream.ApcIdT] | None:
		"""Wait until something is ready, then take it all; None once the thread is to exit."""
		with self._wake:
			while True:
				ready = self._queued
				self._queued = []
				now = time.monotonic()
				due = sorted((when, key, timer) for timer, (when, key) in self._timers.items() if when <= now)
				for _when, key, timer in due:
					del self._timers[timer]
					timer._thread = None
					ready.append(key)
				if ready:
					return ready
				if self.exit:
					return None
				nextDue = min((when for when, _key in self._timers.values()), default=None)
				self._wake.wait(None if nextDue is None else nextDue - now)

	def run(self) -> None:
		try:
			while (ready := self._takeReady()) is not None:
				for internalParam in ready:
					upstream.IoThread._internalApc(internalParam)
		except Exception:
			log.critical("Exception in IoThread function", exc_info=True)
			stacks = getFormattedStacksForAllThreads()
			log.info(f"Listing stacks for Python threads after IoThread crash:\n{stacks}")
		finally:
			with self._wake:
				abandoned = self._queued + [key for _when, key in self._timers.values()]
				for timer in self._timers:
					timer._thread = None
				self._queued = []
				self._timers.clear()
			for internalParam in abandoned:
				upstream.IoThread._apcStore.pop(internalParam, None)
