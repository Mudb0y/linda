//! The kernel's event codes the helper uses, a set of key codes, and which devices count as
//! keyboards.

use crate::protocol::Ignored;

pub const EV_SYN: u16 = 0x00;
pub const EV_KEY: u16 = 0x01;
pub const EV_REL: u16 = 0x02;
pub const EV_ABS: u16 = 0x03;
pub const EV_MSC: u16 = 0x04;
pub const EV_SW: u16 = 0x05;
pub const EV_LED: u16 = 0x11;
pub const EV_SND: u16 = 0x12;
pub const EV_REP: u16 = 0x14;
pub const EV_FF: u16 = 0x15;
pub const EV_MAX: u16 = 0x1f;

pub const SYN_REPORT: u16 = 0;
pub const SYN_DROPPED: u16 = 3;

pub const REL_X: u16 = 0x00;
pub const REL_Y: u16 = 0x01;
pub const REL_HWHEEL: u16 = 0x06;
pub const REL_WHEEL: u16 = 0x08;
pub const REL_HWHEEL_HI_RES: u16 = 0x0c;

pub const ABS_X: u16 = 0x00;
pub const ABS_Y: u16 = 0x01;
pub const ABS_VOLUME: u16 = 0x20;
pub const ABS_MT_POSITION_X: u16 = 0x35;
pub const ABS_MT_POSITION_Y: u16 = 0x36;

pub const KEY_ESC: u16 = 1;
pub const KEY_BACKSPACE: u16 = 14;
pub const KEY_ENTER: u16 = 28;
pub const KEY_KPENTER: u16 = 96;
pub const KEY_LEFTSHIFT: u16 = 42;
pub const KEY_CAPSLOCK: u16 = 58;
pub const KEY_A: u16 = 30;
pub const KEY_INSERT: u16 = 110;
pub const KEY_KP0: u16 = 82;
pub const KEY_LEFTALT: u16 = 56;
pub const KEY_RIGHTALT: u16 = 100;
pub const KEY_SYSRQ: u16 = 99;
pub const KEY_POWER: u16 = 116;
pub const KEY_SLEEP: u16 = 142;
pub const KEY_SUSPEND: u16 = 205;
pub const KEY_POWER2: u16 = 0x164;
pub const KEY_RESTART: u16 = 0x198;
pub const KEY_MAX: u16 = 0x2ff;
pub const KEY_CNT: usize = KEY_MAX as usize + 1;

pub const BTN_0: u16 = 0x100;
pub const BTN_1: u16 = 0x101;
pub const BTN_LEFT: u16 = 0x110;
pub const BTN_TOUCH: u16 = 0x14a;

pub const LED_NUML: u16 = 0;
pub const LED_CAPSL: u16 = 1;
pub const LED_SCROLLL: u16 = 2;
pub const LED_COMPOSE: u16 = 3;
pub const LED_KANA: u16 = 4;
/// The lock lights the virtual keyboard has, and the only ones the helper copies.
pub const LEDS: [u16; 5] = [LED_NUML, LED_CAPSL, LED_SCROLLL, LED_COMPOSE, LED_KANA];

/// The keys whose release changes what a later key means, which therefore keep their place in line:
/// both Shifts, Controls, Alts (the right one is AltGr) and Supers, and Caps Lock, which XKB options
/// often make a Control or a level shift held down like one. The release of any other key whose
/// press has gone out may overtake presses still waiting for their verdict.
pub const MODIFIERS: [u16; 9] = [29, 42, 54, 56, 97, 100, 125, 126, KEY_CAPSLOCK];

/// The keys a screen reader holds down to change what other keys mean, beyond the MODIFIERS:
/// Insert and the numpad's Insert, NVDA's own modifier keys, Caps Lock, its third, being among the
/// MODIFIERS already. A key of either list the kernel has down after dropping events, which the
/// helper had not told the client was down, is told as a press, so that a key typed under it is
/// not taken for one typed alone; the numpad's Insert only with Num Lock off, since with it on
/// the key is 0. To a program these two are keys like any other, so made up they are told and
/// never put on the virtual keyboard. A layout can make further keys modifiers, a level shift on
/// the key left of Z, say, which neither list knows: the same question, still open, as which keys
/// keep their place in line.
pub const READER_MODIFIERS: [u16; 2] = [KEY_INSERT, KEY_KP0];

