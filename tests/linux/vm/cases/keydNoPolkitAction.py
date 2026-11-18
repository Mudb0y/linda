"""Admission with the polkit action missing, as on a system where the package's policy file was not
installed: polkit cannot answer for an action it does not know, so the caller is turned away and
nothing is grabbed. With the action back, the same caller is welcomed.
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
	actions = ground.shell("readlink -f /run/current-system/sw/share/polkit-1/actions").strip()
	# The system's actions without the helper's, laid over the real directory.
	ground.shell(
		f"mkdir /tmp/actions && cp {actions}/*.policy /tmp/actions/ && rm /tmp/actions/{ACTION.rsplit('.', 1)[0]}.policy "
		f"&& mount --bind /tmp/actions {actions} && systemctl restart polkit"
	)
	ground.waitFor(
		lambda: ground.machine.execute(f"pkaction --action-id {ACTION}")[0] != 0,
		"polkit to forget the action",
	)
	client = ground.keydClient("main")

	with ground.subtest("without the action the caller is turned away and nothing is grabbed"):
		mark = ground.mark()
		answer = client.connect()
		assert answer["welcome"] is None and answer["goodbye"], answer
		ground.machine.log(f"turned away: {answer['goodbye']['reason']}, {answer['goodbye']['text']!r}")
		assert answer["goodbye"]["reason"] in ("NOT_AUTHORIZED", "NO_ANSWER"), answer
		mark = ground.mark()
		keyboard.tap("KEY_A")
		ground.waitForKeys("x", [("KEY_A", 1), ("KEY_A", 0)], mark, device=UHID)
		assert "capturing" not in " ".join(line for _t, line in ground.helperLog())

	with ground.subtest("with the action back the caller is welcomed"):
		ground.shell(f"umount {actions} && systemctl restart polkit")
		ground.waitFor(
			lambda: ground.machine.execute(f"pkaction --action-id {ACTION}")[0] == 0,
			"polkit to know the action",
		)
		client.capture(UHID)
