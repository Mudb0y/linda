"""App module for KWin, Plasma's compositor, which draws the Alt+Tab switcher in a window of its own.

While Alt is held the switcher's window is active and the item Tab chose has the focus, named for the
window it stands for: NVDA speaks it as it speaks the Windows task switcher's items, and Orca as it
speaks these. The window itself is an unnamed frame, which names nothing the user chose, so it is
neither said as it takes the focus nor as an ancestor of the item.
"""

import appModuleHandler
import controlTypes
from NVDAObjects import NVDAObject


def isUnnamedTopLevel(obj: NVDAObject) -> bool:
	return (
		obj.role in (controlTypes.Role.FRAME, controlTypes.Role.WINDOW)
		and not obj.name
		and getattr(obj, "atspiParentRole", None) == "application"
	)


class UnnamedTopLevel(NVDAObject):
	isPresentableFocusAncestor = False


class AppModule(appModuleHandler.AppModule):
	def chooseNVDAObjectOverlayClasses(self, obj, clsList):
		if isUnnamedTopLevel(obj):
			clsList.insert(0, UnnamedTopLevel)

	def event_gainFocus(self, obj, nextHandler):
		if isinstance(obj, UnnamedTopLevel):
			return
		nextHandler()