/// Whether a key held down changes what other keys mean, by the two lists above.
pub fn modifies(code: u16) -> bool {
    MODIFIERS.contains(&code) || READER_MODIFIERS.contains(&code)
}

/// The emergency chord: a key of each of these held down together makes the helper let go of every
/// keyboard. keyd's panic sequence, which every keyboard has and nothing presses by accident, with
/// the numpad's Enter as good as the main one, since a hand may find either first.
pub const CHORD: [&[u16]; 3] = [&[KEY_BACKSPACE], &[KEY_ESC], &[KEY_ENTER, KEY_KPENTER]];

/// The codes udev requires of a keyboard for ID_INPUT_KEYBOARD: Escape, the digits, the row from
/// Q, and A and S, every code from 1 to 31 (test_key in systemd's udev-builtin-input_id.c). Power
/// buttons, lid switches, mice, and the nodes that carry only media keys or brightness keys fail
/// it.
pub const KEYBOARD_KEYS: std::ops::RangeInclusive<u16> = 1..=31;

/// Whether the virtual keyboard has this code as a key, one that goes to the client and may be
/// typed: every key code but the buttons of mice, joysticks, gamepads, tablets and wheels, which
/// would make udev take the virtual keyboard for one of those. Of the buttons it has only
/// [`CONSUMER_BUTTONS`], which are passed on and nothing more.
pub fn is_virtual_key(code: u16) -> bool {
    matches!(code, 1..=0xff | 0x160..=0x21f | 0x224..=0x2bf | 0x2e8..=KEY_MAX)
}

/// What the kernel's hid-input makes of a HID consumer control collection besides keys, which many
/// keyboards have on the same node as their keys, since hid-input keeps a consumer control
/// application with the keyboard of the same device (hidinput_match_application): AC Pan is the
/// horizontal wheel with its high-resolution twin, Volume is ABS_VOLUME, and Function Buttons is
/// BTN_0 (hidinput_configure_usage). The virtual keyboard has them all, and passes them on from a
/// keyboard it takes; nothing else that is not a key. BTN_1 to BTN_9, which Button usages outside
/// a pointer or joystick collection become, are left out: with BTN_0, BTN_1 and a wheel, udev takes
/// a device for a tablet pad (has_pad_buttons in udev-builtin-input_id.c, since systemd 253), and
/// libinput then reads no key of it.
pub const CONSUMER_RELS: [u16; 2] = [REL_HWHEEL, REL_HWHEEL_HI_RES];
pub const CONSUMER_ABS: [u16; 1] = [ABS_VOLUME];
pub const CONSUMER_BUTTONS: [u16; 1] = [BTN_0];

/// Whether an event of this type and code is one of the consumer controls above.
pub fn is_consumer_control(kind: u16, code: u16) -> bool {
    match kind {
        EV_REL => CONSUMER_RELS.contains(&code),
        EV_ABS => CONSUMER_ABS.contains(&code),
        EV_KEY => CONSUMER_BUTTONS.contains(&code),
        _ => false,
    }
}

/// The top of the range of ABS_VOLUME on the virtual keyboard, whose bottom is 0: fine enough that
/// no step of a control of up to sixteen bits is lost.
pub const VOLUME_MAX: i32 = 0xffff;

/// A keyboard's ABS_VOLUME, from its own range, `min` to `max`, as the kernel has it for that
/// keyboard, into the virtual keyboard's: the one virtual keyboard has one range for every keyboard
/// it passes on. Linear, a value outside the range taken as its nearer end, rounded half up; a
/// range with nothing in it gives the bottom.
pub fn rescale_volume(value: i32, min: i32, max: i32) -> i32 {
    if max <= min {
        return 0;
    }
    let (value, min, max) = (i64::from(value), i64::from(min), i64::from(max));
    let span = max - min;
    let scaled = ((value.clamp(min, max) - min) * 2 * i64::from(VOLUME_MAX) + span) / (2 * span);
    scaled as i32
}

