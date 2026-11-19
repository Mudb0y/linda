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
    /// Reads what the desktop sent the device; the lock lights after it, when it set any.
    fn read_leds(&mut self) -> io::Result<Option<u16>>;
}

/// Shared with the watchdog thread, which uses it when the helper's loop has stopped.
pub trait Guard: Send + Sync {
    /// Whether any keyboard is grabbed.
    fn holding(&self) -> bool;
    /// Lets go of every grabbed keyboard at once, from any thread, whatever the loop is doing.
    fn release_all(&self);
}
