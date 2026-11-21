"""The AT-SPI capture feed: where a document's keys go, kept from AT-SPI with no X server.

The logic tests drive the feed in a child with made-up window events, bus answers and times. Under the
X rig the feed follows marco's switches between GTK 3, GTK 4, Qt 6 and Firefox windows while the
child's main thread, NVDA's, is kept busy. Under a Wayland rig, headless Mutter, the feed hears new
windows become active with no other listener running. Run under the rig:

	nix develop . -c tests/linux/rig python3 -m pytest tests/linux/test_atspiCapture.py
"""

import ast
import json
import os
import textwrap
from pathlib import Path

import harness
from desktopSession import APPS

WAYLAND = os.environ.get("XDG_SESSION_TYPE") == "wayland"

#: Ahead of each logic test: a feed whose bus calls and timers are kept for the test to answer and
#: fire, three windows of two programs, and one of NVDA's own.
LOGIC = textwrap.dedent(
	"""\
	import os
	import time

	from linuxPlatform.keys import x11
	from linuxPlatform.keys.atspiCapture import (
		FOCUS_MOVE_GRACE,
		LOOK_ROUND_TRIP,
		ROOT_PATH,
		STATE_ACTIVE,
		STATE_SHOWING,
		STATE_VISIBLE,
		WINDOW_QUIET,
		AtspiCaptureFeed,
	)


	class Feed(AtspiCaptureFeed):
		def __init__(self, **kwargs):
			super().__init__(**kwargs)
			self.requests = []
			self.timers = []
			self.ended = []
			#: What the feed takes for now wherever it reads the clock itself.
			self.clock = t

		def _now(self):
			return self.clock

		def _request(self, kind, target, then):
			self.requests.append((kind, target, then))

		def _later(self, seconds, call):
			self.timers.append((seconds, call))

		def answer(self, kind, target, value):
			for index, (k, t, then) in enumerate(self.requests):
				if (k, t) == (kind, target):
					del self.requests[index]
					then(value)
					return
			raise AssertionError(f"no {kind} asked of {target}: {self.requests}")

		def asked(self, kind):
			return [t for k, t, _then in self.requests if k == kind]

		def fire(self):
			timers, self.timers = self.timers, []
			for _seconds, call in timers:
				call()

		def done(self, number, known):
			self.ended.append((number, known))


	def states(*numbers):
		words = [0, 0]
		for number in numbers:
			words[number // 32] |= 1 << (number % 32)
		return words


	FIRST = (":1.10", "/org/a11y/atspi/accessible/1")
	SECOND = (":1.10", "/org/a11y/atspi/accessible/2")
	OTHER = (":1.20", "/org/a11y/atspi/accessible/1")
	OWN = (":1.30", "/org/a11y/atspi/accessible/1")
	#: Past the quiet after a window event, as the feed's own arithmetic has it.
	QUIET = WINDOW_QUIET + 1e-6
	t = time.monotonic()


	def feedWithProcesses():
		feed = Feed()
		for busName, process in ((":1.10", 100), (":1.20", 200), (":1.30", os.getpid())):
			feed._processKnown(busName, process)
		return feed


	def anchoredOnFirst(t):
		feed = feedWithProcesses()
		feed._onWindowEvent(FIRST, True, t)
		feed.setCapture("browse", 100)
		return feed
	"""
)


def runLogic(tmp_path: Path, code: str) -> object:
	done = harness.runChild(tmp_path, LOGIC + textwrap.dedent(code), timeout=60)
	assert done.error is None, f"{done.error}\n{done.stderr[-4000:]}"
	return done.value


def test_focusAwayAndBack(tmp_path: Path) -> None:
	runLogic(
		tmp_path,
		"""\
		feed = anchoredOnFirst(t)
		s = feed.captureState
		assert s.window and s.windowProcess == 100 and s.focused, s
		assert (s.mode, s.dormant, s.documentUntil, s.processID, s.top) == ("browse", None, None, 100, 0), s
		anchor = s.window
		feed._onWindowEvent(FIRST, False, t + 1)
		feed._onWindowEvent(OTHER, True, t + 1.01)
		s = feed.captureState
		assert (s.window, s.focused, s.mode, s.dormant) == (anchor, False, "browse", "browse"), s
		# NVDA's focus follows to the other program, which has no document.
		feed.setCapture("off")
		s = feed.captureState
		assert (s.window, s.focused, s.mode, s.dormant, s.processID) == (anchor, False, "off", "browse", 100), s
		feed._onWindowEvent(OTHER, False, t + 2)
		feed._onWindowEvent(FIRST, True, t + 2.01)
		s = feed.captureState
		assert (s.window, s.focused, s.mode, s.dormant) == (anchor, True, "browse", None), s
		assert s.documentUntil == t + 2.01 + x11.DOCUMENT_GRACE, s
		assert any(seconds == x11.DOCUMENT_GRACE for seconds, _call in feed.timers), feed.timers
		# NVDA's focus is back in the document.
		feed.setCapture("browse", 100)
		s = feed.captureState
		assert (s.window, s.focused, s.mode, s.documentUntil) == (anchor, True, "browse", None), s
		""",
	)


