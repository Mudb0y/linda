"""What Linda offers of NVDA's features that Linux lacks, met from the keyboard as a person meets it: the
NVDA menu's Tools and Help, the settings dialog's categories and the panels that lose controls, the
braille display dialog, the Input Gestures dialog, and a hidden command still reached by a gesture.
platformFeatures decides each of these. Every key is pressed on the rig's display under marco.

nix develop . -c tests/linux/rig env LINDA_RIG_WM=marco python3 -m pytest -q tests/linux/test_platformFeatures.py

Without LINDA_RIG_WM=marco, DesktopSession starts marco itself. The transcript holds what the
synthesizer is given, after symbols are spoken as words, so "Alt+P" is "Alt plus P".
"""

import os
import time
from collections.abc import Iterator
from pathlib import Path

import desktopSession
import harness
import pytest
from desktopSession import DesktopSession

SESSION_CONFIG = harness.HERE / "fixtures" / "sessionConfig" / "nvda.ini"
#: Seconds from starting NVDA to its first words; a start under the rig takes about three.
STARTUP_BOUND = 60.0
#: Seconds for anything that should have happened by now to show.
SETTLE = 1.0
ISSUES_URL = "https://github.com/Mudb0y/linda/issues"
UNAVAILABLE = "Not available on Linux."

GTK = ("gtkFixture", "Fixture window")
AT_START = ("Fixture window", "Press me button Does nothing Alt plus P")


@pytest.fixture
def desktop(tmp_path: Path) -> Iterator[DesktopSession]:
	with DesktopSession(tmp_path / "desktop") as session:
		yield session


def start(desktop: DesktopSession, config: Path = SESSION_CONFIG, env: dict[str, str] | None = None):
	app = desktop.startApp(desktop.fixture(GTK[0]), title=GTK[1])
	# On the accessibility desktop before it has presented itself and has the keyboard.
	assert app.stdout.readline() == "ready\n"
	desktopSession.activate(GTK[1])
	nvda = desktop.startNVDA(config, env=env)
	desktop.expect(*AT_START, since=0, timeout=STARTUP_BOUND)
	return nvda


def step(desktop: DesktopSession, chord: str, *expected: str, timeout: float = 5) -> int:
	"""Press chord and wait for expected, in order; return the mark taken before the key."""
	mark = desktop.mark()
	desktop.press(chord)
	desktop.expect(*expected, since=mark, timeout=timeout)
	return mark


def walk(desktop: DesktopSession, key: str, items: list[str], *, wraps: bool = True) -> None:
	"""Press key once for each of items after the first, hearing each, and once more, which comes back to
	the first in a menu and says nothing at the end of a list, so that nothing else is in it."""
	for item in items[1:]:
		step(desktop, key, item)
	if wraps:
		step(desktop, key, items[0])
	else:
		mark = desktop.mark()
		desktop.press(key)
		time.sleep(SETTLE)
		assert desktop.spokenSince(mark) == []


def assertExitedCleanly(nvda: harness.Launch) -> None:
	assert nvda.process.returncode == 0, nvda.describe()
	nvda.assertClean(logNames=("nvda.log",))


def fakeOpener(directory: Path) -> tuple[dict[str, str], Path]:
	"""An environment whose xdg-open writes what it is asked to open to the returned file and opens
	nothing."""
	bin = directory / "bin"
	bin.mkdir(parents=True)
	opened = directory / "opened"
	opener = bin / "xdg-open"
	opener.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" >> {opened}\n', encoding="utf-8")
	opener.chmod(0o755)
	return {"PATH": f"{bin}:{os.environ['PATH']}"}, opened


