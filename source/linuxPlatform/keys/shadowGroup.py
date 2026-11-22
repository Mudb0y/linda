"""The group of a keymap of several layouts where nothing says which is in use, followed from the keys
NVDA passes on.

localed and the XKB_DEFAULT variables name a keymap but never its group, and a group switch an xkb
option makes, as grp:alt_shift_toggle's Alt with Shift, happens inside the compositor with no word to
anyone. Every key that reaches it from a keyboard linda-keyd holds is one NVDA passed on, and so
heard of: an XKB state of NVDA's own, fed those keys as the compositor feeds its own, locks the same
group. A key that reaches the compositor from a keyboard linda-keyd leaves alone is not heard, and
then the two can differ; the layout cross-check in typedChars notices that.

Keys are held in memory only, and only while they are down, and never logged.
"""

import queue
import threading
from collections.abc import Callable

from logHandler import log
from xkbcommon import xkb

from . import desktopLayout
from .desktopLayout import LayoutSnapshot

_DOWN = xkb.KeyDirection.XKB_KEY_DOWN
_UP = xkb.KeyDirection.XKB_KEY_UP
_LOCKED = xkb.StateComponent.XKB_STATE_LAYOUT_LOCKED


def severalLayouts(snapshot: LayoutSnapshot):
	"""The keymap snapshot names when it holds more than one layout; None otherwise, or when it cannot be
	compiled, which the environment says when it compiles the same names."""
	rules, model, layout, variant, options = snapshot.names
	try:
		keymap = xkb.Context().keymap_new_from_names(
			rules=rules or None,
			model=model or None,
			layout=layout or None,
			variant=variant or None,
			options=options or None,
		)
	except xkb.XKBError:
		return None
	return keymap if keymap.num_layouts() > 1 else None


class ShadowGroup:
	"""What a feed of names alone passes on: each layout the desktop gives, and for one of several
	layouts, the group the keys passed on lock, each on a thread of its own."""

	def __init__(self, onChange: Callable[[LayoutSnapshot], None]) -> None:
		self._onChange = onChange
		self._lock = threading.Lock()
		#: Keys passed on, as (key code, whether released), and layouts, as (None, snapshot, keymap), for
		#: the thread; None stops it.
		self._queue: queue.SimpleQueue = queue.SimpleQueue()
		self._thread: threading.Thread | None = None
		#: Whether keys are taken: only while the thread runs, which it does only for a keymap of several
		#: layouts.
		self._listening = False
		self._stopping = False
		#: Whether a layout has come, at start or since.
		self._started = False
		#: The last snapshot passed on, or given by the feed's start.
		self._passedOn: LayoutSnapshot | None = None
		# The thread's own: the desktop's last layout, and the state its keys go to.
		self._base: LayoutSnapshot | None = None
		self._state = None
		self._down: set[int] = set()

	def start(self, snapshot: LayoutSnapshot) -> None:
		"""Follow the group of snapshot, the one the feed's start gave, unless a later one came first."""
		keymap = severalLayouts(snapshot)
		with self._lock:
			if self._started or self._stopping:
				return
			self._started = True
			self._passedOn = snapshot
			if keymap is not None:
				self._startThread()
				self._queue.put((None, snapshot, keymap))

	def change(self, snapshot: LayoutSnapshot) -> None:
		"""A later layout from the desktop, on the feed's own thread: passed on, and its group followed."""
		keymap = severalLayouts(snapshot)
		with self._lock:
			if self._stopping:
				return
			self._started = True
			if self._thread is None and keymap is None:
				self._passedOn = snapshot
				direct = True
			else:
				if self._thread is None:
					self._startThread()
				self._queue.put((None, snapshot, keymap))
				direct = False
		if direct:
			self._passOn(snapshot)

	def notePassed(self, keycode: int, isUp: bool) -> None:
		"""A key NVDA passed on, or its release, as the environment hears of it: an append, from the thread
		that answers keys."""
		if self._listening:
			self._queue.put((keycode, isUp))

	def stop(self) -> None:
		with self._lock:
			self._stopping = True
			self._listening = False
			thread = self._thread
		if thread is None:
			return
		self._queue.put(None)
		if thread is not threading.current_thread():
			thread.join(desktopLayout.STOP_TIMEOUT)
			if thread.is_alive():
				log.debugWarning("Keyboard layout: the group's thread did not end in time")

	def _startThread(self) -> None:
		self._thread = threading.Thread(target=self._run, name="lindaKeyboardLayout", daemon=True)
		self._listening = True
		self._thread.start()

	def _run(self) -> None:
		try:
			while True:
				item = self._queue.get()
				# All that came meanwhile, and one layout for it: a group switched and switched back while
				# NVDA was busy changes nothing.
				while True:
					if item is None:
						return
					if item[0] is None:
						self._use(item[1], item[2])
					else:
						self._key(*item)
					try:
						item = self._queue.get_nowait()
					except queue.Empty:
						break
				self._passOnNow()
		except Exception:
			log.exception("Keyboard layout: following the group failed")
		finally:
			with self._lock:
				self._listening = False
				# A later layout then starts another thread, rather than waiting on the queue for this one.
				if self._thread is threading.current_thread():
					self._thread = None

	def _use(self, snapshot: LayoutSnapshot, keymap) -> None:
		before = self._base
		self._base = snapshot
		if keymap is None:
			self._state = None
			self._down.clear()
			return
		if self._state is not None and before is not None and before.names == snapshot.names:
			# The same keymap: the compositor keeps its group, and so does this.
			return
		self._state = keymap.state_new()
		self._state.update_mask(0, 0, 0, 0, 0, snapshot.group)
		self._down.clear()

	def _key(self, keycode: int, isUp: bool) -> None:
		state = self._state
		if state is None:
			return
		down = self._down
		if isUp:
			if keycode not in down:
				return
			down.discard(keycode)
			state.update_key(keycode, _UP)
			return
		if keycode in down:
			# Its release was never heard, as when the helper let go of it; a second press would hold it
			# down twice in XKB's count, and a lone key after it would make a chord the compositor never had.
			state.update_key(keycode, _UP)
		down.add(keycode)
		state.update_key(keycode, _DOWN)

	def _passOnNow(self) -> None:
		base = self._base
		if base is None:
			return
		snapshot = base
		if self._state is not None:
			snapshot = base._replace(group=self._state.serialize_layout(_LOCKED))
		with self._lock:
			before = self._passedOn
			if snapshot == before or self._stopping:
				return
			self._passedOn = snapshot
		if before is not None and before.names == snapshot.names and before.group != snapshot.group:
			# The feed logs a layout the desktop gave; a group the keys switched to is this one's to log.
			log.debug(f"Keyboard layout from {snapshot.source}: {desktopLayout.activeLayoutName(snapshot)}")
		self._passOn(snapshot)

	def _passOn(self, snapshot: LayoutSnapshot) -> None:
		if self._stopping:
			return
		try:
			self._onChange(snapshot)
		except Exception:
			log.exception("Keyboard layout: passing a change on failed")