def test_documentLetGoAfterTheGrace(tmp_path: Path) -> None:
	runLogic(
		tmp_path,
		"""\
		feed = anchoredOnFirst(t)
		feed._onWindowEvent(OTHER, True, t + 1)
		feed.setCapture("off")
		feed._onWindowEvent(FIRST, True, t + 2)
		until = feed.captureState.documentUntil
		assert until == t + 2 + x11.DOCUMENT_GRACE
		assert feed.captureNow(until - 0.01).mode == "browse"
		s = feed.captureNow(until)
		assert (s.window, s.focused, s.mode, s.documentUntil, s.processID) == (0, False, "off", None, 100), s
		feed._expireDocument(until)
		s = feed.captureState
		assert (s.window, s.focused, s.mode, s.dormant, s.documentUntil) == (0, False, "off", None, None), s
		""",
	)


def test_secondWindowOfTheSameProgram(tmp_path: Path) -> None:
	runLogic(
		tmp_path,
		"""\
		feed = anchoredOnFirst(t)
		anchor = feed.captureState.window
		feed._onWindowEvent(FIRST, False, t + 1)
		feed._onWindowEvent(SECOND, True, t + 1.01)
		s = feed.captureState
		assert (s.window, s.focused, s.dormant) == (anchor, False, "browse"), s
		# NVDA's focus is in a document of the second window: its keys are captured there.
		feed.setCapture("browse", 100)
		s = feed.captureState
		assert s.window and s.window != anchor and s.focused and s.windowProcess == 100, s
		second = s.window
		feed._onWindowEvent(SECOND, False, t + 2)
		feed._onWindowEvent(FIRST, True, t + 2.01)
		assert not feed.captureState.focused
		# NVDA has not followed yet and says nothing of a document: the anchor stays.
		feed.setCapture("off")
		assert (feed.captureState.window, feed.captureState.mode) == (second, "off")
		""",
	)


def test_anotherProgramsWindow(tmp_path: Path) -> None:
	runLogic(
		tmp_path,
		"""\
		feed = anchoredOnFirst(t)
		anchor = feed.captureState.window
		feed._onWindowEvent(FIRST, False, t + 1)
		feed._onWindowEvent(OTHER, True, t + 1.01)
		# NVDA's mode is still the first program's document's: no reason to take the other's keys.
		feed.setCapture("browse", 100)
		s = feed.captureState
		assert (s.window, s.focused, s.mode, s.dormant) == (anchor, False, "browse", "browse"), s
		# NVDA's focus reached a document of the other program.
		feed.setCapture("browse", 200)
		s = feed.captureState
		assert s.window not in (0, anchor) and s.focused and s.windowProcess == 200 and s.processID == 200, s
		""",
	)


def test_foregroundProgramGoes(tmp_path: Path) -> None:
	runLogic(
		tmp_path,
		"""\
		feed = anchoredOnFirst(t)
		feed.noteFocusMayMove(t + 1)
		feed.requestLook(1, t + 1.01, feed.done)
		feed._takeLooks(t + 1.01)
		feed._applicationGone(":1.10", t + 1.05)
		s = feed.captureState
		assert (s.window, s.windowProcess, s.focused) == (0, None, False), s
		assert feed.foreground is None
		feed._serviceLooks(t + 1.05 + QUIET)
		assert feed.ended == [(1, True)], feed.ended
		# Its bus name may be given to another program later, whose process is asked anew.
		feed._onWindowEvent(FIRST, True, t + 2)
		assert feed.asked("process") == [":1.10"], feed.requests
		""",
	)


def test_programEndHeardFromTheBus(tmp_path: Path) -> None:
	runLogic(
		tmp_path,
		"""\
		from gi.repository import GLib


		def ownerChanged(name, old, new):
			feed._onNameOwnerChanged(
				None,
				"org.freedesktop.DBus",
				"/org/freedesktop/DBus",
				"org.freedesktop.DBus",
				"NameOwnerChanged",
				GLib.Variant("(sss)", (name, old, new)),
			)


		feed = anchoredOnFirst(t)
		# A well-known name changing hands, or a program coming, says nothing of the foreground.
		ownerChanged("org.a11y.atspi.Registry", ":1.10", "")
		ownerChanged(":1.10", "", ":1.10")
		assert feed.foreground[:2] == FIRST and feed.captureState.focused, feed.foreground
		ownerChanged(":1.10", ":1.10", "")
		s = feed.captureState
		assert (s.window, s.focused) == (0, False), s
		assert feed.foreground is None
		""",
	)


def test_foregroundWindowGoes(tmp_path: Path) -> None:
	runLogic(
		tmp_path,
		"""\
		feed = anchoredOnFirst(t)
		anchor = feed.captureState.window
		# GTK 4 says a window that has just shown is not showing, so its state is asked.
		feed._onStateChanged(FIRST, "showing", 0, t + 1)
		feed.answer("state", FIRST, states(STATE_ACTIVE, STATE_SHOWING, STATE_VISIBLE))
		assert feed.captureState.focused
		feed._onStateChanged(FIRST, "visible", 0, t + 2)
		feed.answer("state", FIRST, states(STATE_ACTIVE, STATE_SHOWING))
		s = feed.captureState
		assert (s.window, s.focused, s.dormant) == (anchor, False, "browse"), s
		assert feed.foreground is None
		feed._onWindowEvent(FIRST, True, t + 3)
		assert feed.captureState.focused
		feed._onStateChanged(FIRST, "defunct", 1, t + 4)
		s = feed.captureState
		assert (s.window, s.focused) == (0, False), s
		assert feed.foreground is None
		""",
	)


