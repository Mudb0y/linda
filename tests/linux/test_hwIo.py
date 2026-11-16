"""hwIo on Linux: the background thread's queued functions, waitable timers and clean stop, and no device opened.

The first test is upstream's tests/unit/test_hwIo.py, unchanged in substance; the rest hold the
Linux thread to the behaviour upstream's Windows thread has and its callers rely on. Run under the rig:

	nix develop . -c tests/linux/rig python3 -m pytest -p no:cacheprovider tests/linux/test_hwIo.py
"""

import textwrap
from pathlib import Path

import harness

#: Enters NVDA's import cycle where core.main does, and starts the thread.
PRELUDE = """
import threading
import time

import NVDAHelper  # noqa: F401
import hwIo
from hwIo.ioThread import IoThread

#: The longest any wait for an event may take. A wait returns as soon as its event is set, so this
#: is generous: it only has to outlast a heavily loaded machine.
WAIT = 60

hwIo.initialize()
"""


def runWithThread(tmpPath: Path, body: str) -> harness.ChildResult:
	return harness.runChild(tmpPath, PRELUDE + textwrap.dedent(body))


def test_queuedFunctionRunsWithItsParam(tmp_path: Path):
	done = runWithThread(
		tmp_path,
		"""
		event = threading.Event()
		received = []

		def apc(param: int) -> None:
			received.append(param)
			event.set()

		assert not event.is_set()
		hwIo.bgThread.queueAsApc(apc, 42)
		result = {"ran": event.wait(WAIT), "received": received}
		hwIo.terminate()
		""",
	)
	done.assertClean()
	assert done.value == {"ran": True, "received": [42]}


def test_queuedFunctionsRunInOrderOnTheThread(tmp_path: Path):
	done = runWithThread(
		tmp_path,
		"""
		calls = []
		finished = threading.Event()

		def record(param: int) -> None:
			current = threading.current_thread()
			calls.append((param, isinstance(current, IoThread), current is hwIo.bgThread))
			if param == 4:
				finished.set()

		for param in range(5):
			hwIo.bgThread.queueAsApc(record, param)
		result = {"finished": finished.wait(WAIT), "calls": calls}
		hwIo.terminate()
		""",
	)
	done.assertClean()
	assert done.value["finished"]
	assert done.value["calls"] == [[param, True, True] for param in range(5)]


def test_threadsQueueingAtOnceGetDistinctKeys(tmp_path: Path):
	done = runWithThread(
		tmp_path,
		"""
		import sys

		def apc(param: int) -> None:
			pass

		# The braille handler queues from the main thread while the I/O thread arms its timer.
		# Switching as often as the interpreter allows makes two registrations overlap.
		perThread = 20_000
		keys = [[], []]
		errors = []
		together = threading.Barrier(2)

		def register(into: list) -> None:
			together.wait()
			try:
				for _ in range(perThread):
					into.append(hwIo.bgThread._registerToCallAsApc(apc))
			except Exception as e:
				errors.append(repr(e))

		workers = [threading.Thread(target=register, args=(into,)) for into in keys]
		sys.setswitchinterval(1e-6)
		try:
			for worker in workers:
				worker.start()
			for worker in workers:
				worker.join()
		finally:
			sys.setswitchinterval(0.005)
		allKeys = keys[0] + keys[1]
		for key in allKeys:
			IoThread._apcStore.pop(key, None)
		result = {"errors": errors[:3], "count": len(allKeys), "distinct": len(set(allKeys))}
		hwIo.terminate()
		""",
	)
	done.assertClean()
	assert done.value == {"errors": [], "count": 40_000, "distinct": 40_000}


def test_queuedKeysWrapAt32Bits(tmp_path: Path):
	done = runWithThread(
		tmp_path,
		"""
		import itertools

		def apc(param: int) -> None:
			pass

		IoThread._apcParamCounter = itertools.count(0xFFFFFFFF)
		keys = [hwIo.bgThread._registerToCallAsApc(apc) for _ in range(2)]
		for key in keys:
			IoThread._apcStore.pop(key, None)
		result = keys
		hwIo.terminate()
		""",
	)
	done.assertClean()
	# A Windows APC's parameter is a ULONG_PTR, which holds 32 bits.
	assert done.value == [0xFFFFFFFF, 0]


