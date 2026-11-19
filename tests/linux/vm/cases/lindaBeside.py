"""Both of Linda's key sources at once, on the kernel route. A key another program sends through XTEST
is heard as on the X route. A letter typed on a keyboard the helper holds reaches Linda once and is
echoed once, though it reaches the X server too, through the virtual keyboard, where Linda reads the
X server for every other keyboard. A chord split between a keyboard the helper holds and one it
leaves alone leaves no key held, to the X server, the program or Linda, no grab held and no keyboard
dead.
"""

from typing import TYPE_CHECKING

from ground import KERNEL_ROUTE, VIRTUAL_KEYBOARD, WINDOW_TITLE, X_ROUTE

if TYPE_CHECKING:
	from ground import FakeKeyboard, Ground, Mark

UHID = "Ground uhid keyboard"
UINPUT = "Ground uinput keyboard"
CHORD_KEYS = ("KEY_INSERT", "KEY_T")
APART = 0.8
#: What Linda's log has for each key that typed text, in place of its name.
TYPING_INPUT = "(typing key)"


def lindaKeyWorks(ground: "Ground", keyboard: "FakeKeyboard") -> None:
	ground.settle(APART)
	mark = ground.mark()
	keyboard.chord(*CHORD_KEYS)
	ground.expect(WINDOW_TITLE, since=mark)
	ground.settle(0.5)
	reached = [r for r in ground.keys("app", mark) if r["key"] in CHORD_KEYS]
	assert not reached, reached


def plainLetter(ground: "Ground", keyboard: "FakeKeyboard", key: str, keyval: str) -> None:
	"""A letter typed alone reaches the program as itself, once, is echoed once and is no command."""
	ground.settle(APART)
	mark = ground.mark()
	keyboard.tap(key)
	app = ground.waitForKeys("app", [(key, 1), (key, 0)], mark)
	assert app[0]["keyval"] == keyval, app
	ground.expect(keyval, since=mark)
	ground.settle(0.5)
	spoken = [text for _t, text in ground.speech(mark)]
	assert WINDOW_TITLE not in spoken and spoken.count(keyval) == 1, spoken
	assert len([r for r in ground.keys("app", mark) if r["value"] == 1]) == 1, ground.keys("app", mark)


def keysDownInX(ground: "Ground", device: str) -> list[int]:
	"""The key codes the X server has down on the slave device named device."""
	state = ground.run(["xinput", "query-state", device])
	assert state.returncode == 0, state.stderr
	return [
		int(line.split("[")[1].split("]")[0])
		for line in state.stdout.splitlines()
		if line.strip().startswith("key[") and line.strip().endswith("=down")
	]


def balanced(ground: "Ground", mark: "Mark") -> None:
	"""The X server holds no key down on the keyboard left alone or the virtual keyboard. The program's
	own records are not counted: a release that comes while a grab of Linda's holds the keyboard goes
	to Linda alone, as on the X route, and the program has the state from its next key."""
	del mark
	for device in (UINPUT, VIRTUAL_KEYBOARD):
		assert not keysDownInX(ground, device), (device, keysDownInX(ground, device))