def test_windowLeftSaysItIsActiveLate(tmp_path: Path) -> None:
	runLogic(
		tmp_path,
		"""\
		feed = anchoredOnFirst(t)
		feed._onWindowEvent(FIRST, False, t + 1)
		feed._onWindowEvent(OTHER, True, t + 1.001)
		# Qt, a tenth of a second after the window that took the keyboard from it said so.
		feed._onWindowEvent(FIRST, True, t + 1.1)
		feed._onWindowEvent(FIRST, False, t + 1.101)
		assert feed.foreground[:2] == OTHER, feed.foreground
		assert not feed.captureState.focused
		feed.answer("state", OTHER, states(STATE_ACTIVE, STATE_SHOWING, STATE_VISIBLE))
		assert feed.foreground[:2] == OTHER
		# A window that said it was active and went quiet is asked before it is believed.
		feed._onWindowEvent(FIRST, True, t + 2)
		feed._onWindowEvent(OTHER, True, t + 2.5)
		feed._onWindowEvent(OTHER, False, t + 3)
		assert feed.foreground[:2] == FIRST and feed.captureState.focused
		feed.answer("state", FIRST, states(STATE_SHOWING, STATE_VISIBLE))
		assert feed.foreground is None and not feed.captureState.focused
		""",
	)


def test_ownWindowIsNeverTheAnchor(tmp_path: Path) -> None:
	runLogic(
		tmp_path,
		"""\
		feed = feedWithProcesses()
		feed._onWindowEvent(OWN, True, t)
		feed.setCapture("browse", 100)
		s = feed.captureState
		assert (s.window, s.focused, s.mode) == (0, False, "browse"), s
		assert feed.foreground == (*OWN, os.getpid())
		""",
	)


def test_noAnchorBeforeTheProcessIsKnown(tmp_path: Path) -> None:
	runLogic(
		tmp_path,
		"""\
		feed = Feed()
		feed._onWindowEvent(OTHER, True, t)
		assert feed.asked("process") == [":1.20"], feed.requests
		feed.setCapture("browse", 200)
		assert feed.captureState.window == 0
		feed.answer("process", ":1.20", 200)
		feed.setCapture("browse", 200)
		s = feed.captureState
		assert s.window and s.focused and s.windowProcess == 200, s
		""",
	)


def test_lookEndsUnknownAtTheGrace(tmp_path: Path) -> None:
	runLogic(
		tmp_path,
		"""\
		assert FOCUS_MOVE_GRACE == 0.3
		assert FOCUS_MOVE_GRACE + x11.CAPTURE_TIMEOUT <= x11.DOCUMENT_WAIT
		feed = anchoredOnFirst(t)
		assert feed.settled(t + 1)
		feed.noteFocusMayMove(t + 1)
		assert not feed.settled(t + 1.01)
		deadline = feed.requestLook(1, t + 1.01, feed.done)
		assert deadline == t + 1 + FOCUS_MOVE_GRACE, deadline
		feed._takeLooks(t + 1.01)
		assert not feed.asked("roundTrip"), feed.requests
		feed._serviceLooks(t + 1.29)
		assert feed.ended == []
		feed._serviceLooks(t + 1 + FOCUS_MOVE_GRACE)
		assert feed.ended == [(1, False)], feed.ended
		assert feed.settled(t + 1 + FOCUS_MOVE_GRACE)
		# Nothing moved: the state is what it was.
		assert feed.captureState.focused
		""",
	)


def test_lookEndsKnownAtTheWindowEvent(tmp_path: Path) -> None:
	runLogic(
		tmp_path,
		"""\
		feed = anchoredOnFirst(t)
		feed._onWindowEvent(FIRST, False, t + 1)
		feed._onWindowEvent(OTHER, True, t + 1.01)
		# Alt+Tab back to the document, and a letter typed at once.
		feed.noteFocusMayMove(t + 2)
		deadline = feed.requestLook(1, t + 2.01, feed.done)
		feed._takeLooks(t + 2.01)
		feed._onWindowEvent(OTHER, False, t + 2.1)
		feed._serviceLooks(t + 2.1)
		feed._onWindowEvent(FIRST, True, t + 2.11)
		feed._serviceLooks(t + 2.11 + WINDOW_QUIET - 0.001)
		assert feed.ended == [], "ended before the switch settled"
		feed._serviceLooks(t + 2.11 + QUIET)
		assert feed.ended == [(1, True)], feed.ended
		assert feed.captureState.focused
		assert t + 2.11 + QUIET < deadline
		# A look asked once a window event has come takes a round trip.
		feed.requestLook(2, t + 2.2, feed.done)
		feed._takeLooks(t + 2.2)
		assert feed.asked("roundTrip") == [None], feed.requests
		""",
	)


