"""The codes linda-keyd types for a client, TYPEABLE in engine/crates/keyd/src/keys.rs, held to the keys
Linda knows, the FIXED, NUMPAD and LAYOUT tables of source/linuxPlatform/keys/keyMap.py, without
KEY_SLEEP, so that the two cannot drift apart. Run as a script, it prints the Rust constant those tables
make. Run under the rig:

	nix develop . -c tests/linux/rig python3 -m pytest tests/linux/test_keydTypedKeys.py
	nix develop . -c tests/linux/rig python3 tests/linux/test_keydTypedKeys.py
"""

import importlib.util
import re
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
KEYS_RS = ROOT / "engine" / "crates" / "keyd" / "src" / "keys.rs"
KEY_MAP = ROOT / "source" / "linuxPlatform" / "keys" / "keyMap.py"

KEY_SLEEP = 142
#: KEY_POWER, KEY_SUSPEND, KEY_POWER2 and KEY_RESTART, which logind takes from any keyboard as the
#: machine's own.
POWER_KEYS = (116, 205, 0x164, 0x198)


def tableCodes() -> set[int]:
	"""The evdev codes keyMap.py's tables name, loaded from its file alone, which imports only
	xkbcommon."""
	spec = importlib.util.spec_from_file_location("lindaKeyMapForTyping", KEY_MAP)
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return set(module.FIXED) | set(module.NUMPAD) | set(module.LAYOUT)


def typeable() -> list[int]:
	return sorted(tableCodes() - {KEY_SLEEP, *POWER_KEYS})


def rustConstant(codes: list[int]) -> str:
	numbers = textwrap.fill(", ".join(str(code) for code in codes) + ",", width=96)
	return f"pub const TYPEABLE: [u16; {len(codes)}] = [\n{textwrap.indent(numbers, '    ')}\n];"


def rustTypeable() -> tuple[int, list[int]]:
	"""The length TYPEABLE declares, and its codes in the order written."""
	match = re.search(
		r"^pub const TYPEABLE: \[u16; (\d+)\] = \[([^\]]*)\];$", KEYS_RS.read_text(), re.MULTILINE
	)
	assert match, "keys.rs has no TYPEABLE constant"
	return int(match.group(1)), [int(code, 0) for code in re.findall(r"0x[0-9a-fA-F]+|\d+", match.group(2))]


def test_theHelperTypesExactlyTheKeysLindaKnowsButSleep():
	declared, codes = rustTypeable()
	assert declared == len(codes)
	assert codes == sorted(set(codes)), (
		"TYPEABLE must be in rising order, each code once, for its binary search"
	)
	assert set(codes) == tableCodes() - {KEY_SLEEP}
	assert codes == typeable()


def test_theHelperTypesNoPowerKey():
	_declared, codes = rustTypeable()
	assert KEY_SLEEP in tableCodes(), "the tables no longer name KEY_SLEEP, so leaving it out proves nothing"
	for code in (KEY_SLEEP, *POWER_KEYS):
		assert code not in codes, code


if __name__ == "__main__":
	print(rustConstant(typeable()))
