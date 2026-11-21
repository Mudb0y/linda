"""The AT-SPI capture feed under the rig's compositors, headless Mutter and headless sway, with native
GTK 3, GTK 4, Qt 6 and Firefox windows.

Under sway the keyboard is moved between windows with swaymsg. Mutter has nothing that moves it from
outside, so there it is moved as a person moves it by opening and closing windows: a new window takes
the keyboard, and a closed one gives it back to the window under it. The feed runs in a child, as the
kernel source runs it in NVDA, while that child's main thread computes all the while.

tests/linux/wayland-gate runs this file under each compositor, with and without Xwayland:

	nix develop . -c tests/linux/wayland-gate SCRATCH tests/linux/test_waylandCapture.py
"""

import json
import textwrap
from pathlib import Path

import harness
import pytest

wayland = pytest.mark.rigSession("wayland")

#: Windows of one toolkit, opened and closed on command, each saying when it gains the keyboard by the
#: toolkit's own word, which comes from the compositor and not from AT-SPI.
WINDOWS = textwrap.dedent(
	'''\
	"""Arguments: gtk3, gtk4 or qt6. Reads "open NAME", "close NAME", "end NAME" (close it and quit at
	once) and "quit" on standard input; prints "backend NAME", then "shown NAME", "active NAME" and
	"closed NAME"."""

	import sys

	TOOLKIT = sys.argv[1]


	def say(*words):
		print(*words, flush=True)


	if TOOLKIT in ("gtk3", "gtk4"):
		import gi

		gi.require_version("Gtk", "3.0" if TOOLKIT == "gtk3" else "4.0")
		gi.require_version("Gdk", "3.0" if TOOLKIT == "gtk3" else "4.0")
		from gi.repository import Gdk, GLib, Gtk

		if TOOLKIT == "gtk4":
			Gtk.init()
		else:
			Gtk.init(None)
		windows = {}
		loop = GLib.MainLoop()

		def activeChanged(window, _spec, name):
			if window.is_active():
				say("active", name)

		def command(line):
			verb, _, name = line.strip().partition(" ")
			if verb == "open":
				window = Gtk.Window(title=name)
				window.set_default_size(300, 200)
				button = Gtk.Button(label="Button of " + name)
				if TOOLKIT == "gtk4":
					window.set_child(button)
				else:
					window.add(button)
				window.connect("notify::is-active", activeChanged, name)
				windows[name] = window
				if TOOLKIT == "gtk4":
					window.present()
				else:
					window.show_all()
				say("shown", name)
			elif verb in ("close", "end"):
				window = windows.pop(name, None)
				if window is not None:
					window.destroy()
				say("closed", name)
				if verb == "end":
					sys.exit(0)
			elif verb == "quit":
				loop.quit()

		def readable(_channel, _condition):
			line = sys.stdin.readline()
			if not line:
				loop.quit()
				return False
			command(line)
			return True

		GLib.io_add_watch(GLib.IOChannel.unix_new(sys.stdin.fileno()), GLib.IO_IN | GLib.IO_HUP, readable)
		say("backend", type(Gdk.Display.get_default()).__name__)
		loop.run()
	else:
		from PyQt6.QtCore import QSocketNotifier
		from PyQt6.QtWidgets import QApplication, QPushButton, QWidget

		app = QApplication(sys.argv[:1])
		app.setQuitOnLastWindowClosed(False)
		windows = {}

		def focusWindowChanged(window):
			for name, widget in windows.items():
				if window is not None and widget.windowHandle() is window:
					say("active", name)

		app.focusWindowChanged.connect(focusWindowChanged)

		def command(line):
			verb, _, name = line.strip().partition(" ")
			if verb == "open":
				window = windows[name] = QWidget()
				window.setWindowTitle(name)
				window.resize(300, 200)
				QPushButton("Button of " + name, window)
				window.show()
				say("shown", name)
			elif verb in ("close", "end"):
				window = windows.pop(name, None)
				if window is not None:
					window.close()
					window.deleteLater()
				say("closed", name)
				if verb == "end":
					sys.exit(0)
			elif verb == "quit":
				app.quit()

		def readable():
			line = sys.stdin.readline()
			if not line:
				app.quit()
				return
			command(line)

		notifier = QSocketNotifier(sys.stdin.fileno(), QSocketNotifier.Type.Read)
		notifier.activated.connect(readable)
		say("backend", app.platformName())
		app.exec()
	'''
)

