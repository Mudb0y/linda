"""Which way keys come to NVDA: from the kernel through linda-keyd where it is installed and admits
NVDA, from the X display always, and the kernel source coming and going on top of the X source.

The X source reads the X display from start to end. Beside it, the kernel source takes the keys of
every keyboard the helper holds, and the helper puts out on its virtual keyboard what NVDA passes.
Where a document's keys go is the X source's to say, for both: the kernel source asks it where its
grabs are, so that each key goes where it would on the X route. The keys passed reach the X display
too, decided already, and the X source gives them to no one: while
the helper serves this NVDA, a press of the virtual keyboard is the kernel source's, and its release
follows it whenever it comes. Every other key, from a keyboard the helper leaves alone, one made
through uinput, or one another program sends through XTEST, is the X source's, as it is with no
helper. Falling back is only the kernel source going away, so that there is never a moment with no
route that takes NVDA's commands from the applications.

NVDA has the keys of both as one stream, in the order they come to this, which is the order they
were typed as far as the two can tell it. What NVDA asks of the keyboard, the NVDA keys, browse
mode's keys and input help's hold, goes to both; what acts on the X display as a whole, sending keys
and setting the locks, goes to the X source alone.

As the kernel source goes, each key it gave NVDA the press and not the release of is released, and
what NVDA asked of the keyboard is asked again of the next kernel source.

After the emergency chord the helper lets go, and NVDA takes the keyboard back only when the user
asks or NVDA starts again: the user pressed it because something was wrong.
"""

import math
import os
import threading
import time
from collections.abc import Callable
from dataclasses import replace
from functools import partial

from logHandler import log

from . import VIRTUAL_KEYBOARD, CaptureMode, GestureKey, KeyEvent, KeySource, keydClient

#: The routes the configuration names: the helper when it admits NVDA, and the X display otherwise; the
#: X display alone.
ROUTES = ("auto", "x11")
#: Seconds between looks for the helper while it is not in use and may be.
RETRY_INTERVAL = 2.0
#: Seconds the route's own thread waits for the helper's answer: beyond the five the helper allows
#: logind and polkit before it says goodbye by itself.
ADMISSION_WAIT = 7.0
#: Seconds the virtual keyboard's presses stay decided after the kernel source has gone: a press NVDA
#: passed just before may reach the X source after the kernel source has said it is lost. The helper
#: puts out no other press until it serves the next one, seconds later; the real keyboards' keys reach
#: the X source on their own devices at once, and are its own from the moment the helper lets go.
DECIDED_GRACE = 0.3
#: Seconds the X display may take to have the virtual keyboard as an enabled keyboard of a master once
#: the helper holds a keyboard, or to have it again once it stops: keys the helper passes on meanwhile
#: go nowhere. The helper makes it and lets the desktop take it up before it grabs anything, so it is
#: there already as a rule.
VIRTUAL_WAIT = 0.5
#: Seconds the kernel source goes on holding every key, as for input help, after the hold ends, for
#: the X source to let go of the keyboard it grabbed for it.
HOLD_RELEASE_WAIT = 0.2


