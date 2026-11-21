"""The compositor's driver in a Wayland guest: which window is active, and whether the desktop is ready,
asked of the module for the guest's kind, which /etc/ground/guest.json names; for kind gnome that is
focusGnome.py beside this file.

	groundFocus.py ready
	groundFocus.py active
	groundFocus.py windows
	groundFocus.py activate TITLE
	groundFocus.py facts
	groundFocus.py devices UID

All but devices run in the session, through guest/groundSession.py, and answer one JSON value on
standard output. ready exits with status 0 once the desktop takes keys for its windows, and 1 until
then. active answers the active top-level window as windows lists it, or null. activate answers
{"activated": true} when the kind's module moved the focus itself, and {"activated": false} when it
cannot, which tells the ground to move it with keys. facts answers what the kind's module knows about
the desktop, for the log.

devices runs as root and answers the names of the input devices the processes of user UID hold open,
which in a Wayland session are the compositor's: the device a key is sent to must be among them, or
the compositor loses the key, as the X server does before it lists a device.
"""

import importlib
import json
import os
import sys

GUEST = "/etc/ground/guest.json"


def kind() -> str:
	with open(GUEST) as f:
		return json.load(f)["kind"]


def devices(uid: int) -> list[str]:
	names: set[str] = set()
	for pid in os.listdir("/proc"):
		if not pid.isdigit():
			continue
		try:
			if os.stat(f"/proc/{pid}").st_uid != uid:
				continue
			fds = os.listdir(f"/proc/{pid}/fd")
		except OSError:
			continue
		for fd in fds:
			try:
				target = os.readlink(f"/proc/{pid}/fd/{fd}")
				if not target.startswith("/dev/input/event"):
					continue
				# A node already removed reads "(deleted)" and has no sysfs entry left.
				with open(f"/sys/class/input/{os.path.basename(target)}/device/name") as f:
					names.add(f.read().strip())
			except OSError:
				continue
	return sorted(names)


def main() -> int:
	op = sys.argv[1] if len(sys.argv) > 1 else ""
	if op == "devices" and len(sys.argv) == 3:
		print(json.dumps(devices(int(sys.argv[2]))))
		return 0
	module = importlib.import_module("focus" + kind().capitalize())
	if op == "ready" and len(sys.argv) == 2:
		ready = module.ready()
		print(json.dumps(ready))
		return 0 if ready else 1
	if op == "active" and len(sys.argv) == 2:
		print(json.dumps(next((w for w in module.windows() if w["active"]), None)))
		return 0
	if op == "windows" and len(sys.argv) == 2:
		print(json.dumps(module.windows()))
		return 0
	if op == "activate" and len(sys.argv) == 3:
		activate = getattr(module, "activate", None)
		print(json.dumps({"activated": bool(activate and activate(sys.argv[2]))}))
		return 0
	if op == "facts" and len(sys.argv) == 2:
		print(json.dumps(module.facts()))
		return 0
	sys.exit("usage: groundFocus.py ready | active | windows | activate TITLE | facts | devices UID")


if __name__ == "__main__":
	sys.exit(main())
