"""Keys in a Wayland session: from the kernel through linda-keyd alone, with no X source beneath.

There is no X display to read keys from, and none is opened: a client's first X connection would
start Xwayland on GNOME, and a reader of the X display there hears only the X programs' keys. So
until linda-keyd admits NVDA and holds the keyboard, NVDA has no keys, and says so; the screen is
read meanwhile. The layout comes from the desktop's settings and a document's keys from AT-SPI,
through the one WaylandEnvironment every kernel source is attached to in turn.

Admission, asking again every two seconds, losing the helper, asking each new kernel source what NVDA
asked, releasing what a lost source left down, and the emergency chord are RoutedKeySource's own,
shared with the X route and unchanged. What differs is said here: the X source's part, the wording,
and a block that only a new helper or the user lifts, since no X display can show that the helper's
virtual keyboard is read again.
"""

import collections
import os
import time
from functools import partial

from logHandler import log

from . import VIRTUAL_KEYBOARD, CaptureMode, KeySource, keydClient
from .route import RoutedKeySource
from .waylandEnvironment import WaylandEnvironment

#: What the route asks the helper for. The helper grants those it knows, so this never has to change
#: as it learns more.
FEATURES = (
	keydClient.FEATURE_WAIT
	| keydClient.FEATURE_TYPING
	| keydClient.FEATURE_LATE_PASS
	| keydClient.FEATURE_VIRTUAL_SEEN
)
#: Losses of the helper within LOSS_SPAN seconds after which NVDA says the helper keeps stopping, and
#: no more of them until the losses thin out again.
LOSSES_TOO_MANY = 3
LOSS_SPAN = 120.0
#: compositor() names whose session has a run dialog on Alt+F2 that starts a program by name.
RUN_DIALOG_DESKTOPS = ("gnome", "kde")


