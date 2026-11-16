"""The rig keeps what runs in it away from the live desktop, and reports honestly.

The first tests look around from inside the sandbox they run in. The rest start a second rig inside
the first, which bubblewrap allows, to see the rig's own behaviour from its caller's side.

nix develop . -c tests/linux/rig python -m pytest tests/linux/test_rig.py
"""

import os
import signal
import socket
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
RIG = HERE / "rig"

#: Everything the sandbox's environment may hold besides BASEDREADER_ and BENCH_ variables: what
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


def rig(*arguments: str, stdin: str | None = None) -> subprocess.CompletedProcess:
	return subprocess.run(
		[str(RIG), *arguments],
		input=stdin,
		capture_output=True,
		text=True,
		timeout=120,
		check=False,
	)


def test_environmentHoldsOnlyWhatTheRigGives():
	strangers = {
		name
		for name in os.environ
		if name not in ALLOWED_VARIABLES and not name.startswith(("BASEDREADER_", "BENCH_"))
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


def test_noNetwork():
	with socket.socket() as probe:
		probe.settimeout(2)
		with pytest.raises(OSError):
			probe.connect(("192.0.2.1", 9))


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
	monkeypatch.setenv("BASEDREADER_PASSED_ON", "yes")
	done = rig("sh", "-c", 'echo "${SESSION_MANAGER:-unset} ${BASEDREADER_PASSED_ON:-unset}"')
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
