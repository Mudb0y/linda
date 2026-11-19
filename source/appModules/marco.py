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
from linuxPlatform import windowSwitcher
from logHandler import log


class AppModule(appModuleHandler.AppModule):
	#: Events heard from marco although it never has the focus or the foreground.
	backgroundEvents = frozenset({"nameChange"})

	def event_nameChange(self, obj, nextHandler):
		if obj.role == controlTypes.Role.STATUSBAR and obj.name:
			name = obj.name
			if windowSwitcher.cameToFrontJustNow(name) and "showing" not in obj.atspiStates:
				# Heard only once the switcher had gone and the window it named was in front, its title
				# already said, and the focus in it being said, which cancelling would cut off.
				log.debug(f"marco's switcher named {name!r} after it was gone; already in front")
				return
			# A newer choice makes the one still being spoken worthless.
			speech.cancelSpeech()
			ui.message(name)
			windowSwitcher.noteNamed(name)
			return
		nextHandler()
