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
    write_whole(&CUT, b, |b| {
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
            return sent;
        }
        let mut p = libc::pollfd {
            fd,
            events: libc::POLLOUT,
            revents: 0,
        };
        // SAFETY: one pollfd, waiting for nothing.
        if unsafe { libc::poll(&mut p, 1, 0) } == 1 && p.revents & libc::POLLOUT != 0 {
            // SAFETY: b is valid for its length.
            unsafe { libc::write(fd, b.as_ptr().cast(), b.len()) }
        } else {
            -1
        }
    });
}

/// A line was cut short: on a stream, such as the journal's, the next would run on from it.
static CUT: std::sync::atomic::AtomicBool = std::sync::atomic::AtomicBool::new(false);

/// Writes a line with `write`, which says how much it took, or less than nothing. After a line that
/// was cut short, a newline goes first, and lines are dropped until it has gone, so that every line
/// starts whole.
fn write_whole(
    cut: &std::sync::atomic::AtomicBool,
    b: &[u8],
    mut write: impl FnMut(&[u8]) -> isize,
) {
    use std::sync::atomic::Ordering::Relaxed;
    if cut.load(Relaxed) {
        if write(b"\n") != 1 {
            return;
        }
        cut.store(false, Relaxed);
    }
    let took = write(b);
    if took >= 0 && (took as usize) < b.len() {
        cut.store(true, Relaxed);
    }
}

#[cfg(test)]
#[test]
fn a_line_cut_short_is_ended_before_the_next_and_nothing_runs_on() {
    use std::sync::atomic::AtomicBool;
    let cut = AtomicBool::new(false);
    let mut stream = Vec::new();
    let mut room = 12;
    let write = |stream: &mut Vec<u8>, room: &mut usize, b: &[u8]| {
        let n = b.len().min(*room);
        stream.extend_from_slice(&b[..n]);
        *room -= n;
        n as isize
    };
    write_whole(&cut, b"first line\n", |b| write(&mut stream, &mut room, b));
    write_whole(&cut, b"second line\n", |b| write(&mut stream, &mut room, b));
    // Full: the next line is dropped, newline and all.
    write_whole(&cut, b"third line\n", |b| write(&mut stream, &mut room, b));
    room = 100;
    write_whole(&cut, b"fourth line\n", |b| write(&mut stream, &mut room, b));
    assert_eq!(
        String::from_utf8(stream).unwrap(),
        "first line\ns\nfourth line\n"
    );
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
pub mod peer;
pub mod protocol;
pub mod server;
pub mod session;
pub mod watchdog;

#[cfg(test)]
mod client;
#[cfg(test)]
mod mock;
