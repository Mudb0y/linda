"""The helper going under a running Linda while it reads a keyboard the helper holds and one the helper
leaves alone, with keys held on both across each change: killed, stopped with SIGSTOP until systemd's
watchdog ends it, and let go by the emergency chord. Throughout, the keyboard left alone is Linda's
as on the X route; after each change no key is left held, to the X server, the program or Linda, and
both keyboards' Linda keys work, on the X route and again on the kernel route.
"""

from typing import TYPE_CHECKING

from ground import KERNEL_ROUTE, VIRTUAL_KEYBOARD, WINDOW_TITLE, X_ROUTE

if TYPE_CHECKING:
	from ground import FakeKeyboard, Ground, Mark

UHID = "Ground uhid keyboard"
UINPUT = "Ground uinput keyboard"
CHORD_KEYS = ("KEY_INSERT", "KEY_T")
APART = 0.8
LET_GO = "Linda has let go of the keyboard\N{HORIZONTAL ELLIPSIS}"


def lindaKeyWorks(ground: "Ground", keyboard: "FakeKeyboard") -> None:
	ground.settle(APART)
	mark = ground.mark()
	keyboard.chord(*CHORD_KEYS)
	ground.expect(WINDOW_TITLE, since=mark)
	ground.settle(0.5)
	reached = [r for r in ground.keys("app", mark) if r["key"] in CHORD_KEYS]
	assert not reached, reached


def plainLetter(ground: "Ground", keyboard: "FakeKeyboard", key: str, keyval: str) -> None:
	"""A letter typed alone reaches the program as itself, once, is echoed and is no command: nothing
	holds Shift or the Linda key down."""
	ground.settle(APART)
	mark = ground.mark()
	keyboard.tap(key)
	app = ground.waitForKeys("app", [(key, 1), (key, 0)], mark)
	assert app[0]["keyval"] == keyval, app
	ground.expect(keyval, since=mark)
	ground.settle(0.5)
	assert WINDOW_TITLE not in [text for _t, text in ground.speech(mark)], ground.speech(mark)
	assert len([r for r in ground.keys("app", mark) if r["value"] == 1]) == 1, ground.keys("app", mark)


def keysDownInX(ground: "Ground", device: str) -> list[int]:
	"""The key codes the X server has down on the slave keyboard named device; none for one it lacks."""
	keyboard = ground.xinputDevice(device)
	if keyboard is None:
		return []
	state = ground.run(["xinput", "query-state", keyboard])
	if state.returncode != 0:
		return []
	return [
		int(line.split("[")[1].split("]")[0])
		for line in state.stdout.splitlines()
		if line.strip().startswith("key[") and line.strip().endswith("=down")
	]


def nothingHeld(ground: "Ground", mark: "Mark") -> None:
	"""The X server holds no key down on the keyboard left alone or the virtual keyboard. The program's
	own records are not counted: a release that comes while a grab of Linda's holds the keyboard goes
	to Linda alone, as on the X route, and the program has the state from its next key."""
	del mark
	for device in (UINPUT, VIRTUAL_KEYBOARD):
		assert not keysDownInX(ground, device), (device, keysDownInX(ground, device))


def everythingWorks(ground: "Ground", held: "FakeKeyboard", alone: "FakeKeyboard", letters: str) -> None:
	plainLetter(ground, alone, f"KEY_{letters[0].upper()}", letters[0])
	plainLetter(ground, held, f"KEY_{letters[1].upper()}", letters[1])
	lindaKeyWorks(ground, alone)
	lindaKeyWorks(ground, held)