#: A witness on a connection and in a process of its own, which registers nothing: given an application's
#: bus name, it prints "ready" once it listens, and then the time, on the clock every process shares, of
#: the first word on the accessibility bus that a window of that application went or that it ended. That
#: is the soonest the feed could have known, whatever the toolkit was doing before it said so.
WITNESS = textwrap.dedent(
	"""\
	import sys
	import time

	from gi.repository import Gio, GLib

	APPLICATION = sys.argv[1]
	session = Gio.bus_get_sync(Gio.BusType.SESSION, None)
	(address,) = session.call_sync(
		"org.a11y.Bus", "/org/a11y/bus", "org.a11y.Bus", "GetAddress", None, GLib.VariantType("(s)"),
		Gio.DBusCallFlags.NONE, 1000, None,
	).unpack()
	connection = Gio.DBusConnection.new_for_address_sync(
		address,
		Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
		None,
		None,
	)
	loop = GLib.MainLoop()


	def went(*_arguments):
		print("went", time.monotonic(), flush=True)
		loop.quit()


	def stateChanged(_connection, _sender, _path, _interface, _member, parameters):
		state = parameters.get_child_value(0).get_string()
		if parameters.get_child_value(1).get_int32() == (1 if state == "defunct" else 0):
			went()


	def ownerChanged(_connection, _sender, _path, _interface, _member, parameters):
		name, _old, new = parameters.unpack()
		if name == APPLICATION and not new:
			went()


	for member in ("Deactivate", "Destroy"):
		connection.signal_subscribe(
			APPLICATION, "org.a11y.atspi.Event.Window", member, None, None, Gio.DBusSignalFlags.NONE, went
		)
	for state in ("defunct", "showing", "visible"):
		connection.signal_subscribe(
			APPLICATION, "org.a11y.atspi.Event.Object", "StateChanged", None, state,
			Gio.DBusSignalFlags.NONE, stateChanged,
		)
	connection.signal_subscribe(
		"org.freedesktop.DBus", "org.freedesktop.DBus", "NameOwnerChanged", None, APPLICATION,
		Gio.DBusSignalFlags.NONE, ownerChanged,
	)
	# Once this answers, the bus has every match rule above.
	connection.call_sync(
		"org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "GetId", None, None,
		Gio.DBusCallFlags.NONE, 1000, None,
	)
	print("ready", flush=True)
	loop.run()
	"""
)