def test_settledLookTakesOneRoundTrip(tmp_path: Path) -> None:
	runLogic(
		tmp_path,
		"""\
		feed = anchoredOnFirst(t)
		deadline = feed.requestLook(1, t + 1, feed.done)
		assert deadline == t + 1 + LOOK_ROUND_TRIP
		feed._takeLooks(t + 1)
		feed._serviceLooks(t + 1.01)
		assert feed.ended == []
		feed.clock = t + 1.02
		feed.answer("roundTrip", None, True)
		assert feed.ended == [(1, True)], feed.ended
		# A read that may move the foreground holds a look that is otherwise done.
		feed._onStateChanged(FIRST, "showing", 0, t + 2)
		deadline = feed.requestLook(2, t + 3, feed.done)
		feed._takeLooks(t + 3)
		feed.clock = t + 3.005
		feed.answer("roundTrip", None, True)
		assert feed.ended == [(1, True)], feed.ended
		feed.answer("state", FIRST, states(STATE_ACTIVE, STATE_SHOWING, STATE_VISIBLE))
		feed._serviceLooks(t + 3.01)
		assert feed.ended == [(1, True), (2, True)], feed.ended
		# A round trip that does not come back in time ends the look unknown.
		deadline = feed.requestLook(3, t + 4, feed.done)
		feed._takeLooks(t + 4)
		feed._serviceLooks(deadline)
		assert feed.ended[-1] == (3, False), feed.ended
		""",
	)


def test_readsThatCannotMoveTheForegroundHoldNoLook(tmp_path: Path) -> None:
	runLogic(
		tmp_path,
		"""\
		feed = anchoredOnFirst(t)
		# Another program, starting up or stuck, is slow to say what its widget that has shown is.
		widget = (":1.20", "/org/a11y/atspi/accessible/9")
		feed._onStateChanged(widget, "showing", 1, t + 1)
		feed.requestLook(1, t + 1.01, feed.done)
		feed._takeLooks(t + 1.01)
		feed.clock = t + 1.02
		feed.answer("roundTrip", None, True)
		assert feed.ended == [(1, True)], feed.ended
		# It is a window, asked a while later whether it has become active without saying so.
		feed.answer("parent", widget, ROOT_PATH)
		feed.fire()
		assert feed.asked("state") == [widget], feed.requests
		feed.requestLook(2, t + 1.2, feed.done)
		feed._takeLooks(t + 1.2)
		feed.clock = t + 1.21
		feed.answer("roundTrip", None, True)
		assert feed.ended == [(1, True), (2, True)], feed.ended
		assert feed.captureState.focused
		# Whether an object that says it is active is a window can move the foreground now.
		cell = (":1.20", "/org/a11y/atspi/accessible/12")
		feed._onStateChanged(cell, "active", 1, t + 2)
		feed.requestLook(3, t + 2 + QUIET, feed.done)
		feed._takeLooks(t + 2 + QUIET)
		feed.clock = t + 2 + QUIET + 0.005
		feed.answer("roundTrip", None, True)
		assert feed.ended[-1][0] == 2, feed.ended
		feed.answer("parent", cell, "/org/a11y/atspi/accessible/11")
		feed._serviceLooks(t + 2 + QUIET + 0.01)
		assert feed.ended[-1] == (3, True), feed.ended
		""",
	)


def test_oneWindowIsAskedAboutOnce(tmp_path: Path) -> None:
	runLogic(
		tmp_path,
		"""\
		feed = anchoredOnFirst(t)
		# GTK 4 says a frame is showing and visible as it maps.
		new = (":1.20", "/org/a11y/atspi/accessible/5")
		feed._onStateChanged(new, "showing", 0, t + 1)
		feed._onStateChanged(new, "visible", 0, t + 1)
		assert feed.asked("parent") == [new], feed.requests
		feed.answer("parent", new, ROOT_PATH)
		assert len(feed.timers) == 1, feed.timers
		# A window that shows and says it is active before its parent is known is asked about once, and
		# the look waits for the answer.
		other = (":1.20", "/org/a11y/atspi/accessible/6")
		feed._onStateChanged(other, "showing", 1, t + 2)
		feed._onStateChanged(other, "active", 1, t + 2)
		assert feed.asked("parent") == [other], feed.requests
		feed.requestLook(1, t + 2 + QUIET, feed.done)
		feed._takeLooks(t + 2 + QUIET)
		feed.clock = t + 2 + QUIET + 0.005
		feed.answer("roundTrip", None, True)
		assert feed.ended == [], feed.ended
		feed.answer("parent", other, ROOT_PATH)
		assert feed.foreground[:2] == other and not feed.captureState.focused, feed.foreground
		feed._serviceLooks(t + 2 + 2 * QUIET + 0.005)
		assert feed.ended == [(1, True)], feed.ended
		assert feed._reading == 0
		""",
	)


