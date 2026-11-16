"""Linux replacement for NVDAHelper.

Upstream's package loads nvdaHelperLocal.dll, injects nvdaHelperRemote into other processes, and
serves two RPC interfaces. Injected code calls the internal one; accessibility on Linux comes over
AT-SPI instead, so it has nothing to serve. Other programs call the controller interface:
nvdaController_speakText, speakSsml, cancelSpeech, brailleMessage and isSpeaking, which games and
tools use to make NVDA speak, and the internal routes that install an add-on package opened from
the file manager, open the configuration directory and handle a remote access URL. None of these
has a Linux counterpart here, so initialize() registers nothing and a program written against
NVDA's controller client cannot reach this copy.

Every other name of upstream's module is missing here; reaching for one raises
NoLinuxImplementation naming it.
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


def __getattr__(name: str) -> NoReturn:
	# The import system and other tools probe modules for optional names such as __path__ and
	# expect AttributeError when one is absent.
	if name.startswith("__"):
		raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
	raise NoLinuxImplementation(f"{__name__}.{name}")
