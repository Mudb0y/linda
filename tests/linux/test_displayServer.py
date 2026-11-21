"""Which kind of session displayServer says each environment is, with no display opened to find out.

displayServer.py is loaded alone, by its path, as it needs only the standard library. Each case sets
the environment, forgets the cached answer and asks again, while Xlib's Display and every socket
refuse to be made, so that an answer reached by connecting anywhere fails the test.

nix develop . -c tests/linux/rig python3 -m pytest -q tests/linux/test_displayServer.py
"""

import ast
import importlib.util
import socket
import sys
from collections.abc import Iterator
from pathlib import Path
from unittest import mock

import harness
import pytest
import Xlib.display

MODULE_PATH = harness.SOURCE / "linuxPlatform" / "displayServer.py"
_spec = importlib.util.spec_from_file_location("displayServer", MODULE_PATH)
displayServer = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(displayServer)

#: Everything displayServer reads, unset before each case so that the rig's own environment plays no
#: part.
READ_VARIABLES = (
	"DISPLAY",
	"WAYLAND_DISPLAY",
	"XDG_SESSION_TYPE",
	"XDG_CURRENT_DESKTOP",
	"XDG_RUNTIME_DIR",
	"GAMESCOPE_WAYLAND_DISPLAY",
	"SWAYSOCK",
	"NIRI_SOCKET",
	"HYPRLAND_INSTANCE_SIGNATURE",
	displayServer.OVERRIDE_VARIABLE,
)

#: The owner's MATE session under lightdm, as read on 21 November 2026 from the environment of the
#: screen reader they were running; WAYLAND_DISPLAY was not set. It is the session this module must
#: never move off the X route.
OWNER_SESSION = {
	"XDG_SESSION_TYPE": "x11",
	"XDG_CURRENT_DESKTOP": "MATE",
	"DISPLAY": ":0",
}
OWNER_FACTS = [
	"DISPLAY=:0",
	"WAYLAND_DISPLAY is not set.",
	"XDG_SESSION_TYPE=x11",
	"XDG_CURRENT_DESKTOP=MATE",
	"GAMESCOPE_WAYLAND_DISPLAY is not set.",
]

GNOME = {"XDG_SESSION_TYPE": "wayland", "XDG_CURRENT_DESKTOP": "GNOME", "WAYLAND_DISPLAY": "wayland-0"}
#: pam_systemd registers a console login as tty, and sway leaves the type as it found it.
SWAY_FROM_CONSOLE = {
	"XDG_SESSION_TYPE": "tty",
	"WAYLAND_DISPLAY": "wayland-1",
	"SWAYSOCK": "/run/user/1000/sway-ipc.1000.1234.sock",
}
#: What gamescope's main.cpp gives the programs it starts: its Xwayland's DISPLAY and its own
#: socket's name, with WAYLAND_DISPLAY unset unless --expose-wayland is given.
GAMESCOPE_CHILD = {"DISPLAY": ":1", "GAMESCOPE_WAYLAND_DISPLAY": "gamescope-0"}


