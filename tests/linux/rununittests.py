"""Runs NVDA's upstream unit tests, tests/unit, on Linux.

It finds the tests as upstream's runner does, through unittest's rules for test*.py files and
packages, but walks the tree itself so that a module named in unittest-exclusions.txt is never
imported into the run: discovery would import it, and an excluded module can fail or have side effects
at import. Every exclusion states the cause it expects, and the run fails once that cause no longer
holds. An excluded class or test, or every test of an excluded module whose tests are run, must fail
with the exception its entry names, or the run fails. A module or package whose import is expected to
fail is imported in a process of its own, which is then thrown away, and must fail there as its entry
names. An entry that says "Not run:" is dropped instead. A skip that the list does not expect also
fails the run, as does any write into the checkout, any load of an audio library, and a thread that a
test leaves running.

The tests run in a child process that records its result only once the run has reached its end, so
that a test, fixture or module that ends the interpreter, by whatever means, fails the run.

Run it under the rig, after tools/build-artifacts:
	nix develop . -c tests/linux/rig python tests/linux/rununittests.py [-v] [--xml FILE]
"""

import argparse
import contextlib
import faulthandler
import functools
import importlib
import importlib.util
import io
import json
import mmap
import os
import re
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import types
import unittest
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar, NoReturn

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
SOURCE = ROOT / "source"
RUNNER = Path(__file__).resolve()
EXCLUSIONS = HERE / "unittest-exclusions.txt"
TEST_FILE = re.compile(r"^test[A-Za-z0-9_]*\.py$")
PACKAGE = "tests.unit"
REASON_STARTS = ("For good", "Until")
NOT_RUN = "Not run:"
SKIP_KEYWORD = "skipped "
EXPECTATION = re.compile(r"^(Raises|Import raises): ([A-Za-z_][A-Za-z0-9_]*): (.+)$")
# Directories of the checkout that git and the development tools write to while other work goes on in
# the same checkout; the audit hook still refuses writes into them from the tests themselves.
UNWATCHED = {".git", ".claude", ".ruff_cache", os.path.join("engine", "target")}
# Libraries that can reach a sound server or a sound card. The rig has neither, but a test that loads
# one is a test that would make sound outside it, so the load fails the run.
AUDIO_LIBRARIES = ("espeak", "pcaudio", "pulse", "asound", "pipewire", "jack", "portaudio", "gst", "canberra")
# Extension modules that the test package's bootstrap imports and that link audio libraries, with why
# each is allowed to. What they link is mapped but never called: no test asks it for sound.
AUDIO_LINKERS = {
	"_html2": (
		"gui.message imports wx.html2, whose extension module links WebKitGTK, which links GStreamer and "
		"PulseAudio to play media in web pages"
	),
}
# Import checks run alongside the tests; each one starts NVDA's bootstrap in a process of its own.
CHECK_WORKERS = 4


@dataclass(frozen=True)
class Expectation:
	"""The exception an exclusion expects: the name of a class anywhere in its hierarchy, and a fragment
	of its message. Any exception in the chain of causes and contexts may match."""

	typeName: str
	fragment: str

	def matches(self, chain: list[tuple[list[str], str]]) -> bool:
		return any(self.typeName in names and self.fragment in message for names, message in chain)

	def __str__(self) -> str:
		return f"{self.typeName}: {self.fragment}"


def describeChain(error: BaseException) -> list[tuple[list[str], str]]:
	"""Each exception of an exception's chain, by its causes and contexts: its class names and message."""
	chain: list[tuple[list[str], str]] = []
	seen: set[int] = set()
	pending: list[BaseException | None] = [error]
	while pending:
		current = pending.pop(0)
		if current is None or id(current) in seen:
			continue
		seen.add(id(current))
		chain.append(([cls.__name__ for cls in type(current).__mro__], str(current)))
		pending += [current.__cause__, current.__context__]
	return chain


def causeLine(error: BaseException) -> str:
	lines = str(error).splitlines()
	return f"{type(error).__name__}: {lines[0] if lines else ''}"


