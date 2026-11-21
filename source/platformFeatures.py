# A part of NonVisual Desktop Access (NVDA)
# This file is covered by the GNU General Public License.
# See the file COPYING for more details.

"""What the operating system NVDA runs on supports, feature by feature.

The menus, the settings panels and the commands ask here before they offer a feature, so that what
cannot work on a platform is not shown there at all, rather than offered and failing. On Windows every
feature is supported and nothing is hidden.
"""

import sys
from enum import Enum, auto


class Feature(Enum):
	MAGNIFIER = auto()
	SCREEN_CURTAIN = auto()
	#: The Visual Highlight vision enhancement provider, the only one NVDA comes with.
	VISUAL_HIGHLIGHT = auto()
	AUDIO_DUCKING = auto()
	SOUND_SPLIT = auto()
	#: Keeping the display on during say all or reading with braille.
	KEEP_DISPLAY_ON = auto()
	#: Checking for, downloading and installing updates of NVDA, and the usage statistics sent with the check.
	UPDATE_CHECK = auto()
	PORTABLE_COPY = auto()
	INSTALLER = auto()
	#: The System Accessibility Repair Tool, which repairs COM registrations.
	COM_REGISTRATION_FIXES = auto()
	#: NVDA on the sign-in screen and on secure screens, with the user's settings copied for them.
	SECURE_SCREENS = auto()
	UI_AUTOMATION = auto()
	#: NVDA's own support for the Windows Console and Windows Terminal.
	WINDOWS_CONSOLE = auto()
	SAPI4 = auto()
	#: Browsers' documents reached through IAccessible and IAccessible2.
	IACCESSIBLE = auto()
	#: Braille displays NVDA's own drivers reach through serial, USB, HID and Bluetooth ports.
	BRAILLE_PORTS = auto()
	TOUCH = auto()
	WINDOWS_OCR = auto()
	#: Reporting the candidates and composition of an input method.
	INPUT_COMPOSITION = auto()
	#: The text of an object as GDI drew it.
	DISPLAY_MODEL = auto()
	#: Help, training and support, and the shop, from NV Access, who support NVDA as they ship it.
	NV_ACCESS_SUPPORT = auto()
	#: Clicking, moving and finding the mouse pointer, and the object under it.
	POINTER = auto()
	#: Following the pointer as it moves, and the audio coordinates it plays.
	MOUSE_TRACKING = auto()


if sys.platform == "win32":
	_UNSUPPORTED: frozenset[Feature] = frozenset()
	#: What a command of a feature this platform lacks says when something still runs it.
	UNAVAILABLE_MESSAGE = ""
	_WAYLAND_UNSUPPORTED: frozenset[Feature] = frozenset()
	WAYLAND_UNAVAILABLE_MESSAGE = ""
else:
	from linuxPlatform import displayServer

	# A feature that has been made to work on Linux is switched on by deleting its line.
	_UNSUPPORTED = frozenset(
		{
			Feature.MAGNIFIER,
			Feature.AUDIO_DUCKING,
			Feature.SOUND_SPLIT,
			Feature.KEEP_DISPLAY_ON,
			Feature.UPDATE_CHECK,
			Feature.PORTABLE_COPY,
			Feature.INSTALLER,
			Feature.COM_REGISTRATION_FIXES,
			Feature.SECURE_SCREENS,
			Feature.UI_AUTOMATION,
			Feature.WINDOWS_CONSOLE,
			Feature.SAPI4,
			Feature.IACCESSIBLE,
			Feature.BRAILLE_PORTS,
			Feature.TOUCH,
			Feature.WINDOWS_OCR,
			Feature.INPUT_COMPOSITION,
			Feature.DISPLAY_MODEL,
			Feature.NV_ACCESS_SUPPORT,
		},
	)
	UNAVAILABLE_MESSAGE = "Not available on Linux."
	# These work through the X server, which in a Wayland session is Xwayland, if there is one: it sees
	# the pointer only over its own programs, cannot draw over the others, and on GNOME a first
	# connection to it would start it.
	_WAYLAND_UNSUPPORTED = frozenset(
		{
			Feature.SCREEN_CURTAIN,
			Feature.VISUAL_HIGHLIGHT,
			Feature.POINTER,
			Feature.MOUSE_TRACKING,
		},
	)
	WAYLAND_UNAVAILABLE_MESSAGE = "Not available in this Wayland session."
	if displayServer.saysWayland():
		_UNSUPPORTED |= _WAYLAND_UNSUPPORTED


def isSupported(feature: Feature) -> bool:
	return feature not in _UNSUPPORTED


def unavailableMessage(feature: Feature) -> str:
	"""What a command of feature says when it runs where the feature is unsupported."""
	return WAYLAND_UNAVAILABLE_MESSAGE if feature in _WAYLAND_UNSUPPORTED else UNAVAILABLE_MESSAGE


def hideUnsupportedScripts(
	cls: type,
	features: dict[str, Feature],
	categoryFeatures: dict[str, Feature] | None = None,
	keepGestures: frozenset[Feature] | set[Feature] = frozenset(),
) -> None:
	"""Leaves out of a scriptable class, before any instance of it is made, each script of a feature
	this platform lacks: it is bound to no gesture and has no description, so that the Input Gestures
	dialog does not list it, and a gesture a user's or a driver's map still binds to it speaks
	unavailableMessage.

	:param features: The feature of each script, by its name without script_.
	:param categoryFeatures: The feature of every script in a category, by the category's name.
	:param keepGestures: Features whose scripts keep the class's own gestures, so that a key a person
		knows for one says why nothing happens rather than reaching the application.
	"""
	unsupported = {name: feature for name, feature in features.items() if not isSupported(feature)}
	for category, feature in (categoryFeatures or {}).items():
		if not isSupported(feature):
			unsupported.update(
				(attribute[len("script_") :], feature)
				for attribute, script in vars(cls).items()
				if attribute.startswith("script_") and getattr(script, "category", None) == category
			)
	if not unsupported:
		return
	gestures = vars(cls).get(f"_{cls.__name__}__gestures", {})
	for gesture in [
		gesture
		for gesture, name in gestures.items()
		if name in unsupported and unsupported[name] not in keepGestures
	]:
		del gestures[gesture]
	for name, feature in unsupported.items():
		setattr(cls, f"script_{name}", _unavailableScript(name, feature))


def _unavailableScript(name: str, feature: Feature | None = None):
	message = UNAVAILABLE_MESSAGE if feature is None else unavailableMessage(feature)

	def script(self, gesture) -> None:
		import ui

		ui.message(message)

	script.__name__ = script.__qualname__ = f"script_{name}"
	return script
