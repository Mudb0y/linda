"""NVDA's upstream Python imports on Linux after linuxPlatform.bootstrap.install().

Each test starts importSurfaceProbe.py in a fresh interpreter, because what is under test is the
state of a whole process. Run under the rig:

	nix develop . -c tests/linux/rig python3 -m pytest -p no:cacheprovider tests/linux/test_importSurface.py
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
PROBE = HERE / "importSurfaceProbe.py"
ALLOWED_FAILURES = HERE / "importSurfaceAllowedFailures.txt"


def runProbe(tmpPath: Path, *args: str) -> dict:
	resultPath = tmpPath / "result.json"
	done = subprocess.run(
		[sys.executable, "-I", "-B", str(PROBE), str(resultPath), *args],
		capture_output=True,
		text=True,
		timeout=900,
		env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
	)
	assert resultPath.exists(), (
		f"the probe wrote no result (exit status {done.returncode})\n{done.stdout[-3000:]}\n{done.stderr[-3000:]}"
	)
	result = json.loads(resultPath.read_text(encoding="utf-8"))
	assert "error" not in result, f"the probe failed outside the step under test:\n{result['error']['traceback']}"
	assert not result["writeViolations"], f"wrote into the repository: {result['writeViolations']}"
	return result


def failedCoreSteps(result: dict) -> list[dict]:
	return [step for step in result["core"] if step["status"] not in ("ok", "handled")]


def describe(failure: dict) -> str:
	return f"{failure['type']}: {failure['message']} (at {failure['where']})\n{failure['traceback']}"


def readAllowedFailures() -> dict[str, str]:
	"""Module name to reason. A reason applies to the modules listed above it since the last one."""
	allowed: dict[str, str] = {}
	pending: list[str] = []
	reasonLines: list[str] = []

	def settle():
		for module in pending:
			allowed[module] = " ".join(reasonLines)
		pending.clear()
		reasonLines.clear()

	for line in ALLOWED_FAILURES.read_text(encoding="utf-8").splitlines():
		if not line.strip() or line.startswith("#"):
			continue
		if line[0].isspace():
			assert pending, f"a reason with no module above it: {line.strip()!r}"
			reasonLines.append(line.strip())
			continue
		if reasonLines:
			settle()
		module = line.strip()
		assert module not in allowed and module not in pending, f"{module} is listed twice"
		pending.append(module)
	assert not pending or reasonLines, f"no reason given for {pending}"
	settle()
	return allowed


def test_coreMainImportsInOrder(tmp_path):
	"""Every import core.main() performs succeeds, in core.main()'s order."""
	result = runProbe(tmp_path, "core")
	assert len(result["core"]) > 40, "found too few imports in core.main(); has core.py changed shape?"
	failures = failedCoreSteps(result)
	assert not failures, "\n\n".join(f"{step['step']} (core.py:{step['line']})\n{describe(step)}" for step in failures)


def test_sweepMatchesAllowedFailures(tmp_path):
	"""Every module under source/, imported after core.main()'s imports, fails only if it is listed."""
	result = runProbe(tmp_path, "sweep")
	assert not failedCoreSteps(result), "core.main()'s imports fail; see test_coreMainImportsInOrder"
	assert "results" in result, result
	allowed = readAllowedFailures()
	failed = {module: r for module, r in result["results"].items() if r["status"] == "fail"}
	imported = {module for module, r in result["results"].items() if r["status"] == "ok"}
	unexpected = {module: r for module, r in failed.items() if module not in allowed}
	stale = sorted(module for module in allowed if module in imported)
	missing = sorted(module for module in allowed if module not in result["results"])
	report = []
	if unexpected:
		report.append(
			"modules that fail to import and are not in importSurfaceAllowedFailures.txt:\n"
			+ "\n".join(f"{module}: {describe(r)}" for module, r in sorted(unexpected.items())),
		)
	if stale:
		report.append("listed as allowed to fail, but now import; remove them:\n" + "\n".join(stale))
	if missing:
		report.append("listed as allowed to fail, but no such module exists:\n" + "\n".join(missing))
	assert not report, "\n\n".join(report)
	print(f"{len(imported)} modules import, {len(failed)} are allowed to fail")


@pytest.mark.parametrize("module", ["winreg", "msvcrt", "_winapi"])
def test_windowsStdlibModulesAbsent(tmp_path, module):
	"""The standard library changes behaviour when these import, so they must never exist."""
	found = runProbe(tmp_path, "hazard", "windowsModulesAbsent")["hazard"]["found"][module]
	assert found == {"inSysModules": False, "findable": False}


def test_mimetypesKnowsHtml(tmp_path):
	"""mimetypes reads the Windows registry when winreg imports; with none, it uses its own table."""
	assert runProbe(tmp_path, "hazard", "mimetypes")["hazard"]["guess"] == "text/html"


def test_subprocessRuns(tmp_path):
	"""subprocess takes its Windows code path when msvcrt imports."""
	hazard = runProbe(tmp_path, "hazard", "subprocess")["hazard"]
	assert hazard == {"returncode": 0, "stdout": "subprocess ran"}


def test_louisLoadedThroughCdll(tmp_path):
	"""liblouis's binding chooses windll when it can import it, so it must load before bootstrap adds it."""
	hazard = runProbe(tmp_path, "hazard", "louisLoader")["hazard"]
	assert hazard["loaderIsCdll"]
	assert hazard["functypeIsCfunctype"]
	assert hazard["libraryType"] == "CDLL"
	assert hazard["version"]


def test_standInCallsRaise(tmp_path):
	"""Binding to Windows functions works; calling any of them raises NotImplementedError."""
	hazard = runProbe(tmp_path, "hazard", "standInCalls")["hazard"]
	notRaising = {call: o for call, o in hazard["outcomes"].items() if o["raised"] != "NotImplementedError"}
	assert not notRaising, notRaising
	assert "kernel32.GetTickCount" in hazard["outcomes"]["ctypes.windll.kernel32.GetTickCount()"]["message"]
	assert hazard["callbackIsCFuncPtr"], "WINFUNCTYPE applied to a Python callable must make a real callback"
	assert hazard["callbackResult"] == 42


def test_windowsErrorIsDistinct(tmp_path):
	"""An alias of OSError would send every Linux OSError down upstream's `exc.winerror` paths."""
	hazard = runProbe(tmp_path, "hazard", "windowsError")["hazard"]
	assert hazard == {
		"osErrorIsWindowsError": False,
		"isOSErrorSubclass": True,
		"isOSError": False,
		"winerror": None,
	}


def test_installTwiceIsHarmless(tmp_path):
	hazard = runProbe(tmp_path, "hazard", "installTwice")["hazard"]
	assert hazard["before"] == hazard["after"]
	assert hazard["after"]["finders"] == 1


def test_installRefusesOffLinux(tmp_path):
	result = runProbe(tmp_path, "refuse")
	assert result["raised"] == "RuntimeError"
	assert not result["windllAdded"]
