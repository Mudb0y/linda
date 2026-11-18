"""Screen Curtain on X: the gamma ramps of every CRTC set to zero through RandR.

Windows' Magnification API makes the screen black for as long as NVDA's process lives. Here the
ramps are a setting of the X server, which no window can cover, which needs no compositing manager
and which leaves the pointer and clicks alone, but which nothing takes back when the process that
set it dies. A dead screen reader over a black screen leaves its user no speech and a sighted
helper no picture, so the ramps found are kept three ways: by the curtain's thread, which puts them
back when the curtain is lifted; by a guard process with a connection of its own, which puts them
back once NVDA's end of its pipe closes, however NVDA ended; and in a record under the runtime
directory, which the next start of NVDA reads should both have been killed together.

Programs that dim or tint the screen at night rewrite the ramps on a timer, which would lift the
curtain without a sound, so the thread checks them every CHECK_INTERVAL and on every RandR change,
and sets them back to black. Every CRTC is set, those that show nothing too, so that a monitor
plugged in later is black from its first frame.
"""

import json
import os
import secrets
import select
import subprocess
import sys
import threading
import time

from logHandler import log
from Xlib import error
from Xlib.ext import randr

from linuxPlatform import gammaCurtainRamps as ramps

#: Seconds between checks that the ramps are still black.
CHECK_INTERVAL = 1.0
#: Seconds NVDA's main thread waits for the curtain's thread to raise or lift the curtain.
ANSWER_TIMEOUT = 3.0
#: Seconds the guard has to connect to the X server and say it is ready, and to exit once told to.
GUARD_TIMEOUT = 3.0
#: Seconds a start waits to lift a curtain an earlier copy left.
STALE_TIMEOUT = 2.0
#: The oldest RandR with each CRTC's gamma ramps and GetScreenResourcesCurrent, which reads the
#: CRTCs without probing the monitors, as GetScreenResources does at a cost of up to a second.
RANDR_VERSION = (1, 3)
Ramps = ramps.Ramps

#: The guard. It imports nothing of NVDA's but gammaCurtainRamps, so that it runs whatever NVDA's
#: interpreter does. Its
#: standard input carries one JSON value a line: an object whose "crtcs" are the ramps to put back,
#: "release" to exit without putting them back, or "lift" to put them back at once and exit. It
#: writes "ready" once it has its connection and the first ramps. When its input ends, or it is
#: asked to end with a signal, it puts the ramps back on each CRTC that is still black, removes the
#: record if it is still the one it guards, and exits; it exits too when the X server goes.
GUARD_PROGRAM = r"""
import json, os, select, signal, sys
sys.path[:0] = json.loads(sys.argv[1])
displayName, recordPath, token = sys.argv[2:5]
from linuxPlatform import gammaCurtainRamps as ramps


class Lift(Exception):
	pass


def onSignal(number, frame):
	raise Lift


for number in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
	signal.signal(number, onSignal)
saved = {}
connection = None


def lift():
	for number in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
		signal.signal(number, signal.SIG_IGN)
	if connection is not None and ramps.putBack(connection, saved, onlyWhereBlack=True):
		ramps.removeRecord(recordPath, token)


try:
	connection = ramps.connect(displayName)
	pending = b""
	ready = False
	while True:
		readable, _w, _x = select.select([0, connection.fileno()], [], [])
		if connection.fileno() in readable:
			# Raises once the X server has closed the connection.
			connection.pending_events()
		if 0 not in readable:
			continue
		chunk = os.read(0, 1 << 16)
		if not chunk:
			lift()
			break
		*lines, pending = (pending + chunk).split(b"\n")
		for line in lines:
			message = json.loads(line)
			if message == "release":
				sys.exit(0)
			if message == "lift":
				lift()
				sys.exit(0)
			saved = ramps.fromJson(message["crtcs"])
			if not ready:
				ready = True
				sys.stdout.write("ready\n")
				sys.stdout.flush()
except Lift:
	lift()
except ramps.connectionErrors:
	pass
"""


class CurtainUnavailable(Exception):
	"""The curtain cannot be raised here, and the message says why."""


class _Lost(Exception):
	"""The curtain could not be kept, and the message says why."""


