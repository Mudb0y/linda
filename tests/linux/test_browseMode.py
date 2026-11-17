"""Browse mode in Firefox: NVDA reads a web page through its own virtual buffer, built by the engine's
renderer into NVDA's buffer storage, with every key pressed on the rig's display.

BASEDREADER_VBUF_LIB=ROOT/engine/target/vbuf/libnvdaVbuf.so \
BASEDREADER_VBUF_ENGINE=ROOT/engine/target/release/libbre_vbuf.so \
BASEDREADER_RIG_WM=marco nix develop . -c tests/linux/rig python3 -m pytest -q tests/linux/test_browseMode.py

The two libraries are built with:

	nix develop . -c bash engine/vbuf/build.sh nvdaHelper engine/target/vbuf/libnvdaVbuf.so
	cd engine && nix develop .. -c cargo build --release --offline -p basedreader-vbufengine
"""

import json
import time
from collections.abc import Iterator
from pathlib import Path

import harness
import pytest
import webSession
from desktopSession import DesktopSession
from harness import Launch

#: Seconds from NVDA's start to the buffer being built and its say all spoken.
BUFFER_BOUND = 60.0
#: Seconds for anything a key starts to settle before the next key.
SETTLE = 1.0
#: Seconds a followed link has to bring its page.
PAGE_BOUND = 15.0
#: Whether the buffer gets the page's changes by itself; without the update thread a test re-renders it.
UPDATES_ARRIVE = (harness.ROOT / "source" / "linuxPlatform" / "vbufUpdates.py").is_file()
#: The test hook's key, which does what the hook's command file says to the focused buffer.
HOOK_KEY = "Insert+control+shift+F9"
#: The hook's command file, beside the plugin.
HOOK_COMMAND = "browseHook.txt"

#: A global plugin for the test hooks, standing in for the update thread, which hands the buffer each
#: change and document event the engine saw. The command file holds one of:
#: rerender: render the document again and apply it as an update;
#: partial: mark the buffer as holding only the first part of its document;
#: events NAME...: hand the buffer those document events.
HOOK_PLUGIN = """
import os
import threading

import api
import globalPluginHandler
import queueHandler

COMMAND = os.path.join(os.path.dirname(__file__), "browseHook.txt")


def _rerender(buffer):
	from linuxPlatform import vbufEngine, vbufLib

	rendered = vbufEngine.render(buffer.busName, buffer.docPath, buffer.rootDocHandle)
	try:
		vbufLib.replaceSubtree(buffer.VBufHandle.value, buffer.rootDocHandle, buffer.rootID, rendered.stream)
	finally:
		rendered.stream.free()
	queueHandler.queueFunction(queueHandler.eventQueue, buffer.onEngineEvents, (), rendered.info)


class GlobalPlugin(globalPluginHandler.GlobalPlugin):
	def script_browseHook(self, gesture):
		buffer = api.getFocusObject().treeInterceptor
		if buffer is None or not buffer.VBufHandle:
			return
		with open(COMMAND) as f:
			command, *arguments = f.read().split()
		if command == "rerender":
			threading.Thread(target=_rerender, args=(buffer,), daemon=True).start()
		elif command == "partial":
			buffer.complete = False
		elif command == "events":
			buffer.onEngineEvents(tuple(arguments), {})

	__gestures = {"kb:NVDA+control+shift+f9": "browseHook"}
"""


@pytest.fixture
def desktop(tmp_path: Path) -> Iterator[DesktopSession]:
	with DesktopSession(tmp_path / "desktop") as session:
		yield session


def hooksDirectory(directory: Path) -> Path:
	"""A test hooks directory with the transcript synth and the hook plugin."""
	hooks = directory / "browseHooks"
	(hooks / "globalPlugins").mkdir(parents=True)
	(hooks / "synthDrivers").symlink_to(harness.HERE / "synthDrivers")
	(hooks / "globalPlugins" / "browseHook.py").write_text(HOOK_PLUGIN)
	return hooks


