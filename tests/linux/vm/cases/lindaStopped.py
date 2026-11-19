"""Linda going wrong while the helper holds the keyboard. Stopped with SIGSTOP for a few seconds: the
helper lets go, what is typed meanwhile reaches the program, once each and in order, and once
continued Linda takes keys again, from the kernel. Killed: the keyboard is the desktop's at once.
"""

from typing import TYPE_CHECKING

from ground import KERNEL_ROUTE, VIRTUAL_KEYBOARD, WINDOW_TITLE, X_ROUTE, Mark

if TYPE_CHECKING:
	from ground import FakeKeyboard, Ground

UHID = "Ground uhid keyboard"
CHORD_KEYS = ("KEY_INSERT", "KEY_T")
APART = 0.8
STOPPED_FOR = 5.0


def lindaKeyWorks(ground: "Ground", keyboard: "FakeKeyboard") -> None:
	ground.settle(APART)
	mark = ground.mark()
	keyboard.chord(*CHORD_KEYS)
	ground.expect(WINDOW_TITLE, since=mark)
	ground.settle(0.5)
	reached = [r for r in ground.keys("app", mark) if r["key"] in CHORD_KEYS]
	assert not reached, reached


def run(ground: "Ground") -> None:
	ground.openWindow()
	ground.waitForHelper()
	keyboard = ground.uhidKeyboard(UHID)
	ground.startLinda()
	ground.waitForLindaLog(KERNEL_ROUTE, timeout=30)
	lindaKeyWorks(ground, keyboard)

	with ground.subtest("Linda stopped for a few seconds while letters are typed"):
		mark = ground.mark()
		ground.signalLinda("SIGSTOP")
		stopped = ground.now()
		letters = "abcdefghijklmnopqrstuvwxy"
		steps = [s for c in letters for s in ({"tap": f"KEY_{c.upper()}", "hold": 0.02}, {"sleep": 0.15})]
		written = keyboard.run(*steps)
		ground.settle(max(0.0, STOPPED_FOR - (ground.now() - stopped)))
		typed = [(r["key"], r["value"]) for r in written]
		app = ground.waitForKeys("app", typed, mark)
		ground.settle(0.3)
		assert [(r["key"], r["value"]) for r in ground.keys("app", mark)] == typed
		goodbye = ground.waitForHelperLog("", mark)
		helper = "\n".join(message for _t, message in ground.helperLog(mark))
		delays = ground.delays(written, app)
		late = [round(d * 1000) for key, value, d in delays if value == 1]
		ground.machine.log(
			f"while Linda was stopped each press reached the program after (ms): {late}\nthe helper said:\n{helper}"
		)
		direct = [r for r in ground.keys("x", mark) if r["device"] == UHID]
		ground.machine.log(
			f"{len(direct)} key events went to the X server from the keyboard itself, the helper having let go"
		)
		assert goodbye

	with ground.subtest("continued, Linda takes keys again, from the kernel"):
		mark = ground.mark()
		ground.signalLinda("SIGCONT")
		continued = ground.now()
		ground.waitForLindaLog(X_ROUTE, count=2, timeout=10)
		fell = ground.lindaLogRecords(X_ROUTE)[1][1]
		ground.machine.log(f"Linda fell back: {fell.splitlines()[-1]}")
		back, _record = ground.waitForLindaLog(KERNEL_ROUTE, count=2, timeout=20)
		lindaKeyWorks(ground, keyboard)
		ground.machine.log(
			f"continued at {continued:.3f}; back on the kernel route by its log at {back:.3f} (time of day)"
		)
		mark = ground.mark()
		keyboard.tap("KEY_Z")
		ground.waitForKeys("x", [("KEY_Z", 1), ("KEY_Z", 0)], mark, device=VIRTUAL_KEYBOARD)
		ground.expect("z", since=mark)

	with ground.subtest("Linda killed: the keyboard is the desktop's at once"):
		killed = ground.now()
		ground.signalLinda("SIGKILL")
		ground.waitFor(lambda: not ground.running("linda"), "Linda to end")
		mark = ground.mark()
		written = keyboard.tap("KEY_K")
		ground.waitForKeys("x", [("KEY_K", 1), ("KEY_K", 0)], mark, device=UHID)
		app = ground.waitForKeys("app", [("KEY_K", 1), ("KEY_K", 0)], mark)
		released = ground.waitForHelperLog("", Mark(0, killed))
		ground.machine.log(
			f"the helper's first word after the kill came {released[0] - killed:.3f} s after it: {released[1]}; "
			f"K written {written[0]['t'] - killed:.3f} s after the kill reached the program "
			f"{(app[0]['t'] - written[0]['t']) * 1000:.1f} ms later, from the keyboard itself"
		)
