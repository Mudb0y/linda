"""The fault the kernel route is to end: with a menu open, the program holds the keyboard, and Linda's
keys reach it as well as Linda.

On X, Linda keeps its keys with passive grabs on the focused window, which the X server does not
honour while a program holds an active grab, as a GTK menu does. Linda still hears the key, from the
raw events the X server sends whatever the grabs, so it acts, and the menu has the key too.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
	from ground import Ground


def run(ground: "Ground") -> None:
	ground.openWindow()
	ground.startLinda()

	with ground.subtest("F10 opens the File menu"):
		mark = ground.mark()
		ground.press("f10")
		ground.waitFor(
			lambda: any(r["kind"] == "menu" and r["change"] == "shown" for r in ground.records("app", mark)),
			"the File menu to be shown",
		)
		ground.expect("File\N{HORIZONTAL ELLIPSIS}", since=mark)

	with ground.subtest("the Linda key with T in the open menu"):
		mark = ground.mark()
		ground.press("insert-t")
		ground.expect("Ground window\N{HORIZONTAL ELLIPSIS}", since=mark)
		chord = [("KEY_INSERT", 1), ("KEY_T", 1), ("KEY_T", 0), ("KEY_INSERT", 0)]
		ground.waitForKeys("x", chord, mark)

		def reached() -> list[dict]:
			return [r for r in ground.keys("app", mark) if r["key"] in ("KEY_INSERT", "KEY_T")]

		# The program has had as long as Linda to see the chord, and a second more.
		ground.settle(1)
		with ground.expectedFailure("Linda's key reaches a program that holds the keyboard"):
			assert not reached(), f"the program had Linda's chord: {reached()}"

	ground.press("esc")
