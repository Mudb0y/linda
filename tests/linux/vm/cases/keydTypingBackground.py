"""Typing and the session in the background: as a switch to a text console takes the client's session
from the front, the typed keys still waiting in line are lost and never go out, a typed key held down
is released, and every TYPE from then on is refused, so that nothing typed for the session lands on
the console; back in front, typing works again without the client asking for anything but START.

The waiting keys are typed by cases/keydTyping.py's guest half, which then creates a file that a root
shell switches consoles at, so that the switch comes while a typed key to be decided still holds the
line, well inside the bound of its WAIT. A reader on the text console records what reaches it.
"""

import json
import shlex
from typing import TYPE_CHECKING

from ground import VIRTIO_KEYBOARD, VIRTUAL_KEYBOARD

if TYPE_CHECKING:
	from ground import Ground

UHID = "Ground uhid keyboard"
#: FEATURE_TYPING and FEATURE_WAIT in protocol.rs, and VERDICT_WAIT_TIMEOUT.
FEATURE_TYPING, FEATURE_WAIT = 1, 2
WAIT_BOUND = 0.6
CONSOLE = 2
CONSOLE_TEXT = "/tmp/ground/console.txt"
READY = "/tmp/ground/typist-ready"
SWITCHED = "/tmp/ground/switched"
#: What the guest half types: a Shift held down, then T to be decided, then A; and once let go of,
#: B, and C to be decided.
TYPED_KEYS = ("KEY_LEFTSHIFT", "KEY_T", "KEY_A", "KEY_B", "KEY_C")


def run(ground: "Ground") -> None:
	ground.openWindow()
	ground.waitForHelper()
	ground.uhidKeyboard(UHID)
	client = ground.keydClient("main")
	assert client.connect(features=FEATURE_TYPING | FEATURE_WAIT)["welcome"]["features"] == 3
	client.capture(UHID, VIRTIO_KEYBOARD)
	desktop = int(ground.shell("fgconsole").strip())
	ground.shell(
		f"openvt -c {CONSOLE} -- sh -c 'stty raw -echo; exec cat > {CONSOLE_TEXT}'; "
		f"until [ -e {CONSOLE_TEXT} ]; do sleep 0.05; done"
	)

	with ground.subtest(
		"what waited to be typed as the session leaves the front is lost, and no more is typed"
	):
		client.call("close")
		ground.shell(f"rm -f {READY} {SWITCHED}")
		switch = f"until [ -e {READY} ]; do sleep 0.005; done; ground-record now > {SWITCHED}; chvt {CONSOLE}"
		ground.shell(
			"systemd-run --collect --unit=ground-switch --setenv=PATH=/run/current-system/sw/bin "
			f"sh -c {shlex.quote(switch)}"
		)
		mark = ground.mark()
		done = ground.run(["ground-typist", "lost", UHID, READY], timeout=60)
		assert done.returncode == 0, done
		seen = json.loads(done.stdout)
		switched = float(ground.shell(f"cat {SWITCHED}").split()[0])
		released = seen["released"]
		typed = seen["typed"]
		ground.machine.log(
			f"from the KEY of the typed T: the switch {switched - seen['head']['t']:+.3f} s, RELEASED "
			f"{released['t'] - seen['head']['t']:+.3f} s; answers {json.dumps(typed)}"
		)
		# What the test claims rests on the switch coming while T still held the line.
		assert switched - seen["head"]["t"] < WAIT_BOUND - 0.1, (switched, seen["head"])
		assert released["reason"] == "BACKGROUND", released
		assert [typed[tag]["outcome"] for tag in ("2", "3", "4")] == ["LOST"] * 3, typed
		assert typed["2"]["seq"] == seen["head"]["seq"], typed
		assert all(typed[tag]["t"] <= released["t"] for tag in ("2", "3", "4")), (typed, released)
		assert [typed[tag]["outcome"] for tag in ("5", "6")] == ["REFUSED_STATE"] * 2, typed
		assert [typed[tag]["seq"] for tag in ("5", "6")] == [0, 0], typed
		assert not seen["typedKeysAfter"], seen["typedKeysAfter"]
		ground.waitForHelperLog(f"console {CONSOLE} is in front", mark)
		ground.settle(0.5)
		virtual = [
			(r["key"], r["value"])
			for r in ground.keys("evdev", mark, device=VIRTUAL_KEYBOARD)
			if r["key"] in TYPED_KEYS and r["value"] != 2
		]
		ground.machine.log(f"the typed keys on the virtual keyboard: {virtual}")
		assert virtual == [("KEY_LEFTSHIFT", 1), ("KEY_LEFTSHIFT", 0)], virtual
		console = ground.shell(f"cat {CONSOLE_TEXT}")
		ground.machine.log(f"the console had {console!r}")
		assert not set("tabcTABC") & set(console), console

	with ground.subtest("back in front, typing works again"):
		mark = ground.mark()
		ground.shell(f"chvt {desktop}")
		assert client.connect(features=FEATURE_TYPING)["welcome"]["features"] == FEATURE_TYPING
		client.capture(UHID, since=mark)
		mark = ground.mark()
		client.call("type", tag=7, key="KEY_D", down=True, mode="inject")
		client.call("type", tag=8, key="KEY_D", down=False, mode="inject")
		# Typed by two test calls, D is held long enough to repeat.
		ground.waitFor(
			lambda: [r for r in ground.keys("app", mark) if (r["key"], r["value"]) == ("KEY_D", 0)],
			"D's release in the window",
		)
		app = ground.keys("app", mark)
		assert app[0]["keyval"] == "d", app
		got = {r["tag"]: r["outcome"] for r in client.records(mark, "TYPED")}
		assert got == {7: "OUT", 8: "OUT"}, got
