"""Runs NVDA's upstream unit tests, tests/unit, on Linux.

It finds the tests as upstream's runner does, through unittest's rules for test*.py files and
packages, but walks the tree itself so that a module named in unittest-exclusions.txt is never
imported: discovery would import it, and an excluded module can fail or have side effects at
import. Every other module is loaded by name. A class or single test named in the list still runs,
expected to fail, so that an entry which no longer holds fails the run; an entry that says
"Not run:" is dropped instead. A skip that the list does not expect also fails the run, as does any
write into the checkout and any load of an audio library.

Run it under the rig, after tools/build-artifacts:
	nix develop . -c tests/linux/rig python tests/linux/rununittests.py [-v] [--xml FILE]
"""

import argparse
import faulthandler
import importlib
import importlib.util
import io
import os
import re
import shutil
import sys
import tempfile
import threading
import traceback
import unittest
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
SOURCE = ROOT / "source"
EXCLUSIONS = HERE / "unittest-exclusions.txt"
TEST_FILE = re.compile(r"^test[A-Za-z0-9_]*\.py$")
PACKAGE = "tests.unit"
REASON_STARTS = ("For good", "Until")
NOT_RUN = "Not run:"
SKIP_KEYWORD = "skipped "
# Directories of the checkout that git and the development tools write to while other work goes on in
# the same checkout; the audit hook still refuses writes into them from the tests themselves.
UNWATCHED = {".git", ".claude", ".ruff_cache", os.path.join("engine", "target")}
# Libraries that can reach a sound server or a sound card. Whether any of them may be loaded under
# the rig is not decided, so a test that loads one fails the run instead of deciding it.
AUDIO_LIBRARIES = ("espeak", "pcaudio", "pulse", "asound", "pipewire", "jack", "portaudio")


@dataclass
class Entry:
	name: str
	reason: list[str] = field(default_factory=list)
	notRun: bool = False
	matched: int = 0


@dataclass
class EntryList:
	exclusions: dict[str, Entry]
	expectedSkips: dict[str, Entry]


def readEntries(path: Path, package: str) -> EntryList:
	"""Entries start at column 0; the indented lines beneath one are its reason. An entry is a
	dotted name to exclude, or "skipped" and a dotted name whose skips are expected."""
	entries = EntryList({}, {})
	current: Entry | None = None
	for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
		if not line.strip() or line.startswith("#"):
			continue
		if line[0].isspace():
			if current is None:
				raise ValueError(f"{path}:{number}: a reason with no entry above it")
			current.reason.append(line.strip())
			current.notRun = current.notRun or line.strip().startswith(NOT_RUN)
			continue
		table = entries.exclusions
		name = line.strip()
		if name.startswith(SKIP_KEYWORD):
			table = entries.expectedSkips
			name = name.removeprefix(SKIP_KEYWORD).strip()
		if not name.startswith(package + ".") or name in table:
			raise ValueError(f"{path}:{number}: {name!r} is not a new name under {package}")
		current = table[name] = Entry(name)
	for entry in entries.exclusions.values():
		if not entry.reason or not entry.reason[0].startswith(REASON_STARTS):
			raise ValueError(f"{path}: the reason for {entry.name} does not start with For good or Until")
	for entry in entries.expectedSkips.values():
		if not entry.reason:
			raise ValueError(f"{path}: the expected skip {entry.name} gives no reason")
		if entry.notRun:
			raise ValueError(f"{path}: {NOT_RUN!r} has no meaning for the expected skip {entry.name}")
	return entries


def findEntry(testId: str, table: dict[str, Entry]) -> Entry | None:
	for name, entry in table.items():
		if testId == name or testId.startswith(name + "."):
			return entry
	return None


def candidateModules(top: Path, directory: Path) -> Iterator[str]:
	"""Dotted names in the order unittest's discovery imports them: a package, then its contents
	sorted by name. A package that fails to import is not descended into, as in discovery."""
	parts = directory.relative_to(top).parts
	yield ".".join(parts)
	for entry in sorted(os.listdir(directory)):
		path = directory / entry
		if path.is_dir() and (path / "__init__.py").is_file():
			yield from candidateModules(top, path)
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
	expectedToFail: dict[str, Entry]
	notRun: list[str]