/// The codes the client may type with TYPE, in rising order: those the FIXED, NUMPAD and LAYOUT
/// tables of source/linuxPlatform/keys/keyMap.py name, which are the keys Linda knows, without
/// KEY_SLEEP. Generated from those tables by `python3 tests/linux/test_keydTypedKeys.py`, under the
/// rig, and held to them by that file's test. logind takes the power keys of any keyboard, the
/// virtual one included, as the machine's, so none of them is here.
pub const TYPEABLE: [u16; 139] = [
    1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26,
    27, 28, 29, 30, 31, 32, 33, 34, 35, 36, 37, 38, 39, 40, 41, 42, 43, 44, 45, 46, 47, 48, 49, 50,
    51, 52, 53, 54, 55, 56, 57, 58, 59, 60, 61, 62, 63, 64, 65, 66, 67, 68, 69, 70, 71, 72, 73, 74,
    75, 76, 77, 78, 79, 80, 81, 82, 83, 86, 87, 88, 89, 96, 97, 98, 99, 100, 102, 103, 104, 105,
    106, 107, 108, 109, 110, 111, 113, 114, 115, 119, 121, 125, 126, 127, 128, 140, 155, 156, 157,
    158, 159, 163, 164, 165, 166, 172, 173, 183, 184, 185, 186, 187, 188, 189, 190, 191, 192, 193,
    194, 200, 201, 217, 226,
];

/// Whether the client may type this code.
pub fn typeable(code: u16) -> bool {
    TYPEABLE.binary_search(&code).is_ok()
}

/// A set of key codes, as the kernel's key bitmaps hold them.
#[derive(Clone, Default, PartialEq, Eq)]
pub struct KeySet([u64; KEY_CNT / 64]);

impl KeySet {
    pub fn new() -> Self {
        Self::default()
    }

    /// From a bitmap of `unsigned long`s, as EVIOCGBIT and EVIOCGKEY fill it.
    pub fn from_longs(bytes: &[u8]) -> Self {
        let mut s = KeySet::new();
        let word = std::mem::size_of::<libc::c_ulong>();
        for (i, chunk) in bytes.chunks_exact(word).enumerate() {
            let mut v = [0u8; 8];
            v[..word].copy_from_slice(chunk);
            let long = if word == 8 {
                u64::from_ne_bytes(v)
            } else {
                let mut w = [0u8; 4];
                w.copy_from_slice(chunk);
                u64::from(u32::from_ne_bytes(w))
            };
            for bit in 0..word * 8 {
                if long & (1 << bit) != 0 {
                    s.insert((i * word * 8 + bit) as u16);
                }
            }
        }
        s
    }

    /// From a capabilities file in sysfs: hexadecimal words, the highest first, of the kernel's
    /// `unsigned long` width.
    pub fn from_sysfs(text: &str, long_bits: usize) -> Option<Self> {
        let mut s = KeySet::new();
        for (i, word) in text.split_whitespace().rev().enumerate() {
            let v = u64::from_str_radix(word, 16).ok()?;
            for bit in 0..long_bits.min(64) {
                if v & (1 << bit) != 0 {
                    s.insert((i * long_bits + bit).try_into().ok()?);
                }
            }
        }
        Some(s)
    }

    pub fn insert(&mut self, code: u16) {
        if let Some(w) = self.0.get_mut(usize::from(code) / 64) {
            *w |= 1 << (code % 64);
        }
    }

    pub fn remove(&mut self, code: u16) {
        if let Some(w) = self.0.get_mut(usize::from(code) / 64) {
            *w &= !(1 << (code % 64));
        }
    }

    pub fn contains(&self, code: u16) -> bool {
        self.0
            .get(usize::from(code) / 64)
            .is_some_and(|w| w & (1 << (code % 64)) != 0)
    }

    pub fn is_empty(&self) -> bool {
        self.0.iter().all(|w| *w == 0)
    }

