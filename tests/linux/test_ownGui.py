"""NVDA's own windows, used from the keyboard as a person setting NVDA up with nobody's help uses
them: the synthesizer, the voice and its settings ring, the welcome dialog of a first start, the
settings dialog and its keyboard panel, the NVDA menu and saving the configuration. Every key is
pressed on the rig's display under marco.

nix develop . -c tests/linux/rig env LINDA_RIG_WM=marco python3 -m pytest -q tests/linux/test_ownGui.py

Without LINDA_RIG_WM=marco, DesktopSession starts marco itself. The transcript holds what the
synthesizer is given, after symbols are spoken as words, so "Alt+P" is "Alt plus P".
"""

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
#: Seconds NVDA has to exit once its exit dialog is answered.
EXIT_BOUND = 10.0
#: Seconds to wait for anything more after what a step expects.
SETTLE = 0.5
#: Seconds to leave NVDA after a key whose effect cannot be heard, as when it speaks through a
#: synthesizer other than the transcript.
UNHEARD_STEP = 1.0

GTK = ("gtkFixture", "Fixture window")
AT_START = ("Fixture window", "Press me button Does nothing Alt plus P")

#: A synthesizer that writes the transcript as the test synthesizer does, with what the voice
#: settings show for a synthesizer such as eSpeak: two voices, two variants, a check box and four
#: sliders.
VOICED_TRANSCRIPT = """\
from collections import OrderedDict

from autoSettingsUtils.utils import StringParameterInfo
from synthDriverHandler import SynthDriver as BaseSynthDriver
from synthDriverHandler import VoiceInfo
from synthDrivers.transcript import SynthDriver as TranscriptSynthDriver


class SynthDriver(TranscriptSynthDriver):
	name = "voicedTranscript"
	description = "Voiced transcript (testing)"
	supportedSettings = (
		BaseSynthDriver.VoiceSetting(),
		BaseSynthDriver.VariantSetting(),
		BaseSynthDriver.RateSetting(),
		BaseSynthDriver.RateBoostSetting(),
		BaseSynthDriver.PitchSetting(),
		BaseSynthDriver.InflectionSetting(),
		BaseSynthDriver.VolumeSetting(),
	)

	def __init__(self):
		super().__init__()
		self._settings.update(voice="alpha", variant="plain", rateBoost=False)

	def _getAvailableVoices(self):
		return OrderedDict(alpha=VoiceInfo("alpha", "Alpha", "en"), beta=VoiceInfo("beta", "Beta", "en"))

	def _getAvailableVariants(self):
		return OrderedDict(
			plain=StringParameterInfo("plain", "Plain"),
			bright=StringParameterInfo("bright", "Bright"),
		)

	def _get_variant(self):
		return self._settings["variant"]

	def _set_variant(self, value):
		self._settings["variant"] = value

	def _get_rateBoost(self):
		return self._settings["rateBoost"]

	def _set_rateBoost(self, value):
		self._settings["rateBoost"] = value

	def _get_inflection(self):
		return self._settings["inflection"]

	def _set_inflection(self, value):
		self._settings["inflection"] = value
"""


@pytest.fixture
def desktop(tmp_path: Path) -> Iterator[DesktopSession]:
	with DesktopSession(tmp_path / "desktop") as session:
		yield session


def sessionConfig(
	directory: Path, *, replace: dict[str, str] | None = None, drop: tuple[str, ...] = ()
) -> Path:
	"""The session configuration with each line that starts with a key of replace replaced by its value,
	and each line that starts with an entry of drop left out."""
	lines = []
	for line in SESSION_CONFIG.read_text(encoding="utf-8").splitlines():
		stripped = line.strip()
		if any(stripped.startswith(prefix) for prefix in drop):
			continue
		for prefix, replacement in (replace or {}).items():
			if stripped.startswith(prefix):
				line = line[: len(line) - len(line.lstrip())] + replacement
		lines.append(line)
	directory.mkdir(parents=True, exist_ok=True)
	path = directory / "nvda.ini"
	path.write_text("\n".join(lines) + "\n", encoding="utf-8")
	return path


