"""Plasma's lock screen with Linda: KWin's screen locker owns org.freedesktop.ScreenSaver and runs its
greeter, kscreenlocker_greet, as its child, and sets logind's LockedHint. Linda says the session is
locked, reads the greeter's password field, speaks none of the password typed into it through
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
	ground.machine.log(f"the desktop: {json.dumps(ground.focus('facts'))}")
	# E9 found KWin and plasmashell on the bus once a reader claims it, and the greeter is a Qt program
	# that starts after Linda's claim. A key that is no character wakes it without typing into it.
	figures = lockScreen.run(ground, promptIsRead=True, raisePrompt=["KEY_LEFTCTRL"])
	ground.machine.log("LK figures " + json.dumps(figures, sort_keys=True))
