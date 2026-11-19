"""A switch to a text console and back under a running Linda. Control, Alt and F2, passed on by Linda,
switch; the helper lets go as the kernel switches, and what is typed on the console reaches it and not
Linda. On the way back the helper takes the keyboard again by itself, and Linda, which stayed on the
kernel route, takes keys again. The Linda key with T is pressed again and again from the moment of
the switch back, to find whether any press in between reaches the program.
"""

from typing import TYPE_CHECKING

from ground import KERNEL_ROUTE, VIRTUAL_KEYBOARD, WINDOW_TITLE, X_ROUTE

if TYPE_CHECKING:
	from ground import Ground

UHID = "Ground uhid keyboard"
CHORD_KEYS = ("KEY_INSERT", "KEY_T")
CONSOLE = 2
CONSOLE_TEXT = "/tmp/ground/console.txt"


def run(ground: "Ground") -> None:
	ground.openWindow()
	ground.waitForHelper()
	keyboard = ground.uhidKeyboard(UHID)
	ground.startLinda()
	ground.waitForLindaLog(KERNEL_ROUTE, timeout=30)
	desktop = int(ground.shell("fgconsole").strip())
	ground.shell(
		f"openvt -c {CONSOLE} -- sh -c 'stty raw -echo; exec cat > {CONSOLE_TEXT}'; "
		f"until [ -e {CONSOLE_TEXT} ]; do sleep 0.05; done"
	)

	with ground.subtest("Control, Alt and F2 switch to the console, and the helper lets go"):
		mark = ground.mark()
		keyboard.chord("KEY_LEFTCTRL", "KEY_LEFTALT", "KEY_F2")
		ground.waitFor(lambda: ground.shell("fgconsole").strip() == str(CONSOLE), "the switch to the console")
		ground.waitForLindaLog("linda-keyd let go of the keyboard")
		ground.settle(0.5)
		mark = ground.mark()
		keyboard.tap("KEY_H", "KEY_I")
		ground.waitFor(lambda: "hi" in ground.shell(f"cat {CONSOLE_TEXT}"), "hi on the console")
		ground.waitForKeys("evdev", [("KEY_H", 1), ("KEY_H", 0)], mark, device=UHID)
		ground.settle(0.5)
		assert not [t for t in ground.speech(mark) if t[1] in ("h", "i")], ground.speech(mark)

	with ground.subtest("back on the desktop, Linda's keys are its own again"):
		mark = ground.mark()
		switched = ground.now()
		ground.shell(f"chvt {desktop}")
		probes = keyboard.run(
			*[s for _ in range(12) for s in ({"chord": list(CHORD_KEYS), "hold": 0.03}, {"sleep": 0.12})]
		)
		ground.settle(1)
		held = ground.waitForHelperLog("Held", mark)[0]
		leaked = [r for r in ground.keys("app", mark) if r["key"] in CHORD_KEYS]
		direct = [r for r in ground.keys("x", mark, device=UHID) if r["key"] in CHORD_KEYS]
		firstProbe = probes[0]["t"] - switched
		ground.machine.log(
			f"the helper held a keyboard again {held - switched:.3f} s after the switch back; probes from "
			f"{firstProbe:.3f} s to {probes[-1]['t'] - switched:.3f} s; "
			f"{len(direct)} chord events reached the X server from the keyboard itself, "
			f"{len(leaked)} reached the program: {[(r['key'], r['value'], round(r['t'] - switched, 3)) for r in leaked]}"
		)
		assert len(ground.lindaLogRecords(X_ROUTE)) == 1, "Linda left the kernel route"
		ground.settle(0.8)
		mark = ground.mark()
		keyboard.chord(*CHORD_KEYS)
		ground.expect(WINDOW_TITLE, since=mark)
		ground.settle(0.5)
		assert not [r for r in ground.keys("app", mark) if r["key"] in CHORD_KEYS]
		mark = ground.mark()
		keyboard.tap("KEY_J")
		ground.waitForKeys("x", [("KEY_J", 1), ("KEY_J", 0)], mark, device=VIRTUAL_KEYBOARD)
		ground.expect("j", since=mark)
		assert not leaked, leaked
