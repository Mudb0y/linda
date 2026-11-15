"""One import-surface check, run in a fresh interpreter; test_importSurface.py starts it.

usage: importSurfaceProbe.py RESULT_JSON core|sweep|hazard NAME|refuse

It writes its findings to RESULT_JSON and exits 0 whatever they are; deciding pass or fail is the
test's job.
"""

import ast
import builtins
import importlib
import importlib.util
import io
import json
import os
import shutil
import signal
import sys
import tempfile
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SOURCE = os.path.join(ROOT, "source")
_protected = (os.path.join(SOURCE, ""), os.path.join(ROOT, "tests", "unit", ""))
writeViolations: list[str] = []
configDir: str | None = None
#: The standard library changes behaviour when any of these imports.
neverImportable = frozenset({"winreg", "msvcrt", "_winapi"})

#: Modules the sweep never imports, because importing them does something other than define them.
notImported = {
	"setup": "py2exe build script: importing it starts a build",
}

#: Audit events that write to a path, each with the argument positions of every path written and of
#: the directory descriptor that path is relative to, where the event carries one. The "open" event
#: carries none, so a write opened relative to a directory descriptor is missed here; the test's
#: snapshot of the tree catches it, and writes made by child processes.
_writeEvents: dict[str, tuple[tuple[int, int | None], ...]] = {
	"os.mkdir": ((0, 2),),
	"os.remove": ((0, 1),),
	"os.rmdir": ((0, 1),),
	"os.truncate": ((0, None),),
	"os.chmod": ((0, 2),),
	"os.chown": ((0, 3),),
	"os.utime": ((0, 3),),
	"os.rename": ((0, 2), (1, 3)),
	"os.link": ((1, 3),),
	"os.symlink": ((1, 2),),
	"shutil.rmtree": ((0, 1),),
	"shutil.move": ((0, None), (1, None)),
	"shutil.copyfile": ((1, None),),
}


def _isProtected(path: object, dirFd: object = None) -> bool:
	if isinstance(path, int):
		return False
	try:
		path = os.fsdecode(path)
		if isinstance(dirFd, int) and dirFd >= 0 and not os.path.isabs(path):
			path = os.path.join(os.readlink(f"/proc/self/fd/{dirFd}"), path)
		path = os.path.realpath(path)
	except (TypeError, ValueError, OSError):
		return False
	return path.startswith(_protected)


def _auditHook(event: str, args: tuple) -> None:
	written: tuple[tuple[object, object], ...] = ()
	if event == "open":
		path, mode, flags = args
		writeFlags = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
		if (isinstance(flags, int) and flags & writeFlags) or (
			isinstance(mode, str) and any(c in mode for c in "wax+")
		):
			written = ((path, None),)
	elif event in _writeEvents:
		written = tuple((args[p], None if d is None else args[d]) for p, d in _writeEvents[event])
	for path, dirFd in written:
		if _isProtected(path, dirFd):
			writeViolations.append(f"{event} {path!r} dir_fd={dirFd!r}")
			raise PermissionError(f"nothing may write under source/ or tests/unit/ at run time: {path!r}")


def prepare() -> None:
	"""bootstrap.install() and the preconditions nvda.pyw and core.main establish before imports."""
	global configDir
	sys.dont_write_bytecode = True
	sys.addaudithook(_auditHook)
	sys.path.insert(0, SOURCE)
	# nvda.pyw runs from appDir, and languageHandler finds catalogues relative to it.
	os.chdir(SOURCE)
	from linuxPlatform import bootstrap

	bootstrap.install()
	import gettext

	gettext.NullTranslations().install(names=["pgettext", "npgettext", "ngettext"])
	import globalVars

	globalVars.appDir = SOURCE
	globalVars.appPid = os.getpid()
	globalVars.appArgs.configPath = configDir = tempfile.mkdtemp(prefix="nvda-config-")
	import config
	import winKernel

	# STUB, owned by workstream A2 (fileUtils and winKernel.moveFileEx): config.initialize() writes
	# nvda.ini through winKernel.moveFileEx, which has no Linux body yet. This stands in for that
	# body for the one call, and is removed as soon as the call returns.
	def moveFileEx(lpExistingFileName: str, lpNewFileName: str, dwFlags: int) -> None:
		os.replace(lpExistingFileName, lpNewFileName)

	realMoveFileEx = winKernel.moveFileEx
	winKernel.moveFileEx = moveFileEx
	try:
		config.initialize()
	finally:
		winKernel.moveFileEx = realMoveFileEx