def run(ground: "Ground") -> None:
	ground.openWindow()
	ground.waitForHelper()
	held = ground.uhidKeyboard(UHID)
	ground.startLinda()
	ground.waitForLindaLog(KERNEL_ROUTE, timeout=30)
	alone = ground.keyboard(UINPUT)
	ground.waitForHelperLog(f"({UINPUT}): ")
	lindaKeyWorks(ground, held)

	with ground.subtest("a key another program sends through XTEST is heard as on the X route"):
		ground.settle(APART)
		mark = ground.mark()
		# Released in the order a person releases them: xdotool's own key Insert+t lets Insert go first.
		ground.run(["xdotool", "keydown", "Insert", "keydown", "t", "keyup", "t", "keyup", "Insert"])
		ground.expect(WINDOW_TITLE, since=mark)
		ground.settle(0.5)
		reached = [r for r in ground.keys("app", mark) if r["key"] in CHORD_KEYS]
		assert not reached, reached
		ground.settle(APART)
		mark = ground.mark()
		ground.run(["xdotool", "key", "r"])
		app = ground.waitForKeys("app", [("KEY_R", 1), ("KEY_R", 0)], mark)
		assert app[0]["keyval"] == "r", app
		ground.expect("r", since=mark)
		ground.settle(0.5)
		assert [text for _t, text in ground.speech(mark)].count("r") == 1, ground.speech(mark)

	with ground.subtest("typing on a held keyboard reaches Linda once and is echoed once"):
		ground.settle(APART)
		letters = "abcdefghij"
		before = len(ground.lindaLogRecords(TYPING_INPUT))
		mark = ground.mark()
		written = held.tap(*[f"KEY_{c.upper()}" for c in letters], hold=0.03, gap=0.15)
		typed = [(r["key"], r["value"]) for r in written]
		ground.waitForKeys("x", typed, mark, device=VIRTUAL_KEYBOARD)
		app = ground.waitForKeys("app", typed, mark)
		assert "".join(r["keyval"] for r in app if r["value"] == 1) == letters, app
		ground.expect(*letters, since=mark)
		ground.settle(1)
		spoken = [text for _t, text in ground.speech(mark)]
		assert [spoken.count(c) for c in letters] == [1] * len(letters), spoken
		inputs = len(ground.lindaLogRecords(TYPING_INPUT)) - before
		ground.machine.log(f"{len(letters)} letters typed on the held keyboard, {inputs} reached Linda")
		assert inputs == len(letters), inputs
		held.chord("KEY_LEFTCTRL", "KEY_A")
		held.tap("KEY_DELETE")

	with ground.subtest("typing on both at once reaches Linda once a key, in the order typed"):
		ground.settle(APART)
		before = len(ground.lindaLogRecords(TYPING_INPUT))
		mark = ground.mark()
		for heldKey, aloneKey in (("KEY_Q", "KEY_W"), ("KEY_E", "KEY_R"), ("KEY_U", "KEY_I")):
			held.tap(heldKey, hold=0.03)
			alone.tap(aloneKey, hold=0.03)
		app = ground.waitForKeys(
			"app",
			[(k, v) for k in ("KEY_Q", "KEY_W", "KEY_E", "KEY_R", "KEY_U", "KEY_I") for v in (1, 0)],
			mark,
		)
		assert "".join(r["keyval"] for r in app if r["value"] == 1) == "qwerui", app
		ground.expect("q", "w", "e", "r", "u", "i", since=mark)
		ground.settle(1)
		spoken = [text for _t, text in ground.speech(mark)]
		assert [spoken.count(c) for c in "qwerui"] == [1] * 6, spoken
		inputs = len(ground.lindaLogRecords(TYPING_INPUT)) - before
		assert inputs == 6, inputs
		held.chord("KEY_LEFTCTRL", "KEY_A")
		held.tap("KEY_DELETE")

	with ground.subtest("the Linda key held on the keyboard left alone, T on the held one"):
		ground.settle(APART)
		mark = ground.mark()
		alone.run({"press": "KEY_INSERT"}, {"sleep": 0.1})
		held.run({"tap": "KEY_T", "hold": 0.05}, {"sleep": 0.1})
		alone.run({"release": "KEY_INSERT"})
		ground.settle(1)
		ground.machine.log(
			f"Insert held on the keyboard left alone, T on the held one: Linda spoke {ground.speech(mark)}, "
			f"the program had {[(r['key'], r['value']) for r in ground.keys('app', mark)]}"
		)
		balanced(ground, mark)
		plainLetter(ground, alone, "KEY_U", "u")
		plainLetter(ground, held, "KEY_V", "v")
		lindaKeyWorks(ground, alone)
		lindaKeyWorks(ground, held)

	with ground.subtest("the Linda key held on the held keyboard, T on the one left alone"):
		ground.settle(APART)
		mark = ground.mark()
		held.run({"press": "KEY_INSERT"}, {"sleep": 0.1})
		alone.run({"tap": "KEY_T", "hold": 0.05}, {"sleep": 0.1})
		held.run({"release": "KEY_INSERT"})
		ground.settle(1)
		ground.machine.log(
			f"Insert held on the held keyboard, T on the one left alone: Linda spoke {ground.speech(mark)}, "
			f"the program had {[(r['key'], r['value']) for r in ground.keys('app', mark)]}"
		)
		balanced(ground, mark)
		plainLetter(ground, alone, "KEY_X", "x")
		plainLetter(ground, held, "KEY_Z", "z")
		lindaKeyWorks(ground, alone)
		lindaKeyWorks(ground, held)

	with ground.subtest("Shift held on one keyboard and the Linda key on the other, released crosswise"):
		ground.settle(APART)
		mark = ground.mark()
		held.run({"press": "KEY_LEFTSHIFT"}, {"sleep": 0.05})
		alone.run({"press": "KEY_INSERT"}, {"sleep": 0.05})
		held.run({"tap": "KEY_B", "hold": 0.05}, {"sleep": 0.05}, {"release": "KEY_LEFTSHIFT"})
		alone.run({"sleep": 0.05}, {"release": "KEY_INSERT"})
		ground.settle(1)
		balanced(ground, mark)
		plainLetter(ground, alone, "KEY_K", "k")
		plainLetter(ground, held, "KEY_L", "l")
		assert len(ground.lindaLogRecords(X_ROUTE)) == 1, "Linda left the kernel route"
