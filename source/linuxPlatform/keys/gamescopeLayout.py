"""The keyboard layout gamescope compiles for every keyboard, read from gamescope's own Xwayland.

gamescope 3.17 compiles one keymap for all keyboards from the XKB_DEFAULT variables, with the layout
and variant Steam last gave it in place of theirs (wlserver_update_keymap in src/wlserver.cpp). Steam
gives them as the root window property GAMESCOPE_KEYBOARD_LAYOUT of gamescope's Xwayland, "layout" or
"layout:variant", which gamescope reads again on each PropertyNotify (steamcompmgr.cpp), or through
gamescope_control's set_keyboard_layout, which only a Wayland client of gamescope's would see. The
property is read here, on an X connection and a thread of the feed's own.

Xwayland's _XKB_RULES_NAMES says nothing: Xwayland compiles the keymap a compositor sends it from that
keymap's text and leaves the property at its own built-in defaults, US English, whatever the keymap.
"""

import os
import select
import threading
from collections.abc import Callable

from logHandler import log

from .. import displayServer
from .desktopLayout import (
	DEFAULT_MODEL,
	DEFAULT_RULES,
	STOP_TIMEOUT,
	LayoutSnapshot,
	activeLayoutName,
	environmentSnapshot,
)

SOURCE = "gamescope"
#: The root window property of gamescope's Xwayland that Steam writes the layout to.
LAYOUT_PROPERTY = "GAMESCOPE_KEYBOARD_LAYOUT"
#: libxkbcommon's XKB_LOG_LEVEL_CRITICAL: a layout that does not compile is reported here, not by it.
_XKB_LOG_CRITICAL = 10


def namesFor(value: str) -> tuple[str, str, str, str, str] | None:
	"""The names gamescope compiles for the property's value, or None when the value names no layout
	and gamescope goes by the XKB_DEFAULT variables alone."""
	# XGetTextProperty, with which gamescope reads it, gives a C string: it ends at the first NUL.
	layout, _colon, variant = value.split("\0", 1)[0].partition(":")
	if not layout:
		# gamescope_control says a variant without a layout is ignored.
		return None
	env = os.environ
	return (
		env.get("XKB_DEFAULT_RULES") or DEFAULT_RULES,
		env.get("XKB_DEFAULT_MODEL") or DEFAULT_MODEL,
		layout,
		variant,
		env.get("XKB_DEFAULT_OPTIONS", ""),
	)


def _compiles(names: tuple[str, str, str, str, str]) -> bool:
	from xkbcommon import xkb

	context = xkb.Context()
	context.set_log_level(_XKB_LOG_CRITICAL)
	rules, model, layout, variant, options = names
	try:
		context.keymap_new_from_names(
			rules=rules or None,
			model=model or None,
			layout=layout or None,
			variant=variant or None,
			options=options or None,
		)
	except xkb.XKBError:
		return False
	return True


