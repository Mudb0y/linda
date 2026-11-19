"""What a key capture test in cases/ is written with: pressing keys, hearing Linda and reading the
recorders, in the guest guest.nix describes.

A case is a file in cases/ with a function run(ground), which default.nix makes a flake check of its
own (with cases/NAME.nix, if there is one, as a NixOS module added to its guest); it runs in the NixOS test driver, on the host, and reaches the guest through the driver's
machine. Everything that happens in the guest is stamped with the guest's CLOCK_MONOTONIC, which
Ground.mark reads, so a test asks for what happened after a mark.

Keys come two ways. Ground.press sends QEMU's own key events, which reach the guest kernel as the
emulated keyboard's; names are QEMU's sendkey names, as "insert-t", "f10", "ret". Ground.keyboard
makes a fake keyboard inside the guest through its uinput, and Ground.uhidKeyboard one through its
uhid, which the key capture helper takes as it takes a plugged-in keyboard; names are evdev's, as
"KEY_T".

linda-keyd, the key capture helper, runs in a guest whose cases/NAME.nix imports keyd.nix. Its
client in a test is guest/keydClient.py, which Ground.keydClient starts as the test needs it, and
what the helper says in the journal Ground.helperLog reads, stamped with the same clock.

Linda is heard as the tests under tests/linux hear it: through the transcript synth, which the test
hooks give the package, and whose records synthDrivers/_transcriptRecords.py reads.

The recorders say what reached each layer, key by key: "evdev" what the kernel gave an ordinary
reader of each keyboard node (what the X server's libinput reads), "x" what the X server received,
"app" what reached the test window. Each record is a dict as guest/keyRecorder.py describes.
"""

import importlib.util
import json
import re
import shlex
import subprocess
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from types import ModuleType
from typing import Any

GROUND = "/tmp/ground"
LOGS = {
	"evdev": f"{GROUND}/evdev.jsonl",
	"x": f"{GROUND}/x.jsonl",
	"app": f"{GROUND}/app.jsonl",
}
LINDA_DIRECTORY = f"{GROUND}/linda"
#: Reads of a file made before one that is not garbled is given up on.
READ_ATTEMPTS = 3
TRANSCRIPT = f"{LINDA_DIRECTORY}/transcript.jsonl"
WINDOW_TITLE = "Ground window"
USER = "alice"

#: Linda's settings in every test, as tests/linux/test_keys.py has them, before a test's own.
BASE_SETTINGS = {
	"general": {
		"language": "en",
		"playStartAndExitSounds": "False",
		"showWelcomeDialogAtStartup": "False",
	},
	"speech": {"synth": "transcript"},
	"braille": {"display": "noBraille"},
	# Insert, the numpad's Insert and Caps Lock, as the owner has them.
	"keyboard": {"keyboardLayout": "desktop", "NVDAModifierKeys": "7"},
	"featureFlag": {"playErrorSound": "2"},
	"addonStore": {"automaticUpdates": "disabled", "baseServerURL": "http://127.0.0.1:1"},
}
#: Ends an expected text that need only start a speak record, as in tests/linux/desktopSession.py.
PREFIX_MARK = "\N{HORIZONTAL ELLIPSIS}"
#: The first line of each record of Linda's log, as tests/linux/harness.py reads it.
LOG_HEADER = re.compile(r"^[A-Z_]+ - .+? \((?P<time>\d\d:\d\d:\d\d\.\d{3})\) - .*:$", re.MULTILINE)
#: What Linda logs as it moves to the kernel route, and back to the X display.
KERNEL_ROUTE = "Keys come from the kernel through linda-keyd"
X_ROUTE = "Keys are read from the X display"


@dataclass(frozen=True)
class Mark:
	#: How many speak records Linda had made.
	spoken: int
	#: The guest's CLOCK_MONOTONIC, in seconds.
	t: float


def _loadFile(name: str, path: str) -> ModuleType:
	spec = importlib.util.spec_from_file_location(name, path)
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module


def _matches(expected: str, spoken: str) -> bool:
	if expected.endswith(PREFIX_MARK):
		return spoken.startswith(expected[: -len(PREFIX_MARK)])
	return spoken == expected


def _inOrder(expected: Sequence[str], spoken: Sequence[str]) -> bool:
	records = iter(spoken)
	return all(any(_matches(text, record) for record in records) for text in expected)


