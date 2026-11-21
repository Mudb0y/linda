"""linda-keyd's safety on GNOME, with no Linda: every layer the keyd cases prove in the X guest, proven
again under GNOME Shell in safe mode by wl/helperSafety.py, before Linda's own route is trusted there.
Its figures go to the log as one JSON object, after "V2 figures".
"""

import json
from typing import TYPE_CHECKING

from wl import helperSafety

if TYPE_CHECKING:
	from ground import Ground


def run(ground: "Ground") -> None:
	ground.machine.log(f"start-up, seconds from starting the guest: {ground.bootTimes}")
	facts = ground.focus("facts")
	ground.machine.log(f"the shell: {facts}")
	assert facts["unsafeMode"] is False, facts
	figures = helperSafety.run(ground)
	ground.machine.log("V2 figures " + json.dumps(figures, sort_keys=True))
