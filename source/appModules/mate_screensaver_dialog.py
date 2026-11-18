"""App module for mate-screensaver-dialog, the unlock prompt MATE's screen saver shows over a locked screen.

The prompt puts a label beside its password field without tying the two together, and tells how the
password check goes by renaming other labels: "Checking...", then "Incorrect password." or nothing,
and a warning while Caps Lock is on. Windows' lock screen speaks its messages, and NVDA's lockapp
module reads them; here the field takes its label's name, and a label near the field is spoken as it
is renamed. The clock above them, renamed every second, is not near the field and stays quiet.

GTK 3 makes a widget's accessible only when something asks for it, and a label without one sends no
event when it is renamed. So once the field has the focus, every label near it is asked for.
"""

import appModuleHandler
import controlTypes
import ui
from NVDAObjects import NVDAObject

#: Levels below a label's parent at which the password field counts as near it.
NEAR_DEPTH = 3
#: The roles a GTK label has as NVDA sees it.
LABEL_ROLES = (controlTypes.Role.STATICTEXT, controlTypes.Role.LABEL)


def _isPasswordField(obj: NVDAObject) -> bool:
	return (
		obj.role in (controlTypes.Role.EDITABLETEXT, controlTypes.Role.PASSWORDEDIT)
		and controlTypes.State.PROTECTED in obj.states
	)


def _hasPasswordFieldWithin(obj: NVDAObject, depth: int) -> bool:
	for child in obj.children:
		if _isPasswordField(child) or (depth > 1 and _hasPasswordFieldWithin(child, depth - 1)):
			return True
	return False


def _touchWithin(obj: NVDAObject, depth: int) -> None:
	for child in obj.children:
		if depth > 1:
			_touchWithin(child, depth - 1)


class PasswordField(NVDAObject):
	def _get_name(self) -> str:
		name = super().name
		if name:
			return name
		label = self.previous
		if label is not None and label.role in LABEL_ROLES and label.name:
			return label.name
		return name


class AppModule(appModuleHandler.AppModule):
	#: The messages are labels, which are never the focus.
	backgroundEvents = frozenset({"nameChange"})

	def chooseNVDAObjectOverlayClasses(self, obj, clsList):
		if _isPasswordField(obj):
			clsList.insert(0, PasswordField)

	def event_gainFocus(self, obj, nextHandler):
		nextHandler()
		if not isinstance(obj, PasswordField):
			return
		# The labels whose parent the field is NEAR_DEPTH levels below share an ancestor that many
		# levels up; each of that ancestor's descendants that deep is asked for.
		ancestor = obj
		for _level in range(NEAR_DEPTH):
			if ancestor.parent is None:
				break
			ancestor = ancestor.parent
		_touchWithin(ancestor, NEAR_DEPTH + 1)

	def event_nameChange(self, obj, nextHandler):
		if obj.role in LABEL_ROLES and obj.name and obj.name.strip():
			parent = obj.parent
			if parent is not None and _hasPasswordFieldWithin(parent, NEAR_DEPTH):
				ui.message(obj.name)
				return
		nextHandler()