def start(desktop: DesktopSession, config: Path, env: dict[str, str] | None = None) -> harness.Launch:
	app = desktop.startApp(desktop.fixture(GTK[0]), title=GTK[1])
	# On the accessibility desktop before it has presented itself and has the keyboard.
	assert app.stdout.readline() == "ready\n"
	desktopSession.activate(GTK[1])
	return desktop.startNVDA(config, env=env)


def step(desktop: DesktopSession, chord: str, *expected: str, timeout: float = 5) -> int:
	"""Press chord and wait for expected, in order; return the mark taken before the key."""
	mark = desktop.mark()
	desktop.press(chord)
	desktop.expect(*expected, since=mark, timeout=timeout)
	return mark


def downToKeyboard(desktop: DesktopSession) -> None:
	"""Arrow down the settings dialog's categories from General to Keyboard, each spoken."""
	for index, category in enumerate(
		("Speech", "Braille", "Audio", "Privacy and Security", "Keyboard"),
		start=2,
	):
		step(desktop, "Down", f"{category} {index} of …")


def savedConfig(nvda: harness.Launch) -> str:
	return (nvda.directory / "config" / "nvda.ini").read_text(encoding="utf-8")


def waitForLog(nvda: harness.Launch, text: str, timeout: float = 10) -> None:
	deadline = time.monotonic() + timeout
	while text not in nvda.text("nvda.log"):
		assert time.monotonic() < deadline, f"the log never said {text!r}\n{nvda.describe()}"
		time.sleep(0.1)


def assertExitedCleanly(nvda: harness.Launch) -> None:
	assert nvda.process.returncode == 0, nvda.describe()
	nvda.assertClean(logNames=("nvda.log",))
	# The harness allows this report, which a person starting NVDA from a terminal used to see.
	assert "AutoSettings.__del__" not in nvda.text("stderr.txt"), nvda.text("stderr.txt")


def test_synthesizerIsChosenInItsDialogAndKeptForTheNextStart(desktop: DesktopSession):
	nvda = start(desktop, SESSION_CONFIG)
	desktop.expect(*AT_START, since=0, timeout=STARTUP_BOUND)
	step(
		desktop,
		"Insert+control+s",
		"Select Synthesizer dialog",
		"Synthesizer: combo box Transcript (testing) collapsed Alt plus S",
	)
	# The list is in the order of the descriptions; openevv describes itself as Open EVV.
	step(desktop, "Up", "Open EVV")
	step(desktop, "Up", "e Speak NG")
	step(desktop, "Down", "Open EVV")
	desktop.press("Return")
	waitForLog(nvda, "Loaded synthDriver openevv")

	# Answered without hearing it: the transcript has stopped with its synthesizer.
	desktop.press("Insert+q")
	time.sleep(UNHEARD_STEP)
	desktop.press("Return")
	nvda.waitForExit(EXIT_BOUND)
	assertExitedCleanly(nvda)
	assert "synth = openevv" in savedConfig(nvda), savedConfig(nvda)

	again = desktop.startNVDA(nvda.directory / "config")
	waitForLog(again, "Loaded synthDriver openevv", timeout=STARTUP_BOUND)
	assert "Loaded synthDriver transcript" not in again.text("nvda.log")
	desktop.close()
	assertExitedCleanly(again)


