"""Runs NVDA's upstream unit tests, tests/unit, on Linux.

It finds the tests as upstream's runner does, through unittest's rules for test*.py files and
packages, but walks the tree itself so that a module named in unittest-exclusions.txt is never
imported: discovery would import it, and an excluded module can fail or have side effects at
import. Every other module is loaded by name. Single tests and classes named in the list are
dropped after their module loads.

Run it under the rig, after tools/build-artifacts:
	nix develop . -c tests/linux/rig python tests/linux/rununittests.py [-v] [--xml FILE]
"""

import argparse
import faulthandler
import importlib.util
import io
import os
import re
import sys
import traceback
import unittest
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
SOURCE = ROOT / "source"
UNIT = ROOT / "tests" / "unit"
EXCLUSIONS = HERE / "unittest-exclusions.txt"
TEST_FILE = re.compile(r"^test[A-Za-z0-9_]*\.py$")
PACKAGE = "tests.unit"


@dataclass
class Exclusion:
	name: str
	reason: list[str] = field(default_factory=list)
	matched: int = 0


def readExclusions(path: Path) -> dict[str, Exclusion]:
	"""Entries start at column 0; the indented lines beneath one are its reason."""
	entries: dict[str, Exclusion] = {}
	current: Exclusion | None = None
	for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
		if not line.strip() or line.startswith("#"):
			continue
		if line[0].isspace():
			if current is None:
				raise ValueError(f"{path}:{number}: a reason with no entry above it")
			current.reason.append(line.strip())
			continue
		name = line.strip()
		if not name.startswith(PACKAGE + ".") or name in entries:
			raise ValueError(f"{path}:{number}: {name!r} is not a new name under {PACKAGE}")
		current = entries[name] = Exclusion(name)
	for entry in entries.values():
		if not entry.reason:
			raise ValueError(f"{path}: {entry.name} gives no reason")
	return entries


def findExclusion(testId: str, exclusions: dict[str, Exclusion]) -> Exclusion | None:
	for name, entry in exclusions.items():
		if testId == name or testId.startswith(name + "."):
			return entry
	return None


def candidateModules(directory: Path) -> Iterator[str]:
	"""Dotted names in the order unittest's discovery imports them: a package, then its contents
	sorted by name. A package that fails to import is not descended into, as in discovery."""
	parts = directory.relative_to(ROOT).parts
	yield ".".join(parts)
	for entry in sorted(os.listdir(directory)):
		path = directory / entry
		if path.is_dir() and (path / "__init__.py").is_file():
			yield from candidateModules(path)
		elif path.is_file() and TEST_FILE.match(entry):
			yield ".".join((*parts, entry[:-3]))


class _ImportFailure(unittest.TestCase):
	"""Stands for a test module that failed to import, so that the failure counts as an error."""

	def __init__(self, moduleName: str, formatted: str):
		super().__init__("runTest")
		self.moduleName = moduleName
		self.formatted = formatted

	def id(self) -> str:
		return self.moduleName

	def __str__(self) -> str:
		return f"import of {self.moduleName}"

	def runTest(self) -> None:
		raise ImportError(f"{self.moduleName} failed to import:\n{self.formatted}")


class _ModuleSkipped(unittest.TestCase):
	"""Stands for a test module that raised SkipTest as it was imported."""

	def __init__(self, moduleName: str, reason: str):
		super().__init__("runTest")
		self.moduleName = moduleName
		self.reason = reason

	def id(self) -> str:
		return self.moduleName

	def runTest(self) -> None:
		self.skipTest(self.reason)


def flatten(suite: unittest.TestSuite) -> Iterator[unittest.TestCase]:
	for test in suite:
		if isinstance(test, unittest.TestSuite):
			yield from flatten(test)
		else:
			yield test


@dataclass
class Collected:
	suite: unittest.TestSuite
	excludedModules: list[str]
	excludedTests: list[str]


def collect(exclusions: dict[str, Exclusion]) -> Collected:
	loader = unittest.TestLoader()
	suite = unittest.TestSuite()
	excludedModules: list[str] = []
	excludedTests: list[str] = []
	failedPackages: list[str] = []
	for name in candidateModules(UNIT):
		if any(name.startswith(package + ".") for package in failedPackages):
			continue
		entry = findExclusion(name, exclusions)
		if entry is not None:
			entry.matched += 1
			excludedModules.append(name)
			continue
		try:
			# Imported here rather than by loadTestsFromName, which turns a failed import into a test
			# and so hides that a package's contents must not be loaded.
			module = importlib.import_module(name)
		except unittest.SkipTest as e:
			suite.addTest(_ModuleSkipped(name, str(e)))
			continue
		except Exception:  # noqa: BLE001
			# Reported as a failing test, as discovery would, so that one broken module does not hide
			# the results of the others.
			failedPackages.append(name)
			suite.addTest(_ImportFailure(name, traceback.format_exc()))
			continue
		for test in flatten(loader.loadTestsFromModule(module)):
			entry = findExclusion(test.id(), exclusions)
			if entry is None:
				suite.addTest(test)
			else:
				entry.matched += 1
				excludedTests.append(test.id())
	return Collected(suite, excludedModules, excludedTests)


