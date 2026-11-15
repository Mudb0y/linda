"""The one entry point that makes NVDA's upstream Python importable on Linux.

The launcher and every test runner call install() before importing any NVDA module.
"""

import builtins
import sys

_installed = False


class WindowsError(OSError):
	"""Windows' own OSError subclass.

	Upstream reads exc.winerror after `isinstance(exc, WindowsError)`. Linux raises no WindowsError,
	so that test is False for every Linux OSError rather than an AttributeError, as it would be if
	this were an alias of OSError.
	"""

	winerror: int | None = None


def install() -> None:
	"""Prepare the interpreter for NVDA's upstream modules. Safe to call more than once."""
	global _installed
	if _installed:
		return
	if sys.platform != "linux":
		raise RuntimeError(f"linuxPlatform is for Linux, not {sys.platform}")
	# liblouis's binding picks its loader by trying `from ctypes import WINFUNCTYPE, windll`,
	# so it must be imported before those names exist or it takes its Windows branch.
	import louis  # noqa: F401

	from . import windowsCtypes, finder

	windowsCtypes.install()
	if getattr(builtins, "WindowsError", WindowsError) is not WindowsError:
		raise RuntimeError("builtins.WindowsError already exists and is not the Linux one")
	builtins.WindowsError = WindowsError
	finder.install()
	_installed = True
