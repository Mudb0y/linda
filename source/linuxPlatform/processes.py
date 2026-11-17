"""Facts about other processes, read from /proc, for appModuleHandler.

Windows NVDA asks the kernel through a process handle; Linux has no such handle, and everything
NVDA asks of one is either in /proc or answered by a pidfd.
"""

import os
import re
import select
import threading
import weakref
from dataclasses import dataclass

from . import NoLinuxImplementation

# NixOS's wrapProgram moves a program to .name-wrapped and puts a wrapper in its place, and a
# program wrapped twice becomes ..name-wrapped-wrapped.
_wrappedName = re.compile(r"\.(.+)-wrapped")


def _unwrap(name: str) -> str:
	while match := _wrappedName.fullmatch(name):
		name = match.group(1)
	return name


#: NVDA's bundled app modules that work on Linux. The others were written against Windows objects and
#: window classes, and a Linux program that shares a Windows program's name, as VS Code's code and
#: Audacity do, would load one and fail in it on every object. A bundled module therefore stays unused
#: until it is ported and named here. Add-on app modules are not limited, since their authors chose the
#: program and the platform they serve.
#: marco's and caja's were written for MATE's programs, not for Windows programs of those names.
portedAppModules: frozenset[str] = frozenset({"nvda", "marco", "caja"})


def isAppModuleUsable(name: str, origin: str | None, bundledDirectory: str) -> bool:
	"""Whether the app module named name, found at origin, may serve a Linux process."""
	if name in portedAppModules or not origin:
		return True
	bundled = os.path.realpath(bundledDirectory) + os.sep
	return not os.path.realpath(origin).startswith(bundled)


@dataclass(frozen=True)
class _Interpreter:
	"""Where in an interpreter's command line the program it runs is named."""

	#: The app name when no program file can be found, so that a single app module can host every
	#: version of the interpreter through getAppNameFromHost.
	family: str
	executable: re.Pattern[str]
	#: Options taking a value, either attached or as the next argument.
	valueOptions: frozenset[str] = frozenset()
	#: Options taking only an attached value, which may be empty.
	attachedOptions: frozenset[str] = frozenset()
	#: Options after which the program is given inline, so there is no file to name it after.
	inlineOptions: frozenset[str] = frozenset()
	#: Options whose value names the program, as python -m or java -jar do.
	programOptions: frozenset[str] = frozenset()
	#: Whether single-letter options combine into one argument, as in python -uc.
	clusters: bool = False
	suffixes: tuple[str, ...] = ()
	#: Trailing path components that hold an app rather than name it, as electron is pointed at
	#: name/resources/app.asar or name/electron.
	containers: frozenset[str] = frozenset()


