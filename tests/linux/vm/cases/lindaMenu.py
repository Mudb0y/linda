"""The fault xRouteMenuLeak records, ended by the kernel route: with a menu open, and with a program
that has taken the keyboard for itself as a password prompt does, the Linda key with T is spoken and
never reaches the program. The same grab on the X route, with the helper stopped, still leaks, and is
recorded as the expected failure it is there.
"""

from typing import TYPE_CHECKING

from ground import KERNEL_ROUTE, VIRTIO_KEYBOARD, WINDOW_TITLE, X_ROUTE

if TYPE_CHECKING:
	from ground import Ground, Mark

UHID = "Ground uhid keyboard"
GRABBER = "Grabbing window"
CHORD_KEYS = ("KEY_INSERT", "KEY_T")
APART = 0.8


def reached(ground: "Ground", mark: "Mark") -> list[dict]:
	return [r for r in ground.keys("app", mark) if r["key"] in CHORD_KEYS]


def run(ground: "Ground") -> None:
	ground.openWindow()
	ground.waitForHelper()
	keyboard = ground.uhidKeyboard(UHID)
	ground.startLinda()
	ground.waitForLindaLog(KERNEL_ROUTE, timeout=30)

	with ground.subtest("F10 opens the File menu"):
		mark = ground.mark()
		ground.press("f10")
		ground.waitFor(
			lambda: any(r["kind"] == "menu" and r["change"] == "shown" for r in ground.records("app", mark)),
			"the File menu to be shown",
		)
		ground.expect("File\N{HORIZONTAL ELLIPSIS}", since=mark)

	with ground.subtest("in the open menu the Linda key with T is spoken and the menu never has it"):
		for name, press in (
			(VIRTIO_KEYBOARD, lambda: ground.press("insert-t")),
			(UHID, lambda: keyboard.chord(*CHORD_KEYS)),
		):
			mark = ground.mark()
			press()
			ground.expect(f"{WINDOW_TITLE}\N{HORIZONTAL ELLIPSIS}", since=mark)
			ground.settle(1)
			assert not reached(ground, mark), (name, reached(ground, mark))
			for source in ("evdev", "x"):
				chord = [r for r in ground.keys(source, mark) if r["key"] in CHORD_KEYS]
				assert not chord, (name, source, chord)
			assert any(
				r["kind"] == "menu" and r["change"] == "shown" for r in ground.records("app")
			) and not any(r["kind"] == "menu" and r["change"] == "hidden" for r in ground.records("app"))
			ground.settle(APART)

	with ground.subtest("Escape, passed on, closes the menu"):
		mark = ground.mark()
		ground.press("esc")
		ground.waitFor(
			lambda: any(r["kind"] == "menu" and r["change"] == "hidden" for r in ground.records("app", mark)),
			"the File menu to be hidden",
		)

	ground.openWindow(GRABBER, name="grabber", grab=True)

	with ground.subtest("a program holding the keyboard never has the Linda key with T"):
		ground.settle(APART)
		mark = ground.mark()
		keyboard.chord(*CHORD_KEYS)
		ground.expect(f"{GRABBER}\N{HORIZONTAL ELLIPSIS}", since=mark)
		ground.settle(1)
		assert not reached(ground, mark), reached(ground, mark)
		# The grab is real: a key Linda passes on comes to the grabbing window.
		mark = ground.mark()
		keyboard.tap("KEY_G")
		app = ground.waitForKeys("app", [("KEY_G", 1), ("KEY_G", 0)], mark)
		assert {r["window"] for r in app} == {GRABBER}, app

	with ground.subtest("the same grab on the X route, with the helper stopped"):
		ground.shell("systemctl stop linda-keyd.service")
		ground.waitForLindaLog(X_ROUTE, count=2)
		ground.settle(APART)
		mark = ground.mark()
		keyboard.chord(*CHORD_KEYS)
		ground.expect(f"{GRABBER}\N{HORIZONTAL ELLIPSIS}", since=mark)
		ground.settle(1)
		with ground.expectedFailure("on the X route Linda's key reaches a program that holds the keyboard"):
			assert not reached(ground, mark), (
				f"the grabbing window had Linda's chord: {reached(ground, mark)}"
			)
