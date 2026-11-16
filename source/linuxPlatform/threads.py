"""Waiting for threads without stopping NVDA's own main loop."""

import threading


def joinWhileYieldingToWx(thread: threading.Thread) -> None:
	"""Wait for an already started thread to finish while wx keeps handling events.

	On Windows ExecAndPump dispatches window messages while it waits, so NVDA's dialogs, such as an
	add-on install's progress dialog, stay alive. On Linux the wx main loop is what dispatches them.
	"""
	import wx

	while thread.is_alive():
		thread.join(0.05)
		# Yielding from any other thread, or with no application to yield to, is an error in wx.
		if thread.is_alive() and wx.GetApp() is not None and wx.IsMainThread():
			wx.YieldIfNeeded()
