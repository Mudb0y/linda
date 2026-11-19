"""Which way keys come to NVDA: from the kernel through linda-keyd where it is installed and admits
NVDA, from the X display otherwise, and the change from one to the other as the helper comes and goes.

Either way NVDA sees one key source. Keys come from the X display from the start, as with no helper,
while the helper is asked to capture; the X route goes only once the helper says it holds a keyboard,
so that there is never a moment with no route that takes NVDA's commands from the applications. From
that moment the keys of the helper's source are NVDA's and the X source's go nowhere: a key the
helper passes on reaches the X display as well, and while the X source is being stopped it would come
to NVDA a second time. Muted, the X source passes every key to the application, as the helper has
decided it already.

On every change each key NVDA was told is down and was not told is up is released to it, and what
NVDA asked of the keyboard, the NVDA keys, browse mode's keys and input help's hold, is asked again of
the new source.

After the emergency chord the helper lets go, and NVDA takes the keyboard back only when the user
asks or NVDA starts again: the user pressed it because something was wrong.
"""

import os
import threading
import time
from collections.abc import Callable
from dataclasses import replace
from functools import partial

from logHandler import log

from . import CaptureMode, GestureKey, KeyEvent, KeySource, keydClient

#: The routes the configuration names: the helper when it admits NVDA, and the X display otherwise; the
#: X display alone.
ROUTES = ("auto", "x11")
#: Seconds between looks for the helper while it is not in use and may be.
RETRY_INTERVAL = 2.0
#: Seconds the route's own thread waits for the helper's answer: beyond the five the helper allows
#: logind and polkit before it says goodbye by itself.
ADMISSION_WAIT = 7.0


