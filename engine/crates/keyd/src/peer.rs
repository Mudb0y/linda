//! The process at the other end of a connection, held by a pidfd taken as the helper accepts it.
//! Admission asks logind and polkit about a process by its number, which another process can take
//! once the caller has ended; a pidfd stays with the process that connected, and says when that
//! process has ended, so a caller cannot be judged by whatever process has its number by then.

use std::io;
use std::os::fd::{AsFd, AsRawFd, BorrowedFd, FromRawFd, OwnedFd};
use std::sync::atomic::{AtomicBool, Ordering::Relaxed};

use rustix::event::{PollFd, PollFlags, Timespec};

/// The socket option that gives a pidfd for the peer, from Linux 6.5; libc does not name it yet.
#[cfg(any(target_arch = "sparc", target_arch = "sparc64"))]
const SO_PEERPIDFD: libc::c_int = 0x0056;
#[cfg(not(any(target_arch = "sparc", target_arch = "sparc64")))]
const SO_PEERPIDFD: libc::c_int = 77;

/// What the helper holds of the process that connected.
#[derive(Debug)]
pub enum Process {
    /// A pidfd for it.
    Held(OwnedFd),
    /// It had ended, and been reaped, before the helper could take a pidfd for it.
    Ended,
    /// The kernel gave no pidfd, being older than 6.5 or out of descriptors: admission has only the
    /// process's start time to go by, which cannot tell it from a process that took its number
    /// before it was first looked at.
    Unheld,
}

/// Whether the helper has said that it judges a caller without a pidfd.
static TOLD_UNHELD: AtomicBool = AtomicBool::new(false);

impl Process {
    /// The process at the other end of a connected socket: the one that connected, which the kernel
    /// recorded then.
    pub fn of(socket: BorrowedFd) -> Process {
        Process::from_answer(peer_pidfd(socket))
    }

    fn from_answer(answer: io::Result<OwnedFd>) -> Process {
        match answer {
            Ok(fd) => Process::Held(fd),
            // How an older kernel says the peer has been reaped: EINVAL or ESRCH by version, or
            // ENOENT for a number no longer a thread group's. A newer one, 6.18 among them, gives
            // a pidfd all the same, which reads as ended.
            Err(e)
                if matches!(
                    e.raw_os_error(),
                    Some(libc::ESRCH | libc::EINVAL | libc::ENOENT)
                ) =>
            {
                Process::Ended
            }
            Err(e) => {
                if !TOLD_UNHELD.swap(true, Relaxed) {
                    log!(
                        "the kernel gives no pidfd for a caller ({e}), so callers are judged by \
                         their start time alone"
                    );
                }
                Process::Unheld
            }
        }
    }

    pub fn pidfd(&self) -> Option<BorrowedFd<'_>> {
        match self {
            Process::Held(fd) => Some(fd.as_fd()),
            _ => None,
        }
    }

    /// Whether the process is known to have ended. While it has not, it keeps its number, so
    /// whatever was looked up by that number since it connected was looked up of it. Without a
    /// pidfd nothing is known.
    pub fn ended(&self) -> bool {
        match self {
            Process::Held(fd) => has_ended(fd.as_fd()),
            Process::Ended => true,
            Process::Unheld => false,
        }
    }
}

fn peer_pidfd(socket: BorrowedFd) -> io::Result<OwnedFd> {
    let mut fd: libc::c_int = -1;
    let mut len = size_of::<libc::c_int>() as libc::socklen_t;
    // SAFETY: fd and len are valid for writes of the sizes given.
    let r = unsafe {
        libc::getsockopt(
            socket.as_raw_fd(),
            libc::SOL_SOCKET,
            SO_PEERPIDFD,
            (&raw mut fd).cast(),
            &mut len,
        )
    };
    if r != 0 {
        return Err(io::Error::last_os_error());
    }
    if fd < 0 {
        return Err(io::Error::other(
            "the kernel answered SO_PEERPIDFD with no descriptor",
        ));
    }
    // SAFETY: the kernel has just made this descriptor for the helper, and nothing else owns it.
    Ok(unsafe { OwnedFd::from_raw_fd(fd) })
}

