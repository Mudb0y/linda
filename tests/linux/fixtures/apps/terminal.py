"""MATE Terminal, titled "Terminal fixture", running bash with line editing, a short prompt and nothing
from the host's start-up files.

terminal.py [title]

The shell's prompt is "$ ". Its functions run the other terminal fixtures by short names, so that a
test types little:
	menu    runs terminalMenu.py, a full-screen list in curses
	secret  asks for a password that is not shown, then says how long it was
	lines N prints N numbered lines
"""

import glob
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
TITLE = "Terminal fixture"
PROMPT = "$ "
FUNCTIONS = {
	"menu": f"'{sys.executable}' '{HERE / 'terminalMenu.py'}'",
	"secret": "read -r -s -p 'Password: ' p; echo; echo \"${#p} characters\"",
	"lines": "seq -f 'line %g' \"$1\"",
}


def hasLineEditing(bash: str) -> bool:
	result = subprocess.run([bash, "--norc", "-c", "set -o emacs"], capture_output=True, check=False)
	return result.returncode == 0


def interactiveBash() -> str:
	"""A bash built with readline. The build environment's own bash has none, so the arrows would type
	escape sequences; nix keeps an interactive one in the store wherever a dev shell was entered."""
	candidates = [
		shutil.which("bash"),
		*sorted(glob.glob("/nix/store/*-bash-interactive-*/bin/bash"), reverse=True),
	]
	for candidate in candidates:
		if candidate and hasLineEditing(candidate):
			return candidate
	raise SystemExit("no bash with line editing found")


def main() -> None:
	title = sys.argv[1] if len(sys.argv) > 1 else TITLE
	env = dict(os.environ)
	env["TERM"] = "xterm-256color"
	env["PS1"] = PROMPT
	env["HISTFILE"] = "/dev/null"
	env["INPUTRC"] = "/dev/null"
	for name, body in FUNCTIONS.items():
		env[f"BASH_FUNC_{name}%%"] = f"() {{ {body}; }}"
	# --norc, because nixpkgs' bash reads /etc/bashrc first otherwise, which on NixOS replaces PATH.
	command = [
		"mate-terminal",
		"--disable-factory",
		f"--title={title}",
		"--geometry=80x24",
		"--",
		interactiveBash(),
		"--norc",
		"--noprofile",
		"-i",
	]
	os.execvpe(command[0], command, env)


if __name__ == "__main__":
	main()
