"""The upstream unit-test runner, checked against probe tests, and the product fixes its runs led to.

The probes live in a package of their own under the test's temporary directory; nothing is added to
tests/unit.

	nix develop . -c tests/linux/rig python3 -m pytest -p no:cacheprovider tests/linux/test_unitTests.py
"""

import os
import re
import subprocess
import sys
import textwrap
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import harness
import pytest
import rununittests

ROOT = Path(__file__).resolve().parent.parent.parent
RUNNER = Path(rununittests.__file__)
PACKAGE = "probeUnit"
TRANSIENT = ROOT / "tests" / "unit" / "probeTransient.tmp"

# The real bootstrap makes its configuration directory this way; the runner has to remove it.
BOOTSTRAP = textwrap.dedent("""
	import tempfile

	_configDir = tempfile.TemporaryDirectory(prefix="probe-config-")
""")

# A module that must only ever be imported by the runner's import check, never into the run.
CHECKED_ONLY = textwrap.dedent("""
	import sys

	if "--check-import" not in sys.argv:
		raise SystemExit("an excluded module was imported into the run")
""")

PROBES = {
	"__init__.py": BOOTSTRAP,
	"test_problems.py": f"""
		import ctypes
		import unittest

		class TestProblems(unittest.TestCase):
			def test_passes(self):
				pass

			def test_fails(self):
				self.assertEqual(1, 2)

			def test_errors(self):
				raise RuntimeError("probe error")

			def test_subtests(self):
				for i in range(3):
					with self.subTest(i=i):
						self.assertEqual(i, 99)

			@unittest.expectedFailure
			def test_unexpectedSuccess(self):
				pass

			def test_skipsUnexpectedly(self):
				self.skipTest("probe skip")

			def test_writesIntoCheckout(self):
				with open({str(TRANSIENT)!r}, "w") as f:
					f.write("the runner should have refused this")

			def test_loadsAudioLibrary(self):
				# A path that does not exist, so that nothing loads even if the runner lets it through.
				try:
					ctypes.CDLL("/nonexistent/libpulse-probe.so")
				except OSError:
					pass

		class TestFixture(unittest.TestCase):
			@classmethod
			def setUpClass(cls):
				raise RuntimeError("probe setUpClass")

			def test_neverRuns(self):
				pass
	""",
	"test_importFails.py": """
		raise ImportError("probe import failure")
	""",
	"test_constructorRaises.py": """
		import unittest

		class TestConstructorRaises(unittest.TestCase):
			def __init__(self, *args, **kwargs):
				raise RuntimeError("probe constructor")

			def test_neverBuilt(self):
				pass
	""",
	"brokenPackage/__init__.py": """
		raise ImportError("probe package failure")
	""",
	"brokenPackage/test_inside.py": """
		raise SystemExit("a module inside a package that failed to import was imported")
	""",
	"test_excludedModule.py": CHECKED_ONLY
	+ textwrap.dedent("""
		raise ImportError("probe excluded import")
	"""),
	"excludedPackage/__init__.py": "",
	"excludedPackage/test_a.py": CHECKED_ONLY
	+ textwrap.dedent("""
		raise ImportError("probe excluded package")
	"""),
	"excludedPackage/test_b.py": CHECKED_ONLY
	+ textwrap.dedent("""
		raise ImportError("probe excluded package, again")
	"""),
	"test_excluded.py": """
		import unittest

		class TestExcluded(unittest.TestCase):
			def test_stillFails(self):
				self.fail("excluded and still failing")

			def test_nowPasses(self):
				pass

			def test_notRun(self):
				raise SystemExit("a test marked not run was run")

			def test_firstFailureCounts(self):
				for i in range(3):
					with self.subTest(i=i):
						self.assertEqual(i, -1, "first cause" if i == 0 else "a consequence")

		class TestSetUpFails(unittest.TestCase):
			def setUp(self):
				raise RuntimeError("probe setUp")

			def test_one(self):
				pass
	""",
	"test_excludedTests.py": """
		import unittest

		class TestModuleExcluded(unittest.TestCase):
			def test_x(self):
				raise ValueError("probe module cause")

			def test_y(self):
				raise ValueError("probe module cause, again")
	""",
	"test_skips.py": """
		import unittest

		@unittest.skip("probe expected skip")
		class TestSkips(unittest.TestCase):
			def test_a(self):
				pass

			def test_b(self):
				pass
	""",
}