def test_voiceSettingsSpeakEachControlAndItsChanges(desktop: DesktopSession, tmp_path: Path):
	hooks = tmp_path / "hooks"
	(hooks / "synthDrivers").mkdir(parents=True)
	for name in ("transcript.py", "_transcriptRecords.py"):
		(hooks / "synthDrivers" / name).symlink_to(harness.HERE / "synthDrivers" / name)
	(hooks / "synthDrivers" / "voicedTranscript.py").write_text(VOICED_TRANSCRIPT, encoding="utf-8")
	(hooks / "globalPlugins").symlink_to(harness.HERE / "fixtures" / "globalPlugins")
	config = sessionConfig(tmp_path / "config", replace={"synth =": "synth = voicedTranscript"})
	nvda = start(desktop, config, env={"LINDA_TEST_HOOKS_DIR": str(hooks)})
	desktop.expect(*AT_START, since=0, timeout=STARTUP_BOUND)

	mark = step(
		desktop,
		"Insert+control+v",
		"Linda Settings: Speech (normal configuration) dialog",
		"Synthesizer grouping",
		"Synthesizer edit read only Voiced transcript (testing)",
	)
	time.sleep(SETTLE)
	assert not [text for text in desktop.spokenSince(mark) if text.startswith("OK button")]
	step(desktop, "Tab", "Change... button Alt plus H")
	step(desktop, "Tab", "Voice: combo box Alpha collapsed Alt plus V")
	step(desktop, "Down", "Beta")
	step(desktop, "Tab", "Variant: combo box Plain collapsed Alt plus A")
	step(desktop, "Down", "Bright")
	step(desktop, "Tab", "Rate: slider 50")
	step(desktop, "Right", "51")
	step(desktop, "Tab", "Rate boost check box not checked Alt plus T")
	step(desktop, "space", "checked")
	step(desktop, "Tab", "Pitch: slider 50")
	step(desktop, "Left", "49")
	step(desktop, "Tab", "Inflection: slider 50")
	step(desktop, "Tab", "Volume: slider 50")
	step(desktop, "Tab", "Automatic language switching (when supported) check box checked…")
	step(desktop, "space", "not checked")
	# Enter on a check box presses OK, as on Windows.
	step(desktop, "Return", *AT_START)

	# The settings ring starts at the rate.
	step(desktop, "Insert+control+Right", "Rate boost on")
	step(desktop, "Insert+control+Down", "Rate boost off")
	step(desktop, "Insert+control+Left", "Rate 51")
	mark = step(desktop, "Insert+control+Up", "Rate …")
	assert desktop.spokenSince(mark)[-1] != "Rate 51", desktop.spokenSince(mark)
	desktop.close()
	assertExitedCleanly(nvda)


def test_welcomeDialogOfAFirstStartSetsUpTheKeyboard(desktop: DesktopSession, tmp_path: Path):
	config = sessionConfig(tmp_path / "config", drop=("showWelcomeDialogAtStartup", "NVDAModifierKeys"))
	nvda = start(desktop, config)
	welcome = "Welcome to Linda dialog Welcome to Linda! Most commands for controlling Linda require you…"
	desktop.expect(
		welcome,
		"Options grouping",
		"Keyboard layout: combo box desktop collapsed Alt plus K",
		since=0,
		timeout=STARTUP_BOUND,
	)
	spoken = desktop.spokenSince(0)
	texts = [text for text in spoken if text.startswith("Welcome to Linda dialog")]
	assert len(texts) == 1, spoken
	assert texts[0].endswith("access other Linda functions."), texts[0]

	step(desktop, "Down", "laptop")
	# Enter in the open list chooses from it, and leaves the dialog open.
	step(desktop, "alt+Down", "laptop 2 of 2")
	step(desktop, "Return", "Keyboard layout: combo box laptop collapsed Alt plus K")
	step(desktop, "Tab", "Use Caps Lock as a Linda modifier key check box not checked Alt plus U")
	step(desktop, "space", "checked")
	step(desktop, "shift+Tab", "Keyboard layout: combo box laptop collapsed Alt plus K")
	# Enter on the closed list presses OK, as on Windows.
	step(desktop, "Return", *AT_START)
	step(desktop, "Caps_Lock+t", "Fixture window")
	saved = savedConfig(nvda)
	assert "keyboardLayout = laptop" in saved and "NVDAModifierKeys = 7" in saved, saved
	desktop.close()
	assertExitedCleanly(nvda)


