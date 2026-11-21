"""The kernel key source's surroundings in a Wayland session: the layout from the desktop's own settings
and a document's keys from AT-SPI, with no X server to ask either.

One environment serves the route for NVDA's whole life, and each kernel source the helper admits is
attached to it in turn. It follows the layout for the route's own questions, what a key types and what
it is called, whether or not a kernel source exists, and for the kernel source attached. What the kernel
source reports of the keys it passed goes to callbacks that do nothing until a later part of NVDA sets
them, so that it can listen without an edit here.
"""

import threading
from collections.abc import Callable

from logHandler import log
from xkbcommon import xkb

from . import atspiCapture, desktopLayout, keyMap

#: Seconds between looks at whether the capture feed still has its connection to the accessibility
#: bus, the first time it is found without; doubled at each failed start up to KEEP_MOST.
KEEP_INTERVAL = 2.0
KEEP_MOST = 60.0


def _nothing(*_args: object) -> None:
	pass


class LayoutQueries:
	"""What keyboardHandler asks of the layout, answered from the desktop's layout alone: the route's
	own translator, which needs no kernel source."""

	def __init__(self) -> None:
		self._lock = threading.Lock()
		self._translator: keyMap.Translator | None = None
		self._names: tuple[str, ...] | None = None

	def use(self, layout: desktopLayout.LayoutSnapshot) -> None:
		"""Answer in layout from now on, compiled on the caller's thread.

		:raise Exception: When the layout cannot be compiled.
		"""
		names = tuple(layout.names)
		translator = self._translator
		if translator is not None and names == self._names:
			keymap = translator.keymap
		else:
			rules, model, name, variant, options = names
			keymap = xkb.Context().keymap_new_from_names(
				rules=rules or None,
				model=model or None,
				layout=name or None,
				variant=variant or None,
				options=options or None,
			)
		state = keymap.state_new()
		state.update_mask(0, 0, 0, 0, 0, layout.group)
		with self._lock:
			self._translator = keyMap.Translator(keymap, state)
			self._names = names

	def charForVk(self, vkCode: int) -> int:
		with self._lock:
			return self._translator.mapVirtualKeyToChar(vkCode) if self._translator else 0

	def vkForChar(self, ch: str) -> tuple[int, int]:
		with self._lock:
			if self._translator is None:
				raise LookupError(ch)
			return self._translator.vkKeyScan(ch)

	def keyName(self, scanCode: int, extended: bool) -> str:
		with self._lock:
			return self._translator.keyName(scanCode, extended) if self._translator else ""