def test_newWindowThatSaysNothing(tmp_path: Path) -> None:
	runLogic(
		tmp_path,
		"""\
		new = (":1.40", "/org/linda/Probe/a11y/1")
		feed = feedWithProcesses()
		feed._onWindowEvent(OTHER, True, t)
		# GTK 4 at times sends only this for a window that then becomes active.
		feed._onStateChanged(new, "showing", 0, t + 1)
		feed.answer("parent", new, ROOT_PATH)
		assert len(feed.timers) == 1, feed.timers
		feed.fire()
		feed.answer("state", new, states(STATE_SHOWING, STATE_VISIBLE))
		feed.fire()
		feed.answer("state", new, states(STATE_ACTIVE, STATE_SHOWING, STATE_VISIBLE))
		assert feed.foreground == (*new, None), feed.foreground
		assert feed.asked("process") == [":1.40"], feed.requests
		assert feed.timers == []
		# A window event after it showed says where the keyboard is; the new window is not asked again.
		later = (":1.50", "/org/linda/Probe/a11y/1")
		feed._onStateChanged(later, "showing", 1, t + 2)
		feed.answer("parent", later, ROOT_PATH)
		feed._onWindowEvent(OTHER, True, t + 2.05)
		feed.fire()
		assert not feed.asked("state"), feed.requests
		assert feed.foreground[:2] == OTHER
		""",
	)


def test_windowActiveFourSecondsAfterItShowedIsFound(tmp_path: Path) -> None:
	assert runLogic(
		tmp_path,
		"""\
		new = (":1.40", "/org/linda/Probe/a11y/1")
		never = (":1.50", "/org/linda/Probe/a11y/1")


		def readUntil(window, activeAfter):
			\"\"\"Fire each read the feed asks for, at its time, until it has no more; the window says it is
			active from activeAfter seconds after it showed.\"\"\"
			shown = feed.clock = t + 1
			feed._onStateChanged(window, "showing", 1, shown)
			feed.answer("parent", window, ROOT_PATH)
			reads = []
			while feed.timers:
				((seconds, _call),) = feed.timers
				feed.clock += seconds
				reads.append(round(feed.clock - shown, 3))
				feed.fire()
				active = feed.clock - shown >= activeAfter
				feed.answer("state", window, states(STATE_SHOWING, STATE_VISIBLE, *([STATE_ACTIVE] if active else [])))
			return reads


		feed = feedWithProcesses()
		feed._onWindowEvent(OTHER, True, t)
		# E3 measured GTK 4 giving a new window the keyboard 4.1 s after it mapped, saying nothing.
		found = readUntil(new, 4.1)
		assert feed.foreground[:2] == new, feed.foreground
		feed = feedWithProcesses()
		feed._onWindowEvent(OTHER, True, t)
		givenUp = readUntil(never, 60)
		assert feed.foreground[:2] == OTHER and not feed._asking, (feed.foreground, feed._asking)
		result = [found, givenUp]
		""",
	) == [[0.1, 0.5, 1.0, 2.0, 3.0, 4.5], [0.1, 0.5, 1.0, 2.0, 3.0, 4.5, 6.0]]


def test_newWindowReadsAreTheFocusHandlers() -> None:
	"""The feed's reads of a window that has shown are the focus handler's, read from both files' source,
	as neither module imports the other."""

	def constant(path: Path, name: str) -> object:
		for node in ast.parse(path.read_text(encoding="utf-8")).body:
			if isinstance(node, ast.Assign) and [target.id for target in node.targets] == [name]:
				return ast.literal_eval(node.value)
		raise AssertionError(f"no {name} in {path}")

	platform = harness.SOURCE / "linuxPlatform"
	reads = constant(platform / "keys" / "atspiCapture.py", "NEW_WINDOW_READS")
	assert reads == constant(platform / "atspi" / "handler.py", "SHOWN_PROBE_SECONDS")
	assert max(reads) > 4.1, reads


def test_activeStateCountsOnlyForTopLevels(tmp_path: Path) -> None:
	runLogic(
		tmp_path,
		"""\
		cell = (":1.20", "/org/a11y/atspi/accessible/77")
		feed = anchoredOnFirst(t)
		feed._onStateChanged(cell, "active", 1, t + 1)
		feed.answer("parent", cell, "/org/a11y/atspi/accessible/76")
		assert feed.foreground[:2] == FIRST
		feed._onStateChanged(cell, "active", 1, t + 2)
		assert not feed.requests, "a cell was asked about twice"
		# GTK can give a window back the keyboard, as a dialog over it closes, with only this.
		feed._onStateChanged(OTHER, "active", 1, t + 3)
		feed.answer("parent", OTHER, ROOT_PATH)
		assert feed.foreground[:2] == OTHER and not feed.captureState.focused
		feed._onStateChanged(OTHER, "active", 0, t + 4)
		feed._onStateChanged(FIRST, "active", 1, t + 4.01)
		assert feed.captureState.focused
		""",
	)