EXCLUSIONS = """
	probeUnit.test_excludedModule
	  For good: probe.
	  Import raises: ImportError: probe excluded import
	probeUnit.excludedPackage
	  For good: probe.
	  Import raises: ImportError: probe excluded package
	probeUnit.test_excluded.TestExcluded.test_stillFails
	  Until probe.
	  Raises: AssertionError: excluded and still failing
	probeUnit.test_excluded.TestExcluded.test_notRun
	  Until probe.
	  Not run: probe.
	probeUnit.test_excluded.TestExcluded.test_firstFailureCounts
	  Until probe.
	  Raises: AssertionError: first cause
	probeUnit.test_excluded.TestSetUpFails
	  Until probe.
	  Raises: RuntimeError: probe setUp
	probeUnit.test_excludedTests
	  Until probe.
	  Raises: ValueError: probe module cause
	skipped probeUnit.test_skips.TestSkips
	  Probe.
"""

STALE_PROBES = {
	"test_staleImport.py": CHECKED_ONLY
	+ textwrap.dedent("""
		import unittest

		class TestImports(unittest.TestCase):
			def test_passes(self):
				pass
	"""),
	"test_otherImportCause.py": CHECKED_ONLY
	+ textwrap.dedent("""
		raise ValueError("probe other import cause")
	"""),
	"excludedPackage/test_c.py": CHECKED_ONLY,
	"test_otherCause.py": """
		import unittest

		class TestOtherCause(unittest.TestCase):
			def test_raisesOtherwise(self):
				raise KeyError("probe other cause")

			def test_setUpCauseHolds(self):
				pass
	""",
}

PROBLEM_EXCLUSIONS = (
	EXCLUSIONS
	+ """
	probeUnit.test_excluded.TestExcluded.test_nowPasses
	  Until probe.
	  Raises: AssertionError: probe
	probeUnit.test_stale
	  Until probe.
	  Import raises: ImportError: probe
	probeUnit.test_staleImport
	  Until probe.
	  Import raises: ImportError: probe
	probeUnit.test_otherImportCause
	  Until probe.
	  Import raises: ImportError: probe
	probeUnit.test_otherCause.TestOtherCause.test_raisesOtherwise
	  Until probe.
	  Raises: AssertionError: probe
"""
)

REPORT_LINE = re.compile(r"^(ERROR|FAIL|UNEXPECTED SUCCESS|UNEXPECTED SKIP): |^rununittests: (?!start)")


def makeProbes(tmp_path: Path, modules: dict[str, str], exclusions: str) -> Path:
	for name, body in modules.items():
		path = tmp_path / "top" / PACKAGE / name
		path.parent.mkdir(parents=True, exist_ok=True)
		path.write_text(textwrap.dedent(body), encoding="utf-8")
	listPath = tmp_path / "exclusions.txt"
	listPath.write_text(textwrap.dedent(exclusions), encoding="utf-8")
	return listPath


def runRunner(tmp_path: Path, listPath: Path, *extra: str) -> tuple[int, list[str], str]:
	"""The exit status, the lines that report problems and counts, and the whole output."""
	tmpDir = tmp_path / "tmp"
	tmpDir.mkdir(exist_ok=True)
	done = subprocess.run(
		[
			sys.executable,
			str(RUNNER),
			"--top",
			str(tmp_path / "top"),
			"--package",
			PACKAGE,
			"--exclusions",
			str(listPath),
			*extra,
		],
		capture_output=True,
		text=True,
		timeout=300,
		env={**os.environ, "TMPDIR": str(tmpDir)},
		check=False,
	)
	output = done.stdout + done.stderr
	assert not list(tmpDir.iterdir()), f"the run left temporary files: {list(tmpDir.iterdir())}"
	return done.returncode, sorted(line for line in output.splitlines() if REPORT_LINE.match(line)), output


