"""The rig keeps what runs in it away from the live desktop, and reports honestly.

The first tests look around from inside the sandbox they run in. The rest start a second rig inside
the first, which bubblewrap allows, to see the rig's own behaviour from its caller's side.

nix develop . -c tests/linux/rig python -m pytest tests/linux/test_rig.py

The tests of the rig's internet run only in a rig that has it, and are skipped otherwise:

LINDA_RIG_NET=1 nix develop . -c tests/linux/rig python -m pytest tests/linux/test_rig.py
"""

import errno
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import network
import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
RIG = HERE / "rig"

#: Everything the sandbox's environment may hold besides LINDA_ and BENCH_ variables: what
#: the rig sets, what it passes on from the dev shell, and what bash, Python and pytest add.
ALLOWED_VARIABLES = {
	"AT_SPI_BUS_LAUNCHER",
	"DBUS_SESSION_BUS_ADDRESS",
	"DBUS_STARTER_ADDRESS",
	"DBUS_STARTER_BUS_TYPE",
	"DBUS_SYSTEM_BUS_ADDRESS",
	"DISPLAY",
	"GDK_PIXBUF_MODULE_FILE",
	"GI_TYPELIB_PATH",
	"GNOME_ACCESSIBILITY",
	"GSETTINGS_BACKEND",
	"HOME",
	"LANG",
	"LOCALE_ARCHIVE",
	"LOGNAME",
	"OLDPWD",
	"PATH",
	"PULSE_SERVER",
	"PWD",
	"PYTEST_ADDOPTS",
	"PYTEST_CURRENT_TEST",
	"PYTEST_VERSION",
	"PYTHONDONTWRITEBYTECODE",
	"PYTHONNOUSERSITE",
	"QT_ACCESSIBILITY",
	"QT_LINUX_ACCESSIBILITY_ALWAYS_ON",
	"SHLVL",
	"TERM",
	"TMPDIR",
	"TZ",
	"USER",
	"XDG_CACHE_HOME",
	"XDG_CONFIG_HOME",
	"XDG_DATA_DIRS",
	"XDG_DATA_HOME",
	"XDG_RUNTIME_DIR",
	"XDG_STATE_HOME",
	"_",
}