def coreMainImports() -> list[dict]:
	"""Every import statement in core.main(), in source order, with the exceptions core.main
	itself catches around it."""
	with open(os.path.join(SOURCE, "core.py"), encoding="utf-8") as f:
		tree = ast.parse(f.read())
	main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main")
	steps = []

	def visit(node: ast.AST, handled: tuple[str, ...]) -> None:
		for child in ast.iter_child_nodes(node):
			if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
				continue
			if isinstance(child, ast.Import):
				for alias in child.names:
					steps.append(
						{"line": child.lineno, "module": alias.name, "name": None, "handled": handled}
					)
			elif isinstance(child, ast.ImportFrom):
				for alias in child.names:
					steps.append(
						{
							"line": child.lineno,
							"module": child.module,
							"name": alias.name,
							"handled": handled,
						},
					)
			if isinstance(child, ast.Try):
				caught = []
				for handler in child.handlers:
					types = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
					caught.extend(t.id for t in types if isinstance(t, ast.Name))
				for stmt in child.body:
					visit(ast.Module(body=[stmt], type_ignores=[]), handled + tuple(caught))
				for part in (child.handlers, child.orelse, child.finalbody):
					for stmt in part:
						visit(ast.Module(body=[stmt], type_ignores=[]), handled)
			else:
				visit(child, handled)

	visit(main, ())
	return steps


_finderFile = os.path.join(SOURCE, "linuxPlatform", "finder.py")


def _failure(e: BaseException) -> dict:
	frames = traceback.extract_tb(e.__traceback__)
	# Where the failing import is written, not the finder that refused it.
	inSource = [f for f in frames if f.filename.startswith(SOURCE) and f.filename != _finderFile]
	where = inSource[-1] if inSource else (frames[-1] if frames else None)
	return {
		"type": f"{type(e).__module__}.{type(e).__qualname__}",
		"message": str(e)[:500],
		"where": f"{os.path.relpath(where.filename, ROOT)}:{where.lineno}" if where else None,
		"traceback": "".join(traceback.format_exception(e))[-3000:],
	}


def _doImport(module: str, name: str | None) -> None:
	imported = __import__(module, fromlist=[name] if name else [])
	if name and not hasattr(imported, name):
		raise ImportError(f"cannot import name {name!r} from {module!r}")


def runCoreMainImports() -> list[dict]:
	results = []
	for step in [{"line": 0, "module": "core", "name": None, "handled": ()}, *coreMainImports()]:
		label = f"from {step['module']} import {step['name']}" if step["name"] else f"import {step['module']}"
		result = {"step": label, "line": step["line"]}
		try:
			_doImport(step["module"], step["name"])
			result["status"] = "ok"
		except Exception as e:  # noqa: BLE001
			# A catch-all handler would hide any failure, so only a named exception counts.
			handledBy = [
				n
				for n in step["handled"]
				if n not in ("Exception", "BaseException") and isinstance(e, getattr(builtins, n, ()))
			]
			if handledBy:
				result["status"] = "handled"
				result["handledBy"] = handledBy
				result["message"] = str(e)
			else:
				result["status"] = "fail"
				result.update(_failure(e))
		results.append(result)
	return results


def allSourceModules() -> list[str]:
	modules = []
	for dirPath, dirNames, fileNames in os.walk(SOURCE):
		dirNames[:] = sorted(d for d in dirNames if d != "__pycache__")
		rel = os.path.relpath(dirPath, SOURCE)
		parts = [] if rel == "." else rel.split(os.sep)
		# Their files are imported under the names they replace or stand in for.
		if parts[:2] in (["linuxPlatform", "replacements"], ["linuxPlatform", "standIns"]):
			dirNames[:] = []
			continue
		for fileName in sorted(fileNames):
			if not fileName.endswith(".py"):
				continue
			stem = fileName[:-3]
			modules.append(".".join(parts if stem == "__init__" else [*parts, stem]))
	return sorted(m for m in modules if m)


def _importInChild(module: str, timeout: int) -> dict:
	readFd, writeFd = os.pipe()
	pid = os.fork()
	if pid == 0:
		os.close(readFd)
		signal.alarm(timeout)
		sys.stdout = sys.stderr = io.StringIO()
		del writeViolations[:]
		try:
			imported = importlib.import_module(module)
			# The file actually loaded, which for a replaced module is not upstream's.
			result = {"status": "ok", "file": getattr(imported, "__file__", None)}
		except BaseException as e:  # noqa: BLE001
			result = {"status": "fail", **_failure(e)}
		result["writeViolations"] = writeViolations
		result["windowsModulesLoaded"] = sorted(neverImportable & set(sys.modules))
		with os.fdopen(writeFd, "w") as pipe:
			json.dump(result, pipe)
		os._exit(0)
	os.close(writeFd)
	with os.fdopen(readFd) as pipe:
		data = pipe.read()
	_, status = os.waitpid(pid, 0)
	if data:
		return json.loads(data)
	if os.WIFSIGNALED(status) and os.WTERMSIG(status) == signal.SIGALRM:
		return {"status": "fail", "type": "timeout", "message": f"import took more than {timeout} s"}
	return {"status": "fail", "type": "crash", "message": f"child ended with wait status {status}"}


