"""Which speech an echo of typing may cut: only echoes before it, however late it comes and whatever was
given to speak around it. Run under the rig:

	nix develop . -c tests/linux/rig python3 -m pytest tests/linux/test_typedEchoOrder.py

Speech is stood in for as NVDA's own extension points see it: what is given to speak notifies
pre_speech, as speech.speak does, and a cancel notifies speechCanceled, as speech.cancelSpeech does.
"""

from pathlib import Path

import harness
import pytest

#: Enters NVDA's import cycle where core.main does, as test_inputAtRest does.
PRELUDE = """
import NVDAHelper  # noqa: F401
import languageHandler

languageHandler.setLanguage("en")
import speech  # noqa: E402
"""
#: Each step: "cancel", a key's interrupt or anything else that cancels speech; "say", a command's
#: speech, a message, a live region's or a notification, at whatever priority; "echo", the echo of a
#: typed character; "word", a character's echo that ends a word, which says the word and the character;
#: "deletion", what a deletion removed. The result is, for each echo, whether it cut speech first.
ORDER_CHILD = (
	PRELUDE
	+ r"""
from linuxPlatform.atspi import typedChars
from speech.extensions import pre_speech, speechCanceled
from speech.priorities import Spri
import types

import speech.sayAll

cuts = []


def cancel():
	cuts.append(True)
	speechCanceled.notify()


speech.cancelSpeech = cancel
speech.sayAll.SayAllHandler = types.SimpleNamespace(isRunning=lambda: SAY_ALL)


def say(*texts, priority=Spri.NORMAL):
	for text in texts:
		pre_speech.notify(speechSequence=[text], symbolLevel=None, priority=priority)


result = []
for step in STEPS:
	if step == "cancel":
		speechCanceled.notify()
	elif step == "say":
		say("Fixture window")
	elif step == "now":
		say("Alert", priority=Spri.NOW)
	else:
		before = len(cuts)
		speak = {
			"echo": lambda: say("q"),
			"word": lambda: say("quick", "space"),
			"deletion": lambda: say("q"),
			"silent": lambda: None,
		}[step]
		typedChars._echo(step == "deletion" or INTERRUPT, speak)
		result.append(len(cuts) > before)
"""
)


def order(tmp_path: Path, steps: list[str], *, interrupt: bool = True, sayAll: bool = False) -> list[bool]:
	code = f"STEPS = {steps!r}\nINTERRUPT = {interrupt!r}\nSAY_ALL = {sayAll!r}\n" + ORDER_CHILD
	done = harness.runChild(tmp_path, code, timeout=60)
	assert done.error is None, f"{done.error}\n{done.stderr[-4000:]}"
	return done.value


@pytest.mark.parametrize(
	("steps", "cuts"),
	[
		# The fault the review found: the command's speech came before the late echoes.
		(["cancel", "say", "echo", "echo"], [False, False]),
		# Between them, and after them, the command is not cut either; after it, the next echo neither.
		(["cancel", "echo", "say", "echo"], [False, False]),
		(["cancel", "echo", "echo", "say", "echo"], [False, True, False]),
		# A message at a higher priority, or a live region's, in a burst of typing.
		(["cancel", "echo", "now", "echo", "echo"], [False, False, False]),
		# Only echoes since the last cancel: each cuts the one before, the word's included.
		(["cancel", "echo", "echo", "word", "echo"], [False, True, True, True]),
		# A key's interrupt after the command: what comes after it is only echoes again.
		(["say", "cancel", "echo", "echo"], [False, True]),
		# What a deletion removed cuts the echo before it, and is cut by the next.
		(["cancel", "echo", "deletion", "echo"], [False, True, True]),
		(["cancel", "say", "deletion"], [False]),
		# An echo that said nothing, as a letter with only words echoed, is no reason to cut later.
		(["cancel", "silent", "echo"], [False, False]),
	],
)
def test_anEchoCutsOnlyEchoesBeforeIt(tmp_path: Path, steps: list[str], cuts: list[bool]):
	assert order(tmp_path, steps) == cuts


def test_noEchoCutsWithoutTheInterruptForCharacters(tmp_path: Path):
	assert order(tmp_path, ["cancel", "echo", "echo", "deletion"], interrupt=False) == [False, False, True]


def test_noEchoStopsSayAll(tmp_path: Path):
	# Say all begun, with nothing yet given to speak, after echoes only.
	assert order(tmp_path, ["cancel", "echo", "echo"], sayAll=True) == [False, False]


RAISING_CHILD = (
	PRELUDE
	+ r"""
from linuxPlatform.atspi import typedChars


def broken():
	raise RuntimeError("the echo failed")


try:
	typedChars._echo(True, broken)
except RuntimeError:
	pass
result = {"echoing": typedChars._echoing}
"""
)


def test_anEchoThatRaisesIsOver(tmp_path: Path):
	done = harness.runChild(tmp_path, RAISING_CHILD, timeout=60)
	assert done.error is None, f"{done.error}\n{done.stderr[-4000:]}"
	assert done.value == {"echoing": False}
