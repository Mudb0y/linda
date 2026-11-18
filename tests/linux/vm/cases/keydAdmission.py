"""Admission against the real logind and polkit: a process of the X session in front is welcomed, as
the session logind names; so is a process of the same user started by the user's systemd manager,
which is in no session at all, as Linda is when a desktop starts it that way. A caller that says
nothing in time, or speaks another version, is turned away, and a welcomed client that breaks the
protocol is dropped.
"""

import struct
from typing import TYPE_CHECKING

if TYPE_CHECKING:
	from ground import Ground


def run(ground: "Ground") -> None:
	ground.openWindow()
	ground.waitForHelper()
	xSession = ground.shell("loginctl show-seat seat0 -P ActiveSession").strip()

	with ground.subtest("a process of the session in front is welcomed"):
		mark = ground.mark()
		client = ground.keydClient("inX")
		answer = client.connect()
		assert answer["welcome"], answer
		said = ground.waitForHelperLog("admitted process", mark)[1]
		ground.machine.log(f"the helper: {said}; the X session is {xSession}")
		assert said.endswith(f"of session {xSession}"), said
		ground.machine.log(
			f"admitted {answer['welcome']['t'] - mark.t:.3f} s after the mark, logind and polkit asked"
		)
		client.call("close")

	with ground.subtest(
		"a process of the user outside any session is welcomed as of the user's graphical session"
	):
		mark = ground.mark()
		client = ground.keydClient("outside", "userManager")
		pid = client.pid
		cgroup = ground.shell(f"cat /proc/{pid}/cgroup").strip()
		ground.machine.log(f"the client's control group: {cgroup}")
		assert "session-" not in cgroup, cgroup
		answer = client.connect()
		said = [line for _t, line in ground.helperLog(mark)]
		ground.machine.log(f"the helper: {said}")
		assert answer["welcome"], answer
		assert said[-1].endswith(f"of session {xSession}"), said
		client.call("close")

	with ground.subtest("a caller that says nothing, or speaks another version, is turned away"):
		mark = ground.mark()
		client = ground.keydClient("mute")
		answer = client.connect(hello=False)
		assert answer["goodbye"] and answer["goodbye"]["reason"] == "SILENT", answer
		ground.machine.log(f"a mute caller was turned away after {answer['goodbye']['t'] - mark.t:.3f} s")
		answer = client.connect(version=2)
		assert answer["goodbye"]["reason"] == "VERSION" and answer["goodbye"]["version"] == 1, answer

	with ground.subtest("a welcomed client that sends rubbish is dropped"):
		client = ground.keydClient("rubbish")
		assert client.connect()["welcome"]
		mark = ground.mark()
		client.call("send", hex=struct.pack("<BQB", 0x04, 1, 7).hex())
		goodbye = client.waitFor("GOODBYE", mark)
		assert goodbye["reason"] == "MALFORMED", goodbye
		ground.machine.log(f"rubbish: {goodbye['text']!r}")
