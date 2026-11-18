"""Visual Highlight on the X display: what NVDAHighlighter's GDI+ window does on Windows.

Each outline is an override-redirect window of its own around what it highlights, filled with the
outline's colour and shaped to its line, so that everything inside and around it shows through
whether or not a compositing manager runs. Its input shape is empty, so that every click and
pointer motion goes to the window beneath, which stays the window the pointer is in. The window
manager does not manage an override-redirect window, so it is in no window list, Alt+Tab never
reaches it and it never has the focus; no toolkit draws it, so it is no accessible object.

An outline that changes is moved or resized, never only reshaped: a compositing manager may not ask
to hear of the shapes of windows it does not manage, as marco does not, and would go on showing the
old shape until something else moved, while a window that moves it always hears of.

The outlines are kept above other windows by raising them when another comes above them: one is
mapped, raised or placed directly above one of them. A raise comes at most RAISE_INTERVAL after the
last, and later still while something keeps coming back above them, so that two programs that each
want to be uppermost cannot spin each other.

The thread reads what to draw from the highlighter every NVDAHighlighter._refreshInterval, as the
Windows window's timer does, and at once when refresh is called. It alone talks to the X server; the
other threads only wake it, so a server that is slow to answer delays the outlines and nothing else.
"""

import os
import select
import threading
import time
from collections.abc import Callable
from typing import NamedTuple

from locationHelper import RectLTRB
from logHandler import log
from vision import providerBase
from winGDI import DashStyleDash, DashStyleDashDot, DashStyleDashDotDot, DashStyleDot

#: Seconds the X server has to take the connection and answer, the most a start waits.
START_TIMEOUT = 2.0
#: Seconds terminate waits for the thread to take the outlines away.
STOP_TIMEOUT = 2.0
#: Seconds at least between two raises, and the most that gap grows to while raises keep coming.
RAISE_INTERVAL = 0.25
RAISE_INTERVAL_LIMIT = 5.0
#: The lengths of GDI+'s dashes and the gaps between them, in pen widths, by dash style.
DASH_PATTERNS = {
	DashStyleDash: (3, 1),
	DashStyleDot: (1, 1),
	DashStyleDashDot: (3, 1, 1, 1),
	DashStyleDashDotDot: (3, 1, 1, 1, 1, 1),
}
#: The outlines' windows' class and name, as the Windows window's are.
WINDOW_CLASS = "NVDAHighlighter"
WINDOW_NAME = "NVDA Highlighter Window"

#: x, y, width and height, as X takes a rectangle.
Rectangle = tuple[int, int, int, int]


class Outline(NamedTuple):
	"""An outline as X draws it: where its window goes on the screen, and the rectangles of its line
	within that window."""

	window: Rectangle
	line: tuple[Rectangle, ...]


_startable: bool | None = None


def canStart() -> bool:
	"""Whether there is an X display to draw on that has input shapes, asked once; never in a Wayland
	session, whose X server draws only over the programs that use it."""
	global _startable
	if _startable is None:
		_startable = os.environ.get("XDG_SESSION_TYPE") != "wayland" and _probe()
	return _startable


def _probe() -> bool:
	result: list[bool] = []

	def probe() -> None:
		try:
			from Xlib import display

			connection = display.Display()
		except Exception:  # noqa: BLE001
			log.debugWarning("Visual Highlight has no X display", exc_info=True)
			return
		try:
			version = connection.shape_query_version() if connection.has_extension("SHAPE") else None
			# Shapes of what takes input came with version 1.1.
			result.append(
				version is not None and (version.major_version, version.minor_version) >= (1, 1),
			)
		finally:
			connection.close()

	thread = threading.Thread(target=probe, name=f"{__name__}.probe", daemon=True)
	thread.start()
	thread.join(START_TIMEOUT)
	if thread.is_alive():
		log.warning(f"The X server did not answer within {START_TIMEOUT} s; Visual Highlight cannot start")
	elif result == [False]:
		log.info("The X server has no input shapes; Visual Highlight cannot start")
	return result == [True]


