"""Callers in a sandboxed program's scope are turned away: a process the session starts in a scope named
as flatpak names its apps', or as snapd names its snaps', is refused as not authorized before logind or
polkit is asked, and nothing is grabbed. Only the last part of the control group path counts, so a
scope whose name merely holds the same words, or that sits in slices named with them, is welcomed as
any program of the user's is.
"""

import shlex
from typing import TYPE_CHECKING

from ground import KEYD_CLIENT, KEYD_DIRECTORY, KeydClient

if TYPE_CHECKING:
	from ground import Ground

UHID = "Ground uhid keyboard"
SYSTEMD_RUN = "/run/current-system/sw/bin/systemd-run"


class ScopedClient(KeydClient):
	"""A client the X session starts in a scope of its user's systemd manager, as a desktop starts a
	Flatpak app."""

	def __init__(self, ground: "Ground", name: str, *options: str) -> None:
		self.ground = ground
		self.name = name
		self.how = "scope"
		self.unit = ""
		ground.start(
			f"keyd-{name}",
			[
				SYSTEMD_RUN,
				"--user",
				"--scope",
				"--quiet",
				"--collect",
				*options,
				"--",
				KEYD_CLIENT,
				"serve",
				name,
			],
		)
		socketPath = shlex.quote(f"{KEYD_DIRECTORY}/{name}.sock")
		ground.waitFor(lambda: ground.machine.execute(f"test -S {socketPath}")[0] == 0, f"the client {name}")

	def cgroup(self) -> str:
		return self.ground.shell(f"cat /proc/{self.pid}/cgroup").strip()


def nothingHeld(ground: "Ground", keyboard) -> None:
	mark = ground.mark()
	keyboard.tap("KEY_A")
	ground.waitForKeys("evdev", [("KEY_A", 1), ("KEY_A", 0)], mark, device=UHID)
	assert "capturing" not in " ".join(line for _t, line in ground.helperLog())


def run(ground: "Ground") -> None:
	ground.openWindow()
	ground.waitForHelper()
	keyboard = ground.uhidKeyboard(UHID)

	for name, unit in (
		("flatpak", "app-flatpak-test-1"),
		("snap", "snap.test.test-1"),
	):
		with ground.subtest(f"a caller in a scope named as {name} names its own is turned away"):
			client = ScopedClient(ground, name, f"--unit={unit}")
			cgroup = client.cgroup()
			ground.machine.log(f"{name}'s control group: {cgroup}")
			assert cgroup.endswith(f"/{unit}.scope"), cgroup
			mark = ground.mark()
			answer = client.connect()
			assert answer["welcome"] is None and answer["goodbye"], answer
			goodbye = answer["goodbye"]
			ground.machine.log(f"{name}: {goodbye['reason']}, {goodbye['text']!r}")
			assert goodbye["reason"] == "NOT_AUTHORIZED", goodbye
			assert f"{unit}.scope" in goodbye["text"] and "sandboxed" in goodbye["text"], goodbye
			said = [line for _t, line in ground.helperLog(mark)]
			assert any(f"turned away process {client.pid} " in line for line in said), said
			nothingHeld(ground, keyboard)

	for name, options in (
		("inName", ["--unit=ground-app-flatpak-test-2"]),
		("inSlice", ["--unit=ground-sliced-3", "--slice=app-flatpak-test"]),
	):
		with ground.subtest(f"a caller with the words elsewhere in its path is welcomed ({name})"):
			client = ScopedClient(ground, name, *options)
			cgroup = client.cgroup()
			ground.machine.log(f"{name}'s control group: {cgroup}")
			assert "app-flatpak-" in cgroup, cgroup
			assert not cgroup.rsplit("/", 1)[1].startswith("app-flatpak-"), cgroup
			answer = client.connect()
			assert answer["welcome"], answer
			client.capture(UHID)
			mark = ground.mark()
			keyboard.tap("KEY_A")
			key = client.waitFor("KEY", mark)
			assert (key["key"], key["value"]) == ("KEY_A", 1), key
			client.call("close")
