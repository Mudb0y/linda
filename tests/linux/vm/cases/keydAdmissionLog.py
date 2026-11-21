"""What the journal says of an admitted client: the helper names the process by its comm, the program it
was started as and the systemd unit or scope it runs in, for a program of the X session and for one
of the user's systemd manager, before it says the client was admitted. Nothing of the keys the client
then hears and decides reaches the journal.
"""

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
	from ground import Ground

UHID = "Ground uhid keyboard"
CLIENT_LINE = re.compile(
	r'^linda-keyd: process (?P<pid>\d+) is "(?P<comm>[^"]*)", run as "(?P<program>[^"]*)", in "(?P<unit>[^"]*)"$'
)


def facts(ground: "Ground", pid: int) -> tuple[str, str, str]:
	"""The comm, first word of the command line and unit of process pid, as the guest has them."""
	comm = ground.shell(f"cat /proc/{pid}/comm").strip()
	program = ground.shell(f"tr '\\0' '\\n' < /proc/{pid}/cmdline | head -n 1").strip().split()[0]
	cgroup = ground.shell(f"cat /proc/{pid}/cgroup").strip()
	unified = [line for line in cgroup.splitlines() if line.startswith("0::")]
	unit = (unified or cgroup.splitlines())[0].rsplit("/", 1)[1]
	return comm, program, unit


def run(ground: "Ground") -> None:
	ground.openWindow()
	ground.waitForHelper()
	keyboard = ground.uhidKeyboard(UHID)

	for name, how in (("inX", "session"), ("outside", "userManager")):
		with ground.subtest(f"the admitted client is named by its comm and unit ({how})"):
			client = ground.keydClient(name, how)
			pid = client.pid
			comm, program, unit = facts(ground, pid)
			ground.machine.log(f"{name}: comm {comm!r}, program {program!r}, unit {unit!r}")
			if how == "session":
				assert unit.startswith("session-") and unit.endswith(".scope"), unit
			else:
				assert unit == f"{client.unit}.service", unit
			mark = ground.mark()
			assert client.connect()["welcome"]
			admitted = ground.waitForHelperLog(f"admitted process {pid} ", mark)
			said = [(t, line) for t, line in ground.helperLog(mark) if f": process {pid} is " in line]
			assert len(said) == 1, said
			ground.machine.log(f"the helper: {said[0][1]!r}, then {admitted[1]!r}")
			found = CLIENT_LINE.match(said[0][1])
			assert found, said
			assert found["comm"] == comm and found["program"] == program and found["unit"] == unit, (
				found.groupdict(),
				(comm, program, unit),
			)
			assert said[0][0] <= admitted[0], (said, admitted)
			client.call("close")

	with ground.subtest("nothing of the keys a client hears reaches the journal"):
		mark = ground.mark()
		client = ground.keydClient("typing")
		client.verdicts("pass", KEY_Q="keep", KEY_J="keep")
		client.capture(UHID, since=mark)
		typed = ["KEY_Q", "KEY_W", "KEY_J", "KEY_Z", "KEY_LEFTSHIFT", "KEY_X"]
		keyboard.tap(*typed)
		ground.waitFor(
			lambda: sum(1 for k in client.keys(mark) if k["value"] == 1) >= len(typed),
			"the client to hear every key",
		)
		client.stop()
		ground.waitForHelperLog("let go", mark)
		said = [line for _t, line in ground.helperLog(mark)]
		ground.machine.log(f"the helper said {len(said)} lines meanwhile")
		for line in said:
			assert "KEY_" not in line, line
			for word in ("key code", "keycode", "pressed", "released"):
				assert word not in line.lower(), line