def outline(rect: RectLTRB, width: int, dashStyle: int, margin: int, screen: RectLTRB) -> Outline | None:
	"""The outline NVDAHighlighter draws around rect with a pen width wide in dashStyle, centred margin
	outside it; None when rect is off the screen.

	The part of rect off the screen is cut away first, as on Windows, so that the outline of something
	larger than the screen runs along the screen's edge.
	"""
	if (
		rect.right < screen.left
		or rect.left > screen.right
		or rect.bottom < screen.top
		or rect.top > screen.bottom
	):
		return None
	rect = rect.intersection(screen)
	# GDI+ centres the pen on the rectangle, so the line begins half its width inside the margin.
	gap = margin - (width + 1) // 2
	left, top = rect.left - gap - width, rect.top - gap - width
	outerWidth = rect.right - rect.left + 2 * (gap + width)
	outerHeight = rect.bottom - rect.top + 2 * (gap + width)
	if outerWidth <= 0 or outerHeight <= 0:
		return None
	innerWidth, innerHeight = outerWidth - 2 * width, outerHeight - 2 * width
	if innerWidth <= 0 or innerHeight <= 0:
		bands = [(0, 0, outerWidth, outerHeight, True)]
	else:
		bands = [
			(0, 0, outerWidth, width, True),
			(0, outerHeight - width, outerWidth, outerHeight, True),
			(0, width, width, outerHeight - width, False),
			(outerWidth - width, width, outerWidth, outerHeight - width, False),
		]
	pattern = DASH_PATTERNS.get(dashStyle)
	line = []
	for bandLeft, bandTop, bandRight, bandBottom, across in bands:
		if pattern is None:
			line.append((bandLeft, bandTop, bandRight - bandLeft, bandBottom - bandTop))
			continue
		position, end = (bandLeft, bandRight) if across else (bandTop, bandBottom)
		index = 0
		while position < end:
			length = pattern[index % len(pattern)] * width
			if index % 2 == 0:
				stop = min(position + length, end)
				if across:
					line.append((position, bandTop, stop - position, bandBottom - bandTop))
				else:
					line.append((bandLeft, position, bandRight - bandLeft, stop - position))
			position += length
			index += 1
	return Outline((left, top, outerWidth, outerHeight), tuple(line))


class _Window:
	"""An outline's window, and what it shows now."""

	def __init__(self, window) -> None:
		self.window = window
		self.outline: Outline | None = None
		self.mapped = False


