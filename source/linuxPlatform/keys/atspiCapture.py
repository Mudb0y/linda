"""Where a document's keys go, with no X server: x11.CaptureState rebuilt from AT-SPI alone.

The kernel source decides each key typed in a document by the capture state, which on the X route the
X source keeps from its grabs and the X server's focus. Without X, the foreground comes from the
toolkits' own window events, heard here on a connection to the accessibility bus of this feed's own
and on a thread of its own, so that neither NVDA's main thread being busy nor its listener's choices
hold it up. The feed registers the events it needs with the registry itself: Electron sends nothing,
and GTK 4 little, for a reader that only listens.

Toolkits do not always say that a window went. A process that ends, by a crash, Ctrl+Q or its last
window closing, sends no deactivation, so the bus name of the foreground's application going away
counts as one, as does the foreground window going defunct or no longer showing, or saying it is
destroyed, which is all Firefox says as its last window closes, most of a second before its process
ends. GTK 4 at times sends nothing at all as a new window becomes active, so a top-level window that
appears and is heard nothing more of is asked for its state a few times after it shows, as is each
window of an application that joins the desktop: GTK 4 joins only as its first window shows, and then
at times has said nothing of that window that anyone could hear.

The window keys are captured on is an anchor, by the X source's rules, the foreground standing in for
the X focus window: it is the foreground as NVDA last said browse or focus while there was no anchor or
the anchor did not have the keyboard, it stays while the keyboard is away, and moves only when NVDA
names another process or when the foreground is another window of the anchor's process. Unlike the X
server's focus, the feed's foreground can be behind NVDA's own focus, so a foreground of another
process than the one NVDA names is not anchored: the first window of that process to be the foreground
is.

A look, which the kernel source asks for before a key whose place depends on the focus, ends once the
feed knows the foreground as of the key: after a chord that may move the focus, at the first window
events and the quiet after them, or at FOCUS_MOVE_GRACE, known if a window event came after the chord
began and unknown if none did; otherwise after one round trip on the feed's own connection, which the
bus answers only after every event it routed here before it. Notes of a chord within FOCUS_MOVE_GRACE
of each other are one chord: sway, and Mutter with no shell, switch as Tab goes down, so their window
events come before the note of Alt's release, and none after it.
"""

import collections
import os
import threading
import time
from collections.abc import Callable

from logHandler import log

from . import CaptureMode
from .x11 import DOCUMENT_GRACE, NO_CAPTURE, CaptureState

#: Seconds after a chord that may move the focus, as Alt+Tab, in which a key waits for a window event
#: before it is passed as typed outside any document. With the kernel's CAPTURE_TIMEOUT after it, it
#: must stay within DOCUMENT_WAIT.
FOCUS_MOVE_GRACE = 0.3
#: Seconds a look with the focus settled waits for its round trip before it ends unknown.
LOOK_ROUND_TRIP = 0.05
#: Seconds without a window event after which a switch between windows has settled: toolkits bounce
#: activation between the window left and the one entered for a few milliseconds.
WINDOW_QUIET = 0.03
#: Seconds after a top-level window shows at which it is asked whether it is active, while it has sent
#: no window event of its own and no other window has: GTK 4 sends none for some new windows, measured
#: becoming active 20 ms and 4.1 s after they showed. The same as the focus handler's
#: SHOWN_PROBE_SECONDS, so that NVDA's focus and the keys' place are found by the same reads. It is
#: not imported from there, which would bring the handler's libatspi client in with a feed that keeps
#: to a connection of its own; test_atspiCapture.py holds the two equal.
NEW_WINDOW_READS = (0.1, 0.5, 1.0, 2.0, 3.0, 4.5, 6.0)
#: Milliseconds any call on the bus may take; the feed's looks never wait for one beyond their own end.
CALL_TIMEOUT_MS = 1000
#: Seconds start waits for the connection, and stop for the thread.
START_TIMEOUT = 2.0
STOP_TIMEOUT = 2.0
#: Milliseconds between checks of the open looks while there are any.
LOOK_POLL_MS = 5
#: Objects whose being a top-level window or not is remembered at most, and new windows asked about at
#: once at most; a flood of objects showing beyond that is not followed.
MAX_KNOWN = 4096
MAX_ASKING = 32
#: Windows remembered as having said they are active, for the one before to be the foreground again.
MAX_ACTIVE = 16

