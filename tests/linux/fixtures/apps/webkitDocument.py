"""A WebKitGTK document, as MATE Help (yelp) shows its pages: a window whose web view's page lives
in WebKitGTK's web process, which serves AT-SPI itself, names its objects with UUIDs and has no
object at /org/a11y/atspi/accessible/root. wxPython's WebView is WebKitGTK's on GTK.

Prints "ready" once the page has loaded, and exits when its input closes.
"""

import sys
import threading

import wx
import wx.html2

TITLE = "WebKit fixture page"
PAGE = f"""<!doctype html>
<html><head><title>{TITLE}</title></head><body>
<h1>Fixture heading</h1>
<p>The first paragraph has <a href="#end">a link</a> in it.</p>
<h2>Second heading</h2>
<ul><li>First item</li><li>Second item</li></ul>
<p id="end">The last paragraph.</p>
</body></html>"""


def main() -> None:
	app = wx.App()
	frame = wx.Frame(None, title="WebKit fixture", size=(600, 400))
	view = wx.html2.WebView.New(frame, backend=wx.html2.WebViewBackendWebKit)
	if view is None:
		print("error no WebKitGTK WebView", flush=True)
		sys.exit(1)
	said = []

	def loaded(evt: wx.html2.WebViewEvent) -> None:
		if not said and view.GetPageText():
			said.append(True)
			print("ready", flush=True)

	view.Bind(wx.html2.EVT_WEBVIEW_LOADED, loaded)
	view.SetPage(PAGE, "")
	frame.Show()

	def waitForInput() -> None:
		sys.stdin.read()
		wx.CallAfter(frame.Close)

	threading.Thread(target=waitForInput, daemon=True).start()
	app.MainLoop()


if __name__ == "__main__":
	main()
