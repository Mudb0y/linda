"""Ending the modal dialogs that are open, one at a time and innermost first, so that NVDA can exit.

NVDA's exit ends each modal dialog with EndModal, in the order the dialogs were made, and on Windows
the end of the session posts WM_QUIT, which ends every nested message loop at once. On wxGTK both
leave NVDA running for ever, with its plugins already terminated and its main frame destroyed, when
more than one dialog is open. A modal dialog cannot end while another runs inside it: EndModal on the
outer one leaves its loop running. And the dialogs wxGTK shows with GTK's own widgets, which
wx.MessageBox, wx.MessageDialog, wx.FileDialog and wx.DirDialog open, are not among wx's windows at
all.

So the dialog ended is the innermost, the one holding GTK's grab, and the next one out is ended only
once the call that showed it has returned. GLib dispatches from one nested loop fewer by then.
"""

import time
from collections.abc import Callable

import gi
import wx
from logHandler import log

# wxGTK has already loaded GTK 3; this binds to that same library.
gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk

#: Milliseconds between looks at the dialogs while they close.
_INTERVAL = 20
#: Dialogs ended at most. Code that shows another dialog each time its own is ended would otherwise
#: keep NVDA from exiting for ever.
MAX_DIALOGS = 20
#: Seconds the call that showed an ended dialog has to return, before the dialogs outside it are given
#: up on.
RETURN_TIMEOUT = 3.0

ModalDialog = wx.Dialog | Gtk.Dialog


def _openDialogs() -> list[ModalDialog]:
	"""The modal dialogs showing, wx's and GTK's own, the innermost last."""
	native = [
		window
		for window in Gtk.Window.list_toplevels()
		if isinstance(window, Gtk.Dialog) and window.get_modal() and window.get_visible()
	]
	dialogs = [*native, *(w for w in wx.GetTopLevelWindows() if isinstance(w, wx.Dialog) and w.IsModal())]
	# GTK gives the grab to each modal window as it is shown, so the one holding it is the innermost.
	# wx's dialogs are GTK windows but not GTK dialogs, so a grab that is not one of GTK's dialogs is
	# held by the innermost of wx's, the one made last.
	grab = Gtk.grab_get_current()
	if grab is not None and any(grab is dialog for dialog in native):
		dialogs.remove(grab)
		dialogs.append(grab)
	return dialogs


def isOneWxDialogAtMost() -> bool:
	"""Whether the modal dialogs open are no more than one of wx's own, which EndModal ends on wxGTK as it
	does on Windows."""
	dialogs = _openDialogs()
	return not dialogs or (len(dialogs) == 1 and isinstance(dialogs[0], wx.Dialog))


def _end(dialog: ModalDialog, returnCode: int) -> None:
	log.debug(f"ending modal {dialog} during exit process")
	if isinstance(dialog, Gtk.Dialog):
		# wx returns ID_CANCEL, or wx.CANCEL from wx.MessageBox, for this whatever dialog it is.
		dialog.response(Gtk.ResponseType.CANCEL)
	else:
		dialog.EndModal(returnCode)


class _Unwinding:
	def __init__(self, returnCode: int, then: Callable[[], None], until: Callable[[], bool]) -> None:
		self._returnCode = returnCode
		self._then = then
		self._until = until
		self._ended = 0
		#: The dialog ended last, and those open around it then.
		self._waitingFor: list[ModalDialog] = []
		#: How deep in nested loops GLib was dispatching when the last dialog was ended.
		self._depth = 0
		self._deadline = 0.0

	def turn(self) -> bool:
		"""End the innermost dialog if it may be ended now, finish, or give up; True to be called again.

		Giving up leaves then uncalled. Whatever kept the dialogs open would keep NVDA from exiting
		however its exit went on, and then may well start ending them again.
		"""
		try:
			dialogs = _openDialogs()
			innermost = dialogs[-1] if dialogs else None
			if (
				self._waitingFor
				and GLib.main_depth() >= self._depth
				and (innermost is None or any(innermost is d for d in self._waitingFor))
			):
				# The call that showed the dialog ended last has not returned, and no dialog has opened
				# inside it since.
				if time.monotonic() < self._deadline:
					return True
				log.error(
					f"Gave up ending modal dialogs: the call that showed {self._waitingFor[0]} has not returned "
					f"after {RETURN_TIMEOUT} s",
				)
				return False
			if innermost is None or self._until():
				return self._finish()
			if self._ended >= MAX_DIALOGS:
				log.error(
					f"Gave up ending modal dialogs after ending {MAX_DIALOGS}; {innermost} is still open"
				)
				return False
			_end(innermost, self._returnCode)
		except Exception:
			log.exception("Gave up ending modal dialogs")
			return False
		self._ended += 1
		self._waitingFor = [innermost, *dialogs[:-1]]
		self._depth = GLib.main_depth()
		self._deadline = time.monotonic() + RETURN_TIMEOUT
		return True

	def _finish(self) -> bool:
		try:
			self._then()
		except Exception:
			log.exception(f"Error in {self._then} once modal dialogs were ended")
		# wx destroys the windows scheduled for destruction in its idle processing, which a GLib source
		# does not wake. Not called through wx.CallAfter instead: wx would destroy them in the same pass,
		# before a dialog that core ended had left its modal loop.
		wx.WakeUpIdle()
		return False

	def _turnFromTimeout(self) -> bool:
		return GLib.SOURCE_CONTINUE if self.turn() else GLib.SOURCE_REMOVE


def endModalDialogs(
	returnCode: int,
	then: Callable[[], None],
	until: Callable[[], bool] = lambda: False,
) -> None:
	"""End the modal dialogs that are open, innermost first, until until() is true or none is left, and
	then call then.

	The first dialog is ended at once, and each one after it on a later turn of the main loop. Every
	call that showed a dialog gets its answer and returns before the dialog outside it is ended.

	:param returnCode: What each of wx's dialogs returns to the code that showed it. GTK's own dialogs
		are cancelled, which wx returns as ID_CANCEL, or as wx.CANCEL from wx.MessageBox.
	:param then: Called from the main loop once the dialogs are ended. Never called when they are given
		up on, which logs an error: when the call that showed one has not returned within
		RETURN_TIMEOUT, or once MAX_DIALOGS have been ended.
	:param until: Ends the unwinding early once it returns True.
	"""
	unwinding = _Unwinding(returnCode, then, until)
	if unwinding.turn():
		GLib.timeout_add(_INTERVAL, unwinding._turnFromTimeout)
