"""The owner's own setup on the kernel route: the Polish programmer's layout, whose AltGr letters are
typed and echoed; Insert, the numpad's Insert and Caps Lock, each the Linda key; Caps Lock never
toggling when Linda keeps it and passing on when pressed twice; Num Lock and the numpad review keys.

All on a uhid keyboard, which the helper holds, so that every key goes through it.
"""

from typing import TYPE_CHECKING

from ground import KERNEL_ROUTE, WINDOW_TITLE

if TYPE_CHECKING:
	from ground import Ground, Mark

UHID = "Ground uhid keyboard"
APART = 0.8
#: Key typed with AltGr: (GDK's name for what it types, the letter).
ALTGR_LETTERS = {
	"KEY_A": ("aogonek", "ą"),
	"KEY_C": ("cacute", "ć"),
	"KEY_E": ("eogonek", "ę"),
	"KEY_L": ("lstroke", "ł"),
	"KEY_N": ("nacute", "ń"),
	"KEY_O": ("oacute", "ó"),
	"KEY_S": ("sacute", "ś"),
	"KEY_X": ("zacute", "ź"),
	"KEY_Z": ("zabovedot", "ż"),
}
NVDA_KEYS = ("KEY_INSERT", "KEY_KP0", "KEY_CAPSLOCK")
#: The kernel's light bit of Caps Lock and of Num Lock, as the uhid keyboard records its lights.
CAPS_LIGHT = 1 << 1
NUM_LIGHT = 1 << 0


def locks(ground: "Ground") -> dict[str, bool]:
	"""Caps Lock and Num Lock as the X server has them."""
	out = ground.run(["xset", "q"]).stdout
	found = {}
	for name in ("Caps Lock", "Num Lock"):
		after = out.split(f"{name}:", 1)[1].split()[0]
		found[name] = after == "on"
	return found


def pressed(ground: "Ground", mark: "Mark", key: str) -> list[dict]:
	return [r for r in ground.keys("app", mark) if r["key"] == key and r["value"] == 1]