def test_toolsAndHelpMenusOfferOnlyWhatWorksAndHelpWithLinda(desktop: DesktopSession, tmp_path: Path):
	env, opened = fakeOpener(tmp_path)
	nvda = start(desktop, env=env)
	step(desktop, "Insert+n", "Preferences sub Menu P 1 of 9")
	step(desktop, "Down", "Tools sub Menu T 2 of 9")
	step(desktop, "Right", "View log L 1 of 6")
	walk(
		desktop,
		"Down",
		[
			"View log L 1 of 6",
			"Speech viewer not checked S 2 of 6",
			"Braille viewer not checked B 3 of 6",
			"Add-on store... A 4 of 6",
			"Python console P 5 of 6",
			"Reload plugins 6 of 6",
		],
	)
	step(desktop, "Escape", "Tools sub Menu T 2 of 9")
	step(desktop, "Down", "Help sub Menu H 3 of 9")
	step(desktop, "Right", "User Guide U 1 of 8")
	walk(
		desktop,
		"Down",
		[
			"User Guide U 1 of 8",
			"Commands Quick Reference Q 2 of 8",
			"What's new N 3 of 8",
			"NV Access web site W 4 of 8",
			"Report a problem with Linda R 5 of 8",
			"License I 6 of 8",
			"Welcome dialog... L 7 of 8",
			"About... A 8 of 8",
		],
	)
	step(desktop, "r", *AT_START)
	deadline = time.monotonic() + 10
	while not opened.exists() and time.monotonic() < deadline:
		time.sleep(0.1)
	assert opened.read_text(encoding="utf-8").splitlines() == [ISSUES_URL]
	desktop.close()
	assertExitedCleanly(nvda)


def tabAround(desktop: DesktopSession, limit: int = 60) -> list[str]:
	"""Press Tab until the categories list has the focus again, and give what was heard after each
	press, its records joined."""
	heard = []
	for _ in range(limit):
		mark = desktop.mark()
		desktop.press("Tab")
		deadline = time.monotonic() + 5
		while not desktop.spokenSince(mark) and time.monotonic() < deadline:
			time.sleep(0.05)
		# A control's name and its position can come as two records.
		time.sleep(0.3)
		spoken = desktop.spokenSince(mark)
		heard.append(" | ".join(spoken))
		if spoken and spoken[0] == "Categories: list":
			return heard
	raise AssertionError(f"Tab never came back to the categories: {heard}")


def readWindow(desktop: DesktopSession, last: str) -> list[str]:
	"""Everything Linda+B reads of the active window, up to and including last."""
	mark = step(desktop, "Insert+b", last, timeout=15)
	return desktop.spokenSince(mark)


BUTTONS = ["Apply button Alt plus A", "Cancel button Alt plus C", "OK button Alt plus O"]

CATEGORIES = [
	"General 1 of 15",
	"Speech 2 of 15",
	"Braille 3 of 15",
	"Audio 4 of 15",
	"Privacy and Security 5 of 15",
	"Keyboard 6 of 15",
	"Mouse 7 of 15",
	"Review Cursor 8 of 15",
	"Object Presentation 9 of 15",
	"Browse Mode 10 of 15",
	"Document Formatting 11 of 15",
	"Document Navigation 12 of 15",
	"Remote Access 13 of 15",
	"Add-on Store 14 of 15",
	"Advanced 15 of 15",
]

#: What Linda+B reads in each panel or group that a feature Linux lacks was taken out of, and would
#: read on Windows.
NOT_READ = (
	"sign-in",
	"update",
	"Update mirror",
	"Prevent display",
	"ducking",
	"Sound split",
	"sound split",
	"Screen Curtain",
	"usage statistics",
	"UI Automation",
	"Windows Console",
	"Windows Terminal",
	"SAPI 4",
	"Virtual Buffers",
	"Chromium",
	"Touch",
	"Displays to detect automatically",
)


def assertNoneRead(spoken: list[str]) -> None:
	leaked = [text for text in spoken if any(mark in text for mark in NOT_READ)]
	assert not leaked, spoken


