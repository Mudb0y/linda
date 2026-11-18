"""Starts programs inside the guest's signed-in X session, for a test script that reaches the guest only
as root through the test framework's shell.

The X session's own start-up runs "serve", so that what it starts belongs to that session as a
program the desktop's autostart starts does: same user, same environment, and the session's own
logind session, which is the one in front on the seat. A process started from the root shell with su
or systemd-run would be in another session or in none, and a key capture helper that admits only the
active session's processes would refuse it.

	groundSession.py serve
	groundSession.py call JSON

A request is one JSON object on a line, answered by one: {"op": "start", "name", "argv", "env"}
starts a program with its standard output and error in OUTPUT/NAME.out and NAME.err and answers its
pid; {"op": "run", "argv"} runs a short command and answers its exit status and output;
{"op": "signal", "name", "signal"} signals it; {"op": "status", "name"} answers its pid and exit
status, null while it runs; {"op": "env"} answers the session's environment. An answer with "error"
makes call exit with status 1.
"""

import json
import os
import signal
import socket
import subprocess
import sys

DIRECTORY = "/tmp/ground"
SOCKET = os.path.join(DIRECTORY, "session.sock")
OUTPUT = os.path.join(DIRECTORY, "jobs")


def _handle(request: dict, jobs: dict[str, subprocess.Popen]) -> dict:
	op = request.get("op")
	if op == "ping":
		return {"ok": True}
	if op == "env":
		return {"env": dict(os.environ)}
	if op == "run":
		completed = subprocess.run(
			request["argv"],
			capture_output=True,
			text=True,
			timeout=request.get("timeout", 30),
			check=False,
		)
		return {"returncode": completed.returncode, "stdout": completed.stdout, "stderr": completed.stderr}
	if op == "start":
		name = request["name"]
		if name in jobs and jobs[name].poll() is None:
			return {"error": f"{name} is still running"}
		env = {**os.environ, **request.get("env", {})}
		with (
			open(os.path.join(OUTPUT, f"{name}.out"), "wb") as out,
			open(os.path.join(OUTPUT, f"{name}.err"), "wb") as err,
		):
			process = subprocess.Popen(
				request["argv"],
				env=env,
				cwd=request.get("cwd") or DIRECTORY,
				stdin=subprocess.DEVNULL,
				stdout=out,
				stderr=err,
			)
		jobs[name] = process
		return {"pid": process.pid}
	process = jobs.get(request.get("name", ""))
	if process is None:
		return {"error": f"no job {request.get('name')!r}"}
	if op == "signal":
		if process.poll() is None:
			process.send_signal(signal.Signals[request.get("signal", "SIGTERM")])
		return {"pid": process.pid}
	if op == "status":
		return {"pid": process.pid, "returncode": process.poll()}
	return {"error": f"unknown op {op!r}"}


def serve() -> None:
	os.makedirs(OUTPUT, exist_ok=True)
	if os.path.exists(SOCKET):
		os.unlink(SOCKET)
	server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
	# Bound under a temporary name and renamed, so that the socket appearing means it accepts.
	temporary = f"{SOCKET}.{os.getpid()}"
	server.bind(temporary)
	os.chmod(temporary, 0o600)
	server.listen(4)
	os.rename(temporary, SOCKET)
	jobs: dict[str, subprocess.Popen] = {}
	while True:
		connection, _address = server.accept()
		with connection, connection.makefile("rwb") as stream:
			line = stream.readline()
			try:
				answer = _handle(json.loads(line), jobs)
			except Exception as e:  # noqa: BLE001
				answer = {"error": repr(e)}
			stream.write(json.dumps(answer).encode() + b"\n")
			stream.flush()
		for process in jobs.values():
			process.poll()


def call(text: str) -> int:
	with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
		client.connect(SOCKET)
		with client.makefile("rwb") as stream:
			stream.write(json.dumps(json.loads(text)).encode() + b"\n")
			stream.flush()
			answer = stream.readline().decode()
	print(answer, end="")
	return 1 if "error" in json.loads(answer) else 0


if __name__ == "__main__":
	if sys.argv[1:2] == ["serve"]:
		serve()
	elif sys.argv[1:2] == ["call"] and len(sys.argv) == 3:
		sys.exit(call(sys.argv[2]))
	else:
		sys.exit("usage: groundSession.py serve | call JSON")
