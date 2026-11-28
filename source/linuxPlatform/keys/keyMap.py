"""From an X key code and the XKB state to what the Windows keyboard hook would report for the key.

The Windows hook gives keyboardHandler a virtual-key code, a PC set 1 scan code and an extended
flag. The scan code and the extended flag are the key's position, which on Linux the kernel's evdev
code fixes: an X key code is the evdev code plus 8. The virtual-key code is fixed by position for
every key outside the alphanumeric block, whose code Windows takes from the active layout; here it
comes from the first level of the key in the active XKB group, as Windows layouts assign them.
Numpad keys change code with Num Lock, as on Windows.
"""

from xkbcommon import xkb

#: Windows virtual-key codes used below.
VK = {
	"BACK": 0x08,
	"TAB": 0x09,
	"CLEAR": 0x0C,
	"RETURN": 0x0D,
	"SHIFT": 0x10,
	"CONTROL": 0x11,
	"MENU": 0x12,
	"PAUSE": 0x13,
	"CAPITAL": 0x14,
	"ESCAPE": 0x1B,
	"SPACE": 0x20,
	"PRIOR": 0x21,
	"NEXT": 0x22,
	"END": 0x23,
	"HOME": 0x24,
	"LEFT": 0x25,
	"UP": 0x26,
	"RIGHT": 0x27,
	"DOWN": 0x28,
	"SNAPSHOT": 0x2C,
	"INSERT": 0x2D,
	"DELETE": 0x2E,
	"LWIN": 0x5B,
	"RWIN": 0x5C,
	"APPS": 0x5D,
	"SLEEP": 0x5F,
	"NUMPAD0": 0x60,
	"MULTIPLY": 0x6A,
	"ADD": 0x6B,
	"SEPARATOR": 0x6C,
	"SUBTRACT": 0x6D,
	"DECIMAL": 0x6E,
	"DIVIDE": 0x6F,
	"F1": 0x70,
	"F13": 0x7C,
	"NUMLOCK": 0x90,
	"SCROLL": 0x91,
	"LSHIFT": 0xA0,
	"RSHIFT": 0xA1,
	"LCONTROL": 0xA2,
	"RCONTROL": 0xA3,
	"LMENU": 0xA4,
	"RMENU": 0xA5,
	"BROWSER_BACK": 0xA6,
	"BROWSER_FORWARD": 0xA7,
	"BROWSER_REFRESH": 0xA8,
	"BROWSER_STOP": 0xA9,
	"BROWSER_SEARCH": 0xAA,
	"BROWSER_FAVORITES": 0xAB,
	"BROWSER_HOME": 0xAC,
	"VOLUME_MUTE": 0xAD,
	"VOLUME_DOWN": 0xAE,
	"VOLUME_UP": 0xAF,
	"MEDIA_NEXT_TRACK": 0xB0,
	"MEDIA_PREV_TRACK": 0xB1,
	"MEDIA_STOP": 0xB2,
	"MEDIA_PLAY_PAUSE": 0xB3,
	"LAUNCH_MAIL": 0xB4,
	"LAUNCH_MEDIA_SELECT": 0xB5,
	"LAUNCH_APP1": 0xB6,
	"LAUNCH_APP2": 0xB7,
	"OEM_1": 0xBA,
	"OEM_PLUS": 0xBB,
	"OEM_COMMA": 0xBC,
	"OEM_MINUS": 0xBD,
	"OEM_PERIOD": 0xBE,
	"OEM_2": 0xBF,
	"OEM_3": 0xC0,
	"ABNT_C1": 0xC1,
	"OEM_4": 0xDB,
	"OEM_5": 0xDC,
	"OEM_6": 0xDD,
	"OEM_7": 0xDE,
	"OEM_8": 0xDF,
	"OEM_102": 0xE2,
	"PACKET": 0xE7,
	"NONE": 0xFF,
}

