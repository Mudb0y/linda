"""What decides how NVDA runs in this session, as linda --session-report prints it for a tester to read
out or paste into a report.

The variables that decide the session's kind, the kind and the rule that decided it, the desktop as
NVDA tells it apart, and whether linda-keyd's socket is there. Like displayServer, it reads only the
environment and the file system, and opens no connection; nothing it prints can hold anything typed.
"""

import os
import stat
import sys
from typing import NoReturn

from . import displayServer


def _helperSocket() -> str:
	from .keys import helperSocketPath

	path = helperSocketPath()
	try:
		mode = os.stat(path).st_mode
	except FileNotFoundError:
		return f"linda-keyd's socket, {path}, is not there."
	except OSError as e:
		return f"linda-keyd's socket, {path}, cannot be looked at: {e.strerror}."
	if not stat.S_ISSOCK(mode):
		return f"linda-keyd's socket, {path}, is there but is not a socket."
	return f"linda-keyd's socket, {path}, is there."


def lines() -> list[str]:
	return [
		*displayServer.environmentFacts(),
		f"Session kind: {displayServer.reason()}.",
		f"Desktop: {displayServer.compositor()}.",
		_helperSocket(),
	]


def main() -> NoReturn:
	print("\n".join(lines()), flush=True)
	sys.exit(0)
