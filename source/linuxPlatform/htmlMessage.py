"""The control gui.message.HtmlMessageDialog shows its message in on Linux.

NVDA asks wx for Internet Explorer's WebView, which wxGTK does not have, so wx.html2.WebView.New gave
None and every browseable message failed as it opened. The message is shown as the text of its HTML
in a read-only multi-line field, which is always read.

wx here has WebKitGTK's WebView, but it is not used by default: focus given to it stays on the
wxGTK widget around it, and WebKitGTK's web process says nothing of its document gaining focus, so
NVDA never reached the document and the message was silent but for its title. LINDA_HTML_MESSAGE=webkit
uses it anyway, for work on that.
"""

import os
import re
from html.parser import HTMLParser

import wx

#: HtmlMessageDialog._ACTION_URL_PREFIX.
ACTION_URL_PREFIX = "nvda-action://"

#: Elements whose start and end break a line, as a browser lays them out.
_BLOCKS = frozenset(
	{
		"address",
		"article",
		"aside",
		"blockquote",
		"br",
		"dd",
		"div",
		"dl",
		"dt",
		"fieldset",
		"figure",
		"footer",
		"form",
		"h1",
		"h2",
		"h3",
		"h4",
		"h5",
		"h6",
		"header",
		"hr",
		"li",
		"main",
		"nav",
		"ol",
		"p",
		"pre",
		"section",
		"table",
		"tr",
		"ul",
	},
)
#: Elements whose content is never shown.
_HIDDEN = frozenset({"head", "script", "style", "template", "title"})


class _TextOf(HTMLParser):
	def __init__(self) -> None:
		super().__init__(convert_charrefs=True)
		self.parts: list[str] = []
		#: (label, action) of each nvda-action:// link, in order.
		self.actions: list[tuple[str, str]] = []
		self._hidden = 0
		self._pre = 0
		#: The action of the link being read and the parts its text starts at.
		self._link: tuple[str, int] | None = None

	def _breakLine(self) -> None:
		if self.parts and not self.parts[-1].endswith("\n"):
			self.parts.append("\n")

	def handle_starttag(self, tag: str, attrs) -> None:
		if tag in _HIDDEN:
			self._hidden += 1
		elif tag in _BLOCKS:
			self._breakLine()
			if tag == "li":
				self.parts.append("• ")
		if tag == "a":
			href = dict(attrs).get("href") or ""
			if href.lower().startswith(ACTION_URL_PREFIX):
				self._link = (href[len(ACTION_URL_PREFIX) :], len(self.parts))
		if tag == "pre":
			self._pre += 1
		elif tag in ("td", "th") and self.parts and not self.parts[-1].endswith(("\n", "\t")):
			self.parts.append("\t")

	def handle_endtag(self, tag: str) -> None:
		if tag == "a" and self._link is not None:
			action, start = self._link
			self._link = None
			label = " ".join("".join(self.parts[start:]).split()) or action
			self.actions.append((label, action))
		if tag in _HIDDEN:
			self._hidden = max(0, self._hidden - 1)
		elif tag in _BLOCKS:
			self._breakLine()
		if tag == "pre":
			self._pre = max(0, self._pre - 1)

	def handle_startendtag(self, tag: str, attrs) -> None:
		if tag in _BLOCKS:
			self._breakLine()

	def handle_data(self, data: str) -> None:
		if self._hidden:
			return
		if not self._pre:
			data = re.sub(r"\s+", " ", data)
			if data.startswith(" ") and (not self.parts or self.parts[-1].endswith(("\n", " ", "\t"))):
				data = data[1:]
		if data:
			self.parts.append(data)


def textOf(html: str) -> tuple[str, list[tuple[str, str]]]:
	"""The text of html as it reads, one line per block, with no markup and nothing from its head; and
	the label and action of each of its nvda-action:// links."""
	parser = _TextOf()
	parser.feed(html)
	parser.close()
	# A plain-text message comes as one pre, whose indentation is kept.
	lines = [line.rstrip() for line in "".join(parser.parts).split("\n")]
	return "\n".join(line for line in lines if line), parser.actions


class TextMessageControl(wx.TextCtrl):
	"""A read-only multi-line field standing in for the WebView, with the two methods of it that
	HtmlMessageDialog and ui.browseableMessage call."""

	def __init__(self, parent: wx.Window) -> None:
		super().__init__(parent, style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_DONTWRAP)
		#: (label, action) of each nvda-action:// link of the message.
		self.actions: list[tuple[str, str]] = []

	def SetPage(self, html: str, baseUrl: str) -> None:
		text, self.actions = textOf(html)
		self.SetValue(text)
		self.SetInsertionPoint(0)

	def GetPageText(self) -> str:
		return self.GetValue()


def addActionButtons(dialog) -> None:
	"""A button for each nvda-action:// link of the message the field shows, which a field cannot
	follow, running what the link would. Copy is left out: ui.browseableMessage gives it a button of
	its own when it offers it."""
	control = dialog._messageControl
	if not isinstance(control, TextMessageControl):
		return
	for label, action in control.actions:
		if action == "copy":
			continue
		dialog.addButton(
			wx.ID_ANY,
			label=label,
			callback=lambda payload, action=action: _runAction(dialog, action),
			closesDialog=False,
		)


def _runAction(dialog, action: str) -> None:
	"""What HtmlMessageDialog._onNavigating does on reaching nvda-action://action."""
	if action == "close":
		dialog.Close()
	elif handler := dialog._actionHandlers.get(action):
		handler()


def createControl(parent: wx.Window) -> wx.Window:
	"""The field in parent; with LINDA_HTML_MESSAGE=webkit, a WebKitGTK WebView where wx can make one."""
	if os.environ.get("LINDA_HTML_MESSAGE") == "webkit":
		import wx.html2

		if wx.html2.WebView.IsBackendAvailable(wx.html2.WebViewBackendWebKit):
			control = wx.html2.WebView.New(parent, backend=wx.html2.WebViewBackendWebKit)
			if control is not None:
				return control
		from logHandler import log

		log.debugWarning("No WebKitGTK WebView; the message is shown as text")
	return TextMessageControl(parent)
