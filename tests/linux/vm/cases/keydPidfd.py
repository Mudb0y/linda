"""Admission judges the very process that connected, by the pidfd the helper takes as it accepts the
connection. A process of the X session connects and ends, leaving the connection to a child of its
own; its number is then given to another process of the same session and user, and only then does
the connection say HELLO. Asked by number alone, logind, polkit and /proc would all describe that
other process, which the helper would admit; it turns the caller away as ended, asking neither. The
same caller still there when it says HELLO is welcomed, and polkit finds its unit by the pidfd the
helper gave it, which polkit can only do with a pidfd.

This file is also the program that runs the race in the guest, as root, which keydPidfd.nix installs
as ground-pidfd-race: ground-pidfd-race USER CGROUP ends|stays prints what happened as JSON.
"""

import json
import os
import pwd
import select
import signal
import socket
import struct
import sys
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
	from ground import Ground

SOCKET_PATH = "/run/linda-keyd/socket"
HELLO, WELCOME, GOODBYE = 0x01, 0x81, 0x82
GOODBYE_REASONS = {2: "NOT_IN_FRONT", 3: "NOT_AUTHORIZED", 4: "BUSY", 5: "NO_ANSWER", 6: "SILENT"}
RACE = "/run/current-system/sw/bin/ground-pidfd-race"
#: The tag keydPidfd.nix's polkit rule logs under.
POLKIT_TAG = "ground-polkit"
#: Long enough to outlast the race; each process is killed as soon as it is over.
LIFE = 60


def _become(user: str, cgroup: str) -> None:
	"""Make this process one of the session's: in its control group, as its user."""
	entry = pwd.getpwnam(user)
	with open(f"/sys/fs/cgroup{cgroup}/cgroup.procs", "w") as procs:
		procs.write(str(os.getpid()))
	os.initgroups(user, entry.pw_gid)
	os.setgid(entry.pw_gid)
	os.setuid(entry.pw_uid)


def _say(fd: int, what: dict) -> None:
	os.write(fd, json.dumps(what).encode() + b"\n")


def _hear(fd: int, timeout: float = 10) -> dict:
	data = b""
	while not data.endswith(b"\n"):
		if not select.select([fd], [], [], timeout)[0]:
			raise TimeoutError(f"nothing said within {timeout} s")
		chunk = os.read(fd, 4096)
		if not chunk:
			raise EOFError("the other end closed")
		data += chunk
	return json.loads(data)


def _answer(message: bytes) -> dict:
	if message[0] == WELCOME:
		return {"type": "WELCOME"}
	if message[0] == GOODBYE:
		_type, reason, _version = struct.unpack_from("<BBH", message)
		return {
			"type": "GOODBYE",
			"reason": GOODBYE_REASONS.get(reason, reason),
			"text": message[4:].decode(),
		}
	return {"type": message[0]}


def _hold(connection: socket.socket, go: int, out: int) -> None:
	"""The connection's keeper: says HELLO when told to, and tells what came back."""
	_hear(go, LIFE)
	connection.send(struct.pack("<BHI", HELLO, 1, 0))
	connection.settimeout(10)
	try:
		_say(out, {"answer": _answer(connection.recv(256))})
	except TimeoutError:
		_say(out, {"answer": None})
	time.sleep(LIFE)


def _kill(pid: int | None) -> None:
	if pid:
		try:
			os.kill(pid, signal.SIGKILL)
		except ProcessLookupError:
			pass


def _reap(pid: int | None) -> None:
	if pid:
		try:
			os.waitpid(pid, 0)
		except ChildProcessError:
			pass


def _takeNumber(number: int, user: str, cgroup: str) -> tuple[int, int, str]:
	"""Start a process of the session with the number given, which must be free; answers its number,
	how many tries it took, and its control group."""
	for attempt in range(1, 101):
		ready, readyOut = os.pipe()
		with open("/proc/sys/kernel/ns_last_pid", "w") as last:
			last.write(str(number - 1))
		pid = os.fork()
		if pid == 0:
			try:
				_become(user, cgroup)
				_say(readyOut, {"ready": True})
				time.sleep(LIFE)
			finally:
				os._exit(0)
		os.close(readyOut)
		if pid == number:
			_hear(ready)
			os.close(ready)
			with open(f"/proc/{pid}/cgroup") as f:
				return pid, attempt, f.read().strip()
		os.close(ready)
		_kill(pid)
		_reap(pid)
	raise AssertionError(f"no process could be given number {number}")