#: Virtual-key codes whose physical key varies with the keyboard layout (punctuation and dead keys),
#: so a PC set 1 scan code from the sender locates the key directly when one is given (issue 68).
OEM_VKS = frozenset(
	{
		VK["OEM_1"],
		VK["OEM_PLUS"],
		VK["OEM_COMMA"],
		VK["OEM_MINUS"],
		VK["OEM_PERIOD"],
		VK["OEM_2"],
		VK["OEM_3"],
		VK["ABNT_C1"],
		VK["OEM_4"],
		VK["OEM_5"],
		VK["OEM_6"],
		VK["OEM_7"],
		VK["OEM_8"],
		VK["OEM_102"],
	},
)

#: evdev code: (set 1 scan code, extended, virtual-key code), for keys whose code is their position.
FIXED: dict[int, tuple[int, bool, int]] = {
	1: (0x01, False, VK["ESCAPE"]),
	14: (0x0E, False, VK["BACK"]),
	15: (0x0F, False, VK["TAB"]),
	28: (0x1C, False, VK["RETURN"]),
	29: (0x1D, False, VK["LCONTROL"]),
	42: (0x2A, False, VK["LSHIFT"]),
	54: (0x36, False, VK["RSHIFT"]),
	55: (0x37, False, VK["MULTIPLY"]),
	56: (0x38, False, VK["LMENU"]),
	57: (0x39, False, VK["SPACE"]),
	58: (0x3A, False, VK["CAPITAL"]),
	# The Windows hook reports Num Lock as extended and Pause as not, though the scan codes say otherwise.
	69: (0x45, True, VK["NUMLOCK"]),
	70: (0x46, False, VK["SCROLL"]),
	74: (0x4A, False, VK["SUBTRACT"]),
	78: (0x4E, False, VK["ADD"]),
	87: (0x57, False, VK["F1"] + 10),
	88: (0x58, False, VK["F1"] + 11),
	96: (0x1C, True, VK["RETURN"]),
	97: (0x1D, True, VK["RCONTROL"]),
	98: (0x35, True, VK["DIVIDE"]),
	99: (0x37, True, VK["SNAPSHOT"]),
	100: (0x38, True, VK["RMENU"]),
	102: (0x47, True, VK["HOME"]),
	103: (0x48, True, VK["UP"]),
	104: (0x49, True, VK["PRIOR"]),
	105: (0x4B, True, VK["LEFT"]),
	106: (0x4D, True, VK["RIGHT"]),
	107: (0x4F, True, VK["END"]),
	108: (0x50, True, VK["DOWN"]),
	109: (0x51, True, VK["NEXT"]),
	110: (0x52, True, VK["INSERT"]),
	111: (0x53, True, VK["DELETE"]),
	113: (0x20, True, VK["VOLUME_MUTE"]),
	114: (0x2E, True, VK["VOLUME_DOWN"]),
	115: (0x30, True, VK["VOLUME_UP"]),
	119: (0x45, False, VK["PAUSE"]),
	121: (0x7E, False, VK["SEPARATOR"]),
	125: (0x5B, True, VK["LWIN"]),
	126: (0x5C, True, VK["RWIN"]),
	127: (0x5D, True, VK["APPS"]),
	128: (0x68, True, VK["BROWSER_STOP"]),
	140: (0x21, True, VK["LAUNCH_APP2"]),
	142: (0x5F, True, VK["SLEEP"]),
	155: (0x6C, True, VK["LAUNCH_MAIL"]),
	156: (0x66, True, VK["BROWSER_FAVORITES"]),
	157: (0x6B, True, VK["LAUNCH_APP1"]),
	158: (0x6A, True, VK["BROWSER_BACK"]),
	159: (0x69, True, VK["BROWSER_FORWARD"]),
	163: (0x19, True, VK["MEDIA_NEXT_TRACK"]),
	164: (0x22, True, VK["MEDIA_PLAY_PAUSE"]),
	165: (0x10, True, VK["MEDIA_PREV_TRACK"]),
	166: (0x24, True, VK["MEDIA_STOP"]),
	172: (0x32, True, VK["BROWSER_HOME"]),
	173: (0x67, True, VK["BROWSER_REFRESH"]),
	200: (0x22, True, VK["MEDIA_PLAY_PAUSE"]),
	201: (0x22, True, VK["MEDIA_PLAY_PAUSE"]),
	217: (0x65, True, VK["BROWSER_SEARCH"]),
	226: (0x6D, True, VK["LAUNCH_MEDIA_SELECT"]),
}
FIXED.update({59 + i: (0x3B + i, False, VK["F1"] + i) for i in range(10)})
FIXED.update(
	{
		183 + i: (scan, False, VK["F13"] + i)
		for i, scan in enumerate((0x64, 0x65, 0x66, 0x67, 0x68, 0x69, 0x6A, 0x6B, 0x6C, 0x6D, 0x6E, 0x76))
	},
)