@dataclass
class Entry:
	name: str
	reason: list[str] = field(default_factory=list)
	notRun: bool = False
	#: What a test this entry covers must fail with.
	raises: Expectation | None = None
	#: What importing the module or package this entry names must fail with.
	importRaises: Expectation | None = None
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
			text = line.strip()
			current.reason.append(text)
			current.notRun = current.notRun or text.startswith(NOT_RUN)
			found = EXPECTATION.match(text)
			if found is None and text.startswith(("Raises:", "Import raises:")):
				raise ValueError(
					f"{path}:{number}: write an expected cause as Raises: or Import raises:, "
					"an exception class name, a colon and a fragment of its message",
				)
			if found is not None:
				if current.raises is not None or current.importRaises is not None:
					raise ValueError(f"{path}:{number}: a second expected cause for {current.name}")
				expectation = Expectation(found[2], found[3])
				if found[1] == "Raises":
					current.raises = expectation
				else:
					current.importRaises = expectation
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
		if [entry.notRun, entry.raises is not None, entry.importRaises is not None].count(True) != 1:
			raise ValueError(
				f"{path}: {entry.name} needs exactly one line that starts with Raises:, Import raises: "
				f"or {NOT_RUN}",
			)
	for entry in entries.expectedSkips.values():
		if not entry.reason:
			raise ValueError(f"{path}: the expected skip {entry.name} gives no reason")
		if entry.notRun or entry.raises is not None or entry.importRaises is not None:
			raise ValueError(
				f"{path}: the expected skip {entry.name} can state no cause and cannot be not run"
			)
	return entries


def findEntry(testId: str, table: dict[str, Entry]) -> Entry | None:
	"""The entry that names this module, class or test most closely."""
	found: Entry | None = None
	for name, entry in table.items():
		covers = testId == name or testId.startswith(name + ".")
		if covers and (found is None or len(name) > len(found.name)):
			found = entry
	return found


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


def _oneCoreStandIn() -> types.ModuleType:
	module = types.ModuleType("synthDrivers.oneCore")

	class SynthDriver:
		name = "oneCore"

		@classmethod
		def check(cls) -> bool:
			return False

	module.SynthDriver = SynthDriver
	return module


@dataclass(frozen=True)
class StandIn:
	"""Takes the place of a module of NVDA's that cannot be imported on Linux, while a test module that
	imports it is imported. The real module is checked to fail as stated, so that a stand-in which is
	no longer needed fails the run."""

	module: str
	importRaises: Expectation
	build: Callable[[], types.ModuleType]
	reason: str


STAND_INS: dict[str, StandIn] = {
	"tests.unit.test_synthDriverHandler": StandIn(
		"synthDrivers.oneCore",
		Expectation("ModuleNotFoundError", "winreg"),
		_oneCoreStandIn,
		"The OneCore driver imports winreg, which Linux never provides. The test module imports its "
		"SynthDriver at module level, and uses it only in the two tests that skip unless "
		"SynthDriver.check() is true; the stand-in's check() answers False, as there is no OneCore "
		"speech on Linux.",
	),
}


@contextlib.contextmanager
def standingIn(testModule: str) -> Iterator[None]:
	standIn = STAND_INS.get(testModule)
	if standIn is None:
		yield
		return
	parentName, _, childName = standIn.module.rpartition(".")
	saved = sys.modules.get(standIn.module)
	replacement = sys.modules[standIn.module] = standIn.build()
	try:
		yield
	finally:
		if saved is None:
			sys.modules.pop(standIn.module, None)
		else:
			sys.modules[standIn.module] = saved
		parent = sys.modules.get(parentName)
		if parent is not None and getattr(parent, childName, None) is replacement:
			delattr(parent, childName)


def flatten(suite: unittest.TestSuite) -> Iterator[unittest.TestCase]:
	for test in suite:
		if isinstance(test, unittest.TestSuite):
			yield from flatten(test)
		else:
			yield test


def importTests(loader: unittest.TestLoader, name: str) -> list[unittest.TestCase]:
	"""Imports a test module and loads its tests as one step, so that a test class whose constructor
	raises fails like a module that does not import, rather than ending the collection."""
	with standingIn(name):
		module = importlib.import_module(name)
	return list(flatten(loader.loadTestsFromModule(module)))


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


@dataclass
class Collected:
	suite: unittest.TestSuite
	excludedModules: list[str]
	expectedToFail: dict[str, Entry]
	notRun: list[str]
	problems: list[str]


