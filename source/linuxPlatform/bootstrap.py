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
	this were an alias of OSError. Linux code that raises into such a handler raises this class.
	"""

	winerror: int | None = None

	def __init__(self, *args: object):
		super().__init__(*args)
		# OSError(errno, strerror, filename, winerror, filename2) keeps winerror only on Windows.
		if len(args) >= 4 and args[3] is not None:
			self.winerror = args[3]


def install() -> None:
	"""Prepare the interpreter for NVDA's upstream modules. Safe to call more than once."""
	global _installed
	if _installed:
		return
	if sys.platform != "linux":
		raise RuntimeError(f"linuxPlatform is for Linux, not {sys.platform}")
	# liblouis's binding picks its loader by trying `from ctypes import WINFUNCTYPE, windll`,
	# so it must be imported before those names exist or it takes its Windows branch.
	try:
		import louis  # noqa: F401
	except ImportError as e:
		# Failing here rather than carrying on without braille is deliberate: core.main imports the
		# braille package, which imports louis, so without it nothing starts, speech included.
		raise RuntimeError("liblouis's Python binding is missing, and NVDA cannot start without it") from e

	from . import finder, windowsCtypes

	# Every check that can refuse comes before the first change, so that a refusal a caller catches
	# leaves no Windows names in ctypes for upstream modules to bind against without the finder.
	windowsCtypes.check()
	if getattr(builtins, "WindowsError", WindowsError) is not WindowsError:
		raise RuntimeError("builtins.WindowsError already exists and is not the Linux one")
	platformFinder = finder.prepare()
	windowsCtypes.install()
	builtins.WindowsError = WindowsError
	finder.install(platformFinder)
	_installed = True
