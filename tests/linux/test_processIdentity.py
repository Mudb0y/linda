"""App modules find their application from the process on Linux, through /proc and pidfd.

Flatpak apps are not covered: they reach the accessibility bus through xdg-dbus-proxy, so the
process ID the bus reports for one of their objects is the proxy's, not the app's.

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

#: Defines start(path, *args), which starts a process the child owns and so may stop,
#: stop(process), and makeZombie(process), which kills it and leaves it unreaped. Any process it
#: starts is stopped when the child exits.
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
	# Popen returns once exec is past the point of no return, before the kernel has given the new
	# program its arguments, and a program can exec another before it runs: the dev shell's python is a
	# wrapper that execs the interpreter, and /proc shows no command line again while it does. Every
	# program started here ends up sleeping, which only the final program does.
	deadline = time.monotonic() + 60
	while not sleeping(process.pid):
		if process.poll() is not None or time.monotonic() > deadline:
			raise TimeoutError(f"{executable} did not reach its sleep: {process.poll()}")
		time.sleep(0.001)
	return process

def sleeping(pid):
	with open(f"/proc/{pid}/stat", "rb") as f:
		state = f.read().rpartition(b")")[2].split()[0]
	with open(f"/proc/{pid}/wchan", encoding="ascii") as f:
		where = f.read()
	# The kernel hides where an undumpable process waits, as from anyone who may not trace it.
	return state == b"S" and ("nanosleep" in where or where == "0")

def stop(process):
	if process.poll() is None:
		process.kill()
	process.wait()

def makeZombie(process):
	process.kill()
	deadline = time.monotonic() + 10
	while time.monotonic() < deadline:
		with open(f"/proc/{process.pid}/stat", "rb") as f:
			if f.read().rpartition(b")")[2].split()[0] == b"Z":
				return
		time.sleep(0.001)
	raise TimeoutError("the process did not become a zombie")
"""

#: Makes /proc/PID/exe unreadable for the process IDs in the set unreadable, as it is for another
#: user's process, which this user cannot start.
UNREADABLE = """
import os
unreadable = set()
_readlink = os.readlink
def readlink(path, *args, **kwargs):
	if any(path == f"/proc/{pid}/exe" for pid in unreadable):
		raise PermissionError(13, "Permission denied", path)
	return _readlink(path, *args, **kwargs)
os.readlink = readlink
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
		"java options with values": (
			["java", "--add-opens", "java.base/java.lang=ALL-UNNAMED", "-jar", "/opt/app.jar"],
			"app",
		),
		"java module attached": (["java", "--module=org.foo/org.foo.Main", "arg"], "org.foo.main"),
		"java source file": (["java", "--source", "21", "Tool.java"], "tool"),
		"java describe module": (["java", "-d", "org.foo"], "java"),
		"node env file": (["node", "--env-file", ".env", "server.js"], "server"),
		"node V8 option": (["node", "--max-old-space-size", "4096", "app.js"], "app"),
		"node package script": (["node", "--run", "build"], "node"),
		"standard input": (["python3", "-"], "python"),
		"no program": (["python3.13"], "python"),
		"free-threaded python": (["python3.13t", "app.py"], "app"),
		"electron archive": (
			[
				"electron",
				"--ozone-platform-hint=auto",
				"/nix/store/x-vesktop/share/vesktop/resources/app.asar",
			],
			"vesktop",
		),
		"electron directory": (["electron", "/nix/store/x-element/share/element/electron"], "element"),
		"electron app directory": (["electron", "-r", "hook.js", "/opt/Signal/app"], "signal"),
		"electron no app": (["electron"], "electron"),
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


def test_productInfoRaises(tmp_path: Path):
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
	# The kernel denies another user's /proc/PID/exe to anyone without the right to trace the process,
	# and it denies it in the same way for this user's own process when that process is undumpable,
	# which a process becomes by executing a file it may not read, unless fs.suid_dumpable is 1 and keeps
	# it dumpable. The rig maps only this user, so the process's owner is the one fact the test cannot
	# make real, and it changes whom NVDA runs as instead.
	suidDumpable = Path("/proc/sys/fs/suid_dumpable").read_text().strip()
	assert suidDumpable != "1", "fs.suid_dumpable is 1, so no process of this user can be made undumpable"
	path = copyAs(tmp_path, "otherapp")
	os.chmod(path, 0o111)
	done = harness.runChild(
		tmp_path,
		START
		+ f"""
