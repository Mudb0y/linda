"""App modules find their application from the process on Linux, through /proc and pidfd.

nix develop . -c tests/linux/rig python3 -m pytest -p no:cacheprovider tests/linux/test_processIdentity.py
"""

import os
import shutil
import sys
from pathlib import Path

import harness
import pytest

#: A binary the tests copy under other names. coreutils on NixOS is one multi-call binary that picks
#: its program from argv[0], so each copy is started with argv[0] "sleep" and keeps its own file name.
SLEEP = os.path.realpath(shutil.which("sleep") or "/bin/sleep")

#: Defines start(path, *args), which starts a process the child owns and so may stop, and
#: stop(process). Any process it starts is stopped when the child exits.
START = """
import atexit, subprocess, sys, time

def start(executable, *args, argv0=None):
	process = subprocess.Popen(
		[argv0 or executable, *args],
		executable=executable,
		stdin=subprocess.DEVNULL,
		stdout=subprocess.DEVNULL,
		stderr=subprocess.DEVNULL,
	)
	atexit.register(stop, process)
	# Popen returns once exec is past the point of no return, which is before the kernel has given
	# the new program its arguments; until then /proc shows the new executable with no command line.
	deadline = time.monotonic() + 10
	while time.monotonic() < deadline:
		with open(f"/proc/{process.pid}/cmdline", "rb") as f:
			if f.read():
				break
		time.sleep(0.001)
	return process

def stop(process):
	if process.poll() is None:
		process.kill()
	process.wait()
"""


def copyAs(directory: Path, name: str) -> str:
	path = directory / name
	shutil.copy(SLEEP, path)
	return str(path)


def writeScript(directory: Path, name: str) -> str:
	path = directory / name
	path.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(60)\n", encoding="utf-8")
	path.chmod(0o755)
	return str(path)


def test_namesOfExecutables(tmp_path: Path):
	plain = copyAs(tmp_path, "Fooapp")
	wrapped = copyAs(tmp_path, ".fooapp-wrapped")
	twiceWrapped = copyAs(tmp_path, "..barapp-wrapped-wrapped")
	notWrapped = copyAs(tmp_path, ".hidden")
	done = harness.runChild(
		tmp_path,
		START
		+ f"""
import appModuleHandler
result = {{}}
for path in {[plain, wrapped, twiceWrapped, notWrapped]!r}:
	process = start(path, "60", argv0="sleep")
	result[path.rsplit("/", 1)[1]] = appModuleHandler.getAppNameFromProcessID(process.pid)
	stop(process)
""",
	)
	done.assertClean()
	assert done.value == {
		"Fooapp": "fooapp",
		".fooapp-wrapped": "fooapp",
		"..barapp-wrapped-wrapped": "barapp",
		".hidden": ".hidden",
	}


def test_namesOfInterpretedPrograms(tmp_path: Path):
	script = writeScript(tmp_path, "myTool.py")
	wrappedScript = writeScript(tmp_path, ".mytool-wrapped")
	(tmp_path / "sleeper.py").write_text("import time\ntime.sleep(60)\n", encoding="utf-8")
	cases = {
		"script": [script],
		"options before the script": ["-u", "-X", "dev", "-Wignore", "-E", script],
		"wrapped script": [wrappedScript],
		"module": ["-m", "sleeper"],
		"module attached": ["-umsleeper"],
		"inline": ["-c", "import time; time.sleep(60)"],
	}
	done = harness.runChild(
		tmp_path,
		START
		+ f"""
import os, sys, appModuleHandler
os.chdir({str(tmp_path)!r})
result = {{}}
for case, args in {cases!r}.items():
	process = start(sys.executable, *args)
	result[case] = appModuleHandler.getAppNameFromProcessID(process.pid)
	stop(process)
process = start({script!r})
result["started through its #! line"] = appModuleHandler.getAppNameFromProcessID(process.pid)
""",
	)
	done.assertClean()
	assert done.value == {
		"script": "mytool",
		"options before the script": "mytool",
		"wrapped script": "mytool",
		"module": "sleeper",
		"module attached": "sleeper",
		"inline": "python",
		"started through its #! line": "mytool",
	}