def runSweep() -> dict:
	import threading

	if threading.active_count() != 1:
		raise RuntimeError(f"cannot fork a clean child: {threading.active_count()} threads are running")
	results = {}
	windowsModulesLoaded = {}
	for module in allSourceModules():
		if module in notImported:
			results[module] = {"status": "notImported", "reason": notImported[module]}
			continue
		result = results[module] = _importInChild(module, timeout=60)
		writeViolations.extend(result.pop("writeViolations", ()))
		if loaded := result.pop("windowsModulesLoaded", None):
			windowsModulesLoaded[module] = loaded
	return {"results": results, "windowsModulesLoaded": windowsModulesLoaded}


def _outcome(call: object) -> dict:
	try:
		value = call()
	except BaseException as e:  # noqa: BLE001
		return {
			"raised": type(e).__name__,
			"isNotImplementedError": isinstance(e, NotImplementedError),
			"message": str(e),
		}
	return {"raised": None, "returned": repr(value)}


def runHazard(name: str) -> dict:
	import ctypes

	if name == "windowsModulesAbsent":
		found = {}
		for module in sorted(neverImportable):
			try:
				findable = importlib.util.find_spec(module) is not None
			except ModuleNotFoundError:
				findable = False
			found[module] = {"inSysModules": module in sys.modules, "findable": findable}
		return {"found": found}
	if name == "shadowsRefused":
		# A file named after a module Linux never provides, anywhere on sys.path, must not import,
		# and nor may a file added to a replaced package's search path.
		shadowDir = tempfile.mkdtemp()
		for fileName in ("winreg.py", "msvcrt.py", "injected.py"):
			with open(os.path.join(shadowDir, fileName), "w", encoding="utf-8") as f:
				f.write("")
		sys.path.insert(0, shadowDir)
		import comtypes.gen
		import NVDAHelper

		paths = {"NVDAHelper": list(NVDAHelper.__path__), "comtypes.gen": list(comtypes.gen.__path__)}
		comtypes.gen.__path__.append(shadowDir)
		outcomes = {
			module: _outcome(lambda module=module: importlib.import_module(module))
			for module in ("winreg", "msvcrt", "comtypes.gen.injected", "NVDAHelper.localWin10")
		}
		return {"paths": paths, "outcomes": outcomes}
	if name == "mimetypes":
		import mimetypes

		return {"guess": mimetypes.guess_type("x.html")[0]}
	if name == "subprocess":
		import subprocess

		done = subprocess.run(
			[sys.executable, "-I", "-c", "print('subprocess ran')"],
			capture_output=True,
			text=True,
			check=False,
		)
		return {"returncode": done.returncode, "stdout": done.stdout.strip()}
	if name == "louisLoader":
		import louis

		return {
			"loaderIsCdll": louis._loader is ctypes.cdll,
			"functypeIsCfunctype": louis._functype is ctypes.CFUNCTYPE,
			"libraryType": type(louis.liblouis).__name__,
			"version": louis.version(),
		}
	if name == "standInCalls":
		import winsound

		import comtypes
		import comtypes.client
		import linuxPlatform
		from pycaw.utils import AudioUtilities

		audited: list[str] = []

		def onAudit(event: str, args: tuple) -> None:
			if event == linuxPlatform.noLinuxImplementationEvent:
				audited.append(args[0])

		sys.addaudithook(onAudit)
		calls = {
			"ctypes.windll.kernel32.GetTickCount()": lambda: ctypes.windll.kernel32.GetTickCount(),
			"ctypes.oledll.ole32.CoInitialize(None)": lambda: ctypes.oledll.ole32.CoInitialize(None),
			"ctypes.WinDLL('user32').MessageBeep(0)": lambda: ctypes.WinDLL("user32").MessageBeep(0),
			"WINFUNCTYPE prototype bound to user32.GetForegroundWindow": lambda: ctypes.WINFUNCTYPE(
				ctypes.c_void_p
			)(
				("GetForegroundWindow", ctypes.windll.user32),
			)(),
			"ctypes.GetLastError()": lambda: ctypes.GetLastError(),
			"ctypes.FormatError(5)": lambda: ctypes.FormatError(5),
			"ctypes.WinError()": lambda: ctypes.WinError(),
			"winsound.MessageBeep()": lambda: winsound.MessageBeep(),
			"comtypes.CoCreateInstance(GUID())": lambda: comtypes.CoCreateInstance(comtypes.GUID()),
			"comtypes.client.CreateObject('SAPI.SpVoice')": lambda: comtypes.client.CreateObject(
				"SAPI.SpVoice"
			),
			"comtypes.COMObject()": lambda: comtypes.COMObject(),
			"pycaw AudioUtilities.GetAllSessions()": lambda: AudioUtilities.GetAllSessions(),
		}
		outcomes = {label: _outcome(call) for label, call in calls.items()}
		callback = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_int)(lambda x: x * 2)
		return {
			"outcomes": outcomes,
			"audited": audited,
			"callbackIsCFuncPtr": isinstance(callback, ctypes._CFuncPtr),
			"callbackResult": callback(21),
		}
	if name == "windowsError":
		error = WindowsError(2, "x", None, 5)  # noqa: UP024
		return {
			"osErrorIsWindowsError": isinstance(OSError(), WindowsError),
			"isOSErrorSubclass": issubclass(WindowsError, OSError),
			"isOSError": WindowsError is OSError,
			"winerror": WindowsError(2, "x").winerror,  # noqa: UP024
			"givenWinerror": error.winerror,
			"givenErrno": error.errno,
		}
	if name == "lockScreen":
		# Both run on hot paths: the first for every focus and navigator change, the second on every
		# core pump cycle. A raise from either becomes an ERROR record in the log each time.
		import logging

		import IAccessibleHandler
		from logHandler import log
		from utils.security import objectBelowLockScreenAndWindowsIsLocked

		records: list[str] = []

		class Recorder(logging.Handler):
			def emit(self, record: logging.LogRecord) -> None:
				records.append(record.levelname)

		log.addHandler(Recorder())
		return {
			"objectBelowLockScreen": _outcome(lambda: objectBelowLockScreenAndWindowsIsLocked(object())),
			"IAccessibleHandler.pumpAll": _outcome(IAccessibleHandler.pumpAll),
			"logLevels": records,
		}
	if name == "installTwice":
		from linuxPlatform import bootstrap, finder, windowsCtypes

		before = {
			"windll": id(ctypes.windll),
			"WindowsError": id(builtins.WindowsError),
			"finders": sum(isinstance(f, finder.PlatformFinder) for f in sys.meta_path),
		}
		bootstrap.install()
		bootstrap._installed = False
		bootstrap.install()
		windowsCtypes.install()
		finder.install()
		after = {
			"windll": id(ctypes.windll),
			"WindowsError": id(builtins.WindowsError),
			"finders": sum(isinstance(f, finder.PlatformFinder) for f in sys.meta_path),
		}
		return {"before": before, "after": after}
	raise ValueError(f"unknown hazard {name}")


