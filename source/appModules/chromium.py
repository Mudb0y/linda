"""App module for Chromium on Linux, and the base of those for programs built on it, Electron's among
them. On Windows these programs are served by NVDA's IAccessible2 support, and the module gives them
the default app module, as before.

Chromium joins the accessibility bus only when the desktop's accessibility status was on as it
started; then it shows its windows, but builds the accessible tree of a web page only once a client
asks for something that, by Chromium's reckoning, only a screen reader asks for: an object's
attributes or its relations. Asking for the attributes of each window that comes to the foreground
makes sure that happens whatever else NVDA happens to ask first.

A web page's landmarks are named for their kind, "main landmark" rather than "landmark", from the
ARIA roles Chromium gives with them, as NVDA does for Chromium's IAccessible2 objects on Windows.
"""

import sys

import appModuleHandler

if sys.platform == "linux":
	import aria
	import controlTypes
	from linuxPlatform.atspi import handler
	from linuxPlatform.atspi.client import NodeGone
	from NVDAObjects.AtSpi import AtSpi

	class WebLandmark(AtSpi):
		def _get_landmark(self) -> str | None:
			xmlRoles = self.atspiAttributes.get("xml-roles", "").split(" ")
			return next((role for role in xmlRoles if role in aria.landmarkRoles), None)

	class AppModule(appModuleHandler.AppModule):
		def chooseNVDAObjectOverlayClasses(self, obj, clsList):
			if isinstance(obj, AtSpi) and obj.role == controlTypes.Role.LANDMARK:
				clsList.insert(0, WebLandmark)

		def event_foreground(self, obj, nextHandler):
			if isinstance(obj, AtSpi):
				try:
					handler.client().attributes(obj.node)
				except NodeGone:
					pass
			nextHandler()

else:
	AppModule = appModuleHandler.AppModule
