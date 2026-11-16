"""NVDA's upstream Python imports on Linux after linuxPlatform.bootstrap.install().

Each test starts importSurfaceProbe.py in a fresh interpreter, because what is under test is the
state of a whole process. Run under the rig:

	nix develop . -c tests/linux/rig python3 -m pytest -p no:cacheprovider tests/linux/test_importSurface.py
"""

import ast
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
PROBE = HERE / "importSurfaceProbe.py"
ALLOWED_FAILURES = HERE / "importSurfaceAllowedFailures.txt"
#: Nothing that runs NVDA code may write here.
PROTECTED = (ROOT / "source", ROOT / "tests" / "unit")


def treeState() -> dict[str, tuple]:
	state: dict[str, tuple] = {}
	for top in PROTECTED:
		for dirPath, dirNames, fileNames in os.walk(top):
			state[dirPath] = ("directory",)
			for name in fileNames:
				path = os.path.join(dirPath, name)
				info = os.lstat(path)
				state[path] = (info.st_size, info.st_mtime_ns)
	return state


def runProbe(tmpPath: Path, *args: str) -> dict:
	resultPath = tmpPath / "result.json"
	before = treeState()
	done = subprocess.run(
		[sys.executable, "-I", "-B", str(PROBE), str(resultPath), *args],
		capture_output=True,
		text=True,
		timeout=900,
		env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
		check=False,
	)
	after = treeState()
	# The probe's audit hook sees writes from its own process only; this also sees its children's.
	changed = sorted(path for path in before.keys() | after.keys() if before.get(path) != after.get(path))
	assert not changed, f"the probe changed the repository: {changed}"
	assert resultPath.exists(), (
		f"the probe wrote no result (exit status {done.returncode})\n{done.stdout[-3000:]}\n{done.stderr[-3000:]}"
	)
	result = json.loads(resultPath.read_text(encoding="utf-8"))
	assert "error" not in result, (
		f"the probe failed outside the step under test:\n{result['error']['traceback']}"
	)
	assert not result["writeViolations"], f"wrote into the repository: {result['writeViolations']}"
	return result


def failedCoreSteps(result: dict) -> list[dict]:
	return [step for step in result["core"] if step["status"] not in ("ok", "handled")]


def describe(failure: dict) -> str:
	return f"{failure['type']}: {failure['message']} (at {failure['where']})\n{failure['traceback']}"


@dataclass
class AllowedFailures:
	"""importSurfaceAllowedFailures.txt, read.

	Modules are listed in groups, each with one reason. Under the reason, a group may list modules
	that import cleanly but import one of the group's modules when they run, each with a sentence
	saying whether and why that is safe.
	"""

	group: dict[str, int] = field(default_factory=dict)
	reasons: list[str] = field(default_factory=list)
	firstModule: list[str] = field(default_factory=list)
	callers: dict[tuple[int, str], str] = field(default_factory=dict)


def readAllowedFailures() -> AllowedFailures:
	allowed = AllowedFailures()
	pending: list[str] = []
	reasonLines: list[str] = []
	callerLines: dict[str, list[str]] = {}
	caller: str | None = None

	def settle() -> None:
		assert reasonLines, f"no reason given for {pending}"
		index = len(allowed.reasons)
		allowed.reasons.append(" ".join(reasonLines))
		allowed.firstModule.append(pending[0])
		for module in pending:
			allowed.group[module] = index
		for name, lines in callerLines.items():
			why = " ".join(lines)
			assert why.startswith(("safe", "not safe")), f"say first whether {name} is safe: {why!r}"
			allowed.callers[(index, name)] = why
		pending.clear()
		reasonLines.clear()
		callerLines.clear()

	for line in ALLOWED_FAILURES.read_text(encoding="utf-8").splitlines():
		if not line.strip() or line.startswith("#"):
			continue
		if not line[0].isspace():
			if reasonLines:
				settle()
				caller = None
			module = line.strip()
			assert module not in allowed.group and module not in pending, f"{module} is listed twice"
			pending.append(module)
			continue
		assert pending, f"a reason with no module above it: {line.strip()!r}"
		text = line.strip()
		if text.startswith("reached from "):
			caller, _, why = text.removeprefix("reached from ").partition(":")
			assert caller not in callerLines, f"{caller} is listed twice under {pending[0]}"
			callerLines[caller] = [why.strip()] if why.strip() else []
		elif caller is not None:
			callerLines[caller].append(text)
		else:
			reasonLines.append(text)
	if pending:
		settle()
	return allowed


def _isTypeChecking(test: ast.expr) -> bool:
	return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
		isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
	)