def run(ground: "Ground") -> None:
	ground.openWindow()
	ground.waitForHelper()
	keyboard = ground.uhidKeyboard(UHID)
	ground.startLinda()
	ground.waitForLindaLog(KERNEL_ROUTE, timeout=30)
	assert locks(ground) == {"Caps Lock": False, "Num Lock": False}, locks(ground)

	with ground.subtest("AltGr letters of the Polish programmer's layout are typed and echoed"):
		for key, (keyval, letter) in ALTGR_LETTERS.items():
			mark = ground.mark()
			keyboard.chord("KEY_RIGHTALT", key)
			altGr = [("KEY_RIGHTALT", 1), (key, 1), (key, 0), ("KEY_RIGHTALT", 0)]
			app = ground.waitForKeys("app", altGr, mark)
			assert app[1]["keyval"] == keyval, (key, app)
			ground.expect(letter, since=mark)
		mark = ground.mark()
		keyboard.chord("KEY_LEFTSHIFT", "KEY_RIGHTALT", "KEY_L")
		shifted = [
			("KEY_LEFTSHIFT", 1),
			("KEY_RIGHTALT", 1),
			("KEY_L", 1),
			("KEY_L", 0),
			("KEY_RIGHTALT", 0),
			("KEY_LEFTSHIFT", 0),
		]
		app = ground.waitForKeys("app", shifted, mark)
		assert app[2]["keyval"] == "Lstroke", app
		ground.expect("Ł", since=mark)
		log = ground.lindaLog()
		assert "alt+a" not in log and "alt+l" not in log, "an AltGr letter was taken for a command"

	with ground.subtest("Insert, the numpad's Insert and Caps Lock are each the Linda key"):
		for nvdaKey in NVDA_KEYS:
			ground.settle(APART)
			mark = ground.mark()
			keyboard.chord(nvdaKey, "KEY_T")
			ground.expect(WINDOW_TITLE, since=mark)
			ground.settle(0.5)
			for source in ("evdev", "x", "app"):
				chord = [r for r in ground.keys(source, mark) if r["key"] in (nvdaKey, "KEY_T")]
				assert not chord, (nvdaKey, source, chord)
		# Kept, Caps Lock reached nothing that owns the lock, so it is still off, light and all.
		assert locks(ground)["Caps Lock"] is False
		assert not [r for r in keyboard.records() if r.get("leds", 0) & CAPS_LIGHT], keyboard.records()

	with ground.subtest("Caps Lock held with nothing else and let go toggles nothing"):
		ground.settle(APART)
		mark = ground.mark()
		keyboard.tap("KEY_CAPSLOCK", hold=0.2)
		ground.settle(0.5)
		assert locks(ground)["Caps Lock"] is False
		assert not pressed(ground, mark, "KEY_CAPSLOCK")

	with ground.subtest("Caps Lock pressed twice passes on and toggles the lock"):
		ground.settle(APART)
		mark = ground.mark()
		keyboard.tap("KEY_CAPSLOCK", "KEY_CAPSLOCK", hold=0.03, gap=0.1)
		ground.expect("caps lock on", since=mark)
		ground.waitFor(lambda: locks(ground)["Caps Lock"], "Caps Lock to be on")
		assert len(pressed(ground, mark, "KEY_CAPSLOCK")) == 1, ground.keys("app", mark)
		ground.waitFor(
			lambda: any(r.get("leds", 0) & CAPS_LIGHT for r in keyboard.records(mark)), "the Caps Lock light"
		)
		mark = ground.mark()
		keyboard.tap("KEY_Q")
		app = ground.waitForKeys("app", [("KEY_Q", 1), ("KEY_Q", 0)], mark)
		assert app[0]["keyval"] == "Q", app
		ground.settle(APART)
		mark = ground.mark()
		keyboard.tap("KEY_CAPSLOCK", "KEY_CAPSLOCK", hold=0.03, gap=0.1)
		ground.expect("caps lock off", since=mark)
		ground.waitFor(lambda: not locks(ground)["Caps Lock"], "Caps Lock to be off")
		mark = ground.mark()
		keyboard.tap("KEY_Q")
		app = ground.waitForKeys("app", [("KEY_Q", 1), ("KEY_Q", 0)], mark)
		assert app[0]["keyval"] == "q", app

	with ground.subtest("with Num Lock off the numpad reviews, with it on it types"):
		mark = ground.mark()
		keyboard.tap("KEY_END", "KEY_SPACE", "KEY_W", "KEY_O", "KEY_R", "KEY_D")
		ground.expect("w", "o", "r", "d", since=mark)
		ground.settle(APART)
		mark = ground.mark()
		keyboard.tap("KEY_KP8")
		ground.expect("\N{HORIZONTAL ELLIPSIS}", since=mark)
		spoken = ground.speech(mark)
		assert spoken and spoken[0][1].endswith("word"), spoken
		ground.settle(0.5)
		assert not ground.keys("app", mark), ground.keys("app", mark)
		mark = ground.mark()
		keyboard.tap("KEY_NUMLOCK")
		ground.expect("num lock on", since=mark)
		ground.waitFor(lambda: locks(ground)["Num Lock"], "Num Lock to be on")
		mark = ground.mark()
		keyboard.tap("KEY_KP8", "KEY_KP0")
		app = ground.waitForKeys(
			"app", [("KEY_KP8", 1), ("KEY_KP8", 0), ("KEY_KP0", 1), ("KEY_KP0", 0)], mark
		)
		assert [r["keyval"] for r in app if r["value"] == 1] == ["KP_8", "KP_0"], app
		ground.expect("8", "0", since=mark)
		mark = ground.mark()
		keyboard.tap("KEY_NUMLOCK")
		ground.expect("num lock off", since=mark)
		ground.waitFor(lambda: not locks(ground)["Num Lock"], "Num Lock to be off")
		ground.settle(APART)
		mark = ground.mark()
		keyboard.chord("KEY_KP0", "KEY_T")
		ground.expect(WINDOW_TITLE, since=mark)
		ground.settle(0.5)
		assert not ground.keys("app", mark), ground.keys("app", mark)