def keyEvents(records: list[dict]) -> list[tuple[str, int]]:
	"""Each key record as (name, value), presses 1 and releases 0, kernel repeats left out."""
	return [(r["key"], r["value"]) for r in records if r["kind"] == "key" and r["value"] != 2]


def unitName(prefix: str, name: str) -> str:
	return prefix + "".join(c if c.isalnum() else "-" for c in name)


class FakeKeyboard:
	"""A keyboard made in the guest through uinput or uhid, by guest/fakeKeyboard.py running as root."""

	def __init__(self, ground: "Ground", name: str, kind: str = "uinput", *, started: bool = False) -> None:
		"""started says the keyboard is made and served already, by guest/poke.py race."""
		self.ground = ground
		self.name = name
		self.unit = unitName("ground-keyboard-", name)
		if not started:
			ground.machine.succeed(
				f"systemd-run --collect --unit={self.unit} ground-keyboard serve {shlex.quote(name)} {kind}"
			)
		ready = shlex.quote(f"{GROUND}/keyboards/{name}.ready")
		ground.waitFor(lambda: ground.machine.execute(f"test -e {ready}")[0] == 0, f"the keyboard {name}")
		# A key sent before the X server has the device is lost (Documentation/input/uinput.rst).
		ground.waitForDevice("evdev", name)
		ground.waitForDevice("x", name)

	def run(self, *steps: dict) -> list[dict]:
		"""Run steps in the guest, at the guest's own timing; answers the events written, with times."""
		output = self.ground.shell(
			f"ground-keyboard call {shlex.quote(self.name)} {shlex.quote(json.dumps(list(steps)))}"
		)
		return json.loads(output)["written"]

	def tap(self, *keys: str, hold: float = 0.03, gap: float = 0.05) -> list[dict]:
		"""Press and release each key in turn."""
		steps: list[dict] = []
		for key in keys:
			steps += [{"tap": key, "hold": hold}, {"sleep": gap}]
		return self.run(*steps)

	def chord(self, *keys: str, hold: float = 0.05) -> list[dict]:
		"""Press keys in order, hold them, release them in reverse."""
		return self.run({"chord": list(keys), "hold": hold})

	def vanish(self) -> None:
		"""Destroy the keyboard at once, with whatever keys it holds down."""
		self.run({"vanish": True})
		self.ground.waitForDevice("x", self.name, change="removed")

	def records(self, since: "Mark | None" = None) -> list[dict]:
		"""What the keyboard wrote, and for a uhid one what the kernel set its lights to."""
		records = self.ground.readJsonLines(f"{GROUND}/keyboards/{self.name}.jsonl")
		return [r for r in records if since is None or r["t"] >= since.t]


#: The keyboards' names, as the kernel and the X server give them: the helper's own, the guest's two
#: and the USB keyboard QEMU's monitor plugs in.
VIRTUAL_KEYBOARD = "Linda virtual keyboard"
VIRTIO_KEYBOARD = "QEMU Virtio Keyboard"
AT_KEYBOARD = "AT Translated Set 2 keyboard"
USB_KEYBOARD = "QEMU QEMU USB Keyboard"
#: The client's program, by the path every user's processes can run it from.
KEYD_CLIENT = "/run/current-system/sw/bin/ground-keyd-client"
KEYD_DIRECTORY = f"{GROUND}/keyd"
KEYD_UNIT = "linda-keyd.service"