def test_startAfterSignInIsOfferedAndSaysWhyThisCopyCannot(desktop: DesktopSession, tmp_path: Path):
	# The rig runs Linda from its source tree, which has no launcher a desktop entry could name.
	config = sessionConfig(tmp_path / "config", drop=("showWelcomeDialogAtStartup",))
	autostart = tmp_path / "xdgConfig" / "autostart"
	nvda = start(desktop, config, env={"XDG_CONFIG_HOME": str(tmp_path / "xdgConfig")})
	desktop.expect("Welcome to Linda dialog…", "Keyboard layout: combo box…", since=0, timeout=STARTUP_BOUND)
	step(desktop, "Tab", "Use Caps Lock as a Linda modifier key check box…")
	step(desktop, "Tab", "Start Linda after I sign in check box not checked Alt plus A")
	step(desktop, "space", "checked")
	step(
		desktop,
		"Return",
		"Start after sign-in dialog Linda cannot start after you sign in, because this copy…",
		"OK button Alt plus O",
	)
	(said,) = [text for text in desktop.spokenSince(0) if text.startswith("Start after sign-in dialog")]
	# The dialog wraps its message, and speech reads each line break as a space.
	reason = " ".join(said.split())
	assert reason.endswith(
		"because this copy runs from a source tree rather than from an installed launcher,"
		" so there is no command that would start it again."
	), reason
	step(desktop, "Return", *AT_START)
	assert not autostart.exists()

	step(desktop, "Insert+n", "Preferences sub Menu P 1 of 9")
	step(desktop, "Return", "Settings... S 1 of 4")
	step(desktop, "Return", "Categories: list General")
	for _press in range(12):
		mark = desktop.mark()
		desktop.press("Tab")
		time.sleep(SETTLE)
		if any(text.startswith("Start Linda after I sign in") for text in desktop.spokenSince(mark)):
			break
	desktop.expect("Start Linda after I sign in check box not checked Alt plus A", since=mark)
	step(desktop, "Escape", *AT_START)
	desktop.close()
	assertExitedCleanly(nvda)


def openInputGestures(desktop: DesktopSession, *expected: str) -> None:
	step(desktop, "Insert+n", "Preferences sub Menu P 1 of 9")
	step(desktop, "p", "Settings... S 1 of 4")
	step(desktop, "n", "Input Gestures dialog", "tree view", *expected)


def findNavigatorObjectScript(desktop: DesktopSession) -> None:
	"""Filter the Input Gestures tree and move to the script that reports the navigator object."""
	step(desktop, "shift+Tab", "Filter by: edit Alt plus F blank")
	desktop.typeText("current time")
	step(desktop, "Tab", "tree view")
	step(desktop, "Down", "Configuration (1 result) expanded 1 of …")
	step(desktop, "Down", "level 2 Pressing once reverts…")
	step(desktop, "Down", "level 1 Object navigation (1 result) expanded 2 of …")
	step(desktop, "Down", "level 2 Reports the current navigator object…")


def test_inputGesturesAreReadAndChangedFromTheKeyboard(desktop: DesktopSession):
	nvda = start(desktop, SESSION_CONFIG)
	desktop.expect(*AT_START, since=0, timeout=STARTUP_BOUND)

	# The tree, which wxGTK draws itself, is read from wx: each move, expansion and collapse.
	openInputGestures(desktop, "level 1 Braille collapsed 1 of …")
	step(desktop, "Down", "Browse mode collapsed 2 of …")
	step(desktop, "Right", "expanded")
	step(desktop, "Down", "level 2 …")
	step(desktop, "Left", "level 1 Browse mode expanded 2 of …")
	step(desktop, "Left", "collapsed")

	# A gesture added from the keyboard, choosing one of the forms of the key, works once saved.
	findNavigatorObjectScript(desktop)
	step(desktop, "alt+a", "level 3 Enter input gesture: 3 of 3")
	step(desktop, "shift+F12", "shift plus f 12 (desktop keyboard) 1 of 2")
	step(desktop, "Down", "shift plus f 12 (keyboard, all layouts) 2 of 2")
	step(desktop, "Return", "shift plus f 12 (keyboard, all layouts) 3 of 3…")
	# Enter in the tree presses OK, as it does on Windows.
	step(desktop, "Return", *AT_START)
	step(desktop, "shift+F12", "Press me button…")
	assert "navigatorObject_current = kb:f12+shift" in (nvda.directory / "config" / "gestures.ini").read_text(
		encoding="utf-8"
	)

	# Removed with Delete, it does nothing once saved.
	openInputGestures(desktop, "level 1 Braille collapsed 1 of …")
	findNavigatorObjectScript(desktop)
	step(desktop, "Right", "expanded")
	step(desktop, "Down", "level 3 shift plus f 12 (keyboard, all layouts) 1 of 3")
	# The item that takes its place is read.
	step(desktop, "Delete", "Linda plus numpad 5 (keyboard, all layouts) 1 of 2…")
	step(desktop, "alt+o", *AT_START)
	mark = desktop.mark()
	desktop.press("shift+F12")
	time.sleep(UNHEARD_STEP)
	assert not [t for t in desktop.spokenSince(mark) if t.startswith("Press me")], desktop.spokenSince(mark)
	desktop.close()
	assertExitedCleanly(nvda)