#: Numpad keys: evdev code: (scan code, vk with Num Lock on, vk with it off). None is extended.
NUMPAD: dict[int, tuple[int, int, int]] = {
	71: (0x47, VK["NUMPAD0"] + 7, VK["HOME"]),
	72: (0x48, VK["NUMPAD0"] + 8, VK["UP"]),
	73: (0x49, VK["NUMPAD0"] + 9, VK["PRIOR"]),
	75: (0x4B, VK["NUMPAD0"] + 4, VK["LEFT"]),
	76: (0x4C, VK["NUMPAD0"] + 5, VK["CLEAR"]),
	77: (0x4D, VK["NUMPAD0"] + 6, VK["RIGHT"]),
	79: (0x4F, VK["NUMPAD0"] + 1, VK["END"]),
	80: (0x50, VK["NUMPAD0"] + 2, VK["DOWN"]),
	81: (0x51, VK["NUMPAD0"] + 3, VK["NEXT"]),
	82: (0x52, VK["NUMPAD0"] + 0, VK["INSERT"]),
	83: (0x53, VK["DECIMAL"], VK["DELETE"]),
}

#: The alphanumeric block: evdev code: (scan code, the vk a US layout gives the key).
LAYOUT: dict[int, tuple[int, int]] = {
	2: (0x02, ord("1")),
	3: (0x03, ord("2")),
	4: (0x04, ord("3")),
	5: (0x05, ord("4")),
	6: (0x06, ord("5")),
	7: (0x07, ord("6")),
	8: (0x08, ord("7")),
	9: (0x09, ord("8")),
	10: (0x0A, ord("9")),
	11: (0x0B, ord("0")),
	12: (0x0C, VK["OEM_MINUS"]),
	13: (0x0D, VK["OEM_PLUS"]),
	26: (0x1A, VK["OEM_4"]),
	27: (0x1B, VK["OEM_6"]),
	39: (0x27, VK["OEM_1"]),
	40: (0x28, VK["OEM_7"]),
	41: (0x29, VK["OEM_3"]),
	43: (0x2B, VK["OEM_5"]),
	51: (0x33, VK["OEM_COMMA"]),
	52: (0x34, VK["OEM_PERIOD"]),
	53: (0x35, VK["OEM_2"]),
	86: (0x56, VK["OEM_102"]),
	89: (0x73, VK["ABNT_C1"]),
}
LAYOUT.update({16 + i: (0x10 + i, ord(c)) for i, c in enumerate("QWERTYUIOP")})
LAYOUT.update({30 + i: (0x1E + i, ord(c)) for i, c in enumerate("ASDFGHJKL")})
LAYOUT.update({44 + i: (0x2C + i, ord(c)) for i, c in enumerate("ZXCVBNM")})

#: PC set 1 scan code: evdev code for the alphanumeric block.
_LAYOUT_BY_SCAN: dict[int, int] = {scan: evdev for evdev, (scan, _vk) in LAYOUT.items()}

#: The vk a key injected for a generic modifier is sent as.
GENERIC_MODIFIERS = {VK["SHIFT"]: VK["LSHIFT"], VK["CONTROL"]: VK["LCONTROL"], VK["MENU"]: VK["LMENU"]}

