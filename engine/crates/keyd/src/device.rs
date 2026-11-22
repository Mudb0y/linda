//! The device layer: the real keyboards, the one virtual keyboard, and the notice of keyboards
//! coming and going, behind traits so that the helper's logic can be tested against a mock. The
//! real implementation is in [`crate::evdev`].

use std::io;
use std::os::fd::BorrowedFd;
use std::sync::Arc;

use crate::keys::{Assessment, KeySet};

/// One event as the kernel reports it, without its time.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct InputEvent {
    pub kind: u16,
    pub code: u16,
    pub value: i32,
}

/// An input device found by a scan, not yet opened.
#[derive(Debug, Clone)]
pub struct Found {
    /// The kernel's number of the input device, N in /sys/class/input/inputN. The kernel never
    /// gives a number twice in one boot, so it tells a keyboard that came back from the one that
    /// went, even when both have the same event node.
    pub number: u32,
    pub node: String,
    pub name: String,
    pub assessment: Assessment,
}

#[derive(Debug)]
pub enum ReadError {
    /// The device is gone.
    Gone,
    Io(io::Error),
}

#[derive(Debug)]
pub enum GrabError {
    /// Another program holds the device.
    Busy,
    Gone,
    Io(io::Error),
}

pub trait Devices {
    type Keyboard: Keyboard;
    type Virtual: VirtualKeyboard;

    /// Every input device of interest there is now: the keyboards with their assessment, and the
    /// keyboard-like devices with why they are not taken.
    fn scan(&mut self) -> io::Result<Vec<Found>>;
    /// Opens a keyboard a scan found, not grabbed. Fails if it is no longer that device, or no
    /// longer one to take.
    fn open(&mut self, found: &Found) -> io::Result<Self::Keyboard>;
    /// Makes the virtual keyboard.
    fn create_virtual(&mut self) -> io::Result<Self::Virtual>;
    /// Starts or stops watching for keyboards that come and go.
    fn watch(&mut self, on: bool) -> io::Result<()>;
    /// What to poll for the watch, while it is on.
    fn watch_fd(&self) -> Option<BorrowedFd<'_>>;
    /// Empties the watch's queue; true when something may have come or gone.
    fn watch_drain(&mut self) -> bool;
    /// What the watchdog thread uses to let go of everything from outside the loop.
    fn guard(&self) -> Arc<dyn Guard>;
}

pub trait Keyboard {
    fn fd(&self) -> BorrowedFd<'_>;
    /// Appends every event waiting to `out`, without blocking.
    fn read(&mut self, out: &mut Vec<InputEvent>) -> Result<(), ReadError>;
    /// The keys down now, as the kernel holds them.
    fn keys_down(&self) -> io::Result<KeySet>;
    fn grab(&mut self) -> Result<(), GrabError>;
    /// Lets go. Dropping the keyboard lets go too, as closing the device does.
    /// False when the kernel would not let go, as when the call is interrupted; the keyboard is
    /// then still grabbed, and closing it is what lets go.
    fn ungrab(&mut self) -> bool;
    /// The lock lights that are on, as bits of the LED codes.
    fn leds(&self) -> io::Result<u16>;
    fn set_leds(&mut self, leds: u16) -> io::Result<()>;
}

pub trait VirtualKeyboard {
    fn fd(&self) -> BorrowedFd<'_>;
    /// Whether the device has appeared: it has an event node, which udev has processed, so that
    /// the desktop can have been told of it.
    fn appeared(&mut self) -> io::Result<bool>;
    /// Puts out one key event and the report that ends it.
    fn emit(&mut self, code: u16, value: i32) -> io::Result<()>;
    /// Reads what the desktop sent the device; the lock lights after it, when it set any. The echo of
    /// the helper's own write, by set_own_leds, is none of the desktop's.
    fn read_leds(&mut self) -> io::Result<Option<u16>>;
    /// Sets the device's own lights, as no desktop would have it: for the light probe, and to put them
    /// back after it. The kernel hands the write back to the device as though a reader had made it,
    /// and read_leds does not report that echo.
    fn set_own_leds(&mut self, leds: u16) -> io::Result<()>;
}

