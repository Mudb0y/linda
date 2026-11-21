"""linda-keyd's safety on KDE Plasma, with no Linda: every subtest of wl/helperSafety.py, as
wlGnomeHelper runs them under GNOME, here in the Plasma guest under KWin and plasmashell. Its figures
go to the log as one JSON object, after "VH plasma figures".
"""

import json
from typing import TYPE_CHECKING

from wl import helperSafety

if TYPE_CHECKING:
	from ground import Ground


def run(ground: "Ground") -> None:
	ground.machine.log(f"start-up, seconds from starting the guest: {ground.bootTimes}")
	ground.machine.log(f"the desktop: {json.dumps(ground.focus('facts'))}")
	figures = helperSafety.run(ground)
	ground.machine.log("VH plasma figures " + json.dumps(figures, sort_keys=True))
