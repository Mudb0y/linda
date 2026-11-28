"""Which AT-SPI calls TextSource.setSelection makes.

VTE before 0.85 crashes when a GTK 4 terminal is sent AddSelection (VTE issue 2959), which the review
cursor's copy sends on its first press, so a GTK 4 terminal must only ever be sent SetSelection.
"""

from pathlib import Path

import harness
import pytest

#: Run under harness.runChild with TOOLKIT, ROLE and COUNT set: the calls setSelection(2, 5) makes.
SELECTION_CHILD = r"""
import gi

gi.require_version("Atspi", "2.0")
from gi.repository import Atspi

from linuxPlatform.atspi import textSource
from linuxPlatform.atspi.client import Node

calls = []


def fakeCall(node, func, *args):
	name = func.get_name()
	calls.append(name)
	if name == "get_n_selections":
		return COUNT
	if name == "get_role":
		return getattr(Atspi.Role, ROLE)
	# Refuse, as VTE's SetSelection does, so that a second call would show.
	return False


textSource._call = fakeCall
source = textSource.TextSource(Node("bus", "/node", object()), "codepoints", TOOLKIT)
source.setSelection(2, 5)
result = calls
"""


def _calls(tmpPath: Path, toolkit: str, role: str, count: int = 0) -> list[str]:
	code = f"TOOLKIT = {toolkit!r}\nROLE = {role!r}\nCOUNT = {count!r}\n" + SELECTION_CHILD
	done = harness.runChild(tmpPath, code, prepare="none", timeout=60)
	assert done.error is None, f"{done.error}\n{done.stderr[-4000:]}"
	return done.value


@pytest.mark.parametrize("count", [0, 1])
def test_gtk4TerminalIsNeverSentAddSelection(tmp_path: Path, count: int):
	calls = _calls(tmp_path, "GTK", "TERMINAL", count)
	assert "add_selection" not in calls, calls
	assert calls[-1] == "set_selection", calls


def test_gtk4TextViewStillGetsAddSelection(tmp_path: Path):
	assert _calls(tmp_path, "GTK", "TEXT") == [
		"get_n_selections",
		"get_role",
		"add_selection",
		"set_selection",
	]


def test_gtk3TerminalStillGetsAddSelection(tmp_path: Path):
	assert _calls(tmp_path, "gtk", "TERMINAL") == ["get_n_selections", "add_selection", "set_selection"]
