//! linda-keyd, the helper that takes keys in the kernel for Linda: it grabs the real keyboards
//! while Linda, as the client of the session in front, asks it to, sends each key to Linda, waits
//! a bounded time for Linda to keep it or pass it, and puts out on a virtual keyboard what Linda
//! does not keep.
//!
//! A keyboard grabbed by a program that stops making progress cannot be used at all, not even to
//! report it, so most of what is here is about letting go. The layers, from the inside out:
//! - the logic in [`core`], which lets go whenever the client, its session or the virtual keyboard
//!   gives cause, and passes on any key the client does not decide in time;
//! - the emergency chord, read in the same path as every key, before anything else;
//! - the watchdog thread in [`watchdog`], which lets go of every keyboard and ends the helper when
//!   its loop stops turning;
//! - systemd's watchdog, which the loop feeds, and which kills a helper whose loop has stopped;
//! - the kernel, which ends the grabs of a dead process and destroys its virtual keyboard,
//!   releasing every key that keyboard held down.
//!
//! [`protocol`] is the whole of what a client needs.

macro_rules! log {
    ($($t:tt)*) => {
        eprintln!("linda-keyd: {}", format_args!($($t)*))
    };
}

pub mod core;
pub mod device;
pub mod evdev;
pub mod keys;
pub mod protocol;
pub mod server;
pub mod session;
pub mod watchdog;

#[cfg(test)]
mod client;
#[cfg(test)]
mod mock;
