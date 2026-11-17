"""Web pages for browse-mode tests: the pages, Firefox started on one with a fresh profile, its
documents found on the accessibility desktop, and what the keys page logged.

Inside the rig only. Firefox runs in a process group of its own, led by the process this starts;
the desktop session's close stops that process, and waits for or kills the rest of the group
through it, so that no content process outlives the test.
"""

import html
import json
import os
import re
import signal
import subprocess
import tempfile
import time
from pathlib import Path

import desktopSession
import harness
import pytest
from desktopSession import DesktopSession

#: The pages the tests open.
PAGES = harness.HERE / "fixtures" / "web"
#: The NVDA configuration for reading web pages: the session configuration, with the change between
#: browse and focus mode spoken rather than played as a sound.
BROWSE_CONFIG = harness.HERE / "fixtures" / "browseConfig" / "nvda.ini"
#: The preferences every fresh profile starts with: no first-run pages, no updates, no network.
USER_JS = harness.HERE / "bench" / "user.js"
#: The variables naming the buffer's storage library and its renderer, which browse mode needs.
BROWSE_LIBRARY_VARIABLES = ("BASEDREADER_VBUF_LIB", "BASEDREADER_VBUF_ENGINE")
#: Seconds from starting Firefox to its window on the accessibility desktop; a first start under
#: the rig takes a few.
FIREFOX_WINDOW_TIMEOUT = 60.0
#: Seconds the window then has to take the keyboard.
ACTIVATE_TIMEOUT = 30.0
#: The keys page's title and the label of the element it logs keys into.
KEYS_PAGE_TITLE = "Keys page"
KEY_LOG_LABEL = "Key log"
KEY_LOG_ID = "log"
#: Seconds pageKeyLog waits for the log to be on the accessibility desktop.
KEY_LOG_TIMEOUT = 10.0
#: Seconds Firefox takes to fill a part of its accessibility cache that a client asked for the first
#: time; until then the answer from that part is empty, and nothing announces when it is filled.
CACHE_FILL_SECONDS = 0.5

_primedApplications: set[int] = set()


def pageURL(name: str) -> str:
	"""The address of the page name under PAGES, or name itself when it is already an address."""
	if re.match(r"[a-z][a-z0-9+.-]*:", name):
		return name
	path = PAGES / name
	assert path.is_file(), f"no page {name} in {PAGES}"
	return path.as_uri()


def _pageTitle(name: str) -> str | None:
	"""The title the page name under PAGES gives itself, or None when it is not one of them."""
	path = PAGES / name
	if re.match(r"[a-z][a-z0-9+.-]*:", name) or not path.is_file():
		return None
	found = re.search(r"<title>(.*?)</title>", path.read_text(encoding="utf-8"), re.DOTALL)
	return html.unescape(found[1].strip()) if found else None


def _stat(pid: int) -> tuple[str, int, int, int] | None:
	"""State, parent, process group and start time of a process, or None when it is gone."""
	try:
		text = Path(f"/proc/{pid}/stat").read_text()
	except OSError:
		return None
	fields = text.rsplit(")", 1)[1].split()
	return fields[0], int(fields[1]), int(fields[2]), int(fields[19])


class FirefoxProcess(subprocess.Popen):
	"""A Firefox that leads a process group of its own.

	poll answers None while any process of the group, or any process it started, is still running,
	so that a caller who stops it by its process ID waits for its content processes too. SIGTERM
	goes to Firefox alone, which shuts its children down; kill sends SIGKILL to all of them.
	"""

	#: The profile directory it was started with.
	profile: Path
	#: The name of its window when it took the keyboard.
	window: str
	#: time.monotonic() when that window was first seen on the accessibility desktop.
	shown: float

	def __init__(self, *args, **kwargs) -> None:
		super().__init__(*args, start_new_session=True, **kwargs)
		#: (process ID, start time) of each process seen in the group or below it, so that one that
		#: left the group is still found after Firefox has gone and it was given another parent.
		self._seen: set[tuple[int, int]] = set()

	def liveProcesses(self) -> list[int]:
		"""The process IDs of every process of this Firefox that is still running, Firefox's own
		included until it is reaped."""
		children: dict[int, list[tuple[int, int]]] = {}
		for entry in Path("/proc").iterdir():
			if not entry.name.isdecimal():
				continue
			stat = _stat(int(entry.name))
			if stat is None or stat[0] in "ZX":
				continue
			_state, parent, group, started = stat
			children.setdefault(parent, []).append((int(entry.name), started))
			if group == self.pid:
				self._seen.add((int(entry.name), started))
		pending = [self.pid]
		while pending:
			for child in children.get(pending.pop(), []):
				self._seen.add(child)
				pending.append(child[0])
		live = []
		for pid, started in list(self._seen):
			stat = _stat(pid)
			if stat is None or stat[0] in "ZX" or stat[3] != started:
				self._seen.discard((pid, started))
			else:
				live.append(pid)
		return sorted(live)

	def poll(self) -> int | None:
		code = super().poll()
		if code is not None and self.liveProcesses():
			return None
		return code

	def firefoxHasExited(self) -> bool:
		"""Whether the process this started has exited, whatever its children are doing."""
		return super().poll() is not None

	def send_signal(self, sig: int) -> None:
		live = self.liveProcesses()
		if not self.firefoxHasExited():
			os.kill(self.pid, sig)
			return
		for pid in live:
			try:
				os.kill(pid, sig)
			except ProcessLookupError:
				pass

	def kill(self) -> None:
		for pid in self.liveProcesses():
			try:
				os.kill(pid, signal.SIGKILL)
			except ProcessLookupError:
				pass

	def wait(self, timeout: float | None = None) -> int:
		deadline = None if timeout is None else time.monotonic() + timeout
		code = super().wait(timeout)
		while self.liveProcesses():
			if deadline is not None and time.monotonic() >= deadline:
				raise subprocess.TimeoutExpired(self.args, timeout)
			time.sleep(0.05)
		return code