def hook(desktop: DesktopSession, command: str, *expected: str) -> None:
	"""Have the hook plugin carry out command on the focused buffer."""
	(desktop.directory / "browseHooks" / "globalPlugins" / HOOK_COMMAND).write_text(command)
	step(desktop, HOOK_KEY, *expected)


def expect(desktop: DesktopSession, *texts: str, since: int, timeout: float = 5) -> None:
	"""desktop.expect, read again when it met a transcript line the synth was still writing."""
	for _ in range(50):
		try:
			return desktop.expect(*texts, since=since, timeout=timeout)
		except json.JSONDecodeError:
			time.sleep(0.01)
	return desktop.expect(*texts, since=since, timeout=timeout)


def step(desktop: DesktopSession, chord: str, *expected: str, timeout: float = 5) -> None:
	mark = desktop.mark()
	desktop.press(chord)
	expect(desktop, *expected, since=mark, timeout=timeout)
	time.sleep(SETTLE)


def browse(desktop: DesktopSession, tmp_path: Path, page: str, title: str, *sayAll: str) -> Launch:
	"""Firefox on page, then NVDA, which builds the page's buffer and reads it from the top: the page's
	title, then the records sayAll names, the last of them the page's last line."""
	webSession.requireBrowseLibraries()
	webSession.startFirefox(desktop, page)
	webSession.waitForDocument(title)
	nvda = desktop.startNVDA(
		webSession.BROWSE_CONFIG,
		env={"BASEDREADER_TEST_HOOKS_DIR": str(hooksDirectory(desktop.directory))},
	)
	nvda.waitForLog("Buffer rendered", BUFFER_BOUND)
	expect(desktop, title, *sayAll, since=0, timeout=BUFFER_BOUND)
	time.sleep(SETTLE)
	return nvda


def quit(desktop: DesktopSession, nvda: Launch) -> None:
	step(desktop, "Insert+q", "Exit NVDA…")
	desktop.press("Return")
	nvda.waitForExit(10)
	assert nvda.process.returncode == 0, nvda.describe()
	nvda.assertClean(logNames=("nvda.log",))


def test_loadSaysAllThenReadsByLineCharacterAndWord(desktop: DesktopSession, tmp_path: Path):
	nvda = browse(
		desktop,
		tmp_path,
		"basic.html",
		"Basic page",
		"heading level 1 Basic page",
		"The first paragraph has link a link to the contact section on this page.",
		"content info landmark heading level 2 Contact",
		"Write to us.",
	)
	step(desktop, "control+Home", "heading level 1 Basic page")
	step(desktop, "Down", "The first paragraph has link a link to the contact section on this page.")
	step(desktop, "Up", "heading level 1 Basic page")
	step(desktop, "h", "main landmark Text heading level 2")
	step(desktop, "Down", "An emoji  grinning face  sits in this sentence.")
	step(desktop, "Right", "n")
	step(desktop, "control+Right", "emoji")
	# The emoji is two UTF-16 code units in the buffer, and one word.
	step(desktop, "control+Right", "grinning face")
	step(desktop, "control+Right", "sits")
	step(desktop, "control+Left", "grinning face")
	quit(desktop, nvda)


