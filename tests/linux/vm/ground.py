"""What a key capture test in cases/ is written with: pressing keys, hearing Linda and reading the
recorders, in the guest guest.nix describes.

A case is a file in cases/ with a function run(ground), which default.nix makes a flake check of its
own (with cases/NAME.nix, if there is one, as a NixOS module added to its guest); it runs in the NixOS test driver, on the host, and reaches the guest through the driver's
machine. Everything that happens in the guest is stamped with the guest's CLOCK_MONOTONIC, which
Ground.mark reads, so a test asks for what happened after a mark.

Keys come two ways. Ground.press sends QEMU's own key events, which reach the guest kernel as the
emulated keyboard's; names are QEMU's sendkey names, as "insert-t", "f10", "ret". Ground.keyboard
makes a fake keyboard inside the guest through its uinput; names are evdev's, as "KEY_T".

Linda is heard as the tests under tests/linux hear it: through the transcript synth, which the test
hooks give the package, and whose records synthDrivers/_transcriptRecords.py reads.

The recorders say what reached each layer, key by key: "evdev" what the kernel gave an ordinary
reader of each keyboard node (what the X server's libinput reads), "x" what the X server received,
"app" what reached the test window. Each record is a dict as guest/keyRecorder.py describes.
"""

import importlib.util
import json
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


class FakeKeyboard:
	"""A keyboard made in the guest through uinput, by guest/fakeKeyboard.py running as root."""

	def __init__(self, ground: "Ground", name: str) -> None:
		self.ground = ground
		self.name = name
		self.unit = "ground-keyboard-" + "".join(c if c.isalnum() else "-" for c in name)
		ground.machine.succeed(
			f"systemd-run --collect --unit={self.unit} ground-keyboard serve {shlex.quote(name)}"
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

	def openWindow(self, title: str = WINDOW_TITLE) -> None:
		"""Start the test window, guest/testWindow.py, and wait until its text field has the focus."""
		self.start("window", ["ground-window", LOGS["app"], title])
		self.waitFor(lambda: any(r["kind"] == "ready" for r in self.records("app")), "the test window", 30)

	# Linda.

	def startLinda(
		self, settings: dict[str, dict[str, str]] | None = None, *, speaks: str = WINDOW_TITLE
	) -> None:
		"""Start Linda in the session with the transcript synth and wait until it speaks speaks.

		settings are nvda.ini sections over BASE_SETTINGS.
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

	def spoken(self) -> list[str]:
		"""The text of each of Linda's speak records so far."""
		text = self.shell(f"cat {TRANSCRIPT} 2>/dev/null || true")
		records = [json.loads(line) for line in text.splitlines() if line.strip()]
		return self.transcriptRecords.spokenTexts(records)

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

	def keyboard(self, name: str) -> FakeKeyboard:
		"""Make a fake keyboard in the guest, ready once the kernel and the X server both have it."""
		keyboard = FakeKeyboard(self, name)
		self.keyboards.append(keyboard)
		return keyboard

	# The recorders.

	def records(self, source: str, since: Mark | None = None) -> list[dict]:
		"""Every record of source ("evdev", "x" or "app") read after since."""
		text = self.shell(f"cat {LOGS[source]} 2>/dev/null || true")
		records = [json.loads(line) for line in text.splitlines() if line.strip()]
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
		"""What is worth reading when a test fails: the session's jobs, Linda's log, the recorders."""
		return self.machine.execute(
			f'for f in {GROUND}/session.log {GROUND}/jobs/*; do echo "== $f"; tail -c 3000 "$f"; done; '
			f"echo '== nvda.log'; tail -c 8000 {LINDA_DIRECTORY}/nvda.log; "
			f'for f in {" ".join(LOGS.values())}; do echo "== $f"; tail -n 40 "$f"; done',
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
			errors = _loadFile("harness", harness).nvdaLogErrorRecords(
				ground.shell(f"cat {LINDA_DIRECTORY}/nvda.log")
			)
			assert not errors, f"Linda's log holds {len(errors)} records at ERROR or above: {errors}"
	except BaseException:
		machine.log(ground.diagnostics())
		raise