class Session:
	"""An environment to classify, with a runtime directory whose Wayland sockets are real."""

	def __init__(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
		self._monkeypatch = monkeypatch
		self.runtimeDir = tmp_path / "run"
		self.runtimeDir.mkdir(mode=0o700)
		self._sockets: list[socket.socket] = []
		self.displaysMade: list[tuple] = []
		# A socket's path may be no longer than 107 bytes, and pytest's folders can be longer.
		monkeypatch.chdir(self.runtimeDir)

	def listen(self, name: str) -> Path:
		"""Listen on a Unix socket called name in the runtime directory, as a compositor does."""
		server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
		self._sockets.append(server)
		server.bind(name)
		server.listen()
		return self.runtimeDir / name

	def close(self) -> None:
		for server in self._sockets:
			server.close()

	def set(self, env: dict[str, str | None]) -> None:
		"""Give the process exactly env, with XDG_RUNTIME_DIR the runtime directory unless env says."""
		for name in READ_VARIABLES:
			self._monkeypatch.delenv(name, raising=False)
		self._monkeypatch.setenv("XDG_RUNTIME_DIR", str(self.runtimeDir))
		for name, value in env.items():
			if value is None:
				self._monkeypatch.delenv(name, raising=False)
			else:
				self._monkeypatch.setenv(name, value)
		displayServer._decision.cache_clear()

	def change(self, name: str, value: str) -> None:
		"""Set one variable and leave the cached answer as it is."""
		self._monkeypatch.setenv(name, value)

	def ask(self, question):
		"""question(), with an X display and any socket refusing to be made while it runs."""

		def display(*args, **kwargs):
			self.displaysMade.append(args)
			raise AssertionError("an X display was opened")

		def anySocket(*args, **kwargs):
			raise AssertionError("a socket was made")

		with (
			mock.patch.object(Xlib.display, "Display", display),
			mock.patch.object(socket, "socket", anySocket),
		):
			return question()

	def classify(self, env: dict[str, str | None]) -> tuple[str, int, str]:
		self.set(env)
		return self.ask(
			lambda: (displayServer.kind(), displayServer._decision().rule, displayServer.compositor())
		)


@pytest.fixture
def session(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Session]:
	made = Session(monkeypatch, tmp_path)
	yield made
	made.close()
	assert made.displaysMade == []
	displayServer._decision.cache_clear()


#: Each case: its name, the environment, the sockets listening in the runtime directory, and the
#: kind, deciding rule and compositor expected.
CASES = [
	("owner's session", OWNER_SESSION, (), ("x11", 2, "other")),
	(
		"owner's session with a stray WAYLAND_DISPLAY naming a real socket",
		{**OWNER_SESSION, "WAYLAND_DISPLAY": "wayland-0"},
		("wayland-0",),
		("x11", 2, "other"),
	),
	(
		"owner's session with a nested sway's socket and its own variables",
		{**OWNER_SESSION, "WAYLAND_DISPLAY": "wayland-1", "SWAYSOCK": "/run/user/1000/sway-ipc.sock"},
		("wayland-1",),
		("x11", 2, "sway"),
	),
	("DISPLAY=:99 alone, as in the rig", {"DISPLAY": ":99"}, (), ("x11", 6, "other")),
	(
		"startx with a WAYLAND_DISPLAY whose socket is missing",
		{"XDG_SESSION_TYPE": "tty", "DISPLAY": ":0", "WAYLAND_DISPLAY": "wayland-1"},
		(),
		("x11", 6, "other"),
	),
	(
		"startx with a WAYLAND_DISPLAY that names no socket at all",
		{"DISPLAY": ":0", "WAYLAND_DISPLAY": "notASocket"},
		(),
		("x11", 6, "other"),
	),
	(
		"a relative WAYLAND_DISPLAY with no XDG_RUNTIME_DIR to find it in",
		{"DISPLAY": ":0", "WAYLAND_DISPLAY": "wayland-0", "XDG_RUNTIME_DIR": None},
		("wayland-0",),
		("x11", 6, "other"),
	),
	(
		"sway from a console with Xwayland",
		{**SWAY_FROM_CONSOLE, "DISPLAY": ":0"},
		("wayland-1",),
		("wayland", 4, "sway"),
	),
	("sway from a console without Xwayland", SWAY_FROM_CONSOLE, ("wayland-1",), ("wayland", 4, "sway")),
	("GNOME with DISPLAY", {**GNOME, "DISPLAY": ":0"}, ("wayland-0",), ("wayland", 4, "gnome")),
	("GNOME without DISPLAY", GNOME, ("wayland-0",), ("wayland", 4, "gnome")),
	(
		"Ubuntu's GNOME",
		{**GNOME, "XDG_CURRENT_DESKTOP": "ubuntu:GNOME", "DISPLAY": ":0"},
		("wayland-0",),
		("wayland", 4, "gnome"),
	),
	(
		"Plasma",
		{"XDG_SESSION_TYPE": "wayland", "XDG_CURRENT_DESKTOP": "KDE", "WAYLAND_DISPLAY": "wayland-0"},
		("wayland-0",),
		("wayland", 4, "kde"),
	),
	(
		"COSMIC",
		{"XDG_SESSION_TYPE": "wayland", "XDG_CURRENT_DESKTOP": "COSMIC", "WAYLAND_DISPLAY": "wayland-1"},
		("wayland-1",),
		("wayland", 4, "cosmic"),
	),
	(
		"niri",
		{
			"XDG_CURRENT_DESKTOP": "niri",
			"NIRI_SOCKET": "/run/user/1000/niri.sock",
			"WAYLAND_DISPLAY": "wayland-1",
		},
		("wayland-1",),
		("wayland", 4, "niri"),
	),
	(
		"Hyprland",
		{
			"XDG_CURRENT_DESKTOP": "Hyprland",
			"HYPRLAND_INSTANCE_SIGNATURE": "abc",
			"WAYLAND_DISPLAY": "wayland-1",
		},
		("wayland-1",),
		("wayland", 4, "hyprland"),
	),
	(
		"session type wayland with DISPLAY alone",
		{"XDG_SESSION_TYPE": "wayland", "DISPLAY": ":0"},
		(),
		("wayland", 5, "other"),
	),
	("session type wayland alone", {"XDG_SESSION_TYPE": "wayland"}, (), ("wayland", 5, "other")),
	("gamescope's default children", GAMESCOPE_CHILD, ("gamescope-0",), ("x11", 3, "gamescope")),
	(
		"gamescope's children in a session registered as wayland",
		{**GAMESCOPE_CHILD, "XDG_SESSION_TYPE": "wayland"},
		("gamescope-0",),
		("x11", 3, "gamescope"),
	),
	(
		"gamescope's children with --expose-wayland",
		{**GAMESCOPE_CHILD, "WAYLAND_DISPLAY": "gamescope-0"},
		("gamescope-0",),
		("x11", 3, "gamescope"),
	),
	(
		"a unit with only DISPLAY and gamescope's desktop name",
		{"DISPLAY": ":1", "XDG_CURRENT_DESKTOP": "gamescope"},
		(),
		("x11", 3, "gamescope"),
	),
	(
		"a unit with only DISPLAY and gamescope's socket variable",
		{"DISPLAY": ":1", "GAMESCOPE_WAYLAND_DISPLAY": "gamescope-0"},
		(),
		("x11", 3, "gamescope"),
	),
	(
		"gamescope's marker and socket with no DISPLAY",
		{"GAMESCOPE_WAYLAND_DISPLAY": "gamescope-0", "WAYLAND_DISPLAY": "gamescope-0"},
		("gamescope-0",),
		("wayland", 4, "gamescope"),
	),
	(
		"the override over the owner's session",
		{**OWNER_SESSION, "LINDA_SESSION_TYPE": "wayland"},
		(),
		("wayland", 1, "other"),
	),
	(
		"the override over gamescope's children",
		{**GAMESCOPE_CHILD, "LINDA_SESSION_TYPE": "wayland"},
		("gamescope-0",),
		("wayland", 1, "gamescope"),
	),
	(
		"the override to x11 over GNOME with DISPLAY",
		{**GNOME, "DISPLAY": ":0", "LINDA_SESSION_TYPE": "x11"},
		("wayland-0",),
		("x11", 1, "gnome"),
	),
	(
		"the override to x11 with no DISPLAY",
		{**GNOME, "LINDA_SESSION_TYPE": "x11"},
		("wayland-0",),
		("none", 1, "gnome"),
	),
	(
		"an override that is neither x11 nor wayland",
		{**OWNER_SESSION, "LINDA_SESSION_TYPE": "X11"},
		(),
		("x11", 2, "other"),
	),
	("nothing", {}, (), ("none", 7, "other")),
	(
		"only empty values",
		{"DISPLAY": "", "WAYLAND_DISPLAY": "", "XDG_SESSION_TYPE": ""},
		(),
		("none", 7, "other"),
	),
]


@pytest.mark.parametrize(
	("env", "sockets", "expected"), [case[1:] for case in CASES], ids=[case[0] for case in CASES]
)
def test_eachSessionClassifiesAsWritten(session: Session, env, sockets, expected):
	(session.runtimeDir / "notASocket").write_text("", encoding="utf-8")
	for name in sockets:
		session.listen(name)
	assert session.classify(env) == expected
	assert session.ask(displayServer.isWayland) == (expected[0] == "wayland")
	assert session.ask(displayServer.reason).startswith(f"{expected[0]} by rule {expected[1]}: ")


def test_theOwnersSessionIsPinned(session: Session):
	assert session.classify(OWNER_SESSION) == ("x11", 2, "other")
	assert session.ask(displayServer.reason) == "x11 by rule 2: XDG_SESSION_TYPE is x11 and DISPLAY is set"
	assert session.ask(displayServer.environmentFacts) == OWNER_FACTS
	assert not session.ask(displayServer.saysWayland)
	assert not session.ask(displayServer.isGamescope)
	assert session.ask(displayServer.waylandSocket) is None


def test_theKindIsDecidedOnce(session: Session):
	session.set(OWNER_SESSION)
	assert session.ask(displayServer.kind) == "x11"
	session.change(displayServer.OVERRIDE_VARIABLE, "wayland")
	session.change("WAYLAND_DISPLAY", str(session.listen("wayland-0")))
	assert session.ask(displayServer.kind) == "x11"
	assert session.ask(displayServer.reason).startswith("x11 by rule 2: ")


def test_saysWaylandWhereTheKindOrTheSessionTypeDoes(session: Session):
	session.listen("wayland-0")
	session.listen("wayland-1")
	for env, kind, says in [
		(OWNER_SESSION, "x11", False),
		({"DISPLAY": ":99"}, "x11", False),
		(GAMESCOPE_CHILD, "x11", False),
		({**GNOME, "DISPLAY": ":0"}, "wayland", True),
		({**SWAY_FROM_CONSOLE, "DISPLAY": ":0"}, "wayland", True),
		({**GNOME, "DISPLAY": ":0", "LINDA_SESSION_TYPE": "x11"}, "x11", True),
		({**GAMESCOPE_CHILD, "XDG_SESSION_TYPE": "wayland"}, "x11", True),
	]:
		session.set(env)
		assert (session.ask(displayServer.kind), session.ask(displayServer.saysWayland)) == (kind, says), env


def test_theWaylandSocketIsFoundWhereLibwaylandLooks(session: Session, tmp_path: Path):
	socketPath = session.listen("wayland-0")
	session.set({"WAYLAND_DISPLAY": "wayland-0"})
	assert session.ask(displayServer.waylandSocket) == str(socketPath)
	session.set({"WAYLAND_DISPLAY": str(socketPath), "XDG_RUNTIME_DIR": None})
	assert session.ask(displayServer.waylandSocket) == str(socketPath)
	session.set({"WAYLAND_DISPLAY": "wayland-0", "XDG_RUNTIME_DIR": None})
	assert session.ask(displayServer.waylandSocket) is None
	session.set({"WAYLAND_DISPLAY": "wayland-9"})
	assert session.ask(displayServer.waylandSocket) is None
	(session.runtimeDir / "aFolder").mkdir()
	session.set({"WAYLAND_DISPLAY": "aFolder"})
	assert session.ask(displayServer.waylandSocket) is None
	# libwayland's fallback to wayland-0 is not taken.
	session.set({})
	assert session.ask(displayServer.waylandSocket) is None


def test_theReasonSaysAnOverrideWasIgnored(session: Session):
	session.classify({**OWNER_SESSION, "LINDA_SESSION_TYPE": "auto"})
	assert session.ask(displayServer.reason) == (
		"x11 by rule 2: XDG_SESSION_TYPE is x11 and DISPLAY is set; "
		"LINDA_SESSION_TYPE is 'auto', which is neither x11 nor wayland, and was ignored"
	)
	session.classify({**GNOME, "LINDA_SESSION_TYPE": "x11"})
	assert (
		session.ask(displayServer.reason)
		== "none by rule 1: LINDA_SESSION_TYPE is x11, but DISPLAY is not set"
	)


def test_theFactsAreTheFiveVariables(session: Session):
	session.set({**GAMESCOPE_CHILD, "XDG_SESSION_TYPE": "wayland", "WAYLAND_DISPLAY": ""})
	assert session.ask(displayServer.environmentFacts) == [
		"DISPLAY=:1",
		"WAYLAND_DISPLAY is not set.",
		"XDG_SESSION_TYPE=wayland",
		"XDG_CURRENT_DESKTOP is not set.",
		"GAMESCOPE_WAYLAND_DISPLAY=gamescope-0",
	]


def test_theModuleImportsOnlyOsAndFunctools():
	tree = ast.parse(MODULE_PATH.read_bytes(), str(MODULE_PATH))
	imported = set()
	for node in ast.walk(tree):
		if isinstance(node, ast.Import):
			imported.update(alias.name for alias in node.names)
		elif isinstance(node, ast.ImportFrom):
			imported.add(node.module or "")
		elif isinstance(node, ast.Name):
			assert node.id != "__import__", node.lineno
	assert imported == {"os", "functools"}


def test_loadingTheModuleMakesNoConnection(session: Session):
	"""Loading it afresh, under the same refusals, imports nothing that could connect."""
	before = set(sys.modules)

	def load():
		spec = importlib.util.spec_from_file_location("displayServerAgain", MODULE_PATH)
		module = importlib.util.module_from_spec(spec)
		spec.loader.exec_module(module)
		return module

	session.set(OWNER_SESSION)
	again = session.ask(load)
	assert session.ask(again.kind) == "x11"
	assert set(sys.modules) - before == set()
