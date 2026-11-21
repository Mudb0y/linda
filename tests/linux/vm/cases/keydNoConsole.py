"""A session logind places on no console, on a machine whose kernel has consoles, as logind does
when it finds none on seat0: the helper captures for a client of it, and what is typed reaches the
X server through the virtual keyboard. A switch to a text console lets go at once and the console
has the keyboard; the switch back takes it again; logind, knowing of no consoles, says nothing of
either, and saying the same session again does not take the keyboard while the text console is in
front.
"""

import shlex
from typing import TYPE_CHECKING

from ground import KEYD_CLIENT, KEYD_DIRECTORY, USER, VIRTIO_KEYBOARD, VIRTUAL_KEYBOARD, KeydClient, unitName

if TYPE_CHECKING:
	from ground import Ground

UHID = "Ground uhid keyboard"
CONSOLE_TEXT = "/tmp/ground/console.txt"
#: SESSION_RECHECK in protocol.rs, how often the helper asks logind again.
SESSION_RECHECK = 2


class NoConsoleClient(KeydClient):
	"""A client in a logind session of its own on seat0, of type wayland, which logind places on no
	console and puts in front at once, as it does each new session of a seat with no consoles."""

	def __init__(self, ground: "Ground", name: str) -> None:
		self.ground = ground
		self.name = name
		self.how = "noConsole"
		self.unit = unitName("ground-keyd-", name)
		properties = [
			"PAMName=login",
			f"User={USER}",
			"Environment=XDG_SEAT=seat0",
			"Environment=XDG_SESSION_TYPE=wayland",
			"Environment=XDG_SESSION_CLASS=user",
		]
		ground.shell(
			f"systemd-run --collect --unit={self.unit} "
			+ " ".join(f"-p {shlex.quote(p)}" for p in properties)
			+ f" {KEYD_CLIENT} serve {shlex.quote(name)}"
		)
		socketPath = shlex.quote(f"{KEYD_DIRECTORY}/{name}.sock")
		ground.waitFor(lambda: ground.machine.execute(f"test -S {socketPath}")[0] == 0, f"the client {name}")


def sessionOf(ground: "Ground", pid: int) -> str:
	for line in ground.shell(f"cat /proc/{pid}/cgroup").splitlines():
		for part in line.split("/"):
			if part.startswith("session-") and part.endswith(".scope"):
				return part.removeprefix("session-").removesuffix(".scope")
	raise AssertionError(f"process {pid} is in no session")


def properties(ground: "Ground", session: str) -> dict[str, str]:
	text = ground.shell(f"loginctl show-session {session} -p VTNr -p Seat -p Active -p Class -p Type")
	return dict(line.split("=", 1) for line in text.splitlines() if "=" in line)


def run(ground: "Ground") -> None:
	ground.openWindow()
	ground.waitForHelper()
	keyboard = ground.uhidKeyboard(UHID)
	desktop = int(ground.shell("fgconsole").strip())
	# With logind saying seat0 has no consoles, X is not on console 7 but on whichever it was given.
	console = next(n for n in (3, 4, 5) if n != desktop)

	with ground.subtest("logind puts a session on no console in front, with the kernel's consoles there"):
		canTty = ground.shell("loginctl show-seat seat0 -P CanTTY").strip()
		assert canTty == "no", f"logind found consoles on seat0: CanTTY={canTty}"
		mark = ground.mark()
		client = NoConsoleClient(ground, "main")
		session = sessionOf(ground, client.pid)
		said = properties(ground, session)
		ground.machine.log(f"session {session}: {said}; console {desktop} is in front")
		assert said["VTNr"] == "0" and said["Seat"] == "seat0" and said["Class"] == "user", said
		ground.waitFor(
			lambda: ground.shell("loginctl show-seat seat0 -P ActiveSession").strip() == session,
			"the session to be in front",
		)
		front = ground.waitForHelperLog("in front on seat0", mark)[1]
		ground.machine.log(f"the helper: {front}")

	with ground.subtest("the helper captures for it, and what is typed reaches X"):
		mark = ground.mark()
		client.capture(UHID, VIRTIO_KEYBOARD)
		ground.machine.log(f"held {ground.now() - mark.t:.3f} s after the mark")
		ground.settle(1)
		mark = ground.mark()
		keyboard.tap("KEY_Q")
		typed = [("KEY_Q", 1), ("KEY_Q", 0)]
		ground.waitForKeys("x", typed, mark, device=VIRTUAL_KEYBOARD)
		assert [k["key"] for k in client.keys(mark) if k["value"] == 1] == ["KEY_Q"], client.keys(mark)

	with ground.subtest("a text console in front lets go at once and has the keyboard"):
		ground.shell(
			f"openvt -c {console} -- sh -c 'stty raw -echo; exec cat > {CONSOLE_TEXT}'; "
			f"until [ -e {CONSOLE_TEXT} ]; do sleep 0.05; done"
		)
		mark = ground.mark()
		ground.shell(f"chvt {console}")
		released = client.waitFor("RELEASED", mark)
		assert released["reason"] == "BACKGROUND", released
		ground.machine.log(f"let go {released['t'] - mark.t:.3f} s after the switch")
		said = ground.waitForHelperLog(f"console {console} is in front", mark)[1]
		ground.machine.log(f"the helper: {said}")
		assert ground.shell("loginctl show-seat seat0 -P ActiveSession").strip() == session
		keyboard.tap("KEY_W")
		ground.waitFor(lambda: "w" in ground.shell(f"cat {CONSOLE_TEXT}"), "W to reach the console")
		ground.settle(SESSION_RECHECK * 2 + 1)
		held = [r for r in client.records(mark, "DEVICE") if r["status"] == "HELD"]
		assert not held, f"the keyboard was taken with the text console in front: {held}"
		assert not client.keys(mark), client.keys(mark)

	with ground.subtest("the switch back takes the keyboard again"):
		mark = ground.mark()
		ground.shell(f"chvt {desktop}")
		client.waitForDevice(UHID, "HELD", mark)
		ground.machine.log(f"held again {ground.now() - mark.t:.3f} s after the switch back")
		ground.settle(1)
		mark = ground.mark()
		keyboard.tap("KEY_Z")
		ground.waitForKeys("x", [("KEY_Z", 1), ("KEY_Z", 0)], mark, device=VIRTUAL_KEYBOARD)
		console = ground.shell(f"cat {CONSOLE_TEXT}")
		assert "z" not in console, console