def test_interpreterCommandLines(tmp_path: Path):
	cases = {
		"node": (["node", "--require", "x", "--inspect=1", "/usr/lib/app/main.js", "a"], "main"),
		"node inline": (["node", "-e", "1"], "node"),
		"gjs": (["gjs", "-m", "/usr/bin/org.gnome.Weather"], "org.gnome.weather"),
		"gjs include path": (["gjs-console", "-I", "/lib", "app.js"], "app"),
		"perl": (["perl", "-w", "-Mstrict", "-I", "/lib", "/usr/bin/shutter"], "shutter"),
		"perl inline": (["perl5.40.0", "-we", "1"], "perl"),
		"ruby": (["ruby3.3", "-r", "json", "tool.rb"], "tool"),
		"java jar": (["java", "-Xmx1g", "-cp", "a.jar", "-jar", "/opt/App.jar"], "app"),
		"java class": (["java", "-classpath", "lib", "org.example.Main"], "org.example.main"),
		"standard input": (["python3", "-"], "python"),
		"no program": (["python3.13"], "python"),
	}
	done = harness.runChild(
		tmp_path,
		f"""
import os
from linuxPlatform import processes
result = {{}}
for case, (argv, expected) in {cases!r}.items():
	name = os.path.basename(argv[0])
	interpreter = next(i for i in processes._interpreters if i.executable.fullmatch(name))
	program = processes._findProgram(interpreter, argv[1:])
	result[case] = (program and processes._programName(interpreter, program)) or interpreter.family
""",
		prepare="bootstrap",
	)
	done.assertClean()
	assert done.value == {case: expected for case, (argv, expected) in cases.items()}


def test_ownProcessIsNVDA(tmp_path: Path):
	done = harness.runChild(
		tmp_path,
		"""
import os, appModuleHandler
mod = appModuleHandler.getAppModuleFromProcessID(os.getpid())
result = {
	"name": appModuleHandler.getAppNameFromProcessID(os.getpid()),
	"name with extension": appModuleHandler.getAppNameFromProcessID(os.getpid(), includeExt=True),
	"module": type(mod).__module__,
	"appName": mod.appName,
	"alive": mod.isAlive,
	"same module again": appModuleHandler.getAppModuleFromProcessID(os.getpid()) is mod,
	"processHandle": mod.processHandle,
	"helperLocalBindingHandle": mod.helperLocalBindingHandle,
}
""",
	)
	done.assertClean()
	assert done.value == {
		"name": "nvda",
		"name with extension": "nvda",
		"module": "appModules.nvda",
		"appName": "nvda",
		"alive": True,
		"same module again": True,
		"processHandle": None,
		"helperLocalBindingHandle": None,
	}


def test_processFacts(tmp_path: Path):
	path = copyAs(tmp_path, "factsapp")
	done = harness.runChild(
		tmp_path,
		START
		+ f"""
import platform, appModuleHandler
process = start({path!r}, "60", argv0="sleep")
mod = appModuleHandler.getAppModuleFromProcessID(process.pid)
result = {{
	"module": type(mod).__module__,
	"appName": mod.appName,
	"processExecutablePath": mod.processExecutablePath,
	"appPath": mod.appPath,
	"is64BitProcess": mod.is64BitProcess,
	"appArchitecture": mod.appArchitecture,
	"isWindowsStoreApp": mod.isWindowsStoreApp,
	"isRunningUnderDifferentLogonSession": mod.isRunningUnderDifferentLogonSession,
	"machine": platform.machine(),
}}
""",
	)
	done.assertClean()
	facts = done.value
	expectedArchitecture = {"x86_64": "AMD64", "aarch64": "ARM64"}.get(facts.pop("machine"))
	if expectedArchitecture is None:
		pytest.skip("the expected architecture is only spelled for x86_64 and aarch64")
	assert facts == {
		"module": "appModuleHandler",
		"appName": "factsapp",
		"processExecutablePath": path,
		"appPath": path,
		"is64BitProcess": True,
		"appArchitecture": expectedArchitecture,
		"isWindowsStoreApp": False,
		"isRunningUnderDifferentLogonSession": False,
	}