def test_everyProblemIsNamedAndCountedAlikeWithAndWithoutXml(tmp_path: Path):
	listPath = makeProbes(tmp_path, PROBES | STALE_PROBES, PROBLEM_EXCLUSIONS)
	try:
		status, lines, output = runRunner(tmp_path, listPath)
		xmlStatus, xmlLines, xmlOutput = runRunner(tmp_path, listPath, "--xml", str(tmp_path / "report.xml"))
	finally:
		leaked = TRANSIENT.exists()
		if leaked:
			TRANSIENT.unlink()
	assert not leaked, "a probe test wrote into tests/unit"
	assert status == 1, output
	assert xmlStatus == 1, xmlOutput
	assert lines == xmlLines
	tests = f"{PACKAGE}.test_problems.TestProblems"
	expected = [
		f"FAIL: {tests}.test_fails",
		f"ERROR: {tests}.test_errors",
		f"FAIL: {tests}.test_subtests (i=0)",
		f"FAIL: {tests}.test_subtests (i=1)",
		f"FAIL: {tests}.test_subtests (i=2)",
		f"ERROR: {tests}.test_writesIntoCheckout",
		f"ERROR: setUpClass ({PACKAGE}.test_problems.TestFixture)",
		f"ERROR: {PACKAGE}.test_importFails",
		f"ERROR: {PACKAGE}.test_constructorRaises",
		f"ERROR: {PACKAGE}.brokenPackage",
		f"UNEXPECTED SUCCESS: {tests}.test_unexpectedSuccess",
		(
			f"UNEXPECTED SUCCESS: {PACKAGE}.test_excluded.TestExcluded.test_nowPasses passes, "
			f"so the exclusion {PACKAGE}.test_excluded.TestExcluded.test_nowPasses is stale"
		),
		(
			f"FAIL: {PACKAGE}.test_otherCause.TestOtherCause.test_raisesOtherwise: the exclusion "
			f"{PACKAGE}.test_otherCause.TestOtherCause.test_raisesOtherwise expects AssertionError: probe, "
			"but the test failed otherwise"
		),
		f"UNEXPECTED SKIP: {tests}.test_skipsUnexpectedly: probe skip",
		f"rununittests: entry matches nothing: {PACKAGE}.test_stale",
		f"rununittests: the exclusion {PACKAGE}.test_staleImport is stale: {PACKAGE}.test_staleImport imports",
		(
			f"rununittests: the exclusion {PACKAGE}.excludedPackage is stale: "
			f"{PACKAGE}.excludedPackage.test_c imports"
		),
		(
			f"rununittests: the exclusion {PACKAGE}.test_otherImportCause expects the import of "
			f"{PACKAGE}.test_otherImportCause to raise ImportError: probe, but it raised otherwise"
		),
		(
			"rununittests: 21 tests ran: 3 passed, 3 skipped, 5 failed as expected, "
			"2 passed unexpectedly, 3 failed, 5 errors"
		),
		"rununittests: 1 errors in class or module set-up and tear-down",
		"rununittests: excluded 7 modules, not imported; 7 tests, run expecting failure; 1 tests, not run",
		"rununittests: 4 imports expected to fail, each checked in a process of its own; 1 fail as stated",
	]
	for line in expected:
		assert line in lines, f"{line!r} missing from {lines}"
	refusals = [line for line in lines if line.startswith("rununittests: refused ")]
	assert len(refusals) == 2, refusals
	assert any(f"open of {TRANSIENT}, from " in line for line in refusals), refusals
	assert any("load of the audio library /nonexistent/libpulse-probe.so, from " in line for line in refusals)
	assert "a test marked not run was run" not in output
	assert "was imported" not in output
	assert "KeyError: 'probe other cause'" in output
	names = {case.get("name") for case in ET.parse(tmp_path / "report.xml").iter("testcase")}
	assert {"test_fails", "test_nowPasses", "test_raisesOtherwise"} <= names
	assert any(name.startswith("test_subtests") for name in names), names