import os, appModuleHandler
from linuxPlatform import processes
process = start({path!r}, "60", argv0="/elsewhere/sleep")
processes.userID = os.getuid() + 1
mod = appModuleHandler.getAppModuleFromProcessID(process.pid)
result = {{
	"appName": mod.appName,
	"alive": mod.isAlive,
	"appPath": mod.appPath,
	"appArchitecture": mod.appArchitecture,
	"is64BitProcess": mod.is64BitProcess,
	"isRunningUnderDifferentLogonSession": mod.isRunningUnderDifferentLogonSession,
}}
try:
	mod.processExecutablePath
except OSError as e:
	result["processExecutablePath"] = type(e).__name__
processes.userID = os.getuid()
result["undumpable but this user's"] = processes.isOtherUser(process.pid)
""",
	)
	done.assertClean()
	assert done.value == {
		"appName": "sleep",
		"alive": True,
		"appPath": None,
		"appArchitecture": "unknown",
		"is64BitProcess": False,
		"isRunningUnderDifferentLogonSession": True,
		"processExecutablePath": "PermissionError",
		"undumpable but this user's": False,
	}


def test_unreadableExecutableIsNamedFromArgv0ThenComm(tmp_path: Path):
	path = copyAs(tmp_path, "filename")
	done = harness.runChild(
		tmp_path,
		START
		+ UNREADABLE
		+ f"""
import appModuleHandler
from linuxPlatform import processes
result = {{}}
process = start({path!r}, "60", argv0="/elsewhere/sleep")
# Run as a multi-call binary, coreutils renames itself after argv[0] through PR_SET_NAME soon after it
# starts, so comm settles on that name cut to 15 bytes, which differs from both argv[0]'s and the
# file's name.
deadline = time.monotonic() + 10
while time.monotonic() < deadline:
	with open(f"/proc/{{process.pid}}/comm") as f:
		if f.read().strip() == "/elsewhere/slee":
			break
	time.sleep(0.001)
unreadable.add(process.pid)
result["argv[0]"] = appModuleHandler.getAppNameFromProcessID(process.pid)
readCommandLine = processes._readCommandLine
processes._readCommandLine = lambda pid: [] if pid == process.pid else readCommandLine(pid)
result["comm"] = appModuleHandler.getAppNameFromProcessID(process.pid)
processes._readCommandLine = readCommandLine
makeZombie(process)
result["zombie"] = appModuleHandler.getAppNameFromProcessID(process.pid)
""",
	)
	done.assertClean()
	assert done.value == {"argv[0]": "sleep", "comm": "/elsewhere/slee", "zombie": ""}


def test_zombieIsUnnamedAndCleanedUp(tmp_path: Path):
	plain = copyAs(tmp_path, "zombieapp")
	longWrapped = copyAs(tmp_path, ".averylongname-wrapped")
	done = harness.runChild(
		tmp_path,
		START
		+ f"""
import appModuleHandler
result = {{}}
for path in ({plain!r}, {longWrapped!r}):
	process = start(path, "60", argv0="sleep")
	makeZombie(process)
	mod = appModuleHandler.getAppModuleFromProcessID(process.pid)
	appModuleHandler.cleanup()
	result[path.rsplit("/", 1)[1]] = {{
		"name": appModuleHandler.getAppNameFromProcessID(process.pid),
		"base module": type(mod) is appModuleHandler.AppModule,
		"appName": mod.appName,
		"alive": mod.isAlive,
		"kept by cleanup": process.pid in appModuleHandler.runningTable,
	}}
	process.wait()
""",
	)
	done.assertClean()
	expected = {"name": "", "base module": True, "appName": "", "alive": False, "kept by cleanup": False}
	assert done.value == {"zombieapp": expected, ".averylongname-wrapped": expected}


def test_isAliveFromManyThreads(tmp_path: Path):
	path = copyAs(tmp_path, "threadedapp")
	done = harness.runChild(
		tmp_path,
		START
		+ f"""
import threading, appModuleHandler
process = start({path!r}, "60", argv0="sleep")
mod = appModuleHandler.getAppModuleFromProcessID(process.pid)
answers = set()
errors = []
start_ = threading.Barrier(8)
def check():
	start_.wait()
	try:
		for _ in range(5000):
			answers.add(mod.isAlive)
	except Exception as e:
		errors.append(repr(e))
threads = [threading.Thread(target=check) for _ in range(8)]
for thread in threads:
	thread.start()
for thread in threads:
	thread.join()