def _recordPath() -> str:
	from linuxPlatform import singleInstance

	directory = os.path.dirname(singleInstance.lockFilePath())
	return os.path.join(directory, f"display{singleInstance.displayName()}.curtain")


def _writeRecord(path: str, record: dict) -> None:
	temporary = f"{path}.new"
	fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_CLOEXEC, 0o600)
	with os.fdopen(fd, "w", encoding="utf-8") as f:
		json.dump(record, f)
	os.replace(temporary, path)


def _send(guard: subprocess.Popen, message: object) -> None:
	guard.stdin.write(json.dumps(message).encode() + b"\n")
	guard.stdin.flush()


class _Curtain(threading.Thread):
	"""The curtain's own connection to the X server, and the thread that raises the curtain on it,
	keeps it raised and lifts it. Nothing but this thread uses the connection."""

	def __init__(self, displayName: str, recordPath: str) -> None:
		super().__init__(name="Screen Curtain", daemon=True)
		self._displayName = displayName
		self._recordPath = recordPath
		self._token = secrets.token_hex(8)
		self._connection = None
		self._roots: list = []
		self._saved: dict[int, Ramps] = {}
		#: The gamma ramp size of each CRTC, read again when RandR reports a change.
		self._sizes: dict[int, int] = {}
		self._guard: subprocess.Popen | None = None
		self._ready = threading.Event()
		self._stopping = threading.Event()
		self._wakeRead, self._wakeWrite = os.pipe()
		#: Held to write to the pipe or close it, so that a stop after the end never writes to a
		#: descriptor that by then names another file.
		self._wakeLock = threading.Lock()
		self._wakeClosed = False
		self._rewritesLogged = False
		self._guardEndLogged = False
		#: Why the curtain could not be raised, once the thread has tried.
		self.failure: str | None = None
		#: Whether the ramps are black and kept so.
		self.active = False
		#: Whether this thread put the ramps back itself when it ended.
		self.lifted = False

	def waitUntilRaised(self, timeout: float) -> bool:
		return self._ready.wait(timeout)

	def stop(self) -> None:
		self._stopping.set()
		with self._wakeLock:
			if not self._wakeClosed:
				os.write(self._wakeWrite, b"x")

	def run(self) -> None:
		try:
			self._raise()
		except CurtainUnavailable as e:
			self.failure = str(e)
		except ramps.connectionErrors as e:
			self.failure = f"the connection to the X server failed: {e!r}"
		except Exception as e:  # noqa: BLE001
			log.debugWarning("Raising the Screen Curtain failed", exc_info=True)
			self.failure = f"unexpected failure: {e!r}"
		if self.failure is not None:
			self._end(self._putBack())
			self._ready.set()
			return
		self.active = True
		self._ready.set()
		reason = None
		try:
			self._watch()
		except _Lost as e:
			reason = str(e)
		except ramps.connectionErrors:
			reason = "the connection to the X server closed"
		except Exception as e:  # noqa: BLE001
			log.debugWarning("Keeping the Screen Curtain failed", exc_info=True)
			reason = f"unexpected failure: {e!r}"
		self.active = False
		self._end(self._putBack())
		if reason is not None:
			log.warning(f"Screen Curtain lifted: {reason}")
			_announceLifted()

	def _raise(self) -> None:
		try:
			connection = ramps.connect(self._displayName)
		except ramps.connectionErrors as e:
			raise CurtainUnavailable(f"cannot connect to the X display {self._displayName}: {e}") from e
		self._connection = connection
		if not connection.has_extension("RANDR"):
			raise CurtainUnavailable(
				"the X server has no RandR extension, through which the screen is made black"
			)
		version = randr.query_version(connection)
		if (version.major_version, version.minor_version) < RANDR_VERSION:
			raise CurtainUnavailable(
				f"the X server's RandR is version {version.major_version}.{version.minor_version}, "
				f"and the gamma ramps of each CRTC need {RANDR_VERSION[0]}.{RANDR_VERSION[1]}",
			)
		self._roots = [connection.screen(number).root for number in range(connection.screen_count())]
		crtcs = self._readCrtcs()
		for crtc in crtcs:
			if self._sizes[crtc]:
				self._save(crtc)
		self._writeRecord()
		self._startGuard()
		for crtc in self._saved:
			ramps.blacken(connection, crtc, self._sizes[crtc])
		connection.sync()
		for crtc in self._saved:
			if not ramps.isBlack(ramps.read(connection, crtc)):
				raise CurtainUnavailable(f"the X server did not keep the gamma ramps of CRTC {crtc} at zero")
		mask = randr.RRScreenChangeNotifyMask | randr.RRCrtcChangeNotifyMask | randr.RROutputChangeNotifyMask
		for root in self._roots:
			randr.select_input(root, mask)
		connection.sync()

	def _readCrtcs(self, *, readSizes: bool = False) -> dict[int, int]:
		"""The config timestamp of each CRTC on every screen, with the gamma ramp size of any not read
		before, or of all when readSizes; refuses where a CRTC showing a picture has no ramps."""
		connection = self._connection
		crtcs: dict[int, int] = {}
		for root in self._roots:
			resources = randr.get_screen_resources_current(root)
			if not resources.crtcs and not self.active:
				# Servers that keep RandR 1.0's screen sizes, Xephyr among them, make their CRTC only once
				# a client has had them probe it.
				resources = randr.get_screen_resources(root)
			for crtc in resources.crtcs:
				crtcs[crtc] = resources.config_timestamp
		if readSizes:
			self._sizes.clear()
		for crtc in crtcs:
			if crtc not in self._sizes:
				self._sizes[crtc] = randr.get_crtc_gamma_size(connection, crtc).size
		if not self.active:
			if not crtcs:
				raise CurtainUnavailable("the X server has no CRTCs, whose gamma ramps make the screen black")
			if not any(self._sizes[crtc] for crtc in crtcs):
				raise CurtainUnavailable(
					"the X server has no gamma ramps to set: the gamma ramp size of every CRTC is 0",
				)
		for crtc, timestamp in crtcs.items():
			if not self._sizes[crtc] and randr.get_crtc_info(connection, crtc, timestamp).mode:
				message = f"CRTC {crtc} shows a picture and has no gamma ramps to set"
				if self.active:
					raise _Lost(message)
				raise CurtainUnavailable(message)
		for gone in set(self._sizes) - set(crtcs):
			del self._sizes[gone]
		return crtcs

	def _save(self, crtc: int) -> None:
		found = ramps.read(self._connection, crtc)
		# Black already is what a curtain left by a copy that died shows, and putting that back would
		# leave the screen black.
		self._saved[crtc] = ramps.linear(len(found[0])) if ramps.isBlack(found) else found

	def _writeRecord(self) -> None:
		_writeRecord(
			self._recordPath,
			{"token": self._token, "display": self._displayName, "crtcs": ramps.toJson(self._saved)},
		)

	def _startGuard(self) -> None:
		try:
			guard = subprocess.Popen(
				[
					sys.executable,
					"-I",
					# It imports NVDA's own linuxPlatform, beside which nothing may be written.
					"-B",
					"-c",
					GUARD_PROGRAM,
					json.dumps([path for path in sys.path if path]),
					self._displayName,
					self._recordPath,
					self._token,
				],
				stdin=subprocess.PIPE,
				stdout=subprocess.PIPE,
				# Out of reach of the terminal NVDA may run in, whose Control+C or hangup would end the
				# guard with NVDA; and of NVDA's process group, which a restart may signal.
				start_new_session=True,
			)
		except OSError as e:
			raise CurtainUnavailable(
				f"the guard that lifts the curtain should NVDA end could not start: {e}"
			) from e
		self._guard = guard
		try:
			self._sendToGuard({"crtcs": ramps.toJson(self._saved)})
			readable, _w, _x = select.select([guard.stdout], [], [], GUARD_TIMEOUT)
			answer = guard.stdout.readline() if readable else b""
		except OSError:
			answer = b""
		if answer != b"ready\n":
			self._stopGuard("release")
			raise CurtainUnavailable("the guard that lifts the curtain should NVDA end did not start")

	def _sendToGuard(self, message: object) -> None:
		_send(self._guard, message)

	def _stopGuard(self, message: str) -> None:
		guard, self._guard = self._guard, None
		if guard is None:
			return
		try:
			_send(guard, message)
		except OSError:
			pass
		try:
			guard.stdin.close()
		except OSError:
			pass
		try:
			guard.wait(GUARD_TIMEOUT)
		except subprocess.TimeoutExpired:
			guard.kill()
			guard.wait()
		guard.stdout.close()

	def _watch(self) -> None:
		connection = self._connection
		socket = connection.fileno()
		nextCheck = time.monotonic() + CHECK_INTERVAL
		while not self._stopping.is_set():
			changed = False
			while connection.pending_events():
				connection.next_event()
				changed = True
			if changed:
				# A monitor plugged in or a mode set: black at once rather than at the next check.
				self._enforce(readSizes=True)
			readable, _w, _x = select.select(
				[socket, self._wakeRead],
				[],
				[],
				max(0.0, nextCheck - time.monotonic()),
			)
			if self._wakeRead in readable:
				os.read(self._wakeRead, 64)
			if time.monotonic() >= nextCheck:
				self._enforce()
				self._checkGuard()
				nextCheck = time.monotonic() + CHECK_INTERVAL

	def _enforce(self, *, readSizes: bool = False) -> None:
		connection = self._connection
		crtcs = self._readCrtcs(readSizes=readSizes)
		added = False
		rewritten = []
		for crtc in crtcs:
			size = self._sizes[crtc]
			if not size:
				continue
			try:
				if crtc not in self._saved or len(self._saved[crtc][0]) != size:
					self._save(crtc)
					added = True
				elif ramps.isBlack(ramps.read(connection, crtc)):
					continue
				else:
					rewritten.append(crtc)
				ramps.blacken(connection, crtc, size)
			except error.XError:
				# Gone between reading the CRTCs and reaching this one.
				continue
		for gone in set(self._saved) - set(crtcs):
			del self._saved[gone]
		if added or rewritten:
			connection.sync()
		if added:
			self._writeRecord()
			try:
				self._sendToGuard({"crtcs": ramps.toJson(self._saved)})
			except OSError:
				pass
		if rewritten and not self._rewritesLogged:
			self._rewritesLogged = True
			log.info(
				f"Another program set the gamma ramps of CRTC {', '.join(map(str, rewritten))} under the "
				"Screen Curtain; they are set back to black whenever that happens",
			)

	def _checkGuard(self) -> None:
		if self._guard is not None and self._guard.poll() is None:
			return
		if not self._guardEndLogged:
			self._guardEndLogged = True
			log.warning("The Screen Curtain's guard ended; starting another")
		if self._guard is not None:
			self._guard.stdin.close()
			self._guard.stdout.close()
			self._guard = None
		try:
			self._startGuard()
		except CurtainUnavailable as e:
			raise _Lost(str(e)) from e

	def _putBack(self) -> bool:
		"""Put back the ramps found, through this thread's connection; whether that worked."""
		if self._connection is None or not self._saved:
			return self._connection is not None
		return ramps.putBack(self._connection, self._saved, onlyWhereBlack=False)

	def _end(self, lifted: bool) -> None:
		"""Let the guard go, or have it lift the curtain where this thread could not; then close."""
		self.lifted = lifted
		if self._guard is not None:
			self._stopGuard("release" if lifted else "lift")
		if lifted:
			try:
				os.remove(self._recordPath)
			except FileNotFoundError:
				pass
			except OSError:
				log.debugWarning("Could not remove the Screen Curtain's record", exc_info=True)
		if self._connection is not None:
			try:
				self._connection.close()
			# A connection the X server has closed, whose socket is gone already.
			except Exception:  # noqa: BLE001, S110
				pass
		with self._wakeLock:
			self._wakeClosed = True
			os.close(self._wakeRead)
			os.close(self._wakeWrite)