def test_functionThatDiedBeforeRunningIsSkipped(tmp_path: Path):
	done = runWithThread(
		tmp_path,
		"""
		import gc

		release = threading.Event()
		ran = []
		after = threading.Event()

		def blocker(param: int) -> None:
			release.wait(WAIT)

		def last(param: int) -> None:
			after.set()

		def doomed(param: int) -> None:
			ran.append(param)

		hwIo.bgThread.queueAsApc(blocker)
		hwIo.bgThread.queueAsApc(doomed, 1)
		hwIo.bgThread.queueAsApc(last)
		# The thread holds queued functions weakly, as upstream's does.
		del doomed
		gc.collect()
		release.set()
		result = {"after": after.wait(WAIT), "ran": ran}
		hwIo.terminate()
		""",
	)
	done.assertClean()
	assert done.value == {"after": True, "ran": []}


def test_functionThatRaisesIsLoggedAndTheThreadCarriesOn(tmp_path: Path):
	done = runWithThread(
		tmp_path,
		"""
		after = threading.Event()

		def broken(param: int) -> None:
			raise ValueError("expected by the test")

		def next_(param: int) -> None:
			after.set()

		hwIo.bgThread.queueAsApc(broken)
		hwIo.bgThread.queueAsApc(next_)
		result = after.wait(WAIT)
		hwIo.terminate()
		""",
	)
	assert done.error is None, done.error
	assert done.value is True
	assert not done.noLinuxImplementation
	assert len(done.logErrors) == 1
	assert done.logErrors[0].startswith("ERROR: Error in APC function")


def test_timerFiresOnTheThreadAfterItsDueTime(tmp_path: Path):
	done = runWithThread(
		tmp_path,
		"""
		from linuxPlatform.ioThread import WaitableTimer

		timer = WaitableTimer()
		fired = threading.Event()
		seen = {}

		def onTimer(param: int) -> None:
			seen["param"] = param
			seen["elapsed"] = time.monotonic() - armed
			seen["onThread"] = threading.current_thread() is hwIo.bgThread
			fired.set()

		armed = time.monotonic()
		hwIo.bgThread.setWaitableTimer(timer, 150, onTimer, 7)
		result = {"fired": fired.wait(WAIT), **seen}
		hwIo.terminate()
		""",
	)
	done.assertClean()
	assert done.value["fired"]
	assert done.value["param"] == 7
	assert done.value["onThread"]
	assert done.value["elapsed"] >= 0.15


def test_timersFireInDueOrder(tmp_path: Path):
	done = runWithThread(
		tmp_path,
		"""
		from linuxPlatform.ioThread import WaitableTimer

		order = []
		both = threading.Event()

		def onTimer(param: int) -> None:
			order.append(param)
			if len(order) == 2:
				both.set()

		late, early = WaitableTimer(), WaitableTimer()
		# Far enough apart that arming the second, however slowly, cannot make it due after the first.
		hwIo.bgThread.setWaitableTimer(late, 1000, onTimer, 2)
		hwIo.bgThread.setWaitableTimer(early, 100, onTimer, 1)
		result = {"both": both.wait(WAIT), "order": order}
		hwIo.terminate()
		""",
	)
	done.assertClean()
	assert done.value == {"both": True, "order": [1, 2]}


def test_armingATimerAgainReplacesItsPendingCall(tmp_path: Path):
	done = runWithThread(
		tmp_path,
		"""
		from linuxPlatform.ioThread import WaitableTimer

		timer = WaitableTimer()
		calls = []
		fired = threading.Event()

		def onTimer(param: int) -> None:
			calls.append(param)
			fired.set()

		# The braille handler arms its acknowledgement timer again on every write.
		stored = len(IoThread._apcStore)
		for param in range(100):
			hwIo.bgThread.setWaitableTimer(timer, 60_000, onTimer, param)
		armed = len(IoThread._apcStore) - stored
		hwIo.bgThread.setWaitableTimer(timer, 50, onTimer, 100)
		result = {
			"fired": fired.wait(WAIT),
			"calls": calls,
			"armed": armed,
			"timers": len(hwIo.bgThread._timers),
			"storedAfter": len(IoThread._apcStore) - stored,
		}
		hwIo.terminate()
		""",
	)
	# A replaced call left in the store would also be run and logged as missing, failing this.
	done.assertClean()
	assert done.value == {"fired": True, "calls": [100], "armed": 1, "timers": 0, "storedAfter": 0}


