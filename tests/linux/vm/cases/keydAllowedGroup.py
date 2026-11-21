"""services.linda-keyd.allowedGroup: with the helper limited to the group linda, a program of the
session in front whose user is not a member is turned away by polkit, and nothing is grabbed. Once the
user is added to the group the same kind of program is welcomed, without signing in again, and one of
another user's is still turned away as before.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
	from ground import Ground

UHID = "Ground uhid keyboard"
ACTION = "io.github.mudb0y.linda.keyd.capture"


def run(ground: "Ground") -> None:
	ground.openWindow()
	ground.waitForHelper()
	keyboard = ground.uhidKeyboard(UHID)
	rules = ground.shell("cat /etc/polkit-1/rules.d/*.rules")
	assert ACTION in rules and '"linda"' in rules, rules

	with ground.subtest("a user outside the group is turned away"):
		assert "linda" not in ground.shell("id -nG alice").split()
		for name, how in (("outX", "session"), ("outManager", "userManager")):
			client = ground.keydClient(name, how)
			answer = client.connect()
			assert answer["welcome"] is None and answer["goodbye"], answer
			goodbye = answer["goodbye"]
			ground.machine.log(f"{name}: {goodbye['reason']}, {goodbye['text']!r}")
			assert goodbye["reason"] == "NOT_AUTHORIZED" and "polkit" in goodbye["text"], goodbye
		mark = ground.mark()
		keyboard.tap("KEY_A")
		ground.waitForKeys("evdev", [("KEY_A", 1), ("KEY_A", 0)], mark, device=UHID)
		assert "capturing" not in " ".join(line for _t, line in ground.helperLog())

	with ground.subtest("a member of the group is welcomed"):
		ground.shell("gpasswd -a alice linda")
		assert "linda" in ground.shell("id -nG alice").split()
		for name, how in (("inX", "session"), ("inManager", "userManager")):
			client = ground.keydClient(name, how)
			mark = ground.mark()
			client.capture(UHID, since=mark)
			keyboard.tap("KEY_B")
			key = client.waitFor("KEY", mark)
			assert (key["key"], key["value"]) == ("KEY_B", 1), key
			client.call("close")

	with ground.subtest("another user is still turned away"):
		client = ground.keydClient("bob", "bob")
		answer = client.connect()
		assert answer["goodbye"] and answer["goodbye"]["reason"] == "NOT_IN_FRONT", answer