class RoutedKeySource:
	"""A KeySource that is the kernel source or the X source, whichever the route gives."""

	def __init__(self, route: str, socketPath: str, notify: Callable[[str], None] | None = None) -> None:
		"""
		:param route: One of ROUTES.
		:param notify: Says a message to the user, from any thread.
		"""
		self._route = route if route in ROUTES else "auto"
		self._socketPath = socketPath
		self._notify = notify
		#: Held while the source changes and while what NVDA asked is recorded and passed on.
		self._lock = threading.RLock()
		#: The source NVDA's questions about the keyboard go to.
		self._active: KeySource | None = None
		self._isKernel = False
		#: A kernel source started, waiting for the helper to hold a keyboard, while the X source works.
		self._pending: KeySource | None = None
		self._pendingWhy = ""
		#: The one source whose keys reach NVDA.
		self._delivering: KeySource | None = None
		self._deliver: Callable[[KeyEvent], bool] | None = None
		self._modeNow: Callable[[], CaptureMode | None] | None = None
		self._consumed: tuple[int, bool] | None = None
		self._gestures: tuple[frozenset[GestureKey], frozenset[GestureKey]] | None = None
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
		#: Source: (scan code, extended): the press NVDA had from it of each key whose release it has not.
		self._keysDown: dict[KeySource, dict[tuple[int, bool], KeyEvent]] = {}

	@property
	def isKernel(self) -> bool:
		return self._isKernel

	def start(
		self,
		deliver: Callable[[KeyEvent], bool],
		modeNow: Callable[[], CaptureMode | None] | None = None,
	) -> None:
		self._deliver, self._modeNow = deliver, modeNow
		if self._route == "x11":
			why = "the key route is set to the X display"
		elif not os.path.exists(self._socketPath):
			why = self._logged = f"linda-keyd is not running, as there is no {self._socketPath}"
			self._nextTry = time.monotonic() + RETRY_INTERVAL
		else:
			why = "until linda-keyd holds the keyboard"
		self._useX(why)
		self._thread = threading.Thread(target=self._supervise, name="keyRoute", daemon=True)
		self._thread.start()

	def stop(self) -> None:
		self._stopping = True
		self._wake.set()
		if self._thread is not None:
			# Asking the helper may take a few seconds, after which the thread lets go of what it got.
			self._thread.join(1)
			self._thread = None
		with self._lock:
			sources = [s for s in (self._pending, self._active) if s is not None]
			self._pending = self._active = self._delivering = None
			self._isKernel = False
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
		if self._isKernel:
			# Translators: Said on Linux when the user asks NVDA to take the keyboard back, and it already
			# takes keys from the kernel.
			self._say(_("NVDA has the keyboard already"))
			return
		self._letGo = False
		self._takingBack = True
		self._nextTry = 0.0
		self._wake.set()

	# What NVDA asks of the keyboard, recorded to be asked again of the next source, and asked of a
	# kernel source waiting for the helper too.

	def _both(self) -> list[KeySource]:
		return [s for s in (self._active, self._pending) if s is not None]

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

	def holdKeyboard(self, hold: bool) -> None:
		with self._lock:
			self._hold = hold
			for source in self._both():
				source.holdKeyboard(hold)

	# The rest goes to the source NVDA's keys come from; during a fallback there is none.

	def setCapsLock(self, on: bool) -> None:
		if (source := self._active) is not None:
			source.setCapsLock(on)

	def inject(self, vkCode: int, scanCode: int, extended: bool, isUp: bool) -> None:
		if (source := self._active) is not None:
			source.inject(vkCode, scanCode, extended, isUp)

	def typeKey(self, vkCode: int, extended: bool, isUp: bool) -> None:
		if (source := self._active) is not None:
			source.typeKey(vkCode, extended, isUp)

	def releaseTypedKeys(self) -> None:
		if (source := self._active) is not None:
			source.releaseTypedKeys()

	def keyState(self, vkCode: int) -> int:
		source = self._active
		return source.keyState(vkCode) if source is not None else 0

	def charForVk(self, vkCode: int) -> int:
		source = self._active
		return source.charForVk(vkCode) if source is not None else 0

	def vkForChar(self, ch: str) -> tuple[int, int]:
		if (source := self._active) is None:
			raise LookupError(ch)
		return source.vkForChar(ch)

	def keyName(self, scanCode: int, extended: bool) -> str:
		source = self._active
		return source.keyName(scanCode, extended) if source is not None else ""

	# Keys.

	def _deliverFrom(self, source: KeySource, event: KeyEvent) -> bool:
		if source is not self._delivering:
			# The X source while the helper holds the keyboard: the key came through the helper, which
			# NVDA has had, and the application has it, whatever this source's grab held it for.
			return True
		down = self._keysDown.setdefault(source, {})
		key = (event.scanCode, event.extended)
		if event.isUp:
			down.pop(key, None)
		elif not event.repeat:
			down[key] = event
		return self._deliver(event)

	def _releaseLeft(self, source: KeySource) -> None:
		"""Release to NVDA each key source gave it the press and not the release of, now that it has gone:
		the release would come to the next source, which never had the press, or to none, and NVDA would
		take the key for held from then on, an NVDA key making every key a command."""
		now = time.monotonic()
		for press in list(self._keysDown.pop(source, {}).values()):
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
			connection, welcome = keydClient.connect(self._socketPath, wait)
		except keydClient.Refused as e:
			return None, f"linda-keyd did not admit NVDA: {e}"
		from . import kernel

		source = kernel.KernelKeySource(connection, welcome, None, None)
		source.setCallbacks(partial(self._onKernelLost, source), partial(self._onKernelHeld, source))
		return source, "linda-keyd admitted NVDA"

	def _replay(self, source: KeySource) -> None:
		with self._lock:
			if self._consumed is not None:
				source.setConsumed(*self._consumed)
			if self._gestures is not None:
				source.setCaptureGestures(*self._gestures)
			if self._capture[0] != "off":
				source.setCapture(*self._capture)
			if self._hold:
				source.holdKeyboard(True)

	def _startKernel(self, kernel: KeySource, why: str) -> bool:
		"""Start a kernel source beside the X source, its keys NVDA's once the helper holds a keyboard."""
		with self._lock:
			self._replay(kernel)
			self._pending, self._pendingWhy = kernel, why
		try:
			kernel.start(partial(self._deliverFrom, kernel), self._modeNow)
		except Exception as e:
			log.warning(f"NVDA cannot take keys from linda-keyd: {e}", exc_info=True)
			with self._lock:
				self._pending = None
			kernel.stop()
			return False
		return True

	def _onKernelHeld(self, kernel: KeySource) -> None:
		"""On the kernel source's own thread, before it reads any key the helper holds: from now on its
		keys are NVDA's, and the X source's go nowhere."""
		if kernel is self._pending:
			self._delivering = kernel
			self._wake.set()

	def _switchToKernel(self) -> None:
		with self._lock:
			kernel, self._pending = self._pending, None
			old, self._active = self._active, kernel
			self._isKernel = True
		if old is not None:
			old.stop()
			self._releaseLeft(old)
		self._logged = None
		log.info(f"Keys come from the kernel through linda-keyd: {self._pendingWhy} and holds the keyboard")
		if self._takingBack:
			self._takingBack = False
			# Translators: Said on Linux when NVDA has taken the keyboard back through linda-keyd, the
			# program that takes the keyboard in the kernel, as the user asked.
			self._say(_("NVDA has the keyboard again"))

	def _useX(self, why: str) -> None:
		"""Read keys from the X display.

		:raise Exception: When it cannot be read.
		"""
		from . import x11

		source = x11.X11KeySource()
		self._delivering = source
		source.start(partial(self._deliverFrom, source), self._modeNow)
		with self._lock:
			self._active, self._isKernel = source, False
			self._replay(source)
		log.info(f"Keys are read from the X display: {why}")

	def _fallBack(self, why: str) -> None:
		try:
			self._useX(why)
		except Exception:
			log.warning("NVDA cannot read the keyboard from the X display either", exc_info=True)

	def _dropActive(self) -> None:
		with self._lock:
			source, self._active = self._active, None
			self._isKernel = False
		if source is not None:
			source.stop()
			self._releaseLeft(source)

	def _dropPending(self) -> None:
		with self._lock:
			source, self._pending = self._pending, None
			if self._delivering is source:
				self._delivering = self._active
		if source is not None:
			source.stop()

	def _onKernelLost(self, kernel: KeySource, why: str, emergency: bool) -> None:
		self._lost.append((kernel, why, emergency))
		self._wake.set()

	def _wantsKernel(self) -> bool:
		return self._route != "x11" and not self._letGo and not self._isKernel and self._pending is None

	def _supervise(self) -> None:
		while not self._stopping:
			wait = None
			if self._wantsKernel():
				wait = max(0.0, self._nextTry - time.monotonic())
			self._wake.wait(wait)
			self._wake.clear()
			if self._stopping:
				return
			try:
				self._turn()
			except Exception:
				log.exception("Choosing the way keys come to NVDA")

	def _turn(self) -> None:
		if self._pending is not None and self._delivering is self._pending:
			# Held, and lost a moment later or not: it is NVDA's route either way, and what follows its loss
			# is the same as for any route NVDA had.
			self._switchToKernel()
		while self._lost:
			kernel, why, emergency = self._lost.pop(0)
			if kernel is self._pending:
				self._dropPending()
				self._nextTry = time.monotonic() + RETRY_INTERVAL
				log.info(
					f"Keys stay with the X display: linda-keyd was given up before it held the keyboard: {why}"
				)
			elif kernel is self._active:
				self._dropActive()
				if emergency:
					self._letGo = True
				self._fallBack(f"linda-keyd was given up: {why}")
				self._nextTry = time.monotonic() + RETRY_INTERVAL
				if emergency:
					self._say(
						# Translators: Said on Linux after the emergency chord, Backspace, Escape and Enter held
						# together, made linda-keyd, the program that takes the keyboard in the kernel for NVDA, let
						# go of it. NVDA then reads keys the way it does without that program.
						_(
							"NVDA has let go of the keyboard, and now reads keys from the X display. "
							"Use the command to take the keyboard back when you want it to take them again",
						),
					)
		if self._route == "x11":
			self._dropPending()
			if self._isKernel:
				self._dropActive()
				self._fallBack("the key route is set to the X display")
			return
		if not self._wantsKernel() or time.monotonic() < self._nextTry:
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
				log.info(f"Keys stay with the X display: {why}")
			if self._takingBack:
				self._takingBack = False
				# Translators: Said on Linux when the user asked NVDA to take the keyboard back and linda-keyd,
				# the program that takes the keyboard in the kernel, is not running or does not let it.
				self._say(_("NVDA cannot take the keyboard: linda-keyd is not running or does not let it"))
			return
		if self._active is None:
			# The X display could not be read: better the helper's keys at once than none.
			self._delivering = kernel
			self._switchToKernel()