def test_cleanRunPasses(tmp_path: Path):
	unwanted = (
		"test_problems.py",
		"test_importFails.py",
		"test_constructorRaises.py",
		"brokenPackage/__init__.py",
		"brokenPackage/test_inside.py",
	)
	modules = {name: body for name, body in PROBES.items() if name not in unwanted}
	listPath = makeProbes(tmp_path, modules, EXCLUSIONS)
	status, lines, output = runRunner(tmp_path, listPath, "--verbose")
	assert status == 0, output
	assert lines == [
		"rununittests: 0 errors in class or module set-up and tear-down",
		"rununittests: 2 imports expected to fail, each checked in a process of its own; 2 fail as stated",
		(
			"rununittests: 8 tests ran: 1 passed, 2 skipped, 5 failed as expected, "
			"0 passed unexpectedly, 0 failed, 0 errors"
		),
		"rununittests: excluded 4 modules, not imported; 5 tests, run expecting failure; 1 tests, not run",
	]
	# The verbose listing names the cause each exclusion was held against.
	assert (
		f"excluded test, run expecting failure: {PACKAGE}.test_excluded.TestExcluded.test_firstFailureCounts: "
		"AssertionError: 0 != -1 : first cause"
	) in output
	assert (
		f"import fails: {PACKAGE}.excludedPackage.test_b: ImportError: probe excluded package, again"
		in output
	)


@pytest.mark.parametrize(
	("where", "modules", "message"),
	[
		(
			"a module at import",
			{"test_exits.py": "import sys\nsys.exit(0)\n"},
			"rununittests: a test, fixture or module exited the interpreter",
		),
		(
			"the bootstrap",
			{"__init__.py": BOOTSTRAP + "\nimport sys\nsys.exit(0)\n"},
			"rununittests: a test, fixture or module exited the interpreter",
		),
		(
			"a class fixture after a failure",
			{
				"test_exits.py": """
					import sys
					import unittest

					class TestA(unittest.TestCase):
						def test_fails(self):
							self.fail("probe")

					class TestB(unittest.TestCase):
						@classmethod
						def setUpClass(cls):
							sys.exit(0)

						def test_neverRuns(self):
							pass
				""",
			},
			"rununittests: a test, fixture or module exited the interpreter",
		),
		(
			"a test, through os._exit, after a failure",
			{
				"test_exits.py": """
					import os
					import unittest

					class TestA(unittest.TestCase):
						def test_fails(self):
							self.fail("probe")

						def test_zexits(self):
							os._exit(0)
				""",
			},
			"rununittests: the test process ended with status 0 before recording its result",
		),
	],
)
def test_endingTheInterpreterFailsTheRun(tmp_path: Path, where: str, modules: dict[str, str], message: str):
	listPath = makeProbes(tmp_path, {"__init__.py": BOOTSTRAP} | modules, "")
	status, lines, output = runRunner(tmp_path, listPath)
	assert status == 1, f"{where}: {output}"
	assert any(line.startswith(message) for line in lines), f"{where}: {output}"
	assert not any(" tests ran: " in line for line in lines), f"{where}: {output}"


def test_threadLeftRunningFailsTheRunWithoutHanging(tmp_path: Path):
	modules = {
		"__init__.py": BOOTSTRAP,
		"test_lingers.py": """
			import threading
			import time
			import unittest

			class TestLingers(unittest.TestCase):
				def test_startsAThread(self):
					threading.Thread(target=time.sleep, args=(600,), name="probe lingering").start()
		""",
	}
	listPath = makeProbes(tmp_path, modules, "")
	started = time.monotonic()
	status, lines, output = runRunner(tmp_path, listPath, "--timeout", "120")
	assert time.monotonic() - started < 60, output
	assert status == 1, output
	assert "rununittests: a test left the thread 'probe lingering' running" in lines
	assert any(line.startswith("rununittests: 1 tests ran: 1 passed") for line in lines), output