class WaylandEnvironment:
	"""A KernelEnvironment, as linuxPlatform.keys.environment describes it, over the desktop's layout
	feed and the AT-SPI capture feed.

	The callbacks onPassed, onPassedLock, onLocks, onVirtual and onDevice get the kernel source's notes
	of the same names, with the same arguments, on the thread that answers keys: like the notes, each
	must only record and return. onNotice gets each sentence a layout feed has for the user, on the
	feed's thread.
	"""

	def __init__(
		self,
		currentDesktop: str | None,
		holdEvents: float = 0.0,
		layoutFeed: desktopLayout.LayoutFeed | None = None,
		captureFeed: atspiCapture.AtspiCaptureFeed | None = None,
	) -> None:
		"""
		:param currentDesktop: XDG_CURRENT_DESKTOP, which chooses the layout feed.
		:param holdEvents: Seconds the capture feed holds back every window event, for tests.
		"""
		self._currentDesktop = currentDesktop
		self._layoutFeed = layoutFeed
		self.feed = captureFeed if captureFeed is not None else atspiCapture.AtspiCaptureFeed(holdEvents)
		self.queries = LayoutQueries()
		#: The layout now, replaced whole on each change.
		self.layout: desktopLayout.LayoutSnapshot | None = None
		#: Held while a layout goes to the kernel source, so that one attached as the layout changes
		#: cannot be left with the older of the two.
		self._layoutLock = threading.Lock()
		self._source = None
		#: keydClient's LOCK_ bits of the lights the helper last said are on; None until it has.
		self.locks: int | None = None
		self.onPassed: Callable[[int, bool], None] = _nothing
		self.onPassedLock: Callable[[int, float], None] = _nothing
		self.onLocks: Callable[[int, float], None] = _nothing
		self.onVirtual: Callable[[int], None] = _nothing
		self.onDevice: Callable[[int], None] = _nothing
		self.onNotice: Callable[[str], None] = _nothing
		self._stopping = threading.Event()
		self._keeper: threading.Thread | None = None

	# Running.

	def start(self) -> None:
		"""Read the layout, waiting for the desktop's first answer, and connect the capture feed, before
		any kernel source is attached."""
		if self._layoutFeed is None:
			self._layoutFeed = desktopLayout.create(self._currentDesktop)
		try:
			snapshot = self._layoutFeed.start(self._onLayout)
		except Exception:
			log.exception("Keyboard layout: the desktop's layout cannot be followed")
			self._layoutFeed = None
			snapshot = desktopLayout.environmentSnapshot()
		self._useLayout(snapshot)
		self.feed.start()
		self._stopping.clear()
		self._keeper = threading.Thread(target=self._keep, name="lindaCaptureKeeper", daemon=True)
		self._keeper.start()

	def stop(self) -> None:
		self._stopping.set()
		if self._keeper is not None:
			self._keeper.join(atspiCapture.START_TIMEOUT + 1)
			self._keeper = None
		self.feed.stop()
		if self._layoutFeed is not None:
			self._layoutFeed.stop()

	def _keep(self) -> None:
		"""Connect the capture feed again whenever it has lost its connection, or never had one: nothing
		else does, and without it no key of a document is captured."""
		wait = KEEP_INTERVAL
		while not self._stopping.wait(wait):
			if self.feed.canLook:
				wait = KEEP_INTERVAL
				continue
			try:
				self.feed.start()
			except Exception:
				log.exception("AT-SPI capture feed: starting it again failed")
			wait = KEEP_INTERVAL if self.feed.canLook else min(wait * 2, KEEP_MOST)

	def _onLayout(self, snapshot: desktopLayout.LayoutSnapshot) -> None:
		try:
			self._useLayout(snapshot)
		except Exception:
			log.exception("Keyboard layout: using a change failed")

	def _useLayout(self, snapshot: desktopLayout.LayoutSnapshot) -> None:
		with self._layoutLock:
			self.queries.use(snapshot)
			self.layout = snapshot
			source = self._source
			if source is not None:
				try:
					source.useLayout(snapshot)
				except Exception:
					log.exception("Keyboard layout: the kernel key source cannot read keys in it")
		if snapshot.notice:
			self.onNotice(snapshot.notice)

	def setCapture(self, mode: str, processID: int | None = None) -> None:
		"""What NVDA says of the document its focus is in, which the capture feed needs whether or not a
		kernel source exists."""
		self.feed.setCapture(mode, processID)

	# The kernel source's environment.

	def attach(self, source) -> None:
		with self._layoutLock:
			layout = self.layout
			if layout is None:
				raise RuntimeError("the keyboard layout is not known yet")
			self._source = source
			try:
				source.useLayout(layout)
			except Exception:
				self._source = None
				raise

	def detach(self, source) -> None:
		# Without the layout's lock, which a layout being compiled holds: this must not wait. A layout
		# that reaches the source after this changes nothing that matters to one that has stopped.
		if self._source is source:
			self._source = None

	@property
	def captureState(self):
		return self.feed.captureState

	def captureNow(self, now: float):
		return self.feed.captureNow(now)

	@property
	def canLook(self) -> bool:
		return self.feed.canLook

	def requestLook(self, number: int, now: float) -> float:
		source = self._source
		if source is None:
			raise LookupError("no kernel key source is attached")
		return self.feed.requestLook(number, now, source.lookDone)

	def noteFocusMayMove(self, now: float) -> None:
		self.feed.noteFocusMayMove(now)

	def noteDevice(self, status: int) -> None:
		self.onDevice(status)

	def notePassed(self, keycode: int, isUp: bool) -> None:
		self.onPassed(keycode, isUp)

	def notePassedLock(self, light: int, now: float) -> None:
		self.onPassedLock(light, now)

	def noteLocks(self, locks: int, now: float) -> None:
		self.locks = locks
		self.onLocks(locks, now)

	def noteVirtual(self, status: int) -> None:
		self.onVirtual(status)