def _application(process: FirefoxProcess):
	"""The accessibility desktop's application for process, or None while it has none."""
	Atspi = desktopSession._atspi()
	from gi.repository import GLib

	desktop = Atspi.get_desktop(0)
	desktop.clear_cache()
	ours = set(process.liveProcesses())
	for index in range(desktop.get_child_count()):
		try:
			app = desktop.get_child_at_index(index)
			if app is not None and app.get_process_id() in ours:
				return app
		except GLib.Error:
			continue
	return None


def _windowNames(app) -> list[str]:
	from gi.repository import GLib

	try:
		app.clear_cache()
		return [
			window.get_name() or ""
			for index in range(app.get_child_count())
			if (window := app.get_child_at_index(index)) is not None
		]
	except GLib.Error:
		return []


def startFirefox(
	desktop: DesktopSession,
	page: str,
	*,
	prefs: dict | None = None,
) -> subprocess.Popen:
	"""Start Firefox on page, a name under PAGES or an address, with a fresh profile made from
	tests/linux/bench/user.js and prefs, and wait until its window is on the accessibility desktop
	and has the keyboard.

	For a page under PAGES the window waited for is the one named after the page's title, so the
	page has at least begun to load; for an address, any window of this Firefox. desktop's close
	stops it, its content processes with it, by the process ID this started. Its output goes to a
	file beside the profile.

	:return: A FirefoxProcess, whose profile, window and shown say where its profile is, what its
		window was called and when it appeared.
	"""
	profile = Path(tempfile.mkdtemp(prefix="firefox", dir=desktop.directory))
	userPrefs = USER_JS.read_text()
	for name, value in (prefs or {}).items():
		userPrefs += f"user_pref({json.dumps(name)}, {json.dumps(value)});\n"
	(profile / "user.js").write_text(userPrefs)
	logPath = profile.with_name(f"{profile.name}.log")
	command = ["firefox", "--no-remote", "--new-instance", "--profile", str(profile), pageURL(page)]
	with open(logPath, "w") as log:
		process = FirefoxProcess(
			command,
			stdin=subprocess.DEVNULL,
			stdout=log,
			stderr=subprocess.STDOUT,
			env=dict(os.environ, MOZ_CRASHREPORTER_DISABLE="1"),
		)
	process.profile = profile
	# The session's close stops what it started, last first, by process ID.
	desktop._apps.append(process)
	title = _pageTitle(page)
	started = time.monotonic()
	names: list[str] = []
	while True:
		app = _application(process)
		names = _windowNames(app) if app is not None else []
		window = next((name for name in names if name and (title is None or name.startswith(title))), None)
		if window is not None:
			process.shown = time.monotonic()
			break
		if process.firefoxHasExited() or time.monotonic() - started >= FIREFOX_WINDOW_TIMEOUT:
			raise AssertionError(
				f"Firefox showed no window for {page!r} within {FIREFOX_WINDOW_TIMEOUT} s, "
				f"{harness.describeExit(process.returncode)}; its windows: {names}\n"
				f"{logPath.read_text(errors='replace')[-3000:]}",
			)
		time.sleep(0.1)
	desktopSession.activate(window, timeout=ACTIVATE_TIMEOUT)
	process.window = window
	return process