def test_cancelledTimerNeverFires(tmp_path: Path):
	done = runWithThread(
		tmp_path,
		"""
		from linuxPlatform.ioThread import WaitableTimer

		timer, later = WaitableTimer(), WaitableTimer()
		calls = []
		laterFired = threading.Event()

		def onTimer(param: int) -> None:
			calls.append(param)

		def onLater(param: int) -> None:
			laterFired.set()

		stored = len(IoThread._apcStore)
		hwIo.bgThread.setWaitableTimer(timer, 1000, onTimer)
		cancelled = timer.cancel()
		storedAfterCancel = len(IoThread._apcStore) - stored
		# Timers fire in due order, so once this one has fired the cancelled one would have too.
		hwIo.bgThread.setWaitableTimer(later, 1200, onLater)
		result = {
			"laterFired": laterFired.wait(WAIT),
			"cancelled": cancelled,
			"calls": calls,
			"cancelUnarmed": timer.cancel(),
			"storedAfterCancel": storedAfterCancel,
		}
		hwIo.terminate()
		""",
	)
	done.assertClean()
	assert done.value == {
		"laterFired": True,
		"cancelled": True,
		"calls": [],
		"cancelUnarmed": True,
		"storedAfterCancel": 0,
	}


def test_timerArmedOnAnotherThreadMovesToTheNewOne(tmp_path: Path):
	done = runWithThread(
		tmp_path,
		"""
		from linuxPlatform.ioThread import IoThread as LinuxIoThread
		from linuxPlatform.ioThread import WaitableTimer

		other = LinuxIoThread()
		other.start()
		timer = WaitableTimer()
		seen = []
		fired = threading.Event()

		def onTimer(param: int) -> None:
			seen.append((param, threading.current_thread() is hwIo.bgThread))
			fired.set()

		stored = len(IoThread._apcStore)
		other.setWaitableTimer(timer, 60_000, onTimer, 1)
		hwIo.bgThread.setWaitableTimer(timer, 50, onTimer, 2)
		moved = {
			"otherTimers": len(other._timers),
			"armedOn": "bgThread" if timer._thread is hwIo.bgThread else repr(timer._thread),
			"stored": len(IoThread._apcStore) - stored,
		}
		result = {"fired": fired.wait(WAIT), "seen": seen, **moved, "storedAfter": len(IoThread._apcStore) - stored}
		other.stop()
		hwIo.terminate()
		""",
	)
	done.assertClean()
	assert done.value == {
		"fired": True,
		"seen": [[2, True]],
		"otherTimers": 0,
		"armedOn": "bgThread",
		"stored": 1,
		"storedAfter": 0,
	}


def test_cancelDropsADueCallTheBusyThreadHasNotTaken(tmp_path: Path):
	done = runWithThread(
		tmp_path,
		"""
		from linuxPlatform.ioThread import WaitableTimer

		timer = WaitableTimer()
		started = threading.Event()
		hold = threading.Event()
		after = threading.Event()
		calls = []

		def blocker(param: int) -> None:
			started.set()
			hold.wait(WAIT)

		def onTimer(param: int) -> None:
			calls.append(param)

		def last(param: int) -> None:
			after.set()

		hwIo.bgThread.queueAsApc(blocker)
		started.wait(WAIT)
		# Due at once, while the thread is still in blocker.
		hwIo.bgThread.setWaitableTimer(timer, 0, onTimer, 1)
		cancelled = timer.cancel()
		hold.set()
		hwIo.bgThread.queueAsApc(last)
		result = {"after": after.wait(WAIT), "cancelled": cancelled, "calls": calls}
		hwIo.terminate()
		""",
	)
	done.assertClean()
	assert done.value == {"after": True, "cancelled": True, "calls": []}


def test_idleThreadDoesNotSpin(tmp_path: Path):
	done = runWithThread(
		tmp_path,
		"""
		from linuxPlatform.ioThread import WaitableTimer

		def onTimer(param: int) -> None:
			pass

		# One timer far off, so the thread is waiting with a timeout rather than without one.
		timer = WaitableTimer()
		hwIo.bgThread.setWaitableTimer(timer, 60_000, onTimer)
		cpuBefore = time.process_time()
		time.sleep(1)
		result = time.process_time() - cpuBefore
		hwIo.terminate()
		""",
	)
	done.assertClean()
	assert done.value < 0.05


def test_stopRunsWhatIsQueuedAndDropsPendingTimers(tmp_path: Path):
	done = runWithThread(
		tmp_path,
		"""
		from linuxPlatform.ioThread import WaitableTimer

		calls = []

		def slow(param: int) -> None:
			time.sleep(0.1)
			calls.append(param)

		def onTimer(param: int) -> None:
			calls.append("timer")

		thread = hwIo.bgThread
		timer = WaitableTimer()
		stored = len(IoThread._apcStore)
		thread.setWaitableTimer(timer, 5_000, onTimer)
		thread.queueAsApc(slow, 1)
		thread.queueAsApc(slow, 2)
		hwIo.terminate()
		storedAfterStop = len(IoThread._apcStore) - stored
		errors = {}
		for name, call in (
			("stop", thread.stop),
			("queue", lambda: thread.queueAsApc(slow, 3)),
			("timer", lambda: thread.setWaitableTimer(timer, 10, onTimer)),
		):
			try:
				call()
			except RuntimeError as e:
				errors[name] = str(e)
		result = {
			"calls": calls,
			"alive": thread.is_alive(),
			"bgThread": hwIo.bgThread,
			"errors": errors,
			"threads": [t.name for t in threading.enumerate()],
			"storedAfterStop": storedAfterStop,
		}
		""",
	)
	done.assertClean()
	assert done.value == {
		"calls": [1, 2],
		"alive": False,
		"bgThread": None,
		"errors": dict.fromkeys(("stop", "queue", "timer"), "Thread is not running"),
		"threads": ["MainThread"],
		"storedAfterStop": 0,
	}