def _loadHarness():
	# By path, because tests/linux on sys.path would put its synthDrivers ahead of NVDA's.
	spec = importlib.util.spec_from_file_location("_linuxTestHarness", HERE / "harness.py")
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module


def _testId(test: unittest.TestCase) -> str:
	# A failing subtest is reported as its own object; it is one of its parent's outcomes.
	return getattr(test, "test_case", test).id()


def main() -> int:
	parser = argparse.ArgumentParser(description="Run NVDA's unit tests on Linux.")
	parser.add_argument("-v", "--verbose", action="store_true", help="list every test and exclusion")
	parser.add_argument("--xml", type=Path, help="also write a JUnit XML report to this file")
	parser.add_argument(
		"--exclusions",
		type=Path,
		default=EXCLUSIONS,
		help="the exclusion list; /dev/null runs every test",
	)
	parser.add_argument(
		"--timeout",
		type=float,
		default=900,
		help="seconds before a hung run is ended with every thread's stack",
	)
	args = parser.parse_args()
	print("rununittests: start", flush=True)
	if sys.platform != "linux":
		print("rununittests: this runner is for Linux; upstream's is rununittests.bat", file=sys.stderr)
		return 2
	# Where upstream's editable install and discovery's top-level directory put them; never this
	# script's directory, whose synthDrivers package would shadow NVDA's.
	sys.path[0:1] = [str(SOURCE), str(ROOT)]
	sys.dont_write_bytecode = True
	faulthandler.dump_traceback_later(args.timeout, exit=True)
	harness = _loadHarness()
	before = harness.treeState()

	exclusions = readExclusions(args.exclusions)
	try:
		# The package is the bootstrap every test module imports first; a failure here would
		# otherwise be repeated once for each of them.
		importlib.import_module(PACKAGE)
	except Exception:  # noqa: BLE001
		traceback.print_exc()
		print("rununittests: the tests/unit bootstrap failed", file=sys.stderr)
		return 1
	collected = collect(exclusions)

	stream = sys.stdout if args.verbose else io.StringIO()
	if args.xml:
		import xmlrunner

		args.xml.parent.mkdir(parents=True, exist_ok=True)
		with open(args.xml, "wb") as report:
			runner = xmlrunner.XMLTestRunner(
				output=report, stream=stream, verbosity=2 if args.verbose else 0, buffer=True
			)
			result = runner.run(collected.suite)
	else:
		runner = unittest.TextTestRunner(stream=stream, verbosity=2 if args.verbose else 0, buffer=True)
		result = runner.run(collected.suite)
	faulthandler.cancel_dump_traceback_later()

	if not args.verbose:
		for kind, problems in (("ERROR", result.errors), ("FAIL", result.failures)):
			for test, formatted in problems:
				print(f"{kind}: {test}\n{formatted}", file=sys.stderr)
		for test in result.unexpectedSuccesses:
			print(f"UNEXPECTED SUCCESS: {test}", file=sys.stderr)
	if args.verbose:
		for name in collected.excludedModules:
			print(f"excluded module: {name}")
		for name in collected.excludedTests:
			print(f"excluded test: {name}")

	status = 0 if result.wasSuccessful() else 1
	stale = [entry.name for entry in exclusions.values() if not entry.matched]
	for name in stale:
		print(f"rununittests: exclusion matches nothing: {name}", file=sys.stderr)
		status = 1
	after = harness.treeState()
	changed = sorted(path for path in before.keys() | after.keys() if before.get(path) != after.get(path))
	for path in changed:
		print(f"rununittests: the run changed {path}", file=sys.stderr)
		status = 1

	notPassed = {
		_testId(test)
		for test, _ in (*result.errors, *result.failures, *result.skipped, *result.expectedFailures)
	} | {_testId(test) for test in result.unexpectedSuccesses}
	print(
		f"rununittests: {result.testsRun - len(notPassed)} passed, {len(result.failures)} failed, "
		f"{len(result.errors)} errors, {len(result.skipped)} skipped, "
		f"{len(collected.excludedModules)} modules and {len(collected.excludedTests)} tests excluded",
		flush=True,
	)
	return status


if __name__ == "__main__":
	sys.exit(main())
