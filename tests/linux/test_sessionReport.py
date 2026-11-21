"""linda --session-report: what decides how NVDA runs in a session, for a tester to read out.

Each case runs the launcher with the session's variables as input, under the rig:

	nix develop . -c tests/linux/rig python3 -m pytest tests/linux/test_sessionReport.py
"""

import os
import socket
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path

import harness
import pytest

#: Every variable that changes the report; each case sets what it names and unsets the rest.
SESSION_VARIABLES = (
	"DISPLAY",
	"WAYLAND_DISPLAY",
	"XDG_SESSION_TYPE",
	"XDG_CURRENT_DESKTOP",
	"GAMESCOPE_WAYLAND_DISPLAY",
	"LINDA_SESSION_TYPE",
	"HYPRLAND_INSTANCE_SIGNATURE",
	"NIRI_SOCKET",
	"SWAYSOCK",
	"LINDA_TEST_HOOKS_DIR",
	"LINDA_TEST_KEYD_SOCKET",
)
HELPER_SOCKET = "/run/linda-keyd/socket"

pytestmark = pytest.mark.rigSession("x11")


def report(env: dict[str, str], *arguments: str) -> subprocess.CompletedProcess:
	harness.requireRig()
	environment = {name: value for name, value in os.environ.items() if name not in SESSION_VARIABLES}
	return subprocess.run(
		[sys.executable, "-I", "-B", str(harness.LAUNCHER), "--session-report", *arguments],
		env={**environment, **env},
		capture_output=True,
		check=False,
		text=True,
		timeout=60,
		stdin=subprocess.DEVNULL,
	)


@pytest.fixture
def shortDir() -> Iterator[Path]:
	"""A directory in the rig's runtime directory, whose path is short enough for a socket's."""
	harness.requireRig()
	with tempfile.TemporaryDirectory(dir=os.environ["XDG_RUNTIME_DIR"]) as directory:
		yield Path(directory)


def helperLine(path: str = HELPER_SOCKET) -> str:
	there = "is there" if Path(path).is_socket() else "is not there"
	return f"linda-keyd's socket, {path}, {there}."


def test_ownersSession():
	# As the owner's screen reader printed its environment on 21 November 2026.
	done = report({"DISPLAY": ":0", "XDG_SESSION_TYPE": "x11", "XDG_CURRENT_DESKTOP": "MATE"})
	assert (done.returncode, done.stderr) == (0, ""), done
	assert done.stdout.splitlines() == [
		"DISPLAY=:0",
		"WAYLAND_DISPLAY is not set.",
		"XDG_SESSION_TYPE=x11",
		"XDG_CURRENT_DESKTOP=MATE",
		"GAMESCOPE_WAYLAND_DISPLAY is not set.",
		"Session kind: x11 by rule 2: XDG_SESSION_TYPE is x11 and DISPLAY is set.",
		"Desktop: other.",
		helperLine(),
	]


def test_waylandSessionWithXwayland(shortDir: Path):
	runtime = shortDir
	with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as compositor:
		compositor.bind(str(runtime / "wayland-1"))
		done = report(
			{
				"XDG_RUNTIME_DIR": str(runtime),
				"DISPLAY": ":1",
				"WAYLAND_DISPLAY": "wayland-1",
				"XDG_SESSION_TYPE": "wayland",
				"XDG_CURRENT_DESKTOP": "GNOME",
			}
		)
	assert (done.returncode, done.stderr) == (0, ""), done
	assert done.stdout.splitlines() == [
		"DISPLAY=:1",
		"WAYLAND_DISPLAY=wayland-1",
		"XDG_SESSION_TYPE=wayland",
		"XDG_CURRENT_DESKTOP=GNOME",
		"GAMESCOPE_WAYLAND_DISPLAY is not set.",
		"Session kind: wayland by rule 4: WAYLAND_DISPLAY names a socket that is there.",
		"Desktop: gnome.",
		helperLine(),
	]


def test_noDisplayStillReports():
	done = report({})
	assert (done.returncode, done.stderr) == (0, ""), done
	lines = done.stdout.splitlines()
	assert lines[5] == "Session kind: none by rule 7: neither DISPLAY nor a Wayland socket is there."


def test_helperSocketAsATestNamesIt(shortDir: Path):
	hooks = shortDir / "hooks"
	hooks.mkdir()
	path = shortDir / "keyd"
	common = {"DISPLAY": ":0", "LINDA_TEST_HOOKS_DIR": str(hooks), "LINDA_TEST_KEYD_SOCKET": str(path)}
	assert report(common).stdout.splitlines()[-1] == f"linda-keyd's socket, {path}, is not there."
	with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as helper:
		helper.bind(str(path))
		assert report(common).stdout.splitlines()[-1] == f"linda-keyd's socket, {path}, is there."
	path.unlink()
	path.write_text("")
	assert (
		report(common).stdout.splitlines()[-1]
		== f"linda-keyd's socket, {path}, is there but is not a socket."
	)


def test_holdsOnlyWhatDecidesTheSession():
	# A report is read out and pasted where anyone can read it: the five variables, the decision, the
	# desktop and the helper, and nothing else, whatever else the environment holds.
	done = report({"DISPLAY": ":0", "XDG_CURRENT_DESKTOP": "MATE", "LINDA_PRIVATE_PROBE": "probeword"})
	assert done.returncode == 0, done
	assert "probeword" not in done.stdout + done.stderr
	prefixes = (
		"DISPLAY",
		"WAYLAND_DISPLAY",
		"XDG_SESSION_TYPE",
		"XDG_CURRENT_DESKTOP",
		"GAMESCOPE_WAYLAND_DISPLAY",
		"Session kind: ",
		"Desktop: ",
		"linda-keyd's socket, ",
	)
	lines = done.stdout.splitlines()
	assert [line.startswith(prefix) for line, prefix in zip(lines, prefixes, strict=True)] == [True] * 8


def test_takesNoOtherArgument():
	done = report({"DISPLAY": ":0"}, "--debug-logging")
	assert (done.returncode, done.stdout) == (2, "")
	assert done.stderr == "linda: --session-report takes no other argument, so nothing was done\n"