def _windowsTest(test: ast.expr) -> bool | None:
	"""True for `sys.platform == "win32"`, False for `sys.platform != "win32"`, None otherwise."""
	if not (
		isinstance(test, ast.Compare)
		and len(test.ops) == 1
		and isinstance(test.left, ast.Attribute)
		and test.left.attr == "platform"
		and isinstance(test.left.value, ast.Name)
		and test.left.value.id == "sys"
		and isinstance(test.comparators[0], ast.Constant)
		and test.comparators[0].value == "win32"
	):
		return None
	if isinstance(test.ops[0], ast.Eq):
		return True
	if isinstance(test.ops[0], ast.NotEq):
		return False
	return None


def importedModules(path: Path, module: str) -> set[str]:
	"""Every module an import statement anywhere in the file names, at module level or inside a
	function, and every package on the way to it. Imports only a type checker sees, and imports
	under a test that sys.platform is Windows, are left out."""
	tree = ast.parse(path.read_text(encoding="utf-8"))
	package = module.split(".") if path.name == "__init__.py" else module.split(".")[:-1]
	names: set[str] = set()

	def visit(nodes: list[ast.AST]) -> None:
		for node in nodes:
			if isinstance(node, ast.If) and _isTypeChecking(node.test):
				visit(node.orelse)
				continue
			if isinstance(node, ast.If) and (onWindows := _windowsTest(node.test)) is not None:
				visit(node.orelse if onWindows else node.body)
				continue
			if isinstance(node, ast.Import):
				names.update(alias.name for alias in node.names)
			elif isinstance(node, ast.ImportFrom):
				base = node.module or ""
				if node.level:
					parts = package[: len(package) - node.level + 1]
					base = ".".join([*parts, base] if base else parts)
				names.add(base)
				names.update(f"{base}.{alias.name}" for alias in node.names)
			visit(list(ast.iter_child_nodes(node)))

	visit(tree.body)
	return {".".join(name.split(".")[:i]) for name in names for i in range(1, name.count(".") + 2)}


@pytest.fixture(scope="module")
def sweep(tmp_path_factory: pytest.TempPathFactory) -> dict:
	result = runProbe(tmp_path_factory.mktemp("sweep"), "sweep")
	assert not failedCoreSteps(result), "core.main()'s imports fail; see test_coreMainImportsInOrder"
	assert "results" in result, result
	return result


def test_coreMainImportsInOrder(tmp_path: Path):
	"""Every import core.main() performs succeeds, in core.main()'s order."""
	result = runProbe(tmp_path, "core")
	assert len(result["core"]) > 40, "found too few imports in core.main(); has core.py changed shape?"
	failures = failedCoreSteps(result)
	assert not failures, "\n\n".join(
		f"{step['step']} (core.py:{step['line']})\n{describe(step)}" for step in failures
	)


def test_sweepMatchesAllowedFailures(sweep: dict):
	"""Every module under source/, imported after core.main()'s imports, fails only if it is listed."""
	allowed = readAllowedFailures()
	failed = {module: r for module, r in sweep["results"].items() if r["status"] == "fail"}
	imported = {module for module, r in sweep["results"].items() if r["status"] == "ok"}
	unexpected = {module: r for module, r in failed.items() if module not in allowed.group}
	stale = sorted(module for module in allowed.group if module in imported)
	missing = sorted(module for module in allowed.group if module not in sweep["results"])
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
	if sweep["windowsModulesLoaded"]:
		report.append(
			f"importing these put winreg, msvcrt or _winapi in sys.modules: {sweep['windowsModulesLoaded']}",
		)
	assert not report, "\n\n".join(report)
	print(f"{len(imported)} modules import, {len(failed)} are allowed to fail")


def test_keptCodeReachesAllowedFailuresOnlyWhereListed(sweep: dict):
	"""A module that imports can still import a failing one later, inside a function. Each such
	caller is listed under the failing module's group, saying whether that is safe."""
	allowed = readAllowedFailures()
	unlisted = []
	reached: set[tuple[int, str]] = set()
	for module, r in sorted(sweep["results"].items()):
		if r["status"] != "ok" or not r.get("file"):
			continue
		for target in sorted(importedModules(Path(r["file"]), module)):
			index = allowed.group.get(target)
			if index is None:
				continue
			reached.add((index, module))
			if (index, module) not in allowed.callers:
				unlisted.append(f"{module} imports {target}")
	stale = sorted(
		f"{caller}, under {allowed.firstModule[index]}"
		for index, caller in allowed.callers
		if (index, caller) not in reached
	)
	report = []
	if unlisted:
		report.append(
			"modules that import cleanly but import a module allowed to fail, with no entry saying whether "
			"that is safe:\n" + "\n".join(unlisted),
		)
	if stale:
		report.append("listed as reaching a module allowed to fail, but no longer do:\n" + "\n".join(stale))
	assert not report, "\n\n".join(report)