def test_crashedThreadIsLoggedAndForgetsWhatWasPending(tmp_path: Path):
	done = runWithThread(
		tmp_path,
		"""
		from linuxPlatform.ioThread import IoThread as LinuxIoThread
		from linuxPlatform.ioThread import WaitableTimer

		breakNow = threading.Event()

		class BreakingThread(LinuxIoThread):
			def _takeReady(self):
				breakNow.wait(WAIT)
				raise RuntimeError("broken by the test")

		def apc(param: int) -> None:
			pass

		thread = BreakingThread()
		thread.start()
		timer = WaitableTimer()
		stored = len(IoThread._apcStore)
		thread.queueAsApc(apc)
		thread.setWaitableTimer(timer, 60_000, apc)
		pending = len(IoThread._apcStore) - stored
		breakNow.set()
		thread.join(WAIT)
		result = {
			"alive": thread.is_alive(),
			"pending": pending,
			"storedAfter": len(IoThread._apcStore) - stored,
			"timerDisarmed": timer._thread is None,
		}
		hwIo.terminate()
		""",
	)
	assert done.error is None, done.error
	assert not done.noLinuxImplementation
	assert done.logErrors == ["CRITICAL: Exception in IoThread function"]
	assert done.value == {"alive": False, "pending": 2, "storedAfter": 0, "timerDisarmed": True}


def test_restartsAsResetConfigurationDoes(tmp_path: Path):
	done = runWithThread(
		tmp_path,
		"""
		first = hwIo.bgThread
		hwIo.terminate()
		hwIo.initialize()
		ran = threading.Event()

		def apc(param: int) -> None:
			ran.set()

		hwIo.bgThread.queueAsApc(apc)
		result = {"ran": ran.wait(WAIT), "new": hwIo.bgThread is not first, "firstAlive": first.is_alive()}
		hwIo.terminate()
		""",
	)
	done.assertClean()
	assert done.value == {"ran": True, "new": True, "firstAlive": False}


def test_overlappedCompletionHasNoLinuxImplementation(tmp_path: Path):
	done = runWithThread(
		tmp_path,
		"""
		from serial.win32 import OVERLAPPED

		from linuxPlatform import NoLinuxImplementation

		def onDone(error: int, numberOfBytes: int, overlapped: object) -> None:
			pass

		try:
			hwIo.bgThread.queueAsCompletionRoutine(onDone, OVERLAPPED())
		except NoLinuxImplementation as e:
			result = e.what
		hwIo.terminate()
		""",
	)
	assert done.error is None, done.error
	assert done.value == "hwIo.ioThread.IoThread.queueAsCompletionRoutine"
	assert done.noLinuxImplementation == [done.value]
	assert not done.logErrors


def test_devicesAreNeverOpened(tmp_path: Path):
	done = runWithThread(
		tmp_path,
		"""
		import sys

		from linuxPlatform import NoLinuxImplementation

		opened = []


		def watch(event, args):
			if event == "open" and isinstance(args[0], str) and args[0].startswith("/dev/"):
				opened.append(args[0])


		sys.addaudithook(watch)

		def onReceive(data: bytes) -> None:
			pass

		missing = {}
		for name, construct in (
			("serial", lambda: hwIo.Serial("/dev/null", baudrate=9600, onReceive=onReceive)),
			("hid", lambda: hwIo.Hid("/dev/null", onReceive)),
			("bulk", lambda: hwIo.Bulk("/dev/null", 0, 1, onReceive)),
		):
			try:
				construct()
			except NoLinuxImplementation as e:
				missing[name] = e.what
		result = {"missing": missing, "opened": opened}
		hwIo.terminate()
		""",
	)
	assert done.error is None, done.error
	assert done.value["opened"] == []
	assert done.value["missing"]["serial"] == "hwIo.Serial"
	assert set(done.value["missing"]) == {"serial", "hid", "bulk"}
	assert done.noLinuxImplementation == list(done.value["missing"].values())
	assert not done.logErrors
