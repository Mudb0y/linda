"""Which way keys come to NVDA: from the kernel through linda-keyd where it is installed and admits
NVDA, from the X display otherwise, and the change from one to the other as the helper comes and goes.

Either way NVDA sees one key source. On a change every key NVDA was told is down is released to it
first, and what NVDA asked of the keyboard, the NVDA keys, browse mode's keys and input help's hold,
is asked again of the new source. The X route is stopped before the helper is asked to capture, as
both at once would hand NVDA each key the helper passes twice; the keys typed in between reach the
applications as they would with no screen reader.

After the emergency chord the helper lets go, and NVDA takes the keyboard back only when the user
asks or NVDA starts again: the user pressed it because something was wrong.
"""

import os
import threading
import time
from collections.abc import Callable
from dataclasses import replace

from logHandler import log

from . import CaptureMode, GestureKey, KeyEvent, KeySource, keydClient

#: The routes the configuration names: the helper when it admits NVDA and the X display otherwise; the
#: helper alone; the X display alone.
ROUTES = ("auto", "kernel", "x11")
#: Seconds between looks for the helper while it is not in use and may be.
RETRY_INTERVAL = 2.0
#: Seconds NVDA waits at its start for the helper to admit it before it reads keys from the X display
#: instead and goes on asking: the helper asks logind and polkit, which take milliseconds once running.
START_ADMISSION_WAIT = 2.5
#: Seconds the route's own thread waits for an answer: beyond the five the helper allows logind and
#: polkit before it says goodbye by itself.
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
		self._active: KeySource | None = None
		self._isKernel = False
		self._deliver: Callable[[KeyEvent], bool] | None = None
		self._modeNow: Callable[[], CaptureMode | None] | None = None
		self._consumed: tuple[int, bool] | None = None
		self._gestures: tuple[frozenset[GestureKey], frozenset[GestureKey]] | None = None
		self._capture: tuple[CaptureMode, int | None] = ("off", None)
		self._hold = False
		self._wake = threading.Event()
		self._stopping = False
		self._thread: threading.Thread | None = None
		#: Why the helper was given up, and whether by the emergency chord, for the route's thread.
		self._lost: tuple[str, bool] | None = None
		#: The emergency chord was pressed: no helper until the user asks for it.
		self._letGo = False
		self._takingBack = False
		self._nextTry = 0.0
		#: The last reason the helper was not used that went to the log, so that each goes there once.
		self._logged: str | None = None
		#: (scan code, extended): the press NVDA was given of each key it was not given the release of.
		self._keysDown: dict[tuple[int, bool], KeyEvent] = {}

	@property
	def isKernel(self) -> bool:
		return self._isKernel

	def start(
		self,
		deliver: Callable[[KeyEvent], bool],
		modeNow: Callable[[], CaptureMode | None] | None = None,
	) -> None:
		self._deliver, self._modeNow = deliver, modeNow
		why = "the key route is set to the X display"
		if self._route != "x11":
			kernel, why = self._admit(START_ADMISSION_WAIT)
			if kernel is not None:
				why = None if self._useKernel(kernel, why) else "NVDA could not take keys from linda-keyd"
		if why is not None:
			self._logged = why
			self._nextTry = time.monotonic() + RETRY_INTERVAL
			if self._route == "kernel":
				log.warning(f"NVDA reads no keys: it is set to take them from the kernel alone, and {why}")
				self._say(
					# Translators: Said at NVDA's start on Linux when its advanced setting has it take keys only
					# through linda-keyd, the program that takes the keyboard in the kernel, and that program does
					# not let it.
					_("NVDA is set to take keys from the kernel alone, and cannot; it reads no keys"),
				)
			else:
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
			source, self._active = self._active, None
			self._isKernel = False
		if source is not None:
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

	# What NVDA asks of the keyboard, recorded to be asked again of the next source.

	def setConsumed(self, nvdaModifierKeys: int, numpadReview: bool) -> None:
		with self._lock:
			self._consumed = (nvdaModifierKeys, numpadReview)
			if self._active is not None:
				self._active.setConsumed(nvdaModifierKeys, numpadReview)

	def setCapture(self, mode: CaptureMode, processID: int | None = None) -> None:
		with self._lock:
			self._capture = (mode, processID)
			if self._active is not None:
				self._active.setCapture(mode, processID)

	def setCaptureGestures(self, browse: frozenset[GestureKey], focus: frozenset[GestureKey]) -> None:
		with self._lock:
			self._gestures = (browse, focus)
			if self._active is not None:
				self._active.setCaptureGestures(browse, focus)

	def holdKeyboard(self, hold: bool) -> None:
		with self._lock:
			self._hold = hold
			if self._active is not None:
				self._active.holdKeyboard(hold)

	# The rest goes to whichever source there is now; during a change there is none.

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

		return kernel.KernelKeySource(connection, welcome, self._onKernelLost), "linda-keyd admitted NVDA"

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

	def _useKernel(self, kernel: KeySource, why: str) -> bool:
		self._replay(kernel)
		try:
			kernel.start(self._deliverKey, self._modeNow)
		except Exception as e:
			log.warning(f"NVDA cannot take keys from linda-keyd: {e}", exc_info=True)
			kernel.stop()
			return False
		with self._lock:
			self._active, self._isKernel = kernel, True
			# What NVDA asked while the source was being started.
			self._replay(kernel)
		self._logged = None
		log.info(f"Keys come from the kernel through linda-keyd: {why}")
		return True

	def _useX(self, why: str) -> None:
		"""Read keys from the X display.

		:raise Exception: When it cannot be read.
		"""
		from . import x11

		source = x11.X11KeySource()
		source.start(self._deliverKey, self._modeNow)
		with self._lock:
			self._active, self._isKernel = source, False
			self._replay(source)
		log.info(f"Keys are read from the X display: {why}")

	def _fallBack(self, why: str) -> None:
		try:
			self._useX(why)
		except Exception:
			log.warning("NVDA cannot read the keyboard from the X display either", exc_info=True)

	def _drop(self) -> None:
		with self._lock:
			source, self._active = self._active, None
			self._isKernel = False
		if source is not None:
			source.stop()
		# The release of a key held as the source went comes to the next one, which never had its press,
		# or to none: NVDA would take the key for held from then on, an NVDA key making every key a
		# command.
		now = time.monotonic()
		for press in list(self._keysDown.values()):
			self._deliverKey(
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

	def _deliverKey(self, event: KeyEvent) -> bool:
		key = (event.scanCode, event.extended)
		if event.isUp:
			self._keysDown.pop(key, None)
		elif not event.repeat:
			self._keysDown[key] = event
		return self._deliver(event)

	def _onKernelLost(self, why: str, emergency: bool) -> None:
		self._lost = (why, emergency)
		self._wake.set()

	def _wantsKernel(self) -> bool:
		return self._route != "x11" and not self._letGo and not self._isKernel

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
		lost, self._lost = self._lost, None
		if lost is not None and self._isKernel:
			why, emergency = lost
			self._drop()
			if emergency:
				self._letGo = True
			if self._route != "kernel" or emergency:
				self._fallBack(f"linda-keyd was given up: {why}")
			else:
				log.warning(
					f"NVDA reads no keys: linda-keyd was given up, as {why}, and NVDA is set to the kernel"
				)
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
		if self._route == "x11" and self._isKernel:
			self._drop()
			self._fallBack("the key route is set to the X display")
			return
		if not self._wantsKernel() or time.monotonic() < self._nextTry:
			return
		kernel, why = self._admit(ADMISSION_WAIT)
		if self._stopping:
			if kernel is not None:
				kernel.stop()
			return
		takingBack, self._takingBack = self._takingBack, False
		if kernel is None:
			self._nextTry = time.monotonic() + RETRY_INTERVAL
			if why != self._logged:
				self._logged = why
				log.info(f"Keys stay with the X display: {why}")
			if takingBack:
				# Translators: Said on Linux when the user asked NVDA to take the keyboard back and linda-keyd,
				# the program that takes the keyboard in the kernel, is not running or does not let it.
				self._say(_("NVDA cannot take the keyboard: linda-keyd is not running or does not let it"))
			return
		# The X route goes first, or the keys the helper passes would come to NVDA twice.
		self._drop()
		if self._useKernel(kernel, why):
			if takingBack:
				# Translators: Said on Linux when NVDA has taken the keyboard back through linda-keyd, the
				# program that takes the keyboard in the kernel, as the user asked.
				self._say(_("NVDA has the keyboard again"))
			return
		self._nextTry = time.monotonic() + RETRY_INTERVAL
		if self._route != "kernel":
			self._fallBack("NVDA could not take keys from linda-keyd")