_interpreters: tuple[_Interpreter, ...] = (
	_Interpreter(
		family="python",
		# A trailing t is the free-threaded build.
		executable=re.compile(r"(python|pypy)[\d.]*t?"),
		valueOptions=frozenset({"-W", "-X", "--check-hash-based-pycs"}),
		inlineOptions=frozenset({"-c"}),
		programOptions=frozenset({"-m"}),
		clusters=True,
		suffixes=(".py", ".pyw"),
	),
	_Interpreter(
		family="node",
		executable=re.compile(r"node(js)?"),
		valueOptions=frozenset(
			{
				"-r",
				"--require",
				"--import",
				"--loader",
				"--experimental-loader",
				"-C",
				"--conditions",
				"--title",
				"--env-file",
				"--env-file-if-exists",
				"--input-type",
				"--inspect-port",
				"--debug-port",
				"--disable-warning",
				"--dns-result-order",
				"--unhandled-rejections",
				"--redirect-warnings",
				"--openssl-config",
				"--icu-data-dir",
				"--localstorage-file",
				"--snapshot-blob",
				"--watch-path",
				"--allow-fs-read",
				"--allow-fs-write",
				"--diagnostic-dir",
				"--report-dir",
				"--report-directory",
				"--report-filename",
				"--report-signal",
				"--heapsnapshot-signal",
				"--cpu-prof-dir",
				"--cpu-prof-name",
				"--heap-prof-dir",
				"--heap-prof-name",
				"--trace-event-categories",
				"--trace-event-file-pattern",
				"--secure-heap",
				"--secure-heap-min",
				"--max-http-header-size",
				"--tls-cipher-list",
				"--use-largepages",
				# V8's own options also take their value from the next argument.
				"--max-old-space-size",
				"--max-semi-space-size",
				"--stack-size",
				"--stack-trace-limit",
			},
		),
		# --run runs a package.json script, which has no file to name the program after.
		inlineOptions=frozenset({"-e", "--eval", "-p", "--print", "--run"}),
		suffixes=(".js", ".mjs", ".cjs"),
	),
	_Interpreter(
		family="gjs",
		executable=re.compile(r"gjs(-console)?"),
		valueOptions=frozenset({"-I", "--include-path", "--coverage-prefix", "--coverage-output"}),
		inlineOptions=frozenset({"-c", "--command"}),
		suffixes=(".js", ".mjs"),
	),
	_Interpreter(
		family="perl",
		executable=re.compile(r"perl[\d.]*"),
		valueOptions=frozenset({"-I"}),
		attachedOptions=frozenset({"-0", "-C", "-d", "-D", "-F", "-i", "-l", "-m", "-M", "-V", "-x"}),
		inlineOptions=frozenset({"-e", "-E"}),
		clusters=True,
		suffixes=(".pl",),
	),
	_Interpreter(
		family="ruby",
		executable=re.compile(r"ruby[\d.]*"),
		valueOptions=frozenset({"-I", "-r", "-C", "-E"}),
		attachedOptions=frozenset({"-0", "-F", "-K", "-T", "-W", "-x"}),
		inlineOptions=frozenset({"-e"}),
		clusters=True,
		suffixes=(".rb",),
	),
	_Interpreter(
		family="java",
		executable=re.compile(r"java"),
		valueOptions=frozenset(
			{
				"-cp",
				"-classpath",
				"--class-path",
				"-p",
				"--module-path",
				"--add-modules",
				"--upgrade-module-path",
				"--add-opens",
				"--add-exports",
				"--add-reads",
				"--patch-module",
				"--limit-modules",
				"--enable-native-access",
				"--source",
			},
		),
		inlineOptions=frozenset({"-d", "--describe-module"}),
		programOptions=frozenset({"-jar", "-m", "--module"}),
		suffixes=(".jar", ".java"),
	),
	# nixpkgs runs many apps on one shared electron, which it points at the app's directory or archive.
	_Interpreter(
		family="electron",
		executable=re.compile(r"electron"),
		valueOptions=frozenset({"-r", "--require"}),
		inlineOptions=frozenset({"-i", "--interactive"}),
		suffixes=(".js", ".mjs", ".cjs", ".asar"),
		containers=frozenset({"app.asar", "app", "resources", "electron"}),
	),
)


def _findProgram(interpreter: _Interpreter, args: list[str]) -> str | None:
	"""The script, module or class an interpreter runs, from its arguments after argv[0]."""
	position = 0
	while position < len(args):
		arg = args[position]
		position += 1
		following = args[position] if position < len(args) else None
		if arg == "--":
			return following
		if arg == "-":
			# A program read from standard input has no file to name it after.
			return None
		if not arg.startswith("-"):
			return arg
		if arg.startswith("--") and "=" in arg:
			option, _, value = arg.partition("=")
			if option in interpreter.programOptions:
				return value
			if option in interpreter.inlineOptions:
				return None
			continue
		if arg in interpreter.inlineOptions:
			return None
		if arg in interpreter.programOptions:
			return following
		if arg in interpreter.valueOptions:
			position += 1
			continue
		if arg.startswith("--") or not interpreter.clusters:
			continue
		for index in range(1, len(arg)):
			option = "-" + arg[index]
			attached = arg[index + 1 :]
			if option in interpreter.inlineOptions:
				return None
			if option in interpreter.programOptions:
				return attached or following
			if option in interpreter.valueOptions:
				if not attached:
					position += 1
				break
			if option in interpreter.attachedOptions:
				break
	return None