ROOT_PATH = "/org/a11y/atspi/accessible/root"
REGISTRY = "org.a11y.atspi.Registry"
REGISTRY_PATH = "/org/a11y/atspi/registry"
ACCESSIBLE = "org.a11y.atspi.Accessible"
WINDOW_EVENTS = "org.a11y.atspi.Event.Window"
OBJECT_EVENTS = "org.a11y.atspi.Event.Object"
BUS = "org.freedesktop.DBus"
BUS_PATH = "/org/freedesktop/DBus"
#: The accessibility bus launcher, on the session bus.
LAUNCHER = "org.a11y.Bus"
LAUNCHER_PATH = "/org/a11y/bus"
REGISTERED_EVENTS = ("window:activate", "window:deactivate", "window:destroy", "object:state-changed")
#: The window events listened for.
WINDOW_MEMBERS = ("Activate", "Deactivate", "Destroy")
#: The state changes listened for, each with a match rule of its own.
WATCHED_STATES = ("active", "showing", "visible", "defunct")
#: AtspiStateType's numbers, bits of GetState's answer.
STATE_ACTIVE = 1
STATE_DEFUNCT = 6
STATE_SHOWING = 25
STATE_VISIBLE = 30

#: An object on the bus: the unique name of its application's connection, and its path.
Key = tuple[str, str]
#: done(number, known), from the feed's thread, as a look ends.
LookDone = Callable[[int, bool], None]


def _hasState(states: list[int], state: int) -> bool:
	word, bit = divmod(state, 32)
	return word < len(states) and bool(states[word] & (1 << bit))


class _Look:
	__slots__ = ("chord", "deadline", "done", "number", "roundTrip", "since")

	def __init__(
		self,
		number: int,
		deadline: float,
		done: LookDone,
		since: float | None,
		chord: float | None = None,
	) -> None:
		self.number = number
		self.deadline = deadline
		self.done = done
		#: When the chord that may have moved the focus came, for a look asked while it had not settled;
		#: None for one that waits for a round trip.
		self.since = since
		#: When that chord's first note came, which is since itself for a chord of one note.
		self.chord = since if chord is None else chord
		self.roundTrip = False