class WaylandRoute(RoutedKeySource):
	"""A KeySource that is the kernel source whenever linda-keyd serves NVDA, and nothing otherwise."""

	_withoutKernel = "NVDA has no keys in this Wayland session"
	_staysWithout = "NVDA stays without keys in this Wayland session"

	def __init__(
		self,
		route: str,
		socketPath: str,
		notify=None,
		environment: WaylandEnvironment | None = None,
		holdEvents: float = 0.0,
	) -> None:
		"""
		:param route: One of route.ROUTES; x11 is taken as auto, as there is no X display to read.
		:param environment: The environment for every kernel source; by default one over the desktop's
			layout and AT-SPI, made as the route starts.
		:param holdEvents: Seconds the default environment's capture feed holds back window events, for
			tests.
		"""
		self._routeLogged = False
		super().__init__(self._ownRoute(route), socketPath, notify, VIRTUAL_KEYBOARD)
		self._environment = environment
		self._holdEvents = holdEvents
		#: Lines to say, from threads that must not wait on NVDA's speech: said by the route's own thread.
		self._toSay: collections.deque[str] = collections.deque()
		self._saidNoHelper = False
		self._saidHasKeyboard = False
		self._hinted = False
		self._saidUnheard = False
		self._saidNotices: set[str] = set()
		#: The helper's sockets, by identity, for which BUSY, or REPLACED, which BUSY follows, was said.
		self._saidBusyFor: set[tuple[int, int, int] | None] = set()
		#: Kernel sources the helper said goodbye to as REPLACED, and the last one given up.
		self._replaced: set[KeySource] = set()
		self._dropped: KeySource | None = None
		#: When the helper was lost, for the last LOSS_SPAN seconds.
		self._losses: collections.deque[float] = collections.deque()
		self._waitLogged = False
		self._loggedCapture: CaptureMode | None = None

	def _ownRoute(self, route: str) -> str:
		if route != "x11":
			return route
		if not self._routeLogged:
			self._routeLogged = True
			log.info(
				"The key route is set to the X display, which this Wayland session does not read keys from; "
				"keys come from linda-keyd"
			)
		return "auto"

	def setRoute(self, route: str) -> None:
		super().setRoute(self._ownRoute(route))

	def _startBase(self, why: str) -> None:
		environment = self._environment
		if environment is None:
			from linuxPlatform import testHooks

			environment = WaylandEnvironment(
				os.environ.get("XDG_CURRENT_DESKTOP"),
				self._holdEvents or testHooks.captureDelay() / 1000,
			)
		environment.onDevice = self._onDevice
		environment.onNotice = self._onNotice
		environment.start()
		with self._lock:
			self._environment = environment
			if self._capture[0] != "off":
				environment.setCapture(*self._capture)
		log.info(f"{self._withoutKernel}: {why}")
		if not os.path.exists(self._socketPath):
			self._saidNoHelper = True
			self._say(
				# Translators: Said on Linux as NVDA starts in a Wayland session where linda-keyd, the program
				# that takes the keyboard in the kernel, is not running: NVDA has no keys at all there without
				# it.
				_(
					"NVDA cannot take keys in this Wayland session until linda-keyd runs. "
					"It reads the screen meanwhile. The readme says how to install linda-keyd.",
				),
			)

	def stop(self) -> None:
		super().stop()
		environment = self._environment
		if environment is not None:
			environment.stop()

	def _connect(self, wait: float):
		connection, welcome = keydClient.connect(self._socketPath, wait, FEATURES)
		if not welcome.features & keydClient.FEATURE_WAIT and not self._waitLogged:
			self._waitLogged = True
			log.info(
				"linda-keyd cannot hold a key while NVDA looks where the focus is, so a key typed just "
				"as the focus moves is passed on"
			)
		return connection, welcome

	def _newKernel(self, connection, welcome) -> KeySource:
		from . import kernel

		source = kernel.KernelKeySource(connection, welcome, environment=self._environment)
		source.setCallbacks(partial(self._onKernelLost, source), partial(self._onKernelHeld, source))
		return source

	# What is said.

	def _later(self, message: str) -> None:
		"""Say message from the route's own thread, as soon as it turns."""
		self._toSay.append(message)
		self._wake.set()

	def _turn(self) -> None:
		try:
			super()._turn()
		finally:
			while self._toSay:
				self._say(self._toSay.popleft())

	def _refused(self, error: keydClient.Refused) -> None:
		goodbye = error.goodbye
		if goodbye is None or goodbye.reason != keydClient.GOODBYE_BUSY:
			return
		identity = self._socketIdentity()
		if identity in self._saidBusyFor:
			return
		self._saidBusyFor.add(identity)
		self._say(
			# Translators: Said on Linux in a Wayland session when linda-keyd, the program that takes the
			# keyboard in the kernel, turns NVDA away because another program of this session has it.
			_(
				"Another program holds linda-keyd for this session, so NVDA has no keys. "
				"If you started no other screen reader, something may be reading your keys.",
			),
		)

	def _onKernelLost(self, kernel: KeySource, why: str, emergency: bool) -> None:
		if not emergency and getattr(kernel, "lostReason", None) == keydClient.GOODBYE_REPLACED:
			self._replaced.add(kernel)
			self._saidBusyFor.add(self._socketIdentity())
			self._later(
				# Translators: Said on Linux in a Wayland session when another program took NVDA's place
				# with linda-keyd, the program that takes the keyboard in the kernel.
				_("Another program took NVDA's place with linda-keyd and reads every key now."),
			)
		super()._onKernelLost(kernel, why, emergency)

	def _dropKernel(self) -> bool:
		self._dropped = self._kernel
		return super()._dropKernel()

	def _sayFellBack(self) -> None:
		dropped, self._dropped = self._dropped, None
		if dropped in self._replaced:
			# Said as it happened, in the words for it.
			self._replaced.discard(dropped)
			return
		now = time.monotonic()
		while self._losses and self._losses[0] <= now - LOSS_SPAN:
			self._losses.popleft()
		self._losses.append(now)
		if len(self._losses) > LOSSES_TOO_MANY:
			return
		if len(self._losses) == LOSSES_TOO_MANY:
			self._say(
				# Translators: Said on Linux in a Wayland session when linda-keyd, the program that takes the
				# keyboard in the kernel, has stopped three times in two minutes.
				_("linda-keyd keeps stopping. NVDA has no keys until it runs again."),
			)
			return
		self._say(
			# Translators: Said on Linux in a Wayland session when linda-keyd, the program that takes the
			# keyboard in the kernel, went away or failed: NVDA has no keys until it is back.
			_("NVDA has lost the keyboard and takes it again as soon as linda-keyd lets it."),
		)

	def _sayEmergency(self) -> None:
		from linuxPlatform import displayServer

		if displayServer.compositor() in RUN_DIALOG_DESKTOPS:
			self._say(
				# Translators: Said on Linux in a GNOME or KDE Wayland session after the emergency chord,
				# Backspace, Escape and Enter held together, made linda-keyd, the program that takes the
				# keyboard in the kernel, let go of it. NVDA then has no keys at all. Alt+F2 opens the
				# desktop's dialog that runs a command, and linda is the command that starts NVDA.
				_(
					"NVDA has let go of the keyboard and has no keys now. "
					"To give it the keyboard back, press Alt+F2, type linda and press Enter.",
				),
			)
			return
		self._say(
			# Translators: Said on Linux in a Wayland session after the emergency chord, Backspace, Escape
			# and Enter held together, made linda-keyd, the program that takes the keyboard in the kernel,
			# let go of it. NVDA then has no keys at all. linda --take-keyboard is a command to type as it
			# is.
			_(
				"NVDA has let go of the keyboard and has no keys now. "
				"To give it the keyboard back, start NVDA again, or run linda --take-keyboard.",
			),
		)

	def _watchVirtual(self) -> None:
		taken = self._isKernel
		super()._watchVirtual()
		if self._isKernel and not taken:
			self._tookKeyboard()

	def _tookKeyboard(self) -> None:
		"""The helper holds the keyboard for NVDA and the route is taken."""
		if self._saidNoHelper and not self._saidHasKeyboard:
			self._saidHasKeyboard = True
			# Translators: Said on Linux in a Wayland session when NVDA takes keys through linda-keyd, the
			# program that takes the keyboard in the kernel, after it said at start that it had none.
			self._say(_("NVDA has the keyboard"))
		if not self._hinted:
			self._hinted = True
			self._say(
				# Translators: Said on Linux in a Wayland session the first time NVDA takes the keyboard
				# through linda-keyd, the program that takes it in the kernel: what to do should the keys
				# NVDA passes on stop reaching the programs, the emergency chord that makes it let go.
				_("If typing stops reaching programs, hold Backspace, Escape and Enter together."),
			)

	def _onDevice(self, status: int) -> None:
		if status in (keydClient.DEVICE_BUSY, keydClient.DEVICE_IGNORED) and not self._saidUnheard:
			self._saidUnheard = True
			self._later(
				# Translators: Said on Linux in a Wayland session when linda-keyd, the program that takes the
				# keyboard in the kernel, cannot take one of the keyboards, or leaves it alone: NVDA does not
				# hear what is typed on it.
				_("NVDA cannot hear one of the keyboards, so its keys reach programs without NVDA."),
			)

	def _onNotice(self, notice: str) -> None:
		if notice not in self._saidNotices:
			self._saidNotices.add(notice)
			self._later(notice)

	# The keyboard.

	def _isBlocked(self) -> bool:
		if self._blocked is None:
			return False
		# No X display can say the helper's virtual keyboard is read again; another helper may.
		if self._socketIdentity() not in (self._blocked, None):
			self._blocked = None
			self._nextTry = 0.0
			return False
		return True

	def setCapture(self, mode: CaptureMode, processID: int | None = None) -> None:
		with self._lock:
			super().setCapture(mode, processID)
			# The kernel source leaves where a document's keys go to its environment, which must know
			# even before the helper admits NVDA.
			if self._environment is not None:
				self._environment.setCapture(mode, processID)
			if mode != self._loggedCapture:
				# As the X source records it, a change of mode only, as NVDA asks again and again.
				self._loggedCapture = mode
				log.debug(f"Keys captured for {mode} mode")

	def _sender(self) -> KeySource | None:
		kernel = self._kernel
		if kernel is not None and self._isKernel and getattr(kernel, "canType", False):
			return kernel
		return None

	@property
	def canSend(self) -> bool:
		"""Whether a key sent to the programs goes anywhere."""
		return self._sender() is not None

	def keyState(self, vkCode: int) -> int:
		kernel = self._kernel
		if kernel is not None:
			# Its locks are the helper's last word whether or not it holds a keyboard now.
			return kernel.keyState(vkCode)
		from .kernel import LOCK_LIGHTS

		environment = self._environment
		light = LOCK_LIGHTS.get(vkCode)
		if light is None or environment is None:
			return 0
		locks = environment.locks
		if locks is None and environment.layout is not None:
			locks = environment.layout.locksGuess
		return 1 if locks and locks & light else 0

	def _layout(self):
		environment = self._environment
		return environment.queries if environment is not None else None
