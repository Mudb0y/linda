"""Runs NVDA code in a fresh interpreter, or starts the launcher, and reports what it did.

NVDA's modules keep their state per process and import in one order only, so a test that needs
them running starts a child for each case. Upstream swallows many exceptions, so a path that merely
returns has proved little; the child therefore records, as it happens, everything that would make a
run unclean, and ChildResult.assertClean holds a run to one definition of clean (see CLEAN_CHECKS).
A test that expects one of those things opts out of that check alone, by name.

The records go to a file a line at a time, so a child that kills itself or is killed still reports
what it did up to then. The child and the launcher run under isolatedCommand, so that a write under
source/ or tests/unit by them or anything they start is seen however it was made.

From a test: harness.runChild(tmp_path, code), or harness.startLauncher(directory, *arguments).
As the child: harness.py child RESULT_JSON EVENTS PREPARE CODE_FILE TIMEOUT RECORD_LEVEL.
As the launcher: harness.py launch EVENTS RECORD_LEVEL LAUNCHER [ARGUMENT...].
Around either: harness.py isolate OVERLAY_DIRECTORY COMMAND [ARGUMENT...].

Only the standard library is imported here, so that a runner with reason to work otherwise, such as
one started with -I or one running tests in its own process, can load this file by its path and share
its checks, without putting tests/linux, whose synthDrivers would shadow NVDA's, on sys.path.
"""

import faulthandler
import importlib.util
import json
import logging
import os
import re
import signal
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import traceback
from collections.abc import Callable, Collection, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
SOURCE = ROOT / "source"
LAUNCHER = SOURCE / "linda.py"
#: Nothing that runs NVDA code may write here.
PROTECTED = (SOURCE, ROOT / "tests" / "unit")
#: Directories of the checkout that git and the development tools write to while other work goes on
#: in the same checkout; treeState does not walk them.
UNWATCHED = frozenset(str(ROOT / name) for name in (".git", ".claude", ".ruff_cache", "engine/target"))

#: How much of NVDA the child sets up before the code runs, each level adding to the one before.
#: "none": source/ on sys.path and nothing else.
#: "bootstrap": linuxPlatform.bootstrap.install().
#: "appDir": what nvda.pyw and linda.py establish before importing config: the gettext
#: builtins, globalVars.appDir and appPid, and appDir as the working directory.
#: "config": a configuration directory of the child's own and config.initialize().
PREPARE_LEVELS = ("none", "bootstrap", "appDir", "config")

#: What a clean run is: each check by the name a test gives to ChildResult.assertClean to opt out of
#: it alone.
CLEAN_CHECKS = {
	"error": "the code returned, and the child reported",
	"noLinuxImplementation": "nothing raised NoLinuxImplementation, even where upstream caught it, and "
	"no gap was noted outside NOTED_GAPS",
	"logErrors": "nothing was logged at ERROR or above, on any logger",
	"threadExceptions": "no thread died of an exception",
	"unraisable": "no finaliser or other unraisable call failed",
	"returnCode": "the process exited with status 0",
	"writes": "nothing was written under source/ or tests/unit, by the run or any process it started, "
	"and the run's Python code did not try to",
	"audio": "no audio library was loaded and no sound device opened",
	"stderr": "standard error holds nothing outside ALLOWED_STANDARD_ERROR",
	"freeze": "the watchdog never found the core frozen",
}

#: What the watchdog logs, at INFO, once the core has not reported alive for as long as it takes to
#: count as frozen. An ERROR follows only after a further 15 s, and a freeze that ends before then,
#: such as a 17 s walk of a long list's siblings, is otherwise logged at INFO alone.
FREEZE_MESSAGE = "Starting freeze recovery after "

#: Gaps that Linux bodies note through linuxPlatform.noteNoLinuxImplementation rather than raise, which
#: a clean run may record, each with where and why.
NOTED_GAPS = {
	"hwPortUtils.listComPorts": "Linux has no SetupAPI and hwIo opens no serial, USB or HID device, so "
	"there is nothing to list; braille display detection asks on every scan.",
	"hwPortUtils.listUsbDevices": "As for hwPortUtils.listComPorts.",
	"hwPortUtils.listHidDevices": "As for hwPortUtils.listComPorts.",
}

#: What standard error, or the report of an unraisable exception, may hold after a clean run, each
#: with the reason it is allowed.
ALLOWED_STANDARD_ERROR = (
	# NVDAHighlighter keeps its settings in a class attribute and globalVars.settingsRing keeps the
	# last synth, both for the life of the process, so AutoSettings.__del__ runs only as the
	# interpreter takes config apart, and fails. Windows does the same, with no standard error to show
	# it.
	re.compile(
		r"Exception ignored in: <function AutoSettings\.__del__ at 0x[0-9a-f]+>\n"
		r"Traceback \(most recent call last\):\n"
		r"(?:  File \"[^\"\n]*/autoSettingsUtils/autoSettings\.py\", line \d+, in \w+\n(?:    .*\n)*)+"
		r"AttributeError: 'NoneType' object has no attribute 'unregister'\n",
	),
	# The development shell gives GTK no SVG image loader, so the theme's check marks and other
	# symbolic icons are not drawn. Nothing NVDA says or does depends on them.
	re.compile(
		r"\n?\([^()\n:]+:\d+\): Gtk-WARNING \*\*: [\d:.]+: Could not load a pixbuf from "
		r"/org/gtk/libgtk/theme/Adwaita/assets/[\w-]+\.svg\.\n"
		r"This may indicate that pixbuf loaders or the mime database could not be found\.\n",
	),
	# wxGTK pops a menu up at the pointer with no event of GTK's to place it by, as the NVDA menu
	# opens from a key NVDA captured rather than from GTK; GTK places it at the pointer all the same.
	re.compile(r"\n?\([^()\n:]+:\d+\): Gtk-WARNING \*\*: [\d:.]+: no trigger event for menu popup\n"),
	# The braille viewer's text controls are laid out once before its window has a width, now and
	# then, and GTK complains of the room they then have. They get their size a moment later.
	re.compile(
		r"\n?\([^()\n:]+:\d+\): Gtk-WARNING \*\*: [\d:.]+: Negative content (?:width|height) -?\d+ "
		r"\(allocation \d+, extents \d+x\d+\) while allocating gadget \(node entry, owner GtkEntry\)\n",
	),
	# libatspi, which NVDA's objects are read through, warns when a program in a container, Steam's or
	# Flatpak's, offers a server of its own in a runtime directory that is not there outside, and then
	# reads that program over the bus, as the engine does (atspi-misc.c in at-spi2-core).
	re.compile(
		r"\n?\([^()\n:]+:\d+\): atspi-WARNING \*\*: [\d:.]+: AT-SPI: Unable to open bus connection for "
		r"application with pid \d+: Failed to connect to socket [^\n]*/at-spi2-\w+/socket: No such file or "
		r"directory\n",
	),
)