class GamescopeLayoutFeed:
	"""The layout gamescope gives every keyboard, followed through PropertyNotify on its Xwayland's
	root window."""

	def __init__(self) -> None:
		self._lock = threading.Lock()
		self._display = None
		self._atom = 0
		self._thread: threading.Thread | None = None
		self._onChange: Callable[[LayoutSnapshot], None] | None = None
		self._stopping = False
		self._last: LayoutSnapshot | None = None
		self._wakeRead, self._wakeWrite = -1, -1

	def start(self, onChange: Callable[[LayoutSnapshot], None]) -> LayoutSnapshot:
		"""Connect to gamescope's Xwayland and give the layout now.

		:raise Exception: When that Xwayland cannot be reached, so that desktopLayout starts the
			desktop's own feed instead.
		"""
		from Xlib import X, display

		if self._display is not None:
			raise RuntimeError("the layout feed has started already")
		self._onChange = onChange
		connection = display.Display()
		try:
			root = connection.screen().root
			self._atom = connection.intern_atom(LAYOUT_PROPERTY)
			# Selected before the first reading, so that no change between the two is missed.
			root.change_attributes(event_mask=X.PropertyChangeMask)
			self._display = connection
			snapshot = self._read(None)
		except BaseException:
			self._display = None
			connection.close()
			raise
		with self._lock:
			self._last = snapshot
		self._wakeRead, self._wakeWrite = os.pipe2(os.O_CLOEXEC | os.O_NONBLOCK)
		self._thread = threading.Thread(target=self._run, name="lindaKeyboardLayout", daemon=True)
		self._thread.start()
		log.info(f"Keyboard layout from {snapshot.source}: {activeLayoutName(snapshot)}")
		return snapshot

	def stop(self) -> None:
		self._stopping = True
		thread = self._thread
		if thread is None:
			return
		try:
			os.write(self._wakeWrite, b"\0")
		except OSError:
			pass
		if thread is not threading.current_thread():
			thread.join(STOP_TIMEOUT)
			if thread.is_alive():
				log.debugWarning("Keyboard layout: the feed's thread did not end in time")
				return
		self._thread = None
		self._close()

	def _close(self) -> None:
		connection, self._display = self._display, None
		if connection is not None:
			try:
				connection.close()
			except Exception:  # noqa: BLE001
				log.debugWarning("Keyboard layout: closing the X connection failed", exc_info=True)
		for fd in (self._wakeRead, self._wakeWrite):
			if fd >= 0:
				os.close(fd)
		self._wakeRead, self._wakeWrite = -1, -1

	def _value(self) -> str:
		from Xlib import X

		prop = self._display.screen().root.get_full_property(self._atom, X.AnyPropertyType)
		if prop is None or prop.format != 8 or not prop.value:
			return ""
		value = prop.value
		return value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value)

	def _read(self, last: LayoutSnapshot | None) -> LayoutSnapshot:
		"""What gamescope compiles now, given the snapshot it compiled last, which it keeps when the
		property names a layout that does not compile."""
		names = namesFor(self._value())
		if names is None:
			return environmentSnapshot()
		if not _compiles(names):
			log.info("Keyboard layout: gamescope's Xwayland names a layout that does not compile")
			# gamescope's own start compiled the environment's.
			return last if last is not None else environmentSnapshot()
		return LayoutSnapshot(names, source=SOURCE)

	def _run(self) -> None:
		from Xlib import X, error

		connection = self._display
		try:
			while not self._stopping:
				# Events Xlib already holds first: reading a reply may have queued some, and select would
				# not see them.
				changed = False
				while connection.pending_events():
					event = connection.next_event()
					if event.type == X.PropertyNotify and event.atom == self._atom:
						changed = True
				if changed:
					self._publish(self._read(self._last))
					continue
				select.select([connection.fileno(), self._wakeRead], [], [])
		except (error.ConnectionClosedError, OSError):
			if not self._stopping:
				log.debugWarning("Keyboard layout: gamescope's Xwayland closed the connection")
		except Exception:
			log.exception("Keyboard layout: following gamescope's layout failed")

	def _publish(self, snapshot: LayoutSnapshot) -> None:
		with self._lock:
			if snapshot == self._last:
				return
			self._last = snapshot
		if self._stopping:
			return
		log.debug(f"Keyboard layout from {snapshot.source}: {activeLayoutName(snapshot)}")
		try:
			self._onChange(snapshot)
		except Exception:
			log.exception("Keyboard layout: passing a change on failed")


def create(currentDesktop: str | None) -> GamescopeLayoutFeed | None:
	"""A feed when NVDA takes keys the Wayland way under gamescope, for desktopLayout.create; None
	otherwise, an X session under gamescope among them, whose keys and layout are the X server's."""
	if not displayServer.isWayland() or displayServer.compositor() != "gamescope":
		return None
	return GamescopeLayoutFeed()