@pytest.mark.parametrize("module", ["winreg", "msvcrt", "_winapi"])
def test_windowsStdlibModulesAbsent(tmp_path: Path, module: str):
	"""The standard library changes behaviour when these import, so they must never exist."""
	found = runProbe(tmp_path, "hazard", "windowsModulesAbsent")["hazard"]["found"][module]
	assert found == {"inSysModules": False, "findable": False}


def test_shadowsRefused(tmp_path: Path):
	"""A file named after a refused module, anywhere on sys.path or added to a replaced package's
	search path, never imports."""
	hazard = runProbe(tmp_path, "hazard", "shadowsRefused")["hazard"]
	assert hazard["paths"] == {"NVDAHelper": [], "comtypes.gen": []}
	imported = {module: o for module, o in hazard["outcomes"].items() if o["raised"] != "ModuleNotFoundError"}
	assert not imported, imported


def test_mimetypesKnowsHtml(tmp_path: Path):
	"""mimetypes reads the Windows registry when winreg imports; with none, it uses its own table."""
	assert runProbe(tmp_path, "hazard", "mimetypes")["hazard"]["guess"] == "text/html"


def test_subprocessRuns(tmp_path: Path):
	"""subprocess takes its Windows code path when msvcrt imports."""
	hazard = runProbe(tmp_path, "hazard", "subprocess")["hazard"]
	assert hazard == {"returncode": 0, "stdout": "subprocess ran"}


def test_louisLoadedThroughCdll(tmp_path: Path):
	"""liblouis's binding chooses windll when it can import it, so it must load before bootstrap adds it."""
	hazard = runProbe(tmp_path, "hazard", "louisLoader")["hazard"]
	assert hazard["loaderIsCdll"]
	assert hazard["functypeIsCfunctype"]
	assert hazard["libraryType"] == "CDLL"
	assert hazard["version"]


def test_installWithoutLouisFails(tmp_path: Path):
	"""Without liblouis nothing can start, so install() says so and installs nothing."""
	result = runProbe(tmp_path, "noLouis")
	assert result["raised"] == "RuntimeError"
	assert "liblouis" in result["message"]
	assert not result["windllAdded"]
	assert not result["windowsErrorAdded"]
	assert not result["findersAdded"]


def test_standInCallsRaise(tmp_path: Path):
	"""Binding to Windows functions works; calling any of them raises NoLinuxImplementation, a
	NotImplementedError, and announces it as an audit event even when the caller swallows it."""
	hazard = runProbe(tmp_path, "hazard", "standInCalls")["hazard"]
	notRaising = {
		call: o
		for call, o in hazard["outcomes"].items()
		if o["raised"] != "NoLinuxImplementation" or not o["isNotImplementedError"]
	}
	assert not notRaising, notRaising
	assert "kernel32.GetTickCount" in hazard["outcomes"]["ctypes.windll.kernel32.GetTickCount()"]["message"]
	assert len(hazard["audited"]) == len(hazard["outcomes"]), hazard["audited"]
	assert "kernel32.GetTickCount" in hazard["audited"]
	assert hazard["callbackIsCFuncPtr"], "WINFUNCTYPE applied to a Python callable must make a real callback"
	assert hazard["callbackResult"] == 42
	# A caller that truth-tests a library, as the Lilli braille driver's check() does, asks whether
	# it can be used.
	assert not any(hazard["truthiness"].values()), hazard["truthiness"]
	# copy, pickle and ctypes itself probe private and special names; a Windows function in answer
	# would mislead them.
	assert hazard["privateAttribute"]["raised"] == "AttributeError"


#: Stand-in functions that build COM method declarations for an interface's _methods_; they call
#: nothing.
COM_DECLARATION_BUILDERS = {"comtypes.COMMETHOD", "comtypes.DISPMETHOD", "comtypes.STDMETHOD"}
#: Stand-in classes that are neither COM objects nor ctypes types, and construct: a DISPID is an
#: integer, and AudioUtilities is only a namespace for static methods, each of which raises.
CONSTRUCTIBLE_CLASSES = {"comtypes.dispid", "pycaw.utils.AudioUtilities"}
#: Class categories that only declare a type: constructing one calls nothing in Windows.
DECLARING_CATEGORIES = {"exception", "enum", "interface", "ctypesType"}


