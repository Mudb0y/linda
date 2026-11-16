"""The stdin protocol every fixture speaks, so one test drives them all.

One command per line: "focus NAME" moves keyboard focus to the control
registered under NAME, "timer on" and "timer off" start and stop the control
that hides and shows itself, "quit" exits. A fixture prints "ready" once its
main window is shown and "ok COMMAND" or "error COMMAND: why" for each
command. The fixture polls stdin from its own main loop's timer, so a
command runs on the toolkit's thread like any user action.
"""

import os
import sys

# The controls every fixture has, under these names. The tests
# refer to them.
NAMES = (
	"push",
	"toggle",
	"check",
	"radio1",
	"radio2",
	"radio3",
	"entry",
	"password",
	"multiline",
	"combo",
	"comboEntry",
	"spin",
	"slider",
	"progress",
	"list",
	"tree",
	"table",
	"tabs",
	"toolbarBold",
	"toolbarItalic",
	"status",
	"dialogButton",
	"messageButton",
	"link",
	"expander",
	"disabled",
	"blinker",
)

# Labels shared by every fixture, so that names can be compared across
# toolkits.
TEXT = {
	"push": "Push me",
	"pushTooltip": "Pushes the button",
	"toggle": "Toggle me",
	"check": "Check me",
	"radio1": "Radio one",
	"radio2": "Radio two",
	"radio3": "Radio three",
	"entryLabel": "Name:",
	"entry": "hello world",
	"passwordLabel": "Password:",
	"password": "secret",
	"multilineLabel": "Notes:",
	"multiline": "first line\nsecond line\nthird line",
	"comboLabel": "Colour:",
	"comboItems": ("Red", "Green", "Blue"),
	"comboEntryLabel": "Fruit:",
	"comboEntryItems": ("Apple", "Banana", "Cherry"),
	"spinLabel": "Count:",
	"sliderLabel": "Volume:",
	"progressLabel": "Progress:",
	"listLabel": "Items:",
	"listItems": tuple(f"Item {i}" for i in range(1, 6)),
	"treeLabel": "Files:",
	"treeColumns": ("Name", "Size"),
	"tree": (
		("Folder A", "2 items", (("File A1", "1 KB"), ("File A2", "2 KB"))),
		("Folder B", "1 item", (("File B1", "3 KB"),)),
		("File C", "4 KB", ()),
	),
	"tableLabel": "Grid:",
	"tableColumns": ("Col 1", "Col 2", "Col 3"),
	"tableRows": tuple(tuple(f"r{r}c{c}" for c in range(1, 4)) for r in range(1, 4)),
	"tabs": ("Page one", "Page two"),
	"tabContents": ("Contents of page one", "Contents of page two"),
	"status": "Ready",
	"dialogButton": "Open dialog...",
	"dialogTitle": "Settings",
	"dialogEntryLabel": "Server:",
	"messageButton": "Show message...",
	"messageTitle": "Notice",
	"messageText": "Something happened",
	"messageDetail": "Details of what happened",
	"link": "Visit example",
	"linkUri": "https://example.org/",
	"expander": "More options",
	"expanderChild": "Extra",
	"disabled": "Disabled",
	"blinker": "Blinker",
	"contextItems": ("Cut", "Copy", "Paste"),
	"toolbarBold": "Bold",
	"toolbarItalic": "Italic",
}


class Commands:
	"""Reads commands from stdin without blocking and hands them to run."""

	def __init__(self, run):
		self._run = run
		self._buf = b""
		os.set_blocking(0, False)

	def poll(self):
		"""Call from a timer on the main loop; returns True to keep it."""
		try:
			chunk = os.read(0, 4096)
		except BlockingIOError:
			return True
		if chunk == b"":
			self._run("quit")
			return False
		self._buf += chunk
		while b"\n" in self._buf:
			line, self._buf = self._buf.split(b"\n", 1)
			line = line.decode().strip()
			if not line:
				continue
			try:
				self._run(line)
				say(f"ok {line}")
			except Exception as e:  # noqa: BLE001 - reported to the test
				say(f"error {line}: {e!r}")
		return True


def say(line):
	sys.stdout.write(line + "\n")
	sys.stdout.flush()