def _programName(interpreter: _Interpreter, program: str) -> str:
	path = program.rstrip("/")
	while path and os.path.basename(path) in interpreter.containers:
		path = os.path.dirname(path)
	name = _unwrap(os.path.basename(path))
	for suffix in interpreter.suffixes:
		if name.lower().endswith(suffix) and len(name) > len(suffix):
			name = name[: -len(suffix)]
			break
	return name.lower()


def _readCommandLine(processID: int) -> list[str]:
	"""The process's arguments; empty for a zombie, a kernel thread, or a process that has gone."""
	try:
		with open(f"/proc/{processID}/cmdline", "rb") as f:
			raw = f.read()
	except OSError:
		return []
	if not raw:
		return []
	# A program that rewrites its command line, as Chromium's processes do, may leave off the final NUL.
	return [os.fsdecode(arg) for arg in raw.removesuffix(b"\0").split(b"\0")]


def getExecutablePath(processID: int) -> str:
	"""The executable's path. Raises OSError when it cannot be read, as for another user's process."""
	return os.readlink(f"/proc/{processID}/exe").removesuffix(" (deleted)")


def findExecutablePath(processID: int) -> str | None:
	try:
		return getExecutablePath(processID)
	except OSError:
		return None


def getAppName(processID: int | None) -> str:
	"""The application name NVDA keys app modules and profile triggers on.

	It is the executable's name in lower case, as on Windows, without NixOS's wrapper decoration.
	For an interpreter it is the name of the program the interpreter runs, without its suffix.
	"""
	if processID is None:
		# Upstream's placeholder objects and those of the unit tests belong to no process. Windows
		# finds no process with this ID either and answers with an empty name, and so does this.
		return ""
	args: list[str] | None = None
	try:
		executable = os.path.basename(getExecutablePath(processID))
	except PermissionError:
		# Reading the executable link needs the right to trace the process, which another user's
		# process does not grant. Windows names every process whoever owns it, and the name picks the
		# app module and the profile, so this falls back to argv[0], which anyone may read, and then to
		# comm, cut to 15 bytes but there for as long as the process exists.
		if _readStat(processID) is None:
			# Another user's zombie has no executable left either, and is as nameless as this user's.
			return ""
		args = _readCommandLine(processID)
		if args:
			executable = os.path.basename(args[0])
		else:
			try:
				with open(f"/proc/{processID}/comm", encoding="utf-8", errors="surrogateescape") as f:
					executable = f.read().rstrip("\n")
			except OSError:
				# It exited after its state was read.
				return ""
	except OSError:
		# The process has exited, or is a zombie whose parent has not reaped it yet; either way it has no
		# executable left. Windows answers an empty name for a process it no longer finds, the base app
		# module takes it, and cleanup() drops that module once the process is seen to be gone.
		return ""
	name = _unwrap(executable).lower()
	for interpreter in _interpreters:
		if interpreter.executable.fullmatch(name):
			if args is None:
				args = _readCommandLine(processID)
			program = _findProgram(interpreter, args[1:])
			return (program and _programName(interpreter, program)) or interpreter.family
	return name


def _readElfHeader(processID: int) -> tuple[int, int] | None:
	"""The ELF class and machine of the process's executable, or None when it cannot be read."""
	try:
		with open(f"/proc/{processID}/exe", "rb") as f:
			header = f.read(20)
	except OSError:
		return None
	if len(header) < 20 or header[:4] != b"\x7fELF":
		return None
	byteOrder = "little" if header[5] == 1 else "big"
	return header[4], int.from_bytes(header[18:20], byteOrder)


# ELF machine numbers, spelled as AppModule.appArchitecture spells them on Windows.
_architectures: dict[int, str] = {
	3: "x86",
	62: "AMD64",
	40: "ARM",
	183: "ARM64",
}
_elfClass64 = 2