#: Ahead of every child: the feed, made to keep a record of the window events and active states each
#: application sent it, the toolkits' programs, Firefox, and the compositor's hand on the keyboard.
COMMON = textwrap.dedent(
	"""\
	import collections
	import json
	import os
	import signal
	import subprocess
	import sys
	import threading
	import time

	from linuxPlatform.keys.atspiCapture import AtspiCaptureFeed

	COMPOSITOR = os.environ["LINDA_RIG_COMPOSITOR"]
	SCRIPTED = ("gtk3", "gtk4", "qt6")
	#: What each toolkit says of its display when it is a native Wayland client.
	NATIVE = {"gtk3": "GdkWaylandDisplay", "gtk4": "GdkWaylandDisplay", "qt6": "wayland"}
	#: Seconds to wait for a window to come and take the keyboard; a first Firefox takes several.
	WINDOW_TIMEOUT = 30


	class Feed(AtspiCaptureFeed):
		\"\"\"The feed, keeping what each application sent: its window events and its active states,
		with the path each came for.\"\"\"

		def __init__(self):
			super().__init__()
			self.heard = collections.defaultdict(set)
			#: Every signal followed, with when it came and the foreground after it, for a failure's report.
			self.trace = collections.deque(maxlen=4000)

		def _traced(self, sender, path, kind):
			foreground = self.foreground
			self.trace.append((time.monotonic(), sender, path.rsplit("/", 1)[-1], kind, foreground and foreground[2]))

		def _onWindowSignal(self, connection, sender, path, interface, member, parameters):
			self.heard[sender].add((path, member))
			super()._onWindowSignal(connection, sender, path, interface, member, parameters)
			self._traced(sender, path, member)

		def _onStateSignal(self, connection, sender, path, interface, member, parameters):
			state = parameters.get_child_value(0).get_string()
			value = parameters.get_child_value(1).get_int32()
			if state == "active":
				self.heard[sender].add((path, f"active {value}"))
			super()._onStateSignal(connection, sender, path, interface, member, parameters)
			self._traced(sender, path, f"{state} {value}")

		def _onNameOwnerChanged(self, connection, sender, path, interface, member, parameters):
			super()._onNameOwnerChanged(connection, sender, path, interface, member, parameters)
			name, _old, new = parameters.unpack()
			if name.startswith(":") and not new:
				self._traced(name, "", "gone")

		def _onChildrenSignal(self, connection, sender, path, interface, member, parameters):
			super()._onChildrenSignal(connection, sender, path, interface, member, parameters)
			self._traced(sender, path, "child added")

		def _activated(self, key, now):
			super()._activated(key, now)
			self._traced(*key, "taken as active")

		def _deactivated(self, key, now):
			super()._deactivated(key, now)
			self._traced(*key, "taken as inactive")

		def traceSince(self, since):
			return [
				[round((at - since) * 1000, 1), sender, path, kind, pid]
				for at, sender, path, kind, pid in list(self.trace)
				if at >= since
			]


	def parentOf(pid):
		try:
			with open(f"/proc/{pid}/stat") as f:
				return int(f.read().rsplit(")", 1)[1].split()[1])
		except (OSError, ValueError, IndexError):
			return None


	def isOf(pid, root):
		\"\"\"Whether pid is root or a process root started, as Firefox's wrapper starts Firefox.\"\"\"
		for _level in range(8):
			if pid is None:
				return False
			if pid == root:
				return True
			pid = parentOf(pid)
		return False


	def foregroundPid():
		return (feed.foreground or (None, None, None))[2]


	def awaitForeground(root, timeout=WINDOW_TIMEOUT):
		\"\"\"Wait for the feed to have a window of root's in front, and give its process.\"\"\"
		started = time.monotonic()
		while not isOf(foregroundPid(), root):
			assert time.monotonic() < started + timeout, (
				f"the feed never heard process {root} come to the front: {feed.foreground}; "
				f"it heard {feed.traceSince(started)[-60:]}"
			)
			time.sleep(0.005)
		return foregroundPid()


	class Program:
		\"\"\"One toolkit's windows program, and the lines it printed with when each came.\"\"\"

		def __init__(self, toolkit):
			self.toolkit = toolkit
			self.process = subprocess.Popen(
				[sys.executable, WINDOWS, toolkit],
				stdin=subprocess.PIPE,
				stdout=subprocess.PIPE,
				stderr=subprocess.DEVNULL,
				text=True,
			)
			self.pid = self.process.pid
			self.lines = []
			self.changed = threading.Condition()
			threading.Thread(target=self._read, daemon=True).start()
			backend = self.waitFor("backend", None, 0, timeout=WINDOW_TIMEOUT)[1]
			assert backend == NATIVE[toolkit], f"{toolkit} is not a native Wayland client: {backend}"

		def _read(self):
			for line in self.process.stdout:
				with self.changed:
					self.lines.append((time.monotonic(), line.split()))
					self.changed.notify_all()

		def send(self, verb, name):
			sent = time.monotonic()
			self.process.stdin.write(f"{verb} {name}\\n")
			self.process.stdin.flush()
			return sent

		def waitFor(self, word, name, since, timeout=10):
			\"\"\"When the program said word of name, at or after since, and the line.\"\"\"
			deadline = time.monotonic() + timeout
			with self.changed:
				while True:
					for at, words in self.lines:
						if at >= since and words[:1] == [word] and (name is None or words[1:] == [name]):
							return at, words[1] if len(words) > 1 else None
					left = deadline - time.monotonic()
					assert left > 0, f"{self.toolkit} never said {word} {name}: {self.lines[-5:]}"
					self.changed.wait(left)

		def open(self, name):
			sent = self.send("open", name)
			return self.waitFor("active", name, sent, timeout=WINDOW_TIMEOUT)[0]

		def stop(self):
			if self.process.poll() is None:
				self.process.kill()
			self.process.wait(10)


	class Firefox(Program):
		\"\"\"Firefox with a fresh profile on a page of its own, as a native Wayland client. The page says on
		standard output when it gains and loses the keyboard and as it goes, Firefox's own word for each,
		which waitFor waits for as Program's does.\"\"\"

		def __init__(self, title):
			profile = os.path.join(SCRATCH, "firefox-" + title.replace(" ", "-"))
			os.makedirs(profile)
			with open(os.path.join(profile, "user.js"), "w") as f:
				f.write(USER_JS + 'user_pref("browser.dom.window.dump.enabled", true);\\n')
			# A file, as Firefox runs no script in a page given as a data address.
			page = os.path.join(profile, "page.html")
			with open(page, "w") as f:
				f.write(
					f"<title>{title}</title><p>Text of {title}</p>"
					"<script>for (const name of ['focus', 'blur', 'beforeunload', 'pagehide', 'unload'])"
					" addEventListener(name, () => dump(`linda-test ${name}\\\\n`))</script>"
				)
			self.process = subprocess.Popen(
				["firefox", "--no-remote", "--new-instance", "--profile", profile, "file://" + page],
				stdin=subprocess.DEVNULL,
				stdout=subprocess.PIPE,
				stderr=subprocess.DEVNULL,
				env=dict(os.environ, MOZ_CRASHREPORTER_DISABLE="1", MOZ_ENABLE_WAYLAND="1"),
				start_new_session=True,
				text=True,
				errors="replace",
			)
			self.pid = self.process.pid
			self.toolkit = "firefox"
			self.lines = []
			self.changed = threading.Condition()
			threading.Thread(target=self._read, daemon=True).start()

		def _read(self):
			for line in self.process.stdout:
				words = line.split()
				if len(words) == 2 and words[0] == "linda-test":
					with self.changed:
						self.lines.append((time.monotonic(), words[1:]))
						self.changed.notify_all()

		def said(self):
			\"\"\"When the page first said each of its words.\"\"\"
			with self.changed:
				first = {}
				for at, (word,) in self.lines:
					first.setdefault(word, at)
				return first

		def stop(self):
			if self.process.poll() is None:
				try:
					os.killpg(self.pid, signal.SIGKILL)
				except ProcessLookupError:
					pass
			self.process.wait(10)


	class Witness:
		\"\"\"WITNESS for one application, listening once it is made.\"\"\"

		def __init__(self, busName):
			self.process = subprocess.Popen(
				[sys.executable, WITNESS, busName], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True
			)
			assert self.process.stdout.readline().strip() == "ready", "the witness did not start"
			self.went = None
			threading.Thread(target=self._read, daemon=True).start()

		def _read(self):
			words = self.process.stdout.readline().split()
			if words[:1] == ["went"]:
				self.went = float(words[1])

		def wait(self, timeout=5):
			deadline = time.monotonic() + timeout
			while self.went is None and time.monotonic() < deadline:
				time.sleep(0.01)
			self.process.kill()
			self.process.wait(10)
			assert self.went is not None, "the witness heard nothing go"
			return self.went


	def sway(*arguments):
		done = subprocess.run(["swaymsg", *arguments], capture_output=True, text=True, timeout=10, check=False)
		assert done.returncode == 0, done.stdout + done.stderr
		return done.stdout


	def swayWindows():
		\"\"\"Every window sway shows: its id, its process, its title, how it is shown, and whether it has the
		keyboard.\"\"\"
		found = []

		def walk(node):
			if node.get("pid"):
				found.append((node["id"], node["pid"], node.get("name"), node.get("shell"), node.get("focused")))
			for child in node.get("nodes", []) + node.get("floating_nodes", []):
				walk(child)

		walk(json.loads(sway("-t", "get_tree")))
		return found


	def swayWindowOf(root, title=None, timeout=WINDOW_TIMEOUT):
		deadline = time.monotonic() + timeout
		while True:
			for con, pid, name, shell, _focused in swayWindows():
				if isOf(pid, root) and (title is None or name == title):
					assert shell == "xdg_shell", f"{name} is not a native Wayland window: {shell}"
					return con
			assert time.monotonic() < deadline, f"sway never showed a window of {root} named {title}: {swayWindows()}"
			time.sleep(0.05)


	def swayFocus(con):
		\"\"\"Have sway give con the keyboard, and say when it had.\"\"\"
		sway(f"[con_id={con}]", "focus")
		at = time.monotonic()
		focused = [c for c, _pid, _name, _shell, isFocused in swayWindows() if isFocused]
		assert focused == [con], f"sway gave the keyboard to {focused}, not {con}"
		return at


	def toolkitRecord(programs):
		\"\"\"For each toolkit, which window events and active states its windows sent the feed.\"\"\"
		processes = dict(feed._processes)
		processes.update(KNOWN_PROCESSES)
		topLevels = dict(feed._topLevels)
		record = {}
		for sender, heard in [(sender, set(heard)) for sender, heard in list(feed.heard.items())]:
			pid = processes.get(sender)
			owner = next((p for p in programs if pid is not None and isOf(pid, p.pid)), None)
			if owner is None:
				continue
			windows = {path for path, kind in heard if kind in ("Activate", "Deactivate")}
			windows |= {path for (bus, path), topLevel in topLevels.items() if bus == sender and topLevel}
			kinds = record.setdefault(owner.toolkit, set())
			kinds |= {kind for path, kind in heard if path in windows}
		return {toolkit: sorted(kinds) for toolkit, kinds in record.items()}


	#: Bus names' processes, kept as they are learnt, since the feed forgets an application that ends.
	KNOWN_PROCESSES = {}


	def remember():
		KNOWN_PROCESSES.update(feed._processes)


	feed = Feed()
	feed.start()
	assert feed.canLook, "the feed did not connect"
	"""
)