/// A light the helper wrote itself, as the kernel will hand it back.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
struct Echo {
    code: u16,
    on: bool,
}

/// The virtual keyboard's lights as the kernel holds them, followed from the events it hands the
/// device. The kernel hands over a light only when it changes, whoever changed it, and hands the
/// helper's own write back at once, within the write itself, ahead of anything a reader does after
/// it: so an echo awaited is looked for only in the next read, and the device is read before each
/// write of its own, so that what is awaited is exactly what changed.
#[derive(Debug, Default)]
pub struct Lights {
    state: u16,
    echoes: Vec<Echo>,
    /// A reader changed them since they were last reported.
    changed: bool,
}

impl Lights {
    pub fn state(&self) -> u16 {
        self.state
    }

    /// The helper sets the lights of `codes` to `leds`: the events to write, one for each light that
    /// changes, each of which comes back as an echo.
    pub fn own(&mut self, leds: u16, codes: &[u16]) -> Vec<(u16, i32)> {
        let mut events = Vec::new();
        for &code in codes.iter().filter(|c| **c < 16) {
            let on = leds & 1 << code != 0;
            if (self.state & 1 << code != 0) == on {
                continue;
            }
            self.flip(code, on);
            self.echoes.push(Echo { code, on });
            events.push((code, i32::from(on)));
        }
        events
    }

    /// One light the kernel handed the device: the echo of the helper's own write, or a reader's
    /// change.
    pub fn event(&mut self, code: u16, on: bool) {
        if code >= 16 {
            return;
        }
        let echo = Echo { code, on };
        // A reader's change of the light between the helper's read and its write, a matter of
        // microseconds, is taken for the echo, or comes ahead of it.
        if let Some(i) = self.echoes.iter().position(|e| e.code == code)
            && self.echoes[i] == echo
        {
            self.echoes.remove(i);
            self.flip(code, on);
            return;
        }
        self.flip(code, on);
        self.changed = true;
    }

    /// The end of a read of everything the kernel had for the device: the lights, when a reader
    /// changed them since they were last reported. An echo not read by now never comes: only one
    /// that changed nothing, which the kernel does not hand back, could be awaited still.
    pub fn read(&mut self) -> Option<u16> {
        self.echoes.clear();
        std::mem::take(&mut self.changed).then_some(self.state)
    }

    fn flip(&mut self, code: u16, on: bool) {
        if on {
            self.state |= 1 << code;
        } else {
            self.state &= !(1 << code);
        }
    }
}

/// Shared with the watchdog thread, which uses it when the helper's loop has stopped.
pub trait Guard: Send + Sync {
    /// Whether any keyboard is grabbed, or any key is down on the virtual keyboard.
    fn holding(&self) -> bool;
    /// Lets go of every grabbed keyboard at once, and destroys the virtual keyboard, which releases
    /// every key it holds down, from any thread, whatever the loop is doing.
    fn release_all(&self);
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::keys::{LED_CAPSL, LED_COMPOSE, LED_KANA, LED_NUML, LED_SCROLLL, LEDS};

    const ALL: u16 = 0b1_1111;
    const NUM: u16 = 1 << LED_NUML;
    const CAPS: u16 = 1 << LED_CAPSL;

    /// The kernel's side: what it hands the device for a write of lights, only those that change.
    struct Kernel(u16);

    impl Kernel {
        fn write(&mut self, lights: &[(u16, i32)]) -> Vec<(u16, bool)> {
            let mut handed = Vec::new();
            for &(code, value) in lights {
                let on = value != 0;
                if (self.0 & 1 << code != 0) != on {
                    self.0 ^= 1 << code;
                    handed.push((code, on));
                }
            }
            handed
        }