def collect(top: Path, package: str, exclusions: dict[str, Entry]) -> Collected:
	loader = unittest.TestLoader()
	collected = Collected(unittest.TestSuite(), [], {}, [])
	failedPackages: list[str] = []
	for name in candidateModules(top, top.joinpath(*package.split("."))):
		if any(name.startswith(failed + ".") for failed in failedPackages):
			continue
		entry = findEntry(name, exclusions)
		if entry is not None:
			entry.matched += 1
			collected.excludedModules.append(name)
			continue
		try:
			# Imported here rather than by loadTestsFromName, which turns a failed import into a test
			# and so hides that a package's contents must not be loaded.
			module = importlib.import_module(name)
		except unittest.SkipTest as e:
			collected.suite.addTest(_ModuleSkipped(name, str(e)))
			continue
		except Exception:  # noqa: BLE001
			# Reported as a failing test, as discovery would, so that one broken module does not hide
			# the results of the others.
			failedPackages.append(name)
			collected.suite.addTest(_ImportFailure(name, traceback.format_exc()))
			continue
		for test in flatten(loader.loadTestsFromModule(module)):
			entry = findEntry(test.id(), exclusions)
			if entry is None:
				collected.suite.addTest(test)
				continue
			entry.matched += 1
			if entry.notRun:
				collected.notRun.append(test.id())
				continue
			# unittest.TestCase.run reads this from the instance as well as from the test method.
			test.__unittest_expecting_failure__ = True
			collected.expectedToFail[test.id()] = entry
			collected.suite.addTest(test)
	return collected


# Ordered from best to worst; a test's outcome is the worst of everything reported for it.
OUTCOMES = ("passed", "skipped", "failed as expected", "passed unexpectedly", "failed", "errors")


def _ownId(test: unittest.TestCase) -> str:
	# A subtest is reported as an object of its own, but it is one of its parent test's outcomes.
	return getattr(test, "test_case", test).id()


class _Recording:
	"""Mixed into whichever result class runs the tests. It keeps one outcome for each test id and
	the reports of problems in plain unittest terms, because xmlrunner's result class keeps its own
	records in place of the test objects and a failing subtest is not a test of its own."""

	#: Ids of the tests that are run expecting failure.
	expectedToFail: frozenset[str] = frozenset()

	def __init__(self, *args, **kwargs) -> None:
		super().__init__(*args, **kwargs)
		self.outcomes: dict[str, str] = {}
		self.problems: list[str] = []
		self.fixtureErrors: list[str] = []
		self.skips: dict[str, str] = {}

	def _worsen(self, test: unittest.TestCase, outcome: str) -> None:
		testId = _ownId(test)
		current = self.outcomes.get(testId, "passed")
		if OUTCOMES.index(outcome) > OUTCOMES.index(current):
			self.outcomes[testId] = outcome

	def _problem(self, kind: str, test: unittest.TestCase, err) -> None:
		self.problems.append(f"{kind}: {test.id()}\n{self._exc_info_to_string(err, test)}")

	def printErrors(self) -> None:
		"""The runner prints the problems itself, the same way for every result class."""

	def startTest(self, test: unittest.TestCase) -> None:
		super().startTest(test)
		self.outcomes.setdefault(test.id(), "passed")

	def _expectedToFail(self, test: unittest.TestCase) -> bool:
		# unittest's expected-failure marking covers only the test method, not setUp and tearDown.
		return isinstance(test, unittest.TestCase) and test.id() in self.expectedToFail

	def addError(self, test: unittest.TestCase, err) -> None:
		if self._expectedToFail(test):
			self.addExpectedFailure(test, err)
			return
		# Class and module set-up and tear-down errors arrive as a stand-in that is not a TestCase.
		if isinstance(test, unittest.TestCase):
			self._worsen(test, "errors")
		else:
			self.fixtureErrors.append(test.id())
		self._problem("ERROR", test, err)
		super().addError(test, err)

	def addFailure(self, test: unittest.TestCase, err) -> None:
		if self._expectedToFail(test):
			self.addExpectedFailure(test, err)
			return
		self._worsen(test, "failed")
		self._problem("FAIL", test, err)
		super().addFailure(test, err)

	def addSubTest(self, test: unittest.TestCase, subtest: unittest.TestCase, err) -> None:
		if err is not None:
			failed = issubclass(err[0], test.failureException)
			self._worsen(test, "failed" if failed else "errors")
			self._problem("FAIL" if failed else "ERROR", subtest, err)
		super().addSubTest(test, subtest, err)

	def addSkip(self, test: unittest.TestCase, reason: str) -> None:
		self._worsen(test, "skipped")
		self.skips.setdefault(_ownId(test), reason)
		super().addSkip(test, reason)

	def addExpectedFailure(self, test: unittest.TestCase, err) -> None:
		self._worsen(test, "failed as expected")
		super().addExpectedFailure(test, err)

	def addUnexpectedSuccess(self, test: unittest.TestCase) -> None:
		self._worsen(test, "passed unexpectedly")
		super().addUnexpectedSuccess(test)