def test_standInSurfaceNeverAnswers(tmp_path: Path):
	"""Every function, method and property a stand-in defines raises NoLinuxImplementation, and so
	does constructing any class of it that is not a type declaration; the only values are integer
	constants. A new stand-in name that returned something plausible would fail here."""
	surface = runProbe(tmp_path, "hazard", "standInSurface")["hazard"]
	answering = {}
	for name, o in surface.items():
		raisedNoLinux = o.get("raised") == "NoLinuxImplementation" and o.get("isNotImplementedError")
		if o["kind"] == "value":
			ok = o["type"] == "int"
		elif name in COM_DECLARATION_BUILDERS:
			ok = o["raised"] is None and o["returned"].startswith("(")
		elif o["kind"] == "class" and (o["category"] in DECLARING_CATEGORIES or name in CONSTRUCTIBLE_CLASSES):
			ok = True
		else:
			ok = raisedNoLinux
		if not ok:
			answering[name] = o
	assert not answering, answering
	listed = COM_DECLARATION_BUILDERS | CONSTRUCTIBLE_CLASSES
	assert listed <= surface.keys(), f"listed here but no longer in the stand-ins: {listed - surface.keys()}"
	assert {"comtypes.GUID.from_progid", "comtypes.automation.tagVARIANT.value"} <= surface.keys()


def test_guidIsAValue(tmp_path: Path):
	"""A GUID is data, not a call into Windows, so it compares, hashes and prints as on Windows."""
	assert runProbe(tmp_path, "hazard", "guid")["hazard"] == {
		"str": "{618736E0-3C3D-11CF-810C-00AA00389B71}",
		"repr": 'GUID("{618736E0-3C3D-11CF-810C-00AA00389B71}")',
		"size": 16,
		"nullIsFalse": True,
		"setIsTrue": True,
		"equalByValue": True,
		"unequal": True,
		"notEqualToText": True,
		"hashByValue": True,
	}


def test_finderRefusesBadTables(tmp_path: Path):
	"""A mistake in the finder's tables stops the install rather than making a module importable
	that must not be, or quietly leaving one out."""
	hazard = runProbe(tmp_path, "hazard", "finderTables")["hazard"]
	assert hazard["asListed"]["raised"] is None
	assert {name: o["raised"] for name, o in hazard.items() if name != "asListed"} == {
		"neverProvidedListed": "RuntimeError",
		"listedTwice": "RuntimeError",
		"keptWithoutReplacedPackage": "RuntimeError",
		"noFile": "FileNotFoundError",
	}


def test_hotPathsAnswerWithoutError(tmp_path: Path):
	"""The lock-screen check runs on every focus and navigator change, and IAccessibleHandler.pumpAll
	on every core pump cycle; neither may raise or log an error."""
	hazard = runProbe(tmp_path, "hazard", "lockScreen")["hazard"]
	assert hazard["objectBelowLockScreen"] == {"raised": None, "returned": "False"}
	assert hazard["IAccessibleHandler.pumpAll"] == {"raised": None, "returned": "None"}
	assert not {"ERROR", "CRITICAL"} & set(hazard["logLevels"]), hazard["logLevels"]


def test_windowsErrorIsDistinct(tmp_path: Path):
	"""An alias of OSError would send every Linux OSError down upstream's `exc.winerror` paths."""
	hazard = runProbe(tmp_path, "hazard", "windowsError")["hazard"]
	assert hazard == {
		"osErrorIsWindowsError": False,
		"isOSErrorSubclass": True,
		"isOSError": False,
		"winerror": None,
		"givenWinerror": 5,
		"givenErrno": 2,
	}


def test_installTwiceIsHarmless(tmp_path: Path):
	hazard = runProbe(tmp_path, "hazard", "installTwice")["hazard"]
	assert hazard["before"] == hazard["after"]
	assert hazard["after"]["finders"] == 1


@pytest.mark.parametrize(
	("condition", "raised", "ctypesNames"),
	[
		("winregImported", "RuntimeError", []),
		# The condition itself puts HRESULT there.
		("ctypesNameTaken", "RuntimeError", ["HRESULT"]),
		("windowsErrorTaken", "RuntimeError", []),
		("missingReplacement", "FileNotFoundError", []),
	],
)
def test_refusedInstallChangesNothing(tmp_path: Path, condition: str, raised: str, ctypesNames: list[str]):
	"""A refusal a caller catches must not leave the Windows names in ctypes without the finder:
	upstream modules imported then would bind against them and be told a Windows library loaded."""
	result = runProbe(tmp_path, "refuseWhen", condition)
	assert result["raised"] == [raised, raised]
	assert not result["installed"]
	assert result["ctypesNames"] == ctypesNames
	assert not result["windowsErrorIsLinux"]
	assert result["findersAdded"] == 0


def test_installRefusesOffLinux(tmp_path: Path):
	result = runProbe(tmp_path, "refuse")
	assert result["raised"] == "RuntimeError"
	assert not result["windllAdded"]