def test_logViewerIsReadByLine(desktop: DesktopSession):
	nvda = start(desktop, SESSION_CONFIG)
	desktop.expect(*AT_START, since=0, timeout=STARTUP_BOUND)
	step(desktop, "Insert+n", "Preferences sub Menu P 1 of 9")
	step(desktop, "t", "View log L 1 of …")
	mark = step(desktop, "l", "Linda Log Viewer", "edit read only DEBUG - …")
	# Each line is read as the caret moves, as in a read-only edit field on Windows.
	for key in ("Down", "Down", "Up"):
		mark = desktop.mark()
		desktop.press(key)
		deadline = time.monotonic() + 5
		while not [text for text in desktop.spokenSince(mark) if text.strip()]:
			assert time.monotonic() < deadline, f"{key} read nothing in the log viewer"
			time.sleep(0.05)
	step(desktop, "Escape", *AT_START)
	desktop.close()
	assertExitedCleanly(nvda)


def test_settingsDialogFromTheMenuChangesTheNvdaKeysAndLayout(desktop: DesktopSession, tmp_path: Path):
	# Insert and numpad Insert only, upstream's default.
	config = sessionConfig(tmp_path / "config", drop=("NVDAModifierKeys",))
	nvda = start(desktop, config)
	desktop.expect(*AT_START, since=0, timeout=STARTUP_BOUND)

	# Escape closes a submenu, and then the menu.
	step(desktop, "Insert+n", "Preferences sub Menu P 1 of 9")
	step(desktop, "Down", "Tools sub Menu T 2 of 9")
	step(desktop, "Right", "View log L 1 of 6")
	step(desktop, "Escape", "Tools sub Menu T 2 of 9")
	step(desktop, "Escape", *AT_START)

	step(desktop, "Insert+n", "Preferences sub Menu P 1 of 9")
	step(desktop, "Return", "Settings... S 1 of 4")
	mark = step(
		desktop,
		"Return",
		"Linda Settings: General (normal configuration) dialog",
		# With the fifteen categories Linux has, the list is spoken before the item it has selected.
		"Categories: list",
		"General 1 of 15",
	)
	time.sleep(SETTLE)
	assert not [text for text in desktop.spokenSince(mark) if text.startswith("OK button")]
	downToKeyboard(desktop)
	step(desktop, "Tab", "Keyboard layout: combo box desktop collapsed Alt plus K")
	step(desktop, "Down", "laptop")
	# Control+S is Apply; Alt+A only moves to the button while another control shares its letter.
	step(desktop, "control+s", "Categories: list…")
	step(desktop, "Escape", *AT_START)

	step(desktop, "Insert+n", "Preferences sub Menu P 1 of 9")
	step(desktop, "Return", "Settings... S 1 of 4")
	step(desktop, "Return", "Categories: list", "General 1 of 15")
	downToKeyboard(desktop)
	# Applied, and kept although the dialog was then cancelled.
	step(desktop, "Tab", "Keyboard layout: combo box laptop collapsed Alt plus K")
	step(desktop, "Tab", "Select Linda Modifier Keys list", "caps lock check box not checked 1 of 3")
	step(desktop, "space", "checked")
	step(desktop, "Return", *AT_START)
	step(desktop, "Caps_Lock+t", "Fixture window")

	step(desktop, "Insert+control+c", "Configuration saved")
	saved = savedConfig(nvda)
	assert "keyboardLayout = laptop" in saved and "NVDAModifierKeys = 7" in saved, saved
	desktop.close()
	assertExitedCleanly(nvda)
