"""App module for plasmashell, Plasma's desktop shell: its panel, the application launcher, its popups
and notifications.

Each launcher or popup is an unnamed top-level frame that takes the keyboard, and names nothing the
user chose; as for KWin's switcher, it is neither said as it takes the focus nor as an ancestor of
what has it, and the launcher is heard from its search field and the item that has the focus.
"""

import appModuleHandler

from .kwin_wayland import UnnamedTopLevel, isUnnamedTopLevel


class AppModule(appModuleHandler.AppModule):
	def chooseNVDAObjectOverlayClasses(self, obj, clsList):
		if isUnnamedTopLevel(obj):
			clsList.insert(0, UnnamedTopLevel)

	def event_gainFocus(self, obj, nextHandler):
		if isinstance(obj, UnnamedTopLevel):
			return
		nextHandler()
