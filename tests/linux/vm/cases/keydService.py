"""The service as the flake's NixOS module installs it: the unit's settings as systemd has them, what
systemd-analyze security makes of its sandbox, the process's user, groups and capabilities, the
socket's directory and mode, /dev/uinput's ACL, the polkit action, and the helper's refusal to run as
root. The analysis goes to the log.
"""

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
	from ground import Ground

UNIT = "linda-keyd.service"
ACTION = "io.github.mudb0y.linda.keyd.capture"


def run(ground: "Ground") -> None:
	pid = ground.waitForHelper()

	with ground.subtest("the unit as systemd has it"):
		properties = dict(
			line.split("=", 1)
			for line in ground.shell(
				f"systemctl show {UNIT} -p Type -p User -p Group -p SupplementaryGroups -p Restart -p WatchdogUSec "
				"-p TimeoutAbortUSec -p WatchdogSignal -p OOMScoreAdjust -p LimitCORE -p NoNewPrivileges -p CapabilityBoundingSet "
				"-p RuntimeDirectory -p UnitFileState -p NotifyAccess"
			).splitlines()
		)
		ground.machine.log(f"{properties}")
		expected = {
			"Type": "notify",
			"User": "linda-keyd",
			"Group": "linda-keyd",
			"SupplementaryGroups": "input",
			"Restart": "always",
			"WatchdogUSec": "2s",
			"TimeoutAbortUSec": "1s",
			"WatchdogSignal": "9",
			"OOMScoreAdjust": "-900",
			"LimitCORE": "0",
			"NoNewPrivileges": "yes",
			"CapabilityBoundingSet": "",
			"RuntimeDirectory": "linda-keyd",
			"UnitFileState": "enabled",
			"NotifyAccess": "main",
		}
		assert {k: properties.get(k) for k in expected} == expected, properties

	with ground.subtest("what systemd-analyze security says of its sandbox"):
		report = ground.machine.execute(f"systemd-analyze security --no-pager {UNIT}")[1]
		ground.machine.log(report)
		exposure = float(re.search(r"Overall exposure level for \S+: ([0-9.]+)", report)[1])
		assert exposure <= 1.0, exposure

	with ground.subtest("the process: its user, groups and capabilities, and what it may reach"):
		status = dict(line.split(":", 1) for line in ground.shell(f"cat /proc/{pid}/status").splitlines())
		uid = ground.shell("id -u linda-keyd").strip()
		inputGid = ground.shell("getent group input | cut -d: -f3").strip()
		ground.machine.log(
			f"Uid {status['Uid'].split()}, Groups {status['Groups'].split()}, CapEff {status['CapEff'].strip()}"
		)
		assert status["Uid"].split() == [uid] * 4, status["Uid"]
		primaryGid = ground.shell("id -g linda-keyd").strip()
		assert sorted(status["Groups"].split()) == sorted([inputGid, primaryGid]), status["Groups"]
		assert int(status["CapEff"], 16) == 0 and int(status["CapPrm"], 16) == 0, status["CapEff"]
		# Not dumpable, so its /proc entries are root's.
		assert ground.shell(f"stat -c %U /proc/{pid}/environ").strip() == "root"
		# /dev/uinput it has from systemd, opened, with no access of its own; its standard input and
		# output are /dev/null after it has taken the device to a descriptor of its own.
		held = ground.shell(f"ls -l /proc/{pid}/fd | grep -c ' -> /dev/uinput$' || true").strip()
		assert held == "1", ground.shell(f"ls -l /proc/{pid}/fd")
		assert ground.shell(f"readlink /proc/{pid}/fd/0 /proc/{pid}/fd/1").split() == ["/dev/null"] * 2
		assert "linda-keyd" not in ground.shell("getfacl -cp /dev/uinput")
		socket = ground.shell("stat -c '%A %U' /run/linda-keyd/socket /run/linda-keyd").splitlines()
		ground.machine.log(f"the socket and its directory: {socket}")
		assert socket == ["srw-rw-rw- linda-keyd", "drwxr-xr-x linda-keyd"], socket
		assert ground.shell(f"pkaction --action-id {ACTION} --verbose | grep implicit").split() == [
			"implicit",
			"any:",
			"no",
			"implicit",
			"inactive:",
			"no",
			"implicit",
			"active:",
			"yes",
		]
		# The sandbox lets it follow the console in front, which it says when it cannot.
		threads = ground.shell(f"cat /proc/{pid}/task/*/comm").split()
		ground.machine.log(f"its threads: {threads}")
		assert "console" in threads, threads
		assert not [line for _t, line in ground.helperLog() if "the console in front" in line]

	with ground.subtest("the helper refuses to run as root"):
		program = ground.shell(
			f"systemctl show -P ExecStart {UNIT} | grep -o 'path=[^ ;]*' | cut -d= -f2"
		).strip()
		status, output = ground.machine.execute(f"{program} 2>&1")
		ground.machine.log(f"{program} as root: status {status}, {output.strip()!r}")
		assert status == 1 and "never as root" in output, output
		assert pid == ground.helperPid()