def _announceLifted() -> None:
	"""Say that the curtain is down, from the curtain's thread, on NVDA's main thread."""
	try:
		import queueHandler
		import speech
		import ui

		queueHandler.queueFunction(
			queueHandler.eventQueue,
			ui.message,
			# Translators: Reported when the screen curtain is disabled.
			_("Screen curtain disabled"),
			speechPriority=speech.priorities.Spri.NOW,
		)
	except Exception:  # noqa: BLE001
		log.debugWarning("Could not say that the Screen Curtain was lifted", exc_info=True)


_lock = threading.Lock()
_curtain: _Curtain | None = None


def enable() -> None:
	"""Make every monitor of the X display black, until disable or until NVDA ends.

	:raises CurtainUnavailable: Saying why it cannot.
	"""
	global _curtain
	with _lock:
		if _curtain is not None and _curtain.active:
			return
		if os.environ.get("XDG_SESSION_TYPE") == "wayland" or os.environ.get("WAYLAND_DISPLAY"):
			# The X server of a Wayland session is not what drives the monitors: its ramps could read
			# back black while the screen still shows.
			raise CurtainUnavailable(
				"this is a Wayland session, where the X server's gamma ramps are not the screen's",
			)
		displayName = os.environ.get("DISPLAY")
		if not displayName:
			raise CurtainUnavailable("there is no X display, and the screen is made black through X")
		try:
			recordPath = _recordPath()
		except OSError as e:
			raise CurtainUnavailable(f"there is nowhere to keep the screen's gamma ramps: {e}") from e
		curtain = _Curtain(displayName, recordPath)
		curtain.start()
		if not curtain.waitUntilRaised(ANSWER_TIMEOUT):
			# The thread lifts what it raised once the X server answers; the guard, if one started, once
			# NVDA ends.
			curtain.stop()
			raise CurtainUnavailable(f"the X server did not answer within {ANSWER_TIMEOUT:g} s")
		if curtain.failure is not None:
			raise CurtainUnavailable(curtain.failure)
		_curtain = curtain


