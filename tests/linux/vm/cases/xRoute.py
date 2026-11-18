"""Today's X route on a real kernel keyboard path: Linda hears the emulated keyboard and a second,
fake one, keeps its own chord from the program, and passes typed keys on, in order.

The recorders' delays are written to the log, for comparison with the kernel route once it exists.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
	from ground import Ground

FAKE = "Ground fake keyboard"
KEPT_CHORD = [("KEY_INSERT", 1), ("KEY_T", 1), ("KEY_T", 0), ("KEY_INSERT", 0)]


def run(ground: "Ground") -> None:
	ground.openWindow()
	ground.startLinda()

	with ground.subtest("the Linda key with T on the emulated keyboard reads the title"):
		mark = ground.mark()
		ground.press("insert-t")
		ground.expect("Ground window", since=mark)
		evdev = ground.waitForKeys("evdev", KEPT_CHORD, mark)
		emulated = evdev[0]["device"]
		ground.waitForKeys("x", KEPT_CHORD, mark, device=emulated)
		# The passive grab on the focused window keeps the chord from the program.
		ground.settle(0.5)
		app = [r for r in ground.keys("app", mark) if r["key"] in ("KEY_INSERT", "KEY_T")]
		assert not app, f"the chord reached the program: {app}"
		ground.machine.log(f"the emulated keyboard is {emulated!r}")

	with ground.subtest("typing on the emulated keyboard is echoed and reaches the field"):
		mark = ground.mark()
		ground.type("ab")
		ground.expect("a", "b", since=mark)
		typed = [("KEY_A", 1), ("KEY_A", 0), ("KEY_B", 1), ("KEY_B", 0)]
		evdev = ground.waitForKeys("evdev", typed, mark)
		x = ground.waitForKeys("x", typed, mark)
		app = ground.waitForKeys("app", typed, mark)
		assert {r["window"] for r in app} == {"Ground window"}, app
		for name, earlier, later in (("kernel to X", evdev, x), ("X to the program", x, app)):
			ground.machine.log(f"{name}: {ground.delays(earlier, later)}")

	keyboard = ground.keyboard(FAKE)

	with ground.subtest("the Linda key with T on the fake keyboard reads the title"):
		mark = ground.mark()
		keyboard.chord("KEY_INSERT", "KEY_T")
		ground.expect("Ground window", since=mark)
		ground.waitForKeys("evdev", KEPT_CHORD, mark, device=FAKE)
		ground.waitForKeys("x", KEPT_CHORD, mark, device=FAKE)
		ground.settle(0.5)
		app = [r for r in ground.keys("app", mark) if r["key"] in ("KEY_INSERT", "KEY_T")]
		assert not app, f"the chord reached the program: {app}"

	with ground.subtest("keys typed fast on the fake keyboard reach the program in order"):
		mark = ground.mark()
		written = keyboard.tap("KEY_C", "KEY_D", "KEY_E", hold=0.01, gap=0.005)
		ground.expect("c", "d", "e", since=mark)
		typed = [(r["key"], r["value"]) for r in written]
		app = ground.waitForKeys("app", typed, mark)
		ground.machine.log(f"fake keyboard to the program: {ground.delays(written, app)}")

	with ground.subtest("a keyboard that vanishes with Shift held has it released by the kernel"):
		vanishing = ground.keyboard("Ground vanishing keyboard")
		mark = ground.mark()
		vanishing.run({"press": "KEY_LEFTSHIFT"}, {"sleep": 0.1}, {"vanish": True})
		ground.waitForDevice("evdev", "Ground vanishing keyboard", change="removed")
		ground.waitForDevice("x", "Ground vanishing keyboard", change="removed")
		shift = [("KEY_LEFTSHIFT", 1), ("KEY_LEFTSHIFT", 0)]
		ground.waitForKeys("evdev", shift, mark, device="Ground vanishing keyboard")
		ground.waitForKeys("x", shift, mark)
		# Typing afterwards is lower case: nothing still holds Shift down.
		mark = ground.mark()
		keyboard.tap("KEY_F")
		app = ground.waitForKeys("app", [("KEY_F", 1), ("KEY_F", 0)], mark)
		assert app[0]["keyval"] == "f", app