def rig(
	*arguments: str, stdin: str | None = None, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess:
	return subprocess.run(
		[str(RIG), *arguments],
		input=stdin,
		capture_output=True,
		text=True,
		timeout=120,
		check=False,
		env=env,
	)


def withoutNetwork() -> dict[str, str]:
	"""This environment without the variable that gives a rig started from it the internet."""
	return {name: value for name, value in os.environ.items() if name != network.VARIABLE}


#: Run in a rig with an address and a port: exits 0 if a TCP connection to them succeeds, 1 if not.
CONNECT = """
import socket, sys
try:
	socket.create_connection((sys.argv[1], int(sys.argv[2])), timeout=5).close()
except OSError as error:
	sys.exit(f"{error.errno} {error}")
"""


def test_environmentHoldsOnlyWhatTheRigGives():
	strangers = {
		name
		for name in os.environ
		if name not in ALLOWED_VARIABLES and not name.startswith(("LINDA_", "BENCH_"))
	}
	assert not strangers, f"variables from outside the dev shell: {sorted(strangers)}"


def test_searchPathsHoldOnlyTheStore():
	for name in ("PATH", "XDG_DATA_DIRS", "GI_TYPELIB_PATH"):
		outside = [entry for entry in os.environ[name].split(":") if not entry.startswith("/nix/store/")]
		assert not outside, f"{name} reaches outside the store: {outside}"


def test_noVariableNamesTheLiveSession():
	live = ("/run/user/", "/run/current-system", "/etc/profiles", "/nix/var/nix/profiles")
	named = {name: value for name, value in os.environ.items() if any(mark in value for mark in live)}
	assert not named


def test_sandboxIsItsOwnProcessTree():
	assert Path("/proc/1/comm").read_text(encoding="ascii").strip() == "bwrap"
	# A machine's process table holds hundreds; this one holds the rig's services and this test.
	assert len([entry for entry in os.listdir("/proc") if entry.isdigit()]) < 40


@pytest.mark.parametrize(
	"path",
	[
		"/run/user",
		"/run/dbus",
		"/run/current-system",
		"/dev/snd",
		"/dev/input",
		"/dev/uinput",
		"/dev/hidraw0",
		"/dev/ttyS0",
		"/tmp/.X11-unix/X0",
		"/var",
		"/mnt",
	],
)
def test_liveResourceIsAbsent(path: str):
	assert not os.path.lexists(path)


def gitHistory() -> Path:
	"""The git directory that holds the checkout's history, which the rig binds beside it: its own
	.git, or for a worktree the .git of the repository the worktree belongs to."""
	dotGit = ROOT / ".git"
	if dotGit.is_dir():
		return dotGit
	gitDir = Path(dotGit.read_text(encoding="utf-8").removeprefix("gitdir:").strip())
	common = gitDir / "commondir"
	return (gitDir / common.read_text(encoding="utf-8").strip()).resolve() if common.exists() else gitDir


def test_realHomeIsAbsent():
	# The checkout and its history are all that is bound in from under a home directory.
	bound = (ROOT, gitHistory())
	visible = {
		str(path)
		for path in Path("/home").rglob("*")
		if not any(path == top or top in path.parents for top in bound)
	}
	assert visible <= {str(parent) for top in bound for parent in top.parents}, sorted(visible)[:10]


def test_historyIsReadOnly():
	with pytest.raises(OSError):
		(gitHistory() / "written-by-test_rig").write_text("", encoding="ascii")


def test_runIsEmpty():
	assert os.listdir("/run") == []


def test_displayIsThePrivateOne():
	assert os.environ["DISPLAY"] == ":99"
	# X display 0 by its abstract socket, which no mount could hide: only the network namespace does.
	with socket.socket(socket.AF_UNIX) as probe, pytest.raises(OSError):
		probe.connect("\0/tmp/.X11-unix/X0")


def test_publicAddressIsUnreachableWithoutTheVariable():
	done = rig(sys.executable, "-c", CONNECT, "9.9.9.9", "443", env=withoutNetwork())
	assert done.returncode == 1
	# At once, as with no route at all, rather than after a timeout somewhere on the way.
	assert done.stderr.startswith(f"{errno.ENETUNREACH} "), done.stderr


def test_checkoutIsReadOnly():
	with pytest.raises(OSError):
		(ROOT / "written-by-test_rig").write_text("", encoding="ascii")


def test_sessionBusCanStartNothing():
	names = subprocess.run(
		[
			"dbus-send",
			"--session",
			"--print-reply",
			"--dest=org.freedesktop.DBus",
			"/org/freedesktop/DBus",
			"org.freedesktop.DBus.ListActivatableNames",
		],
		capture_output=True,
		text=True,
		timeout=30,
		check=True,
	).stdout
	activatable = [line.split('"')[1] for line in names.splitlines() if "string" in line]
	assert activatable == ["org.freedesktop.DBus"]


def test_localeIsFixedAndEveryLocaleExists():
	assert os.environ["LANG"] == "C.UTF-8"
	assert "LANGUAGE" not in os.environ
	available = subprocess.run(["locale", "-a"], capture_output=True, text=True, check=True).stdout.split()
	for wanted in ("pl_PL.utf8", "en_GB.utf8", "pt_BR.utf8", "sq_AL.utf8"):
		assert wanted in available


def test_sigintHasItsDefaultAction():
	assert signal.getsignal(signal.SIGINT) is signal.default_int_handler


def test_statusIsTheCommands():
	assert rig("sh", "-c", "exit 7").returncode == 7


def test_signalDeathIsReported():
	assert rig("sh", "-c", "kill -SEGV $$").returncode == 128 + signal.SIGSEGV


def test_standardInputReachesTheCommand():
	done = rig(sys.executable, "-", stdin="print(6 * 7)\nraise SystemExit(3)\n")
	assert (done.returncode, done.stdout) == (3, "42\n")


def test_rigsOwnFailuresHaveTheirOwnStatus():
	for arguments in ([], ["--bind"], ["--bind", "/nonexistent", "true"]):
		done = rig(*arguments)
		assert done.returncode == 125, arguments
		assert done.stderr.startswith(("rig:", "usage:")), done.stderr


def test_unknownWindowManagerIsRefused(monkeypatch: pytest.MonkeyPatch):
	monkeypatch.setenv("LINDA_RIG_WM", "twm")
	done = rig("true")
	assert done.returncode == 125
	assert "unknown window manager" in done.stderr


@pytest.mark.parametrize(
	"directory",
	["/", "/tmp", "/tmp/.X11-unix", "/run", "/dev", "/dev/shm", "/proc", "/sys", "/nix", "/etc", "/home"],
)
def test_bindOfLiveOrSystemDirectoryIsRefused(directory: str):
	done = rig("--bind", directory, "true")
	assert done.returncode == 125
	assert "--bind" in done.stderr


def test_bindOfCheckoutIsRefused():
	for directory in (ROOT, ROOT / "tests", ROOT.parent):
		done = rig("--bind", str(directory), "true")
		assert done.returncode == 125, directory
		assert "would expose" in done.stderr


def test_boundDirectoryIsWritableAndOutlivesTheRun(tmp_path: Path):
	done = rig("--bind", str(tmp_path), "sh", "-c", f"echo kept > '{tmp_path}/result'")
	assert done.returncode == 0, done.stderr
	assert (tmp_path / "result").read_text(encoding="ascii") == "kept\n"


def test_unboundDirectoryIsInvisible(tmp_path: Path):
	(tmp_path / "secret").write_text("", encoding="ascii")
	assert rig("test", "-e", str(tmp_path / "secret")).returncode == 1


def test_callersVariablesStayOutside(monkeypatch: pytest.MonkeyPatch):
	monkeypatch.setenv("SESSION_MANAGER", "local/host:@/tmp/.ICE-unix/1")
	monkeypatch.setenv("LINDA_PASSED_ON", "yes")
	done = rig("sh", "-c", 'echo "${SESSION_MANAGER:-unset} ${LINDA_PASSED_ON:-unset}"')
	assert done.stdout == "unset yes\n"


def test_nothingIsLeftBehind(tmp_path: Path):
	# The rig makes its working directory under TMPDIR, so one of its own shows what it leaves.
	work = tmp_path / "work"
	work.mkdir()
	done = subprocess.run(
		["env", f"TMPDIR={work}", str(RIG), "sh", "-c", 'mkdir "$HOME/locked" && chmod 0 "$HOME/locked"'],
		capture_output=True,
		text=True,
		timeout=120,
		check=False,
	)
	assert done.returncode == 0, done.stderr
	assert os.listdir(work) == []


def test_httpsWorksWithTheVariable():
	network.requireNetwork()
	done = rig(
		sys.executable,
		"-c",
		"import urllib.request; print(urllib.request.urlopen('https://example.com/', timeout=30).status)",
	)
	assert (done.returncode, done.stdout) == (0, "200\n"), done.stderr


def test_namesResolveWithTheVariable():
	network.requireNetwork()
	done = rig(sys.executable, "-c", "import socket; print(socket.getaddrinfo('example.com', 443)[0][4][0])")
	assert done.returncode == 0, done.stderr
	assert done.stdout.strip(), done.stderr


def hostAddresses() -> list[str]:
	"""This network namespace's addresses, but for loopback's."""
	listing = subprocess.run(
		["ip", "-o", "address", "show"], capture_output=True, text=True, check=True
	).stdout
	return [fields[3].split("/")[0] for fields in map(str.split, listing.splitlines()) if fields[1] != "lo"]


def defaultGateway() -> str:
	route = subprocess.run(
		["ip", "-4", "route", "show", "default"], capture_output=True, text=True, check=True
	)
	fields = route.stdout.split()
	return fields[fields.index("via") + 1]


#: Run in a rig with address and port pairs: prints each pair a TCP connection reached.
CONNECT_ANY = """
import socket, sys
pairs = sys.argv[1:]
for address, port in zip(pairs[::2], pairs[1::2]):
	family = socket.AF_INET6 if ":" in address else socket.AF_INET
	with socket.socket(family) as probe:
		probe.settimeout(3)
		try:
			probe.connect((address, int(port)))
		except OSError:
			continue
		print(address, port)
"""


def test_hostIsUnreachableWithTheVariable():
	network.requireNetwork()
	with (
		socket.create_server(("127.0.0.1", 0)) as loopback,
		socket.create_server(("0.0.0.0", 0)) as everywhere,
	):
		loopbackPort = str(loopback.getsockname()[1])
		everywherePort = str(everywhere.getsockname()[1])
		targets = [("127.0.0.1", loopbackPort)]
		for address in (defaultGateway(), *hostAddresses()):
			targets += [(address, loopbackPort), (address, everywherePort)]
		done = rig(sys.executable, "-c", CONNECT_ANY, *(part for target in targets for part in target))
		assert done.returncode == 0, done.stderr
		assert done.stdout == "", f"reached from inside: {done.stdout}"
		for listener in (loopback, everywhere):
			listener.setblocking(False)
			with pytest.raises(BlockingIOError):
				listener.accept()


def test_hostAddressesAreShutOffWithTheVariable():
	# The caller here has only the address pasta copied into it, which the inner rig shares and so
	# reaches inside itself; a real host has more, and these routes are what keeps them out.
	network.requireNetwork()
	done = rig("sh", "-c", "ip -o -4 route show type prohibit && ip -o -6 route show type prohibit")
	assert done.returncode == 0, done.stderr
	shutOff = {line.split()[1] for line in done.stdout.splitlines()}
	assert set(hostAddresses()) <= shutOff, done.stdout


def test_abstractSocketIsUnreachableWithTheVariable():
	network.requireNetwork()
	name = f"\0linda-test_rig-{os.getpid()}"
	with socket.socket(socket.AF_UNIX) as listener:
		listener.bind(name)
		listener.listen()
		probe = f"import socket; socket.socket(socket.AF_UNIX).connect({name!r})"
		done = rig(sys.executable, "-c", probe)
		assert done.returncode == 1
		assert "ConnectionRefusedError" in done.stderr, done.stderr


#: Run in a rig with a shared directory, a name and whether the rig has the internet: holds a port
#: that every rig run with it holds too, waits until all three hold it, then uses the network.
SIDE_BY_SIDE = """
import pathlib, socket, sys, time, urllib.request
shared, name, networked = pathlib.Path(sys.argv[1]), sys.argv[2], sys.argv[3] == "1"
with socket.create_server(("127.0.0.1", 47031)):
	(shared / name).touch()
	deadline = time.monotonic() + 90
	while len(list(shared.iterdir())) < 3:
		if time.monotonic() > deadline:
			sys.exit("the other rigs never came")
		time.sleep(0.1)
	if networked:
		assert urllib.request.urlopen("https://example.com/", timeout=30).status == 200
	else:
		try:
			socket.create_connection(("9.9.9.9", 443), timeout=5)
		except OSError:
			pass
		else:
			sys.exit("a rig without the variable reached the internet")
"""


def test_networkedRigsRunSideBySide(tmp_path: Path):
	network.requireNetwork()
	runs = [
		subprocess.Popen(
			[
				str(RIG),
				"--bind",
				str(tmp_path),
				sys.executable,
				"-c",
				SIDE_BY_SIDE,
				str(tmp_path),
				name,
				networked,
			],
			env=os.environ if networked == "1" else withoutNetwork(),
			stderr=subprocess.PIPE,
			text=True,
		)
		for name, networked in (("first", "1"), ("second", "1"), ("plain", "0"))
	]
	for run in runs:
		_, errors = run.communicate(timeout=180)
		assert run.returncode == 0, errors


def descendants(ancestor: int) -> dict[int, str]:
	"""The processes below ancestor, by process ID, with their names."""
	parents: dict[int, tuple[int, str]] = {}
	for entry in Path("/proc").iterdir():
		if not entry.name.isdigit():
			continue
		try:
			stat = (entry / "stat").read_text(encoding="ascii", errors="replace")
		except OSError:
			continue
		# The name is in parentheses and may itself hold parentheses; the fields after it are plain.
		name = stat[stat.index("(") + 1 : stat.rindex(")")]
		parents[int(entry.name)] = (int(stat[stat.rindex(")") + 2 :].split()[1]), name)
	found: dict[int, str] = {}
	frontier = [ancestor]
	while frontier:
		parent = frontier.pop()
		for pid, (ppid, name) in parents.items():
			if ppid == parent and pid not in found:
				found[pid] = name
				frontier.append(pid)
	return found


@pytest.mark.parametrize(
	("ending", "status"),
	[("exit", 0), ("fail", 3), (signal.SIGTERM, 143), (signal.SIGINT, 130)],
)
def test_noHelperOutlivesANetworkedRig(tmp_path: Path, ending: str | signal.Signals, status: int):
	network.requireNetwork()
	work = tmp_path / "work"
	shared = tmp_path / "shared"
	work.mkdir()
	shared.mkdir()
	run = subprocess.Popen(
		[
			str(RIG),
			"--bind",
			str(shared),
			"sh",
			"-c",
			'touch "$1/ready"; read -r code; exit "$code"',
			"sh",
			str(shared),
		],
		env={**os.environ, "TMPDIR": str(work)},
		stdin=subprocess.PIPE,
		stderr=subprocess.PIPE,
		text=True,
	)
	try:
		deadline = time.monotonic() + 60
		while not (shared / "ready").exists():
			assert run.poll() is None, run.stderr.read() if run.stderr else ""
			assert time.monotonic() < deadline, "the rig's command never started"
			time.sleep(0.1)
		helpers = descendants(run.pid)
		# pasta runs as whichever build of passt suits the processor.
		assert any(name.startswith(("pasta", "passt")) for name in helpers.values()), helpers
		if isinstance(ending, signal.Signals):
			run.send_signal(ending)
			run.wait(timeout=60)
		else:
			run.communicate(f"{status}\n", timeout=60)
		assert run.returncode == status
	finally:
		if run.poll() is None:
			run.kill()
			run.wait()
	deadline = time.monotonic() + 10
	while (left := {pid: name for pid, name in helpers.items() if Path(f"/proc/{pid}").exists()}) and (
		time.monotonic() < deadline
	):
		time.sleep(0.1)
	assert not left
	assert os.listdir(work) == []