def test_quickNavigationAndRefresh(desktop: DesktopSession, tmp_path: Path):
	nvda = browse(desktop, tmp_path, "basic.html", "Basic page", "heading level 1 Basic page", "Write to us.")
	step(desktop, "control+Home", "heading level 1 Basic page")
	step(desktop, "h", "main landmark Text heading level 2")
	step(desktop, "shift+h", "Basic page heading level 1")
	step(desktop, "2", "main landmark Text heading level 2")
	step(desktop, "control+Home", "heading level 1 Basic page")
	step(desktop, "k", "a link to the contact section link")
	step(desktop, "k", "Pages navigation landmark list with 2 items The second page link")
	step(desktop, "shift+k", "a link to the contact section link")
	step(desktop, "u", "Pages navigation landmark list with 2 items The second page link")
	step(desktop, "v", "This page again visited link")
	step(desktop, "d", "main landmark…")
	step(desktop, "q", "block quote…")
	step(desktop, "s", "separator")
	step(desktop, "l", "list with 2 items bullet  First item")
	step(desktop, "i", "bullet  Second item")
	# The layout table before it is passed over.
	step(desktop, "t", "Prices table with 3 rows and 2 columns…")
	step(desktop, "e", "Your name edit has auto complete")
	step(desktop, "f", "Subscribe check box not checked")
	step(desktop, "r", "Size grouping Small radio button checked")
	step(desktop, "c", "Colour combo box collapsed Red")
	step(desktop, "shift+x", "Subscribe check box not checked")
	step(desktop, "b", "Press me button")
	step(desktop, "g", "A small square graphic")
	step(desktop, "3", "Frame and application heading level 3")
	step(desktop, "m", "Inner frame frame Text inside the frame.")
	# An application's content is its own, as Gecko's buffer leaves it on Windows.
	step(desktop, "o", "Embedded application application")
	step(desktop, "h", "content info landmark Contact heading level 2")
	step(desktop, "h", "no next heading")
	step(desktop, "Insert+F5", "Refreshed")
	# While the rest of a page is still to come, a failed search says so, until the rest arrives.
	hook(desktop, "partial")
	desktop.press("control+End")
	time.sleep(SETTLE)
	step(desktop, "h", "Loading")
	hook(desktop, "events complete")
	step(desktop, "h", "no next heading")
	# A document Firefox replaced loses its buffer, and NVDA+Space makes a new one.
	hook(desktop, "events gone")
	step(desktop, "Insert+space", "Browse mode")
	step(desktop, "control+Home", "heading level 1 Basic page")
	quit(desktop, nvda)


def test_followingLinks(desktop: DesktopSession, tmp_path: Path):
	nvda = browse(desktop, tmp_path, "usable.html", "Usable page", "heading level 1 Welcome", "Write to us.")
	step(desktop, "control+Home", "heading level 1 Welcome")
	step(desktop, "k", "the news link")
	# Firefox moves its caret to the target, and the buffer follows.
	step(desktop, "Return", "heading level 2 News")
	step(desktop, "control+Home", "heading level 1 Welcome")
	# Firefox marks a link to a place on the page visited once it is followed.
	step(desktop, "k", "the news visited link")
	step(desktop, "k", "the second page link")
	step(
		desktop,
		"Return",
		"Second page",
		"heading level 1 Second page",
		"You followed the link.",
		timeout=PAGE_BOUND,
	)
	# Firefox gives the focus back to the link followed, and reading starts there.
	step(desktop, "alt+Left", "Usable page", "visited link the second page…", timeout=PAGE_BOUND)
	quit(desktop, nvda)


def test_focusMode(desktop: DesktopSession, tmp_path: Path):
	nvda = browse(desktop, tmp_path, "usable.html", "Usable page", "heading level 1 Welcome", "Write to us.")
	step(desktop, "control+Home", "heading level 1 Welcome")
	step(desktop, "e", "Your name edit has auto complete")
	step(desktop, "Return", "Focus mode")
	mark = desktop.mark()
	desktop.typeText("Bob")
	expect(desktop, "B", "o", "b", since=mark)
	time.sleep(SETTLE)
	step(desktop, "Escape", "Browse mode")
	step(desktop, "Insert+space", "Focus mode")
	step(desktop, "Insert+space", "Browse mode")
	quit(desktop, nvda)