class AtspiCaptureFeed:
	"""The capture state for the kernel source, kept from AT-SPI.

	captureState, captureNow, canLook, noteFocusMayMove and requestLook are what the kernel source's
	answering thread calls: reads of state replaced whole, an assignment, or an append and a pipe
	write. Everything else happens on the feed's own thread, or under its lock for setCapture.
	"""

	def __init__(self, holdEvents: float = 0.0) -> None:
		"""
		:param holdEvents: Seconds to hold back every window event before it is followed, for tests that
			make the focus's news come late.
		"""
		self._holdEvents = holdEvents
		self._lock = threading.Lock()
		self._ownProcess = os.getpid()
		self._mode: CaptureMode = "off"
		self._dormant: CaptureMode | None = None
		self._documentUntil: float | None = None
		#: The process NVDA last said the document is of.
		self._processID: int | None = None
		self._anchor: Key | None = None
		self._anchorProcess: int | None = None
		#: Whether the anchor is the foreground.
		self._focused = False
		self._foreground: Key | None = None
		#: The windows whose last word was that they are active, the latest last.
		self._active: list[Key] = []
		self._processes: dict[str, int] = {}
		self._askingProcess: set[str] = set()
		#: Whether each object is a top-level window, as far as known.
		self._topLevels: dict[Key, bool] = {}
		#: Top-level windows heard from by a window event of their own.
		self._heardFrom: set[Key] = set()
		#: New top-level windows being asked about.
		self._asking: set[Key] = set()
		#: Objects whose parent is being asked, each with what is to be done once it is known to be a
		#: top-level window and whether that holds the looks.
		self._askingParent: dict[Key, list[tuple[Callable[[], None], bool]]] = {}
		self._windowNumbers: dict[Key, int] = {}
		self._nextWindowNumber = 1
		self._lastWindowEventAt = float("-inf")
		#: The first and the latest note of the chord that may move the focus, replaced whole, as the
		#: answering thread writes it and reads it: notes within FOCUS_MOVE_GRACE of each other are one
		#: chord, as Alt+Tab's Tab and Alt's release are.
		self._moves: tuple[float, float] = (float("-inf"), float("-inf"))
		#: Whether NVDA has named a document whose window is not yet the foreground, the first such window
		#: to be the foreground being its anchor.
		self._awaiting = False
		#: Reads in flight whose answer may move the foreground now: no look ends known before them. A
		#: read about a window that has only shown is not one, as another program starting up or stuck
		#: would otherwise leave every look unknown for as long as it takes to answer.
		self._reading = 0
		self._looks: collections.deque[_Look] = collections.deque()
		self._openLooks: list[_Look] = []
		self._state: CaptureState = NO_CAPTURE
		self._thread: threading.Thread | None = None
		self._context = None
		self._loop = None
		self._connection = None
		self._closedHandler: int | None = None
		self._subscriptions: list[int] = []
		self._lookTimer = None
		#: Made at the first start and never closed: the answering thread may be about to write to it as
		#: the feed stops, and a closed descriptor's number can be another file's by then.
		self._wakeRead: int | None = None
		self._wakeWrite: int | None = None
		self._ready = threading.Event()
		self._listening = False
		#: Whether the feed's thread has stopped listening and is ending: closing its connection and
		#: ending its looks, which a new thread must not do beside it.
		self._ending = False
		#: Held by start and stop, which the route's thread and the environment's keeper both call.
		self._starting = threading.Lock()

	# What the kernel source reads, from any thread.

	@property
	def captureState(self) -> CaptureState:
		return self._state

	def captureNow(self, now: float) -> CaptureState:
		"""The capture state as of now: a document whose window the keyboard came back to without NVDA's
		focus following within DOCUMENT_GRACE has its keys let go, as the X source lets them go."""
		state = self._state
		if state.documentUntil is not None and now >= state.documentUntil:
			return CaptureState(0, None, False, "off", None, None, state.processID)
		return state

	@property
	def canLook(self) -> bool:
		return self._listening

	def settled(self, now: float) -> bool:
		"""Whether no chord that may move the focus waits for its window event."""
		latest = self._moves[1]
		return not (now < latest + FOCUS_MOVE_GRACE and self._lastWindowEventAt < latest)

	def noteFocusMayMove(self, now: float) -> None:
		"""A chord that may move the focus, as Alt+Tab, was passed to the compositor at now."""
		first, latest = self._moves
		self._moves = (first if now - latest <= FOCUS_MOVE_GRACE else now, now)

	def requestLook(self, number: int, now: float, done: LookDone) -> float:
		"""Find out where the focus is as of now, and call done(number, known) from the feed's thread once
		it is known, or with known False once it cannot be.

		:return: When the look ends at the latest.
		"""
		first, latest = self._moves
		since = None if self.settled(now) else latest
		deadline = now + LOOK_ROUND_TRIP if since is None else since + FOCUS_MOVE_GRACE
		self._looks.append(_Look(number, deadline, done, since, None if since is None else first))
		wake = self._wakeWrite
		if wake is not None:
			try:
				os.write(wake, b"\0")
			except (BlockingIOError, OSError):
				pass
		return deadline

	@property
	def foreground(self) -> tuple[str, str, int | None] | None:
		"""The foreground window's bus name, path and process, when there is one."""
		foreground = self._foreground
		if foreground is None:
			return None
		return (*foreground, self._processes.get(foreground[0]))

	# What NVDA tells it.

	def setCapture(self, mode: CaptureMode, processID: int | None = None) -> None:
		"""NVDA's focus is in a document in browse or focus mode, of processID, or is in none: asked again
		and again while it is in one, as the keyboard may have gone to another window since."""
		with self._lock:
			self._setCapture(mode, processID)

	def _setCapture(self, mode: CaptureMode, processID: int | None) -> None:
		self._documentUntil = None
		if mode != "off":
			# Off says nothing of the document whose keys stay captured while the keyboard is away.
			self._processID = processID
		anchor = self._anchor
		if anchor is not None and not self._focused and (mode == "off" or self._modeIsStillAway(processID)):
			self._mode = mode
		elif (mode == "off") != (self._mode == "off") or (
			mode != "off" and (anchor is None or not self._focused)
		):
			self._applyCapture(mode)
		else:
			self._mode = mode
		self._publish()

	def _modeIsStillAway(self, processID: int | None) -> bool:
		"""Whether NVDA's mode may still be that of the document in the anchor, which the keyboard has
		left for a window of another application."""
		away = self._anchorProcess
		if processID and away and processID != away:
			# NVDA's focus has reached another application's document; its window is where the keys go.
			return False
		foreground = self._foreground
		return (self._processes.get(foreground[0]) if foreground is not None else None) != away

	def _applyCapture(self, mode: CaptureMode) -> None:
		self._mode = mode
		self._dropAnchor()
		foreground = self._foreground
		if mode == "off":
			return
		# NVDA's own focus can follow a switch sooner than the feed hears of it, and then names the
		# document of a window that is not yet the foreground, whose keys do not go to the window the
		# feed still has: the anchor waits for a window of the process NVDA names.
		self._awaiting = bool(self._processID)
		if foreground is None:
			return
		if foreground[0] not in self._processes:
			# Asked here from NVDA's thread, and answered on the feed's, where the bus calls back.
			busName = foreground[0]
			self._onFeedThread(lambda: self._askProcess(busName))
			return
		if self._awaiting:
			self._takeAwaited()
			return
		process = self._processes[foreground[0]]
		if process == self._ownProcess:
			# NVDA's own window has the keyboard only until NVDA's focus follows.
			return
		self._anchorForeground(process)

	def _takeAwaited(self) -> None:
		"""Anchor the foreground once it is a window of the process whose document NVDA named while no
		window of it had the keyboard. Under the lock."""
		foreground = self._foreground
		if not self._awaiting or foreground is None:
			return
		process = self._processes.get(foreground[0])
		if process is None or process != self._processID or process == self._ownProcess:
			return
		self._awaiting = False
		self._anchorForeground(process)

	def _anchorForeground(self, process: int) -> None:
		self._anchor = self._foreground
		self._anchorProcess = process
		self._focused = True
		self._dormant = None

	def _dropAnchor(self) -> None:
		self._anchor = None
		self._anchorProcess = None
		self._focused = False
		self._dormant = None
		self._awaiting = False

	def _focusCapture(self, focused: bool, now: float) -> None:
		"""Follow the keyboard leaving or reaching the anchor, as the X source follows it leaving or
		reaching the window its grabs are on: the keys stay captured there while it is away, so that a
		key typed as it comes back, before NVDA's focus follows, is held for NVDA's focus."""
		self._focused = focused
		if not focused:
			if self._mode != "off":
				self._dormant = self._mode
			return
		if self._mode == "off" and self._dormant is not None:
			self._mode = self._dormant
			self._documentUntil = now + DOCUMENT_GRACE
			self._later(DOCUMENT_GRACE, self._expireDocument)
		self._dormant = None

	def _expireDocument(self, now: float | None = None) -> None:
		with self._lock:
			if self._documentUntil is None or (self._now() if now is None else now) < self._documentUntil:
				return
			self._documentUntil = None
			self._dropAnchor()
			self._mode = "off"
			self._publish()

	def _publish(self) -> None:
		anchor = self._anchor
		number = 0
		if anchor is not None:
			number = self._windowNumbers.get(anchor, 0)
			if not number:
				if len(self._windowNumbers) >= MAX_KNOWN:
					self._windowNumbers.clear()
				number = self._windowNumbers[anchor] = self._nextWindowNumber
				self._nextWindowNumber += 1
		state = CaptureState(
			number,
			self._anchorProcess if anchor is not None else None,
			self._focused,
			self._mode,
			self._dormant,
			self._documentUntil,
			self._processID,
		)
		if state != self._state:
			self._state = state

	# The foreground, on the feed's thread.

	def _followFocus(self, now: float) -> None:
		self._takeAwaited()
		focused = self._anchor is not None and self._foreground == self._anchor
		if focused != self._focused:
			self._focusCapture(focused, now)
		self._publish()

	def _activated(self, key: Key, now: float) -> None:
		with self._lock:
			self._lastWindowEventAt = now
			if key in self._active:
				self._active.remove(key)
			elif len(self._active) >= MAX_ACTIVE:
				del self._active[0]
			self._active.append(key)
			self._foreground = key
			self._followFocus(now)
		if key[0] not in self._processes:
			self._askProcess(key[0])

	def _deactivated(self, key: Key, now: float) -> None:
		with self._lock:
			self._lastWindowEventAt = now
			candidate = self._leave(lambda window: window == key)
			self._followFocus(now)
		self._confirm(candidate)

	def _leave(self, gone: Callable[[Key], bool]) -> Key | None:
		"""Forget the windows gone says are no longer active. When the foreground is among them, the
		window that said it was active before it, if any, is the foreground again: Qt says its window is
		active and then not, a tenth of a second after the window that took the keyboard from it said
		so. Under the lock.

		:return: That window, which is to be asked whether it is still active.
		"""
		self._active = [window for window in self._active if not gone(window)]
		if self._foreground is None or not gone(self._foreground):
			return None
		self._foreground = self._active[-1] if self._active else None
		return self._foreground

	def _confirm(self, candidate: Key | None) -> None:
		if candidate is not None:
			self._readState(candidate, self._checkStillActive)

	def _checkStillActive(self, key: Key, states: list[int] | None) -> None:
		if states is None or not _hasState(states, STATE_ACTIVE):
			self._deactivated(key, self._now())

	def _applicationGone(self, busName: str, now: float) -> None:
		with self._lock:
			self._processes.pop(busName, None)
			for known in [key for key in self._topLevels if key[0] == busName]:
				del self._topLevels[known]
			self._heardFrom = {key for key in self._heardFrom if key[0] != busName}
			wasForeground = self._foreground is not None and self._foreground[0] == busName
			candidate = self._leave(lambda window: window[0] == busName)
			if wasForeground:
				self._lastWindowEventAt = now
			if self._anchor is not None and self._anchor[0] == busName:
				# Its windows went with it, and the keyboard cannot come back to one.
				self._windowNumbers.pop(self._anchor, None)
				self._dropAnchor()
			self._followFocus(now)
		self._confirm(candidate)

	def _windowGone(self, key: Key, now: float) -> None:
		with self._lock:
			self._topLevels.pop(key, None)
			self._heardFrom.discard(key)
			wasForeground = self._foreground == key
			candidate = self._leave(lambda window: window == key)
			if wasForeground:
				self._lastWindowEventAt = now
			if self._anchor == key:
				self._windowNumbers.pop(key, None)
				self._dropAnchor()
			self._followFocus(now)
		self._confirm(candidate)

	def _processKnown(self, busName: str, process: int | None) -> None:
		with self._lock:
			self._askingProcess.discard(busName)
			if process is not None:
				if len(self._processes) >= MAX_KNOWN:
					self._processes.clear()
				self._processes[busName] = process
				if self._foreground is not None and self._foreground[0] == busName:
					self._takeAwaited()
					self._publish()

	def _askProcess(self, busName: str) -> None:
		if busName in self._askingProcess:
			return
		self._askingProcess.add(busName)
		self._request("process", busName, lambda process: self._processKnown(busName, process))

	def _onWindowEvent(self, key: Key, activated: bool, now: float) -> None:
		self._noteTopLevel(key, True)
		if len(self._heardFrom) >= MAX_KNOWN:
			self._heardFrom.clear()
		self._heardFrom.add(key)
		if activated:
			self._activated(key, now)
		else:
			self._deactivated(key, now)

	def _onStateChanged(self, key: Key, state: str, value: int, now: float) -> None:
		topLevel = self._topLevels.get(key)
		if state == "active":
			if topLevel is False:
				# Caja's tree view marks its current cell active.
				return
			self._lastWindowEventAt = now
			if topLevel:
				self._onWindowEvent(key, bool(value), now)
			else:
				self._findTopLevel(
					key, lambda: self._onWindowEvent(key, bool(value), self._now()), holdsLooks=True
				)
			return
		if state == "defunct":
			if value:
				self._windowGone(key, now)
			return
		# Showing or visible.
		if key == self._foreground:
			if not value:
				# GTK 4 says so of a window that has just shown, so its state is asked rather than taken.
				self._readState(key, self._checkStillShown)
			return
		if topLevel is False or key in self._heardFrom:
			return
		if topLevel:
			self._watchNewWindow(key, now)
		else:
			self._findTopLevel(key, lambda: self._watchNewWindow(key, now), holdsLooks=False)

	def _checkStillShown(self, key: Key, states: list[int] | None) -> None:
		if states is not None and not (_hasState(states, STATE_SHOWING) and _hasState(states, STATE_VISIBLE)):
			self._deactivated(key, self._now())

	def _noteTopLevel(self, key: Key, topLevel: bool) -> None:
		if len(self._topLevels) >= MAX_KNOWN:
			self._topLevels.clear()
		self._topLevels[key] = topLevel

	def _findTopLevel(self, key: Key, then: Callable[[], None], holdsLooks: bool) -> None:
		"""Find out whether key is a top-level window, the child of its application's root, and call then
		if it is; while holdsLooks, no look ends known before the answer. GTK 4 says a window is showing
		and visible at once, which is asked about once."""
		waiting = self._askingParent.get(key)
		first = waiting is None
		if first:
			if len(self._askingParent) >= MAX_ASKING:
				return
			waiting = self._askingParent[key] = []
		if holdsLooks and not any(holds for _then, holds in waiting):
			self._reading += 1
		waiting.append((then, holdsLooks))
		if first:
			self._request("parent", key, lambda parent: self._parentKnown(key, parent))

	def _parentKnown(self, key: Key, parent: str | None) -> None:
		waiting = self._askingParent.pop(key, [])
		if any(holds for _then, holds in waiting):
			self._reading -= 1
		if parent is None:
			return
		self._noteTopLevel(key, parent == ROOT_PATH)
		if parent == ROOT_PATH:
			for then, _holds in waiting:
				then()

	def _watchNewWindow(self, key: Key, shown: float) -> None:
		"""Ask a top-level window that has just shown whether it is active, a few times, while neither it
		nor any other window has sent a window event since."""
		if key in self._asking or len(self._asking) >= MAX_ASKING:
			return
		self._asking.add(key)
		reads = list(NEW_WINDOW_READS)

		def ask() -> None:
			if not reads or key in self._heardFrom or self._lastWindowEventAt > shown:
				self._asking.discard(key)
				return
			self._later(max(0.0, shown + reads.pop(0) - self._now()), read)

		def read() -> None:
			if key in self._heardFrom or self._lastWindowEventAt > shown or self._foreground == key:
				self._asking.discard(key)
				return
			self._readState(key, answered, holdsLooks=False)

		def answered(_key: Key, states: list[int] | None) -> None:
			if (
				states is not None
				and _hasState(states, STATE_ACTIVE)
				and key not in self._heardFrom
				and self._lastWindowEventAt <= shown
				and self._foreground != key
			):
				self._asking.discard(key)
				log.debug("AT-SPI capture feed: a new window became active without saying so")
				self._activated(key, self._now())
				return
			ask()

		ask()

	def _readState(
		self, key: Key, then: Callable[[Key, list[int] | None], None], holdsLooks: bool = True
	) -> None:
		self._reading += holdsLooks

		def answered(states: list[int] | None) -> None:
			self._reading -= holdsLooks
			then(key, states)

		self._request("state", key, answered)

	def _scanForForeground(self) -> None:
		"""Find the active top-level window as the feed starts, before any window event has said."""
		started = self._now()

		def found(key: Key, states: list[int] | None) -> None:
			if (
				states is not None
				and _hasState(states, STATE_ACTIVE)
				and self._foreground is None
				and self._lastWindowEventAt < started
			):
				self._activated(key, self._now())

		def windows(app: Key, children: list[Key] | None) -> None:
			self._reading -= 1
			for window in children or ():
				self._noteTopLevel(window, True)
				self._readState(window, found)

		def applications(children: list[Key] | None) -> None:
			self._reading -= 1
			for app in children or ():
				if app[0] == REGISTRY:
					continue
				self._reading += 1
				self._request("children", app, lambda kids, app=app: windows(app, kids))

		self._reading += 1
		self._request("children", (REGISTRY, ROOT_PATH), applications)

	def _applicationAdded(self, app: Key, now: float) -> None:
		"""An application joined the desktop: each of its windows is asked about as one that has just
		shown, as GTK 4's first window may already be active without having said so."""
		if app[0] == REGISTRY or app[1] != ROOT_PATH:
			return

		def windows(children: list[Key] | None) -> None:
			for window in children or ():
				self._noteTopLevel(window, True)
				self._watchNewWindow(window, now)

		self._request("children", app, windows)

	# Looks, on the feed's thread.

	def _takeLooks(self, now: float | None = None) -> None:
		while self._looks:
			look = self._looks.popleft()
			self._openLooks.append(look)
			if look.since is None:
				self._request(
					"roundTrip", None, lambda answered, look=look: self._roundTripDone(look, answered)
				)
		self._serviceLooks(self._now() if now is None else now)

	def _roundTripDone(self, look: _Look, answered: object) -> None:
		look.roundTrip = answered is not None
		self._serviceLooks(self._now())

	def _serviceLooks(self, now: float) -> None:
		if not self._openLooks:
			return
		quiet = now - self._lastWindowEventAt >= WINDOW_QUIET and not self._reading
		ended: list[tuple[_Look, bool]] = []
		for look in self._openLooks:
			known = look.roundTrip if look.since is None else self._lastWindowEventAt >= look.since
			if known and quiet:
				ended.append((look, True))
			elif now >= look.deadline:
				# A compositor that switches as Tab goes down has sent its window events before Alt's
				# release: the move was heard, and nothing more has come for the whole grace.
				heard = look.since is not None and self._lastWindowEventAt >= look.chord and quiet
				ended.append((look, heard))
		for look, known in ended:
			self._openLooks.remove(look)
			try:
				look.done(look.number, known)
			except Exception:
				log.exception("AT-SPI capture feed: ending a look failed")
		if self._openLooks and self._lookTimer is None and self._context is not None:
			self._lookTimer = self._every(LOOK_POLL_MS, self._pollLooks)

	def _pollLooks(self) -> bool:
		self._serviceLooks(self._now())
		if self._openLooks:
			return True
		self._lookTimer = None
		return False

	def _endLooks(self) -> None:
		while self._looks:
			self._openLooks.append(self._looks.popleft())
		looks, self._openLooks = self._openLooks, []
		for look in looks:
			try:
				look.done(look.number, False)
			except Exception:
				log.exception("AT-SPI capture feed: ending a look failed")

	# The thread and the bus.

	def start(self) -> bool:
		"""Connect to the accessibility bus and follow the foreground on a thread of the feed's own,
		waiting up to START_TIMEOUT for the connection. Without one, canLook stays False until start is
		called again, as it does once the connection closes.

		:return: Whether the feed's thread is listening or still connecting. When it is not, a warning
			has said why.
		"""
		with self._starting:
			thread = self._thread
			if thread is not None and thread.is_alive():
				if not self._ending:
					return True
				# canLook went False before the thread let go of its connection, so a start called at
				# once, as when the last found no bus, would otherwise find it alive and do nothing.
				thread.join(STOP_TIMEOUT)
				if thread.is_alive():
					log.warning(
						"AT-SPI capture feed: its connection before has not ended, so browse mode's keys are "
						"not captured"
					)
					return False
			from gi.repository import GLib

			self._forgetConnection()
			self._context = GLib.MainContext.new()
			self._loop = GLib.MainLoop.new(self._context, False)
			if self._wakeWrite is None:
				self._wakeRead, self._wakeWrite = os.pipe2(os.O_NONBLOCK | os.O_CLOEXEC)
			self._ready.clear()
			self._ending = False
			self._thread = threading.Thread(target=self._run, name="lindaAtspiCapture", daemon=True)
			self._thread.start()
			if not self._ready.wait(START_TIMEOUT):
				log.debugWarning("AT-SPI capture feed: still connecting to the accessibility bus")
			return not self._ending

	def _forgetConnection(self) -> None:
		"""Forget what was heard on the connection before: the answers to reads still out on it never
		come, so they would hold every look for good, and windows and programs may have come and gone
		while nothing listened. What NVDA last said stays, and NVDA says it again."""
		with self._lock:
			self._reading = 0
			self._askingProcess.clear()
			self._asking.clear()
			self._askingParent.clear()
			self._lookTimer = None
			self._foreground = None
			self._active = []
			self._processes.clear()
			self._topLevels.clear()
			self._heardFrom.clear()
			self._windowNumbers.clear()
			self._lastWindowEventAt = float("-inf")
			self._documentUntil = None
			self._dropAnchor()
			self._publish()

	def stop(self) -> None:
		with self._starting:
			thread = self._thread
			if thread is None:
				return
			from gi.repository import GLib

			# Should the join run out, the thread still quits once its loop gets to it, and a start
			# before then must wait for it rather than take it for one that listens.
			self._ending = True
			source = GLib.idle_source_new()
			source.set_callback(lambda *_args: self._loop.quit() or GLib.SOURCE_REMOVE)
			source.attach(self._context)
			thread.join(STOP_TIMEOUT)
			if thread.is_alive():
				log.debugWarning("AT-SPI capture feed: its thread did not end in time")
				return
			self._thread = None

	def _run(self) -> None:
		self._context.push_thread_default()
		try:
			if self._connect():
				self._listening = True
				self._ready.set()
				self._scanForForeground()
				self._loop.run()
		except Exception:
			log.exception("AT-SPI capture feed failed")
		finally:
			self._ending = True
			self._listening = False
			self._ready.set()
			self._endLooks()
			self._disconnect()
			self._context.pop_thread_default()

	def _connect(self) -> bool:
		from gi.repository import Gio, GLib

		try:
			session = Gio.bus_get_sync(Gio.BusType.SESSION, None)
			(address,) = session.call_sync(
				LAUNCHER,
				LAUNCHER_PATH,
				LAUNCHER,
				"GetAddress",
				None,
				GLib.VariantType("(s)"),
				Gio.DBusCallFlags.NONE,
				CALL_TIMEOUT_MS,
				None,
			).unpack()
			self._connection = Gio.DBusConnection.new_for_address_sync(
				address,
				Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
				| Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
				None,
				None,
			)
		except GLib.Error as e:
			log.warning(
				f"AT-SPI capture feed: no accessibility bus, so browse mode's keys are not captured: {e}"
			)
			return False
		connection = self._connection
		self._closedHandler = connection.connect("closed", self._onClosed)
		for member in WINDOW_MEMBERS:
			self._subscribe(connection, WINDOW_EVENTS, member, None, self._onWindowSignal)
		for state in WATCHED_STATES:
			self._subscribe(connection, OBJECT_EVENTS, "StateChanged", state, self._onStateSignal)
		self._subscribe(connection, BUS, "NameOwnerChanged", None, self._onNameOwnerChanged, sender=BUS)
		# The registry says so of every application that joins, whether anyone registered for it or not.
		self._subscribe(
			connection, OBJECT_EVENTS, "ChildrenChanged", "add", self._onChildrenSignal, path=ROOT_PATH
		)
		for event in REGISTERED_EVENTS:
			try:
				connection.call_sync(
					REGISTRY,
					REGISTRY_PATH,
					REGISTRY,
					"RegisterEvent",
					GLib.Variant("(sass)", (event, [], "")),
					None,
					Gio.DBusCallFlags.NONE,
					CALL_TIMEOUT_MS,
					None,
				)
			except GLib.Error as e:
				log.warning(f"AT-SPI capture feed: the registry did not take {event}: {e}")
		channel = GLib.IOChannel.unix_new(self._wakeRead)
		watch = GLib.io_create_watch(channel, GLib.IOCondition.IN)
		watch.set_callback(self._onWake)
		watch.attach(self._context)
		log.debug("AT-SPI capture feed: following the foreground on its own connection")
		return True

	def _subscribe(self, connection, interface, member, arg0, handler, sender=None, path=None) -> None:
		from gi.repository import Gio

		self._subscriptions.append(
			connection.signal_subscribe(
				sender, interface, member, path, arg0, Gio.DBusSignalFlags.NONE, handler
			)
		)

	def _disconnect(self) -> None:
		from gi.repository import GLib

		connection, self._connection = self._connection, None
		if connection is None:
			return
		if self._closedHandler is not None:
			connection.disconnect(self._closedHandler)
			self._closedHandler = None
		for subscription in self._subscriptions:
			connection.signal_unsubscribe(subscription)
		self._subscriptions.clear()
		try:
			# The registry forgets the feed's registrations as its connection goes.
			connection.close_sync(None)
		except GLib.Error:
			log.debugWarning("AT-SPI capture feed: closing its connection failed", exc_info=True)

	def _onClosed(self, _connection, _vanished: bool, error) -> None:
		log.warning(
			"AT-SPI capture feed: its connection to the accessibility bus closed, so browse mode's keys are "
			f"not captured: {error.message if error is not None else 'no reason given'}"
		)
		self._ending = True
		self._listening = False
		self._loop.quit()

	def _onWake(self, *_args) -> bool:
		try:
			while os.read(self._wakeRead, 64):
				pass
		except (BlockingIOError, OSError):
			pass
		self._takeLooks()
		return True

	def _held(self, handle: Callable[[float], None]) -> None:
		if self._holdEvents > 0:
			self._later(self._holdEvents, lambda: handle(self._now()))
		else:
			handle(self._now())

	def _onWindowSignal(self, _connection, sender, path, _interface, member, _parameters) -> None:
		def handle(now: float) -> None:
			if member == "Destroy":
				self._windowGone((sender, path), now)
			else:
				self._onWindowEvent((sender, path), member == "Activate", now)
			self._serviceLooks(now)

		self._held(handle)

	def _onStateSignal(self, _connection, sender, path, _interface, _member, parameters) -> None:
		state = parameters.get_child_value(0).get_string()
		value = parameters.get_child_value(1).get_int32()

		def handle(now: float) -> None:
			self._onStateChanged((sender, path), state, value, now)
			self._serviceLooks(now)

		self._held(handle)

	def _onChildrenSignal(self, _connection, _sender, _path, _interface, _member, parameters) -> None:
		child = parameters.get_child_value(3).get_variant()
		if child.get_type_string() != "(so)":
			return
		app = child.unpack()

		def handle(now: float) -> None:
			self._applicationAdded(app, now)

		self._held(handle)

	def _onNameOwnerChanged(self, _connection, _sender, _path, _interface, _member, parameters) -> None:
		name, _oldOwner, newOwner = parameters.unpack()
		if not name.startswith(":") or newOwner:
			return

		def handle(now: float) -> None:
			self._applicationGone(name, now)
			self._serviceLooks(now)

		self._held(handle)

	def _now(self) -> float:
		return time.monotonic()

	def _onFeedThread(self, call: Callable[[], None]) -> None:
		if threading.current_thread() is self._thread:
			call()
		else:
			self._later(0, call)

	def _later(self, seconds: float, call: Callable[[], None]) -> None:
		"""call once, after seconds, on the feed's thread; nothing while the feed has no thread."""
		if self._context is None:
			return
		from gi.repository import GLib

		source = GLib.timeout_source_new(max(0, int(seconds * 1000)))
		source.set_callback(lambda *_args: call() or GLib.SOURCE_REMOVE)
		source.attach(self._context)

	def _every(self, milliseconds: int, call: Callable[[], bool]):
		from gi.repository import GLib

		source = GLib.timeout_source_new(milliseconds)
		source.set_callback(lambda *_args: GLib.SOURCE_CONTINUE if call() else GLib.SOURCE_REMOVE)
		source.attach(self._context)
		return source

	def _request(self, kind: str, target, then: Callable) -> None:
		"""Ask the bus, without waiting, and call then with the answer on the feed's thread, or with None
		when there is none: "process" of a bus name, "parent" path, "state" or "children" of an object,
		or a "roundTrip" to the bus itself."""
		connection = self._connection
		if connection is None:
			then(None)
			return
		from gi.repository import Gio, GLib

		if kind == "process":
			call = (BUS, BUS_PATH, BUS, "GetConnectionUnixProcessID", GLib.Variant("(s)", (target,)), "(u)")
		elif kind == "roundTrip":
			call = (BUS, BUS_PATH, BUS, "GetId", None, "(s)")
		elif kind == "parent":
			call = (
				target[0],
				target[1],
				"org.freedesktop.DBus.Properties",
				"Get",
				GLib.Variant("(ss)", (ACCESSIBLE, "Parent")),
				"(v)",
			)
		elif kind == "state":
			call = (target[0], target[1], ACCESSIBLE, "GetState", None, "(au)")
		else:
			call = (target[0], target[1], ACCESSIBLE, "GetChildren", None, "(a(so))")
		destination, path, interface, method, parameters, replyType = call

		def finished(source, result, _data) -> None:
			try:
				reply = source.call_finish(result).unpack()
			except GLib.Error:
				then(None)
				return
			if kind == "process":
				then(int(reply[0]))
			elif kind == "roundTrip":
				then(True)
			elif kind == "parent":
				then(reply[0][1])
			elif kind == "state":
				then(list(reply[0]))
			else:
				then([(bus, child) for bus, child in reply[0]])

		connection.call(
			destination,
			path,
			interface,
			method,
			parameters,
			GLib.VariantType(replyType),
			Gio.DBusCallFlags.NONE,
			CALL_TIMEOUT_MS,
			None,
			finished,
			None,
		)