def _installState() -> dict:
	import ctypes

	return {
		"windllAdded": hasattr(ctypes, "windll"),
		"windowsErrorAdded": hasattr(builtins, "WindowsError"),
		"findersAdded": sum(type(f).__name__ == "PlatformFinder" for f in sys.meta_path),
	}


def runRefuse() -> dict:
	sys.path.insert(0, SOURCE)
	from linuxPlatform import bootstrap

	sys.platform = "win32"
	try:
		bootstrap.install()
	except RuntimeError as e:
		return {"raised": "RuntimeError", "message": str(e), **_installState()}
	return {"raised": None}


def runNoLouis() -> dict:
	sys.path.insert(0, SOURCE)
	sys.modules["louis"] = None
	from linuxPlatform import bootstrap

	try:
		bootstrap.install()
	except RuntimeError as e:
		return {"raised": "RuntimeError", "message": str(e), **_installState()}
	return {"raised": None}


def main() -> None:
	resultPath, mode = sys.argv[1], sys.argv[2]
	result: dict = {"mode": mode}
	try:
		if mode == "refuse":
			result.update(runRefuse())
		elif mode == "noLouis":
			result.update(runNoLouis())
		else:
			prepare()
			result["core"] = runCoreMainImports()
			if mode == "sweep":
				result.update(runSweep())
			elif mode == "hazard":
				result["hazard"] = runHazard(sys.argv[3])
	except BaseException as e:  # noqa: BLE001
		result["error"] = _failure(e)
	result["writeViolations"] = writeViolations
	with open(resultPath, "w", encoding="utf-8") as f:
		json.dump(result, f, indent=1)
	if configDir:
		shutil.rmtree(configDir, ignore_errors=True)
	sys.stdout.flush()
	sys.stderr.flush()
	# Skips interpreter teardown, where NVDA's atexit handlers would run against a half-built core.
	os._exit(0)


if __name__ == "__main__":
	main()