#: Libraries that can reach a sound server or a sound card, by the start of their file name with any
#: "lib" taken off. speech-dispatcher's client reaches one through speech-dispatcher, and mpg123's
#: out123 through the output module it loads when it opens one.
AUDIO_LIBRARIES = (
	"asound",
	"pulse",
	"pipewire",
	"jack",
	"portaudio",
	"pcaudio",
	"openal",
	"sdl",
	"canberra",
	"ao.",
	"sndio",
	"espeak",
	"gst",
	"speechd",
	"out123",
)
#: Audio libraries that a library linking them maps without anything loading them for sound, each by
#: its exact file name, keyed by the start of the linking library's: gui.message imports wx.html2,
#: whose extension links WebKitGTK, which links GStreamer's core libraries and libpulse. They are
#: allowed only while that library is mapped; GStreamer's plugins, its sinks among them, and anything
#: loaded through ctypes are still counted.
LINKED_WITHOUT_SOUND = {
	"libwebkit2gtk-4.1.so": re.compile(
		r"lib(?:gst(?:reamer|allocators|app|audio|base|fft|gl|pbutils|tag|video)-1\.0"
		r"|pulse|pulse-simple|pulsecommon-[\d.]+)\.so(?:\.[\d.]+)?",
	),
}
#: Names the eSpeak NG library that NVDA's default synthesizer loads. That build has no audio output
#: and links no sound library, which tests/linux/test_devShell.py checks, so that file alone of the
#: libraries AUDIO_LIBRARIES matches is never counted; any other eSpeak, such as a stock one that links
#: pcaudiolib, still is.
ESPEAK_LIBRARY_VARIABLE = "LINDA_ESPEAK_LIB"
SOUND_DEVICES = "/dev/snd"
#: Words in the path of a Unix socket that belongs to a sound server.
SOUND_SERVER_SOCKETS = ("pulse", "pipewire")

#: Audit events by which a process reaches past itself, recorded for ChildResult.reached; a test about
#: what NVDA touches at rest reads them, as no one rule fits every test.
REACH_EVENTS = frozenset({"socket.connect", "ctypes.dlopen", "subprocess.Popen", "os.system"})
#: Opening a file under these reaches a device, the kernel or another program.
SYSTEM_FILES = ("/dev/", "/sys/", "/run/")

NO_LINUX_IMPLEMENTATION_EVENT = "linuxPlatform.noLinuxImplementation"
LOG_ERROR = 40

_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
#: Audit events that change the file system: for each path an event changes, the position of the
#: path, of the directory descriptor it is relative to or None, and whether the event acts on what a
#: symbolic link there points to rather than on the link.
WRITE_EVENTS: dict[str, tuple[tuple[int, int | None, bool], ...]] = {
	"os.mkdir": ((0, 2, False),),
	"os.mkfifo": ((0, 2, False),),
	"os.mknod": ((0, 3, False),),
	"os.remove": ((0, 1, False),),
	"os.rmdir": ((0, 1, False),),
	"os.rename": ((0, 2, False), (1, 3, False)),
	# A hard link to a file is a way to write the file.
	"os.link": ((0, 2, True), (1, 3, False)),
	"os.symlink": ((1, 2, False),),
	"os.truncate": ((0, None, True),),
	"os.chmod": ((0, 2, True),),
	"os.chown": ((0, 3, True),),
	"os.utime": ((0, 3, True),),
	"os.setxattr": ((0, None, True),),
	"os.removexattr": ((0, None, True),),
	"shutil.rmtree": ((0, 1, False),),
	"shutil.move": ((0, None, False), (1, None, False)),
	"shutil.copyfile": ((1, None, True),),
	"shutil.unpack_archive": ((1, None, True),),
}
#: Events that act on a path that must exist already; one aimed at nothing changes nothing.
_ON_EXISTING = frozenset(
	{"os.remove", "os.rmdir", "os.rename", "os.link", "os.truncate", "os.chmod", "os.chown", "os.utime"},
)


#: The three signs whyNotInRig reads: a variable the rig sets, the name of process 1, and a
#: directory that exists outside the sandbox only.
RIG_VARIABLE = "LINDA_RIG"
FIRST_PROCESS_NAME = Path("/proc/1/comm")
LIVE_RUNTIME_DIRECTORY = Path("/run/user")


def whyNotInRig() -> str | None:
	"""What shows that this process is outside tests/linux/rig, or None when it is inside.

	The tests start screen readers, open windows and signal whatever holds the display's lock. In
	the rig's sandbox none of that can reach the desktop the machine's owner is using; outside it,
	all of it would.
	"""
	if os.environ.get(RIG_VARIABLE) != "1":
		return f"{RIG_VARIABLE} is not set"
	# The variable alone could have been copied into another shell. These hold only in the sandbox.
	try:
		firstProcess = FIRST_PROCESS_NAME.read_text(encoding="ascii").strip()
	except OSError:
		firstProcess = ""
	if firstProcess != "bwrap":
		return "process 1 is not the sandbox's"
	if LIVE_RUNTIME_DIRECTORY.exists():
		return "the live session's runtime directory is visible"
	return None


def requireRig() -> None:
	why = whyNotInRig()
	if why is not None:
		sys.exit(
			f"refusing to run outside the rig ({why}). Use: nix develop . -c tests/linux/rig COMMAND",
		)


