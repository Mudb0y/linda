"""Saving the configuration while drivers that failed to start are about, as Automatic leaves one
each time it tries a BRLTTY that is not there.

The driver base class registers the driver's settings to be saved before the driver has started. A
driver that failed is held only by the traceback of its failure, weakly by the registration, and
must be gone, or at least unregistered, once the failure has been dealt with: the collector, which
would free a driver caught in a cycle, is turned off here, so that what it would do by chance does
not hide what the code does.

nix develop . -c tests/linux/rig python3 -m pytest tests/linux/test_configSave.py
"""

import socket
from pathlib import Path

import harness
from test_brltty import SOCKET, socketDirectory  # noqa: F401
from test_inputAtRest import BRAILLE_DOWN, BRAILLE_UP, PRELUDE

#: What the code leaves in result about what is registered to be saved, and whether saving works.
SAVE_STATE = """
def saveState():
	owners = [type(getattr(h, "__self__", h)).__module__ for h in config.pre_configSave.handlers]
	try:
		config.conf.save()
		saved = "saved"
	except Exception as error:
		saved = f"{type(error).__name__}: {error}"
	return {"owners": owners, "saved": saved}
"""


def run(tmpPath: Path, code: str) -> harness.ChildResult:
	done = harness.runChild(
		tmpPath,
		PRELUDE + "\nimport gc\ngc.disable()\nimport config\n" + SAVE_STATE + code,
		env={"LINDA_KEYS": "off"},
	)
	assert done.error is None, done.error
	return done


def test_triesAtAMissingBrlttyLeaveNoDriverToSave(tmp_path: Path, socketDirectory: Path):  # noqa: F811
	# What a BRLTTY that was killed leaves: its socket, with nothing listening on it.
	with socket.socket(socket.AF_UNIX) as server:
		server.bind(str(SOCKET))
	done = run(
		tmp_path,
		'config.conf["braille"]["display"] = "noBraille"\n'
		+ BRAILLE_UP
		+ """
from brailleDisplayDrivers import brltty
from linuxPlatform import brlapiClient

# As the detection tries it every few seconds.
tries = [
	handler.setDisplayByName("brltty", isFallback=True, detected=brlapiClient.deviceMatch())
	for _ in range(5)
]
alive = sum(isinstance(o, brltty.BrailleDisplayDriver) for o in gc.get_objects())
result = {"tries": tries, "alive": alive, **saveState()}
"""
		+ BRAILLE_DOWN,
	)
	done.assertClean()
	assert done.value["tries"] == [False] * 5
	assert done.value["alive"] == 0
	assert "brailleDisplayDrivers.brltty" not in done.value["owners"]
	assert done.value["saved"] == "saved"


def test_brlttyDriverThatHasNotConnectedIsDescribed(tmp_path: Path):
	done = run(
		tmp_path,
		"""
from brailleDisplayDrivers import brltty

result = repr(brltty.BrailleDisplayDriver.__new__(brltty.BrailleDisplayDriver))
""",
	)
	done.assertClean()
	assert done.value == "BrailleDisplayDriver('brltty', numCells=0)"


def test_synthThatFailedToStartIsNotSaved(tmp_path: Path):
	done = run(
		tmp_path,
		"""
import synthDriverHandler


class SynthDriver(synthDriverHandler.SynthDriver):
	name = "failing"
	description = "fails to start"
	supportedSettings = ()

	def speak(self, speechSequence):
		pass

	def __init__(self):
		super().__init__()
		# As a driver that keeps a callback of its own does: a cycle only the collector would free.
		self._callback = self.cancel
		raise RuntimeError("no such synthesizer")


SynthDriver.__module__ = "failingSynth"
synthDriverHandler._getSynthDriver = lambda name: SynthDriver
try:
	synthDriverHandler.getSynthInstance("failing")
	raised = None
except RuntimeError as error:
	raised = str(error)
result = {"raised": raised, **saveState()}
""",
	)
	done.assertClean()
	assert done.value["raised"] == "no such synthesizer"
	assert "failingSynth" not in done.value["owners"]
	assert done.value["saved"] == "saved"


def test_saveGoesOnPastAFailingHandlerThatCannotBeDescribed(tmp_path: Path):
	done = run(
		tmp_path,
		"""
class HalfMade:
	def __repr__(self):
		raise AttributeError("not made yet")

	def saveSettings(self):
		raise KeyError("no section")


halfMade = HalfMade()
ran = []


def nextHandler():
	ran.append(True)


config.pre_configSave.register(halfMade.saveSettings)
config.pre_configSave.register(nextHandler)
result = {"ran": ran, **saveState()}
""",
	)
	done.assertClean(ignoring=("logErrors",))
	assert done.value["saved"] == "saved"
	assert done.value["ran"] == [True]
	assert any(
		"Error running handler <bound method saveSettings of HalfMade at 0x" in record
		for record in done.logRecords
	), done.logRecords