        fn reader(&mut self, leds: u16) -> Vec<(u16, bool)> {
            let lights: Vec<(u16, i32)> = LEDS
                .iter()
                .map(|c| (*c, i32::from(leds & 1 << c != 0)))
                .collect();
            self.write(&lights)
        }
    }

    fn hand(lights: &mut Lights, handed: Vec<(u16, bool)>) {
        for (code, on) in handed {
            lights.event(code, on);
        }
    }

    #[test]
    fn with_no_write_of_its_own_every_change_is_reported_as_before() {
        let (mut lights, mut kernel) = (Lights::default(), Kernel(0));
        assert_eq!(lights.read(), None);
        hand(&mut lights, kernel.reader(NUM));
        assert_eq!(lights.read(), Some(NUM));
        assert_eq!(lights.read(), None);
        hand(&mut lights, kernel.reader(NUM | CAPS));
        hand(&mut lights, kernel.reader(0));
        assert_eq!(lights.read(), Some(0));
    }

    #[test]
    fn the_echo_of_its_own_write_is_not_a_change() {
        let (mut lights, mut kernel) = (Lights::default(), Kernel(0));
        let written = lights.own(ALL, &LEDS);
        assert_eq!(written.len(), 5);
        hand(&mut lights, kernel.write(&written));
        assert_eq!(
            lights.read(),
            None,
            "the echo was taken for a reader's write"
        );
        assert_eq!((lights.state(), kernel.0), (ALL, ALL));
        // Lights already as asked are not written, and so no echo is awaited for them.
        assert!(lights.own(ALL, &LEDS).is_empty());
        // Put back: the echo again is nobody's.
        let back = lights.own(0, &LEDS);
        hand(&mut lights, kernel.write(&back));
        assert_eq!(lights.read(), None);
        assert_eq!((lights.state(), kernel.0), (0, 0));
    }

    #[test]
    fn a_readers_write_after_the_echo_is_a_change_in_the_same_read() {
        for desktop in [0, NUM, 1 << LED_SCROLLL | 1 << LED_COMPOSE] {
            let (mut lights, mut kernel) = (Lights::default(), Kernel(0));
            let written = lights.own(ALL, &LEDS);
            let mut handed = kernel.write(&written);
            handed.extend(kernel.reader(desktop));
            hand(&mut lights, handed);
            assert_eq!(lights.read(), Some(desktop), "{desktop:#b}");
            assert_eq!(lights.state(), kernel.0);
        }
    }

    #[test]
    fn a_reader_that_writes_what_is_there_already_is_not_heard() {
        let (mut lights, mut kernel) = (Lights::default(), Kernel(0));
        let written = lights.own(ALL, &LEDS);
        hand(&mut lights, kernel.write(&written));
        assert_eq!(lights.read(), None);
        // A desktop that lights every light, which none does, changes nothing the kernel hands on.
        hand(&mut lights, kernel.reader(ALL));
        assert_eq!(lights.read(), None);
        // Some written, Kana off among them: that one is heard.
        hand(&mut lights, kernel.write(&[(LED_NUML, 1), (LED_KANA, 0)]));
        assert_eq!(lights.read(), Some(ALL & !(1 << LED_KANA)));
    }

    #[test]
    fn an_echo_awaited_is_forgotten_once_read() {
        let mut lights = Lights::default();
        lights.own(CAPS, &LEDS);
        // The echo never came, as when a reader the helper had not read had lit the light already:
        // the next read forgets it, and a reader's later writes are heard.
        assert_eq!(lights.read(), None);
        lights.event(LED_CAPSL, false);
        lights.event(LED_CAPSL, true);
        assert_eq!(lights.read(), Some(CAPS));
    }

    #[test]
    fn lights_beyond_sixteen_are_left_alone() {
        let mut lights = Lights::default();
        lights.event(20, true);
        assert_eq!(lights.read(), None);
        assert!(lights.own(ALL, &[20]).is_empty());
    }
}