def treeState(*tops: Path | str) -> dict[str, tuple]:
	"""A snapshot of the trees under tops, PROTECTED by default, to compare with changedPaths.

	A directory's modification time changes whenever an entry is made or removed in it, so this
	also shows a file that was written and deleted again. The change time is kept as well, because
	nothing can set it back, as os.utime sets back the modification time, and the inode number, which
	shows a file replaced by another.
	"""
	state: dict[str, tuple] = {}
	for top in tops or PROTECTED:
		for dirPath, dirNames, fileNames in os.walk(top):
			dirNames[:] = [d for d in dirNames if os.path.join(dirPath, d) not in UNWATCHED]
			info = os.lstat(dirPath)
			state[dirPath] = ("directory", info.st_ino, info.st_mtime_ns, info.st_ctime_ns)
			# os.walk lists a symbolic link to a directory among the directories, and does not enter it.
			for name in fileNames + [d for d in dirNames if os.path.islink(os.path.join(dirPath, d))]:
				path = os.path.join(dirPath, name)
				info = os.lstat(path)
				state[path] = (info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
	return state


def changedPaths(before: dict[str, tuple], after: dict[str, tuple]) -> list[str]:
	return sorted(path for path in before.keys() | after.keys() if before.get(path) != after.get(path))


_MS_REC = 0x4000
_MS_PRIVATE = 1 << 18


def _overlayUppers(overlay: Path) -> list[tuple[Path, Path]]:
	"""Each of PROTECTED with the upper directory of its overlay under overlay."""
	return [(top, overlay / str(index) / "upper") for index, top in enumerate(PROTECTED)]


def isolatedCommand(overlay: Path, command: list[str]) -> list[str]:
	"""command, run in a mount namespace of its own in which each of PROTECTED is a writable overlay.

	The rig's checkout is read-only, so a write there is refused, and nothing but the audit hooks of
	a Python process would know it was tried. Under the overlay, a write by the command or by any
	process it starts, by whatever means, lands in an upper directory under overlay, an empty
	directory of the caller's, where overlayWrites finds it; the checkout stays as it was.
	"""
	return [sys.executable, "-I", "-B", str(Path(__file__).resolve()), "isolate", str(overlay), *command]


def overlayWrites(overlay: Path) -> list[str]:
	"""The paths under PROTECTED that a command run by isolatedCommand changed, so far."""
	changed = []
	for top, upper in _overlayUppers(overlay):
		if not upper.exists():
			continue
		entries = sorted(path for path in treeState(upper) if path != str(upper))
		# A directory is in the upper tree because something in it changed, which is listed itself;
		# it is listed alone when that was an entry made and removed again.
		leaves = [path for path in entries if not any(other.startswith(path + os.sep) for other in entries)]
		found = [os.path.join(top, os.path.relpath(path, upper)) for path in leaves]
		if not found and upper.stat().st_mtime_ns != 0:
			found = [str(top)]
		changed += found
	return changed


def _isolate(overlay: str, *command: str) -> None:
	"""Mount the overlays isolatedCommand describes, then become command."""
	requireRig()
	import ctypes

	libc = ctypes.CDLL(None, use_errno=True)

	def mount(source: bytes | None, target: bytes, fsType: bytes | None, flags: int, data: bytes | None):
		if libc.mount(source, target, fsType, flags, data) != 0:
			number = ctypes.get_errno()
			raise OSError(number, f"{os.strerror(number)}: mount on {os.fsdecode(target)}")

	try:
		uid, gid = os.getuid(), os.getgid()
		os.unshare(os.CLONE_NEWUSER | os.CLONE_NEWNS)
		for name, text in (
			("setgroups", "deny"),
			("uid_map", f"{uid} {uid} 1"),
			("gid_map", f"{gid} {gid} 1"),
		):
			with open(f"/proc/self/{name}", "w", encoding="ascii") as f:
				f.write(text)
		# So that nothing mounted here shows anywhere else.
		mount(None, b"/", None, _MS_REC | _MS_PRIVATE, None)
		for top, upper in _overlayUppers(Path(overlay)):
			work = upper.parent / "work"
			upper.mkdir(parents=True)
			work.mkdir()
			# overlayfs reads a backslash in an option as escaping the character after it, which lets a
			# path hold the commas and colons that otherwise separate options and layers.
			lower, upperDir, workDir = (
				re.sub(r"([\\,:])", r"\\\1", str(path)) for path in (top, upper, work)
			)
			options = f"lowerdir={lower},upperdir={upperDir},workdir={workDir},userxattr"
			mount(b"overlay", os.fsencode(top), b"overlay", 0, os.fsencode(options))
			# Making and removing an entry leaves only the directory's time changed.
			os.utime(upper, ns=(0, 0))
	except OSError as e:
		sys.exit(f"harness: cannot put an overlay over the checkout: {e}")
	# Each overlay is a file system of its own, where git would stop looking for the repository that
	# a path under it belongs to, as upstream's markdownTranslate asks from source/.
	os.environ["GIT_DISCOVERY_ACROSS_FILESYSTEM"] = "1"
	# Becoming a new program also drops the capabilities the new user namespace gave this process.
	os.execv(command[0], command)


def _absolutePath(path: object, dirFd: object, followsLink: bool) -> str | None:
	if isinstance(path, int):
		return None
	path = os.fsdecode(path)
	if not os.path.isabs(path):
		# The events give -1 for "no directory descriptor".
		useFd = isinstance(dirFd, int) and dirFd >= 0
		path = os.path.join(os.readlink(f"/proc/self/fd/{dirFd}") if useFd else os.getcwd(), path)
	parent, name = os.path.split(path.rstrip(os.sep) or os.sep)
	if followsLink or name in ("", ".", ".."):
		return os.path.realpath(path)
	# The entry itself is what changes, wherever its directory is reached through.
	return os.path.join(os.path.realpath(parent), name)


def writtenPaths(event: str, args: tuple) -> list[str]:
	"""The paths an audit event is about to change, absolute and with symbolic links resolved."""
	try:
		if event == "open":
			path, mode, flags = args
			if isinstance(mode, str):
				writes = any(letter in mode for letter in "wax+")
			else:
				writes = isinstance(flags, int) and bool(flags & _WRITE_FLAGS)
			paths = [_absolutePath(path, None, True)] if writes else []
		else:
			changed = WRITE_EVENTS.get(event, ())
			if event in _ON_EXISTING:
				pathAt, fdAt, _followsLink = changed[0]
				first = _absolutePath(args[pathAt], None if fdAt is None else args[fdAt], False)
				if first is None or not os.path.lexists(first):
					return []
			paths = [
				_absolutePath(args[pathAt], None if fdAt is None else args[fdAt], follows)
				for pathAt, fdAt, follows in changed
			]
	except (TypeError, ValueError, IndexError, OSError):
		return []
	return [path for path in paths if path is not None]


def isProtected(path: str, tops: Collection[Path] = PROTECTED) -> bool:
	return any(path == str(top) or path.startswith(str(top) + os.sep) for top in tops)


def isAudioLibrary(path: str | bytes) -> bool:
	name = os.path.basename(os.fsdecode(path)).lower()
	return name.removeprefix("lib").startswith(AUDIO_LIBRARIES)


def isNamedEspeakLibrary(path: str | bytes) -> bool:
	"""Whether path is, through any links, the file ESPEAK_LIBRARY_VARIABLE names."""
	named = os.environ.get(ESPEAK_LIBRARY_VARIABLE)
	return bool(named) and os.path.realpath(os.fsdecode(path)) == os.path.realpath(named)


def soundReached(*, allowLinked: bool = True) -> list[str]:
	"""Audio libraries mapped into this process, but for the eSpeak NG library ESPEAK_LIBRARY_VARIABLE
	names, and sound devices it holds open, now.

	:param allowLinked: Leave out what LINKED_WITHOUT_SOUND allows. Code that compares what is mapped
		before and after it plays passes False, so that one of those libraries loaded by playing shows.
	"""
	found = []
	with open("/proc/self/maps", encoding="utf-8") as maps:
		mapped = {fields[5].strip() for line in maps if len(fields := line.split(None, 5)) == 6}
	names = {os.path.basename(path) for path in mapped if path.startswith("/")}
	linked = [
		allowed
		for linker, allowed in LINKED_WITHOUT_SOUND.items()
		if allowLinked and any(name.startswith(linker) for name in names)
	]
	for path in sorted(mapped):
		name = os.path.basename(path)
		if (
			path.startswith("/")
			and isAudioLibrary(name)
			and not any(allowed.fullmatch(name) for allowed in linked)
			and not isNamedEspeakLibrary(path)
		):
			found.append(f"mapped {path}")
	for fd in os.listdir("/proc/self/fd"):
		try:
			target = os.readlink(f"/proc/self/fd/{fd}")
		except OSError:
			continue
		if target.startswith(SOUND_DEVICES):
			found.append(f"holds {target} open")
	return found


#: Called by the checks with the kind of record and its fields.
Recorder = Callable[..., None]


class EventFile:
	"""A Recorder that writes each record to a file as one JSON line, as it happens."""

	def __init__(self, path: Path | str) -> None:
		# Line buffered, so that each record is on disk however the process ends.
		self._file = open(path, "a", encoding="utf-8", buffering=1)  # noqa: SIM115
		self._lock = threading.RLock()

	def __call__(self, kind: str, **fields: object) -> None:
		line = json.dumps({"kind": kind, "pid": os.getpid(), **fields}, default=repr)
		try:
			with self._lock:
				self._file.write(line + "\n")
		except (OSError, ValueError):
			# Closed as the interpreter ends; the standard error check still sees it.
			print(f"harness: {line}", file=sys.__stderr__, flush=True)


class Checks:
	"""Everything that makes a run unclean, recorded as it happens.

	The thread and unraisable hooks stand in for the interpreter's own, which would only print. Once
	logHandler.initialize replaces them with NVDA's, a failure is logged as an ERROR record instead,
	which the log check counts, so nothing is counted twice; and where some other hook was already in
	place, that hook is called in their stead.
	"""

	def __init__(self, record: Recorder, *, recordLevel: int = LOG_ERROR) -> None:
		self.record = record
		self._previousThreadHook = threading.excepthook
		self._previousUnraisableHook = sys.unraisablehook

		checks = self

		class Collector(logging.Handler):
			def emit(self, record: logging.LogRecord) -> None:
				checks._onLogRecord(record)

		self._collector = Collector(level=recordLevel)

		class FreezeCollector(logging.Handler):
			def emit(self, record: logging.LogRecord) -> None:
				if isinstance(record.msg, str) and record.msg.startswith(FREEZE_MESSAGE):
					checks.record("freeze", message=record.getMessage())

		self._freezeCollector = FreezeCollector(level=logging.INFO)
		sys.addaudithook(self._onAudit)
		threading.excepthook = self._onThreadException
		sys.unraisablehook = self._onUnraisable
		# On the root logger, which every logger's records reach, NVDA's among them.
		logging.getLogger().addHandler(self._collector)
		logging.getLogger().addHandler(self._freezeCollector)

	def _onAudit(self, event: str, args: tuple) -> None:
		# ctypes opens the program itself, with no name, as it imports.
		if event in REACH_EVENTS and not (event == "ctypes.dlopen" and args[0] is None):
			what = args[0] if event == "ctypes.dlopen" else args[1] if event == "socket.connect" else args
			self.record(
				"reach", event=event, what=os.fsdecode(what) if isinstance(what, bytes) else str(what)
			)
		elif event == "open" and isinstance(args[0], str) and args[0].startswith(SYSTEM_FILES):
			self.record("reach", event=event, what=args[0])
		if event == NO_LINUX_IMPLEMENTATION_EVENT:
			self.record(
				"noLinuxImplementation",
				what=str(args[0]),
				thread=threading.current_thread().name,
				# The frames that raised it, without this hook's and NoLinuxImplementation's own.
				stack=traceback.format_stack(limit=14)[:-2],
			)
		elif event == "ctypes.dlopen":
			if (
				isinstance(args[0], (str, bytes))
				and isAudioLibrary(args[0])
				and not isNamedEspeakLibrary(args[0])
			):
				self.record("audio", what=f"loaded {os.fsdecode(args[0])}")
		elif event == "socket.connect":
			address = args[1]
			if isinstance(address, (str, bytes)) and any(
				word in os.fsdecode(address) for word in SOUND_SERVER_SOCKETS
			):
				self.record("audio", what=f"connected to {os.fsdecode(address)}")
		elif event == "open" or event in WRITE_EVENTS:
			if event == "open" and isinstance(args[0], str) and args[0].startswith(SOUND_DEVICES):
				self.record("audio", what=f"opened {args[0]}")
			for path in writtenPaths(event, args):
				if isProtected(path):
					self.record("write", event=event, path=path, stack=traceback.format_stack(limit=8)[:-1])
					raise PermissionError(
						f"nothing may write under source/ or tests/unit at run time: {path}"
					)

	def _onLogRecord(self, record: logging.LogRecord) -> None:
		try:
			message = record.getMessage()
		except Exception as e:  # noqa: BLE001
			# A record whose own formatting fails; logging reports that through its handlers too.
			message = f"{record.msg!r} (unformattable: {e!r})"
		exception = None
		if record.exc_info and record.exc_info[1] is not None:
			exception = "".join(traceback.format_exception(*record.exc_info))
		self.record(
			"log",
			levelno=record.levelno,
			level=record.levelname,
			logger=record.name,
			# Where NVDA's own log says the record came from; liblouis's messages, for one, are known by it.
			codepath=getattr(record, "codepath", None),
			message=message,
			exception=exception,
		)

	def _onThreadException(self, args: object) -> None:
		if self._previousThreadHook is not threading.__excepthook__:
			self._previousThreadHook(args)
			return
		if args.exc_type is SystemExit:
			# Python ignores SystemExit in a thread, and so does NVDA's hook.
			return
		self.record(
			"threadException",
			thread=getattr(args.thread, "name", None),
			traceback="".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)),
		)

	def _onUnraisable(self, unraisable: object) -> None:
		if self._previousUnraisableHook is not sys.__unraisablehook__:
			self._previousUnraisableHook(unraisable)
			return
		where = unraisable.err_msg or "Exception ignored in"
		self.record(
			"unraisable",
			message=f"{where}: {unraisable.object!r}",
			traceback="".join(
				traceback.format_exception(
					unraisable.exc_type, unraisable.exc_value, unraisable.exc_traceback
				),
			),
		)

	def finish(self) -> None:
		"""Record what can be seen only at the end."""
		for what in soundReached():
			self.record("audio", what=what)


