"""App module for Caja, MATE's file manager.

Caja shows a folder it opens in the view that showed the folder before, and moves no focus: the view's
items are replaced, the window is renamed after the folder, and an item may be selected. Windows
Explorer moves the focus into the new folder's list, which NVDA speaks; here the new folder's name is
spoken, and an item Caja selects in it, such as the folder just left, becomes the focus.
"""

import api
import appModuleHandler
import controlTypes
import eventHandler
import ui
from NVDAObjects.AtSpi import AtSpi, IconItem


def _isIconOfFocusedView(obj) -> bool:
	"""Whether obj is a file in an icon view that has, or last had, the focus."""
	if not isinstance(obj, IconItem):
		return False
	focus = api.getFocusObject()
	if not isinstance(focus, AtSpi):
		return False
	if focus.node == obj.atspiParentNode:
		return True
	# The file that had the focus went with the folder it was in.
	return isinstance(focus, IconItem) and "defunct" in focus.atspiStates


class AppModule(appModuleHandler.AppModule):
	def event_nameChange(self, obj, nextHandler):
		if obj == api.getForegroundObject() and obj.role == controlTypes.Role.FRAME and obj.name:
			ui.message(obj.name)
			return
		nextHandler()

	def event_stateChange(self, obj, nextHandler):
		# Caja never reports an icon selected, only focused, but sends no event for that.
		if _isIconOfFocusedView(obj) and "focused" in obj.atspiStates:
			eventHandler.queueEvent("gainFocus", obj)
			return
		nextHandler()