    pub fn iter(&self) -> impl Iterator<Item = u16> + '_ {
        (0..KEY_CNT as u16).filter(|c| self.contains(*c))
    }
}

impl std::fmt::Debug for KeySet {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_set().entries(self.iter()).finish()
    }
}

impl FromIterator<u16> for KeySet {
    fn from_iter<I: IntoIterator<Item = u16>>(codes: I) -> Self {
        let mut s = KeySet::new();
        for c in codes {
            s.insert(c);
        }
        s
    }
}

/// What the helper knows of an input device before deciding whether to take it.
#[derive(Debug, Clone)]
pub struct Description {
    pub name: String,
    /// Bit n set when the device sends events of type n.
    pub types: u32,
    pub keys: KeySet,
    /// Bit n set when the device has relative axis n.
    pub rel: u64,
    /// Bit n set when the device has absolute axis n.
    pub abs: u64,
    /// The device has no parent device: it was made by a program through uinput.
    pub parentless: bool,
    /// The udev property ID_SEAT, absent for seat0.
    pub seat: Option<String>,
    /// The device is the helper's own virtual keyboard.
    pub ours: bool,
}

/// Whether to take a device.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Assessment {
    Keyboard,
    /// It looks like a keyboard but is not taken, for this reason; the client is told.
    Ignored(Ignored),
    /// It is not a keyboard at all, or it is the helper's own: never mentioned.
    NotKeyboard,
}

/// Which devices count as keyboards the helper takes. A device is taken only when every event it
/// can send is one the virtual keyboard passes on, since a grab takes every event of the device:
/// - the helper's own virtual keyboard never, or its own keys would come round to it again;
/// - it must have every code udev's keyboard test asks for (see [`KEYBOARD_KEYS`]), which leaves
///   out power and sleep buttons, lid switches, mice, and the separate nodes of media and
///   brightness keys, all of which reach the desktop as before and never come to the client;
/// - no device made through uinput by another program, which shows in the kernel as a device with
///   no parent. Another key remapper such as keyd grabs every keyboard but its own, the helper's
///   virtual one included; were the helper to grab the remapper's in turn, each key would go round
///   the two for ever. Keys typed on such a device reach the desktop without the client hearing
///   them. Bluetooth keyboards are not of this kind: they have the HID device as parent, through
///   uhid where BlueZ makes them so;
/// - only devices of seat0, the seat the helper serves;
/// - nothing that is also a pointer or touch device (relative or absolute axes), has switches, or
///   has mouse, joystick or tablet buttons, whose events the virtual keyboard cannot carry and the
///   grab would swallow. The axes and buttons of a consumer control collection (see
///   [`CONSUMER_RELS`]) are not of these: the virtual keyboard carries them. udev's ID_INPUT_*
///   properties are not relied on for this, so that the helper never holds a device it cannot pass
///   on whole.
pub fn assess(d: &Description) -> Assessment {
    let has = |t: u16| d.types & (1 << t) != 0;
    if d.ours || !has(EV_KEY) || !KEYBOARD_KEYS.clone().all(|c| d.keys.contains(c)) {
        return Assessment::NotKeyboard;
    }
    if d.parentless {
        return Assessment::Ignored(Ignored::Virtual);
    }
    if d.seat.as_deref().is_some_and(|s| s != "seat0") {
        return Assessment::Ignored(Ignored::OtherSeat);
    }
    let axes =
        |kind: u16, mask: u64| (0..64).any(|c| mask & 1 << c != 0 && !is_consumer_control(kind, c));
    if axes(EV_REL, d.rel) || axes(EV_ABS, d.abs) {
        return Assessment::Ignored(Ignored::Pointer);
    }
    if has(EV_SW) {
        return Assessment::Ignored(Ignored::Switches);
    }
    if d.keys
        .iter()
        .any(|c| !is_virtual_key(c) && !is_consumer_control(EV_KEY, c))
    {
        return Assessment::Ignored(Ignored::Buttons);
    }
    Assessment::Keyboard
}

#[cfg(test)]
pub mod tests {
    use super::*;