class RoutedKeySource:
	"""A KeySource that is the X source, with the kernel source on top of it whenever the route gives."""

	#: How the log begins each line saying the keys are read without the helper, and each saying they
	#: stay so: a route that reads them otherwise words its own.
	_withoutKernel = "Keys are read from the X display"
	_staysWithout = "Keys stay with the X display"

	def __init__(
		self,
		route: str,
		socketPath: str,
		notify: Callable[[str], None] | None = None,
		virtualKeyboard: str = VIRTUAL_KEYBOARD,
	) -> None:
		"""
		:param route: One of ROUTES.
		:param notify: Says a message to the user, from any thread.
		:param virtualKeyboard: The name of the helper's virtual keyboard among the X display's devices.
		"""
		self._route = route if route in ROUTES else "auto"
		self._socketPath = socketPath
		self._notify = notify
		self._virtualKeyboard = virtualKeyboard
		#: Held while the kernel source changes and while what NVDA asked is recorded and passed on.
		self._lock = threading.RLock()
		self._x: KeySource | None = None
		#: The kernel source from its start until it is given up, whether or not the helper holds a
		#: keyboard for it yet.
		self._kernel: KeySource | None = None
		self._kernelWhy = ""
		#: Whether the helper holds a keyboard for that kernel source, and the X display takes what it
		#: passes on.
		self._isKernel = False
		#: When the helper first said it holds a keyboard for that kernel source, while the X display's
		#: having the virtual keyboard is yet to be seen.
		self._heldAt: float | None = None
		#: Since when the X display has not had the virtual keyboard as a keyboard of a master, while the
		#: helper holds keyboards for the kernel source NVDA uses.
		self._unreadySince: float | None = None
		#: The helper's socket as it was when NVDA gave the helper up because the X display did not take
		#: up its virtual keyboard: NVDA asks again only once the X display has it, the helper's socket is
		#: another, or the user asks, never at every look, which would take the keyboard each time.
		self._blocked: tuple[int, int, int] | None = None
		#: Until when a press of the virtual keyboard is the kernel source's: for ever while the helper
		#: serves it, a little longer as it goes.
		self._decidedUntil = 0.0
		self._deliver: Callable[[KeyEvent], bool] | None = None
		self._modeNow: Callable[[], CaptureMode | None] | None = None
		#: Held while a key goes to NVDA, so that the two sources' threads give it one stream.
		self._delivering = threading.Lock()
		#: .source: the source whose key NVDA has on this thread, for what NVDA asks while it has it.
		self._from = threading.local()
		self._consumed: tuple[int, bool] | None = None
		self._gestures: tuple[frozenset[GestureKey], frozenset[GestureKey]] | None = None
		self._bound: frozenset[GestureKey] = frozenset()
		self._capture: tuple[CaptureMode, int | None] = ("off", None)
		self._hold = False
		self._wake = threading.Event()
		self._stopping = False
		self._thread: threading.Thread | None = None
		#: (source, why, whether by the emergency chord) of each kernel source that gave the helper up.
		self._lost: list[tuple[KeySource, str, bool]] = []
		#: The emergency chord was pressed: no helper until the user asks for it.
		self._letGo = False
		self._takingBack = False
		self._nextTry = 0.0
		#: The last reason the helper was not used that went to the log, so that each goes there once.
		self._logged: str | None = None
		#: That another program has the helper was said, until NVDA is admitted again: it turns NVDA away
		#: at every look while that program has it.
		self._saidBusy = False
		#: The kernel source given up last was lost as another program took NVDA's place with the helper.
		self._lostReplaced = False
		#: Source: (scan code, extended): the press NVDA had from it of each key whose release it has not.
		self._keysDown: dict[KeySource, dict[tuple[int, bool], KeyEvent]] = {}

	@property
	def isKernel(self) -> bool:
		"""Whether the helper holds a keyboard for NVDA's kernel source."""
		return self._isKernel

	def start(
		self,
		deliver: Callable[[KeyEvent], bool],
		modeNow: Callable[[], CaptureMode | None] | None = None,
	) -> None:
		"""
		:raise Exception: When the X display cannot be read.
		"""
		self._deliver, self._modeNow = deliver, modeNow
		if self._route == "x11":
			why = "the key route is set to the X display"
		elif not os.path.exists(self._socketPath):
			why = self._logged = f"linda-keyd is not running, as there is no {self._socketPath}"
			self._nextTry = time.monotonic() + RETRY_INTERVAL
		else:
			why = "until linda-keyd holds the keyboard"
		self._startBase(why)
		self._thread = threading.Thread(target=self._supervise, name="keyRoute", daemon=True)
		self._thread.start()

	def _startBase(self, why: str) -> None:
		"""Start what reads keys for NVDA's whole life, the X source, and say in the log why the helper is
		not in use yet."""
		from . import x11

		source = x11.X11KeySource(self._virtualKeyboard, self._isDecided, self._wake.set)
		source.start(partial(self._deliverFrom, source), self._modeNow)
		with self._lock:
			self._x = source
			self._replay(source)
		log.info(f"{self._withoutKernel}: {why}")

	def stop(self) -> None:
		self._stopping = True
		self._wake.set()
		if self._thread is not None:
			# Asking the helper may take a few seconds, after which the thread lets go of what it got.
			self._thread.join(1)
			self._thread = None
		with self._lock:
			sources = [s for s in (self._kernel, self._x) if s is not None]
			self._kernel = self._x = None
			self._isKernel = False
			self._decidedUntil = 0.0
		for source in sources:
			source.stop()

	def setRoute(self, route: str) -> None:
		"""Follow the configuration's route from now on."""
		route = route if route in ROUTES else "auto"
		if route == self._route:
			return
		self._route = route
		self._nextTry = 0.0
		self._logged = None
		self._wake.set()

	def takeBack(self) -> None:
		"""Take the keyboard back through the helper, as after the emergency chord, and say how it went."""
		if self._route == "x11":
			# Translators: Said on Linux when the user asks NVDA to take the keyboard back from the X
			# display, and NVDA's advanced setting has it read keys from the X display alone.
			self._say(_("NVDA is set to read keys from the X display"))
			return
		kernel = self._kernel
		if self._isKernel and getattr(kernel, "isHolding", True):
			# Translators: Said on Linux when the user asks NVDA to take the keyboard back, and it already
			# takes keys from the kernel.
			self._say(_("NVDA has the keyboard already"))
			return
		if kernel is not None:
			# Translators: Said on Linux when the user asks NVDA to take the keyboard back while linda-keyd,
			# the program that takes the keyboard in the kernel, serves NVDA but holds no keyboard, as while
			# another session is in front: it takes the keyboard by itself as soon as it may.
			self._say(_("linda-keyd holds no keyboard now, and takes it for NVDA as soon as it may"))
			return
		self._letGo = False
		self._blocked = None
		self._takingBack = True
		self._nextTry = 0.0
		self._wake.set()

	# What NVDA asks of the keyboard, recorded to be asked again of the next kernel source.

	def _both(self) -> list[KeySource]:
		return [s for s in (self._x, self._kernel) if s is not None]

	def setConsumed(self, nvdaModifierKeys: int, numpadReview: bool) -> None:
		with self._lock:
			self._consumed = (nvdaModifierKeys, numpadReview)
			for source in self._both():
				source.setConsumed(nvdaModifierKeys, numpadReview)

	def setCapture(self, mode: CaptureMode, processID: int | None = None) -> None:
		with self._lock:
			self._capture = (mode, processID)
			for source in self._both():
				source.setCapture(mode, processID)

	def setCaptureGestures(self, browse: frozenset[GestureKey], focus: frozenset[GestureKey]) -> None:
		with self._lock:
			self._gestures = (browse, focus)
			for source in self._both():
				source.setCaptureGestures(browse, focus)

	def setBoundKeys(self, keys: frozenset[GestureKey]) -> None:
		"""Capture these in every program, a document or not, as X11KeySource.setBoundKeys does."""
		with self._lock:
			self._bound = keys
			for source in self._both():
				source.setBoundKeys(keys)

	def holdKeyboard(self, hold: bool) -> None:
		with self._lock:
			self._hold = hold
			x, kernel = self._x, self._kernel
			if x is not None:
				x.holdKeyboard(hold)
				if not hold and kernel is not None:
					# The kernel source passes keys on again the moment it is told, and a press that reached
					# the X display before the X source had let go of the keyboard went to the X source's
					# grab, the application having its release alone.
					x.awaitCommands(HOLD_RELEASE_WAIT)
			if kernel is not None:
				kernel.holdKeyboard(hold)

	# What acts on the X display as a whole is the X source's, which runs for NVDA's whole life: a key
	# sent through XTEST comes back to it, which knows its own, and to no other. The kernel source sends
	# none.

	def _sender(self) -> KeySource | None:
		return self._x

	def setCapsLock(self, on: bool) -> None:
		# Put back by the source the kept press came from: the X server locked as a key it had went down,
		# and a key the kernel source kept never reached it.
		source = getattr(self._from, "source", None) or self._sender()
		if source is not None:
			source.setCapsLock(on)

	def inject(self, vkCode: int, scanCode: int, extended: bool, isUp: bool) -> None:
		if (source := self._sender()) is not None:
			source.inject(vkCode, scanCode, extended, isUp)

	def typeKey(self, vkCode: int, extended: bool, isUp: bool, scanCode: int | None = None) -> None:
		if (source := self._sender()) is not None:
			source.typeKey(vkCode, extended, isUp, scanCode)

	def releaseTypedKeys(self) -> None:
		if (source := self._sender()) is not None:
			source.releaseTypedKeys()

	def keyState(self, vkCode: int) -> int:
		x, kernel = self._x, self._kernel if self._isKernel else None
		if kernel is None or not getattr(kernel, "isHolding", True):
			# While the helper holds nothing, as while this session is not the one in front, the locks it
			# last reported may be stale; the X display's are the keyboards'.
			return x.keyState(vkCode) if x is not None else 0
		# The locks as the kernel source has them, which knows a lock the helper passed before the X
		# display does; a key down on any keyboard as the X display has it.
		return kernel.keyState(vkCode) | (x.keyState(vkCode) & 0x8000 if x is not None else 0)

	def _layout(self) -> KeySource | None:
		return self._kernel if self._isKernel else self._x

	def charForVk(self, vkCode: int) -> int:
		source = self._layout()
		return source.charForVk(vkCode) if source is not None else 0

	def vkForChar(self, ch: str) -> tuple[int, int]:
		if (source := self._layout()) is None:
			raise LookupError(ch)
		return source.vkForChar(ch)

	def keyName(self, scanCode: int, extended: bool) -> str:
		source = self._layout()
		return source.keyName(scanCode, extended) if source is not None else ""

	# Keys.

	def _isDecided(self, when: float | None = None) -> bool:
		""":param when: When the key happened, by time.monotonic(), when known; now otherwise."""
		return (time.monotonic() if when is None else when) < self._decidedUntil

	def _deliverFrom(self, source: KeySource, event: KeyEvent) -> bool:
		with self._delivering:
			key = (event.scanCode, event.extended)
			if event.isUp:
				if self._keysDown.setdefault(source, {}).pop(key, None) is None:
					# Its press came from the other source.
					for other, down in self._keysDown.items():
						if other is not source and down.pop(key, None) is not None:
							break
			elif not event.repeat:
				self._keysDown.setdefault(source, {})[key] = event
			self._from.source = source
			try:
				return self._deliver(event)
			finally:
				self._from.source = None

	def _releaseLeft(self, source: KeySource) -> None:
		"""Release to NVDA each key source gave it the press and not the release of, now that it has gone:
		the release would come to the other source, which never had the press, or to none, and NVDA would
		take the key for held from then on, an NVDA key making every key a command."""
		now = time.monotonic()
		with self._delivering:
			left = list(self._keysDown.pop(source, {}).values())
			for press in left:
				self._deliver(
					replace(
						press,
						isUp=True,
						repeat=False,
						text="",
						typing=False,
						time=now,
						captured=False,
						nvdaKeyGrab=False,
					),
				)

	# Changing the route.

	def _say(self, message: str) -> None:
		if self._notify is not None:
			try:
				self._notify(message)
			except Exception:
				log.exception("Saying how keys come to NVDA")

	def _admit(self, wait: float):
		"""A kernel source on a connection the helper admitted, or None; and why, for the log."""
		if not os.path.exists(self._socketPath):
			return None, f"linda-keyd is not running, as there is no {self._socketPath}"
		try:
			connection, welcome = self._connect(wait)
		except keydClient.Refused as e:
			self._refused(e)
			return None, f"linda-keyd did not admit NVDA: {e}"
		self._saidBusy = False
		return self._newKernel(connection, welcome), "linda-keyd admitted NVDA"

	def _connect(self, wait: float):
		"""The helper's connection and welcome, asking for the features this route uses.

		:raise keydClient.Refused: When the helper does not admit NVDA.
		"""
		return keydClient.connect(self._socketPath, wait)

	def _refused(self, error: keydClient.Refused) -> None:
		"""The helper did not admit NVDA. The X display has the keys meanwhile and the log says why; only
		another program having the helper is said, as that program can read every key."""
		goodbye = error.goodbye
		if goodbye is None or goodbye.reason != keydClient.GOODBYE_BUSY or self._saidBusy:
			return
		self._saidBusy = True
		self._say(
			# Translators: Said on Linux in an X11 session when linda-keyd, the program that takes the
			# keyboard in the kernel, turns NVDA away because another program of this session has it.
			_(
				"Another program holds linda-keyd for this session, so NVDA takes its keys from the X display. "
				"If you started no other screen reader, something may be reading your keys.",
			),
		)

	def _newKernel(self, connection, welcome) -> KeySource:
		"""A kernel source on a connection the helper admitted, reporting to this route."""
		from . import kernel

		# The kernel source sends a document's keys where the X source's grabs would, by the X source's
		# own account of them, and puts down in the X display through it what the helper never passed.
		x = self._x
		source = kernel.KernelKeySource(connection, welcome, None, None, x)
		source.setCallbacks(partial(self._onKernelLost, source), partial(self._onKernelHeld, source))
		return source

	def _replay(self, source: KeySource) -> None:
		with self._lock:
			if self._consumed is not None:
				source.setConsumed(*self._consumed)
			if self._gestures is not None:
				source.setCaptureGestures(*self._gestures)
			if self._bound:
				source.setBoundKeys(self._bound)
			if self._capture[0] != "off":
				source.setCapture(*self._capture)
			if self._hold:
				source.holdKeyboard(True)

	def _startKernel(self, kernel: KeySource, why: str) -> bool:
		"""Start a kernel source beside the X source, its keys NVDA's once the helper holds a keyboard."""
		with self._lock:
			self._replay(kernel)
			self._kernel, self._kernelWhy = kernel, why
		try:
			kernel.start(partial(self._deliverFrom, kernel), self._modeNow)
		except Exception as e:
			record = f"NVDA cannot take keys from linda-keyd: {e}"
			# With its traceback once, not at every look two seconds apart.
			if record != self._logged:
				self._logged = record
				log.warning(record, exc_info=True)
			with self._lock:
				self._kernel = None
			kernel.stop()
			return False
		return True

	def _onKernelHeld(self, kernel: KeySource) -> None:
		"""On the kernel source's own thread, before it reads any key the helper holds: from now on the
		virtual keyboard's presses are its own."""
		if kernel is self._kernel:
			self._decidedUntil = math.inf
			if not self._isKernel and self._heldAt is None:
				self._heldAt = time.monotonic()
				self._wake.set()

	def _onKernelLost(self, kernel: KeySource, why: str, emergency: bool) -> None:
		if kernel is self._kernel:
			self._decidedUntil = min(self._decidedUntil, time.monotonic() + DECIDED_GRACE)
		self._lost.append((kernel, why, emergency))
		self._wake.set()

	def _watchVirtual(self) -> None:
		"""Take the kernel route once the X display has the virtual keyboard as a keyboard of a master,
		and give the helper up when it has not had it for VIRTUAL_WAIT while the helper holds keyboards:
		every key passed on would go nowhere, with the helper and NVDA both answering as they should."""
		kernel = self._kernel
		if kernel is None:
			return
		now = time.monotonic()
		ready = self._virtualReady()
		if not self._isKernel:
			if self._heldAt is None:
				return
			if ready:
				self._heldAt = None
				self._isKernel = True
				self._logged = None
				log.info(
					f"Keys come from the kernel through linda-keyd: {self._kernelWhy} and holds the keyboard"
				)
				if self._takingBack:
					self._takingBack = False
					# Translators: Said on Linux when NVDA has taken the keyboard back through linda-keyd, the
					# program that takes the keyboard in the kernel, as the user asked.
					self._say(_("NVDA has the keyboard again"))
			elif now >= self._heldAt + VIRTUAL_WAIT:
				self._giveUpVirtual(f"stay with the X display: {self._virtualProblem()}")
			return
		# Let go for a console switch, the helper holds nothing, and the X display may take its devices
		# away meanwhile.
		if ready or not getattr(kernel, "isHolding", True):
			self._unreadySince = None
		elif self._unreadySince is None:
			self._unreadySince = now
		elif now >= self._unreadySince + VIRTUAL_WAIT:
			self._giveUpVirtual(
				f"are read from the X display: linda-keyd was given up: {self._virtualProblem()}"
			)

	def _virtualProblem(self) -> str:
		x = self._x
		known = x is not None and x.virtualKeyboardState[0]
		where = "is not an enabled keyboard of a master" if known else "is not among its devices"
		return (
			f"the X display's {self._virtualKeyboard!r}, which linda-keyd passes keys on through, {where}; "
			"linda-keyd is asked again once the X display has it, linda-keyd starts again or the user asks"
		)

	def _giveUpVirtual(self, record: str) -> None:
		self._blocked = self._socketIdentity()
		self._takingBack = False
		log.info(f"Keys {record}")
		# Said before the kernel source has stopped, which takes up to half a second: the helper lets go
		# as its connection closes, the first thing the stop does, and the X source reads on meanwhile.
		self._sayFellBack()
		self._dropKernel()

	def _dropKernel(self) -> bool:
		"""Stop the kernel source; whether the helper had held a keyboard for it."""
		with self._lock:
			kernel, self._kernel = self._kernel, None
			wasKernel, self._isKernel = self._isKernel, False
			self._heldAt = self._unreadySince = None
			self._decidedUntil = min(self._decidedUntil, time.monotonic() + DECIDED_GRACE)
		if kernel is not None:
			kernel.stop()
			self._releaseLeft(kernel)
		return wasKernel

	def _socketIdentity(self) -> tuple[int, int, int] | None:
		try:
			info = os.stat(self._socketPath)
		except OSError:
			return None
		return (info.st_dev, info.st_ino, info.st_mtime_ns)

	def _virtualReady(self) -> bool:
		x = self._x
		return x is None or x.virtualKeyboardState[1]

	def _isBlocked(self) -> bool:
		if self._blocked is None:
			return False
		if self._virtualReady() or self._socketIdentity() not in (self._blocked, None):
			self._blocked = None
			self._nextTry = 0.0
			return False
		return True

	def _wantsKernel(self) -> bool:
		return self._route != "x11" and not self._letGo and self._kernel is None and not self._isBlocked()

	def _sayFellBack(self) -> None:
		replaced, self._lostReplaced = self._lostReplaced, False
		if replaced:
			# That program has the helper now, which turns NVDA away at every look until it lets go.
			self._saidBusy = True
			self._say(
				# Translators: Said on Linux in an X11 session when another program took NVDA's place with
				# linda-keyd, the program that takes the keyboard in the kernel.
				_(
					"Another program took NVDA's place with linda-keyd and reads every key now. "
					"NVDA takes its keys from the X display.",
				),
			)
			return
		# Translators: Said on Linux when NVDA stops taking keys from linda-keyd, the program that takes
		# the keyboard in the kernel, because that program went away or failed, and reads them from the X
		# display instead.
		self._say(_("NVDA now reads keys from the X display"))

	def _sayEmergency(self) -> None:
		self._say(
			# Translators: Said on Linux after the emergency chord, Backspace, Escape and Enter held
			# together, made linda-keyd, the program that takes the keyboard in the kernel for NVDA, let
			# go of it. NVDA then reads keys the way it does without that program.
			_(
				"NVDA has let go of the keyboard, and now reads keys from the X display. "
				"Use the command to take the keyboard back when you want it to take them again",
			),
		)

	def _supervise(self) -> None:
		while not self._stopping:
			now = time.monotonic()
			deadlines = []
			if self._wantsKernel():
				deadlines.append(self._nextTry)
			elif self._blocked is not None:
				# The socket is looked at, never connected to, until one of the reasons to ask again holds.
				deadlines.append(now + RETRY_INTERVAL)
			for since in (self._heldAt, self._unreadySince):
				if since is not None:
					deadlines.append(since + VIRTUAL_WAIT)
			wait = max(0.0, min(deadlines) - now) if deadlines else None
			self._wake.wait(wait)
			self._wake.clear()
			if self._stopping:
				return
			try:
				self._turn()
			except Exception:
				log.exception("Choosing the way keys come to NVDA")

	def _turn(self) -> None:
		while self._lost:
			kernel, why, emergency = self._lost.pop(0)
			if emergency:
				# The user pressed it because something was wrong: whichever source the helper let go for,
				# and whether or not NVDA had taken the route yet, nothing asks the helper again until the
				# user does.
				if kernel is self._kernel:
					self._dropKernel()
				self._letGo = True
				self._takingBack = False
				log.info(f"{self._withoutKernel}: linda-keyd was given up: {why}")
				self._sayEmergency()
				continue
			if kernel is not self._kernel:
				continue
			if not self._dropKernel():
				self._nextTry = time.monotonic() + RETRY_INTERVAL
				record = f"{self._staysWithout}: linda-keyd was given up before it held the keyboard: {why}"
				# Said once, not at every look two seconds apart.
				if record != self._logged:
					self._logged = record
					log.info(record)
				continue
			self._nextTry = time.monotonic() + RETRY_INTERVAL
			log.info(f"{self._withoutKernel}: linda-keyd was given up: {why}")
			self._lostReplaced = getattr(kernel, "lostReason", None) == keydClient.GOODBYE_REPLACED
			self._sayFellBack()
		# After what the lost sources said, so that an emergency chord is the last word on them.
		self._watchVirtual()
		if self._route == "x11":
			if self._kernel is not None and self._dropKernel():
				log.info(f"{self._withoutKernel}: the key route is set to the X display")
			return
		if not self._wantsKernel() or time.monotonic() < self._nextTry:
			return
		x = self._x
		if not self._takingBack and x is not None and x.virtualKeyboardState == (True, False):
			# Known to go nowhere before asking: asked, the helper would take the keyboard for VIRTUAL_WAIT.
			self._blocked = self._socketIdentity()
			if (why := self._virtualProblem()) != self._logged:
				self._logged = why
				log.info(f"{self._staysWithout}: {why}")
			return
		kernel, why = self._admit(ADMISSION_WAIT)
		if self._stopping:
			if kernel is not None:
				kernel.stop()
			return
		if kernel is None or not self._startKernel(kernel, why):
			self._nextTry = time.monotonic() + RETRY_INTERVAL
			if kernel is None and why != self._logged:
				self._logged = why
				log.info(f"{self._staysWithout}: {why}")
			if self._takingBack:
				self._takingBack = False
				# Translators: Said on Linux when the user asked NVDA to take the keyboard back and linda-keyd,
				# the program that takes the keyboard in the kernel, is not running or does not let it.
				self._say(_("NVDA cannot take the keyboard: linda-keyd is not running or does not let it"))