def openSettings(desktop: DesktopSession) -> None:
	step(desktop, "Insert+n", "Preferences sub Menu P 1 of 9")
	step(desktop, "Return", "Settings... S 1 of 4")
	step(
		desktop,
		"Return",
		"Linda Settings: General (normal configuration) dialog",
		"Categories: list",
		"General 1 of 15",
	)


def toCategory(desktop: DesktopSession, current: str, name: str) -> None:
	"""Arrow down the categories from current to name, hearing each."""
	names = [category.rsplit(" ", 3)[0] for category in CATEGORIES]
	for category in CATEGORIES[names.index(current) + 1 : names.index(name) + 1]:
		step(desktop, "Down", category)


def test_settingsLeaveOutWhatLinuxLacks(desktop: DesktopSession):
	nvda = start(desktop)
	openSettings(desktop)
	walk(desktop, "Down", CATEGORIES, wraps=False)
	step(desktop, "Home", "General 1 of 15")

	assert tabAround(desktop) == [
		"Linda Language (requires restart): combo box English, en collapsed Alt plus L",
		"Save configuration when exiting Linda check box checked Alt plus S",
		"Show exit options when exiting Linda check box checked Alt plus W",
		"Play sounds when starting or exiting Linda check box not checked Alt plus P",
		*BUTTONS,
		"Categories: list | General 1 of 15",
	]
	# Upstream's: disabled, and so passed over by Tab, until it can start Linda.
	general = readWindow(desktop, "separator")
	assert "Start Linda after I sign in check box unavailable not checked Alt plus A" in general
	assertNoneRead(general)

	toCategory(desktop, "General", "Audio")
	assert tabAround(desktop) == [
		"Audio output device: combo box Default output device collapsed Alt plus D",
		"Volume of Linda sounds follows voice volume check box not checked",
		"Volume of Linda sounds slider 100",
		"Time to keep audio device awake after speech (seconds) spin button 30 Alt plus K",
		*BUTTONS,
		"Categories: list | Audio 4 of 15",
	]
	assertNoneRead(readWindow(desktop, "separator"))

	toCategory(desktop, "Audio", "Privacy and Security")
	privacy = readWindow(desktop, "separator")
	# The rig's debug logging flag is what disables the logging level.
	assert privacy[privacy.index("General grouping") :] == [
		"General grouping",
		"Logging level: unavailable",
		"Logging level: combo box debug unavailable collapsed Alt plus O",
		"scroll bar 0",
		"OK button Alt plus O",
		"Cancel button Alt plus C",
		"Apply button Alt plus A",
		"separator",
	], privacy
	assertNoneRead(privacy)

	toCategory(desktop, "Privacy and Security", "Browse Mode")
	browseMode = tabAround(desktop)
	assert browseMode[-5:] == [
		"Native selection mode combo box Default (Disabled) collapsed Alt plus V",
		*BUTTONS,
		"Categories: list | Browse Mode 10 of 15",
	], browseMode
	assertNoneRead(browseMode)
	assertNoneRead(readWindow(desktop, "separator"))

	toCategory(desktop, "Browse Mode", "Advanced")
	step(desktop, "Tab", "I understand that changing these settings may cause Linda to function incorrectly…")
	step(desktop, "space", "checked")
	# Enabling the controls below the box raises events of its own, which would cut the reading short.
	time.sleep(SETTLE)
	advanced = readWindow(desktop, "separator")
	assertNoneRead(advanced)
	for kept in (
		"Speak passwords in all enhanced terminals (may improve performance) check box not checked Alt plus P",
		"Diff algorithm: combo box Automatic (prefer Diff Match Patch) collapsed Alt plus D",
		"Trim leading silence in speech audio check box checked",
		"Editable Text grouping",
	):
		assert kept in advanced, advanced
	step(desktop, "Escape", *AT_START)

	step(
		desktop, "Insert+control+a", "Select Braille Display dialog", "Braille display: combo box No braille…"
	)
	step(desktop, "Up", "Automatic")
	mark = desktop.mark()
	desktop.press("Up")
	time.sleep(SETTLE)
	assert desktop.spokenSince(mark) == []
	assert readWindow(desktop, "separator")[1:] == [
		"Braille display:",
		"Braille display: combo box Automatic collapsed Alt plus D",
		"Port: unavailable",
		"Port: combo box unavailable collapsed Alt plus P",
		"OK button Alt plus O",
		"Cancel button Alt plus C",
		"separator",
	]
	step(desktop, "Escape", *AT_START)
	desktop.close()
	assertExitedCleanly(nvda)


