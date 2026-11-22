"""Whether the keys NVDA passes on in a Wayland session reach the desktop.

With no X display there is no device list to show that the desktop reads the helper's virtual
keyboard, and a desktop that does not leaves every key NVDA passes on going nowhere, NVDA and the
helper both answering as they should. So the route has this watch say, for each time the helper holds
the keyboard, one of three things: proved present, proved absent, or not known.

Presence is proved by a character typed that matched NVDA's layout, which only a key that reached the
program can make; by sway listing the virtual keyboard with its events on; and, once the helper probes
the virtual keyboard's lights, by the desktop having written them. A proof is remembered in Linda's
state directory by the desktop's name and the day, after which the rescue hint is not said at start.

Absence is proved by sway's listing, one second after the helper holds the keyboard or at any change
after that while it holds, saying the virtual keyboard is not there or takes no events, a removal
being asked about again a second later; and by the lock echo. The
echo is hedged against a keyboard remapped under NVDA's feet, since what it costs when wrong is every
command of NVDA's until the user takes the keyboard back: it is taken only where NVDA's layout comes
from the desktop's own settings, options and all; a lock key is noted only by the light the
layout has it change, so that under caps:escape no Caps Lock is a lock; a press counts only for a light
this desktop has answered with the helper's LOCKS before in this run; two presses running must go
unanswered for 0.5 seconds each, with no typed character matched since the last that was answered; a
press followed by another of the same light within the wait proves nothing, since the two may cancel;
and none of it is taken while sway says it takes the keyboard's events, which is the better word.

Only counts, lights by bit and the desktop's name go anywhere: no key code or character.
"""

import collections
import json
import os
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from logHandler import log

from . import keydClient

PRESENT, ABSENT, UNKNOWN = "present", "absent", "unknown"
#: Seconds after the helper holds the keyboard by which sway must list its virtual keyboard: the helper
#: makes it and waits for it to settle before it grabs anything.
LISTING_WAIT = 1.0
#: Seconds within which a change of the helper's LOCKS answers a lock key passed on.
ECHO_WAIT = 0.5
#: Lock key presses unanswered in a row that prove absence.
ECHO_RUN = 2
#: The layout feeds, by LayoutSnapshot.source, whose layout holds the options the compositor itself
#: maps keys by, under which alone the lock echo is taken. Under sway's, localed's or the XKB_DEFAULT
#: variables' an option set in the compositor's own configuration is unseen: Hyprland's
#: caps:escape_shifted_capslock would leave Shift+Caps Lock answered and every plain one an Escape that
#: goes unanswered. gamescope's options are its own environment's, which NVDA's need not be.
ECHO_LAYOUTS = ("gnome", "kde")
#: Seconds between readings of the typed characters matched, while presence is not proved.
MATCHES_INTERVAL = 1.0
#: The file, in Linda's state directory, of the desktops a proof of presence was had on.
RECORD_NAME = "keyDelivery"
#: A record's mark of a proof by the helper's probe of the virtual keyboard's lights.
PROBED = "probed"
#: Named by a test with a stand-in for the helper: the identifier sway gives the keyboard keys go out on,
#: and the record's path. Neither is read for the helper's own socket.
TEST_IDENTIFIER_VARIABLE = "LINDA_TEST_DELIVERY_IDENTIFIER"
TEST_RECORD_VARIABLE = "LINDA_TEST_DELIVERY_RECORD"


def compositorName() -> str | None:
	"""The desktop a proof is remembered for: displayServer's name, or for one it does not know the first
	name XDG_CURRENT_DESKTOP gives; None when nothing names it."""
	from linuxPlatform import displayServer

	name = displayServer.compositor()
	if name != "other":
		return name
	first = (os.environ.get("XDG_CURRENT_DESKTOP") or "").split(":")[0].strip().lower()
	return "".join(c for c in first if c.isalnum() or c in "-_.") or None


def layoutMatches() -> int:
	"""typedChars' count of characters typed that matched NVDA's layout, without importing it: NVDA
	imports it with its objects, and a route used without them has no typing to match."""
	typedChars = sys.modules.get("linuxPlatform.atspi.typedChars")
	return typedChars.layoutMatches() if typedChars is not None else 0


