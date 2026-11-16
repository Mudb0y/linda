"""wxPython fixture, with the controls NVDA's own dialogs are built from.

Where wxGTK has both a native and a generic control, the one NVDA uses is
chosen: wx.ListCtrl (generic on GTK) for the table, wx.TreeCtrl (generic on
GTK) for the tree, wx.ListBox (native) for the list.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import wx
import wx.adv
from commands import TEXT, Commands, say

W = {}


class Frame(wx.Frame):
	def __init__(self):
		super().__init__(None, title="wx fixture", size=(1000, 950))
		self.pushes = 0
		mb = wx.MenuBar()
		fileMenu = wx.Menu()
		fileMenu.Append(wx.ID_ANY, "New")
		fileMenu.Append(wx.ID_ANY, "Open")
		recent = wx.Menu()
		recent.Append(wx.ID_ANY, "one.txt")
		recent.Append(wx.ID_ANY, "two.txt")
		fileMenu.AppendSubMenu(recent, "Recent")
		fileMenu.AppendSeparator()
		fileMenu.Append(wx.ID_ANY, "Quit")
		viewMenu = wx.Menu()
		viewMenu.AppendCheckItem(wx.ID_ANY, "Show toolbar").Check(True)
		viewMenu.AppendRadioItem(wx.ID_ANY, "Small")
		viewMenu.AppendRadioItem(wx.ID_ANY, "Large")
		mb.Append(fileMenu, "&File")
		mb.Append(viewMenu, "&View")
		self.SetMenuBar(mb)

		tb = self.CreateToolBar(wx.TB_TEXT | wx.TB_NOICONS)
		tb.AddCheckTool(wx.ID_ANY, TEXT["toolbarBold"], wx.NullBitmap)
		tb.AddTool(wx.ID_ANY, TEXT["toolbarItalic"], wx.NullBitmap)
		tb.Realize()
		self.status = self.CreateStatusBar()
		self.status.SetStatusText(TEXT["status"])
		W["status"] = self.status

		p = wx.Panel(self)
		outer = wx.BoxSizer(wx.VERTICAL)
		buttons = wx.BoxSizer(wx.HORIZONTAL)
		push = wx.Button(p, label=TEXT["push"])
		push.SetToolTip(TEXT["pushTooltip"])
		push.Bind(wx.EVT_BUTTON, self.onPush)
		toggle = wx.ToggleButton(p, label=TEXT["toggle"])
		check = wx.CheckBox(p, label=TEXT["check"])
		r1 = wx.RadioButton(p, label=TEXT["radio1"], style=wx.RB_GROUP)
		r2 = wx.RadioButton(p, label=TEXT["radio2"])
		r3 = wx.RadioButton(p, label=TEXT["radio3"])
		for name, w in (
			("push", push),
			("toggle", toggle),
			("check", check),
			("radio1", r1),
			("radio2", r2),
			("radio3", r3),
		):
			buttons.Add(w, 0, wx.ALL, 3)
			W[name] = w
		outer.Add(buttons)

		form = wx.FlexGridSizer(2, 4, 8)

		def labelled(labelText, widget, name):
			form.Add(wx.StaticText(p, label="&" + labelText))
			form.Add(widget)
			W[name] = widget

		labelled(TEXT["entryLabel"], wx.TextCtrl(p, value=TEXT["entry"], size=(250, -1)), "entry")
		labelled(
			TEXT["passwordLabel"], wx.TextCtrl(p, value=TEXT["password"], style=wx.TE_PASSWORD), "password"
		)
		labelled(
			TEXT["multilineLabel"],
			wx.TextCtrl(p, value=TEXT["multiline"], style=wx.TE_MULTILINE, size=(300, 70)),
			"multiline",
		)
		combo = wx.Choice(p, choices=list(TEXT["comboItems"]))
		combo.SetSelection(0)
		labelled(TEXT["comboLabel"], combo, "combo")
		labelled(
			TEXT["comboEntryLabel"],
			wx.ComboBox(p, value=TEXT["comboEntryItems"][0], choices=list(TEXT["comboEntryItems"])),
			"comboEntry",
		)
		labelled(TEXT["spinLabel"], wx.SpinCtrl(p, min=0, max=100, initial=5), "spin")
		labelled(
			TEXT["sliderLabel"], wx.Slider(p, value=50, minValue=0, maxValue=100, size=(200, -1)), "slider"
		)
		gauge = wx.Gauge(p, range=100)
		gauge.SetValue(30)
		labelled(TEXT["progressLabel"], gauge, "progress")
		lst = wx.ListBox(p, choices=list(TEXT["listItems"]), size=(200, 100))
		lst.SetSelection(0)
		self.contextMenu = wx.Menu()
		for t in TEXT["contextItems"]:
			self.contextMenu.Append(wx.ID_ANY, t)
		lst.Bind(wx.EVT_CONTEXT_MENU, lambda _e: lst.PopupMenu(self.contextMenu))
		labelled(TEXT["listLabel"], lst, "list")
		tree = wx.TreeCtrl(p, size=(250, 130), style=wx.TR_DEFAULT_STYLE | wx.TR_HIDE_ROOT)
		root = tree.AddRoot("root")
		for name, _size, kids in TEXT["tree"]:
			it = tree.AppendItem(root, name)
			for cname, _csize in kids:
				tree.AppendItem(it, cname)
		tree.SelectItem(tree.GetFirstChild(root)[0])
		labelled(TEXT["treeLabel"], tree, "tree")
		table = wx.ListCtrl(p, style=wx.LC_REPORT | wx.LC_SINGLE_SEL, size=(300, 110))
		for i, title in enumerate(TEXT["tableColumns"]):
			table.InsertColumn(i, title)
		for r, values in enumerate(TEXT["tableRows"]):
			table.InsertItem(r, values[0])
			for c in (1, 2):
				table.SetItem(r, c, values[c])
		table.Select(0)
		table.Focus(0)
		labelled(TEXT["tableLabel"], table, "table")
		outer.Add(form, 0, wx.ALL, 6)

		nb = wx.Notebook(p)
		for title, contents in zip(TEXT["tabs"], TEXT["tabContents"]):
			page = wx.Panel(nb)
			wx.StaticText(page, label=contents)
			nb.AddPage(page, title)
		outer.Add(nb, 0, wx.EXPAND | wx.ALL, 6)
		W["tabs"] = nb

		more = wx.BoxSizer(wx.HORIZONTAL)
		dialogButton = wx.Button(p, label=TEXT["dialogButton"])
		dialogButton.Bind(wx.EVT_BUTTON, lambda _e: wx.CallAfter(self.openDialog))
		messageButton = wx.Button(p, label=TEXT["messageButton"])
		messageButton.Bind(wx.EVT_BUTTON, lambda _e: wx.CallAfter(self.openMessage))
		link = wx.adv.HyperlinkCtrl(p, label=TEXT["link"], url=TEXT["linkUri"])
		link.Bind(wx.adv.EVT_HYPERLINK, lambda _e: self.status.SetStatusText("Link activated"))
		disabled = wx.Button(p, label=TEXT["disabled"])
		disabled.Disable()
		blinker = wx.Button(p, label=TEXT["blinker"])
		for name, w in (
			("dialogButton", dialogButton),
			("messageButton", messageButton),
			("link", link),
			("disabled", disabled),
			("blinker", blinker),
		):
			more.Add(w, 0, wx.ALL, 3)
			W[name] = w
		outer.Add(more)
		expander = wx.CollapsiblePane(p, label=TEXT["expander"])
		wx.CheckBox(expander.GetPane(), label=TEXT["expanderChild"])
		expander.Bind(wx.EVT_COLLAPSIBLEPANE_CHANGED, lambda _e: p.Layout())
		outer.Add(expander, 0, wx.ALL, 6)
		W["expander"] = expander
		p.SetSizer(outer)

	def onPush(self, _e):
		self.pushes += 1
		self.status.SetStatusText(f"Pushed {self.pushes} times")

	def openDialog(self):
		d = wx.Dialog(self, title=TEXT["dialogTitle"])
		s = wx.BoxSizer(wx.VERTICAL)
		s.Add(wx.StaticText(d, label="&" + TEXT["dialogEntryLabel"]))
		s.Add(wx.TextCtrl(d))
		s.Add(d.CreateStdDialogButtonSizer(wx.OK | wx.CANCEL))
		d.SetSizerAndFit(s)
		d.ShowModal()
		d.Destroy()

	def openMessage(self):
		wx.MessageBox(
			TEXT["messageText"] + "\n\n" + TEXT["messageDetail"],
			TEXT["messageTitle"],
			wx.OK | wx.ICON_INFORMATION,
			self,
		)


def main():
	app = wx.App()
	f = Frame()
	blinkTimer = wx.Timer()
	blinkTimer.Bind(wx.EVT_TIMER, lambda _e: W["blinker"].Show(not W["blinker"].IsShown()))

	def run(line):
		cmd, _, arg = line.partition(" ")
		if cmd == "focus":
			if arg not in W:
				raise KeyError(f"{arg}: wx exposes no focusable widget for it")
			W[arg].SetFocus()
		elif cmd == "timer":
			if arg == "on":
				blinkTimer.Start(1000)
			else:
				blinkTimer.Stop()
				W["blinker"].Show(True)
		elif cmd == "quit":
			wx.CallAfter(f.Destroy)
		else:
			raise ValueError("unknown command")

	commands = Commands(run)
	poll = wx.Timer()
	poll.Bind(wx.EVT_TIMER, lambda _e: commands.poll())
	poll.Start(50)
	f.Show()
	wx.CallAfter(say, "ready")
	app.MainLoop()


if __name__ == "__main__":
	main()