#: The global commands of the features Linux lacks.
HIDDEN_COMMANDS = sorted(
	[
		"activateInputCompositionDialog",
		"activateMagnifierSettingsDialog",
		"activateVisionSettingsDialog",
		"activateWindowsOCRSettings",
		"cycleAudioDuckingMode",
		"cycleFilters",
		"cycleOcrLanguage",
		"cycleSoundSplit",
		"cycleTrackingModes",
		"moveMouseToView",
		"panDown",
		"panLeft",
		"panRight",
		"panToBottomEdge",
		"panToLeftEdge",
		"panToRightEdge",
		"panToTopEdge",
		"panUp",
		"recognizeWithUwpOcr",
		"reportScreenCurtainState",
		"showEntireScreenOverview",
		"test_navigatorDisplayModelText",
		"toggleAllFollow",
		"toggleAutomaticRefreshOfRecogResult",
		"toggleFollowMouse",
		"toggleFollowNavigatorObject",
		"toggleFollowReview",
		"toggleFollowSystemFocus",
		"toggleMagnifier",
		"toggleScreenCurtain",
		"toggleTouchSupport",
		"touch_changeMode",
		"touch_explore",
		"touch_hoverUp",
		"touch_newExplore",
		"touch_rightClick",
		"zoomIn",
		"zoomOut",
	],
)


def test_hiddenCommandsAreBoundToNothingListedNowhereAndSayWhy(tmp_path: Path):
	done = harness.runChild(
		tmp_path,
		"""
		import languageHandler

		languageHandler.setLanguage("en")
		import NVDAHelper  # noqa: F401
		import globalCommands
		import keyLabels
		import platformFeatures
		import ui
		from gui import blockAction

		cls = globalCommands.GlobalCommands
		unavailable = platformFeatures._unavailableScript("x").__code__
		hidden = sorted(
			name[len("script_") :]
			for name, script in vars(cls).items()
			if name.startswith("script_") and script.__code__ is unavailable
		)
		spoken = []
		ui.message = spoken.append
		for name in hidden:
			getattr(globalCommands.commands, f"script_{name}")(None)
		result = {
			"hidden": hidden,
			"described": [name for name in hidden if getattr(cls, f"script_{name}").__doc__],
			"bound": sorted(
				gesture
				for gesture, script in globalCommands.commands._gestureMap.items()
				if script.__name__[len("script_") :] in hidden
			),
			"boundInTheClass": sorted(
				gesture for gesture, name in vars(cls)["_GlobalCommands__gestures"].items() if name in hidden
			),
			"spoken": sorted(set(spoken)),
			"spokenCount": len(spoken),
			"stillBound": globalCommands.commands._gestureMap["kb:nvda+t"].__name__,
			"keys": keyLabels.getKeyCombinationLabel("leftWindows+rightWindows+windows+NVDA"),
			"passNextKey": cls.script_passNextKeyThrough.__doc__,
			"clipboard": cls.script_reportClipboardText.__doc__,
			"brailleWindows": cls.script_braille_toggleWindows.__doc__,
			"brailleWindowsShift": cls.script_braille_toggleWindowsShift.__doc__,
			"locked": blockAction.Context.WINDOWS_LOCKED.translatedMessage,
		}
		""",
	)
	done.assertClean()
	assert done.value == {
		"hidden": HIDDEN_COMMANDS,
		"described": [],
		"bound": [],
		"boundInTheClass": [],
		"spoken": [UNAVAILABLE],
		"spokenCount": len(HIDDEN_COMMANDS),
		"stillBound": "script_title",
		"keys": "left super+right super+super+Linda",
		"passNextKey": "The next key that is pressed will not be handled at all by Linda, "
		"it will be passed directly through to the focused application.",
		"clipboard": "Reports the text on the clipboard. Pressing twice spells this information. "
		"Pressing three times spells it using character descriptions.",
		"brailleWindows": "Virtually toggles the left super key to emulate a keyboard shortcut with braille input",
		"brailleWindowsShift": "Virtually toggles the left super and shift keys to emulate a keyboard shortcut "
		"with braille input",
		"locked": "Action unavailable while the computer is locked",
	}


