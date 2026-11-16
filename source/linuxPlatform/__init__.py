"""The Linux platform layer under NVDA's upstream code.

Nothing in this package may be imported before linuxPlatform.bootstrap.install() has run,
apart from bootstrap itself and the exception defined here.
"""

import sys
from typing import NoReturn

#: The audit event raised with each NoLinuxImplementation, carrying what was called.
noLinuxImplementationEvent = "linuxPlatform.noLinuxImplementation"


class NoLinuxImplementation(NotImplementedError):
	"""Raised by every Windows function, COM call and replaced name that Linux does not implement.

	Upstream has many handlers that read NotImplementedError or RuntimeError as "this feature is
	absent" and carry on without logging. So each one raised is also announced through sys.audit,
	where a test can see it even when upstream swallowed the exception.
	"""

	def __init__(self, what: str, message: str | None = None):
		super().__init__(message or f"{what} has no Linux implementation")
		self.what = what
		sys.audit(noLinuxImplementationEvent, what)


class NoLinuxAttribute(NoLinuxImplementation, AttributeError):
	"""Raised when code reads a name that a Linux replacement of an upstream module lacks.

	Code written against upstream's module tests whether a name exists by catching AttributeError,
	or the ImportError that `from module import name` makes of it, and hasattr and getattr with a
	default do the same; add-on app modules import names this way inside such handlers. Those tests
	go the way they go on Windows when the name is absent, while the audit event still records the
	read and NotImplementedError handlers still read it as the feature being absent.
	"""

	def __init__(self, module: str, name: str, message: str | None = None):
		super().__init__(f"{module}.{name}", message)
		self.name = name


def missingAttribute(module: str, name: str, message: str | None = None) -> NoReturn:
	"""The body of a Linux replacement module's ``__getattr__``.

	Special names raise a plain AttributeError: the import system, copy, pickle and inspect probe
	every module for optional ones such as ``__path__``, and those probes are not missing features.
	"""
	if name.startswith("__"):
		raise AttributeError(f"module {module!r} has no attribute {name!r}", name=name)
	raise NoLinuxAttribute(module, name, message)