/// Whether the process a pidfd refers to has ended: the pidfd reads as ready from the moment it
/// has, whether or not its parent has reaped it. A poll that fails counts as ended, so that a
/// caller is never admitted on a question that went unanswered.
pub fn has_ended(pidfd: BorrowedFd) -> bool {
    let mut fds = [PollFd::from_borrowed_fd(pidfd, PollFlags::IN)];
    let now = Timespec {
        tv_sec: 0,
        tv_nsec: 0,
    };
    loop {
        match rustix::event::poll(&mut fds, Some(&now)) {
            Ok(_) => return !fds[0].revents().is_empty(),
            Err(rustix::io::Errno::INTR) => {}
            Err(_) => return true,
        }
    }
}

#[cfg(test)]
pub(crate) mod tests {
    use super::*;

    /// A pidfd for the process given, as pidfd_open makes one.
    pub(crate) fn pidfd_of(pid: libc::pid_t) -> OwnedFd {
        // SAFETY: pidfd_open takes a number and flags, and makes a descriptor or fails.
        let fd = unsafe { libc::syscall(libc::SYS_pidfd_open, pid, 0) };
        assert!(fd >= 0, "pidfd_open: {}", io::Error::last_os_error());
        // SAFETY: the descriptor is new and ours alone.
        unsafe { OwnedFd::from_raw_fd(fd as i32) }
    }

    /// A child that ends at once, and a pidfd taken for it before it can be reaped.
    fn ended_child() -> (libc::pid_t, OwnedFd) {
        // SAFETY: the child makes one call that is safe after fork in a process with threads.
        let pid = unsafe { libc::fork() };
        assert!(pid >= 0, "fork: {}", io::Error::last_os_error());
        if pid == 0 {
            // SAFETY: ends the child at once.
            unsafe { libc::_exit(0) };
        }
        (pid, pidfd_of(pid))
    }

    fn reap(pid: libc::pid_t) {
        let mut status = 0;
        // SAFETY: waits for a child of this process.
        assert_eq!(unsafe { libc::waitpid(pid, &mut status, 0) }, pid);
    }

    #[test]
    fn a_living_process_has_not_ended() {
        let ours = Process::Held(pidfd_of(std::process::id() as libc::pid_t));
        assert!(!ours.ended());
        assert!(ours.pidfd().is_some());
    }

    #[test]
    fn a_process_that_ended_has_ended_whether_or_not_it_was_reaped() {
        let (pid, fd) = ended_child();
        let child = Process::Held(fd);
        let since = std::time::Instant::now();
        while !child.ended() {
            assert!(
                since.elapsed() < std::time::Duration::from_secs(10),
                "the child never ended"
            );
            std::thread::sleep(std::time::Duration::from_millis(5));
        }
        reap(pid);
        assert!(child.ended());
    }

    #[test]
    fn the_kernels_answers_are_read_as_they_mean() {
        for errno in [libc::ESRCH, libc::EINVAL, libc::ENOENT] {
            let p = Process::from_answer(Err(io::Error::from_raw_os_error(errno)));
            assert!(matches!(p, Process::Ended), "{errno}");
            assert!(p.ended() && p.pidfd().is_none());
        }
        // A kernel before 6.5, or a helper out of descriptors: the start time has to do.
        for errno in [libc::ENOPROTOOPT, libc::EMFILE, libc::ENODATA] {
            let p = Process::from_answer(Err(io::Error::from_raw_os_error(errno)));
            assert!(matches!(p, Process::Unheld), "{errno}");
            assert!(!p.ended() && p.pidfd().is_none());
        }
    }

    #[test]
    fn the_peer_of_a_socket_is_the_process_that_connected() {
        let (a, b) = rustix::net::socketpair(
            rustix::net::AddressFamily::UNIX,
            rustix::net::SocketType::SEQPACKET,
            rustix::net::SocketFlags::CLOEXEC,
            None,
        )
        .unwrap();
        let p = Process::of(a.as_fd());
        let Process::Held(fd) = &p else {
            panic!("no pidfd: {p:?}");
        };
        assert_eq!(pid_in(fd.as_fd()), std::process::id() as i64);
        assert!(!p.ended());
        drop(b);
    }

    /// The number of the process a pidfd refers to, by its entry in /proc/self/fdinfo.
    pub(crate) fn pid_in(fd: impl AsRawFd) -> i64 {
        let info =
            std::fs::read_to_string(format!("/proc/self/fdinfo/{}", fd.as_raw_fd())).unwrap();
        info.lines()
            .find_map(|l| l.strip_prefix("Pid:"))
            .expect("a pidfd's fdinfo names its process")
            .trim()
            .parse()
            .unwrap()
    }
}
