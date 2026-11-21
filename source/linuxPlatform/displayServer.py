"""Which kind of graphical session NVDA runs in, X11 or Wayland, decided from the environment alone.

Nothing here opens a connection: on GNOME the first X connection a client makes starts Xwayland, and a
Wayland connection would be the first of its kind in an X session. So only this module reads the
variables that name displays and session types to decide this, and everything else that needs the
answer asks it, which tests/linux/test_sessionSeam.py holds to.
"""

import functools
import os

#: Set to x11 or wayland, this decides, for tests and for sessions the rules below classify wrongly.
OVERRIDE_VARIABLE = "LINDA_SESSION_TYPE"

#: The variables the session report prints, in its order.
FACT_VARIABLES = (
	"DISPLAY",
	"WAYLAND_DISPLAY",
	"XDG_SESSION_TYPE",
	"XDG_CURRENT_DESKTOP",
	"GAMESCOPE_WAYLAND_DISPLAY",
)

# st_mode's file type bits, and their value for a socket, as the stat module would give them.
_FILE_TYPE_BITS = 0o170000
_SOCKET_TYPE = 0o140000


def _variable(name: str) -> str | None:
	"""The variable's value, with an empty one taken as unset, as libX11 and libwayland take it."""
	return os.environ.get(name) or None


def _desktops() -> list[str]:
	"""XDG_CURRENT_DESKTOP's names, casefolded: a list joined by colons, the session's own first."""
	return [name.casefold() for name in (_variable("XDG_CURRENT_DESKTOP") or "").split(":") if name]


def waylandSocket() -> str | None:
	"""The path of the socket WAYLAND_DISPLAY names, if a socket is there.

	libwayland resolves a relative name against XDG_RUNTIME_DIR and fails without it. Its fallback to
	wayland-0 when the variable is unset is not followed: a socket nobody named says nothing about
	this session.
	"""
	name = _variable("WAYLAND_DISPLAY")
	if name is None:
		return None
	if not os.path.isabs(name):
		runtimeDir = _variable("XDG_RUNTIME_DIR")
		if runtimeDir is None:
			return None
		name = os.path.join(runtimeDir, name)
	try:
		mode = os.stat(name).st_mode
	except OSError:
		return None
	return name if mode & _FILE_TYPE_BITS == _SOCKET_TYPE else None


def isGamescope() -> bool:
	"""Whether gamescope's marker is in the environment.

	gamescope sets GAMESCOPE_WAYLAND_DISPLAY for its children, and its session names itself in
	XDG_CURRENT_DESKTOP.
	"""
	return _variable("GAMESCOPE_WAYLAND_DISPLAY") is not None or "gamescope" in _desktops()


class _Decision:
	__slots__ = ("kind", "rule", "why")

	def __init__(self, kind: str, rule: int, why: str):
		self.kind = kind
		self.rule = rule
		self.why = why


@functools.cache
def _decision() -> _Decision:
	"""The first of the seven rules that applies.

	The order matters. Rule 2 comes before the socket, so that an X session whose environment names a
	nested or stray Wayland compositor stays on X. Rule 3 does too: gamescope's children talk to its
	Xwayland, and its Wayland socket is gamescope's own. Rule 4 comes before the session type, because
	a compositor started from a console has XDG_SESSION_TYPE=tty.
	"""
	display = _variable("DISPLAY")
	sessionType = _variable("XDG_SESSION_TYPE")
	override = _variable(OVERRIDE_VARIABLE)
	ignored = ""
	if override == "wayland":
		return _Decision("wayland", 1, f"{OVERRIDE_VARIABLE} is wayland")
	if override == "x11":
		if display is None:
			return _Decision("none", 1, f"{OVERRIDE_VARIABLE} is x11, but DISPLAY is not set")
		return _Decision("x11", 1, f"{OVERRIDE_VARIABLE} is x11")
	if override is not None:
		ignored = f"; {OVERRIDE_VARIABLE} is {override!r}, which is neither x11 nor wayland, and was ignored"
	if sessionType == "x11" and display is not None:
		return _Decision("x11", 2, f"XDG_SESSION_TYPE is x11 and DISPLAY is set{ignored}")
	if isGamescope() and display is not None:
		return _Decision("x11", 3, f"gamescope's marker is in the environment and DISPLAY is set{ignored}")
	if waylandSocket() is not None:
		return _Decision("wayland", 4, f"WAYLAND_DISPLAY names a socket that is there{ignored}")
	if sessionType == "wayland":
		return _Decision("wayland", 5, f"XDG_SESSION_TYPE is wayland, with no Wayland socket found{ignored}")
	if display is not None:
		return _Decision("x11", 6, f"DISPLAY is set{ignored}")
	return _Decision("none", 7, f"neither DISPLAY nor a Wayland socket is there{ignored}")


def kind() -> str:
	"""x11, wayland or none, decided once for the life of the process."""
	return _decision().kind


def isWayland() -> bool:
	return kind() == "wayland"


def saysWayland() -> bool:
	"""Whether the kind is wayland, or the session type says so even where the kind does not.

	For what must not run in a Wayland session whichever way its keys and windows go, such as a
	curtain or highlight drawn on an X server that is not the screen's.
	"""
	return isWayland() or _variable("XDG_SESSION_TYPE") == "wayland"


def reason() -> str:
	"""The kind and the rule that decided it, for one log line."""
	decision = _decision()
	return f"{decision.kind} by rule {decision.rule}: {decision.why}"


def compositor() -> str:
	"""gnome, kde, sway, cosmic, niri, hyprland, gamescope or other.

	This only chooses per desktop feeds and wording; it never decides between X11 and Wayland. A
	compositor's own socket variable comes before the desktop names, since it is set by the innermost
	compositor while XDG_CURRENT_DESKTOP may be inherited from the session around it.
	"""
	if isGamescope():
		return "gamescope"
	if _variable("HYPRLAND_INSTANCE_SIGNATURE") is not None:
		return "hyprland"
	if _variable("NIRI_SOCKET") is not None:
		return "niri"
	if _variable("SWAYSOCK") is not None:
		return "sway"
	desktops = _desktops()
	for name in ("gnome", "kde", "cosmic", "sway", "niri", "hyprland"):
		if name in desktops:
			return name
	return "other"


def environmentFacts() -> list[str]:
	"""The variables that decide the kind, one line each, as the session report prints them."""
	return [
		f"{name}={value}" if (value := _variable(name)) is not None else f"{name} is not set."
		for name in FACT_VARIABLES
	]