def _isHolding(source: object) -> bool:
	return getattr(source, "isHolding", True)


class Record:
	"""The desktops a proof of presence was had on, one line each: name, the day, and PROBED when it
	came from the light probe."""

	def __init__(self, path: str | None) -> None:
		self.path = path

	def read(self) -> dict[str, list[str]]:
		if self.path is None:
			return {}
		try:
			with open(self.path, encoding="utf-8") as file:
				lines = file.read().splitlines()
		except FileNotFoundError:
			return {}
		except (OSError, UnicodeDecodeError) as e:
			log.debugWarning(f"Key delivery: the record of proofs could not be read: {type(e).__name__}")
			return {}
		return {words[0]: words[1:] for words in (line.split() for line in lines) if words}

	def has(self, name: str | None, probed: bool = False) -> bool:
		fields = self.read().get(name) if name else None
		return fields is not None and (not probed or PROBED in fields[1:])

	def write(self, name: str, probed: bool) -> None:
		if self.path is None:
			return
		entries = self.read()
		old = entries.get(name)
		marks = [PROBED] if probed or (old is not None and PROBED in old[1:]) else []
		entries[name] = [time.strftime("%Y-%m-%d"), *marks]
		temporary = f"{self.path}.new"
		try:
			fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_CLOEXEC, 0o600)
			with open(fd, "w", encoding="utf-8") as file:
				# A file left behind by an earlier copy keeps its own mode through O_CREAT.
				os.fchmod(fd, 0o600)
				file.writelines(" ".join([key, *fields]) + "\n" for key, fields in sorted(entries.items()))
			os.replace(temporary, self.path)
		except OSError as e:
			log.debugWarning(f"Key delivery: the record of proofs could not be written: {type(e).__name__}")
			try:
				os.unlink(temporary)
			except OSError:
				pass


@dataclass
class _LockPress:
	light: int
	at: float
	#: Another press of the same light came within its wait: the two may cancel, so it proves nothing.
	followed: bool = False


@dataclass(frozen=True)
class Listing:
	"""What sway's listing says of the virtual keyboard, as swayLayout.VirtualKeyboard has it."""

	listed: bool
	eventsEnabled: bool | None
	#: Whether sway lists any other keyboard of libinput's, where it was asked for all of them.
	othersListed: bool = True


def _listingOf(entry: dict | None) -> Listing:
	if entry is None:
		return Listing(False, None)
	libinput = entry.get("libinput")
	mode = libinput.get("send_events") if isinstance(libinput, dict) else None
	return Listing(True, {"enabled": True, "disabled": False}.get(mode))


class SwayListing:
	"""sway's word on the virtual keyboard: asked when the watch wants it, and followed through sway's
	input events on a connection and a thread of its own."""

	def __init__(self, path: str, identifier: str) -> None:
		self._path = path
		self._identifier = identifier
		self._connection = None
		self._thread: threading.Thread | None = None
		self._stopping = False

	def ask(self) -> Listing | None:
		"""What sway says now; None when it cannot be asked."""
		from . import swayLayout

		try:
			connection = swayLayout._Connection(self._path, swayLayout.STATE_TIMEOUT)
			try:
				inputs = connection.ask(swayLayout.GET_INPUTS)
			finally:
				connection.close()
		except (OSError, EOFError, ValueError) as e:
			log.debug(f"Key delivery: sway did not list its inputs: {type(e).__name__}")
			return None
		if not isinstance(inputs, list):
			return None
		keyboards = [e for e in inputs if isinstance(e, dict) and e.get("type") == "keyboard"]
		entry = next((e for e in keyboards if e.get("identifier") == self._identifier), None)
		if entry is not None:
			return _listingOf(entry)
		# Only libinput's devices go as sway's session does, not a program's virtual keyboard.
		others = any(isinstance(e.get("libinput"), dict) for e in keyboards)
		return Listing(False, None, othersListed=others)

	def follow(self, onChange: Callable[[Listing], None]) -> None:
		"""Call onChange with each change sway reports of the virtual keyboard, from a thread of this
		listing's own, until stop. Nothing follows when sway cannot be reached, and the watch asks only."""
		from . import swayLayout

		try:
			connection = swayLayout._Connection(self._path, swayLayout.START_TIMEOUT)
			try:
				reply = connection.ask(swayLayout.SUBSCRIBE, json.dumps(["input"]))
				if not (isinstance(reply, dict) and reply.get("success")):
					raise ValueError("sway refused the subscription to input events")
				connection.settimeout(None)
			except BaseException:
				connection.close()
				raise
		except (OSError, EOFError, ValueError) as e:
			log.debugWarning(f"Key delivery: sway's input events cannot be followed: {type(e).__name__}")
			return
		self._connection = connection
		self._thread = threading.Thread(
			target=self._run, args=(connection, onChange), name="lindaDeliveryListing", daemon=True
		)
		self._thread.start()

	def _run(self, connection, onChange: Callable[[Listing], None]) -> None:
		from . import swayLayout

		try:
			while not self._stopping:
				messageType, payload = connection.read()
				if messageType != swayLayout.EVENT_INPUT or not isinstance(payload, dict):
					continue
				device = payload.get("input")
				if not isinstance(device, dict) or device.get("identifier") != self._identifier:
					continue
				change = payload.get("change")
				if change == "removed":
					onChange(Listing(False, None))
				elif change in ("added", "libinput_config"):
					onChange(_listingOf(device))
		except (OSError, EOFError, ValueError, TypeError):
			if not self._stopping:
				log.debugWarning("Key delivery: sway's IPC connection ended", exc_info=True)

	def stop(self) -> None:
		self._stopping = True
		connection = self._connection
		if connection is None:
			return
		connection.shutdown()
		thread = self._thread
		if thread is not None:
			thread.join(2)
		connection.close()


