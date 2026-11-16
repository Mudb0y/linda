"""Runs a piece of NVDA code in a fresh interpreter and reports what it did.

NVDA's modules keep their state per process and import in one order only, so a test that needs
them running starts a child for each case. The child reports the value the code left in
``result``, any exception, every Windows or unfinished function that was called, and every log
record at ERROR or above. Upstream swallows many exceptions, so a path that merely returns has
proved little; a test should require it to be clean, with ChildResult.assertClean.

From a test: harness.runChild(tmp_path, code).
As the child: harness.py RESULT_JSON PREPARE CODE_FILE.
"""

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import traceback
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
SOURCE = ROOT / "source"
PROTECTED = (SOURCE, ROOT / "tests" / "unit")

#: How much of NVDA the child sets up before the code runs.
#: "none": source/ on sys.path and nothing else.
#: "bootstrap": also linuxPlatform.bootstrap.install().
#: "config": also what nvda.pyw and core.main establish before their own imports, namely the
#: gettext builtins, appDir, a temporary configuration directory and config.initialize().
PREPARE_LEVELS = ("none", "bootstrap", "config")


@dataclass
class ChildResult:
	returnCode: int
	value: object = None
	error: str | None = None
	noLinuxImplementation: list[str] = field(default_factory=list)
	logErrors: list[str] = field(default_factory=list)
	stdout: str = ""
	stderr: str = ""

	def assertClean(self) -> None:
		"""The code returned, called nothing that Linux lacks, and logged no error."""
		assert self.error is None, self.error
		assert not self.noLinuxImplementation, (
			f"called without a Linux implementation: {self.noLinuxImplementation}"
		)
		assert not self.logErrors, f"logged at ERROR or above: {self.logErrors}"


def whyNotInRig() -> str | None:
	"""What shows that this process is outside tests/linux/rig, or None when it is inside.

	The tests start screen readers, open windows and signal whatever holds the display's lock. In
	the rig's sandbox none of that can reach the desktop the machine's owner is using; outside it,
	all of it would.
	"""
	if os.environ.get("BASEDREADER_RIG") != "1":
		return "BASEDREADER_RIG is not set"
	# The variable alone could have been copied into another shell. These hold only in the sandbox.
	try:
		firstProcess = Path("/proc/1/comm").read_text(encoding="ascii").strip()
	except OSError:
		firstProcess = ""
	if firstProcess != "bwrap":
		return "process 1 is not the sandbox's"
	if os.path.exists("/run/user"):
		return "the live session's runtime directory is visible"
	return None


def requireRig() -> None:
	why = whyNotInRig()
	if why is not None:
		sys.exit(
			f"refusing to run outside the rig ({why}). Use: nix develop . -c tests/linux/rig COMMAND",
		)


def treeState() -> dict[str, tuple]:
	state: dict[str, tuple] = {}
	for top in PROTECTED:
		for dirPath, _dirNames, fileNames in os.walk(top):
			state[dirPath] = ("directory",)
			for name in fileNames:
				path = os.path.join(dirPath, name)
				info = os.lstat(path)
				state[path] = (info.st_size, info.st_mtime_ns)
	return state


def runChild(
	tmpPath: Path,
	code: str,
	*,
	prepare: str = "config",
	env: dict[str, str] | None = None,
	timeout: float = 300,
) -> ChildResult:
	"""Run code in a fresh interpreter and fail if it changed source/ or tests/unit/.

	The code runs as a module body. Whatever it leaves in the name ``result`` comes back as
	ChildResult.value and has to survive JSON.
	"""
	requireRig()
	assert prepare in PREPARE_LEVELS, prepare
	work = Path(tempfile.mkdtemp(prefix="child-", dir=tmpPath))
	codePath = work / "code.py"
	codePath.write_text(textwrap.dedent(code), encoding="utf-8")
	resultPath = work / "result.json"
	before = treeState()
	done = subprocess.run(
		[sys.executable, "-I", "-B", str(Path(__file__).resolve()), str(resultPath), prepare, str(codePath)],
		capture_output=True,
		text=True,
		timeout=timeout,
		env={**os.environ, **(env or {})},
		cwd=work,
		check=False,
	)
	after = treeState()
	changed = sorted(path for path in before.keys() | after.keys() if before.get(path) != after.get(path))
	assert not changed, f"the child changed the repository: {changed}"
	assert resultPath.exists(), (
		f"the child wrote no result (exit status {done.returncode})\n{done.stdout[-3000:]}\n{done.stderr[-3000:]}"
	)
	outcome = json.loads(resultPath.read_text(encoding="utf-8"))
	return ChildResult(returnCode=done.returncode, stdout=done.stdout, stderr=done.stderr, **outcome)


def _child(resultPath: str, prepare: str, codePath: str) -> None:
	requireRig()
	called: list[str] = []
	logErrors: list[str] = []
	outcome: dict = {"value": None, "error": None, "noLinuxImplementation": called, "logErrors": logErrors}

	def onAudit(event: str, args: tuple) -> None:
		if event == "linuxPlatform.noLinuxImplementation":
			called.append(str(args[0]))

	sys.addaudithook(onAudit)
	sys.dont_write_bytecode = True
	try:
		if prepare == "config":
			# -I keeps the script's directory off sys.path.
			sys.path.insert(0, str(HERE))
			import importSurfaceProbe

			importSurfaceProbe.prepare()
			import logging

			from logHandler import log

			class Collector(logging.Handler):
				def emit(self, record: logging.LogRecord) -> None:
					logErrors.append(f"{record.levelname}: {record.getMessage()}")

			log.addHandler(Collector(level=logging.ERROR))
		else:
			sys.path.insert(0, str(SOURCE))
			if prepare == "bootstrap":
				from linuxPlatform import bootstrap

				bootstrap.install()
		namespace: dict = {"__name__": "__harness__", "result": None}
		with open(codePath, encoding="utf-8") as f:
			# The code is the test's own, written beside the call to runChild.
			exec(compile(f.read(), codePath, "exec"), namespace)  # noqa: S102
		outcome["value"] = namespace.get("result")
	# SystemExit and KeyboardInterrupt included: whatever ended the code is the result to report.
	except BaseException:  # noqa: BLE001
		outcome["error"] = traceback.format_exc()
	with open(resultPath, "w", encoding="utf-8") as f:
		json.dump(outcome, f, default=repr)


if __name__ == "__main__":
	_child(*sys.argv[1:4])