def test_find(desktop: DesktopSession, tmp_path: Path):
	nvda = browse(desktop, tmp_path, "usable.html", "Usable page", "heading level 1 Welcome", "Write to us.")
	step(desktop, "control+Home", "heading level 1 Welcome")
	step(desktop, "Insert+control+f", "Find dialog", "Type the text you wish to find…")
	mark = desktop.mark()
	desktop.typeText("Contact")
	expect(desktop, "C", "o", "n", "t", "a", "c", "t", since=mark)
	# Return pressed while the dialog still echoes the last letter can leave its modal loop running.
	time.sleep(SETTLE)
	mark = desktop.mark()
	desktop.press("Return")
	expect(desktop, "heading level 2 Contact", since=mark, timeout=PAGE_BOUND)
	time.sleep(SETTLE)
	step(desktop, "Down", "Write to us.")
	step(desktop, "control+Home", "heading level 1 Welcome")
	step(desktop, "Insert+F3", "heading level 2 Contact")
	# NVDA's exit dialog does not take the keyboard from Firefox once NVDA's find dialog has been
	# used, so NVDA is asked to exit as the session's end asks it. The find dialog also makes GTK
	# complain on standard error that it was made modal without a window.
	nvda.process.terminate()
	nvda.waitForExit(10)
	assert nvda.process.returncode == 0, nvda.describe()
	nvda.assertClean(ignoring={"stderr"}, logNames=("nvda.log",))


def startFirst(desktop: DesktopSession) -> Launch:
	"""NVDA, started before any Firefox, as on a desktop where it is the screen reader."""
	webSession.requireBrowseLibraries()
	nvda = desktop.startNVDA(webSession.BROWSE_CONFIG)
	nvda.waitForLog("NVDA initialized", BUFFER_BOUND)
	return nvda


def test_pageStillLoadingIsReadWhole(desktop: DesktopSession):
	# Firefox focuses the page while it holds only its first line; reading waits for the rest.
	nvda = startFirst(desktop)
	with webSession.servePages() as pages:
		webSession.startFirefox(desktop, pages.url("realSlow.html"))
		expect(
			desktop,
			"Slow page…",
			"link Skip to the content",
			"main landmark heading level 1 Arrived late",
			"The last paragraph of the slow page.",
			since=0,
			timeout=BUFFER_BOUND,
		)
		time.sleep(SETTLE)
		quit(desktop, nvda)


@pytest.mark.xfail(
	strict=True,
	reason="NVDAObjects.AtSpi asks virtualBuffers.atspi for a buffer class only for a document, not a dialog",
)
def test_dialogOverThePageIsReadInBrowseMode(desktop: DesktopSession):
	# As on Windows, a dialog on a page gets a buffer of its own, which keeps reading inside it.
	nvda = startFirst(desktop)
	webSession.startFirefox(desktop, "realDialog.html")
	expect(desktop, "Consent page…", "Reject all button", since=0, timeout=BUFFER_BOUND)
	time.sleep(SETTLE)
	step(desktop, "control+Home", "heading level 2 Before you continue")
	step(desktop, "Down", "We use cookies to keep the site working.")
	step(desktop, "control+End", "button Reject all button Accept all")
	step(desktop, "Tab", "Accept all button")
	# Answered, the dialog leaves the page, whose own buffer takes the focus.
	step(desktop, "Return", "main landmark Video site heading level 1…")
	step(desktop, "Down", "The videos would be here.")
	quit(desktop, nvda)


def test_pageThatChanges(desktop: DesktopSession, tmp_path: Path):
	nvda = browse(
		desktop,
		tmp_path,
		"dynamic.html",
		"Dynamic page",
		"heading level 1 Dynamic page",
		"Tail paragraph.",
	)
	step(desktop, "control+Home", "heading level 1 Dynamic page")
	step(desktop, "b", "Add paragraph button")
	desktop.press("Return")
	time.sleep(SETTLE)
	if not UPDATES_ARRIVE:
		hook(desktop, "rerender", "button Add paragraph")
	step(desktop, "Down", "Added paragraph 1 with link a new link dot")
	quit(desktop, nvda)
