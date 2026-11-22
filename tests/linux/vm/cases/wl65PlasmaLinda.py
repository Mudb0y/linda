"""Plasma 6.5, as NixOS 25.11 ships it, with Linda and linda-keyd from this flake's own nixpkgs: every
subtest of wlPlasmaHelper, the helper's safety with its own test client and no Linda, and then
wlGnomeLinda's acceptance as it reads on Plasma, in one boot. It is Plasma 6.5's half of experiment
E25. The helper's figures go to the log after "E25 helper figures", as JSON.

wlGnomeLinda's subtests are run as they are but two. GNOME's two input sources and Super+Space become
KWin's two layouts, pl and us, written to the user's kxkbrc and switched with Meta+Alt+K, KWin's own
shortcut; and since KWin compiles both layouts into one keymap, in which the right Alt key of the us
group is Alt_R, the gesture name and the echo after the switch are held to that keymap rather than to
GNOME's, which keeps AltGr. Experiment E4, whose timings are of GNOME's overview and run dialog, is
not run. Alt+F2 opens KRunner, which Linda names as the way back on KDE as on GNOME.

wl66PlasmaLinda runs what is here on Plasma 6.6.
"""

import shlex
from typing import TYPE_CHECKING

import wl49GnomeLinda as older
import wlGnomeLinda as gnome
from ground import WINDOW_TITLE
from wl import lindaAcceptance as acceptance
from wl.lindaAcceptance import said

if TYPE_CHECKING:
	from ground import FakeKeyboard, Ground

KWIN_VERSION = "6.5.6"
LAYOUT_LOG = "Keyboard layout from kde: "
KXKBRC = "$HOME/.config/kxkbrc"
TWO_LAYOUTS = "[Layout]\nLayoutList=pl,us\nUse=true\n"
#: What KConfig sends when Plasma's keyboard settings write kxkbrc's layouts, which KWin's
#: KConfigWatcher answers by reading the file again (src/keyboard_layout.cpp in 6.5.6).
RELOAD = [
	"gdbus",
	"emit",
	"--session",
	"--object-path",
	"/kxkbrc",
	"--signal",
	"org.kde.kconfig.notify.ConfigChanged",
	"{'Layout': [b'LayoutList']}",
]


def layoutChanges(ground: "Ground", layout: str) -> int:
	return len(ground.lindaLogRecords(f"{LAYOUT_LOG}{layout}\n"))


def kwinLayouts(ground: "Ground") -> tuple[list[str], int | None]:
	facts = ground.focus("facts")
	layouts = facts["layouts"]
	return ([entry[0] for entry in layouts] if isinstance(layouts, list) else []), facts["layout"]


def reconfigure(ground: "Ground", text: str | None, layouts: list[str]) -> None:
	"""Write the user's kxkbrc, or remove it with None, and have KWin read it as its settings do."""
	write = f"printf %s {shlex.quote(text)} > {KXKBRC}" if text is not None else f"rm -f {KXKBRC}"
	answer = ground.run(["sh", "-c", f"mkdir -p $HOME/.config && {write}"])
	assert answer.returncode == 0, answer
	answer = ground.run(RELOAD)
	assert answer.returncode == 0, answer
	ground.waitFor(lambda: kwinLayouts(ground)[0] == layouts, f"KWin to have the layouts {layouts}")


def switchTo(ground: "Ground", keyboard: "FakeKeyboard", layout: str) -> None:
	before = layoutChanges(ground, layout)
	ground.settle(acceptance.APART)
	keyboard.chord("KEY_LEFTMETA", "KEY_LEFTALT", "KEY_K")
	try:
		ground.waitFor(
			lambda: layoutChanges(ground, layout) > before, f"Linda to follow the switch to {layout}", 10
		)
	finally:
		ground.machine.log(f"after Meta+Alt+K to {layout} KWin has {kwinLayouts(ground)}")


def layoutAfterSwitch(ground: "Ground", keyboard: "FakeKeyboard", layout: str) -> None:
	name = acceptance.gestureName(ground, keyboard, "KEY_RIGHTALT", "KEY_A")
	ground.machine.log(f"input help in {layout} names AltGr with A: {name}")
	ground.settle(acceptance.APART)
	mark = ground.mark()
	keyboard.chord("KEY_RIGHTALT", "KEY_A")
	app = ground.waitForKeys(
		"app", [("KEY_RIGHTALT", 1), ("KEY_A", 1), ("KEY_A", 0), ("KEY_RIGHTALT", 0)], mark
	)
	if layout == "pl":
		assert any(gnome.OGONEK in text for text in name), name
		assert app[1]["keyval"] == "aogonek", app
		ground.waitForSpeech(gnome.OGONEK, mark)
	else:
		# In the us group of KWin's keymap the right Alt key is Alt_R, so the program has Alt with a,
		# and Linda names it so and echoes nothing.
		assert not any(gnome.OGONEK in text for text in name), name
		assert any("alt plus a" in text for text in name), name
		assert app[0]["keyval"] == "Alt_R" and app[1]["keyval"] == "a", app
		ground.settle(0.5)
		assert not any(gnome.OGONEK in text for text in said(ground, mark)), said(ground, mark)
	ground.machine.log(
		f"in {layout} AltGr with A reached the program as {app[1]['keyval']}; Linda said {said(ground, mark)}"
	)


def twoLayouts(ground: "Ground", keyboard: "FakeKeyboard") -> None:
	with ground.subtest("two layouts, pl and us, switched with Meta+Alt+K"):
		before = layoutChanges(ground, "pl")
		reconfigure(ground, TWO_LAYOUTS, ["pl", "us"])
		ground.waitFor(lambda: layoutChanges(ground, "pl") > before, "Linda to read the two layouts")
		ground.machine.log(f"with two layouts KWin has {kwinLayouts(ground)}")
		for layout in ("us", "pl"):
			switchTo(ground, keyboard, layout)
			layoutAfterSwitch(ground, keyboard, layout)
		reconfigure(ground, None, ["pl"])


def plasmaRun(ground: "Ground", version: str) -> None:
	ground.machine.log(f"start-up, seconds from starting the guest: {ground.bootTimes}")
	facts = ground.focus("facts")
	ground.machine.log(f"the desktop: {facts}")
	assert version in facts["kwin"], facts
	keyboard = older.helperFirst(ground)
	gnome.noHelper(ground, keyboard)
	gnome.keptKeys(ground, keyboard)
	gnome.polish(ground, keyboard)
	twoLayouts(ground, keyboard)
	gnome.helperKilled(ground, keyboard)
	gnome.lindaStopped(ground, keyboard)
	gnome.console(ground, keyboard)
	gnome.pointer(ground, keyboard)
	ground.activate(WINDOW_TITLE)
	pid = gnome.emergency(ground, keyboard)
	gnome.nothingLogged(ground, keyboard, pid)


def run(ground: "Ground") -> None:
	plasmaRun(ground, KWIN_VERSION)