def prepareChild(level: str, workDir: Path) -> None:
	"""Establish in this process what the level in PREPARE_LEVELS names."""
	assert level in PREPARE_LEVELS, level
	sys.dont_write_bytecode = True
	sys.path.insert(0, str(SOURCE))
	if level == "none":
		return
	from linuxPlatform import bootstrap

	bootstrap.install()
	if level == "bootstrap":
		return
	import gettext

	gettext.NullTranslations().install(names=["pgettext", "npgettext", "ngettext"])
	import globalVars

	# As nvda.pyw does: upstream finds its locale directory, among others, relative to it.
	os.chdir(SOURCE)
	globalVars.appDir = str(SOURCE)
	globalVars.appPid = os.getpid()
	if level == "appDir":
		return
	configDir = workDir / "config"
	configDir.mkdir()
	globalVars.appArgs.configPath = str(configDir)
	import config

	config.initialize()


def unexpectedStandardError(text: str) -> str:
	"""What is left of standard error once the allowed lines are taken out."""
	for pattern in ALLOWED_STANDARD_ERROR:
		text = pattern.sub("", text)
	return text


@dataclass
class ChildResult:
	returnCode: int | None
	value: object = None
	#: Why the code did not return: its traceback, or how the child ended without reporting.
	error: str | None = None
	noLinuxImplementation: list[str] = field(default_factory=list)
	#: "LEVEL: message" for each record at ERROR or above.
	logErrors: list[str] = field(default_factory=list)
	#: "LEVEL: message" for each record at or above the level runChild was given.
	logRecords: list[str] = field(default_factory=list)
	threadExceptions: list[str] = field(default_factory=list)
	unraisable: list[str] = field(default_factory=list)
	writes: list[str] = field(default_factory=list)
	audio: list[str] = field(default_factory=list)
	#: What the watchdog logged as each freeze began.
	freezes: list[str] = field(default_factory=list)
	#: "EVENT WHAT" for each of REACH_EVENTS and each open under SYSTEM_FILES; no check reads it.
	reached: list[str] = field(default_factory=list)
	stdout: str = ""
	stderr: str = ""
	#: Every record the child made, as written.
	events: list[dict] = field(default_factory=list)

	@classmethod
	def fromEvents(cls, events: list[dict], overlay: Path | None = None, **fields: object) -> "ChildResult":
		""":param overlay: The directory given to isolatedCommand, whose changes count as writes."""
		if overlay is not None:
			events = events + [
				{"kind": "write", "event": "changed", "path": path} for path in overlayWrites(overlay)
			]
		result = cls(events=events, **fields)
		for event in events:
			kind = event["kind"]
			if kind == "noLinuxImplementation":
				result.noLinuxImplementation.append(event["what"])
			elif kind == "log":
				text = f"{event['level']}: {event['message']}"
				result.logRecords.append(text)
				if event["levelno"] >= LOG_ERROR:
					result.logErrors.append(text)
			elif kind == "threadException":
				result.threadExceptions.append(f"in thread {event['thread']}:\n{event['traceback']}")
			elif kind == "unraisable":
				result.unraisable.append(f"{event['message']}\n{event['traceback']}")
			elif kind == "write":
				result.writes.append(f"{event['event']} {event['path']}")
			elif kind == "audio":
				result.audio.append(event["what"])
			elif kind == "freeze":
				result.freezes.append(event["message"])
			elif kind == "reach":
				result.reached.append(f"{event['event']} {event['what']}")
		return result

	def _detail(self, kind: str) -> str:
		parts = []
		for event in self.events:
			if event["kind"] != kind:
				continue
			if kind == "noLinuxImplementation" and event["what"] not in NOTED_GAPS:
				parts.append(f"{event['what']}, from:\n{''.join(event['stack'])}")
			elif kind == "log" and event["levelno"] >= LOG_ERROR:
				parts.append(
					f"{event['level']} on {event['logger']}: {event['message']}\n{event['exception'] or ''}"
				)
			elif kind == "write" and "stack" in event:
				parts.append(f"{event['event']} {event['path']}, from:\n{''.join(event['stack'])}")
			elif kind == "write":
				parts.append(f"{event['event']} {event['path']}, by this process or one it started")
		return "\n".join(parts)

	def problems(self, ignoring: Collection[str] = ()) -> list[str]:
		"""What keeps this run from being clean, leaving out the checks named in ignoring."""
		unknown = set(ignoring) - CLEAN_CHECKS.keys()
		assert not unknown, f"no such check: {sorted(unknown)}; the checks are {list(CLEAN_CHECKS)}"
		found = {
			"error": self.error,
			"noLinuxImplementation": self._detail("noLinuxImplementation"),
			"logErrors": self.logErrors and self._detail("log"),
			"threadExceptions": "\n".join(self.threadExceptions),
			"unraisable": "\n".join(u for u in self.unraisable if unexpectedStandardError(u)),
			"returnCode": self.returnCode != 0 and describeExit(self.returnCode),
			"writes": self.writes and self._detail("write"),
			"audio": "\n".join(self.audio),
			"stderr": unexpectedStandardError(self.stderr)[-4000:],
			"freeze": "\n".join(self.freezes),
		}
		return [
			f"not clean: {CLEAN_CHECKS[name]} does not hold:\n{detail}"
			for name, detail in found.items()
			if detail and name not in ignoring
		]

	def assertClean(self, *, ignoring: Collection[str] = ()) -> None:
		"""Every check in CLEAN_CHECKS holds, apart from those named in ignoring, which a test names
		because it expects what that check would find."""
		problems = self.problems(ignoring)
		assert not problems, "\n\n".join(problems)