    pub fn full_keyboard() -> KeySet {
        (1..=0x7f).collect()
    }

    pub fn description(name: &str, types: &[u16], keys: KeySet) -> Description {
        Description {
            name: name.into(),
            types: types.iter().fold(0, |t, b| t | 1 << b),
            keys,
            rel: 0,
            abs: 0,
            parentless: false,
            seat: None,
            ours: false,
        }
    }

    pub fn keyboard(name: &str) -> Description {
        description(
            name,
            &[EV_SYN, EV_KEY, EV_MSC, EV_LED, EV_REP],
            full_keyboard(),
        )
    }

    #[test]
    fn a_keyboard_is_taken() {
        assert_eq!(
            assess(&keyboard("AT Translated Set 2 keyboard")),
            Assessment::Keyboard
        );
        // A keyboard with media keys and a beeper, on a seat0 named outright.
        let mut d = keyboard("USB keyboard");
        d.keys.insert(0x160);
        d.keys.insert(KEY_MAX);
        d.types |= 1 << EV_SND;
        d.seat = Some("seat0".into());
        assert_eq!(assess(&d), Assessment::Keyboard);
    }

    #[test]
    fn buttons_switches_mice_and_media_keys_are_never_taken() {
        let power = description(
            "Power Button",
            &[EV_SYN, EV_KEY],
            [KEY_POWER].into_iter().collect(),
        );
        let lid = description("Lid Switch", &[EV_SYN, EV_SW], KeySet::new());
        let mouse = description(
            "USB mouse",
            &[EV_SYN, EV_KEY, EV_REL, EV_MSC],
            (0x110..=0x117).collect(),
        );
        let media = description(
            "USB keyboard Consumer Control",
            &[EV_SYN, EV_KEY, EV_ABS, EV_MSC],
            [113, 114, 115, 163, 164, 165].into_iter().collect(),
        );
        let video = description(
            "Video Bus",
            &[EV_SYN, EV_KEY],
            [224, 225].into_iter().collect(),
        );
        // Every key but one of udev's keyboard keys.
        let mut almost = keyboard("Macro pad");
        almost.keys.remove(KEY_A);
        for d in [power, lid, mouse, media, video, almost] {
            assert_eq!(assess(&d), Assessment::NotKeyboard, "{}", d.name);
        }
    }

    #[test]
    fn the_helpers_own_keyboard_is_never_taken() {
        let mut d = keyboard("Linda virtual keyboard");
        d.ours = true;
        d.parentless = true;
        assert_eq!(assess(&d), Assessment::NotKeyboard);
    }

    /// A keyboard with the axes and buttons of a consumer control collection on its node, as
    /// hid-input gives them.
    pub fn consumer_keyboard(name: &str) -> Description {
        let mut d = keyboard(name);
        d.types |= 1 << EV_REL | 1 << EV_ABS;
        d.rel = 1 << REL_HWHEEL | 1 << REL_HWHEEL_HI_RES;
        d.abs = 1 << ABS_VOLUME;
        d.keys.insert(BTN_0);
        d
    }

    #[test]
    fn keyboard_like_devices_that_cannot_be_passed_on_whole_are_left_alone() {
        let mut remapper = keyboard("keyd virtual keyboard");
        remapper.parentless = true;
        let mut touchpad = keyboard("Logitech K400");
        touchpad.types |= 1 << EV_REL;
        touchpad.rel = 1 << REL_X | 1 << REL_Y;
        let mut tablet = keyboard("Tablet keyboard");
        tablet.types |= 1 << EV_ABS;
        tablet.abs = 1 << ABS_X | 1 << ABS_Y;
        let mut switches = keyboard("Convertible keyboard");
        switches.types |= 1 << EV_SW;
        let mut buttons = keyboard("Keyboard with a mouse button");
        buttons.keys.insert(0x110);
        let mut seat1 = keyboard("Second seat keyboard");
        seat1.seat = Some("seat1".into());
        for (d, why) in [
            (remapper, Ignored::Virtual),
            (touchpad, Ignored::Pointer),
            (tablet, Ignored::Pointer),
            (switches, Ignored::Switches),
            (buttons, Ignored::Buttons),
            (seat1, Ignored::OtherSeat),
        ] {
            assert_eq!(assess(&d), Assessment::Ignored(why), "{}", d.name);
        }
    }

