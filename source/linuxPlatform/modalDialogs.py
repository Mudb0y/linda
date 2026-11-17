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

When the dialogs cannot be ended, NVDA is ended without them, with its configuration saved: the loops
they run would keep it from ever leaving its main loop, half shut down.
"""

import os
import sys
import time
from collections.abc import Callable
from typing import NoReturn

import gi
import wx
from logHandler import log

# wxGTK has already loaded GTK 3; this binds to that same library.
gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk

#: Milliseconds between looks at the dialogs while they close.
_INTERVAL = 20
#: Dialogs ended at most while NVDA exits. Code that shows another dialog each time its own is ended
#: would otherwise keep NVDA from exiting for ever.
MAX_DIALOGS = 20
#: Seconds the call that showed an ended dialog has to return, before the dialogs outside it are given
#: up on.
RETURN_TIMEOUT = 3.0
#: The exit status when the dialogs are given up on.
GIVEN_UP_STATUS = 1

ModalDialog = wx.Dialog | Gtk.Dialog | Gtk.Menu

#: Dialogs ended so far, by every unwinding: core's runs again after each, and a quit signal's first.
_ended = 0


def _shownMenus() -> list[Gtk.Menu]:
	"""The popup menus showing, the NVDA menu among them, without their submenus."""
	return [
		window.get_child()
		for window in Gtk.Window.list_toplevels()
		if window.get_window_type() == Gtk.WindowType.POPUP
		and isinstance(window.get_child(), Gtk.Menu)
		and window.get_mapped()
		and not isinstance(window.get_child().get_attach_widget(), Gtk.MenuItem)
	]


def _openDialogs() -> list[ModalDialog]:
	"""The modal dialogs showing, wx's and GTK's own, the innermost last, and after them any popup menu.

	A popup menu is closed like a dialog: NVDA's exit, run from inside the menu, destroyed the NVDA
	menu while it was showing, and with the add-on store open that crashed NVDA as the store was closed.
	"""
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
	# A menu is shown over the window it was opened from, and nothing modal opens while it shows.
	return [*dialogs, *_shownMenus()]


def areNoneOpen() -> bool:
	"""Whether no modal dialog or popup menu is showing, so that NVDA's exit runs in the main loop itself.

	Even one of wx's dialogs, which EndModal ends on wxGTK as on Windows, is ended here first: the exit
	otherwise ran inside that dialog's loop, and once the loop had returned the main loop slept with
	NVDA's windows scheduled for destruction, which a restart from the Exit dialog left running until
	the new copy killed it.
	"""
	return not _openDialogs()


def _end(dialog: ModalDialog, returnCode: int) -> None:
	log.debug(f"ending modal {dialog} during exit process")
	if isinstance(dialog, Gtk.Menu):
		dialog.cancel()
	elif isinstance(dialog, Gtk.Dialog):
		# wx returns ID_CANCEL, or wx.CANCEL from wx.MessageBox, for this whatever dialog it is.
		dialog.response(Gtk.ResponseType.CANCEL)
	else:
		dialog.EndModal(returnCode)


def _destroyOnceReturned(dialog: ModalDialog) -> None:
	"""Destroy one of wx's dialogs that was ended, now that the call that showed it has returned.

	Left alive, it is no longer modal, and NVDA's exit closes it like any other window: the welcome
	dialog then ended itself again from its close handler, which wx refuses for a dialog that is not
	modal.
	"""
	if isinstance(dialog, wx.Dialog) and dialog and not dialog.IsBeingDeleted() and not dialog.IsModal():
		dialog.Destroy()


def _giveUp(reason: str) -> NoReturn:
	import config

	message = f"Gave up ending modal dialogs: {reason}; ending NVDA without finishing its exit"
	log.error(message)
	config.saveOnExit()
	if sys.__stderr__ is not None:
		print(f"linda: {message}", file=sys.__stderr__, flush=True)
	os._exit(GIVEN_UP_STATUS)


class _Unwinding:
	def __init__(self, returnCode: int, then: Callable[[], None], until: Callable[[], bool]) -> None:
		self._returnCode = returnCode
		self._then = then
		self._until = until
		#: The dialog ended last, and those open around it then.
		self._waitingFor: list[ModalDialog] = []
		#: How deep in nested loops GLib was dispatching when the last dialog was ended.
		self._depth = 0
		self._deadline = 0.0

	def turn(self) -> bool:
		"""End the innermost dialog if it may be ended now, or finish; True to be called again."""
		global _ended
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
				_giveUp(
					f"the call that showed {self._waitingFor[0]} has not returned after {RETURN_TIMEOUT} s"
				)
			if self._waitingFor:
				_destroyOnceReturned(self._waitingFor[0])
			if innermost is None or self._until():
				return self._finish()
			if _ended >= MAX_DIALOGS:
				_giveUp(f"{innermost} is still open after ending {MAX_DIALOGS}")
			_end(innermost, self._returnCode)
		except Exception:
			log.exception("Error ending modal dialogs")
			_giveUp("an error ended the unwinding")
		_ended += 1
		if isinstance(innermost, Gtk.Menu):
			# wx's PopupMenu returns as the menu hides, and a combo box's list has no call to wait for.
			self._waitingFor = []
			return True
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

	The dialogs are given up on when the call that showed one has not returned within RETURN_TIMEOUT,
	or once MAX_DIALOGS have been ended during this exit. That logs an error, saves the configuration
	and ends the process at once with GIVEN_UP_STATUS.

	:param returnCode: What each of wx's dialogs returns to the code that showed it. GTK's own dialogs
		are cancelled, which wx returns as ID_CANCEL, or as wx.CANCEL from wx.MessageBox.
	:param then: Called from the main loop once the dialogs are ended.
	:param until: Ends the unwinding early once it returns True.
	"""
	unwinding = _Unwinding(returnCode, then, until)
	if unwinding.turn():
		GLib.timeout_add(_INTERVAL, unwinding._turnFromTimeout)