def run(ground: "Ground") -> None:
	ground.openWindow()
	ground.waitForHelper()
	held = ground.uhidKeyboard(UHID)
	ground.startLinda()
	ground.waitForLindaLog(KERNEL_ROUTE, timeout=30)
	alone = ground.keyboard(UINPUT)
	ground.waitForHelperLog(f"({UINPUT}): ")
	everythingWorks(ground, held, alone, "ab")

	with ground.subtest("killed with Shift held on the held keyboard and the Linda key on the other"):
		mark = ground.mark()
		held.run({"press": "KEY_LEFTSHIFT"})
		ground.waitForKeys("x", [("KEY_LEFTSHIFT", 1)], mark, device=VIRTUAL_KEYBOARD)
		alone.run({"press": "KEY_INSERT"}, {"sleep": 0.2})
		ground.shell("systemctl kill --signal=SIGKILL linda-keyd.service")
		ground.waitForKeys("x", [("KEY_LEFTSHIFT", 1), ("KEY_LEFTSHIFT", 0)], mark, device=VIRTUAL_KEYBOARD)
		ground.waitForLindaLog(X_ROUTE, count=2)
		# The Linda key still held on the keyboard left alone makes T Linda's.
		speech = ground.mark()
		alone.run({"tap": "KEY_T", "hold": 0.05}, {"sleep": 0.1}, {"release": "KEY_INSERT"})
		ground.expect(WINDOW_TITLE, since=speech)
		held.run({"release": "KEY_LEFTSHIFT"})
		ground.settle(0.5)
		nothingHeld(ground, mark)
		everythingWorks(ground, held, alone, "cd")
		ground.waitForLindaLog(KERNEL_ROUTE, count=2, timeout=20)
		mark = ground.mark()
		everythingWorks(ground, held, alone, "ef")
		nothingHeld(ground, mark)

	with ground.subtest("stopped with SIGSTOP, the keyboard left alone working through it"):
		restarts = ground.helperRestarts()
		pid = ground.helperPid()
		ground.settle(APART)
		mark = ground.mark()
		held.run({"press": "KEY_INSERT"}, {"sleep": 0.1})
		alone.run({"press": "KEY_LEFTSHIFT"}, {"sleep": 0.05})
		ground.shell(f"kill -STOP {pid}")
		ground.waitForLindaLog(X_ROUTE, count=3, timeout=10)
		alone.run({"tap": "KEY_G", "hold": 0.03}, {"sleep": 0.05}, {"release": "KEY_LEFTSHIFT"})
		app = ground.waitForKeys(
			"app", [("KEY_LEFTSHIFT", 1), ("KEY_G", 1), ("KEY_G", 0), ("KEY_LEFTSHIFT", 0)], mark
		)
		assert app[1]["keyval"] == "G", app
		# Released on a keyboard the stopped helper still holds.
		held.run({"release": "KEY_INSERT"})
		if ground.helperRestarts() == restarts:
			plainLetter(ground, alone, "KEY_H", "h")
		ground.waitFor(lambda: ground.helperRestarts() > restarts, "systemd to restart the helper", 15)
		ground.settle(0.5)
		nothingHeld(ground, mark)
		everythingWorks(ground, held, alone, "jk")
		ground.waitForLindaLog(KERNEL_ROUTE, count=3, timeout=20)
		mark = ground.mark()
		everythingWorks(ground, held, alone, "lm")
		nothingHeld(ground, mark)

	with ground.subtest("the emergency chord with Shift held on the keyboard left alone"):
		ground.settle(APART)
		mark = ground.mark()
		alone.run({"press": "KEY_LEFTSHIFT"}, {"sleep": 0.05})
		held.run(
			{"press": "KEY_BACKSPACE"},
			{"sleep": 0.05},
			{"press": "KEY_ESC"},
			{"sleep": 0.05},
			{"press": "KEY_ENTER"},
			{"sleep": 0.1},
		)
		ground.waitForSpeech(LET_GO, mark)
		ground.waitForLindaLog(X_ROUTE, count=4)
		typed = ground.mark()
		alone.run({"tap": "KEY_N", "hold": 0.03}, {"sleep": 0.05})
		app = ground.waitForKeys("app", [("KEY_N", 1), ("KEY_N", 0)], typed)
		assert app[0]["keyval"] == "N", app
		alone.run({"release": "KEY_LEFTSHIFT"})
		held.run({"release": "KEY_ENTER"}, {"release": "KEY_ESC"}, {"release": "KEY_BACKSPACE"})
		ground.settle(0.5)
		nothingHeld(ground, mark)
		everythingWorks(ground, held, alone, "op")
		ground.settle(APART)
		back = ground.mark()
		held.chord("KEY_INSERT", "KEY_LEFTSHIFT", "KEY_F12")
		ground.expect("Linda has the keyboard again", since=back, timeout=15)
		ground.waitForLindaLog(KERNEL_ROUTE, count=4)
		mark = ground.mark()
		everythingWorks(ground, held, alone, "qr")
		nothingHeld(ground, mark)
