"""GNOME's lock screen with Linda: gnome-shell draws it and owns org.gnome.ScreenSaver, and neither the
shell nor Linda, both started by the user's systemd manager, is in the logind session, so Linda's lock
monitor has to learn from logind that the shell's lock is its own session's. Then it says the session
is locked, reads the shell's password field, speaks none of the password typed into it through
linda-keyd, and says the session is unlocked, while a page in browse mode below the lock keeps none of
the keys; wl/lockScreen.py holds the case.
"""

import json
from typing import TYPE_CHECKING

from wl import lockScreen

if TYPE_CHECKING:
	from ground import Ground


def run(ground: "Ground") -> None:
	ground.machine.log(f"start-up, seconds from starting the guest: {ground.bootTimes}")
	facts = ground.focus("facts")
	ground.machine.log(f"the shell: {facts}")
	assert facts["unsafeMode"] is False, facts
	# E9 found gnome-shell on the bus. A key that is no character brings the password field up over
	# the clock without typing into it.
	figures = lockScreen.run(ground, promptIsRead=True, raisePrompt=["KEY_LEFTCTRL"])
	ground.machine.log("LK figures " + json.dumps(figures, sort_keys=True))