def readEvents(path: Path) -> list[dict]:
	if not path.exists():
		return []
	events = []
	for line in path.read_text(encoding="utf-8").splitlines():
		try:
			events.append(json.loads(line))
		except json.JSONDecodeError:
			# The last line of a process killed while writing it.
			events.append({"kind": "unreadable", "line": line})
	return events


def describeExit(returnCode: int | None) -> str:
	if returnCode is None:
		return "still running"
	if returnCode < 0:
		return f"killed by {signal.Signals(-returnCode).name}"
	return f"exit status {returnCode}"


def _environment(env: dict[str, str | None] | None) -> dict[str, str]:
	merged = {**os.environ, **(env or {})}
	return {name: value for name, value in merged.items() if value is not None}


def runChild(
	tmpPath: Path,
	code: str,
	*,
	prepare: str = "config",
	env: dict[str, str | None] | None = None,
	timeout: float = 300,
	recordLevel: int = LOG_ERROR,
) -> ChildResult:
	"""Run code in a fresh interpreter, after the preparation PREPARE_LEVELS names, and report.

	The code runs as a module body. Whatever it leaves in the name ``result`` comes back as
	ChildResult.value and has to survive JSON. The name ``harness`` holds this module, loaded in the
	child, for what the code can only ask from inside, such as soundReached.

	:param env: Variables to set for the child, over this process's; None unsets one.
	:param timeout: Seconds after which the child prints every thread's stack and ends.
	:param recordLevel: The level from which records are kept in ChildResult.logRecords. A record
		below its logger's level is never made, so a test that wants those sets the level itself.
	"""
	requireRig()
	assert prepare in PREPARE_LEVELS, prepare
	work = Path(tempfile.mkdtemp(prefix="child-", dir=tmpPath))
	codePath = work / "code.py"
	codePath.write_text(textwrap.dedent(code), encoding="utf-8")
	resultPath = work / "result.json"
	eventsPath = work / "events.jsonl"
	overlay = work / "overlay"
	before = treeState()
	command = isolatedCommand(
		overlay,
		[
			sys.executable,
			"-I",
			"-B",
			str(Path(__file__).resolve()),
			"child",
			str(resultPath),
			str(eventsPath),
			prepare,
			str(codePath),
			str(timeout),
			str(recordLevel),
		],
	)
	with subprocess.Popen(
		command,
		stdout=subprocess.PIPE,
		stderr=subprocess.PIPE,
		text=True,
		env=_environment(env),
		cwd=work,
	) as process:
		try:
			# The child ends itself at the timeout, with its stacks; this is for one that cannot.
			stdout, stderr = process.communicate(timeout=timeout + 30)
		except subprocess.TimeoutExpired:
			process.kill()
			stdout, stderr = process.communicate()
	changed = changedPaths(before, treeState())
	assert not changed, f"the child changed the repository: {changed}"
	reported = {"value": None, "error": None}
	if resultPath.exists():
		reported = json.loads(resultPath.read_text(encoding="utf-8"))
	else:
		reported["error"] = f"the child ended without reporting a result: {describeExit(process.returncode)}"
	return ChildResult.fromEvents(
		readEvents(eventsPath),
		overlay,
		returnCode=process.returncode,
		stdout=stdout,
		stderr=stderr,
		**reported,
	)