def disable() -> None:
	"""Put back the gamma ramps found when the curtain was raised."""
	global _curtain
	with _lock:
		curtain, _curtain = _curtain, None
		if curtain is None:
			return
		curtain.stop()
		curtain.join(ANSWER_TIMEOUT)
		if curtain.is_alive():
			log.warning(
				f"The X server did not answer within {ANSWER_TIMEOUT:g} s of lifting the Screen Curtain; "
				"it is lifted once it answers, or by its guard once NVDA ends",
			)


def isActive() -> bool:
	"""Whether the screen is black and kept so."""
	curtain = _curtain
	return curtain is not None and curtain.active


def liftStale() -> str | None:
	"""Put back the ramps a copy of NVDA left black, if it ended with the curtain raised and its
	guard ended with it; what was done, for the log, or None if there was nothing to do.

	Only CRTCs that are still black are touched, so that a curtain lifted since, or an X server
	started afresh, keeps the ramps it has.
	"""
	try:
		path = _recordPath()
	except OSError as e:
		return f"Could not look for a Screen Curtain left by a copy of NVDA that ended: {e}"
	try:
		with open(path, encoding="utf-8") as f:
			record = json.load(f)
		saved = ramps.fromJson(record["crtcs"])
		displayName = record["display"]
	except FileNotFoundError:
		return None
	except (OSError, ValueError, KeyError, TypeError) as e:
		try:
			os.remove(path)
		except OSError:
			pass
		return f"Removed an unreadable Screen Curtain record: {e!r}"
	outcome: list[str] = []

	def lift() -> None:
		try:
			connection = ramps.connect(displayName)
		except ramps.connectionErrors as e:
			outcome.append(f"could not connect to {displayName}: {e}")
			return
		try:
			if ramps.putBack(connection, saved, onlyWhereBlack=True):
				ramps.removeRecord(path, record.get("token"))
				outcome.append("")
			else:
				outcome.append("the X server did not take them")
		finally:
			connection.close()

	thread = threading.Thread(target=lift, name="Screen Curtain left over", daemon=True)
	thread.start()
	thread.join(STALE_TIMEOUT)
	if not outcome:
		return (
			f"A Screen Curtain left by a copy of NVDA that ended was not lifted: {displayName} did not answer"
		)
	if outcome[0]:
		return f"A Screen Curtain left by a copy of NVDA that ended was not lifted: {outcome[0]}"
	return f"Lifted a Screen Curtain left on {displayName} by a copy of NVDA that ended with it raised"