#: What a dead key names itself after, since Windows' fallback for them, GetKeyNameText, has no
#: Linux counterpart.
DEAD_SPACING = {
	"dead_grave": "`",
	"dead_acute": "´",
	"dead_circumflex": "^",
	"dead_tilde": "~",
	"dead_macron": "¯",
	"dead_breve": "˘",
	"dead_abovedot": "˙",
	"dead_diaeresis": "¨",
	"dead_abovering": "˚",
	"dead_doubleacute": "˝",
	"dead_caron": "ˇ",
	"dead_cedilla": "¸",
	"dead_ogonek": "˛",
}

#: Keysyms of a key that selects a level rather than acting as Alt: AltGr on most layouts.
LEVEL_SHIFT_KEYSYMS = frozenset(
	(xkb.keysym_from_name("ISO_Level3_Shift"), xkb.keysym_from_name("Mode_switch")),
)

_MODS_EFFECTIVE = xkb.StateComponent.XKB_STATE_MODS_EFFECTIVE


class Translator:
	"""Turns X key codes into the hook's (vk, scan, extended), and answers keyboardHandler's layout
	questions, for a keymap and a state that the caller keeps in step with the X server."""

	def __init__(self, keymap: xkb.Keymap, state: xkb.KeyboardState) -> None:
		self.keymap = keymap
		self.state = state

	def _layout(self, keycode: int) -> int | None:
		try:
			return self.state.key_get_layout(keycode)
		except xkb.XKBError:
			# A key the keymap gives no symbols, such as the Brazilian key on a US keymap.
			return None

	def _levelSyms(self, keycode: int, layout: int | None) -> list[int]:
		if layout is None:
			return []
		syms = []
		for level in range(self.keymap.num_levels_for_key(keycode, layout)):
			found = self.keymap.key_get_syms_by_level(keycode, layout, level)
			syms.append(found[0] if found else 0)
		return syms

	def _layoutVk(self, evdev: int, layout: int | None) -> int:
		usVk = LAYOUT[evdev][1]
		syms = self._levelSyms(evdev + 8, layout)
		base = xkb.keysym_to_string(syms[0]) if syms and syms[0] else None
		if base and len(base) == 1 and base.isascii() and "a" <= base.lower() <= "z":
			return ord(base.upper())
		# A digit on either of the first two levels, as on the French number row.
		for sym in syms[:2]:
			text = xkb.keysym_to_string(sym) if sym else None
			if text and len(text) == 1 and text.isascii() and text.isdigit():
				return ord(text)
		return usVk

	def isNumLockOn(self) -> bool:
		try:
			return self.state.mod_name_is_active("Mod2", _MODS_EFFECTIVE)
		except xkb.XKBError:
			return False

	def translate(self, keycode: int) -> tuple[int, int, bool]:
		"""(vk, scan, extended) for the key with this X key code, in the current state."""
		evdev = keycode - 8
		if evdev in FIXED:
			scan, extended, vk = FIXED[evdev]
			return vk, scan, extended
		if evdev in NUMPAD:
			scan, onVk, offVk = NUMPAD[evdev]
			# By Num Lock alone, as Windows names numpad7 whatever shift does to the key.
			return (onVk if self.isNumLockOn() else offVk), scan, False
		if evdev in LAYOUT:
			return self._layoutVk(evdev, self._layout(keycode)), LAYOUT[evdev][0], False
		return VK["NONE"], evdev & 0xFF, False

	def isLevelShift(self, keycode: int) -> bool:
		"""Whether the key is AltGr or another key that picks a level instead of being Alt."""
		syms = self._levelSyms(keycode, self._layout(keycode))
		return bool(syms) and syms[0] in LEVEL_SHIFT_KEYSYMS

	def level(self, keycode: int) -> int:
		"""The level the key types at in the current state: 0 alone, 1 shifted, 2 and above with AltGr."""
		layout = self._layout(keycode)
		if layout is None:
			return 0
		try:
			return self.state.key_get_level(keycode, layout)
		except xkb.XKBError:
			return 0

	def levelThreeMask(self) -> int:
		"""The X modifier mask AltGr sets, or 0 when no key of the keymap is AltGr."""
		third = xkb.keysym_from_name("ISO_Level3_Shift")
		for keycode in range(self.keymap.min_keycode(), self.keymap.max_keycode() + 1):
			syms = self._levelSyms(keycode, self._layout(keycode))
			if syms and syms[0] == third:
				state = self.keymap.state_new()
				state.update_key(keycode, xkb.KeyDirection.XKB_KEY_DOWN)
				# The real modifiers only, which are all a core grab knows.
				return state.serialize_mods(xkb.StateComponent.XKB_STATE_MODS_DEPRESSED) & 0xFF
		return 0

	def keycodeFor(self, vk: int, extended: bool, scan: int | None = None) -> int | None:
		"""The X key code that gives vk, for injecting it."""
		vk = GENERIC_MODIFIERS.get(vk, vk)
		if scan and not extended and vk in OEM_VKS and scan in _LAYOUT_BY_SCAN:
			return _LAYOUT_BY_SCAN[scan] + 8
		for evdev, (_scan, fixedExtended, fixedVk) in FIXED.items():
			if fixedVk == vk and fixedExtended == extended:
				return evdev + 8
		for evdev, (_scan, onVk, offVk) in NUMPAD.items():
			if vk in (onVk, offVk) and not extended:
				return evdev + 8
		for evdev, (_scan, fixedExtended, fixedVk) in FIXED.items():
			if fixedVk == vk:
				return evdev + 8
		for evdev in LAYOUT:
			if self._layoutVk(evdev, self._layout(evdev + 8)) == vk:
				return evdev + 8
		return None

	def mapVirtualKeyToChar(self, vk: int) -> int:
		"""As MapVirtualKeyEx(vk, MAPVK_VK_TO_CHAR) in the active layout: the key's unshifted character."""
		for evdev in LAYOUT:
			keycode = evdev + 8
			layout = self._layout(keycode)
			if self._layoutVk(evdev, layout) != vk:
				continue
			syms = self._levelSyms(keycode, layout)
			if not syms or not syms[0]:
				continue
			name = xkb.keysym_get_name(syms[0])
			if name.startswith("dead_"):
				return ord(DEAD_SPACING.get(name, "?"))
			text = xkb.keysym_to_string(syms[0]) or ""
			return ord(text) if len(text) == 1 else 0
		return 0

	def vkKeyScan(self, char: str) -> tuple[int, int]:
		"""As VkKeyScanEx: (modifiers, vk) that type char, 1 being shift, 2 control, 4 alt, and the
		third level, AltGr, 6, as Windows has it.

		:raise LookupError: When no key of the alphanumeric block types char.
		"""
		for evdev in LAYOUT:
			keycode = evdev + 8
			layout = self._layout(keycode)
			for level, sym in enumerate(self._levelSyms(keycode, layout)):
				if sym and xkb.keysym_to_string(sym) == char:
					mods = {0: 0, 1: 1, 2: 6, 3: 7}.get(level)
					if mods is not None:
						return mods, self._layoutVk(evdev, layout)
		raise LookupError(char)

	def keyName(self, scan: int, extended: bool) -> str:
		"""As GetKeyNameText: the name of the keysym the key gives on its first level, or ""."""
		for evdev, (fixedScan, fixedExtended, _vk) in FIXED.items():
			if (fixedScan, fixedExtended) == (scan, extended):
				break
		else:
			found = [evdev for evdev, (layoutScan, _vk) in LAYOUT.items() if layoutScan == scan]
			found += [evdev for evdev, (numpadScan, *_vks) in NUMPAD.items() if numpadScan == scan]
			if extended or not found:
				return ""
			evdev = found[0]
		syms = self._levelSyms(evdev + 8, self._layout(evdev + 8))
		return xkb.keysym_get_name(syms[0]) if syms and syms[0] else ""
