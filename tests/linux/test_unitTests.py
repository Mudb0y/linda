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
import rununittests

ROOT = Path(__file__).resolve().parent.parent.parent
RUNNER = Path(rununittests.__file__)
PACKAGE = "probeUnit"
TRANSIENT = ROOT / "tests" / "unit" / "probeTransient.tmp"

# The real bootstrap makes its configuration directory this way; the runner has to remove it.
BOOTSTRAP = """
	import tempfile

	_configDir = tempfile.TemporaryDirectory(prefix="probe-config-")
"""

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
	"brokenPackage/__init__.py": """
		raise ImportError("probe package failure")
	""",
	"brokenPackage/test_inside.py": """
		raise SystemExit("a module inside a package that failed to import was imported")
	""",
	"test_excludedModule.py": """
		raise SystemExit("an excluded module was imported")
	""",
	"test_excluded.py": """
		import unittest

		class TestExcluded(unittest.TestCase):
			def test_stillFails(self):
				self.fail("excluded and still failing")

			def test_nowPasses(self):
				pass

			def test_notRun(self):
				raise SystemExit("a test marked not run was run")

		class TestSetUpFails(unittest.TestCase):
			def setUp(self):
				raise RuntimeError("probe setUp")

			def test_one(self):
				pass
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
	probeUnit.test_excluded.TestExcluded.test_stillFails
	  Until probe.
	probeUnit.test_excluded.TestExcluded.test_notRun
	  Until probe.
	  Not run: probe.
	probeUnit.test_excluded.TestSetUpFails
	  Until probe.
	skipped probeUnit.test_skips.TestSkips
	  Probe.
"""

PROBLEM_EXCLUSIONS = (
	EXCLUSIONS
	+ """
	probeUnit.test_excluded.TestExcluded.test_nowPasses
	  Until probe.
	probeUnit.test_stale
	  Until probe.
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
	listPath = makeProbes(tmp_path, PROBES, PROBLEM_EXCLUSIONS)
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
		f"ERROR: {PACKAGE}.brokenPackage",
		f"UNEXPECTED SUCCESS: {tests}.test_unexpectedSuccess",
		(
			f"UNEXPECTED SUCCESS: {PACKAGE}.test_excluded.TestExcluded.test_nowPasses passes, "
			f"so the exclusion {PACKAGE}.test_excluded.TestExcluded.test_nowPasses is stale"
		),
		f"UNEXPECTED SKIP: {tests}.test_skipsUnexpectedly: probe skip",
		f"rununittests: entry matches nothing: {PACKAGE}.test_stale",
		(
			"rununittests: 15 tests ran: 2 passed, 3 skipped, 2 failed as expected, "
			"2 passed unexpectedly, 2 failed, 4 errors"
		),
		"rununittests: 1 errors in class or module set-up and tear-down",
		"rununittests: excluded 1 modules, not imported; 3 tests, run expecting failure; 1 tests, not run",
	]
	for line in expected:
		assert line in lines, f"{line!r} missing from {lines}"
	refusals = [line for line in lines if line.startswith("rununittests: refused ")]
	assert len(refusals) == 2, refusals
	assert any(f"open of {TRANSIENT}, from " in line for line in refusals), refusals
	assert any("load of the audio library /nonexistent/libpulse-probe.so, from " in line for line in refusals)
	assert "a test marked not run was run" not in output
	assert "was imported" not in output
	names = {case.get("name") for case in ET.parse(tmp_path / "report.xml").iter("testcase")}
	assert {"test_fails", "test_nowPasses"} <= names
	assert any(name.startswith("test_subtests") for name in names), names


def test_cleanRunPasses(tmp_path: Path):
	unwanted = (
		"test_problems.py",
		"test_importFails.py",
		"brokenPackage/__init__.py",
		"brokenPackage/test_inside.py",
	)
	modules = {name: body for name, body in PROBES.items() if name not in unwanted}
	listPath = makeProbes(tmp_path, modules, EXCLUSIONS)
	status, lines, output = runRunner(tmp_path, listPath)
	assert status == 0, output
	assert lines == [
		"rununittests: 0 errors in class or module set-up and tear-down",
		(
			"rununittests: 5 tests ran: 1 passed, 2 skipped, 2 failed as expected, "
			"0 passed unexpectedly, 0 failed, 0 errors"
		),
		"rununittests: excluded 1 modules, not imported; 2 tests, run expecting failure; 1 tests, not run",
	]


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
	}
	listPath = makeProbes(tmp_path, modules, "")
	started = time.monotonic()
	status, _lines, output = runRunner(tmp_path, listPath, "--timeout", "3")
	assert status == 1, output
	assert time.monotonic() - started < 60
	assert "rununittests: no result after 3 s" in output
	assert "test_hangs" in output


def test_treeStateSeesAFileWrittenAndRemovedAgain(tmp_path: Path):
	(tmp_path / "sub").mkdir()
	before = rununittests.treeState(tmp_path)
	time.sleep(0.01)
	transient = tmp_path / "sub" / "transient.tmp"
	transient.write_text("x")
	transient.unlink()
	assert rununittests.treeState(tmp_path) != before


def test_writtenPathsResolvesDescriptorRelativePaths(tmp_path: Path):
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