def _documents(app) -> list:
	Atspi = desktopSession._atspi()
	rule = Atspi.MatchRule.new(
		None,
		Atspi.CollectionMatchType.ALL,
		None,
		Atspi.CollectionMatchType.ALL,
		[Atspi.Role.DOCUMENT_WEB],
		Atspi.CollectionMatchType.ANY,
		None,
		Atspi.CollectionMatchType.ALL,
		False,
	)
	return list(
		Atspi.Collection.get_matches(app, rule, Atspi.CollectionSortOrder.CANONICAL, 0, True) or [],
	)


def _geckoApplications() -> list:
	Atspi = desktopSession._atspi()
	from gi.repository import GLib

	desktop = Atspi.get_desktop(0)
	desktop.clear_cache()
	found = []
	for index in range(desktop.get_child_count()):
		try:
			app = desktop.get_child_at_index(index)
			if app is not None and app.get_toolkit_name() == "Gecko":
				found.append(app)
		except GLib.Error:
			continue
	return found


def _findDocument(title: str, *, ready: bool):
	"""A document web named title in any Firefox, not busy when ready is True; with what was seen."""
	Atspi = desktopSession._atspi()
	from gi.repository import GLib

	seen = []
	for app in _geckoApplications():
		try:
			documents = _documents(app)
		except GLib.Error:
			continue
		for document in documents:
			try:
				# libatspi keeps names and states from events that only a main loop delivers.
				document.clear_cache()
				name = document.get_name() or ""
				busy = document.get_state_set().contains(Atspi.StateType.BUSY)
			except GLib.Error:
				continue
			seen.append(f"{name} (busy)" if busy else name)
			if name == title and not (ready and busy):
				return document, seen
	return None, seen


def waitForDocument(title: str, timeout: float = 30) -> None:
	"""Wait until a Firefox on the rig's desktop has a document web named title that is not busy."""
	deadline = time.monotonic() + timeout
	while True:
		document, seen = _findDocument(title, ready=True)
		if document is not None:
			return
		assert time.monotonic() < deadline, (
			f"no document {title!r} ready within {timeout} s; documents: {seen}"
		)
		time.sleep(0.1)


def _keyLog(node, depth: int = 0):
	"""The keys page's log element at or under node, or None."""
	from gi.repository import GLib

	try:
		attributes = node.get_attributes() or {}
		if attributes.get("id") == KEY_LOG_ID or node.get_name() == KEY_LOG_LABEL:
			return node
		count = node.get_child_count()
	except GLib.Error:
		return None
	if depth > 20:
		return None
	for index in range(count):
		try:
			child = node.get_child_at_index(index)
		except GLib.Error:
			continue
		if child is not None and (found := _keyLog(child, depth + 1)) is not None:
			return found
	return None


def pageKeyLog() -> list[str]:
	"""What keys.html logged, read over the accessibility bus from this process: one entry for each
	key press that reached the page, in order.

	An entry is the key's name as the browser gives it ("a", "H", "Enter", "ArrowDown"), "Space" for
	the space bar, with "Control+", "Alt+" and "Meta+" before it for those held, and "Shift+" before
	a named key with Shift held. Modifiers pressed alone are not logged. Auto-repeated presses are.
	"""
	Atspi = desktopSession._atspi()
	deadline = time.monotonic() + KEY_LOG_TIMEOUT
	while True:
		document, seen = _findDocument(KEYS_PAGE_TITLE, ready=False)
		log = _keyLog(document) if document is not None else None
		if log is not None:
			break
		assert time.monotonic() < deadline, (
			f"no key log on {KEYS_PAGE_TITLE!r} within {KEY_LOG_TIMEOUT} s; documents: {seen}"
		)
		time.sleep(CACHE_FILL_SECONDS)
	application = log.get_application()
	pid = application.get_process_id() if application is not None else 0
	if pid not in _primedApplications:
		Atspi.Text.get_text(log, 0, -1)
		time.sleep(CACHE_FILL_SECONDS)
		_primedApplications.add(pid)
	return Atspi.Text.get_text(log, 0, -1).split()


def requireBrowseLibraries() -> None:
	"""Skip the test unless both variables naming browse mode's libraries are set, and fail it when
	one names no file."""
	missing = [name for name in BROWSE_LIBRARY_VARIABLES if not os.environ.get(name)]
	if missing:
		pytest.skip(f"browse mode needs {' and '.join(missing)} naming its built libraries")
	for name in BROWSE_LIBRARY_VARIABLES:
		path = Path(os.environ[name])
		assert path.is_file(), f"{name} names {path}, which is not a file"
