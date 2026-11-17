"""The desktop's clipboard for api.copyToClip and api.getClipData, through GTK, which wx already runs.

GTK serves what NVDA copies from the main loop for as long as NVDA owns the clipboard, as
Windows serves it from its own clipboard. Reading asks the owner, another process, for its text,
and GTK hands the answer to the main loop; NVDA's callers want the text back at once, so the main
loop is run here until it comes, but only for a moment, since an owner that is hung or gone
would otherwise keep NVDA waiting as long as GTK waits for it, half a minute.
"""

import threading
import time

from logHandler import log

#: Seconds to wait for the clipboard's owner to give its text.
READ_TIMEOUT = 1.0


def _gtk():
	# Imported only once wx is running GTK: importing Gtk initialises GTK, which must not happen
	# before wx.App exists.
	import gi

	gi.require_version("Gtk", "3.0")
	gi.require_version("Gdk", "3.0")
	from gi.repository import Gdk, GLib, Gtk

	return Gtk, Gdk, GLib


def _clipboard():
	Gtk, Gdk, _GLib = _gtk()
	clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
	if clipboard is None:
		raise OSError("there is no clipboard: GTK has no display")
	return clipboard


def _requireMainThread() -> None:
	import core

	# GTK, like the Windows clipboard NVDA opens with its main window, belongs to the main thread.
	if threading.get_ident() != core.mainThreadId:
		raise OSError("the clipboard is only used from NVDA's main thread")


def setText(text: str) -> None:
	"""Own the clipboard with text, which GTK serves to any program that asks while NVDA owns it.

	:raises OSError: When GTK cannot take the clipboard.
	"""
	_requireMainThread()
	clipboard = _clipboard()
	try:
		clipboard.set_text(text, -1)
	except (TypeError, ValueError) as e:
		# A NUL, which neither GTK's text nor Windows' CF_UNICODETEXT can carry.
		raise OSError(f"the clipboard cannot hold this text: {e}") from e
	# A clipboard manager, where the desktop runs one, keeps it once NVDA exits.
	clipboard.set_can_store(None)


def getText(timeout: float = READ_TIMEOUT) -> str:
	"""The clipboard's text, or "" when it holds none.

	:param timeout: Seconds to wait for the clipboard's owner.
	:raises OSError: When the owner has not answered within timeout.
	"""
	_requireMainThread()
	_Gtk, _Gdk, GLib = _gtk()
	clipboard = _clipboard()
	answer: list[str | None] = []

	def received(_clipboard, text: str | None, *_data) -> None:
		answer.append(text)

	clipboard.request_text(received)
	if not answer:
		_runMainLoopUntil(lambda: bool(answer), timeout, GLib)
	if not answer:
		log.debugWarning(f"The clipboard's owner did not give its text within {timeout} s")
		raise OSError("the clipboard's owner did not answer")
	return answer[0] or ""


def _runMainLoopUntil(done, timeout: float, GLib) -> None:
	"""Run the main loop's sources until done() or until timeout seconds have passed.

	The source being dispatched, such as the timer of the core pump that runs NVDA's scripts, is not
	run again inside it, as GLib does not recurse into a source unless it asks.
	"""
	context = GLib.MainContext.default()
	deadline = time.monotonic() + timeout
	expired = []

	def expire() -> bool:
		expired.append(True)
		return GLib.SOURCE_REMOVE

	# Wakes the blocking iteration below once time is up, whatever else happens.
	wakeUp = GLib.timeout_add(max(1, int(timeout * 1000)), expire)
	try:
		while not done() and not expired and time.monotonic() < deadline:
			context.iteration(True)
	finally:
		if not expired:
			GLib.source_remove(wakeUp)


def copyToClip(text: str, notify: bool | None = False) -> bool:
	"""api.copyToClip on Linux: the same contract, with the copy checked by reading it back."""
	import ui

	if not isinstance(text, str) or len(text) == 0:
		return False
	# NVDA joins copied lines with Windows' line ends, which GTK hands out as Linux's, so the copy
	# would never read back as it went in.
	text = text.replace("\r\n", "\n")
	try:
		setText(text)
		got = getText()
	except OSError:
		log.debugWarning("Could not copy to the clipboard", exc_info=True)
		if notify:
			ui.reportTextCopiedToClipboard()  # No argument reports a failure.
		return False
	if got == text:
		if notify:
			ui.reportTextCopiedToClipboard(text)
		return True
	if notify:
		ui.reportTextCopiedToClipboard()  # No argument reports a failure.
	return False
