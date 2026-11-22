"""App module for GNOME Shell, which draws the desktop's own interfaces in a window of its own named
"Main stage": the Alt+Tab switcher, the overview, the run dialog, notifications, and its prompts for a
password, polkit's authentication dialog, the keyring's and the lock screen.

The stage is active whenever the shell has the keyboard and names nothing the user chose, so it is
neither said as it takes the focus nor as an ancestor of what has it. Neither is an unnamed panel the
shell gives the keyboard to, as its switcher's popup takes it.

The switcher never moves the keyboard among its items: the one Tab chose says it is selected, with an
empty name and the window or program it stands for in a label inside it. Speaking that text as each
item is selected gives what NVDA says for the Windows task switcher, and what Orca says for this one.
The window switcher's items are icons with nothing to say; the title of the one selected is put in a
label beneath them, which is spoken as it is renamed.

Only the shell's controls say whether they are sensitive. A panel or label that does not say so is not
unavailable: taken for it, a prompt's panels and the labels in them would be left out of the text NVDA
reads as the prompt opens, and the overview would be said to be unavailable.

A prompt tells how the check of a password went by renaming a label beside its field, "Sorry, that
didn't work. Please try again.", as MATE's unlock prompt does, and that label is spoken as it is renamed.
"""

import time

import api
import appModuleHandler
import controlTypes
import speech
import ui
from NVDAObjects import NVDAObject
from NVDAObjects.AtSpi import DesktopNotification, _descendantText

from .mate_screensaver_dialog import LABEL_ROLES, _hasPasswordFieldWithin

#: Roles of the switcher's items as the shell names them.
ITEM_ROLES = frozenset({controlTypes.Role.BUTTON, controlTypes.Role.LISTITEM})
#: The roles of the shell's objects that say whether they are sensitive.
CONTROL_ROLES = frozenset(
	{
		controlTypes.Role.BUTTON,
		controlTypes.Role.TOGGLEBUTTON,
		controlTypes.Role.CHECKBOX,
		controlTypes.Role.RADIOBUTTON,
		controlTypes.Role.MENUITEM,
		controlTypes.Role.CHECKMENUITEM,
		controlTypes.Role.RADIOMENUITEM,
		controlTypes.Role.MENU,
		controlTypes.Role.EDITABLETEXT,
		controlTypes.Role.PASSWORDEDIT,
		controlTypes.Role.LINK,
		controlTypes.Role.LISTITEM,
		controlTypes.Role.COMBOBOX,
		controlTypes.Role.SLIDER,
		controlTypes.Role.SPINBUTTON,
		controlTypes.Role.TAB,
	},
)
#: Seconds after an item with nothing to say for itself is selected within which the label the shell
#: renames is the item's name: the window switcher's items are icons, and the title of the one selected
#: is put in a label beneath them as it is selected.
CHOICE_LABEL_SECONDS = 0.5
#: Levels above a switcher's item at which the popup holding it, which has the keyboard, is looked for.
POPUP_LEVELS = 6
#: Levels above a renamed label, and below that ancestor, at which a password field counts as beside it:
#: the field and the label are each in a panel of their own, side by side.
BESIDE_LEVELS = 2


def _isStage(obj: NVDAObject) -> bool:
	return obj.role == controlTypes.Role.WINDOW and getattr(obj, "atspiParentRole", None) == "application"


class ShellObject(NVDAObject):
	def _get_states(self) -> set[controlTypes.State]:
		states = super().states
		if self.role not in CONTROL_ROLES:
			states.discard(controlTypes.State.UNAVAILABLE)
		return states


class Stage(ShellObject):
	isPresentableFocusAncestor = False


class UnnamedPanel(ShellObject):
	isPresentableFocusAncestor = False


class NamedByContent(ShellObject):
	def _get_name(self) -> str:
		return super().name or _descendantText(self.node)


def _inFocusedPopup(item: NVDAObject) -> bool:
	"""Whether item lies in a popup that has the keyboard, as the switcher's items lie in the switcher,
	which takes it as it shows. The calendar's day buttons, which the shell selects as it builds its
	calendar for the lock screen, do not."""
	ancestor = item
	for _level in range(POPUP_LEVELS):
		ancestor = ancestor.parent
		if ancestor is None:
			return False
		if controlTypes.State.FOCUSED in ancestor.states:
			return True
	return False


def _besidePasswordField(label: NVDAObject) -> bool:
	ancestor = label
	for _level in range(BESIDE_LEVELS):
		ancestor = ancestor.parent
		if ancestor is None:
			return False
		if _hasPasswordFieldWithin(ancestor, BESIDE_LEVELS):
			return True
	return False


class AppModule(appModuleHandler.AppModule):
	def chooseNVDAObjectOverlayClasses(self, obj, clsList):
		role = obj.role
		if _isStage(obj):
			clsList.insert(0, Stage)
		elif role == controlTypes.Role.PANEL and not obj.atspiName:
			clsList.insert(0, UnnamedPanel)
		elif role in ITEM_ROLES and not obj.atspiName:
			clsList.insert(0, NamedByContent)
		elif role == controlTypes.Role.ALERT:
			# The shell's notification is an unnamed object inside the stage, whose text is in its labels.
			clsList.insert(0, DesktopNotification)
		else:
			clsList.insert(0, ShellObject)

	def event_gainFocus(self, obj, nextHandler):
		if isinstance(obj, (Stage, UnnamedPanel)):
			return
		nextHandler()

	#: When an item with nothing to say for itself was last selected, whose name the label renamed next
	#: gives.
	_unnamedChoiceAt = 0.0

	def event_stateChange(self, obj, nextHandler):
		if (
			obj.role in ITEM_ROLES
			and controlTypes.State.SELECTED in obj.states
			and obj is not api.getFocusObject()
			and _inFocusedPopup(obj)
		):
			name = obj.name
			if name:
				_sayChoice(name)
				return
			self._unnamedChoiceAt = time.monotonic()
		nextHandler()

	def event_nameChange(self, obj, nextHandler):
		if obj.role in LABEL_ROLES and obj.name and obj.name.strip():
			if time.monotonic() - self._unnamedChoiceAt < CHOICE_LABEL_SECONDS:
				self._unnamedChoiceAt = 0.0
				_sayChoice(obj.name)
				return
			if _besidePasswordField(obj):
				ui.message(obj.name)
				return
		nextHandler()


def _sayChoice(name: str) -> None:
	# A newer choice makes the one still being spoken worthless.
	speech.cancelSpeech()
	ui.message(name)