class Overlay:
	"""The outlines' windows, and the thread that draws them on a connection of its own.

	:param contents: What to draw, asked on the thread: each outline's style, a
		NVDAHighlighter.HighlightStyle, and the rectangle it goes round, the later drawn over the earlier.
	:param interval: Seconds between two looks at contents when refresh is not called.
	"""

	def __init__(self, contents: Callable[[], list[tuple[object, RectLTRB]]], interval: float) -> None:
		self._contents = contents
		self._interval = interval
		self._stopping = False
		self._ready = threading.Event()
		self._started = False
		self._wakeRead, self._wakeWrite = os.pipe()
		os.set_blocking(self._wakeWrite, False)
		#: Whether start gave the thread up, and whether the thread has ended: whichever comes second
		#: closes the pipe, the two decided under the lock.
		self._abandoned = False
		self._ended = False
		self._endLock = threading.Lock()
		self._thread = threading.Thread(target=self._run, name=f"{__name__}.Overlay", daemon=True)
		self._display = None
		self._root = None
		self._screen = RectLTRB(0, 0, 0, 0)
		#: A window for each style, with the number of earlier outlines in that style; kept unmapped
		#: while there is no such outline, as NVDA draws in a handful of styles.
		self._windows: dict[tuple[object, int], _Window] = {}
		#: The windows that show an outline, in the order they are drawn.
		self._shown: list[_Window] = []
		self._pixels: dict[tuple[int, int, int], int] = {}
		self._refreshWanted = True
		self._raiseWanted = False
		self._lastRaise = 0.0
		self._raiseGap = RAISE_INTERVAL

	def start(self) -> None:
		"""Open the connection, waiting START_TIMEOUT seconds at most.

		:raises RuntimeError: When it could not be opened in that time.
		"""
		self._thread.start()
		if not self._ready.wait(START_TIMEOUT) or not self._started:
			self._stopping = True
			self._wake()
			with self._endLock:
				self._abandoned = True
				if self._ended:
					self._closePipe()
			raise RuntimeError("Highlighter thread wasn't able to initialize correctly")

	def refresh(self) -> None:
		"""Have the thread look at what to draw now rather than at its next look."""
		self._wake()

	def stop(self) -> None:
		"""Take the outlines away and close the connection, waiting STOP_TIMEOUT seconds at most."""
		self._stopping = True
		self._wake()
		if self._thread.is_alive():
			self._thread.join(STOP_TIMEOUT)
			if self._thread.is_alive():
				log.warning(f"The X server held Visual Highlight's windows for more than {STOP_TIMEOUT} s")
				return
		self._closePipe()

	def _closePipe(self) -> None:
		os.close(self._wakeRead)
		os.close(self._wakeWrite)
		# Their numbers may soon be another file's.
		self._wakeRead = self._wakeWrite = -1

	def _wake(self) -> None:
		if self._wakeWrite < 0:
			return
		try:
			os.write(self._wakeWrite, b"\0")
		# A full pipe has a wake in it already.
		except BlockingIOError:
			pass

	def _run(self) -> None:
		try:
			self._drawUntilStopped()
		finally:
			with self._endLock:
				self._ended = True
				if self._abandoned:
					self._closePipe()

	def _drawUntilStopped(self) -> None:
		from Xlib import error

		try:
			self._open()
		except Exception:
			log.exception("Visual Highlight could not connect to the X display")
			self._ready.set()
			self._close()
			return
		self._started = True
		self._ready.set()
		try:
			self._loop()
		except (error.ConnectionClosedError, OSError):
			if not self._stopping:
				log.warning(
					"The X server closed Visual Highlight's connection; nothing is highlighted any more"
				)
		except Exception:
			log.exception("Visual Highlight stopped drawing")
		finally:
			self._close()

	def _open(self) -> None:
		from Xlib import X, display

		d = self._display = display.Display()
		d.set_error_handler(self._onError)
		self._root = d.screen().root
		geometry = self._root.get_geometry()
		self._screen = RectLTRB(0, 0, geometry.width, geometry.height)
		self._root.change_attributes(event_mask=X.StructureNotifyMask | X.SubstructureNotifyMask)
		d.sync()

	def _close(self) -> None:
		from Xlib import error

		d = self._display
		if d is None:
			return
		try:
			for window in self._windows.values():
				window.window.destroy()
			d.close()
		except (error.ConnectionClosedError, OSError):
			pass

	def _onError(self, err, request) -> None:
		log.debugWarning(f"Visual Highlight's X request failed: {err}")

	def _loop(self) -> None:
		d = self._display
		nextLook = 0.0
		while not self._stopping:
			while d.pending_events():
				self._handle(d.next_event())
			now = time.monotonic()
			if self._refreshWanted or now >= nextLook:
				self._refreshWanted = False
				nextLook = now + self._interval
				self._draw()
			raiseAt = self._lastRaise + self._raiseGap
			if self._raiseWanted and now >= raiseAt:
				self._raise(now)
			d.flush()
			timeout = nextLook - now
			if self._raiseWanted:
				timeout = min(timeout, raiseAt - now)
			readable, _w, _x = select.select([d.fileno(), self._wakeRead], [], [], max(timeout, 0))
			if self._wakeRead in readable:
				os.read(self._wakeRead, 4096)
				self._refreshWanted = True

	def _isOwn(self, window) -> bool:
		windowId = getattr(window, "id", window)
		return any(own.window.id == windowId for own in self._windows.values())

	def _handle(self, event) -> None:
		from Xlib import X

		kind = event.type
		if kind == X.ConfigureNotify and event.window.id == self._root.id:
			# Monitors were added, taken away or rearranged.
			self._screen = RectLTRB(0, 0, event.width, event.height)
			self._refreshWanted = True
			return
		# Another window was mapped, or raised or placed above an outline.
		cameAbove = (
			kind == X.MapNotify
			or (kind == X.ConfigureNotify and self._isOwn(event.above_sibling))
			or (kind == X.CirculateNotify and event.place == X.PlaceOnTop)
		)
		if cameAbove and self._shown and not self._isOwn(event.window):
			self._raiseWanted = True

	def _draw(self) -> None:
		from Xlib import X
		from Xlib.ext import shape

		try:
			contents = self._contents()
		# Whatever the highlighter raises: nothing is drawn until it answers again.
		except Exception:  # noqa: BLE001
			log.debugWarning("Asking Visual Highlight what to draw", exc_info=True)
			contents = []
		shown = []
		styles: list[object] = []
		for style, rect in contents:
			drawn = outline(rect, style.width, style.style, style.margin, self._screen)
			key = (style, styles.count(style))
			styles.append(style)
			if drawn is None:
				continue
			window = self._windows.get(key)
			if window is None:
				window = self._windows[key] = _Window(self._createWindow(style.color))
			if drawn != window.outline:
				x, y, width, height = drawn.window
				if window.outline is None or window.outline.line != drawn.line:
					window.window.shape_rectangles(
						shape.SO.Set, shape.SK.Bounding, X.Unsorted, 0, 0, drawn.line
					)
				window.window.configure(x=x, y=y, width=width, height=height)
				window.outline = drawn
			shown.append(window)
		for window in self._windows.values():
			if window.mapped and window not in shown:
				window.window.unmap()
				window.mapped = False
		if shown != self._shown or not all(window.mapped for window in shown):
			# Raised before it is mapped, an outline never shows beneath another window.
			self._restack(shown)
			for window in shown:
				if not window.mapped:
					window.window.map()
					window.mapped = True
		self._shown = shown

	def _createWindow(self, color):
		from Xlib import X
		from Xlib.ext import shape

		window = self._root.create_window(
			0,
			0,
			1,
			1,
			0,
			X.CopyFromParent,
			X.InputOutput,
			X.CopyFromParent,
			background_pixel=self._pixel(color),
			override_redirect=True,
		)
		window.set_wm_class(WINDOW_CLASS, WINDOW_CLASS)
		window.set_wm_name(WINDOW_NAME)
		# Shaped before a compositing manager first looks at it, so that it puts no shadow round it.
		window.shape_rectangles(shape.SO.Set, shape.SK.Bounding, X.Unsorted, 0, 0, [])
		window.shape_rectangles(shape.SO.Set, shape.SK.Input, X.Unsorted, 0, 0, [])
		return window

	def _restack(self, shown: list[_Window]) -> None:
		"""Put the outlines above every other window, the later of them above the earlier."""
		from Xlib import X

		for window in shown:
			window.window.configure(stack_mode=X.Above)

	def _raise(self, now: float) -> None:
		# A raise soon after the last is a sign that something else raises itself over the outlines.
		if now - self._lastRaise < 2 * self._raiseGap:
			self._raiseGap = min(self._raiseGap * 2, RAISE_INTERVAL_LIMIT)
		else:
			self._raiseGap = RAISE_INTERVAL
		self._lastRaise = now
		self._raiseWanted = False
		self._restack(self._shown)

	def _pixel(self, color) -> int:
		key = (color.red, color.green, color.blue)
		pixel = self._pixels.get(key)
		if pixel is None:
			colormap = self._display.screen().default_colormap
			pixel = self._pixels[key] = colormap.alloc_color(*(channel * 257 for channel in key)).pixel
		return pixel