class DeliveryWatch:
	"""Says, for each time the helper holds the keyboard, whether the keys NVDA passes on reach the
	desktop, and calls onAbsent once when they are proved not to.

	held, released and the notes may be called from any thread, the notes from the thread that answers
	keys: each only records and wakes the watch's own thread, which does the rest.
	"""

	def __init__(
		self,
		compositor: str | None,
		onAbsent: Callable[[object, str], None],
		*,
		record: Record | None = None,
		listing: SwayListing | None = None,
		matches: Callable[[], int] | None = None,
		layoutSource: Callable[[], str | None] | None = None,
	) -> None:
		"""
		:param compositor: The desktop's name a proof is remembered by; None remembers nothing.
		:param onAbsent: Called on the watch's thread with the kernel source held and why, the moment the
			keys it passes on are proved to go nowhere; it must only record and return.
		:param listing: sway's listing of the virtual keyboard, where there is one to ask.
		:param matches: The count of typed characters matched so far, which only grows; layoutMatches by
			default.
		:param layoutSource: Where NVDA's layout comes from now, as LayoutSnapshot.source; without it the
			lock echo proves nothing.
		"""
		self._compositor = compositor
		self._onAbsent = onAbsent
		self._record = record or Record(None)
		self._listing = listing
		self._matches = matches or (lambda: layoutMatches())
		self._layoutSource = layoutSource or (lambda: None)
		self._events: collections.deque[tuple] = collections.deque()
		self._wake = threading.Event()
		self._stopping = False
		self._thread: threading.Thread | None = None
		#: The rest is the watch thread's alone.
		self.state = UNKNOWN
		#: The kernel source the helper holds the keyboard for, and since when, while it does.
		self._source: object | None = None
		self._heldAt = 0.0
		#: When sway is to be asked next, if it is; and whether it has been in this hold, before which
		#: what it reports is not taken.
		self._askAt: float | None = None
		self._listingAsked = False
		#: sway's last word on the virtual keyboard during this hold.
		self._listed: Listing | None = None
		self._matchesAtHold = 0
		#: The helper's last LOCKS, which a change of is an answer; None until it has said.
		self._lastLocks: int | None = None
		#: The lights this desktop has answered a press of with LOCKS in this run.
		self._answered = 0
		self._matchesAtAnswer = 0
		self._pending: list[_LockPress] = []
		self._unanswered = 0
		self._virtual: int | None = None
		self._remembered = False
		#: Whether this hold has logged that the echo proved nothing, and that sway listed no keyboard.
		self._echoDeclined = False
		self._swayEmptySaid = False

	def start(self) -> None:
		if self._listing is not None:
			self._listing.follow(lambda listed: self._post("listed", listed, time.monotonic()))
		self._thread = threading.Thread(target=self._run, name="lindaDeliveryWatch", daemon=True)
		self._thread.start()

	def stop(self) -> None:
		self._stopping = True
		self._wake.set()
		if self._listing is not None:
			self._listing.stop()
		if self._thread is not None:
			self._thread.join(2)
			self._thread = None

	def remembered(self) -> bool:
		"""Whether a proof of presence was had on this desktop before, so that the rescue hint need not
		be said."""
		return self._record.has(self._compositor)

	# What the route and the kernel source say, from their threads.

	def _post(self, *event: object) -> None:
		self._events.append(event)
		self._wake.set()

	def held(self, source: object) -> None:
		"""The helper holds the keyboard for source, again or for the first time."""
		self._post("held", source, time.monotonic())

	def released(self, source: object) -> None:
		"""The helper no longer holds the keyboard for source, or source has gone."""
		self._post("released", source)

	def passedLock(self, light: int, now: float) -> None:
		"""environment.onPassedLock: a lock key passed on at now, by the light the layout has it change."""
		self._post("lock", light, now)

	def locks(self, locks: int, now: float) -> None:
		"""environment.onLocks: the helper said at now which lights are on."""
		self._post("locks", locks, now)

	def virtual(self, status: int) -> None:
		"""environment.onVirtual: the helper's probe of the virtual keyboard's lights, one of keydClient's
		VIRTUAL_ statuses."""
		self._post("virtual", status)

	# The watch's own thread.

	def _run(self) -> None:
		while not self._stopping:
			self._wake.wait(self._wait(time.monotonic()))
			self._wake.clear()
			if self._stopping:
				return
			try:
				self._turn()
			except Exception:
				log.exception("Key delivery: watching whether keys reach the desktop failed")

	def _wait(self, now: float) -> float | None:
		deadlines = [press.at + ECHO_WAIT for press in self._pending]
		# While the helper does not hold, nothing is asked, and its next hold wakes the watch.
		if self._source is not None and _isHolding(self._source):
			if self._askAt is not None:
				deadlines.append(self._askAt)
			if self.state != PRESENT:
				deadlines.append(now + MATCHES_INTERVAL)
		return max(0.0, min(deadlines) - now) if deadlines else None

	def _turn(self) -> None:
		while self._events:
			kind, *args = self._events.popleft()
			getattr(self, f"_on_{kind}")(*args)
		now = time.monotonic()
		self._expire(now)
		source = self._source
		if source is None or not _isHolding(source):
			return
		if self._listing is not None and self._askAt is not None and now >= self._askAt:
			self._askAt = None
			self._listingAsked = True
			listed = self._listing.ask()
			# The helper may have let go while sway was asked, and sway takes its keyboard away then.
			if listed is not None and self._source is source and _isHolding(source):
				self._takeListing(listed)
		if self._source is not None and self.state != PRESENT and self._matches() > self._matchesAtHold:
			self._present("a character typed matched NVDA's layout")
		if self._source is not None and self._virtual is not None:
			status, self._virtual = self._virtual, None
			self._takeVirtual(status)

	def _on_held(self, source: object, at: float) -> None:
		# A kernel source says it holds only as it turns from not holding, so the same source again is a
		# hold after a let-go, as for a console switch, through which sway took its devices away and gave
		# them back: everything is asked afresh.
		self._source, self._heldAt = source, at
		self._askAt = at + LISTING_WAIT if self._listing is not None else None
		self._listingAsked = False
		self._listed = None
		self._matchesAtHold = self._matches()
		self._pending.clear()
		self._unanswered = 0
		self._virtual = None
		self._echoDeclined = False
		self._swayEmptySaid = False
		self.state = UNKNOWN

	def _on_released(self, source: object) -> None:
		if source is not self._source:
			return
		self._source = None
		self._pending.clear()
		self._unanswered = 0
		self.state = UNKNOWN

	def _on_listed(self, listed: Listing, at: float) -> None:
		source = self._source
		# Before the listing is first asked, the helper's keyboard may be settling still; and while the
		# helper does not hold, sway's devices are no word on the keys passed on.
		if source is None or not self._listingAsked or not _isHolding(source):
			return
		if not listed.listed:
			# sway takes every device away as its session goes to the background, and the virtual keyboard
			# as the helper breaks it, either of which may come before the helper's word that it let go: so
			# sway is asked again once that word would have come.
			again = at + LISTING_WAIT
			self._askAt = again if self._askAt is None else min(self._askAt, again)
			return
		self._takeListing(listed)

	def _on_virtual(self, status: int) -> None:
		self._virtual = status

	def _on_lock(self, light: int, at: float) -> None:
		if self._source is None:
			return
		for press in self._pending:
			if press.light & light and at < press.at + ECHO_WAIT:
				press.followed = True
		self._pending.append(_LockPress(light, at))

	def _on_locks(self, locks: int, at: float) -> None:
		changed = 0 if self._lastLocks is None else locks ^ self._lastLocks
		self._lastLocks = locks
		for press in list(self._pending):
			if press.light & changed and press.at <= at <= press.at + ECHO_WAIT:
				changed &= ~press.light
				self._pending.remove(press)
				self._answered |= press.light
				self._unanswered = 0
				self._matchesAtAnswer = self._matches()

	def _expire(self, now: float) -> None:
		holding = self._source is not None and _isHolding(self._source)
		for press in [p for p in self._pending if now >= p.at + ECHO_WAIT]:
			self._pending.remove(press)
			# Not holding, the helper may have let go before its LOCKS could answer.
			if press.followed or not press.light & self._answered or not holding:
				continue
			self._unanswered += 1
			if self._unanswered < ECHO_RUN:
				continue
			if (layout := self._layoutSource()) not in ECHO_LAYOUTS:
				why = (
					f"NVDA's layout comes from {layout or 'nowhere known'}, which may not hold the desktop's "
					"own options"
				)
			elif self._matches() != self._matchesAtAnswer:
				why = "a character typed has matched since"
			elif self._listed is not None and self._listed.listed and self._listed.eventsEnabled:
				why = "sway says it takes linda-keyd's keyboard's events"
			else:
				self._absent("lock keys passed on changed no light, which the desktop has answered before")
				return
			# Once a hold, as a line for each press would say when a lock key was pressed.
			if not self._echoDeclined:
				self._echoDeclined = True
				log.debug(
					f"Key delivery: {ECHO_RUN} lock keys passed on running were not answered, which proves "
					f"nothing: {why}"
				)

	def _takeListing(self, listed: Listing) -> None:
		if not listed.listed and not listed.othersListed:
			# Back in front, sway lists none of libinput's keyboards until it has taken its devices up again,
			# seconds after the helper holds; until it lists one, it is asked again.
			if not self._swayEmptySaid:
				self._swayEmptySaid = True
				log.debug("Key delivery: sway lists no keyboard of libinput's yet, and is asked again")
			self._askAt = time.monotonic() + LISTING_WAIT
			return
		self._listed = listed
		if not listed.listed:
			self._absent("sway does not list linda-keyd's keyboard")
		elif listed.eventsEnabled is False:
			self._absent("sway takes no events from linda-keyd's keyboard")
		elif listed.eventsEnabled:
			self._present("sway lists linda-keyd's keyboard with its events on")

	def _takeVirtual(self, status: int) -> None:
		if status == keydClient.VIRTUAL_SEEN:
			self._present("the desktop wrote the lights of linda-keyd's keyboard", probed=True)
		elif status == keydClient.VIRTUAL_UNSEEN and self._record.has(self._compositor, probed=True):
			self._absent("the desktop did not write the lights of linda-keyd's keyboard, as it did before")

	def _present(self, why: str, probed: bool = False) -> None:
		if self._compositor is not None and (not self._remembered or probed):
			self._remembered = True
			self._record.write(self._compositor, probed)
		if self.state != PRESENT:
			self.state = PRESENT
			log.info(f"Keys NVDA passes on reach the desktop: {why}")

	def _absent(self, why: str) -> None:
		source, self._source = self._source, None
		self._pending.clear()
		self._unanswered = 0
		self.state = ABSENT
		log.info(f"Keys NVDA passes on do not reach the desktop: {why}")
		try:
			self._onAbsent(source, why)
		except Exception:
			log.exception("Key delivery: reporting that keys do not reach the desktop failed")
