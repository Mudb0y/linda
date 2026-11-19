"""Linda on the kernel route with the guest's processors kept busy by another program: letters typed for
some seconds with the Linda key with T among them. How many presses the helper passed on because Linda
answered late, and whether the helper dropped Linda, go to the log; every letter reaches the program
once and in order whatever happens.
"""

import statistics
from typing import TYPE_CHECKING

from ground import KERNEL_ROUTE, VIRTUAL_KEYBOARD, WINDOW_TITLE, X_ROUTE

if TYPE_CHECKING:
	from ground import Ground

UHID = "Ground uhid keyboard"
#: Busy loops, twice as many as the guest has processors, at the same priority as Linda.
BURNERS = 4
ROUNDS = 8
#: No T among them, so that a T the program has is the Linda key's.
LETTERS = "quickbrownfoxjumps"
EXPIRED = "on before NVDA answered it"
DEFAULTED = "answered without NVDA"


def summary(delays: list[float]) -> str:
	ms = sorted(d * 1000 for d in delays)
	return f"median {statistics.median(ms):.1f} ms, worst {ms[-1]:.1f} ms over {len(ms)}"


def run(ground: "Ground") -> None:
	ground.openWindow()
	ground.waitForHelper()
	keyboard = ground.uhidKeyboard(UHID)
	ground.startLinda()
	ground.waitForLindaLog(KERNEL_ROUTE, timeout=30)
	ground.settle(1)
	before = len(ground.lindaLogRecords(EXPIRED))

	with ground.subtest("typing and Linda's commands with the processors busy"):
		ground.shell(
			"systemd-run --collect --unit=ground-burn sh -c "
			f"'for i in {' '.join(['x'] * BURNERS)}; do (while :; do :; done) & done; wait'"
		)
		ground.settle(1)
		assert ground.shell("systemctl is-active ground-burn.service").strip() == "active"
		mark = ground.mark()
		steps: list[dict] = []
		for _ in range(ROUNDS):
			for c in LETTERS:
				steps += [{"tap": f"KEY_{c.upper()}", "hold": 0.03}, {"sleep": 0.04}]
			steps += [{"chord": ["KEY_INSERT", "KEY_T"], "hold": 0.05}, {"sleep": 0.6}]
		written = keyboard.run(*steps)
		ground.settle(2)
		load = ground.shell("cat /proc/loadavg").strip()
		ground.shell("systemctl stop ground-burn.service")
		letters = [(r["key"], r["value"]) for r in written if r["key"] not in ("KEY_INSERT", "KEY_T")]
		app = [r for r in ground.keys("app", mark) if r["key"] not in ("KEY_INSERT", "KEY_T")]
		leaked = [r for r in ground.keys("app", mark) if r["key"] == "KEY_T" and r["value"] == 1]
		titles = sum(text == WINDOW_TITLE for _t, text in ground.speech(mark))
		expired = len(ground.lindaLogRecords(EXPIRED)) - before
		defaulted = len(ground.lindaLogRecords(DEFAULTED))
		dropped = len(ground.lindaLogRecords(X_ROUTE)) > 1
		helperDropped = [m for _t, m in ground.helperLog(mark) if "dropping the client" in m]
		presses = sum(1 for r in written if r["value"] == 1)
		virtual = ground.keys("evdev", mark, device=VIRTUAL_KEYBOARD)
		passedDelays = [d for _k, value, d in ground.delays(written, virtual) if value == 1]
		ground.machine.log(
			f"under load ({load}): {presses} presses over {written[-1]['t'] - written[0]['t']:.1f} s; "
			f"the helper passed {expired} on because Linda answered late; Linda's own thread answered "
			f"{defaulted} itself; the Linda key with T was pressed {ROUNDS} times, spoken {titles} times, and "
			f"reached the program {len(leaked)} times; the helper dropped Linda: {helperDropped or 'never'}; "
			f"key to virtual keyboard for passed presses: {summary(passedDelays)}"
		)
		assert [(r["key"], r["value"]) for r in app] == letters, [(r["key"], r["value"]) for r in app]
		assert not dropped and not helperDropped, (helperDropped, ground.lindaLogRecords(X_ROUTE))