#: The child of the alternation test. For each toolkit in turn, its window is the anchor: the feed is
#: told NVDA's focus is in a document of its, and the keyboard goes to a window of another toolkit and
#: back, 25 times, each checked 200 ms after the compositor or the toolkit says it moved.
ALTERNATION = textwrap.dedent(
	"""\
	ALTERNATIONS = 100
	CHECK_AFTER = 0.2
	ANCHORS = ("gtk3", "gtk4", "qt6", "firefox")
	#: The toolkit whose window the keyboard goes to away from each anchor.
	OTHER = {"gtk3": "gtk4", "gtk4": "qt6", "qt6": "gtk3", "firefox": "gtk3"}

	programs = {toolkit: Program(toolkit) for toolkit in SCRIPTED}
	results = {"wrong": [], "lags": []}


	def check(alternation, switched, anchor, target, window):
		\"\"\"Wait for the feed to hear the switch, and check its state 200 ms after it.\"\"\"
		while not isOf(foregroundPid(), target.pid) and time.monotonic() < switched + CHECK_AFTER:
			time.sleep(0.002)
		results["lags"].append(time.monotonic() - switched)
		time.sleep(max(0.0, switched + CHECK_AFTER - time.monotonic()))
		state = feed.captureState
		seen = foregroundPid()
		remember()
		expected = (target is anchor, window, True)
		if (state.focused, state.window, isOf(seen, target.pid)) != expected:
			results["wrong"].append(
				[alternation, anchor.toolkit, target.toolkit, state.focused, state.window, window, seen, target.pid]
			)
			if len(results["wrong"]) <= 3:
				results["wrong"].append(feed.traceSince(switched - 0.3))


	def anchorOn(anchor, pid):
		feed.setCapture("browse", pid)
		state = feed.captureState
		assert state.window and state.focused and state.windowProcess == pid, state
		return state.window


	def swayBlock(block, anchor, other):
		if anchor.toolkit == "firefox":
			anchorCon = swayWindowOf(anchor.pid)
		else:
			anchor.open(f"anchor{block}")
			anchorCon = swayWindowOf(anchor.pid, f"anchor{block}")
		other.open(f"other{block}")
		otherCon = swayWindowOf(other.pid, f"other{block}")
		swayFocus(anchorCon)
		window = anchorOn(anchor, awaitForeground(anchor.pid))
		for step in range(ALTERNATIONS // len(ANCHORS)):
			target = other if step % 2 == 0 else anchor
			switched = swayFocus(otherCon if target is other else anchorCon)
			check(block * 25 + step, switched, anchor, target, window)
		other.send("close", f"other{block}")
		if anchor.toolkit != "firefox":
			anchor.send("close", f"anchor{block}")


	def mutterBlock(block, anchor, other):
		if anchor.toolkit != "firefox":
			anchor.open(f"anchor{block}")
		window = anchorOn(anchor, awaitForeground(anchor.pid))
		for step in range(ALTERNATIONS // len(ANCHORS)):
			name = f"other{block}-{step // 2}"
			if step % 2 == 0:
				target = other
				switched = other.open(name)
			else:
				target = anchor
				sent = other.send("close", name)
				if anchor.toolkit == "firefox":
					switched = anchor.waitFor("focus", None, sent)[0]
				else:
					switched = anchor.waitFor("active", f"anchor{block}", sent)[0]
			check(block * 25 + step, switched, anchor, target, window)
		other.send("close", f"other{block}-12")
		if anchor.toolkit != "firefox":
			anchor.send("close", f"anchor{block}")


	def drive():
		firefox = None
		try:
			for block, toolkit in enumerate(ANCHORS):
				if toolkit == "firefox":
					firefox = Firefox("Anchor page")
					anchor = firefox
				else:
					anchor = programs[toolkit]
				(swayBlock if COMPOSITOR == "sway" else mutterBlock)(block, anchor, programs[OTHER[toolkit]])
				time.sleep(0.3)
			remember()
			results["toolkits"] = toolkitRecord([*programs.values(), firefox])
		except BaseException:
			import traceback

			results["error"] = traceback.format_exc()
		finally:
			results["finished"] = True
			if firefox is not None:
				firefox.stop()
			for program in programs.values():
				program.stop()


	# What the kernel source sets while it runs, and the feed runs only under it: at the default, every
	# signal the feed's thread takes waits up to five milliseconds for the busy main thread to let go.
	sys.setswitchinterval(0.001)
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

#: The child of the tests that end the foreground: its window is the anchor, and then its program is
#: killed, or the window is closed, by the program itself or by Ctrl+W in Firefox. How long the feed
#: takes to leave it unfocused is measured from the kill, the command to close, or, for Firefox, whose
#: own way from the key to taking its window down is not the feed's, from the witness's first word that
#: its window went.
ENDING = textwrap.dedent(
	"""\
	if TOOLKIT == "firefox":
		program = Firefox("Only page")
	else:
		program = Program(TOOLKIT)
		program.open("only")
	try:
		pid = awaitForeground(program.pid)
		feed.setCapture("browse", pid)
		state = feed.captureState
		assert state.window and state.focused, state
		time.sleep(0.3)
		said = {}
		if HOW == "kill":
			started = time.monotonic()
			os.kill(pid, signal.SIGKILL)
		elif TOOLKIT == "firefox":
			sys.path.append(str(harness.HERE))
			import waylandKeyboard

			witness = Witness(feed.foreground[0])
			started = time.monotonic()
			# Pressing it takes as long as the key is held and let go, while the feed is to be watched.
			threading.Thread(target=waylandKeyboard.press, args=("ctrl+w",), daemon=True).start()
		else:
			started = program.send("close" if HOW == "close" else "end", "only")
		while feed.captureState.focused or isOf(foregroundPid(), program.pid):
			assert time.monotonic() - started < 5, f"the end was never heard: {feed.captureState} {feed.foreground}"
			time.sleep(0.001)
		unfocused = time.monotonic()
		if HOW == "ctrl+w":
			pressed = started
			started = witness.wait()
			time.sleep(0.5)
			said = {word: round((at - pressed) * 1000, 1) for word, at in program.said().items()}
			said.update(went=round((started - pressed) * 1000, 1), unfocused=round((unfocused - pressed) * 1000, 1))
		alive = program.process.poll() is None
		after = unfocused - started
		trace = feed.traceSince(started - 0.3)
		remember()
		record = toolkitRecord([program])
	finally:
		program.stop()
	feed.stop()
	result = {"after": after, "alive": alive, "toolkits": record, "trace": trace, "said": said}
	"""
)


def runFeedChild(tmp_path: Path, code: str, **names: object) -> dict:
	windows = tmp_path / "windows.py"
	windows.write_text(WINDOWS, encoding="utf-8")
	scratch = tmp_path / "scratch"
	scratch.mkdir()
	witness = tmp_path / "witness.py"
	witness.write_text(WITNESS, encoding="utf-8")
	values = {
		"WINDOWS": str(windows),
		"WITNESS": str(witness),
		"SCRATCH": str(scratch),
		"USER_JS": (harness.HERE / "bench" / "user.js").read_text(encoding="utf-8"),
		**names,
	}
	prelude = "".join(f"{name} = {json.dumps(value)}\n" for name, value in values.items())
	done = harness.runChild(tmp_path, prelude + COMMON + code, timeout=600)
	assert done.error is None, f"{done.error}\n{done.stderr[-4000:]}"
	return done.value


@wayland
def test_followsAlternations(tmp_path: Path) -> None:
	results = runFeedChild(tmp_path, ALTERNATION)
	assert "error" not in results, results["error"]
	lags = sorted(results["lags"])
	assert len(lags) == 100, len(lags)
	assert results["wrong"] == [], f"{results['wrong']}; lags {lags[len(lags) // 2]:.3f} {lags[-1]:.3f}"
	assert results["busyRounds"] > 0, "the main thread was never busy"
	assert set(results["toolkits"]) == {"gtk3", "gtk4", "qt6", "firefox"}, results["toolkits"]
	print(
		f"{harness.rigSession()}: feed lag after the switch: median {lags[50] * 1000:.0f} ms, "
		f"worst {lags[-1] * 1000:.0f} ms; main thread busy for {results['busyRounds']} rounds"
	)
	for toolkit, kinds in sorted(results["toolkits"].items()):
		print(f"{harness.rigSession()}: {toolkit} sent {', '.join(kinds)}")


def describeEnding(results: dict) -> str:
	"""What the feed heard as the foreground ended, a line for each signal or decision, in milliseconds
	from the end."""
	lines = [f"unfocused after {results['after'] * 1000:.1f} ms; the program alive after: {results['alive']}"]
	lines += [" ".join(str(part) for part in entry) for entry in results["trace"]]
	return "\n".join(lines)


#: Seconds within which the feed must leave the anchor unfocused once its program or window ends.
ENDED_WITHIN = 0.1


@wayland
@pytest.mark.parametrize("toolkit", ["gtk3", "gtk4", "qt6", "firefox"])
def test_killedForegroundProgram(tmp_path: Path, toolkit: str) -> None:
	results = runFeedChild(tmp_path, ENDING, TOOLKIT=toolkit, HOW="kill")
	print(f"{harness.rigSession()}: {toolkit} killed, unfocused after {results['after'] * 1000:.1f} ms")
	assert results["after"] < ENDED_WITHIN, describeEnding(results)


@wayland
@pytest.mark.parametrize(
	("toolkit", "how"),
	[
		("gtk3", "close"),
		("gtk4", "close"),
		("qt6", "close"),
		("gtk3", "end"),
		("gtk4", "end"),
		("qt6", "end"),
		("firefox", "ctrl+w"),
	],
)
def test_closedLastWindow(tmp_path: Path, toolkit: str, how: str) -> None:
	results = runFeedChild(tmp_path, ENDING, TOOLKIT=toolkit, HOW=how)
	print(
		f"{harness.rigSession()}: {toolkit}'s last window closed ({how}), unfocused after "
		f"{results['after'] * 1000:.1f} ms; heard {results['toolkits'].get(toolkit)}"
	)
	if results["said"]:
		print(f"{harness.rigSession()}: milliseconds after Ctrl+W: {results['said']}")
	if how == "close":
		# The program stays, as one with a service in the background does, so its bus name stays too.
		assert results["alive"], describeEnding(results)
	assert results["after"] < ENDED_WITHIN, describeEnding(results)


#: Ahead of each logic test, in any session: a feed whose bus calls and timers are kept for the test to
#: answer and fire, and signals as the bus hands them to it.
LOGIC = textwrap.dedent(
	"""\
	from gi.repository import GLib

	from linuxPlatform.keys.atspiCapture import NEW_WINDOW_READS, REGISTRY, ROOT_PATH, STATE_ACTIVE, AtspiCaptureFeed


	class Feed(AtspiCaptureFeed):
		def __init__(self):
			super().__init__()
			self.requests = []
			self.timers = []

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

		def fire(self):
			timers, self.timers = self.timers, []
			for _seconds, call in timers:
				call()


	def window(feed, sender, path, member):
		feed._onWindowSignal(None, sender, path, "org.a11y.atspi.Event.Window", member, None)


	def childAdded(feed, sender, path, child):
		parameters = GLib.Variant("(siiva{sv})", ("add", 0, 0, GLib.Variant("(so)", child), {}))
		feed._onChildrenSignal(None, sender, path, "org.a11y.atspi.Event.Object", "ChildrenChanged", parameters)


	FIRST = (":1.10", "/org/a11y/atspi/accessible/1")
	SECOND = (":1.10", "/org/a11y/atspi/accessible/2")
	"""
)


def runLogic(tmp_path: Path, code: str) -> None:
	done = harness.runChild(tmp_path, LOGIC + textwrap.dedent(code), timeout=60)
	assert done.error is None, f"{done.error}\n{done.stderr[-4000:]}"


def test_destroyedForegroundWindowIsGone(tmp_path: Path) -> None:
	runLogic(
		tmp_path,
		"""\
		feed = Feed()
		feed._processKnown(":1.10", 100)
		window(feed, *FIRST, "Activate")
		feed.setCapture("browse", 100)
		assert feed.captureState.focused
		# Another window of the program going leaves the foreground where it is.
		window(feed, *SECOND, "Destroy")
		assert feed.foreground[:2] == FIRST and feed.captureState.focused
		# Firefox says only this as its last window closes, and its process lives on a while.
		window(feed, *FIRST, "Destroy")
		s = feed.captureState
		assert (s.window, s.focused, feed.foreground) == (0, False, None), (s, feed.foreground)
		""",
	)


def test_windowsOfAJoiningApplicationAreAsked(tmp_path: Path) -> None:
	runLogic(
		tmp_path,
		"""\
		feed = Feed()
		app = (":1.40", ROOT_PATH)
		new = (":1.40", "/org/gtk/application/unknown/a11y/1")
		# A window added to an application is not an application joining.
		childAdded(feed, ":1.40", ROOT_PATH, new)
		childAdded(feed, REGISTRY, ROOT_PATH, (REGISTRY, ROOT_PATH))
		assert feed.requests == [], feed.requests
		# GTK 4 joins as its first window shows, and may say nothing more of it.
		childAdded(feed, ":1.2", ROOT_PATH, app)
		feed.answer("children", app, [new])
		assert len(feed.timers) == 1 and 0 < feed.timers[0][0] <= NEW_WINDOW_READS[0], feed.timers
		feed.fire()
		feed.answer("state", new, [1 << STATE_ACTIVE, 0])
		assert feed.foreground == (*new, None), feed.foreground
		assert [kind for kind, _target, _then in feed.requests] == ["process"], feed.requests
		""",
	)