#: The child of the tests of start: a feed on a GLib thread of its own, as the real one, with a made-up
#: connection, so that no bus is needed, whose thread takes ENDING seconds to let its connection go
#: once it has stopped listening, as closing a real one may. LOOKED says whether a look asked now ends.
STARTS = textwrap.dedent(
	"""\
	import logging
	import threading
	import time

	from logHandler import log
	from linuxPlatform.keys.atspiCapture import STOP_TIMEOUT, AtspiCaptureFeed


	class Feed(AtspiCaptureFeed):
		def __init__(self, connects):
			super().__init__()
			self.connects = list(connects)
			self.ending = 1.0

		def _connect(self):
			from gi.repository import GLib

			if not self.connects.pop(0):
				return False
			channel = GLib.IOChannel.unix_new(self._wakeRead)
			watch = GLib.io_create_watch(channel, GLib.IOCondition.IN)
			watch.set_callback(self._onWake)
			watch.attach(self._context)
			return True

		def _disconnect(self):
			time.sleep(self.ending)
			super()._disconnect()


	class Warnings(logging.Handler):
		def __init__(self):
			super().__init__(logging.WARNING)
			self.lines = []

		def emit(self, record):
			self.lines.append(record.getMessage())


	warnings = Warnings()
	log.addHandler(warnings)


	def looked(feed):
		ended = threading.Event()
		feed.requestLook(1, time.monotonic(), lambda _number, _known: ended.set())
		return ended.wait(2)


	def closeUnder(feed):
		feed._later(0, lambda: feed._onClosed(None, True, None))
		closed = time.monotonic()
		while feed.canLook:
			assert time.monotonic() - closed < 2, "the closing was never followed"
			time.sleep(0.001)
	"""
)


def runStarts(tmp_path: Path, code: str) -> object:
	return runLogic(tmp_path, STARTS + textwrap.dedent(code))


def test_startStraightAfterAStartThatFoundNoBus(tmp_path: Path) -> None:
	assert runStarts(
		tmp_path,
		"""\
		feed = Feed([False, True])
		feed.start()
		assert not feed.canLook
		# Its thread is still letting go as start returns, and this start must not be lost to it.
		feed.start()
		listening = feed.canLook
		result = [listening, looked(feed)]
		feed.stop()
		""",
	) == [True, True]


def test_startStraightAfterTheConnectionClosed(tmp_path: Path) -> None:
	assert runStarts(
		tmp_path,
		"""\
		feed = Feed([True, True])
		feed.start()
		assert feed.canLook and looked(feed)
		closeUnder(feed)
		feed.start()
		listening = feed.canLook
		result = [listening, looked(feed)]
		feed.stop()
		""",
	) == [True, True]


def test_startSaysWhenTheThreadBeforeDoesNotEnd(tmp_path: Path) -> None:
	value = runStarts(
		tmp_path,
		"""\
		feed = Feed([True, True])
		feed.ending = STOP_TIMEOUT + 1
		feed.start()
		closeUnder(feed)
		warnings.lines.clear()
		started = time.monotonic()
		refused = feed.start()
		took = time.monotonic() - started
		said = warnings.lines[:]
		# The thread ends in its own time, and the next start finds it gone.
		while feed._thread.is_alive():
			time.sleep(0.01)
		feed.ending = 0
		again = feed.start()
		result = {"refused": refused, "took": took, "said": said, "again": [again, feed.canLook, looked(feed)]}
		feed.stop()
		""",
	)
	assert value["refused"] is False, value
	assert value["said"] == [
		"AT-SPI capture feed: its connection before has not ended, so browse mode's keys are not captured"
	], value
	assert value["took"] < 2.0 + 0.5, value
	assert value["again"] == [True, True, True], value


#: The child of the alternation test: the feed started, the anchor set in each window in turn while it
#: is the foreground, and the keyboard moved between it and the next window by marco, each move checked
#: 200 ms after the X server says it happened, while the main thread computes all the while.
ALTERNATION = textwrap.dedent(
	"""\
	import subprocess
	import threading
	import time

	from linuxPlatform.keys.atspiCapture import AtspiCaptureFeed

	ALTERNATIONS = 100
	CHECK_AFTER = 0.2


	def active():
		\"\"\"The X server's active window, by its title and its process.\"\"\"
		shown = subprocess.run(
			["xdotool", "getactivewindow", "getwindowpid", "getwindowname"],
			capture_output=True,
			text=True,
			check=False,
		).stdout.splitlines()
		if len(shown) != 2 or not shown[0].isdecimal():
			return None, None
		return shown[1], int(shown[0])


	def activate(title):
		\"\"\"Have marco give title the keyboard, and say when the X server had it there, and its process.\"\"\"
		deadline = time.monotonic() + 10
		while True:
			subprocess.run(["wmctrl", "-F", "-a", title], capture_output=True, check=False)
			for _ in range(20):
				name, pid = active()
				if name == title:
					return time.monotonic(), pid
				time.sleep(0.01)
			assert time.monotonic() < deadline, f"{title} did not become active"


	def awaitForeground(pid, timeout=5):
		deadline = time.monotonic() + timeout
		while (feed.foreground or (None, None, None))[2] != pid:
			assert time.monotonic() < deadline, f"the feed never heard process {pid} come to the front"
			time.sleep(0.005)


	PIDS = {}
	feed = AtspiCaptureFeed()
	feed.start()
	assert feed.canLook
	results = {"wrong": [], "lags": []}


	def drive():
		try:
			for title in TITLES:
				_at, PIDS[title] = activate(title)
			for alternation in range(ALTERNATIONS):
				block, step = divmod(alternation, ALTERNATIONS // len(TITLES))
				anchor = TITLES[block]
				other = TITLES[(block + 1) % len(TITLES)]
				if step == 0:
					activate(anchor)
					awaitForeground(PIDS[anchor])
					feed.setCapture("browse", PIDS[anchor])
					window = feed.captureState.window
					assert window and feed.captureState.focused, feed.captureState
				target = other if step % 2 == 0 else anchor
				switched, pid = activate(target)
				while (feed.foreground or (None, None, None))[2] != pid and time.monotonic() < switched + CHECK_AFTER:
					time.sleep(0.002)
				results["lags"].append(time.monotonic() - switched)
				time.sleep(max(0.0, switched + CHECK_AFTER - time.monotonic()))
				state = feed.captureState
				seen = (feed.foreground or (None, None, None))[2]
				if (state.focused, state.window, seen) != (target == anchor, window, pid):
					results["wrong"].append([alternation, target, state.focused, state.window, window, seen, pid])
		except BaseException as e:
			results["error"] = repr(e)
		finally:
			results["finished"] = True


	driver = threading.Thread(target=drive)
	driver.start()
	busy = 0
	while not results.get("finished"):
		sum(i * i for i in range(20000))
		busy += 1
	driver.join()
	feed.stop()
	results["busyRounds"] = busy
	result = results
	"""
)

