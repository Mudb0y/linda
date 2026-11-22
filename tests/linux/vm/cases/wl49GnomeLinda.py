"""GNOME 49, as NixOS 25.11 ships it, with Linda and linda-keyd from this flake's own nixpkgs: every
subtest of wlGnomeHelper, the helper's safety with its own test client and no Linda, and then every
subtest of wlGnomeLinda, Linda's acceptance with the real helper, in one boot. It is GNOME 49's half
of experiment E25. The helper's figures go to the log after "E25 helper figures", E5's after "E5
figures" and E4's after "E4 figures", as JSON.

The other older desktops' cases run what is here (default.nix gives it to them by name):
wl50GnomeLinda for GNOME 50, and wl65PlasmaLinda and wl66PlasmaLinda for the helper's part.
"""

import json
from typing import TYPE_CHECKING

import wlGnomeLinda as gnome
from ground import WINDOW_TITLE
from wl import helperSafety
from wl import lindaAcceptance as acceptance

if TYPE_CHECKING:
	from ground import FakeKeyboard, Ground

SHELL_VERSION = "49.4"
#: The helper's test client, as helperSafety.run starts it, by ground's name for its job.
HELPER_CLIENT = "keyd-main"
FIREFOX_152 = (
	"NixOS 25.11's Firefox 152, started as E4 starts it, shows on AT-SPI only an active frame named "
	"Mozilla Firefox and never the page's title within 180 s, and Linda says only Mozilla Firefox; "
	"why is not known (results/E25.md)"
)


def helperFirst(ground: "Ground") -> "FakeKeyboard":
	"""helperSafety's subtests, then the guest left as wlGnomeLinda's run has it once it has made its
	window and keyboard; answers that keyboard, which helperSafety made."""
	figures = helperSafety.run(ground)
	ground.machine.log("E25 helper figures " + json.dumps(figures, sort_keys=True))
	with ground.subtest("the helper's test client goes, and the helper is left as Linda finds it at boot"):
		ground.session({"op": "signal", "name": HELPER_CLIENT, "signal": "SIGTERM"})
		ground.waitFor(lambda: not ground.running(HELPER_CLIENT), "the helper's test client to end", 30)
		# helperSafety's console subtest leaves a reader on the text console, and openvt will not
		# start wlGnomeLinda's on a console still in use.
		holders = (
			f"find /proc/[0-9]*/fd -lname {helperSafety.CONSOLE_TEXT} 2>/dev/null | cut -d/ -f3 | sort -u"
		)
		ground.shell(f"{holders} | xargs -r kill")
		ground.waitFor(lambda: not ground.shell(holders).strip(), "the text console's reader to end")
		# The safety subtests had systemd restart the helper; Linda's own may need it to again.
		ground.shell("systemctl reset-failed linda-keyd")
		ground.waitForHelper()
		keyboard = next(k for k in ground.keyboards if k.name == acceptance.UHID)
		ground.activate(WINDOW_TITLE)
	return keyboard


def firefoxTimings(ground: "Ground", keyboard: "FakeKeyboard", figures: dict) -> None:
	firefox = acceptance.startFirefox(ground)
	ground.settle(3)
	figures["Firefox Alt+Tab"] = [acceptance.altTab(ground, keyboard) for _ in range(acceptance.SWITCHES)]
	ground.activate(firefox)
	figures["Firefox overview"] = [acceptance.overview(ground, keyboard) for _ in range(3)]
	figures["Firefox run dialog"] = [acceptance.runDialog(ground, keyboard) for _ in range(3)]
	figures["Alt+Tab from Firefox in say all"] = [
		acceptance.sayAllThenAltTab(ground, keyboard, firefox) for _ in range(3)
	]
	figures["two Firefox windows"] = acceptance.twoWindows(ground, keyboard, firefox)


def windowTimings(ground: "Ground", keyboard: "FakeKeyboard", firefoxFault: str | None) -> dict:
	"""lindaAcceptance.windowTimings, with the release's own Firefox held to firefoxFault where that
	version's Firefox shows it, so that the GTK windows' timings are had all the same."""
	figures: dict[str, list[dict]] = {}
	acceptance.startListening(ground)
	ground.openWindow(acceptance.GTK4_TITLE, name="window4", gtk4=True)
	with ground.subtest("E4: Alt+Tab between a GTK 3 and a GTK 4 window, Linda idle"):
		ground.activate(WINDOW_TITLE)
		figures["GTK Alt+Tab"] = [acceptance.altTab(ground, keyboard) for _ in range(acceptance.SWITCHES)]
	with ground.subtest("E4: the overview and the run dialog over a GTK 3 window"):
		figures["GTK 3 overview"] = [acceptance.overview(ground, keyboard) for _ in range(3)]
		figures["GTK 3 run dialog"] = [acceptance.runDialog(ground, keyboard) for _ in range(3)]
	with ground.subtest("E4: Firefox against Alt+Tab, the overview and the run dialog, idle and in say all"):
		if firefoxFault is None:
			firefoxTimings(ground, keyboard, figures)
		else:
			with ground.expectedFailure(firefoxFault):
				try:
					firefoxTimings(ground, keyboard, figures)
				except AssertionError:
					ground.machine.log(f"the windows as Firefox's part ended: {ground.focus('windows')}")
					raise
	for job in ("firefox", "window4", "windowEvents"):
		# A job never started has no status but an error.
		if "returncode" in ground.session({"op": "status", "name": job}) and ground.running(job):
			ground.session({"op": "signal", "name": job, "signal": "SIGTERM"})
			ground.waitFor(lambda job=job: not ground.running(job), f"{job} to end", 30)
	return {"summary": acceptance._kinds(figures), "samples": figures}


def lindaAcceptance(ground: "Ground", keyboard: "FakeKeyboard", firefoxFault: str | None = None) -> None:
	"""wlGnomeLinda's run, from its first subtest on."""
	gnome.noHelper(ground, keyboard)
	gnome.keptKeys(ground, keyboard)
	gnome.polish(ground, keyboard)
	figures = gnome.twoSources(ground, keyboard)
	ground.machine.log("E5 figures " + json.dumps(figures, sort_keys=True, ensure_ascii=False))
	gnome.helperKilled(ground, keyboard)
	gnome.lindaStopped(ground, keyboard)
	gnome.console(ground, keyboard)
	gnome.pointer(ground, keyboard)
	timings = windowTimings(ground, keyboard, firefoxFault)
	ground.machine.log("E4 figures " + json.dumps(timings, sort_keys=True))
	ground.activate(WINDOW_TITLE)
	pid = gnome.emergency(ground, keyboard)
	gnome.nothingLogged(ground, keyboard, pid)


def gnomeRun(ground: "Ground", version: str, firefoxFault: str | None = None) -> None:
	ground.machine.log(f"start-up, seconds from starting the guest: {ground.bootTimes}")
	facts = ground.focus("facts")
	ground.machine.log(f"the shell: {facts}")
	assert facts["shellVersion"] == version, facts
	assert facts["unsafeMode"] is False, facts
	keyboard = helperFirst(ground)
	lindaAcceptance(ground, keyboard, firefoxFault)


def run(ground: "Ground") -> None:
	gnomeRun(ground, SHELL_VERSION, FIREFOX_152)