def test_timeoutEndsTheRunAndLeavesNothingBehind(tmp_path: Path):
	modules = {
		"__init__.py": BOOTSTRAP,
		"test_hangs.py": """
			import time
			import unittest

			class TestHangs(unittest.TestCase):
				def test_hangs(self):
					time.sleep(120)
		""",
		"test_hangsAtImport.py": "import time\ntime.sleep(120)\n",
	}
	exclusions = f"{PACKAGE}.test_hangsAtImport\n  Until probe.\n  Import raises: ImportError: probe\n"
	listPath = makeProbes(tmp_path, modules, exclusions)
	started = time.monotonic()
	status, lines, output = runRunner(tmp_path, listPath, "--timeout", "3")
	assert status == 1, output
	assert time.monotonic() - started < 60
	assert "rununittests: no result after 3 s" in output
	assert "test_hangs" in output
	assert (
		"rununittests: the test process recorded no result within its timeout of 3 s, and its watchdog ended it"
		in lines
	), output
	assert any(
		line.startswith(
			f"rununittests: the import check for the exclusion {PACKAGE}.test_hangsAtImport recorded no "
			"result within its timeout of 3 s, and its watchdog ended it; its output follows"
		)
		for line in lines
	), output
	assert "ended the interpreter" not in output


def checkoutStandIn(tmp_path: Path) -> Path:
	"""A writable directory for the runner to watch in place of the read-only checkout."""
	checkout = tmp_path / "checkout"
	checkout.mkdir()
	(checkout / "same.txt").write_text("original")
	return checkout


def test_changesByChildProcessesFailTheRun(tmp_path: Path):
	"""The audit hook sees only the process running the tests; what child processes change is found
	afterwards, even a rewrite that keeps the file's size and puts its times back."""
	checkout = checkoutStandIn(tmp_path)
	modules = {
		"__init__.py": BOOTSTRAP,
		"test_writes.py": f"""
			import subprocess
			import sys
			import unittest

			CHECKOUT = {str(checkout)!r}
			REWRITE = (
				"import os\\n"
				"path = " + repr(CHECKOUT + "/same.txt") + "\\n"
				"times = os.stat(path)\\n"
				"with open(path, 'r+b') as f:\\n"
				"    f.write(b'ALTERED!')\\n"
				"os.utime(path, ns=(times.st_atime_ns, times.st_mtime_ns))\\n"
			)

			class TestWrites(unittest.TestCase):
				def test_createsFromAChild(self):
					create = "open(" + repr(CHECKOUT + "/new.txt") + ", 'w').close()"
					subprocess.run([sys.executable, "-c", create], check=True)

				def test_rewritesKeepingSizeAndTimes(self):
					subprocess.run([sys.executable, "-c", REWRITE], check=True)
		""",
	}
	listPath = makeProbes(tmp_path, modules, "")
	status, lines, output = runRunner(tmp_path, listPath, "--checkout", str(checkout))
	assert status == 1, output
	assert f"rununittests: the run changed {checkout / 'new.txt'}" in lines
	assert f"rununittests: the run changed {checkout / 'same.txt'}" in lines
	assert any(line.startswith("rununittests: 2 tests ran: 2 passed") for line in lines), output


def test_writeThroughALinkIntoTheCheckoutIsRefused(tmp_path: Path):
	checkout = checkoutStandIn(tmp_path)
	modules = {
		"__init__.py": BOOTSTRAP,
		"test_writes.py": f"""
			import os
			import tempfile
			import unittest

			class TestWrites(unittest.TestCase):
				def test_writesThroughALink(self):
					link = os.path.join(tempfile.gettempdir(), "link")
					os.symlink({str(checkout)!r}, link)
					try:
						with open(os.path.join(link, "same.txt"), "r+b") as f:
							f.write(b"ALTERED!")
					except PermissionError:
						pass
		""",
	}
	listPath = makeProbes(tmp_path, modules, "")
	status, lines, output = runRunner(tmp_path, listPath, "--checkout", str(checkout))
	assert status == 1, output
	assert any(line.startswith(f"rununittests: refused open of {checkout / 'same.txt'}") for line in lines)
	assert (checkout / "same.txt").read_text() == "original"