def getArchitecture(processID: int) -> str:
	"""The architecture the process runs as, or "unknown", which Windows also answers when it cannot tell."""
	header = _readElfHeader(processID)
	if header is None:
		return "unknown"
	return _architectures.get(header[1], "unknown")


def is64Bit(processID: int) -> bool:
	"""Whether the process is 64-bit; False when that cannot be read, as upstream answers on Windows."""
	header = _readElfHeader(processID)
	return header is not None and header[0] == _elfClass64


#: The real user ID NVDA runs as, which isOtherUser compares each process's real user ID with.
userID: int = os.getuid()


def isOtherUser(processID: int | None) -> bool:
	"""Whether the process runs as a different user from NVDA.

	This is the Linux sense of Windows' different logon session: a program started with runas there,
	with sudo or pkexec here. The logind or audit session is not used, because programs that the user's
	own service manager starts, D-Bus activated ones included, sit outside the login session. Nor is the
	owner of the files in /proc/PID, which are root's for a process that makes itself undumpable, as
	ssh-agent does.
	"""
	if processID is None:
		return False
	try:
		with open(f"/proc/{processID}/status", "rb") as f:
			for line in f:
				if line.startswith(b"Uid:"):
					return int(line.split()[1]) != userID
	except OSError:
		pass
	return False


def getProductInfo(processID: int) -> tuple[str, str]:
	"""The product name and version, which Windows reads from the executable's version resource."""
	raise NoLinuxImplementation(
		"product name and version of a process",
		"Linux executables carry no version resource",
	)


def _readStat(processID: int) -> list[bytes] | None:
	"""The fields of /proc/PID/stat after comm, or None if the process is gone or a zombie."""
	try:
		with open(f"/proc/{processID}/stat", "rb") as f:
			stat = f.read()
	except OSError:
		return None
	# comm, in parentheses, may itself contain spaces and parentheses.
	fields = stat.rpartition(b")")[2].split()
	if len(fields) < 20 or fields[0] in (b"Z", b"X"):
		return None
	return fields


def _readStartTime(processID: int) -> bytes | None:
	"""The process's start time in clock ticks since boot, or None if it is gone or a zombie."""
	fields = _readStat(processID)
	return fields[19] if fields is not None else None


class ProcessWatch:
	"""Whether one process still runs, without being fooled by its process ID being reused.

	A pidfd stays bound to the process it was opened for and becomes readable when that process exits,
	so a check is one poll, cheap enough for appModuleHandler.cleanup() on every focus change.
	"""

	def __init__(self, processID: int | None) -> None:
		self.processID = processID
		self._alive = True
		# A select.poll object refuses to be polled from two threads at once, and the pidfd must not be
		# closed under a poll in progress.
		self._lock = threading.Lock()
		self._poll: select.poll | None = None
		self._startTime: bytes | None = None
		if processID is None:
			# No process can end under objects that belong to none, and reporting them dead would have
			# cleanup() drop their app module, and the objects' reference to it, on every focus change.
			return
		try:
			pidfd = os.pidfd_open(processID)
		except ProcessLookupError:
			self._alive = False
			return
		except OSError:
			# A full descriptor table, or a kernel before 5.3. The start time tells a reused process ID
			# apart as well, at the cost of reading /proc on each check.
			self._startTime = _readStartTime(processID)
			self._alive = self._startTime is not None
			return
		self._poll = select.poll()
		self._poll.register(pidfd, select.POLLIN)
		self._release = weakref.finalize(self, os.close, pidfd)

	@property
	def isAlive(self) -> bool:
		if not self._alive:
			return False
		with self._lock:
			if self._poll is not None:
				if self._poll.poll(0):
					self._alive = False
					self._close()
			elif self._startTime is not None:
				self._alive = _readStartTime(self.processID) == self._startTime
			return self._alive

	def close(self) -> None:
		"""Release the pidfd. Afterwards the watch keeps the last answer it gave."""
		with self._lock:
			self._close()

	def _close(self) -> None:
		if self._poll is not None:
			self._poll = None
			self._release()