def test_productInfoIsNotBuilt(tmp_path: Path):
	done = harness.runChild(
		tmp_path,
		"""
import os, appModuleHandler
from linuxPlatform import NoLinuxImplementation
mod = appModuleHandler.getAppModuleFromProcessID(os.getppid())
try:
	mod.productName
	result = "answered"
except NoLinuxImplementation:
	result = "raised"
""",
	)
	assert done.error is None, done.error
	assert done.value == "raised"
	assert done.noLinuxImplementation == ["product name and version of a process"]
	assert not done.logErrors


def test_otherUsersProcess(tmp_path: Path):
	info = os.stat("/proc/1")
	if info.st_uid == os.getuid() or os.access("/proc/1/exe", os.R_OK):
		pytest.skip("process 1 belongs to this user here")
	comm = Path("/proc/1/comm").read_text(encoding="utf-8").strip()
	if len(comm) >= 15:
		pytest.skip("process 1's comm may be cut short")
	done = harness.runChild(
		tmp_path,
		"""
import appModuleHandler
mod = appModuleHandler.getAppModuleFromProcessID(1)
result = {
	"appName": mod.appName,
	"alive": mod.isAlive,
	"appPath": mod.appPath,
	"appArchitecture": mod.appArchitecture,
	"isRunningUnderDifferentLogonSession": mod.isRunningUnderDifferentLogonSession,
}
""",
	)
	done.assertClean()
	assert done.value == {
		"appName": comm.lower(),
		"alive": True,
		"appPath": None,
		"appArchitecture": "unknown",
		"isRunningUnderDifferentLogonSession": True,
	}


@pytest.mark.parametrize("watch", ["pidfd", "proc"])
def test_deadProcessIsCleanedUp(tmp_path: Path, watch: str):
	path = copyAs(tmp_path, "shortlived")
	# The /proc fallback is what runs when the kernel or a full descriptor table refuses a pidfd.
	refusePidfd = """
import errno, os
def refuse(pid, flags=0):
	raise OSError(errno.ENOSYS, "refused by the test")
os.pidfd_open = refuse
"""
	done = harness.runChild(
		tmp_path,
		START
		+ (refusePidfd if watch == "proc" else "")
		+ f"""
import appModuleHandler
process = start({path!r}, "60", argv0="sleep")
mod = appModuleHandler.getAppModuleFromProcessID(process.pid)
result = {{"usesPidfd": mod._processWatch._poll is not None, "aliveBefore": mod.isAlive}}
terminated = []
original = type(mod).terminate
def recordTerminate(self):
	terminated.append(self.processID)
	original(self)
type(mod).terminate = recordTerminate
appModuleHandler.cleanup()
result["keptWhileAlive"] = process.pid in appModuleHandler.runningTable
process.kill()
process.wait()
result["aliveAfter"] = mod.isAlive
appModuleHandler.cleanup()
result["keptAfterDeath"] = process.pid in appModuleHandler.runningTable
result["terminated"] = terminated == [process.pid]
result["nameAfterDeath"] = appModuleHandler.getAppNameFromProcessID(process.pid)
""",
	)
	done.assertClean()
	assert done.value == {
		"usesPidfd": watch == "pidfd",
		"aliveBefore": True,
		"keptWhileAlive": True,
		"aliveAfter": False,
		"keptAfterDeath": False,
		"terminated": True,
		"nameAfterDeath": "",
	}