def race(user: str, cgroup: str, connectorEnds: bool) -> dict:
	held, heldOut = os.pipe()
	go, goIn = os.pipe()
	out, outIn = os.pipe()
	connector = os.fork()
	if connector == 0:
		try:
			_become(user, cgroup)
			connection = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
			connection.connect(SOCKET_PATH)
			holder = os.fork()
			if holder == 0:
				_hold(connection, go, outIn)
				os._exit(0)
			_say(heldOut, {"holder": holder})
			if not connectorEnds:
				time.sleep(LIFE)
		except (OSError, KeyError) as e:
			_say(heldOut, {"error": repr(e)})
		os._exit(0)
	result: dict = {"connector": connector, "impostor": None}
	holder = None
	impostor = None
	try:
		told = _hear(held)
		if "error" in told:
			raise AssertionError(f"the caller could not connect: {told['error']}")
		holder = told["holder"]
		result["holder"] = holder
		if connectorEnds:
			_reap(connector)
			# Its number is free from here on, and may belong to anything when the cleanup runs.
			number, connector = connector, None
			impostor, result["attempts"], result["impostorCgroup"] = _takeNumber(number, user, cgroup)
			result["impostor"] = impostor
		_say(goIn, {"go": True})
		result.update(_hear(out))
	finally:
		for pid in (holder, impostor, connector):
			_kill(pid)
		_reap(impostor)
		_reap(connector)
	return result


def _raceIn(ground: "Ground", cgroup: str, how: str) -> dict:
	from ground import USER

	result = json.loads(ground.shell(f"{RACE} {USER} {cgroup} {how}"))
	ground.machine.log(f"the race, the caller {how}: {result}")
	return result


def _polkitLog(ground: "Ground", since: float) -> list[str]:
	text = ground.shell(f"journalctl -o json --no-pager -t {POLKIT_TAG} 2>/dev/null || true")
	lines = []
	for line in text.splitlines():
		if not line.strip():
			continue
		entry = json.loads(line)
		message = entry.get("MESSAGE")
		if isinstance(message, list):
			message = bytes(message).decode(errors="replace")
		if int(entry["__MONOTONIC_TIMESTAMP"]) / 1e6 >= since and "ground: process" in str(message):
			lines.append(str(message))
	return lines


def run(ground: "Ground") -> None:
	ground.openWindow()
	ground.waitForHelper()
	session = ground.shell("loginctl show-seat seat0 -P ActiveSession").strip()
	unit = f"session-{session}.scope"
	cgroup = ground.shell(f"systemctl show {unit} -P ControlGroup").strip()
	assert cgroup.endswith(f"/{unit}"), cgroup

	with ground.subtest("a caller that ended, whose number a process of the session took, is turned away"):
		mark = ground.mark()
		result = _raceIn(ground, cgroup, "ends")
		caller = result["connector"]
		assert result["impostor"] == caller, result
		assert result["impostorCgroup"].endswith(f"/{unit}"), result
		answer = result["answer"]
		assert answer and answer["type"] == "GOODBYE", result
		assert answer["reason"] == "NOT_IN_FRONT" and answer["text"] == f"process {caller} has ended", answer
		ground.waitForHelperLog(f"turned away process {caller} ", mark)
		said = [line for _t, line in ground.helperLog(mark)]
		assert not any("admitted process" in line or f"process {caller} is" in line for line in said), said
		asked = _polkitLog(ground, mark.t)
		assert not asked, f"polkit was asked about the process that took the number: {asked}"

	with ground.subtest("the same caller still there is welcomed, and polkit is given its pidfd"):
		mark = ground.mark()
		result = _raceIn(ground, cgroup, "stays")
		caller = result["connector"]
		assert result["answer"] == {"type": "WELCOME"}, result
		said = ground.waitForHelperLog(f"admitted process {caller} ", mark)[1]
		assert said.endswith(f"of session {session}"), said
		asked = ground.waitFor(lambda: _polkitLog(ground, mark.t), "polkit's rule to log the question")
		ground.machine.log(f"polkit: {asked}")
		assert asked == [f"ground: process {caller} is in unit {unit}"], asked

	with ground.subtest("every caller was held by a pidfd"):
		said = [line for _t, line in ground.helperLog()]
		assert not any("judged by their start time alone" in line for line in said), said


if __name__ == "__main__":
	user, cgroup, how = sys.argv[1:]
	print(json.dumps(race(user, cgroup, how == "ends")))
