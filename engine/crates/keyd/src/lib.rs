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
        $crate::log_line(format_args!($($t)*))
    };
}

/// The longest log line; the rest of a longer one is cut.
const LINE_MAX: usize = 512;

/// Logs a line to standard error, which under systemd is a socket to the journal, without ever
/// waiting: a line that cannot be written at once is dropped. The line is made on the stack and
/// written with one call, taking no lock and allocating nothing, since lines are logged on the
/// paths that let go of the keyboards and end the helper, where the journal not reading, or a
/// stalled thread holding std's lock on standard error or the allocator's, must not hold them up.
pub fn log_line(line: std::fmt::Arguments) {
    use std::fmt::Write;
    #[cfg(test)]
    {
        LOGGED.with(|n| n.set(n.get() + 1));
        if !RAW_STDERR.load(std::sync::atomic::Ordering::Relaxed) {
            eprintln!("linda-keyd: {line}");
            return;
        }
    }
    let mut out = Line {
        buf: [0; LINE_MAX],
        len: 0,
    };
    let _ = write!(out, "linda-keyd: {line}");
    out.buf[out.len] = b'\n';
    write_now(2, &out.buf[..=out.len]);
}

struct Line {
    buf: [u8; LINE_MAX],
    len: usize,
}

impl std::fmt::Write for Line {
    fn write_str(&mut self, s: &str) -> std::fmt::Result {
        // One byte is kept for the newline.
        let n = s.len().min(LINE_MAX - 1 - self.len);
        self.buf[self.len..self.len + n].copy_from_slice(&s.as_bytes()[..n]);
        self.len += n;
        Ok(())
    }
}

/// Writes `b` to `fd` if it can be written now. A socket is sent to without waiting; anything else,
/// a terminal or a pipe when the helper is run by hand, is written to only when poll says it can
/// take more now.
fn write_now(fd: std::os::fd::RawFd, b: &[u8]) {
    // SAFETY: b is valid for its length; the flags make the call return at once.
    let sent = unsafe {
        libc::send(
            fd,
            b.as_ptr().cast(),
            b.len(),
            libc::MSG_DONTWAIT | libc::MSG_NOSIGNAL,
        )
    };
    if sent >= 0 || std::io::Error::last_os_error().raw_os_error() != Some(libc::ENOTSOCK) {
        return;
    }
    let mut p = libc::pollfd {
        fd,
        events: libc::POLLOUT,
        revents: 0,
    };
    // SAFETY: one pollfd, waiting for nothing.
    if unsafe { libc::poll(&mut p, 1, 0) } == 1 && p.revents & libc::POLLOUT != 0 {
        // SAFETY: b is valid for its length.
        unsafe { libc::write(fd, b.as_ptr().cast(), b.len()) };
    }
}

/// Ends the process at once after a panic on any thread: a thread that panicked would leave its
/// part undone while the rest went on, the watchdog among them, so the whole helper ends instead
/// and the kernel lets go of what it held.
pub fn end_on_panic() {
    std::panic::set_hook(Box::new(|info| {
        log_line(format_args!("{info}"));
        // SAFETY: ends the process at once, which is all that is wanted here.
        unsafe { libc::_exit(PANIC_EXIT) };
    }));
}

/// The exit status after a panic on any thread.
pub const PANIC_EXIT: i32 = 101;

/// In tests, lines go through std's standard error, which the test harness captures, unless a test
/// of the helper's own way of writing sets this.
#[cfg(test)]
static RAW_STDERR: std::sync::atomic::AtomicBool = std::sync::atomic::AtomicBool::new(false);

#[cfg(test)]
thread_local! {
    /// How many lines this thread has logged, for the tests of what reaches the log.
    static LOGGED: std::cell::Cell<usize> = const { std::cell::Cell::new(0) };
}

#[cfg(test)]
fn logged() -> usize {
    LOGGED.with(|n| n.get())
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
