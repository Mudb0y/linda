"""The Linux platform layer under NVDA's upstream code.

Nothing in this package may be imported before linuxPlatform.bootstrap.install() has run,
apart from bootstrap itself and the exception defined here.
"""

import sys

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