def _child(
	resultPath: str, eventsPath: str, prepare: str, codePath: str, timeout: str, recordLevel: str
) -> None:
	requireRig()
	checks = Checks(EventFile(eventsPath), recordLevel=int(recordLevel))
	# For a hang, every thread's stack, then exit status 1.
	faulthandler.dump_traceback_later(float(timeout), exit=True)
	outcome: dict = {"value": None, "error": None}
	try:
		prepareChild(prepare, Path(resultPath).parent)
		namespace: dict = {"__name__": "__harness__", "result": None, "harness": sys.modules[__name__]}
		with open(codePath, encoding="utf-8") as f:
			# The code is the test's own, written beside the call to runChild.
			exec(compile(f.read(), codePath, "exec"), namespace)  # noqa: S102
		outcome["value"] = namespace.get("result")
	# SystemExit and KeyboardInterrupt included: whatever ended the code is the result to report.
	except BaseException:  # noqa: BLE001
		outcome["error"] = traceback.format_exc()
	checks.finish()
	faulthandler.cancel_dump_traceback_later()
	with open(resultPath, "w", encoding="utf-8") as f:
		json.dump(outcome, f, default=repr)


def _launch(eventsPath: str, recordLevel: str, launcher: str, *arguments: str) -> None:
	"""Run the launcher as the main program of this process, with the checks in place.

	The launcher's path stays as given, after this file's own arguments, so that a copy NVDA starts
	to restart itself carries them over with the interpreter's options, and runs checked too.
	"""
	requireRig()
	record = EventFile(eventsPath)
	checks = Checks(record, recordLevel=int(recordLevel))
	record("start", launcher=launcher, arguments=list(arguments))
	# Signal dispositions are left as the caller's were, since a test may start NVDA as nohup does,
	# with SIGHUP or SIGINT ignored; the rig gives its command their default actions.
	import atexit
	import runpy

	# The first registered runs last, after NVDA's own.
	atexit.register(checks.finish)
	sys.argv[:] = [launcher, *arguments]
	runpy.run_path(launcher, run_name="__main__")


