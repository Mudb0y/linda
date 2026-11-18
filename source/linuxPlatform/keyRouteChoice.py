"""The choice, in the Advanced settings panel, of where keys come from: linda-keyd, the helper that
takes the keyboards in the kernel, or the X display; see linuxPlatform.keys.route."""

import config
import wx

#: The configuration's routes, in the order the choice lists them.
ROUTES = ("auto", "kernel", "x11")


def _labels() -> list[str]:
	return [
		# Translators: A choice on Linux in the Advanced settings panel, of where NVDA takes keys from:
		# linda-keyd, the program that takes the keyboard in the kernel, when it runs and lets NVDA, and
		# the X display otherwise.
		_("linda-keyd when it lets NVDA, otherwise the X display"),
		# Translators: A choice on Linux in the Advanced settings panel, of where NVDA takes keys from:
		# linda-keyd alone, the program that takes the keyboard in the kernel; NVDA reads no keys while it
		# does not run or does not let NVDA.
		_("linda-keyd alone"),
		# Translators: A choice on Linux in the Advanced settings panel, of where NVDA takes keys from:
		# the X display alone, never linda-keyd.
		_("the X display alone"),
	]


class KeyRouteChoice:
	def __init__(self, panel: wx.Window, sHelper) -> None:
		self.choice: wx.Choice = sHelper.addLabeledControl(
			# Translators: The label on Linux in the Advanced settings panel of the choice of where NVDA
			# takes keys from.
			_("Take &keys from:"),
			wx.Choice,
			choices=_labels(),
		)
		self.choice.SetSelection(ROUTES.index(config.conf["keyboard"]["keyRoute"]))
		self._default = ROUTES.index(config.conf.getConfigValidation(["keyboard", "keyRoute"]).default)

	def isDefault(self) -> bool:
		return self.choice.GetSelection() == self._default

	def restoreToDefault(self) -> None:
		self.choice.SetSelection(self._default)

	def save(self) -> None:
		config.conf["keyboard"]["keyRoute"] = ROUTES[self.choice.GetSelection()]