result = {{"errors": errors, "answers": sorted(answers)}}
""",
	)
	done.assertClean()
	assert done.value == {"errors": [], "answers": [True]}


#: Makes os.pidfd_open fail, so that ProcessWatch takes the /proc fallback that runs when the kernel or
#: a full descriptor table refuses a pidfd.
REFUSE_PIDFD = """
import errno, os
def refuse(pid, flags=0):
	raise OSError(errno.ENOSYS, "refused by the test")
os.pidfd_open = refuse
"""


@pytest.mark.parametrize("watch", ["pidfd", "proc"])
def test_deadProcessIsCleanedUp(tmp_path: Path, watch: str):
	path = copyAs(tmp_path, "shortlived")
	done = harness.runChild(
		tmp_path,
		START
		+ (REFUSE_PIDFD if watch == "proc" else "")
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


def test_reusedProcessIDIsNotAliveWithoutPidfd(tmp_path: Path):
	path = copyAs(tmp_path, "reusedapp")
	done = harness.runChild(
		tmp_path,
		START
		+ REFUSE_PIDFD
		+ f"""
import appModuleHandler
from linuxPlatform import processes
process = start({path!r}, "60", argv0="sleep")
mod = appModuleHandler.getAppModuleFromProcessID(process.pid)
result = {{"usesPidfd": mod._processWatch._poll is not None, "aliveBefore": mod.isAlive}}
# The process ID now belongs to a process that started a tick later than the one the module serves.
readStartTime = processes._readStartTime
processes._readStartTime = lambda pid: str(int(readStartTime(pid)) + 1).encode()
result["aliveAfterReuse"] = mod.isAlive
""",
	)
	done.assertClean()
	assert done.value == {"usesPidfd": False, "aliveBefore": True, "aliveAfterReuse": False}


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
	# An add-on may serve a program that a bundled Windows app module is also named after.
	(addonDir / "code.py").write_text(
		"import appModuleHandler\n\nclass AppModule(appModuleHandler.AppModule):\n\tfromAddon = True\n",
		encoding="utf-8",
	)
	direct = copyAs(tmp_path, "addonapp")
	registered = copyAs(tmp_path, "other.app")
	sharedName = copyAs(tmp_path, "code")
	done = harness.runChild(
		tmp_path,
		START
		+ f"""
import appModuleHandler, appModules
# What an add-on's addToPackagePath does for its appModules directory.
appModules.__path__.insert(0, {str(addonDir)!r})
appModuleHandler.registerExecutableWithAppModule("other.app", "addonapp")
result = {{}}
for path in ({direct!r}, {registered!r}, {sharedName!r}):
	process = start(path, "60", argv0="sleep")
	mod = appModuleHandler.getAppModuleFromProcessID(process.pid)
	result[mod.appName] = [type(mod).__module__, getattr(mod, "fromAddon", False)]
	stop(process)
""",
	)
	done.assertClean()
	assert done.value == {
		"addonapp": ["appModules.addonapp", True],
		"other.app": ["appModules.addonapp", True],
		"code": ["appModules.code", True],
	}


def test_bundledWindowsAppModulesStayUnused(tmp_path: Path):
	# Each of these names a Linux program as well as a bundled app module written for Windows: code
	# and notepad fail to import, and audacity, eclipse, spotify and thunderbird fail on every object.
	names = ["code", "notepad", "audacity", "eclipse", "spotify", "thunderbird"]
	paths = [copyAs(tmp_path, name) for name in names]
	done = harness.runChild(
		tmp_path,
		START
		+ f"""
import sys, appModuleHandler, ui
from NVDAObjects import NVDAObject
spoken = []
ui.message = lambda text, *args, **kwargs: spoken.append(text)
result = {{}}
for path in {paths!r}:
	name = path.rsplit("/", 1)[1]
	process = start(path, "60", argv0="sleep")

	class InProcess(NVDAObject):
		processID = process.pid
		windowThreadID = 0
		name = "object"
		role = 0

	obj = InProcess()
	result[name] = [type(obj.appModule).__module__, obj.appModule.appName, f"appModules.{{name}}" in sys.modules]
	stop(process)
result["spoken"] = spoken
result["exists"] = {{name: appModuleHandler.doesAppModuleExist(name) for name in ("code", "audacity", "nvda")}}
""",
	)
	done.assertClean()
	assert done.value == {
		**{name: ["appModuleHandler", name, False] for name in names},
		"spoken": [],
		"exists": {"code": False, "audacity": False, "nvda": True},
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