def _loadFile(name: str, path: Path) -> ModuleType:
	spec = importlib.util.spec_from_file_location(name, path)
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module


_loaded: dict[str, ModuleType] = {}


def transcriptRecords() -> ModuleType:
	"""tests/linux/synthDrivers/_transcriptRecords.py, which reads what the transcript synth wrote."""
	if "transcriptRecords" not in _loaded:
		_loaded["transcriptRecords"] = _loadFile(
			"_transcriptRecords",
			HERE / "synthDrivers" / "_transcriptRecords.py",
		)
	return _loaded["transcriptRecords"]


def singleInstance() -> ModuleType:
	"""source/linuxPlatform/singleInstance.py, loaded alone, as it needs only the standard library."""
	if "singleInstance" not in _loaded:
		_loaded["singleInstance"] = _loadFile(
			"singleInstance", SOURCE / "linuxPlatform" / "singleInstance.py"
		)
	return _loaded["singleInstance"]


#: The first line of each NVDA log record: level, code path, time, thread. The code path can hold
#: spaces: logHandler writes "unhandled exception", "Exception ignored in: <...>" and "Python warning"
#: there.
NVDA_LOG_RECORD_HEADER = re.compile(
	r"^(?P<level>[A-Z_]+) - .+? \(\d\d:\d\d:\d\d\.\d{3}\) - .*:$", re.MULTILINE
)
#: Any line that starts as a record at ERROR or above does, whatever follows it.
_nvdaLogErrorRecord = re.compile(r"^(?:ERROR|CRITICAL) - .*$", re.MULTILINE)


def nvdaLogErrorRecords(text: str) -> list[str]:
	"""The records at ERROR or above in an NVDA log file, or lines that look like one."""
	return _nvdaLogErrorRecord.findall(text)


#: How long a typed marker must be, so that finding its letters strung together in a log is no accident.
MARKER_LENGTH = 8


def _letters(text: str) -> str:
	return "".join(character for character in text.casefold() if character.isalnum())


def textsHoldingMarker(marker: str, texts: dict[str, str]) -> list[str]:
	"""The names of the texts, such as log files by name, that hold marker, a string a test typed.

	Held in any case, and also with anything at all between its letters, as a log that wrote each key
	on a line of its own would hold it.
	"""
	if len(_letters(marker)) < MARKER_LENGTH or _letters(marker) != marker.casefold():
		raise ValueError(f"a marker is {MARKER_LENGTH} or more letters and digits and nothing else")
	wanted = marker.casefold()
	return [name for name, text in texts.items() if wanted in text.casefold() or wanted in _letters(text)]


def assertMarkerAbsent(marker: str, texts: dict[str, str]) -> None:
	"""Fails, naming only the texts, when any of them holds marker: what was typed is never logged."""
	holding = textsHoldingMarker(marker, texts)
	assert not holding, f"what the test typed is written in {', '.join(holding)}"


#: The sessions a test names in pytest.mark.rigSession: the rig's X display, either of its Wayland
#: compositors, or one of them by name.
RIG_SESSIONS = ("x11", "wayland", "mutter", "sway")


def rigSession() -> str:
	"""x11, mutter or sway: the session this rig gave its command. The rig sets LINDA_RIG_COMPOSITOR in
	every Wayland session, and refuses it for an X one."""
	return os.environ.get("LINDA_RIG_COMPOSITOR") or "x11"


def rigHasXwayland() -> bool:
	return rigSession() != "x11" and os.environ.get("LINDA_RIG_XWAYLAND") == "1"


def rigSessionIsOneOf(names: Collection[str]) -> bool:
	unknown = set(names) - set(RIG_SESSIONS)
	if unknown or not names:
		raise ValueError(f"rigSession takes some of {', '.join(RIG_SESSIONS)}, not {sorted(unknown)}")
	session = rigSession()
	return session in names or ("wayland" in names and session != "x11")