def test_noProcess(tmp_path: Path):
	done = harness.runChild(
		tmp_path,
		"""
import appModuleHandler
from NVDAObjects import NVDAObject

class Placeholder(NVDAObject):
	processID = None
	windowThreadID = 0

obj = Placeholder()
mod = obj.appModule
appModuleHandler.cleanup()
result = {
	"name": appModuleHandler.getAppNameFromProcessID(None),
	"base module": type(mod) is appModuleHandler.AppModule,
	"appName": mod.appName,
	"alive": mod.isAlive,
	"kept by cleanup": appModuleHandler.runningTable.get(None) is mod,
	"isRunningUnderDifferentLogonSession": mod.isRunningUnderDifferentLogonSession,
	"appPath": mod.appPath,
	"appArchitecture": mod.appArchitecture,
}
appModuleHandler.terminate()
""",
	)
	done.assertClean()
	assert done.value == {
		"name": "",
		"base module": True,
		"appName": "",
		"alive": True,
		"kept by cleanup": True,
		"isRunningUnderDifferentLogonSession": False,
		"appPath": None,
		"appArchitecture": "unknown",
	}


def test_addonAppModules(tmp_path: Path):
	addonDir = tmp_path / "addon" / "appModules"
	addonDir.mkdir(parents=True)
	(addonDir / "addonapp.py").write_text(
		"import appModuleHandler\n\nclass AppModule(appModuleHandler.AppModule):\n\tfromAddon = True\n",
		encoding="utf-8",
	)
	direct = copyAs(tmp_path, "addonapp")
	registered = copyAs(tmp_path, "other.app")
	windowsOnly = copyAs(tmp_path, "code")
	done = harness.runChild(
		tmp_path,
		START
		+ f"""
import appModuleHandler, appModules
# What an add-on's addToPackagePath does for its appModules directory.
appModules.__path__.insert(0, {str(addonDir)!r})
appModuleHandler.registerExecutableWithAppModule("other.app", "addonapp")
result = {{}}
for path in ({direct!r}, {registered!r}):
	process = start(path, "60", argv0="sleep")
	mod = appModuleHandler.getAppModuleFromProcessID(process.pid)
	result[mod.appName] = [type(mod).__module__, getattr(mod, "fromAddon", False)]
	stop(process)
process = start({windowsOnly!r}, "60", argv0="sleep")
result["Windows app module's name"] = appModuleHandler.getAppNameFromProcessID(process.pid)
""",
	)
	done.assertClean()
	assert done.value == {
		"addonapp": ["appModules.addonapp", True],
		"other.app": ["appModules.addonapp", True],
		"Windows app module's name": "code",
	}


def test_lookupsStayFast(tmp_path: Path):
	path = copyAs(tmp_path, "busyapp")
	done = harness.runChild(
		tmp_path,
		START
		+ f"""
import time, appModuleHandler
from NVDAObjects import NVDAObject

process = start({path!r}, "60", argv0="sleep")

class InProcess(NVDAObject):
	processID = process.pid
	windowThreadID = 0

objects = 2000
began = time.perf_counter()
for _ in range(objects):
	InProcess().appModule
constructions = time.perf_counter() - began

lookups = 100000
began = time.perf_counter()
for _ in range(lookups):
	appModuleHandler.getAppModuleFromProcessID(process.pid)
cached = time.perf_counter() - began

names = 500
began = time.perf_counter()
for _ in range(names):
	appModuleHandler.getAppNameFromProcessID(process.pid)
uncached = time.perf_counter() - began

cleanups = 2000
began = time.perf_counter()
for _ in range(cleanups):
	appModuleHandler.cleanup()
cleanup = time.perf_counter() - began
result = {{
	"one module": len(appModuleHandler.runningTable) == 1,
	"construction": constructions / objects,
	"cached": cached / lookups,
	"uncached": uncached / names,
	"cleanup": cleanup / cleanups,
}}
""",
	)
	done.assertClean()
	timings = done.value
	assert timings.pop("one module")
	# Bounds far above what was measured, so that only a lookup that has stopped being cached, or
	# that does real work on every call, fails.
	assert timings["construction"] < 2e-3, timings
	assert timings["cached"] < 50e-6, timings
	assert timings["uncached"] < 5e-3, timings
	assert timings["cleanup"] < 1e-3, timings