def treeState(top: Path) -> dict[str, tuple]:
	"""A directory's modification time changes whenever an entry is made or removed in it, so this
	also shows a file that was written and deleted again."""
	state: dict[str, tuple] = {}
	for dirPath, dirNames, fileNames in os.walk(top):
		dirNames[:] = [d for d in dirNames if os.path.relpath(os.path.join(dirPath, d), top) not in UNWATCHED]
		state[dirPath] = ("directory", os.lstat(dirPath).st_mtime_ns)
		for name in fileNames + [d for d in dirNames if os.path.islink(os.path.join(dirPath, d))]:
			path = os.path.join(dirPath, name)
			info = os.lstat(path)
			state[path] = (info.st_size, info.st_mtime_ns)
	return state


_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
# Audit events that change the file system: the position of each path argument, and of the directory
# descriptor that path is relative to, or None.
_PATH_EVENTS: dict[str, tuple[tuple[int, int | None], ...]] = {
	"os.mkdir": ((0, 2),),
	"os.remove": ((0, 1),),
	"os.rmdir": ((0, 1),),
	"os.rename": ((0, 2), (1, 3)),
	"os.link": ((0, 2), (1, 3)),
	"os.symlink": ((1, 2),),
	"os.truncate": ((0, None),),
	"os.chmod": ((0, 2),),
	"os.utime": ((0, 3),),
}


def _absolute(path, dirFd) -> str | None:
	if isinstance(path, int):
		return None
	path = os.fsdecode(path)
	if not os.path.isabs(path):
		# The events give -1 for "no directory descriptor".
		useFd = isinstance(dirFd, int) and dirFd >= 0
		base = os.readlink(f"/proc/self/fd/{dirFd}") if useFd else os.getcwd()
		path = os.path.join(base, path)
	return os.path.normpath(path)


# Events that act on a path that must exist already; one aimed at nothing changes nothing.
_ON_EXISTING = {"os.remove", "os.rmdir", "os.rename", "os.link", "os.truncate", "os.chmod", "os.utime"}


def writtenPaths(event: str, args: tuple) -> list[str]:
	"""The paths an audit event is about to change, as absolute paths."""
	if event == "open":
		path, mode, flags = args
		if isinstance(mode, str):
			writes = any(letter in mode for letter in "wax+")
		else:
			writes = bool(flags & _WRITE_FLAGS)
		paths = [_absolute(path, None)] if writes else []
	else:
		paths = [
			_absolute(args[pathAt], None if fdAt is None else args[fdAt])
			for pathAt, fdAt in _PATH_EVENTS.get(event, ())
		]
		if event in _ON_EXISTING and paths and paths[0] is not None and not os.path.lexists(paths[0]):
			return []
	return [path for path in paths if path is not None]


