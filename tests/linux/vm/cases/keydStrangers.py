"""Callers the helper must turn away: a process of another user, a process of root, and processes of
the session's own user whose session is not the one in front, because the same user has signed in on
a text console and switched to it. None of them is welcomed, and nothing is grabbed meanwhile.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
	from ground import Ground

UHID = "Ground uhid keyboard"


def refused(ground: "Ground", name: str, how: str) -> dict:
	client = ground.keydClient(name, how)
	answer = client.connect()
	assert answer["welcome"] is None and answer["goodbye"], answer
	ground.machine.log(f"{name}, run as {how}: {answer['goodbye']['reason']}, {answer['goodbye']['text']!r}")
	return answer["goodbye"]


def nothingHeld(ground: "Ground", keyboard) -> None:
	mark = ground.mark()
	keyboard.tap("KEY_A")
	ground.waitForKeys("evdev", [("KEY_A", 1), ("KEY_A", 0)], mark, device=UHID)
	assert "capturing" not in " ".join(line for _t, line in ground.helperLog())


def run(ground: "Ground") -> None:
	ground.openWindow()
	ground.waitForHelper()
	keyboard = ground.uhidKeyboard(UHID)

	with ground.subtest("a process of another user is turned away"):
		assert refused(ground, "bob", "bob")["reason"] == "NOT_IN_FRONT"
		assert refused(ground, "root", "root")["reason"] == "NOT_IN_FRONT"
		nothingHeld(ground, keyboard)

	desktop = int(ground.shell("fgconsole").strip())
	console = 3 if desktop != 3 else 4
	xSession = ground.shell("loginctl show-seat seat0 -P ActiveSession").strip()
	ground.machine.log(f"the X session, {xSession}, is on console {desktop}; the text console is {console}")

	with ground.subtest("a process of a session of the same user that is not in front is turned away"):
		mark = ground.mark()
		ground.shell(f"chvt {console}")

		def textSession() -> str:
			active = ground.shell("loginctl show-seat seat0 -P ActiveSession").strip()
			if active in ("", xSession):
				return ""
			return active if ground.shell(f"loginctl show-session {active} -P User").strip() == "1000" else ""

		session = ground.waitFor(textSession, "alice's text console session to be in front", 30)
		ground.waitForHelperLog(f'Session {{ id: "{session}", uid: 1000, ', mark)
		ground.machine.log(ground.shell("loginctl list-sessions --no-legend"))
		goodbye = refused(ground, "inX", "session")
		assert goodbye["reason"] == "NOT_IN_FRONT" and "is not in front" in goodbye["text"], goodbye
		goodbye = refused(ground, "outside", "userManager")
		assert goodbye["reason"] == "NOT_IN_FRONT", goodbye
		nothingHeld(ground, keyboard)

	with ground.subtest("back in front, the session's process is welcomed"):
		mark = ground.mark()
		ground.shell(f"chvt {desktop}")
		ground.waitForHelperLog(f'Session {{ id: "{xSession}", uid: 1000, ', mark)
		client = ground.keydClient("again")
		assert client.connect()["welcome"]
