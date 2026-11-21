"""Prints the keymap a Wayland compositor gives its clients, for the key codes NVDA's key map names.

It binds the seat's keyboard and maps no surface, so it takes no focus and sees no key: the
compositor sends the keymap to every keyboard a client binds. That is the keymap programs type
with, which a layout feed has to match.

	waylandKeymapDump.py [--key-map KEYMAP_PY] [--timeout SECONDS]

The first line is "layouts" and the keymap's layout names, separated by tabs. Then a line for each
evdev code keyMap.py's FIXED, NUMPAD and LAYOUT tables name, in each of the key's layouts: the code,
the layout's index, and the keysym names of its levels, all separated by tabs, with NoSymbol for a
level with none. A code the keymap gives no symbols has no line.

KEYMAP_PY is source/linuxPlatform/keys/keyMap.py of this checkout by default. The exit status is 1
when the compositor sent no keymap in time.
"""

import argparse
import importlib.util
import mmap
import os
import sys
import time
from pathlib import Path

KEY_MAP = Path(__file__).resolve().parent.parent.parent / "source" / "linuxPlatform" / "keys" / "keyMap.py"
#: wl_seat.capability.keyboard, and wl_keyboard.keymap_format.xkb_v1.
SEAT_KEYBOARD = 2
KEYMAP_FORMAT_XKB_V1 = 1


def namedCodes(keyMapPath: Path) -> list[int]:
	"""The evdev codes keyMap.py's tables name, loaded from its file alone, which imports only xkbcommon."""
	spec = importlib.util.spec_from_file_location("lindaKeyMapTables", keyMapPath)
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return sorted(set(module.FIXED) | set(module.NUMPAD) | set(module.LAYOUT))


def readKeymap(timeout: float) -> str | None:
	"""The text of the first keymap the compositor sends this client's keyboard, or None."""
	from pywayland.client import Display
	from pywayland.protocol.wayland import WlSeat

	display = Display()
	display.connect()
	registry = display.get_registry()
	found: dict[str, tuple[int, int]] = {}

	def onGlobal(_registry, name, interface, version):
		found.setdefault(interface, (name, version))

	registry.dispatcher["global"] = onGlobal
	display.roundtrip()
	seatName, seatVersion = found["wl_seat"]
	seat = registry.bind(seatName, WlSeat, min(seatVersion, 7))
	got: list[str] = []
	keyboards = []

	def onCapabilities(_seat, capabilities):
		if capabilities & SEAT_KEYBOARD and not keyboards:
			keyboard = seat.get_keyboard()
			keyboard.dispatcher["keymap"] = onKeymap
			keyboards.append(keyboard)

	def onKeymap(_keyboard, keymapFormat, fd, size):
		try:
			if keymapFormat == KEYMAP_FORMAT_XKB_V1:
				with mmap.mmap(fd, size, mmap.MAP_PRIVATE, mmap.PROT_READ) as data:
					got.append(data[:].rstrip(b"\0").decode())
		finally:
			os.close(fd)

	seat.dispatcher["capabilities"] = onCapabilities
	deadline = time.monotonic() + timeout
	while not got and time.monotonic() < deadline:
		display.roundtrip()
		if not got:
			time.sleep(0.05)
	display.disconnect()
	return got[0] if got else None


def describe(text: str, codes: list[int]) -> list[str]:
	from xkbcommon import xkb

	keymap = xkb.Context().keymap_new_from_string(text)
	lines = ["\t".join(["layouts", *(keymap.layout_get_name(i) for i in range(keymap.num_layouts()))])]
	for evdev in codes:
		keycode = evdev + 8
		if not keymap.min_keycode() <= keycode <= keymap.max_keycode():
			continue
		for layout in range(keymap.num_layouts_for_key(keycode)):
			levels = []
			for level in range(keymap.num_levels_for_key(keycode, layout)):
				syms = keymap.key_get_syms_by_level(keycode, layout, level)
				levels.append(xkb.keysym_get_name(syms[0]) if syms else "NoSymbol")
			if levels:
				lines.append("\t".join([str(evdev), str(layout), *levels]))
	return lines


def main() -> int:
	parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
	parser.add_argument("--key-map", type=Path, default=KEY_MAP)
	parser.add_argument("--timeout", type=float, default=5)
	args = parser.parse_args()
	codes = namedCodes(args.key_map)
	text = readKeymap(args.timeout)
	if text is None:
		print("waylandKeymapDump: the compositor sent no keymap", file=sys.stderr)
		return 1
	print("\n".join(describe(text, codes)))
	return 0


if __name__ == "__main__":
	sys.exit(main())