def collect(top: Path, package: str, exclusions: dict[str, Entry], guard: "Guard") -> Collected:
	loader = unittest.TestLoader()
	collected = Collected(unittest.TestSuite(), [], {}, [], [])
	failedPackages: list[str] = []
	for name in candidateModules(top, top.joinpath(*package.split("."))):
		if any(name.startswith(failed + ".") for failed in failedPackages):
			continue
		entry = findEntry(name, exclusions)
		if entry is not None and entry.raises is None:
			entry.matched += 1
			collected.excludedModules.append(name)
			continue
		try:
			# Imported here rather than by loadTestsFromName, which turns a failed import into a test
			# and so hides that a package's contents must not be loaded.
			tests = importTests(loader, name)
		except unittest.SkipTest as e:
			collected.suite.addTest(_ModuleSkipped(name, str(e)))
			continue
		except Exception:  # noqa: BLE001
			# Reported as a failing test, as discovery would, so that one broken module does not hide
			# the results of the others.
			failedPackages.append(name)
			collected.suite.addTest(_ImportFailure(name, traceback.format_exc()))
			continue
		finally:
			guard.checkLibraries(f"the import of {name}")
		for test in tests:
			entry = findEntry(test.id(), exclusions)
			if entry is None:
				collected.suite.addTest(test)
				continue
			entry.matched += 1
			if entry.notRun:
				collected.notRun.append(test.id())
				continue
			if entry.importRaises is not None:
				collected.problems.append(
					f"rununittests: {entry.name} expects an import to fail, but it names {test.id()}, "
					"which imports and runs",
				)
				collected.suite.addTest(test)
				continue
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
	records in place of the test objects and a failing subtest is not a test of its own.

	A test that an exclusion covers is run like any other, and its first failure, wherever it
	arrives, is held against the exclusion's expected cause; later failures of the same test follow
	from the first.
	"""

	#: The exclusion covering each test that is run expecting failure, by test id.
	expectations: ClassVar[dict[str, Entry]] = {}
	#: Checks for libraries mapped while each test ran.
	guard: "Guard | None" = None

	def __init__(self, *args, **kwargs) -> None:
		super().__init__(*args, **kwargs)
		self.outcomes: dict[str, str] = {}
		self.problems: list[str] = []
		self.fixtureErrors: list[str] = []
		self.skips: dict[str, str] = {}
		#: The first failure of each test run expecting failure, as one line.
		self.causes: dict[str, str] = {}

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

	def stopTest(self, test: unittest.TestCase) -> None:
		super().stopTest(test)
		if self.guard is not None:
			self.guard.checkLibraries(test.id())

	def _expectation(self, test) -> Entry | None:
		# Class and module set-up and tear-down errors arrive as a stand-in that is not a TestCase.
		return self.expectations.get(test.id()) if isinstance(test, unittest.TestCase) else None

	def _expectedFailure(self, test: unittest.TestCase, err, entry: Entry) -> None:
		testId = test.id()
		if testId in self.causes:
			return
		self.causes[testId] = causeLine(err[1])
		if entry.raises.matches(describeChain(err[1])):
			self._worsen(test, "failed as expected")
			super().addExpectedFailure(test, err)
			return
		self._worsen(test, "failed")
		self.problems.append(
			f"FAIL: {testId}: the exclusion {entry.name} expects {entry.raises}, but the test failed "
			f"otherwise\n{self._exc_info_to_string(err, test)}",
		)
		super().addFailure(test, err)

	def addError(self, test: unittest.TestCase, err) -> None:
		entry = self._expectation(test)
		if entry is not None:
			self._expectedFailure(test, err, entry)
			return
		if isinstance(test, unittest.TestCase):
			self._worsen(test, "errors")
		else:
			self.fixtureErrors.append(test.id())
		self._problem("ERROR", test, err)
		super().addError(test, err)

	def addFailure(self, test: unittest.TestCase, err) -> None:
		entry = self._expectation(test)
		if entry is not None:
			self._expectedFailure(test, err, entry)
			return
		self._worsen(test, "failed")
		self._problem("FAIL", test, err)
		super().addFailure(test, err)

	def addSubTest(self, test: unittest.TestCase, subtest: unittest.TestCase, err) -> None:
		entry = self._expectation(test)
		if entry is not None and err is not None:
			self._expectedFailure(test, err, entry)
			return
		if err is not None:
			failed = issubclass(err[0], test.failureException)
			self._worsen(test, "failed" if failed else "errors")
			self._problem("FAIL" if failed else "ERROR", subtest, err)
		super().addSubTest(test, subtest, err)

	def addSuccess(self, test: unittest.TestCase) -> None:
		if self._expectation(test) is not None:
			self.addUnexpectedSuccess(test)
			return
		super().addSuccess(test)

	def addSkip(self, test: unittest.TestCase, reason: str) -> None:
		self._worsen(test, "skipped")
		self.skips.setdefault(_ownId(test), reason)
		super().addSkip(test, reason)

	def addExpectedFailure(self, test: unittest.TestCase, err) -> None:
		# Only for a test that upstream marks with unittest.expectedFailure.
		self._worsen(test, "failed as expected")
		super().addExpectedFailure(test, err)

	def addUnexpectedSuccess(self, test: unittest.TestCase) -> None:
		self._worsen(test, "passed unexpectedly")
		super().addUnexpectedSuccess(test)


def treeState(top: Path) -> dict[str, tuple]:
	"""A directory's modification time changes whenever an entry is made or removed in it, so this
	also shows a file that was written and deleted again. The change time is kept too, because
	nothing can set it back, unlike the modification time."""
	state: dict[str, tuple] = {}
	for dirPath, dirNames, fileNames in os.walk(top):
		dirNames[:] = [d for d in dirNames if os.path.relpath(os.path.join(dirPath, d), top) not in UNWATCHED]
		info = os.lstat(dirPath)
		state[dirPath] = ("directory", info.st_ino, info.st_mtime_ns, info.st_ctime_ns)
		for name in fileNames + [d for d in dirNames if os.path.islink(os.path.join(dirPath, d))]:
			path = os.path.join(dirPath, name)
			info = os.lstat(path)
			state[path] = (info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
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
# Events that follow a symbolic link at the end of their path, and so change what it points to.
_FOLLOWING = {"open", "os.truncate", "os.chmod", "os.utime"}


def _absolute(path, dirFd, following: bool) -> list[str]:
	"""The path an event names, with the directories it passes through resolved, and also with the
	link at its end resolved when the event follows one."""
	if isinstance(path, int):
		return []
	path = os.fsdecode(path)
	if not os.path.isabs(path):
		# The events give -1 for "no directory descriptor".
		useFd = isinstance(dirFd, int) and dirFd >= 0
		base = os.readlink(f"/proc/self/fd/{dirFd}") if useFd else os.getcwd()
		path = os.path.join(base, path)
	parent, name = os.path.split(os.path.normpath(path))
	paths = [os.path.join(os.path.realpath(parent), name)]
	if following and os.path.realpath(path) != paths[0]:
		paths.append(os.path.realpath(path))
	return paths


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
		return _absolute(path, None, True) if writes else []
	paths: list[str] = []
	for pathAt, fdAt in _PATH_EVENTS.get(event, ()):
		paths += _absolute(args[pathAt], None if fdAt is None else args[fdAt], event in _FOLLOWING)
	if event in _ON_EXISTING and paths and not os.path.lexists(paths[0]):
		return []
	return paths


def mappedLibraries() -> set[str]:
	"""The files mapped executable into this process: every shared object it has loaded."""
	paths: set[str] = set()
	with open("/proc/self/maps", encoding="utf-8", errors="replace") as maps:
		for line in maps:
			fields = line.split(maxsplit=5)
			if len(fields) == 6 and "x" in fields[1] and fields[5].startswith("/"):
				paths.add(fields[5].rstrip("\n"))
	return paths


@functools.cache
def elfLinks(path: str) -> tuple[str | None, tuple[str, ...]]:
	"""A 64-bit little-endian shared object's own name and the libraries it links, read from its
	dynamic section; nothing for any other file."""
	try:
		with open(path, "rb") as file, mmap.mmap(file.fileno(), 0, access=mmap.ACCESS_READ) as image:
			if image[:4] != b"\x7fELF" or image[4] != 2 or image[5] != 1:
				return None, ()
			(headersAt,) = struct.unpack_from("<Q", image, 0x20)
			headerSize, headerCount = struct.unpack_from("<HH", image, 0x36)
			loads: list[tuple[int, int, int]] = []
			dynamic: tuple[int, int] | None = None
			for index in range(headerCount):
				kind, _flags, offset, address, _physical, size = struct.unpack_from(
					"<IIQQQQ",
					image,
					headersAt + index * headerSize,
				)
				if kind == 1:
					loads.append((address, offset, size))
				elif kind == 2:
					dynamic = (offset, size)
			if dynamic is None:
				return None, ()
			strings: int | None = None
			tags: list[tuple[int, int]] = []
			for at in range(dynamic[0], dynamic[0] + dynamic[1], 16):
				tag, value = struct.unpack_from("<qQ", image, at)
				if tag == 0:
					break
				if tag == 5:
					strings = value
				elif tag in (1, 14):
					tags.append((tag, value))
			if strings is None:
				return None, ()
			base = next(
				(
					offset + strings - address
					for address, offset, size in loads
					if address <= strings < address + size
				),
				None,
			)
			if base is None:
				return None, ()

			def string(at: int) -> str:
				return image[base + at : image.find(b"\0", base + at)].decode(errors="replace")

			soname = next((string(value) for tag, value in tags if tag == 14), None)
			return soname, tuple(string(value) for tag, value in tags if tag == 1)
	except (OSError, ValueError, struct.error):
		return None, ()


def linkedClosure(path: str, mapped: set[str]) -> list[str]:
	"""Every library a shared object links, directly or through others: the mapped path where it is
	mapped, otherwise the name it is linked by."""
	byName: dict[str, str] = {}
	for library in mapped:
		byName.setdefault(os.path.basename(library), library)
		soname = elfLinks(library)[0]
		if soname is not None:
			byName.setdefault(soname, library)
	reached: list[str] = []
	seen = {path}
	pending = [path]
	while pending:
		for name in elfLinks(pending.pop())[1]:
			target = byName.get(name, name)
			if target not in seen:
				seen.add(target)
				reached.append(target)
				if target in mapped:
					pending.append(target)
	return reached


def isAudioLibrary(path: str) -> bool:
	name = os.path.basename(path).lower()
	return any(library in name for library in AUDIO_LIBRARIES)


class Guard:
	"""Refuses, and records, every write into the checkout made in this process and every load of
	an audio library. Writes by child processes are found by comparing treeState before and after.

	Loads through ctypes are refused as they happen. Libraries the dynamic linker maps for an
	extension module raise no audit event, so the mapped libraries are also compared after the
	bootstrap, after each import and after each test: a library mapped since must neither be an audio
	library nor link one, even one that something else mapped earlier.
	"""

	def __init__(self, root: Path, tests: Path):
		self.root = os.path.realpath(root)
		self.tests = os.path.realpath(tests)
		self.armed = True
		self.refused: list[str] = []
		#: The libraries mapped when last checked, once the bootstrap has run.
		self.libraries: set[str] | None = None

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
			if isAudioLibrary(os.fsdecode(args[0])):
				self._refuse(f"load of the audio library {args[0]}")
				raise OSError(f"rununittests: loading the audio library {args[0]} is refused")
			return
		if event != "open" and event not in _PATH_EVENTS:
			return
		for path in writtenPaths(event, args):
			if self._inCheckout(path):
				self._refuse(f"{event} of {path}")
				raise PermissionError(f"rununittests: {event} of {path} in the checkout is refused")

	def watchLibraries(self) -> None:
		"""Called once the bootstrap has run: every audio library mapped by then must be linked by one
		of AUDIO_LINKERS, and every library mapped later is checked."""
		self.libraries = mappedLibraries()
		allowed: set[str] = set()
		for path in self.libraries:
			if os.path.basename(path).split(".")[0] in AUDIO_LINKERS:
				allowed.update(linkedClosure(path, self.libraries))
		for path in sorted(self.libraries - allowed):
			if isAudioLibrary(path):
				self.refused.append(
					f"the audio library {path}, which the bootstrap mapped unlinked by AUDIO_LINKERS"
				)

	def checkLibraries(self, during: str) -> None:
		if self.libraries is None:
			return
		current = mappedLibraries()
		added = current - self.libraries
		self.libraries = current
		for path in sorted(added):
			if isAudioLibrary(path):
				self.refused.append(f"load of the audio library {path}, during {during}")
				continue
			reached = next(
				(library for library in linkedClosure(path, current) if isAudioLibrary(library)), None
			)
			if reached is not None:
				self.refused.append(
					f"load of {path}, which links the audio library {reached}, during {during}"
				)


def _startWatchdog(seconds: float) -> None:
	def watch() -> None:
		time.sleep(seconds)
		# sys.__stderr__, because while a test runs sys.stderr is its output buffer.
		print(
			f"rununittests: no result after {seconds:g} s; the stack of every thread follows",
			file=sys.__stderr__,
			flush=True,
		)
		faulthandler.dump_traceback(file=sys.__stderr__, all_threads=True)
		os._exit(1)

	threading.Thread(target=watch, name="rununittests watchdog", daemon=True).start()
	# For a hang that never releases the interpreter lock, which the thread above cannot interrupt.
	faulthandler.dump_traceback_later(seconds + 60, exit=True)


def runSuite(collected: Collected, guard: Guard, xml: bool, verbose: bool) -> tuple[_Recording, bytes | None]:
	"""Runs the suite, and returns the result and, if asked for, the JUnit XML report."""
	suite = collected.suite
	stream = sys.stdout if verbose else io.StringIO()
	verbosity = 2 if verbose else 0
	attributes = {"expectations": collected.expectedToFail, "guard": guard}
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


def _parseArguments() -> argparse.Namespace:
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
	parser.add_argument(
		"--checkout",
		type=Path,
		default=ROOT,
		help="the tree that the run must not change; for testing this runner",
	)
	# Set by the runner for the processes it starts.
	parser.add_argument("--record", type=Path, help=argparse.SUPPRESS)
	parser.add_argument("--check-import", dest="checkImport", help=argparse.SUPPRESS)
	return parser.parse_args()


def main() -> int:
	_requireRig()
	sys.dont_write_bytecode = True
	args = _parseArguments()
	if args.record is not None:
		_childMain(args)
	print("rununittests: start", flush=True)
	if sys.platform != "linux":
		print("rununittests: this runner is for Linux; upstream's is rununittests.bat", file=sys.stderr)
		return 2
	entries = readEntries(args.exclusions, args.package)
	checkout = args.checkout.resolve()
	before = treeState(checkout)
	# Every temporary file of the run, the bootstrap's configuration directory among them, goes here,
	# and this process removes it, so that a child that dies leaves nothing behind either.
	workDir = tempfile.mkdtemp(prefix="rununittests-")
	try:
		status, summary, report = _supervise(args, entries, Path(workDir))
		after = treeState(checkout)
		changed = sorted(path for path in before.keys() | after.keys() if before.get(path) != after.get(path))
		for path in changed:
			print(f"rununittests: the run changed {path}", file=sys.stderr)
			status = 1
		sys.stderr.flush()
		for line in summary:
			print(line, flush=True)
		if args.xml is not None and report.exists():
			# Written after the check of the checkout, as it may be asked for inside it.
			args.xml.parent.mkdir(parents=True, exist_ok=True)
			shutil.copyfile(report, args.xml)
		return status
	finally:
		shutil.rmtree(workDir, ignore_errors=True)


def _runChild(command: list[str], tmpDir: Path, timeout: float, capture: bool) -> tuple[int | None, str]:
	"""Runs a child in a process group of its own, and ends whatever is left of that group after
	it; the status is None when the timeout ended it."""
	tmpDir.mkdir()
	process = subprocess.Popen(
		command,
		env={**os.environ, "TMPDIR": str(tmpDir)},
		process_group=0,
		stdout=subprocess.PIPE if capture else None,
		stderr=subprocess.STDOUT if capture else None,
		text=True,
	)
	try:
		output, _ = process.communicate(timeout=timeout)
		status = process.returncode
	except subprocess.TimeoutExpired:
		os.killpg(process.pid, signal.SIGKILL)
		output, _ = process.communicate()
		status = None
	finally:
		with contextlib.suppress(ProcessLookupError):
			os.killpg(process.pid, signal.SIGKILL)
	return status, output or ""


def _childArguments(args: argparse.Namespace, record: Path) -> list[str]:
	return [
		sys.executable,
		str(RUNNER),
		"--record",
		str(record),
		"--exclusions",
		str(args.exclusions.resolve()),
		"--timeout",
		str(args.timeout),
		"--top",
		str(args.top.resolve()),
		"--package",
		args.package,
		"--checkout",
		str(args.checkout.resolve()),
	]


@dataclass(frozen=True)
class ImportCheck:
	#: What states the expectation, as the report names it.
	owner: str
	module: str
	expectation: Expectation


def _importChecks(args: argparse.Namespace, entries: EntryList) -> list[ImportCheck]:
	top = args.top.resolve()
	candidates = set(candidateModules(top, top.joinpath(*args.package.split("."))))
	checks = [
		ImportCheck(f"the exclusion {entry.name}", entry.name, entry.importRaises)
		for entry in entries.exclusions.values()
		if entry.importRaises is not None and entry.name in candidates
	]
	checks += [
		ImportCheck(f"the stand-in for {standIn.module}", standIn.module, standIn.importRaises)
		for testModule, standIn in STAND_INS.items()
		if testModule in candidates
	]
	return checks


def _runImportCheck(
	args: argparse.Namespace,
	workDir: Path,
	index: int,
	check: ImportCheck,
) -> tuple[list[str], list[str]]:
	"""Imports what the check names in a process of its own. Returns the problems found, none when
	every import it made that was not of a package failed as stated, and how each import failed."""
	record = workDir / f"check-{index}.json"
	command = [*_childArguments(args, record), "--check-import", check.module]
	status, output = _runChild(command, workDir / f"check-{index}", args.timeout, capture=True)
	if not record.exists():
		return [
			(
				f"rununittests: the import check for {check.owner} ended with status {status} before "
				f"recording a result; its output follows\n{output}"
			),
		], []
	result = json.loads(record.read_text(encoding="utf-8"))
	problems = [
		f"rununittests: refused {refused}, in the import check for {check.owner}"
		for refused in result["refused"]
	]
	if result["bootstrap"] is not None:
		return [
			*problems,
			f"rununittests: the bootstrap failed in the import check for {check.owner}\n{result['bootstrap']}",
		], []
	failed = [outcome for outcome in result["outcomes"] if outcome["chain"] is not None]
	if not failed:
		problems.append(f"rununittests: {check.owner} is stale: {check.module} imports")
	else:
		# A package that imports is not stale while the test modules beneath it fail instead.
		problems += [
			f"rununittests: {check.owner} is stale: {outcome['module']} imports"
			for outcome in result["outcomes"]
			if outcome["chain"] is None and not outcome["package"]
		]
	for outcome in failed:
		if not check.expectation.matches(outcome["chain"]):
			problems.append(
				f"rununittests: {check.owner} expects the import of {outcome['module']} to raise "
				f"{check.expectation}, but it raised otherwise\n{outcome['traceback']}",
			)
	return problems, [f"import fails: {outcome['module']}: {outcome['cause']}" for outcome in failed]


def _supervise(args: argparse.Namespace, entries: EntryList, workDir: Path) -> tuple[int, list[str], Path]:
	"""Runs the tests in one child and the import checks in others, alongside; returns the status,
	the summary lines and where the XML report is written."""
	record = workDir / "record.json"
	report = workDir / "report.xml"
	command = _childArguments(args, record)
	if args.verbose:
		command.append("--verbose")
	if args.xml is not None:
		command += ["--xml", str(report)]
	checks = _importChecks(args, entries)
	with ThreadPoolExecutor(max_workers=CHECK_WORKERS) as pool:
		pending = [
			pool.submit(_runImportCheck, args, workDir, index, check) for index, check in enumerate(checks)
		]
		# The child's own watchdog fires at the timeout, and its fallback a minute later.
		status, _output = _runChild(command, workDir / "tmp", args.timeout + 120, capture=False)
		checkResults = [future.result() for future in pending]
	summary: list[str] = []
	if status is None:
		print("rununittests: the test process outlived its own timeout and was killed", file=sys.stderr)
		status = 1
	elif not record.exists():
		print(
			f"rununittests: the test process ended with status {status} before recording its result: "
			"a test, fixture or module ended the interpreter",
			file=sys.stderr,
		)
		status = 1
	else:
		result = json.loads(record.read_text(encoding="utf-8"))
		summary = result["summary"]
		status = 1 if status or result["status"] else 0
	for problems, notes in checkResults:
		for problem in problems:
			print(problem, file=sys.stderr)
			status = 1
		for note in notes if args.verbose else ():
			print(note, flush=True)
	summary.append(
		f"rununittests: {len(checks)} imports expected to fail, each checked in a process of its own; "
		f"{sum(not problems for problems, _notes in checkResults)} fail as stated",
	)
	return status, summary, report


def _prepareImports(args: argparse.Namespace) -> None:
	# Where upstream's editable install and discovery's top-level directory put them; never this
	# script's directory, whose synthDrivers package would shadow NVDA's.
	others = [path for path in sys.path if not path or os.path.realpath(path) != str(HERE)]
	sys.path[:] = [str(SOURCE), str(args.top.resolve()), *others]


def _childMain(args: argparse.Namespace) -> NoReturn:
	"""Runs the tests, or one import check, and leaves through os._exit once the record is written,
	so that no thread left running keeps the process alive."""
	_prepareImports(args)
	guard = Guard(args.checkout, args.top.joinpath(*args.package.split(".")))
	sys.addaudithook(guard)
	_startWatchdog(args.timeout)
	if args.checkImport is not None:
		record = _checkImport(args, guard)
		status = 0
	else:
		try:
			status, summary = _runTests(args, guard)
		except SystemExit:
			traceback.print_exc()
			print("rununittests: a test, fixture or module exited the interpreter", file=sys.stderr)
			status, summary = 1, []
		guard.armed = False
		for name in _threadsLeftRunning():
			print(f"rununittests: a test left the thread {name!r} running", file=sys.stderr)
			status = 1
		record = {"status": status, "summary": summary}
	guard.armed = False
	args.record.write_text(json.dumps(record), encoding="utf-8")
	sys.stdout.flush()
	sys.stderr.flush()
	os._exit(status)


def _threadsLeftRunning() -> list[str]:
	deadline = time.monotonic() + 1
	names: list[str] = []
	for thread in threading.enumerate():
		if thread is threading.current_thread() or thread.daemon:
			continue
		thread.join(max(0.0, deadline - time.monotonic()))
		if thread.is_alive():
			names.append(thread.name)
	return names


def _checkImport(args: argparse.Namespace, guard: Guard) -> dict:
	"""Imports the bootstrap, then the module, or the package and the test modules beneath it, and
	records how each import ended."""
	record: dict = {"bootstrap": None, "outcomes": [], "refused": guard.refused}
	try:
		importlib.import_module(args.package)
	except Exception:  # noqa: BLE001
		record["bootstrap"] = traceback.format_exc()
		return record
	guard.watchLibraries()
	loader = unittest.TestLoader()
	top = args.top.resolve()
	directory = top.joinpath(*args.checkImport.split("."))
	isPackage = (directory / "__init__.py").is_file()
	names = list(candidateModules(top, directory)) if isPackage else [args.checkImport]
	failedPackages: list[str] = []
	for name in names:
		if any(name.startswith(failed + ".") for failed in failedPackages):
			continue
		outcome = {"module": name, "package": (top.joinpath(*name.split(".")) / "__init__.py").is_file()}
		try:
			if name.startswith(args.package + "."):
				importTests(loader, name)
			else:
				importlib.import_module(name)
		except Exception as error:  # noqa: BLE001
			failedPackages.append(name)
			outcome.update(
				chain=describeChain(error), cause=causeLine(error), traceback=traceback.format_exc()
			)
		else:
			outcome.update(chain=None, cause=None, traceback=None)
		record["outcomes"].append(outcome)
		guard.checkLibraries(f"the import of {name}")
	return record


def _runTests(args: argparse.Namespace, guard: Guard) -> tuple[int, list[str]]:
	"""Runs the suite and reports its problems; returns the status and the summary lines."""
	entries = readEntries(args.exclusions, args.package)
	try:
		# The package is the bootstrap every test module imports first; a failure here would
		# otherwise be repeated once for each of them.
		importlib.import_module(args.package)
	except Exception:  # noqa: BLE001
		traceback.print_exc()
		print("rununittests: the test package's bootstrap failed", file=sys.stderr)
		return 1, []
	guard.watchLibraries()
	collected = collect(args.top.resolve(), args.package, entries.exclusions, guard)
	result, report = runSuite(collected, guard, args.xml is not None, args.verbose)
	guard.armed = False

	status = 0
	for problem in (*collected.problems, *result.problems):
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
			print(f"excluded test, run expecting failure: {name}: {result.causes.get(name, 'no failure')}")
		for name in collected.notRun:
			print(f"excluded test, not run: {name}")

	for entry in (*entries.exclusions.values(), *entries.expectedSkips.values()):
		if not entry.matched:
			print(f"rununittests: entry matches nothing: {entry.name}", file=sys.stderr)
			status = 1
	for refused in guard.refused:
		print(f"rununittests: refused {refused}", file=sys.stderr)
		status = 1

	counts = {outcome: 0 for outcome in OUTCOMES}
	for outcome in result.outcomes.values():
		counts[outcome] += 1
	summary = [
		f"rununittests: {len(result.outcomes)} tests ran: "
		+ ", ".join(f"{count} {outcome}" for outcome, count in counts.items()),
		f"rununittests: {len(result.fixtureErrors)} errors in class or module set-up and tear-down",
		(
			f"rununittests: excluded {len(collected.excludedModules)} modules, not imported; "
			f"{len(collected.expectedToFail)} tests, run expecting failure; {len(collected.notRun)} tests, not run"
		),
	]
	if report is not None:
		args.xml.write_bytes(report)
	return status, summary


if __name__ == "__main__":
	sys.exit(main())
