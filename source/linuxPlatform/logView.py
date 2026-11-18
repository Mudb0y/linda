"""How much of the log NVDA's log viewer holds on Linux.

GTK's text view takes about half a second a megabyte to fill, on the main thread, during which NVDA says
nothing and runs no command; with debug logging on, a log grows by megabytes an hour. So the viewer
holds at most the end of the log, in whole lines.
"""

import os
import shutil

import globalVars
import wx

#: The most of the log, in bytes, that the viewer holds.
MAX_SHOWN = 1 << 20


def refresh(viewer) -> None:
	"""Append to gui.logViewer.LogViewer what has been logged since its last refresh, or, once it would
	hold more than MAX_SHOWN, have it hold only the whole lines at the end of the log that fit."""
	outputCtrl: wx.TextCtrl = viewer.outputCtrl
	pos = outputCtrl.GetInsertionPoint()
	try:
		with open(globalVars.appArgs.logFileName, "rb") as f:
			size = os.fstat(f.fileno()).st_size
			start = viewer._lastFilePos
			if outputCtrl.GetLastPosition() + size - start > MAX_SHOWN:
				start = max(0, size - MAX_SHOWN)
				f.seek(start)
				if start > 0:
					# The rest of a line cut in two, which may start inside a character.
					start += len(f.readline())
				outputCtrl.Clear()
				viewer._showsOnlyTheEnd = True
				pos = 0
			else:
				f.seek(start)
			data = f.read(size - start)
	except OSError:
		return
	# A line still being written waits for the next refresh, so that a character is never cut in two.
	data = data[: data.rfind(b"\n") + 1]
	outputCtrl.AppendText(data.decode("UTF-8", errors="replace"))
	viewer._lastFilePos = start + len(data)
	outputCtrl.SetInsertionPoint(pos)


def saveWholeLog(viewer, filename: str) -> bool:
	"""Copy the whole log to filename if the viewer holds only its end, as a log saved to report a
	problem needs all of it. Return whether it did.

	:raise OSError: The log could not be copied.
	"""
	if not getattr(viewer, "_showsOnlyTheEnd", False):
		return False
	shutil.copyfile(globalVars.appArgs.logFileName, filename)
	return True