class Guard:
	"""Refuses, and records, every write into the checkout made in this process and every load of
	an audio library. Writes by child processes are found by comparing treeState before and after."""

	def __init__(self, root: Path, tests: Path):
		self.root = os.path.realpath(root)
		self.tests = os.path.realpath(tests)
		self.armed = True
		self.refused: list[str] = []

	def _inCheckout(self, path: str) -> bool:
		return path == self.root or path.startswith(self.root + os.sep)

	def _refuse(self, what: str) -> None:
		# The innermost test frame says which test it was; the refusal itself may be swallowed.
		frame = sys._getframe(2)
		while frame is not None and not frame.f_code.co_filename.startswith(self.tests + os.sep):
			frame = frame.f_back
		where = "" if frame is None else f", from {frame.f_code.co_filename}:{frame.f_lineno}"
		self.refused.append(what + where)

	def __call__(self, event: str, args: tuple) -> None:
		if not self.armed:
			return
		if event == "ctypes.dlopen" and isinstance(args[0], (str, bytes)):
			name = os.path.basename(os.fsdecode(args[0])).lower()
			if any(library in name for library in AUDIO_LIBRARIES):
				self._refuse(f"load of the audio library {args[0]}")
				raise OSError(f"rununittests: loading the audio library {args[0]} is refused")
			return
		if event != "open" and event not in _PATH_EVENTS:
			return
		for path in writtenPaths(event, args):
			if self._inCheckout(path):
				self._refuse(f"{event} of {path}")
				raise PermissionError(f"rununittests: {event} of {path} in the checkout is refused")


def _startWatchdog(seconds: float, workDir: str) -> threading.Event:
	finished = threading.Event()

	def watch() -> None:
		if finished.wait(seconds):
			return
		# sys.__stderr__, because while a test runs sys.stderr is its output buffer.
		print(
			f"rununittests: no result after {seconds:g} s; the stack of every thread follows",
			file=sys.__stderr__,
			flush=True,
		)
		faulthandler.dump_traceback(file=sys.__stderr__, all_threads=True)
		shutil.rmtree(workDir, ignore_errors=True)
		os._exit(1)

	threading.Thread(target=watch, name="rununittests watchdog", daemon=True).start()
	# For a hang that never releases the interpreter lock, which the thread above cannot interrupt.
	faulthandler.dump_traceback_later(seconds + 60, exit=True)
	return finished


def runSuite(collected: Collected, xml: bool, verbose: bool) -> tuple[_Recording, bytes | None]:
	"""Runs the suite, and returns the result and, if asked for, the JUnit XML report."""
	suite = collected.suite
	stream = sys.stdout if verbose else io.StringIO()
	verbosity = 2 if verbose else 0
	attributes = {"expectedToFail": frozenset(collected.expectedToFail)}
	if not xml:
		resultClass = type("RecordingResult", (_Recording, unittest.TextTestResult), attributes)
		runner = unittest.TextTestRunner(
			stream=stream, verbosity=verbosity, buffer=True, resultclass=resultClass
		)
		return runner.run(suite), None
	import xmlrunner
	from xmlrunner.result import _XMLTestResult

	report = io.BytesIO()
	resultClass = type("RecordingResult", (_Recording, _XMLTestResult), attributes)
	runner = xmlrunner.XMLTestRunner(
		output=report, stream=stream, verbosity=verbosity, buffer=True, resultclass=resultClass
	)
	return runner.run(suite), report.getvalue()


def _requireRig() -> None:
	# By path: tests/linux must stay off sys.path, where its synthDrivers would shadow NVDA's.
	spec = importlib.util.spec_from_file_location("harness", HERE / "harness.py")
	harness = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(harness)
	harness.requireRig()