if not WAYLAND:
	import webSession
	from desktopSession import DesktopSession

	def test_followsAlternationsWhileTheMainThreadIsBusy(tmp_path: Path) -> None:
		with DesktopSession(tmp_path / "desktop") as desktop:
			desktop.startApp(desktop.fixture("gtkFixture"), title="Fixture window")
			desktop.startApp(desktop.fixture("gtk4"), title="GTK4 fixture")
			desktop.startApp(desktop.fixture("qtFixture"), title="Qt fixture window")
			firefox = webSession.startFirefox(desktop, "basic.html")
			titles = ["Fixture window", "GTK4 fixture", "Qt fixture window", firefox.window]
			done = harness.runChild(tmp_path, f"TITLES = {json.dumps(titles)}\n" + ALTERNATION, timeout=600)
		assert done.error is None, f"{done.error}\n{done.stderr[-4000:]}"
		results = done.value
		assert "error" not in results, results
		lags = sorted(results["lags"])
		assert len(lags) == 100, len(lags)
		assert results["wrong"] == [], f"{results['wrong']}; lags {lags[len(lags) // 2]:.3f} {lags[-1]:.3f}"
		assert results["busyRounds"] > 0, "the main thread was never busy"
		print(
			f"feed lag after the X server: median {lags[50] * 1000:.0f} ms, worst {lags[-1] * 1000:.0f} ms, "
			f"main thread busy for {results['busyRounds']} rounds"
		)


#: Ahead of the children that start the feed again: waiting for it to find the window of process PID,
#: and a look asked as the kernel source asks one.
AGAIN = textwrap.dedent(
	"""\
	import os
	import signal
	import threading
	import time

	from linuxPlatform.keys.atspiCapture import AtspiCaptureFeed


	def awaitForeground(feed, timeout=5):
		started = time.monotonic()
		while (feed.foreground or (None, None, None))[2] != PID:
			assert time.monotonic() - started < timeout, f"the active window was never found: {feed.foreground}"
			time.sleep(0.005)
		return time.monotonic() - started


	def look(feed, number):
		ended = []
		heard = threading.Event()

		def done(n, known):
			ended.append(known)
			heard.set()

		asked = time.monotonic()
		feed.requestLook(number, asked, done)
		assert heard.wait(2), "the look never ended"
		return [ended[0], time.monotonic() - asked]
	"""
)

#: The child of the restart test: the feed started while a window already has the keyboard, stopped
#: while a read is out to that window's program, which the test has paused, and started again.
RESTART = textwrap.dedent(
	"""\
	feed = AtspiCaptureFeed()
	feed.start()
	assert feed.canLook
	# The window had the keyboard before the feed started, so only its scan at the start can say so.
	found = awaitForeground(feed)
	time.sleep(0.1)
	before = look(feed, 1)
	wake = feed._wakeWrite
	feed.stop()
	# A look asked as the feed stops writes to the pipe it always had, never to a descriptor reused.
	pipeKept = feed._wakeWrite == wake and os.get_blocking(wake) is False
	os.kill(PID, signal.SIGSTOP)
	try:
		feed.start()
		time.sleep(0.3)
		inFlight = feed._reading
		feed.stop()
	finally:
		os.kill(PID, signal.SIGCONT)
	feed.start()
	refound = awaitForeground(feed)
	time.sleep(0.1)
	after = look(feed, 2)
	feed.stop()
	pipeKept = pipeKept and feed._wakeWrite == wake
	result = {
		"found": found,
		"before": before,
		"pipeKept": pipeKept,
		"inFlight": inFlight,
		"refound": refound,
		"after": after,
	}
	"""
)