    #[test]
    fn a_keyboard_with_a_consumer_control_collection_is_taken() {
        // A Keychron Q6 Ultra 8K's keyboard node, as /proc/bus/input/devices has it: EV=12001f,
        // REL=1040, ABS=100000000, and its key bitmap, with BTN_0 among some 280 keys.
        let keys = KeySet::from_sysfs(
            "3f00733fff 0 0 483ffff17aff32d bfd4444600000000 1 130ff38b17d007 \
             ffff7bfad9415fff ffbeffdfffefffff fffffffffffffffe",
            64,
        )
        .unwrap();
        assert!(keys.contains(BTN_0) && keys.contains(0x265));
        let mut d = description(
            "Keychron Keychron Q6 Ultra 8K",
            &[EV_SYN, EV_KEY, EV_REL, EV_ABS, EV_MSC, EV_LED, EV_REP],
            keys,
        );
        d.rel = 0x1040;
        d.abs = 0x1_0000_0000;
        assert_eq!(d.types, 0x12001f);
        assert_eq!(assess(&d), Assessment::Keyboard);
        assert_eq!(
            assess(&consumer_keyboard("USB keyboard")),
            Assessment::Keyboard
        );
        // Each part of the collection alone.
        for (rel, abs, button) in [
            (1 << REL_HWHEEL, 0, None),
            (0, 1 << ABS_VOLUME, None),
            (0, 0, Some(BTN_0)),
        ] {
            let mut d = keyboard("USB keyboard");
            d.rel = rel;
            d.abs = abs;
            if let Some(b) = button {
                d.keys.insert(b);
            }
            assert_eq!(
                assess(&d),
                Assessment::Keyboard,
                "{rel:#x} {abs:#x} {button:?}"
            );
        }
    }

    #[test]
    fn a_consumer_control_collection_does_not_let_in_a_pointer() {
        let mut cases = Vec::new();
        for (name, rel, abs) in [
            ("relative pointer", 1 << REL_X | 1 << REL_Y, 0),
            ("vertical wheel", 1 << REL_WHEEL, 0),
            ("absolute pointer", 0, 1 << ABS_X | 1 << ABS_Y),
            ("touch", 0, 1 << ABS_MT_POSITION_X | 1 << ABS_MT_POSITION_Y),
        ] {
            let mut d = consumer_keyboard(name);
            d.rel |= rel;
            d.abs |= abs;
            cases.push((d, Ignored::Pointer));
        }
        for (name, button) in [
            ("mouse button", BTN_LEFT),
            ("touch", BTN_TOUCH),
            ("BTN_1", BTN_1),
        ] {
            let mut d = consumer_keyboard(name);
            d.keys.insert(button);
            cases.push((d, Ignored::Buttons));
        }
        let mut switches = consumer_keyboard("switches");
        switches.types |= 1 << EV_SW;
        cases.push((switches, Ignored::Switches));
        for (d, why) in cases {
            assert_eq!(assess(&d), Assessment::Ignored(why), "{}", d.name);
        }
    }

    #[test]
    fn volume_is_rescaled_into_the_virtual_keyboards_range() {
        assert_eq!(rescale_volume(0, 0, 4095), 0);
        assert_eq!(rescale_volume(4095, 0, 4095), VOLUME_MAX);
        // 65535 / 4095 is 16.0037, and half of it 8.0018.
        assert_eq!(rescale_volume(1, 0, 4095), 16);
        assert_eq!(rescale_volume(2047, 0, 4095), 32759);
        assert_eq!(rescale_volume(-1, -1, 1), 0);
        assert_eq!(rescale_volume(0, -1, 1), 32768, "half up");
        assert_eq!(rescale_volume(1, -1, 1), VOLUME_MAX);
        // Outside the range, the nearer end; no range at all, the bottom.
        assert_eq!(rescale_volume(5000, 0, 4095), VOLUME_MAX);
        assert_eq!(rescale_volume(-5, 0, 4095), 0);
        assert_eq!(rescale_volume(i32::MAX, i32::MIN, i32::MAX), VOLUME_MAX);
        assert_eq!(rescale_volume(i32::MIN, i32::MIN, i32::MAX), 0);
        assert_eq!(rescale_volume(7, 7, 7), 0);
        assert_eq!(rescale_volume(7, 9, 2), 0);
    }