def test_audioLibraryLinkedThroughAnExtensionModuleIsRefused(tmp_path: Path):
	"""The bootstrap's wx.html2 maps GStreamer and PulseAudio, which is allowed; wx.media links the
	same GStreamer, already mapped, and is refused all the same."""
	modules = {
		"__init__.py": BOOTSTRAP + "\nimport wx.html2\n",
		"test_media.py": """
			import unittest

			class TestMedia(unittest.TestCase):
				def test_importsMedia(self):
					import wx.media
		""",
	}
	listPath = makeProbes(tmp_path, modules, "")
	status, lines, output = runRunner(tmp_path, listPath)
	assert status == 1, output
	refusals = [line for line in lines if line.startswith("rununittests: refused ")]
	assert refusals, output
	assert all(
		re.search(r"which links the audio library \S*libgst\S*, during probeUnit\.test_media", line)
		for line in refusals
	), refusals
	assert any("/wx/_media." in line for line in refusals), refusals


def test_audioLibraryTheBootstrapMapsIsRefusedUnlessAnAllowedModuleLinksIt(tmp_path: Path):
	modules = {
		"__init__.py": BOOTSTRAP + "\nimport wx.media\n",
		"test_passes.py": """
			import unittest

			class TestPasses(unittest.TestCase):
				def test_passes(self):
					pass
		""",
	}
	listPath = makeProbes(tmp_path, modules, "")
	status, lines, output = runRunner(tmp_path, listPath)
	assert status == 1, output
	refusals = [line for line in lines if line.startswith("rununittests: refused ")]
	assert refusals, output
	assert all(
		re.fullmatch(
			r"rununittests: refused the audio library \S*libgst\S*, which the bootstrap mapped unlinked by "
			r"AUDIO_LINKERS",
			line,
		)
		for line in refusals
	), refusals


def test_entriesMustStateTheirExpectedCause(tmp_path: Path):
	cases = {
		"probeUnit.test_a\n  Until probe.\n": "needs exactly one line",
		"probeUnit.test_a\n  Until probe.\n  Raises: no class here\n": "write an expected cause",
		"probeUnit.test_a\n  Until probe.\n  Raises: A: b\n  Not run: c.\n": "needs exactly one line",
		"probeUnit.test_a\n  Until probe.\n  Raises: A: b\n  Import raises: A: b\n": "a second expected cause",
		"skipped probeUnit.test_a\n  Probe.\n  Raises: A: b\n": "can state no cause",
	}
	for text, message in cases.items():
		listPath = tmp_path / "list.txt"
		listPath.write_text(text, encoding="utf-8")
		with pytest.raises(ValueError, match=message):
			rununittests.readEntries(listPath, PACKAGE)


def test_expectationMatchesAnyExceptionInTheChain():
	try:
		try:
			raise KeyError("inner cause")
		except KeyError as e:
			raise RuntimeError("outer") from e
	except RuntimeError as e:
		chain = rununittests.describeChain(e)
	assert rununittests.Expectation("LookupError", "inner cause").matches(chain)
	assert rununittests.Expectation("Exception", "outer").matches(chain)
	# The class and the fragment must belong to the same exception.
	assert not rununittests.Expectation("KeyError", "outer").matches(chain)


def test_standInIsInPlaceOnlyWhileItsTestModuleIsImported():
	name = "synthDrivers.oneCore"
	assert name not in sys.modules
	with rununittests.standingIn("tests.unit.test_synthDriverHandler"):
		assert sys.modules[name].SynthDriver.check() is False
	assert name not in sys.modules
	with rununittests.standingIn("tests.unit.test_other"):
		assert name not in sys.modules