class KeydClient:
	"""A client of linda-keyd, guest/keydClient.py, driven one step at a time.

	It runs as how says: "session", started by the X session's runner as a program of the session in
	front, as Linda is; "userManager", by the session user's systemd user manager, outside any
	session; or the name of a user, as a system service of that user's, outside any session.
	"""

	def __init__(self, ground: "Ground", name: str, how: str = "session") -> None:
		self.ground = ground
		self.name = name
		self.how = how
		self.unit = unitName("ground-keyd-", name)
		quoted = shlex.quote(name)
		if how == "session":
			ground.start(f"keyd-{name}", [KEYD_CLIENT, "serve", name])
		elif how == "userManager":
			ground.shell(
				f"systemd-run --machine={USER}@.host --user --collect --unit={self.unit} "
				f"{KEYD_CLIENT} serve {quoted}"
			)
		else:
			ground.shell(f"systemd-run --uid={how} --collect --unit={self.unit} {KEYD_CLIENT} serve {quoted}")
		socketPath = shlex.quote(f"{KEYD_DIRECTORY}/{name}.sock")
		ground.waitFor(lambda: ground.machine.execute(f"test -S {socketPath}")[0] == 0, f"the client {name}")

	def call(self, op: str, **fields: object) -> dict:
		request = json.dumps({"op": op, **fields})
		output = self.ground.shell(f"ground-keyd-client call {shlex.quote(self.name)} {shlex.quote(request)}")
		return json.loads(output)

	def connect(self, **fields: object) -> dict:
		"""Connect and say HELLO; answers {"welcome": ..., "goodbye": ...}, whichever came."""
		return self.call("connect", **fields)

	def start(self) -> None:
		self.call("start")

	def stop(self) -> None:
		self.call("stop")

	def verdicts(self, default: object = "pass", **keys: object) -> None:
		"""How the client answers each press: "pass", "keep", "never", or {"after": S, "keep": B}."""
		self.call("verdicts", default=default, keys=keys)

	def heartbeats(self, on: bool) -> None:
		self.call("heartbeats", on=on)

	@property
	def pid(self) -> int:
		return self.call("state")["pid"]

	def records(self, since: "Mark | None" = None, type: str | None = None) -> list[dict]:
		"""What the client got from the helper and sent it, as guest/keydClient.py records it."""
		records = self.ground.readJsonLines(f"{KEYD_DIRECTORY}/{self.name}.jsonl")
		return [
			r for r in records if (since is None or r["t"] >= since.t) and (type is None or r["type"] == type)
		]

	def waitFor(
		self,
		type: str,
		since: "Mark | None" = None,
		where: Callable[[dict], bool] = lambda _r: True,
		timeout: float = 10,
	) -> dict:
		"""The first record of type after since for which where holds, once there is one."""
		found: list[dict] = []

		def arrived() -> bool:
			nonlocal found
			found = [r for r in self.records(since, type) if where(r)]
			return bool(found)

		try:
			self.ground.waitFor(arrived, f"{type} at the client {self.name}", timeout)
		except AssertionError as e:
			raise AssertionError(f"{e}; it recorded {self.records(since)[-30:]}") from None
		return found[0]

	def waitForDevice(
		self, device: str, status: str, since: "Mark | None" = None, timeout: float = 10
	) -> dict:
		return self.waitFor(
			"DEVICE", since, lambda r: r["name"] == device and r["status"] == status, timeout=timeout
		)

	def keys(self, since: "Mark | None" = None) -> list[dict]:
		return self.records(since, "KEY")

	def capture(self, *devices: str, since: "Mark | None" = None) -> None:
		"""Connect if need be, START, and wait until the helper holds every keyboard named."""
		if not self.call("state")["connected"]:
			answer = self.connect()
			assert answer["welcome"], f"the client {self.name} was not welcomed: {answer}"
		self.start()
		for device in devices:
			self.waitForDevice(device, "HELD", since)