    #[test]
    fn the_virtual_keyboard_declares_no_buttons_but_that_of_consumer_controls() {
        for code in 0x100..=0x15f {
            assert!(!is_virtual_key(code), "{code:#x}");
            assert_eq!(
                is_consumer_control(EV_KEY, code),
                code == BTN_0,
                "{code:#x}"
            );
        }
        // Not a tablet pad to udev.
        assert!(
            !(CONSUMER_BUTTONS.contains(&BTN_0) && CONSUMER_BUTTONS.contains(&BTN_1)),
            "BTN_0 and BTN_1 with a wheel make a tablet pad"
        );
        assert!(!CONSUMER_RELS.contains(&REL_X) && !CONSUMER_ABS.contains(&ABS_X));
        for code in [0x220, 0x223, 0x2c0, 0x2e7, 0, 0x300] {
            assert!(!is_virtual_key(code), "{code:#x}");
        }
        for code in [
            1,
            KEY_CAPSLOCK,
            0xff,
            0x160,
            0x21f,
            0x224,
            0x2bf,
            0x2e8,
            KEY_MAX,
        ] {
            assert!(is_virtual_key(code), "{code:#x}");
        }
        assert!(
            KEYBOARD_KEYS
                .clone()
                .chain(CHORD.into_iter().flatten().copied())
                .all(is_virtual_key)
        );
    }

    #[test]
    fn only_keys_the_virtual_keyboard_sends_and_no_power_key_may_be_typed() {
        assert!(
            TYPEABLE.windows(2).all(|w| w[0] < w[1]),
            "not in rising order"
        );
        assert!(TYPEABLE.iter().all(|c| is_virtual_key(*c)));
        for code in [
            KEY_POWER,
            KEY_SLEEP,
            KEY_SUSPEND,
            KEY_POWER2,
            KEY_RESTART,
            0,
            0x110,
            BTN_0,
        ] {
            assert!(!typeable(code), "{code:#x}");
        }
        for code in [
            1,
            KEY_A,
            KEY_LEFTALT,
            KEY_RIGHTALT,
            KEY_SYSRQ,
            KEY_INSERT,
            KEY_KP0,
            226,
        ] {
            assert!(typeable(code), "{code}");
        }
    }

    #[test]
    fn key_sets_read_the_kernels_bitmaps() {
        // Bits 1 and 31 of the first word, bit 0 of the third, as sysfs writes 64-bit words.
        let s = KeySet::from_sysfs("1 0 80000002", 64).unwrap();
        assert_eq!(s.iter().collect::<Vec<_>>(), vec![1, 31, 128]);
        let s = KeySet::from_sysfs("1 0 80000002", 32).unwrap();
        assert_eq!(s.iter().collect::<Vec<_>>(), vec![1, 31, 64]);
        assert!(KeySet::from_sysfs("zz", 64).is_none());
        let mut bytes = vec![0u8; KEY_CNT / 8];
        bytes[0] = 0b10;
        bytes[7] = 0x80;
        bytes[95] = 0x80;
        let s = KeySet::from_longs(&bytes);
        if cfg!(target_endian = "little") {
            assert_eq!(s.iter().collect::<Vec<_>>(), vec![1, 63, KEY_MAX]);
        }
        let mut s: KeySet = [KEY_ESC, KEY_MAX].into_iter().collect();
        s.remove(KEY_ESC);
        s.insert(9999);
        assert!(!s.contains(KEY_ESC) && s.contains(KEY_MAX) && !s.contains(9999));
        assert!(!s.is_empty());
        s.remove(KEY_MAX);
        assert!(s.is_empty());
    }
}