def main() -> int:
	_requireRig()
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
	parser.add_argument(
		"--top",
		type=Path,
		default=ROOT,
		help="the directory the test package is imported from; for testing this runner",
	)
	parser.add_argument("--package", default=PACKAGE, help="the test package; for testing this runner")
	args = parser.parse_args()
	print("rununittests: start", flush=True)
	if sys.platform != "linux":
		print("rununittests: this runner is for Linux; upstream's is rununittests.bat", file=sys.stderr)
		return 2
	# Where upstream's editable install and discovery's top-level directory put them; never this
	# script's directory, whose synthDrivers package would shadow NVDA's.
	others = [path for path in sys.path if not path or os.path.realpath(path) != str(HERE)]
	sys.path[:] = [str(SOURCE), str(args.top.resolve()), *others]
	sys.dont_write_bytecode = True
	entries = readEntries(args.exclusions, args.package)
	before = treeState(ROOT)
	guard = Guard(ROOT, args.top.joinpath(*args.package.split(".")))
	sys.addaudithook(guard)
	# Every temporary file of the run, the bootstrap's configuration directory among them, goes
	# here, so that a run ended by its timeout leaves nothing behind either.
	workDir = tempfile.mkdtemp(prefix="rununittests-")
	os.environ["TMPDIR"] = tempfile.tempdir = workDir
	finished = _startWatchdog(args.timeout, workDir)
	try:
		return _run(args, entries, guard, before)
	finally:
		finished.set()
		faulthandler.cancel_dump_traceback_later()
		shutil.rmtree(workDir, ignore_errors=True)


def _run(args: argparse.Namespace, entries: EntryList, guard: Guard, before: dict[str, tuple]) -> int:
	try:
		# The package is the bootstrap every test module imports first; a failure here would
		# otherwise be repeated once for each of them.
		importlib.import_module(args.package)
	except Exception:  # noqa: BLE001
		traceback.print_exc()
		print("rununittests: the test package's bootstrap failed", file=sys.stderr)
		return 1
	collected = collect(args.top.resolve(), args.package, entries.exclusions)
	result, report = runSuite(collected, args.xml is not None, args.verbose)
	guard.armed = False

	status = 0
	for problem in result.problems:
		print(problem, file=sys.stderr)
		status = 1
	for testId, outcome in result.outcomes.items():
		if outcome != "passed unexpectedly":
			continue
		status = 1
		entry = collected.expectedToFail.get(testId)
		if entry is None:
			print(f"UNEXPECTED SUCCESS: {testId}", file=sys.stderr)
		else:
			print(
				f"UNEXPECTED SUCCESS: {testId} passes, so the exclusion {entry.name} is stale",
				file=sys.stderr,
			)
	for testId, reason in result.skips.items():
		entry = findEntry(testId, entries.expectedSkips)
		if entry is None:
			print(f"UNEXPECTED SKIP: {testId}: {reason}", file=sys.stderr)
			status = 1
		else:
			entry.matched += 1
	if args.verbose:
		for name in collected.excludedModules:
			print(f"excluded module: {name}")
		for name in collected.expectedToFail:
			print(f"excluded test, run expecting failure: {name}")
		for name in collected.notRun:
			print(f"excluded test, not run: {name}")

	for entry in (*entries.exclusions.values(), *entries.expectedSkips.values()):
		if not entry.matched:
			print(f"rununittests: entry matches nothing: {entry.name}", file=sys.stderr)
			status = 1
	for refused in guard.refused:
		print(f"rununittests: refused {refused}", file=sys.stderr)
		status = 1
	after = treeState(ROOT)
	changed = sorted(path for path in before.keys() | after.keys() if before.get(path) != after.get(path))
	for path in changed:
		print(f"rununittests: the run changed {path}", file=sys.stderr)
		status = 1

	counts = {outcome: 0 for outcome in OUTCOMES}
	for outcome in result.outcomes.values():
		counts[outcome] += 1
	print(
		f"rununittests: {len(result.outcomes)} tests ran: "
		+ ", ".join(f"{count} {outcome}" for outcome, count in counts.items()),
		flush=True,
	)
	print(f"rununittests: {len(result.fixtureErrors)} errors in class or module set-up and tear-down")
	print(
		f"rununittests: excluded {len(collected.excludedModules)} modules, not imported; "
		f"{len(collected.expectedToFail)} tests, run expecting failure; {len(collected.notRun)} tests, not run",
		flush=True,
	)
	if report is not None:
		# Written after the check of the checkout, as it may be asked for inside it.
		args.xml.parent.mkdir(parents=True, exist_ok=True)
		args.xml.write_bytes(report)
	return status


if __name__ == "__main__":
	sys.exit(main())
