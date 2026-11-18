"""NVDA's add-on store on Linux: off until the user turns it on, and named as NVDA's.

The store at addonStore.nvaccess.org holds add-ons written for NVDA on Windows. Many of them do not
work on Linux, and some keep Linda from starting, so Linda asks the store for nothing, not even its
list, until the user has turned it on in its settings category and confirmed a warning. Without the
store the same window still manages the installed add-ons and installs one from a file, and is
called Add-ons; with it, it is the NVDA Add-on Store, a name that keeps NVDA because the add-ons are
NVDA's (linuxPlatform.branding leaves the word alone in that name).
"""

import config
import wx

from . import branding

#: The store's name, NVDA's translated "Add-on Store" put after NVDA. Listed in
#: branding.UPSTREAM_NAMED_MESSAGES, which keeps the word NVDA in it in every language.
STORE_NAME = "NVDA {addonStore}"


def isEnabled() -> bool:
	return config.conf["addonStore"]["enabled"]


def storeName() -> str:
	return pgettext(
		"addonStore",
		# Translators: The name of NVDA's add-on store where a program other than NVDA uses it.
		# {addonStore} is replaced with the translation of "Add-on Store". NVDA stays NVDA.
		STORE_NAME,
	).format(addonStore=pgettext("addonStore", "Add-on Store"))


def windowName() -> str:
	"""The add-ons window's name: the store's while it is on, and Add-ons while it is off."""
	if isEnabled():
		return storeName()
	return pgettext("addonStore", "Add-ons")


def menuLabel() -> str:
	return f"&{windowName()}..."


def warningText() -> str:
	return pgettext(
		"addonStore",
		# Translators: Shown before NVDA's add-on store is turned on where a program other than NVDA uses
		# it. {store} is the store's name, {nvda} is NVDA, {name} is the program's name and {command}
		# the command that starts it without add-ons.
		"The {store} holds add-ons written for {nvda} on Windows. Many of them do not work on Linux, "
		"and some can make {name} misbehave or stop it from starting. "
		"An add-on that says it supports Linux can be expected to work.\n"
		"If {name} stops working after you install an add-on, start it with the command {command} "
		"and remove that add-on.\n"
		"Turn on the {store}?",
	).format(
		store=storeName(),
		nvda=branding.UPSTREAM_NAME,
		name=branding.NAME,
		command=f"{branding.NAME.lower()} --disable-addons",
	)


def confirmTurningOn(parent: wx.Window) -> bool:
	"""Warn that the store's add-ons are written for Windows; True once the user has agreed."""
	import gui

	answer = gui.messageBox(
		warningText(),
		storeName(),
		wx.YES_NO | wx.NO_DEFAULT | wx.ICON_WARNING,
		parent,
	)
	return answer == wx.YES


class StoreSwitch:
	"""The check box in the store's settings category that turns the store on, and the controls
	of the category that apply only while it is on."""

	def __init__(self, panel: wx.Window, sHelper) -> None:
		self._panel = panel
		self._governed: tuple[wx.Window, ...] = ()
		self.checkBox: wx.CheckBox = sHelper.addItem(
			wx.CheckBox(
				panel,
				# Translators: The label of the check box that turns NVDA's add-on store on, in its
				# settings category. {store} is the store's name.
				label=pgettext("addonStore", "&Enable the {store}").format(store=storeName()),
			),
		)
		self.checkBox.SetValue(isEnabled())
		self.checkBox.Bind(wx.EVT_CHECKBOX, self._onChange)

	def governs(self, *controls: wx.Window) -> None:
		self._governed = controls
		self._enableGoverned()

	def _enableGoverned(self) -> None:
		for control in self._governed:
			control.Enable(self.checkBox.IsChecked())

	def _onChange(self, evt: wx.CommandEvent) -> None:
		if self.checkBox.IsChecked() and not isEnabled() and not confirmTurningOn(self._panel):
			self.checkBox.SetValue(False)
		self._enableGoverned()

	def save(self) -> None:
		enabled = self.checkBox.IsChecked()
		if enabled != isEnabled():
			config.conf["addonStore"]["enabled"] = enabled
			relabelMenuItem()


def relabelMenuItem() -> None:
	"""Give the Tools menu's item the window's name after the store was turned on or off."""
	import gui

	item = getattr(getattr(gui.mainFrame, "sysTrayIcon", None), "menu_tools_addonStore", None)
	if item is not None:
		item.SetItemLabel(menuLabel())
