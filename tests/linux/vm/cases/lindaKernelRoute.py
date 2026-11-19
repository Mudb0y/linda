"""Linda with linda-keyd running: it says in its log that keys come from the kernel, and how long
after its start that was, and the Linda key with T reads the title and never reaches the program, on
the kernel route and on the way to it from the X route, while the helper comes up under a running
Linda.
"""

from typing import TYPE_CHECKING

from ground import KERNEL_ROUTE, VIRTIO_KEYBOARD, VIRTUAL_KEYBOARD, WINDOW_TITLE, X_ROUTE

if TYPE_CHECKING:
	from ground import Ground, Mark

UHID = "Ground uhid keyboard"
CHORD_KEYS = ("KEY_INSERT", "KEY_T")
#: Longer than NVDA's double press, after which a second Linda key with T spells the title.
APART = 0.8


def leaked(ground: "Ground", mark: "Mark") -> list[dict]:
	return [r for r in ground.keys("app", mark) if r["key"] in CHORD_KEYS]


def titles(ground: "Ground", mark: "Mark") -> int:
	return sum(text == WINDOW_TITLE for _t, text in ground.speech(mark))


def run(ground: "Ground") -> None:
	ground.openWindow()
	ground.waitForHelper()
	keyboard = ground.uhidKeyboard(UHID)
	ground.startLinda()

	with ground.subtest("Linda's log says keys come from the kernel, and when"):
		kernelAt, record = ground.waitForLindaLog(KERNEL_ROUTE, timeout=30)
		assert "linda-keyd admitted NVDA" in record, record
		startAt = ground.lindaLogRecords("Starting NVDA version")[0][0]
		xAt, xRecord = ground.lindaLogRecords(X_ROUTE)[0]
		assert "until linda-keyd holds the keyboard" in xRecord, xRecord
		ground.machine.log(
			f"Linda read keys from X {xAt - startAt:.3f} s after its start, and from the kernel "
			f"{kernelAt - startAt:.3f} s after it, {kernelAt - xAt:.3f} s after the X route"
		)

	with ground.subtest("on the kernel route the Linda key with T is Linda's alone"):
		for name, press in (
			(UHID, lambda: keyboard.chord(*CHORD_KEYS)),
			(VIRTIO_KEYBOARD, lambda: ground.press("insert-t")),
		):
			mark = ground.mark()
			press()
			ground.expect(WINDOW_TITLE, since=mark)
			ground.settle(0.5)
			assert not leaked(ground, mark), (name, leaked(ground, mark))
			# Kept in the kernel: neither the X server nor any reader of the keyboards had it.
			for source in ("evdev", "x"):
				chord = [r for r in ground.keys(source, mark) if r["key"] in CHORD_KEYS]
				assert not chord, (name, source, chord)
			ground.settle(APART)

	with ground.subtest("typing on the kernel route reaches the program through the virtual keyboard"):
		mark = ground.mark()
		keyboard.tap("KEY_A", "KEY_B")
		typed = [("KEY_A", 1), ("KEY_A", 0), ("KEY_B", 1), ("KEY_B", 0)]
		ground.waitForKeys("x", typed, mark, device=VIRTUAL_KEYBOARD)
		app = ground.waitForKeys("app", typed, mark)
		assert [r["keyval"] for r in app if r["value"] == 1] == ["a", "b"], app
		ground.expect("a", "b", since=mark)

	with ground.subtest("with the helper stopped, Linda reads X and the Linda key is still its own"):
		ground.shell("systemctl stop linda-keyd.service")
		ground.waitForLindaLog(X_ROUTE, count=2, timeout=10)
		mark = ground.mark()
		keyboard.chord(*CHORD_KEYS)
		ground.expect(WINDOW_TITLE, since=mark)
		ground.waitForKeys(
			"x", [("KEY_INSERT", 1), ("KEY_T", 1), ("KEY_T", 0), ("KEY_INSERT", 0)], mark, device=UHID
		)
		ground.settle(0.5)
		assert not leaked(ground, mark), leaked(ground, mark)
		ground.settle(APART)

	with ground.subtest("while the helper comes back, the Linda key with T is Linda's throughout"):
		mark = ground.mark()
		ground.shell("systemctl start linda-keyd.service")
		presses = 0
		after = 0
		while after < 3:
			switched = len(ground.lindaLogRecords(KERNEL_ROUTE)) >= 2
			keyboard.chord(*CHORD_KEYS)
			presses += 1
			ground.waitFor(lambda want=presses: titles(ground, mark) >= want, f"title number {presses}", 10)
			assert not leaked(ground, mark), (presses, leaked(ground, mark))
			after += switched
			assert presses < 40, "Linda did not move to the kernel route within 30 s of the helper's start"
			ground.settle(APART)
		ground.settle(0.5)
		assert titles(ground, mark) == presses, (presses, ground.speech(mark))
		assert not leaked(ground, mark), leaked(ground, mark)
		ground.machine.log(
			f"{presses} presses of the Linda key with T over the change, {presses - after} of them before Linda "
			"logged the kernel route, every one Linda's"
		)