#: The child of the lost bus test: a feed whose first connection fails, and whose second is closed
#: under it, started again each time.
BUS_LOST = textwrap.dedent(
	"""\
	class Feed(AtspiCaptureFeed):
		tries = 0

		def _connect(self):
			self.tries += 1
			return self.tries > 1 and super()._connect()


	feed = Feed()
	feed.start()
	failed = feed.canLook
	feed.start()
	retried = feed.canLook
	awaitForeground(feed)
	feed._connection.close_sync(None)
	closed = time.monotonic()
	while feed.canLook:
		assert time.monotonic() - closed < 2, "the closed connection was never noticed"
		time.sleep(0.005)
	noticed = time.monotonic() - closed
	feed.start()
	again = feed.canLook
	awaitForeground(feed)
	time.sleep(0.1)
	after = look(feed, 1)
	feed.stop()
	result = {"failed": failed, "retried": retried, "noticed": noticed, "again": again, "after": after}
	"""
)

if not WAYLAND:
	from desktopSession import activate

	def runAgain(tmp_path: Path, code: str) -> dict:
		with DesktopSession(tmp_path / "desktop") as desktop:
			fixture = desktop.startApp(desktop.fixture("gtkFixture"), title="Fixture window")
			activate("Fixture window")
			done = harness.runChild(tmp_path, f"PID = {fixture.pid}\n" + AGAIN + code, timeout=120)
		assert done.error is None, f"{done.error}\n{done.stderr[-4000:]}"
		return done.value

	def test_startsAgainOnceTheBusIsBack(tmp_path: Path) -> None:
		results = runAgain(tmp_path, BUS_LOST)
		assert (results["failed"], results["retried"], results["again"]) == (False, True, True), results
		assert results["after"][0], f"a look after the new connection ended unknown: {results}"
		print(f"the closed connection noticed after {results['noticed'] * 1000:.0f} ms")

	def test_startsAgainAfterAStopWithAReadOut(tmp_path: Path) -> None:
		results = runAgain(tmp_path, RESTART)
		assert results["before"][0], results
		assert results["pipeKept"], results
		assert results["inFlight"] >= 1, f"no read was out at the stop: {results}"
		assert results["after"][0], f"a look after the restart ended unknown: {results}"
		print(
			f"found {results['found'] * 1000:.0f} ms and again {results['refound'] * 1000:.0f} ms after start; "
			f"looks took {results['before'][1] * 1000:.1f} and {results['after'][1] * 1000:.1f} ms"
		)


#: The child of the Wayland test: the feed started with no other listener in the session, and a
#: program started, whose window it must hear come to the front, and then its program end.
NEW_WINDOW = textwrap.dedent(
	"""\
	import os
	import subprocess
	import time

	from linuxPlatform.keys.atspiCapture import AtspiCaptureFeed

	def isOurs(pid):
		\"\"\"Whether pid is the program's process or one it started, as a wrapper starts the real one.\"\"\"
		for _level in range(8):
			if pid == app.pid:
				return True
			try:
				with open(f"/proc/{pid}/stat") as f:
					pid = int(f.read().rsplit(")", 1)[1].split()[1])
			except (OSError, ValueError, IndexError):
				return False
		return False


	feed = AtspiCaptureFeed()
	feed.start()
	assert feed.canLook
	app = subprocess.Popen(COMMAND, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
	started = time.monotonic()
	try:
		while not ((foreground := feed.foreground) and foreground[2] and isOurs(foreground[2])):
			assert app.poll() is None, f"the program ended: {app.returncode}"
			assert time.monotonic() - started < 30, f"never heard its window come to the front: {foreground}"
			time.sleep(0.01)
		heard = time.monotonic() - started
		feed.setCapture("browse", foreground[2])
		state = feed.captureState
		assert state.window and state.focused and state.windowProcess == foreground[2], state
	finally:
		app.terminate()
		app.wait(10)
	ended = time.monotonic()
	while feed.foreground is not None or feed.captureState.focused:
		assert time.monotonic() - ended < 2, f"its end was not heard: {feed.foreground}"
		time.sleep(0.005)
	gone = time.monotonic() - ended
	feed.stop()
	result = {"heard": heard, "gone": gone}
	"""
)

if WAYLAND:

	def runNewWindow(tmp_path: Path, command: list[str]) -> dict:
		done = harness.runChild(tmp_path, f"COMMAND = {json.dumps(command)}\n" + NEW_WINDOW, timeout=120)
		assert done.error is None, f"{done.error}\n{done.stderr[-4000:]}"
		print(
			f"heard the window {done.value['heard']:.2f} s after start, its end {done.value['gone'] * 1000:.0f} ms after"
		)
		return done.value

	def test_hearsANewGtk4WindowWithNoOtherListener(tmp_path: Path) -> None:
		runNewWindow(tmp_path, ["python3", str(APPS / "gtk4.py")])

	def test_hearsANewElectronWindowWithNoOtherListener(tmp_path: Path) -> None:
		profile = tmp_path / "electron"
		profile.mkdir()
		runNewWindow(
			tmp_path,
			[
				"electron",
				"--ozone-platform=wayland",
				"--disable-gpu",
				f"--user-data-dir={profile}",
				str(APPS / "electronChat"),
			],
		)