def filterGestures(desktop: DesktopSession, text: str) -> list[str]:
	"""Filter the Input Gestures tree by text from its filter field, and give what is heard of the tree."""
	desktop.press("control+a")
	desktop.typeText(text)
	time.sleep(SETTLE)
	mark = step(desktop, "Tab", "tree view")
	for _ in range(3):
		desktop.press("Down")
		time.sleep(0.3)
	time.sleep(SETTLE)
	heard = desktop.spokenSince(mark)
	step(desktop, "shift+Tab", "Filter by: edit…")
	return heard


def test_hiddenCommandsAreNotOfferedAndSayWhyWhenStillReached(desktop: DesktopSession, tmp_path: Path):
	config = tmp_path / "config"
	config.mkdir()
	(config / "nvda.ini").write_text(SESSION_CONFIG.read_text(encoding="utf-8"), encoding="utf-8")
	# A gesture a user bound to a command before it was hidden.
	(config / "gestures.ini").write_text(
		"[globalCommands.GlobalCommands]\ntoggleMagnifier = kb:f12+shift\n", encoding="utf-8"
	)
	nvda = start(desktop, config)

	step(desktop, "Insert+n", "Preferences sub Menu P 1 of 9")
	step(desktop, "p", "Settings... S 1 of 4")
	# Audio held only audio ducking and sound split.
	step(desktop, "n", "Input Gestures dialog", "tree view", "level 1 Braille collapsed 1 of …")
	step(desktop, "shift+Tab", "Filter by: edit Alt plus F blank")
	shown = filterGestures(desktop, "current time")
	assert "level 2 Pressing once reverts…" not in shown
	assert [text for text in shown if "time" in text], shown
	for hidden in ("magnif", "curtain", "ducking", "sound split", "touch", "OCR", "display model"):
		shown = filterGestures(desktop, hidden)
		assert shown == ["tree view"], (hidden, shown)
	step(desktop, "Escape", *AT_START)

	# A hidden command's default key is no longer taken from the application.
	mark = desktop.mark()
	desktop.press("Insert+shift+w")
	time.sleep(SETTLE)
	assert desktop.spokenSince(mark) == []
	step(desktop, "shift+F12", UNAVAILABLE)

	step(desktop, "Insert+1", "input help on…")
	mark = step(desktop, "Insert+F2", "Linda plus f 2…")
	assert (
		"The next key that is pressed will not be handled at all by Linda, "
		"it will be passed directly through to the focused application."
	) in " ".join(desktop.spokenSince(mark)), desktop.spokenSince(mark)
	step(desktop, "Super_L", "left super")
	mark = desktop.mark()
	desktop.press("Super_R")
	time.sleep(SETTLE)
	# The key source reports right Super with a Super modifier held; only the key's name matters here.
	assert [text for text in desktop.spokenSince(mark) if text.endswith("right super")], desktop.spokenSince(
		mark
	)
	step(desktop, "Insert+1", "input help off…")
	desktop.close()
	assertExitedCleanly(nvda)