class XHighlighter:
	"""What NVDAHighlighter does differently on X, put before it among a subclass's bases.

	Everything but the drawing is upstream's: the settings, the events followed, the rectangles
	asked for each context and how the focus and the navigator object are merged when they are one.
	"""

	_overlay: Overlay | None = None

	@classmethod
	def canStart(cls) -> bool:
		return canStart()

	def __init__(self) -> None:
		# NVDAHighlighter's own starts GDI+ and a window of the Windows kind.
		providerBase.VisionEnhancementProvider.__init__(self)
		log.debug("Starting NVDAHighlighter")
		self.contextToRectMap = {}
		self._overlay = Overlay(self._outlines, self._refreshInterval / 1000)
		self._overlay.start()

	def terminate(self) -> None:
		log.debug("Terminating NVDAHighlighter")
		if self._overlay is not None:
			self._overlay.stop()
			self._overlay = None
		self.contextToRectMap.clear()
		providerBase.VisionEnhancementProvider.terminate(self)

	def refresh(self) -> None:
		if self._overlay is not None:
			self._overlay.refresh()

	def _outlines(self) -> list[tuple[object, RectLTRB]]:
		from visionEnhancementProviders.NVDAHighlighter import HighlightWindow

		# It reads the highlighter alone, nothing of the Windows window it is a method of.
		contextRects = HighlightWindow._getDrawRects(None, self)
		return [(self._ContextStyles[context], rect) for context, rect in contextRects.items()]
