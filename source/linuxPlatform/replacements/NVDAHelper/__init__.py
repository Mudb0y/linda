"""Linux replacement for NVDAHelper, skeleton.

Upstream's package loads nvdaHelperLocal.dll, serves the in-process RPC interface that injected
code and controller clients call, and injects nvdaHelperRemote into other processes. Linux has
none of these: accessibility comes over AT-SPI, so there is nothing to start or stop.
"""

from typing import NoReturn  # noqa: I001

from linuxPlatform import NoLinuxImplementation

from . import localLib  # noqa: F401

# core.main imports NVDAHelper first after config.initialize(), and NVDA's import graph is one cycle
# that only resolves when entered where upstream's NVDAHelper enters it, through these, in this order.
import config  # noqa: F401
import eventHandler  # noqa: F401
import queueHandler  # noqa: F401
import api  # noqa: F401


def initialize() -> None:
	pass


def terminate() -> None:
	pass


def getHelperLocalWin10Dll() -> NoReturn:
	raise NoLinuxImplementation(
		"NVDAHelper.getHelperLocalWin10Dll",
		"NVDAHelper.getHelperLocalWin10Dll: nvdaHelperLocalWin10.dll exists only on Windows",
	)