class Ground:
	"""The booted guest, its session, its recorders and the Linda a test started."""

	def __init__(self, machine: Any, subtest: Callable, linda: str, transcriptRecords: str) -> None:
		self.machine = machine
		self._subtest = subtest
		#: The package with the test hooks beside its source, as default.nix makes it.
		self.linda = linda
		self.transcriptRecords = _loadFile("_transcriptRecords", transcriptRecords)
		self.lindaStarted = False
		self.keyboards: list[FakeKeyboard] = []

	# The guest and its session.

	def boot(self) -> None:
		"""Start the guest and wait until the X session runs with marco and both recorders listen."""
		machine = self.machine
		machine.start()
		machine.wait_for_unit("ground-record-evdev.service")
		machine.wait_for_unit("display-manager.service")
		self.waitFor(
			lambda: self.machine.execute('ground-session call \'{"op": "ping"}\'')[0] == 0, "the session", 120
		)
		self.waitFor(
			lambda: "window id" in self.run(["xprop", "-root", "_NET_SUPPORTING_WM_CHECK"]).stdout,
			"marco to manage the screen",
			60,
		)
		self.start("recorder-x", ["ground-record", "x", LOGS["x"]])
		self.waitFor(lambda: any(r["kind"] == "ready" for r in self.records("x")), "the X recorder", 30)
		self.waitFor(
			lambda: any(r["kind"] == "ready" for r in self.records("evdev")), "the evdev recorder", 30
		)

	def shell(self, command: str) -> str:
		"""Run command in the guest as root and answer its output, failing when it fails, without the
		driver's log of it, which polling would fill."""
		status, output = self.machine.execute(command)
		if status != 0:
			raise AssertionError(f"{command} failed with status {status}: {output}")
		return output

	def session(self, request: dict) -> dict:
		"""Ask the runner inside the X session, guest/groundSession.py."""
		output = self.shell(f"ground-session call {shlex.quote(json.dumps(request))}")
		return json.loads(output)

	def start(self, name: str, argv: Sequence[str], env: dict[str, str] | None = None) -> int:
		"""Start a program in the X session as its user; its output goes to /tmp/ground/jobs/NAME.*."""
		return self.session({"op": "start", "name": name, "argv": list(argv), "env": env or {}})["pid"]

	def readJsonLines(self, path: str) -> list[dict]:
		"""Each record of a JSON Lines file in the guest, which may be written to as it is read, so that
		its last line may be half written and is then left out. Now and then a read comes back with a
		stretch of lines garbled or run together, which Linda's log and the file's own end, printed after
		the failure, had whole; such a read is made again."""
		for _attempt in range(READ_ATTEMPTS):
			text = self.shell(f"cat {shlex.quote(path)} 2>/dev/null || true")
			lines = [line for line in text.split("\n") if line.strip()]
			try:
				records = [json.loads(line) for line in lines[:-1]]
			except json.JSONDecodeError:
				continue
			if lines:
				try:
					records.append(json.loads(lines[-1]))
				except json.JSONDecodeError:
					pass
			return records
		raise AssertionError(f"{path} came back garbled {READ_ATTEMPTS} times running")

	def run(self, argv: Sequence[str], timeout: float = 30) -> subprocess.CompletedProcess:
		"""Run a short command in the X session as its user, and answer how it ended."""
		answer = self.session({"op": "run", "argv": list(argv), "timeout": timeout})
		return subprocess.CompletedProcess(argv, answer["returncode"], answer["stdout"], answer["stderr"])

	def running(self, name: str) -> bool:
		return self.session({"op": "status", "name": name})["returncode"] is None

	def now(self) -> float:
		return float(self.shell("ground-record now").strip())

	def mark(self) -> Mark:
		return Mark(len(self.spoken()) if self.lindaStarted else 0, self.now())

	def waitFor(self, condition: Callable[[], object], what: str, timeout: float = 10) -> Any:
		deadline = time.monotonic() + timeout
		while not (result := condition()):
			if time.monotonic() >= deadline:
				raise AssertionError(f"waited {timeout} s for {what}")
			time.sleep(0.1)
		return result

	def settle(self, seconds: float) -> None:
		"""Wait, before asserting that something did not happen."""
		time.sleep(seconds)

	@contextmanager
	def subtest(self, name: str) -> Iterator[None]:
		with self._subtest(name):
			yield

	@contextmanager
	def expectedFailure(self, fault: str) -> Iterator[None]:
		"""For a fault that exists today: the body asserts what should hold, and is expected to fail.

		A failure is logged and swallowed. A pass fails the test, so that the fault going away is seen
		and the body becomes an ordinary test.
		"""
		try:
			yield
		except AssertionError as e:
			self.machine.log(f"expected failure, the fault stands: {fault}\n{e}")
			return
		raise AssertionError(f"passed where it was expected to fail: {fault} no longer happens")

	# The test window.

	def openWindow(self, title: str = WINDOW_TITLE, *, name: str = "window", grab: bool = False) -> None:
		"""Start a test window, guest/testWindow.py, as the session's program name, and wait until its
		text field has the focus, and with grab, until it has taken the keyboard for itself."""
		mark = Mark(0, self.now())
		self.start(name, ["ground-window", LOGS["app"], title, *(["grab"] if grab else [])])
		self.waitFor(
			lambda: any(r["kind"] == "ready" for r in self.records("app", mark)), "the test window", 30
		)
		if grab:
			grabbed = self.waitFor(
				lambda: [r for r in self.records("app", mark) if r["kind"] == "grab"], "the keyboard grab"
			)
			assert grabbed[0]["status"] == "success", grabbed

	# Linda.

	def startLinda(
		self,
		settings: dict[str, dict[str, str]] | None = None,
		*,
		speaks: str = WINDOW_TITLE,
		gestures: str = "",
	) -> None:
		"""Start Linda in the session with the transcript synth and wait until it speaks speaks.

		settings are nvda.ini sections over BASE_SETTINGS; gestures, if given, is its gestures.ini.
		"""
		merged = {section: dict(values) for section, values in BASE_SETTINGS.items()}
		for section, values in (settings or {}).items():
			merged.setdefault(section, {}).update(values)
		lines = ["schemaVersion = 25"]
		for section, values in merged.items():
			lines.append(f"[{section}]")
			lines += [f"\t{key} = {value}" for key, value in values.items()]
		config = f"{LINDA_DIRECTORY}/config"
		self.machine.succeed(
			f"install -d -o {USER} {LINDA_DIRECTORY} {config}",
			f"echo {shlex.quote(chr(10).join(lines))} > {config}/nvda.ini",
			f"chown {USER} {config}/nvda.ini",
		)
		if gestures:
			self.machine.succeed(
				f"echo {shlex.quote(gestures)} > {config}/gestures.ini",
				f"chown {USER} {config}/gestures.ini",
			)
		self.start(
			"linda",
			[f"{self.linda}/bin/linda", "-c", config, "-f", f"{LINDA_DIRECTORY}/nvda.log", "--debug-logging"],
			env={
				"LINDA_TEST_HOOKS_DIR": f"{self.linda}/share/linda/tests/linux/testHooks",
				"LINDA_TRANSCRIPT_FILE": TRANSCRIPT,
				"LINDA_TRANSCRIPT_MODE": "immediate",
			},
		)
		self.lindaStarted = True
		self.expect(speaks, since=Mark(0, 0.0), timeout=120)

	def stopLinda(self) -> None:
		self.session({"op": "signal", "name": "linda", "signal": "SIGTERM"})
		self.waitFor(lambda: not self.running("linda"), "Linda to exit", 30)

	def signalLinda(self, signal: str) -> None:
		"""Send Linda a signal by its name, as "SIGSTOP"."""
		self.session({"op": "signal", "name": "linda", "signal": signal})

	def _transcript(self) -> list[dict]:
		return self.readJsonLines(TRANSCRIPT)

	def spoken(self) -> list[str]:
		"""The text of each of Linda's speak records so far."""
		return self.transcriptRecords.spokenTexts(self._transcript())

	def speech(self, since: Mark) -> list[tuple[float, str]]:
		"""Each of Linda's speak records after since, with the moment the synth was given it, on the
		guest's clock."""
		speak = [r for r in self._transcript() if r["kind"] == "speak"][since.spoken :]
		return [(r["t"], self.transcriptRecords.spokenText(r)) for r in speak]

	def waitForSpeech(self, expected: str, since: Mark, timeout: float = 10) -> float:
		"""The moment of the first speak record after since that is expected, once there is one."""
		found: list[float] = []

		def arrived() -> bool:
			found[:] = [t for t, text in self.speech(since) if _matches(expected, text)]
			return bool(found)

		try:
			self.waitFor(arrived, f"Linda to speak {expected!r}", timeout)
		except AssertionError as e:
			raise AssertionError(f"{e}; it spoke {self.speech(since)}\n{self.lindaLogTail()}") from None
		return found[0]

	def lindaLog(self) -> str:
		return self.shell(f"cat {LINDA_DIRECTORY}/nvda.log 2>/dev/null || true")

	def lindaLogRecords(self, text: str) -> list[tuple[float, str]]:
		"""Each record of Linda's log that holds text, with its time of day in seconds, as the log
		stamps it, to the millisecond."""
		log = self.lindaLog()
		headers = list(LOG_HEADER.finditer(log))
		found = []
		for index, header in enumerate(headers):
			end = headers[index + 1].start() if index + 1 < len(headers) else len(log)
			record = log[header.start() : end]
			if text in record:
				hours, minutes, seconds = header.group("time").split(":")
				found.append((int(hours) * 3600 + int(minutes) * 60 + float(seconds), record.strip()))
		return found

	def waitForLindaLog(self, text: str, count: int = 1, timeout: float = 10) -> tuple[float, str]:
		"""The count-th record of Linda's log that holds text, once there is one."""
		found: list[tuple[float, str]] = []

		def arrived() -> bool:
			found[:] = self.lindaLogRecords(text)
			return len(found) >= count

		try:
			self.waitFor(arrived, f"Linda to log {text!r} {count} times", timeout)
		except AssertionError as e:
			raise AssertionError(f"{e}; it logged it {len(found)} times\n{self.lindaLogTail()}") from None
		return found[count - 1]

	def expect(self, *texts: str, since: Mark, timeout: float = 10) -> None:
		"""Wait until Linda has spoken texts, in this order, after since.

		Each text must be a whole speak record, or its start when it ends with "…".
		"""
		deadline = time.monotonic() + timeout
		while True:
			spoken = self.spoken()[since.spoken :]
			if _inOrder(texts, spoken):
				return
			if not self.running("linda") or time.monotonic() >= deadline:
				raise AssertionError(
					f"Linda did not speak {list(texts)} in order within {timeout} s; it spoke {spoken}\n"
					f"running: {self.running('linda')}\n{self.lindaLogTail()}"
				)
			time.sleep(0.1)

	def lindaLogTail(self, size: int = 6000) -> str:
		return self.shell(f"tail -c {size} {LINDA_DIRECTORY}/nvda.log 2>/dev/null || true")

	# Keys.

	def press(self, *chords: str, gap: float = 0.15) -> None:
		"""Press each chord in turn on the emulated keyboard, by QEMU's sendkey names."""
		for chord in chords:
			self.machine.send_key(chord, log=False)
			time.sleep(gap)

	def type(self, text: str) -> None:
		"""Type text on the emulated keyboard, one key at a time."""
		self.machine.send_chars(text)

	def keyboard(self, name: str, kind: str = "uinput") -> FakeKeyboard:
		"""Make a fake keyboard in the guest, ready once the kernel and the X server both have it."""
		keyboard = FakeKeyboard(self, name, kind)
		self.keyboards.append(keyboard)
		return keyboard

	def qemuKey(self, key: str, down: bool) -> None:
		"""Press or release one key on QEMU's own keyboard, by its QKeyCode name, as "shift": unlike
		press, which always releases, a key can be left down."""
		event = {"type": "key", "data": {"down": down, "key": {"type": "qcode", "data": key}}}
		result = self.machine.qmp_client.send("input-send-event", {"events": [event]})
		assert "error" not in result, result

	def plugUsbKeyboard(self, id: str = "usbkbd") -> None:
		"""Plug a USB keyboard into the guest, which then has QEMU's keys, until it is unplugged."""
		self.machine.send_monitor_command(f"device_add usb-kbd,id={id},bus=usb-bus.0")
		self.waitForDevice("x", USB_KEYBOARD)

	def unplugUsbKeyboard(self, id: str = "usbkbd") -> None:
		self.machine.send_monitor_command(f"device_del {id}")
		self.waitForDevice("x", USB_KEYBOARD, change="removed")

	def uhidKeyboard(self, name: str) -> FakeKeyboard:
		"""Make a keyboard through the guest's uhid, which the key capture helper takes."""
		return self.keyboard(name, "uhid")

	# The key capture helper.

	def keydClient(self, name: str, how: str = "session") -> KeydClient:
		return KeydClient(self, name, how)

	def helperPid(self) -> int:
		return int(self.shell(f"systemctl show -P MainPID {KEYD_UNIT}").strip())

	def helperRestarts(self) -> int:
		return int(self.shell(f"systemctl show -P NRestarts {KEYD_UNIT}").strip())

	def waitForHelper(self, timeout: float = 30) -> int:
		"""Wait until the helper runs; answers its pid."""
		self.waitFor(
			lambda: self.shell(f"systemctl is-active {KEYD_UNIT} || true").strip() == "active",
			"linda-keyd to run",
			timeout,
		)
		return self.helperPid()

	def helperLog(self, since: Mark | None = None) -> list[tuple[float, str]]:
		"""What the helper's unit put in the journal, each line with its time on the guest's clock."""
		text = self.shell(f"journalctl -o json --no-pager -u {KEYD_UNIT} 2>/dev/null || true")
		lines = []
		for line in text.splitlines():
			if not line.strip():
				continue
			entry = json.loads(line)
			t = int(entry["__MONOTONIC_TIMESTAMP"]) / 1e6
			message = entry.get("MESSAGE")
			if isinstance(message, list):
				message = bytes(message).decode(errors="replace")
			if since is None or t >= since.t:
				lines.append((t, message or ""))
		return lines

	def waitForHelperLog(
		self, text: str, since: Mark | None = None, timeout: float = 10
	) -> tuple[float, str]:
		"""The first line of the helper's after since that holds text, once there is one."""
		found: list[tuple[float, str]] = []

		def arrived() -> bool:
			nonlocal found
			found = [line for line in self.helperLog(since) if text in line[1]]
			return bool(found)

		try:
			self.waitFor(arrived, f"the helper to say {text!r}", timeout)
		except AssertionError as e:
			said = "\n".join(message for _t, message in self.helperLog(since)[-30:])
			raise AssertionError(f"{e}; it said:\n{said}") from None
		return found[0]

	# The recorders.

	def records(self, source: str, since: Mark | None = None) -> list[dict]:
		"""Every record of source ("evdev", "x" or "app") read after since."""
		records = self.readJsonLines(LOGS[source])
		return [r for r in records if since is None or r["received"] >= since.t]

	def keys(self, source: str, since: Mark, *, device: str | None = None) -> list[dict]:
		"""The key records of source after since, of the device named device if given."""
		return [
			r
			for r in self.records(source, since)
			if r["kind"] == "key" and (device is None or r["device"] == device)
		]

	def waitForKeys(
		self,
		source: str,
		expected: Sequence[tuple[str, int]],
		since: Mark,
		*,
		device: str | None = None,
		timeout: float = 5,
	) -> list[dict]:
		"""Wait until source has recorded expected, as (key, value) in this order with nothing else between."""
		found: list[dict] = []

		def arrived() -> bool:
			nonlocal found
			found = self.keys(source, since, device=device)
			return keyEvents(found)[: len(expected)] == list(expected)

		try:
			self.waitFor(arrived, f"{list(expected)} from {source}", timeout)
		except AssertionError as e:
			raise AssertionError(f"{e}; it recorded {keyEvents(found)}") from None
		return found

	def waitForDevice(self, source: str, name: str, *, change: str = "added", timeout: float = 10) -> None:
		self.waitFor(
			lambda: any(
				r["kind"] == "device" and r["device"] == name and r["change"] == change
				for r in self.records(source)
			),
			f"{source} to see {name} {change}",
			timeout,
		)

	def delays(self, earlier: list[dict], later: list[dict]) -> list[tuple[str, int, float]]:
		"""For each key record in later, the seconds since the same key and value in earlier, matched
		in order: how long a key took from one layer to the next. A fake keyboard's written events
		count as records."""
		pending = [r for r in earlier if r.get("kind", "key") == "key"]
		result = []
		for record in later:
			if record.get("kind", "key") != "key":
				continue
			for index, candidate in enumerate(pending):
				if (candidate["code"], candidate["value"]) == (record["code"], record["value"]):
					result.append((record["key"], record["value"], record["t"] - candidate["t"]))
					del pending[: index + 1]
					break
		return result

	# The end.

	def diagnostics(self) -> str:
		"""What is worth reading when a test fails: the session's jobs, Linda's log, the recorders, and
		the helper's journal and its clients' records."""
		return self.machine.execute(
			f'for f in {GROUND}/session.log {GROUND}/jobs/*; do echo "== $f"; tail -c 3000 "$f"; done; '
			f"echo '== nvda.log'; tail -c 8000 {LINDA_DIRECTORY}/nvda.log; "
			f'for f in {" ".join(LOGS.values())}; do echo "== $f"; tail -n 40 "$f"; done; '
			f'for f in {KEYD_DIRECTORY}/*.jsonl; do echo "== $f"; tail -n 40 "$f"; done; '
			f"echo '== {KEYD_UNIT}'; journalctl -o short-monotonic --no-pager -u {KEYD_UNIT} | tail -n 80",
		)[1]


def main(
	machine: Any, subtest: Callable, case: str, linda: str, transcriptRecords: str, harness: str
) -> None:
	"""Boot the guest and run the case at path case, then hold Linda's log to what a clean run of the
	tests under tests/linux is held to.

	Nothing is copied out of the guest, so that a passing check's output is the same each time and
	nix build --rebuild runs it again; a failure writes what is worth reading to the log.
	"""
	ground = Ground(machine, subtest, linda, transcriptRecords)
	module = _loadFile("case", case)
	try:
		ground.boot()
		module.run(ground)
		if ground.lindaStarted:
			errors = _loadFile("harness", harness).nvdaLogErrorRecords(ground.lindaLog())
			whole = [record for _t, record in ground.lindaLogRecords("") if record.startswith(tuple(errors))]
			assert not errors, f"Linda's log holds {len(errors)} records at ERROR or above:\n" + "\n".join(
				whole or errors
			)
	except BaseException:
		machine.log(ground.diagnostics())
		raise
