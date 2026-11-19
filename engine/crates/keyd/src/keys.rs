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

pub const KEY_ESC: u16 = 1;
pub const KEY_BACKSPACE: u16 = 14;
pub const KEY_ENTER: u16 = 28;
pub const KEY_KPENTER: u16 = 96;
pub const KEY_LEFTSHIFT: u16 = 42;
pub const KEY_CAPSLOCK: u16 = 58;
pub const KEY_A: u16 = 30;
pub const KEY_INSERT: u16 = 110;
pub const KEY_POWER: u16 = 116;
pub const KEY_MAX: u16 = 0x2ff;
pub const KEY_CNT: usize = KEY_MAX as usize + 1;

pub const LED_NUML: u16 = 0;
pub const LED_CAPSL: u16 = 1;
pub const LED_SCROLLL: u16 = 2;
pub const LED_COMPOSE: u16 = 3;
pub const LED_KANA: u16 = 4;
/// The lock lights the virtual keyboard has, and the only ones the helper copies.
pub const LEDS: [u16; 5] = [LED_NUML, LED_CAPSL, LED_SCROLLL, LED_COMPOSE, LED_KANA];

/// The emergency chord: a key of each of these held down together makes the helper let go of every
/// keyboard. keyd's panic sequence, which every keyboard has and nothing presses by accident, with
/// the numpad's Enter as good as the main one, since a hand may find either first.
pub const CHORD: [&[u16]; 3] = [&[KEY_BACKSPACE], &[KEY_ESC], &[KEY_ENTER, KEY_KPENTER]];

/// The codes udev requires of a keyboard for ID_INPUT_KEYBOARD: Escape, the digits, the row from
/// Q, and A and S, every code from 1 to 31 (test_key in systemd's udev-builtin-input_id.c). Power
/// buttons, lid switches, mice, and the nodes that carry only media keys or brightness keys fail
/// it.
pub const KEYBOARD_KEYS: std::ops::RangeInclusive<u16> = 1..=31;

/// Whether the virtual keyboard can send this code: every key code but the buttons of mice,
/// joysticks, gamepads, tablets and wheels, which would make udev take the virtual keyboard for
/// one of those.
pub fn is_virtual_key(code: u16) -> bool {
    matches!(code, 1..=0xff | 0x160..=0x21f | 0x224..=0x2bf | 0x2e8..=KEY_MAX)
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
///   grab would swallow.
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
    if has(EV_REL) || has(EV_ABS) {
        return Assessment::Ignored(Ignored::Pointer);
    }
    if has(EV_SW) {
        return Assessment::Ignored(Ignored::Switches);
    }
    if d.keys.iter().any(|c| !is_virtual_key(c)) {
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

    #[test]
    fn keyboard_like_devices_that_cannot_be_passed_on_whole_are_left_alone() {
        let mut remapper = keyboard("keyd virtual keyboard");
        remapper.parentless = true;
        let mut touchpad = keyboard("Logitech K400");
        touchpad.types |= 1 << EV_REL;
        let mut tablet = keyboard("Tablet keyboard");
        tablet.types |= 1 << EV_ABS;
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
    fn the_virtual_keyboard_declares_no_buttons() {
        for code in 0x100..=0x15f {
            assert!(!is_virtual_key(code), "{code:#x}");
        }
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
