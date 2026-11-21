"""The Xorg half of experiment E6, in the X guest the keyd cases boot: what the X server writes to a
new keyboard's lock lights, by guest/ledProbe.py's experiment, the same as the Wayland guests' probe
cases make it, so that a desktop's answer can be set beside the owner's own X session's. Its figures
go to the log as one JSON object, after "E6 x".
"""

import json
import shlex
from typing import TYPE_CHECKING

if TYPE_CHECKING:
	from ground import Ground

#: The X server's process, by its comm: NixOS starts it as X, Xorg's other name.
READER = "^X(org)?$"


def run(ground: "Ground") -> None:
	with ground.subtest("E6: what the X server writes to a new keyboard's lights"):
		result = json.loads(ground.shell(f"ground-led-probe experiment --reader {shlex.quote(READER)}"))
		ground.machine.log("E6 x " + json.dumps(result))
		assert result["presserOpened"], f"the X server never opened the presser: {result}"
		for run in result["runs"]:
			ground.machine.log(
				f"E6 x, Num Lock {'on' if run['numLock'] else 'off'}, preset {run['preset']} "
				f"({run['presetBits']}): the X server opened it {run['desktop'] and run['desktop']['after']} s "
				f"after it was made and wrote {[(e['code'], e['value'], e['after']) for e in run['ledsByDesktop']]}, "
				f"changing bits {run['desktopChangedBits']}; before it, {run['ledsBeforeDesktop']}; echoed "
				f"{run['echoed']}; the probe ended with {run['final']}; the reference keyboard had "
				f"{run['referenceBefore']} before and {run['referenceAfter']} after"
			)
			assert run["desktop"] is not None, f"the X server never opened the probe: {run['openers']}"
