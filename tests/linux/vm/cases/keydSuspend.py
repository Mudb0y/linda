"""The machine going to sleep while the helper holds a keyboard, with the unit as shipped.

Through logind's suspend, with every process frozen for five seconds by the kernel's suspend test
mode, which logind goes through as for any suspend: the helper lets go before the sleep, telling the
client BACKGROUND, holds on to it, survives the thaw with systemd's watchdog its own, and captures
again once every key is up. A real suspend to RAM cannot be made in these guests: their store is
shared over virtio-fs, which refuses to suspend, so the kernel gives the suspend up as devices
suspend; the test mode stops before that.

Frozen for five seconds without logind's word, as a path that skips logind would freeze it: the
helper's own watchdog takes the freeze for what it is; systemd's may kill the helper as it thaws,
as it did before the helper followed logind, and the keyboard is the desktop's again either way.
"""

from typing import TYPE_CHECKING

from ground import VIRTUAL_KEYBOARD

if TYPE_CHECKING:
	from ground import Ground, KeydClient, Mark

UHID = "Ground uhid keyboard"
FROZEN_FOR = 5


def heldAgain(ground: "Ground", client: "KeydClient", mark: "Mark") -> None:
	"""Wait until the client's last word on the keyboard since mark is that it is held."""

	def held() -> bool:
		said = [
			r
			for r in client.records(mark)
			if r["type"] == "RELEASED" or (r["type"] == "DEVICE" and r["name"] == UHID)
		]
		return bool(said) and said[-1]["type"] == "DEVICE" and said[-1]["status"] == "HELD"

	ground.waitFor(held, "the keyboard to be held again", 20)


def throughSleep(ground: "Ground", client: "KeydClient", keyboard, pid: int, mark: "Mark", key: str) -> None:
	"""What must hold after a sleep logind told the helper of."""
	ground.waitForHelperLog("the machine is going to sleep", mark, timeout=30)
	ground.waitForHelperLog("the machine has woken", mark, timeout=60)
	said = [line for _t, line in ground.helperLog(mark)]
	ground.machine.log("the helper said:\n" + "\n".join(said))
	released = client.waitFor("RELEASED", mark)
	assert released["reason"] == "BACKGROUND", released
	heldAgain(ground, client, mark)
	assert ground.helperPid() == pid, "the helper was ended"
	assert ground.helperRestarts() == 0
	assert not [line for line in said if "Watchdog timeout" in line], said
	assert client.call("state")["connected"]
	typed = ground.mark()
	keyboard.tap(key)
	ground.waitForKeys("x", [(key, 1), (key, 0)], typed, device=VIRTUAL_KEYBOARD)


def run(ground: "Ground") -> None:
	ground.openWindow()
	pid = ground.waitForHelper()
	keyboard = ground.uhidKeyboard(UHID)
	client = ground.keydClient("main")
	client.capture(UHID)
	ground.machine.log(
		"the guest's sleep states: "
		+ ground.shell("cat /sys/power/state /sys/power/mem_sleep 2>&1 | tr '\\n' ' '")
	)

	with ground.subtest("frozen through logind's suspend, it holds again, systemd's watchdog its own"):
		ground.shell(
			f"echo {FROZEN_FOR} > /sys/module/suspend/parameters/pm_test_delay && "
			"echo freezer > /sys/power/pm_test"
		)
		mark = ground.mark()
		ground.shell("systemctl suspend --no-block")
		throughSleep(ground, client, keyboard, pid, mark, "KEY_B")
		ground.shell("echo none > /sys/power/pm_test")
		assert "WatchdogUSec=2s" in ground.shell("systemctl show -p WatchdogUSec linda-keyd")

	with ground.subtest("frozen without logind's word, the keyboard is the desktop's again"):
		mark = ground.mark()
		ground.shell(
			"echo freezer > /sys/power/pm_test && echo freeze > /sys/power/state; echo none > /sys/power/pm_test"
		)
		ground.settle(4)
		said = [line for _t, line in ground.helperLog(mark)]
		ground.machine.log("the helper said:\n" + "\n".join(said))
		assert not [line for line in said if line.startswith("linda-keyd: watchdog:")], said
		now = ground.waitForHelper()
		if now == pid:
			ground.machine.log("the helper lived through it")
			heldAgain(ground, client, mark)
			device = VIRTUAL_KEYBOARD
		else:
			ground.machine.log("systemd's watchdog ended the helper as it thawed, and started it again")
			client.waitFor("CLOSED", mark)
			device = UHID
		typed = ground.mark()
		keyboard.tap("KEY_C")
		ground.waitForKeys("x", [("KEY_C", 1), ("KEY_C", 0)], typed, device=device)
		if device == UHID:
			again = ground.keydClient("again")
			again.capture(UHID, since=typed)
			typed = ground.mark()
			keyboard.tap("KEY_D")
			ground.waitForKeys("x", [("KEY_D", 1), ("KEY_D", 0)], typed, device=VIRTUAL_KEYBOARD)
