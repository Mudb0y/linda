"""App module for marco, MATE's window manager.

While Alt+Tab is held, and as the workspace changes, marco shows a popup that never takes the focus,
naming the window that releasing Alt would activate or the workspace switched to. The popup is a
window of marco's own on the accessibility bus, and the name is that of a label inside it whose role
is status bar, which marco renames for each choice. Speaking each new name gives what NVDA says for
the Windows task switcher, and what Orca says for this one.
"""

import appModuleHandler
import controlTypes
import speech
import ui


class AppModule(appModuleHandler.AppModule):
	#: Events heard from marco although it never has the focus or the foreground.
	backgroundEvents = frozenset({"nameChange"})

	def event_nameChange(self, obj, nextHandler):
		if obj.role == controlTypes.Role.STATUSBAR and obj.name:
			# A newer choice makes the one still being spoken worthless.
			speech.cancelSpeech()
			ui.message(obj.name)
			return
		nextHandler()