def test_treeStateSeesAFileWrittenAndRemovedAgain(tmp_path: Path):
	(tmp_path / "sub").mkdir()
	before = rununittests.treeState(tmp_path)
	time.sleep(0.01)
	transient = tmp_path / "sub" / "transient.tmp"
	transient.write_text("x")
	transient.unlink()
	assert rununittests.treeState(tmp_path) != before


def test_treeStateSeesAFileRewrittenWithItsTimesRestored(tmp_path: Path):
	path = tmp_path / "file.txt"
	path.write_text("original")
	times = path.stat()
	before = rununittests.treeState(tmp_path)
	time.sleep(0.01)
	path.write_text("ALTERED!")
	os.utime(path, ns=(times.st_atime_ns, times.st_mtime_ns))
	assert rununittests.treeState(tmp_path) != before


def test_writtenPathsResolvesDescriptorRelativePathsAndLinks(tmp_path: Path):
	real = tmp_path / "real"
	real.mkdir()
	(real / "file").write_text("x")
	(tmp_path / "dirLink").symlink_to(real)
	(tmp_path / "fileLink").symlink_to(real / "file")
	fd = os.open(tmp_path, os.O_RDONLY)
	try:
		assert rununittests.writtenPaths("os.mkdir", ("new", 0o777, fd)) == [str(tmp_path / "new")]
		assert rununittests.writtenPaths("os.mkdir", (str(tmp_path / "abs"), 0o777, -1)) == [
			str(tmp_path / "abs")
		]
		assert rununittests.writtenPaths("open", (str(tmp_path / "f"), "r", 0)) == []
		assert rununittests.writtenPaths("open", (str(tmp_path / "f"), None, os.O_WRONLY | os.O_CREAT)) == [
			str(tmp_path / "f")
		]
		# Removing what does not exist changes nothing.
		assert rununittests.writtenPaths("os.remove", (str(tmp_path / "missing"), -1)) == []
		# A directory reached through a link is resolved; so is a link at the end of the path, when the
		# event writes to what it points to, but not when the event removes the link itself.
		assert rununittests.writtenPaths("open", (str(tmp_path / "dirLink" / "file"), "w", 0)) == [
			str(real / "file")
		]
		assert rununittests.writtenPaths("open", (str(tmp_path / "fileLink"), "w", 0)) == [
			str(tmp_path / "fileLink"),
			str(real / "file"),
		]
		assert rununittests.writtenPaths("os.remove", (str(tmp_path / "fileLink"), -1)) == [
			str(tmp_path / "fileLink")
		]
		# ".." after a link leads out of where the link points to, not back to where the link is.
		(real / "inner").mkdir()
		(tmp_path / "outside").mkdir()
		(tmp_path / "outside" / "innerLink").symlink_to(real / "inner")
		up = str(tmp_path / "outside" / "innerLink" / os.pardir)
		assert rununittests.writtenPaths("os.mkdir", (os.path.join(up, "new"), 0o777, -1)) == [
			str(real / "new")
		]
		assert rununittests.writtenPaths("os.rmdir", (up, -1)) == [str(real)]
	finally:
		os.close(fd)


def test_outboundRemoteConnectionKeepsAliveAsOnWindows(tmp_path: Path):
	done = harness.runChild(
		tmp_path,
		"""
		import socket
		from unittest import mock

		# NVDA's imports form one cycle, which resolves only when entered where tests/unit enters it.
		import appModuleHandler
		from _remoteClient.transport import TCPTransport

		transport = TCPTransport(mock.MagicMock(), ("localhost", 8090))
		sock = transport.createOutboundSocket("localhost", 8090, insecure=True)
		try:
			result = {
				"keepAlive": sock.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE),
				"idle": sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE),
				"interval": sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL),
				"count": sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT),
			}
		finally:
			sock.close()
		""",
	)
	done.assertClean()
	# Windows' SIO_KEEPALIVE_VALS (1, 60000, 2000), with the 10 probes Windows always sends.
	assert done.value == {"keepAlive": 1, "idle": 60, "interval": 2, "count": 10}
