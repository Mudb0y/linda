"""Exiting NVDA on SIGTERM, which a copy replacing this one and the end of the session send, and on
SIGHUP and SIGINT.

CPython runs a signal.signal handler only when the main thread next runs Python code, and an idle wx
main loop waits in C, so NVDA would not notice the signal until something else woke it. GLib runs
these handlers from the main loop itself, which wxGTK's loop is. Once that loop has returned nothing
runs them any more, and from then on a second quit signal ends the process at once.
"""

import signal

from gi.repository import GLib, GLibUnix

_quitSignals = (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)
#: The signals this process exits on.
_handled: list[signal.Signals] = []
_received = 0
#: Whether an exit is under way, or the dialogs are being ended so that it can start.
_isExiting = False


def install() -> None:
	"""Exit through core.triggerNVDAExit on each quit signal, from the main loop.

	Call it before taking the single-instance lock: from then on a copy replacing this one may send
	SIGTERM, which would otherwise end this one without letting go of anything it had started.
	"""
	for signalNumber in _quitSignals:
		# nohup and similar wrappers start a program with these ignored so that it outlives its terminal,
		# and a program is expected to leave them so. Nothing ignores SIGTERM to keep NVDA alive.
		if signalNumber != signal.SIGTERM and signal.getsignal(signalNumber) == signal.SIG_IGN:
			continue
		GLibUnix.signal_add(GLib.PRIORITY_HIGH, signalNumber, _onQuitSignal, signalNumber)
		_handled.append(signalNumber)


def _onQuitSignal(signalNumber: int) -> bool:
	import config
	import wx
	from gui.message import isModalMessageBoxActive
	from logHandler import log

	from . import modalDialogs

	global _received, _isExiting
	_received += 1
	log.info(f"Exiting on {signal.Signals(signalNumber).name}")
	if not _isExiting:
		_isExiting = True
		# Saved first, as Windows' end of session handler saves, so that the settings survive whatever
		# might keep NVDA from exiting after this.
		config.saveOnExit()
		# triggerNVDAExit refuses while a message box waits for an answer, and on Windows the session's
		# end posts WM_QUIT, which ends every message box's loop. A signal ends no loop, so the message
		# boxes are cancelled, which is the answer gui.message.messageBox gives once shutdown has begun.
		# ID_CLOSE_ALL, which core gives the dialogs it ends, is no answer messageBox can translate.
		modalDialogs.endModalDialogs(
			wx.ID_CANCEL,
			then=_triggerExit,
			until=lambda: not isModalMessageBoxActive(),
		)
	# Kept rather than removed, so that a repeated signal reaches this handler again instead of the
	# default action, which ends the process without saving anything.
	return GLib.SOURCE_CONTINUE


def _triggerExit() -> None:
	import core

	global _isExiting
	core.triggerNVDAExit()
	# Not triggered when a message box outlived the attempt to end it; the next quit signal tries again.
	_isExiting = core._hasShutdownBeenTriggered


def mainLoopReturned() -> None:
	"""From now on, let any quit signal but the first this process gets end it at once.

	Nothing runs the main loop's handlers any more, so without this every quit signal would be lost,
	and a shutdown that hangs could not be interrupted short of SIGKILL.
	"""
	if _received:
		_endOnNextSignal()
		return
	# The first is ignored: a copy that this one started to restart NVDA sends SIGTERM as it takes over,
	# and this one is already exiting.
	for signalNumber in _handled:
		signal.signal(signalNumber, _onSignalAfterMainLoop)


def _onSignalAfterMainLoop(signalNumber: int, frame: object) -> None:
	from logHandler import log

	log.info(f"{signal.Signals(signalNumber).name} while exiting; the next quit signal ends NVDA at once")
	_endOnNextSignal()


def _endOnNextSignal() -> None:
	for signalNumber in _handled:
		signal.signal(signalNumber, signal.SIG_DFL)
