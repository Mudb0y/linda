"""What the kernel key source takes from its surroundings when there is no X server to take it from,
as in a Wayland session: the keyboard layout, where a document's keys go, and an ear for what becomes
of the keys it passes on.

Both are structural protocols. Whatever follows the layout or the focus matches them by shape and
imports nothing from here, so that this file is the one statement of what the kernel source asks.

The kernel source calls the methods below from the thread that answers keys, which must answer the
helper in time however busy NVDA's other threads keep the interpreter. The exceptions: attach is
called from the thread that starts the source; detach from the thread that stops it, or from the thread
that answers keys as it loses the helper; and captureNow from the thread that puts keys in NVDA's terms
as well. So each method but attach only reads state replaced whole, or appends and writes to a pipe,
and never calls into a C library or waits: whatever follows the layout or the focus does its work on
threads of its own. What one raises is logged and taken as no capture, no look or nothing heard.

The notes are handed key codes, which are the user's typing: none may go into a log, nor into an
exception, as a KeyError of one would. The kernel source logs what a method raises by its type and
frames only, but whatever else catches it may not.
"""

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
	from . import x11
	from .kernel import KernelKeySource


class Layout(Protocol):
	"""The keyboard layout as the desktop has it, which the kernel source compiles to read keys in."""

	#: XKB's rules, model, layout, variant and options, as the X server's _XKB_RULES_NAMES gives them.
	names: tuple[str, str, str, str, str]
	#: The locked group, from 0.
	group: int
	#: Whether keys repeat, and the seconds before the first repeat and between repeats; None for the
	#: kernel source's own defaults.
	repeat: tuple[bool, float, float] | None
	#: keydClient's LOCK_ bits of the locks the desktop says are on at start, until the helper's word;
	#: None when it does not say.
	locksGuess: int | None
	#: Where the layout was read from, for the log.
	source: str
	#: Whether an input method is in front, which makes the characters a key types those of the method
	#: rather than of the layout.
	inputMethod: bool
	#: A sentence to say once about the layout, as when it could not be read and is a guess.
	notice: str | None


class KernelEnvironment(Protocol):
	"""The kernel source's surroundings in a session with no X server to read them from."""

	def attach(self, source: "KernelKeySource") -> None:
		"""Follow the layout for source, calling source.useLayout with the layout there is now before
		returning; and call it again, from a thread of the environment's own, on every change.

		:raise Exception: When there is no layout to give, which keeps source from starting.
		"""

	def detach(self, source: "KernelKeySource") -> None:
		"""Stop telling source anything; it is stopping or has lost the helper."""

	@property
	def captureState(self) -> "x11.CaptureState":
		"""Where a document's keys go, replaced whole as it changes."""

	def captureNow(self, now: float) -> "x11.CaptureState":
		"""Where a document's keys go for a key that came at now."""

	@property
	def canLook(self) -> bool:
		"""Whether a key whose kind turns on where the focus is can wait for a look at it."""

	def requestLook(self, number: int, now: float) -> float:
		"""Find out where the focus is for a key that came at now, and say so by calling the source's
		lookDone(number, known) once it is known, or once it cannot be, from any thread.

		:return: The time on time.monotonic()'s clock by which the look will have ended.
		"""

	def noteFocusMayMove(self, now: float) -> None:
		"""A chord that commonly moves the focus to another window was passed on at now."""

	def noteDevice(self, status: int) -> None:
		"""The helper said a keyboard is in this state, one of keydClient's DEVICE_ values."""

	def notePassed(self, keycode: int, isUp: bool) -> None:
		"""A key passed on to the desktop, as an X key code, or its release; kept in memory only."""

	def notePassedLock(self, light: int, now: float) -> None:
		"""A lock key passed on at now, by keydClient's LOCK_ bit of the light it turns on or off."""

	def noteLocks(self, locks: int, now: float) -> None:
		"""The helper said at now which lights are on, as keydClient's LOCK_ bits."""

	def noteVirtual(self, status: int) -> None:
		"""The helper said whether the desktop reads its virtual keyboard."""