class Launch:
	"""One run of the launcher, as a process of its own, and what it recorded and spoke."""

	def __init__(
		self, directory: Path, process: subprocess.Popen, started: float, overlay: Path | None = None
	) -> None:
		self.directory = directory
		self.process = process
		#: time.monotonic() as the process was started; the transcript synth stamps its records with
		#: the same clock, which is system wide.
		self.started = started
		#: The directory given to isolatedCommand for the launch, if it was run that way.
		self.overlay = overlay

	@property
	def eventsPath(self) -> Path:
		return self.directory / "events.jsonl"

	def text(self, name: str) -> str:
		"""A file in the launch's directory, such as nvda.log or stderr.txt, or "" if there is none."""
		path = self.directory / name
		return path.read_text(encoding="utf-8") if path.exists() else ""

	def spoken(self) -> list[dict]:
		"""The transcript synth's speak records."""
		path = self.directory / "transcript.jsonl"
		if not path.exists():
			return []
		return [r for r in transcriptRecords().readRecords(path) if r["kind"] == "speak"]

	def spokenTexts(self) -> list[str]:
		return [transcriptRecords().spokenText(r) for r in self.spoken()]

	def describe(self) -> str:
		parts = [describeExit(self.process.poll())]
		for name in ("stdout.txt", "stderr.txt"):
			if text := self.text(name):
				parts.append(f"{name}:\n{text[-3000:]}")
		problems = self.result().problems(ignoring={"returnCode", "stderr"})
		if problems:
			parts.append("\n".join(problems)[-6000:])
		if log := self.text("nvda.log"):
			parts.append(f"log tail:\n{log[-6000:]}")
		return "\n".join(parts)

	def waitForSpeech(self, text: str, timeout: float) -> float:
		"""Seconds from starting the process to text being spoken, as a whole speak record."""
		deadline = self.started + timeout
		while True:
			exited = self.process.poll() is not None
			# Read after the poll, so that what a process spoke before it exited is seen.
			for record in self.spoken():
				if transcriptRecords().spokenText(record) == text:
					return record["t"] - self.started
			if exited or time.monotonic() >= deadline:
				raise AssertionError(f"{text!r} was not spoken within {timeout} s\n{self.describe()}")
			time.sleep(0.05)

	def waitForLog(self, text: str, timeout: float, name: str = "nvda.log") -> None:
		"""Wait until the file name in the launch's directory holds text."""
		deadline = time.monotonic() + timeout
		while True:
			exited = self.process.poll() is not None
			# Read after the poll, so that what a process wrote before it exited is seen.
			if text in self.text(name):
				return
			if exited or time.monotonic() >= deadline:
				raise AssertionError(f"{text!r} was not in {name} within {timeout} s\n{self.describe()}")
			time.sleep(0.05)

	def waitForExit(self, timeout: float) -> int:
		try:
			return self.process.wait(timeout)
		except subprocess.TimeoutExpired:
			raise AssertionError(f"NVDA did not exit within {timeout} s\n{self.describe()}") from None

	def stop(self) -> None:
		"""Kill the process if it is still running."""
		if self.process.poll() is None:
			self.process.kill()
		self.process.wait()

	def result(self) -> ChildResult:
		"""What the launch recorded so far, with the copies it started to restart itself."""
		events = readEvents(self.eventsPath)
		returnCode = self.process.poll()
		return ChildResult.fromEvents(
			events,
			self.overlay,
			returnCode=returnCode,
			error=None if any(e["kind"] == "start" for e in events) else "the launch recorded nothing",
			stdout=self.text("stdout.txt"),
			stderr=self.text("stderr.txt"),
		)

	def assertClean(self, *, ignoring: Collection[str] = (), logNames: Collection[str] = ()) -> None:
		"""The launch has exited and every check in CLEAN_CHECKS holds, apart from those in ignoring.

		:param logNames: NVDA log files in the launch's directory that must hold records, none of them
			at ERROR or above, unless logErrors is ignored.
		"""
		problems = self.result().problems(ignoring)
		for name in logNames:
			log = self.text(name)
			if not NVDA_LOG_RECORD_HEADER.search(log):
				problems.append(f"{name} holds no records")
			elif "logErrors" not in ignoring and (errors := nvdaLogErrorRecords(log)):
				problems.append(f"{name} holds {len(errors)} records at ERROR or above\n{log[-8000:]}")
		assert not problems, "\n\n".join(problems)


def startLauncher(
	directory: Path,
	*arguments: str,
	env: dict[str, str | None] | None = None,
	recordLevel: int = LOG_ERROR,
) -> Launch:
	"""Start source/linda.py with arguments, in directory, under the checks runChild makes.

	directory receives stdout.txt, stderr.txt, the checks' records and the transcript synth's
	transcript.jsonl. The test hooks give NVDA tests/linux/synthDrivers and
	tests/linux/fixtures/globalPlugins; env can change any of that, and None unsets a variable.
	"""
	requireRig()
	directory.mkdir(parents=True, exist_ok=True)
	hooks = directory / "testHooks"
	hooks.mkdir(exist_ok=True)
	for name, target in (
		("synthDrivers", HERE / "synthDrivers"),
		("globalPlugins", HERE / "fixtures" / "globalPlugins"),
	):
		if not (hooks / name).is_symlink():
			(hooks / name).symlink_to(target)
	environment = _environment(
		{
			"LINDA_TEST_HOOKS_DIR": str(hooks),
			"LINDA_TRANSCRIPT_FILE": str(directory / "transcript.jsonl"),
			"LINDA_TRANSCRIPT_MODE": "immediate",
			**(env or {}),
		},
	)
	overlay = Path(tempfile.mkdtemp(prefix="overlay-", dir=directory))
	command = isolatedCommand(
		overlay,
		[
			sys.executable,
			"-I",
			"-B",
			str(Path(__file__).resolve()),
			"launch",
			str(directory / "events.jsonl"),
			str(recordLevel),
			str(LAUNCHER),
			*arguments,
		],
	)
	with open(directory / "stdout.txt", "w") as stdout, open(directory / "stderr.txt", "w") as stderr:
		started = time.monotonic()
		process = subprocess.Popen(command, env=environment, cwd=directory, stdout=stdout, stderr=stderr)
	return Launch(directory, process, started, overlay)


def stopRunningCopy() -> None:
	"""Stop the copy of NVDA holding the lock of the rig's display, if any, as a new copy would.

	A copy that a launch started to restart itself is not the test's child, and is found this way.
	"""
	module = singleInstance()
	lock = module.InstanceLock(module.lockFilePath())
	try:
		module.terminateRunning(lock)
	finally:
		lock.close()


@dataclass
class _Launches:
	directory: Path
	started: list[Launch] = field(default_factory=list)

	def __call__(self, name: str, *arguments: str, env: dict[str, str | None] | None = None) -> Launch:
		launch = startLauncher(self.directory / name, *arguments, env=env)
		self.started.append(launch)
		return launch


def launches(directory: Path) -> Iterator[_Launches]:
	"""Starts launches, each in a directory of its own under directory, and afterwards makes sure none
	outlives the caller and none changed the repository. Made a fixture by conftest.py."""
	before = treeState()
	started = _Launches(directory)
	yield started
	for launch in started.started:
		launch.stop()
	stopRunningCopy()
	changed = changedPaths(before, treeState())
	assert not changed, f"a launch changed the repository: {changed}"


if __name__ == "__main__":
	if sys.argv[1] == "child":
		_child(*sys.argv[2:8])
	elif sys.argv[1] == "launch":
		_launch(*sys.argv[2:])
	elif sys.argv[1] == "isolate":
		_isolate(*sys.argv[2:])
	else:
		sys.exit(f"harness: unknown mode {sys.argv[1]!r}")
