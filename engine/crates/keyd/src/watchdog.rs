//! The watchdogs. Inside the helper, a thread of its own watches the loop: when the loop stops
//! turning, or the oldest key in line stays undecided long past its bound, while a keyboard is
//! grabbed, the thread lets go of every keyboard itself and ends the helper, so that the kernel
//! destroys the virtual keyboard and with it every key it held down. Outside, systemd's watchdog
//! kills a helper whose loop stops feeding it, which covers the thread itself.

use std::io;
use std::os::unix::net::UnixDatagram;
use std::sync::Arc;
use std::sync::atomic::{AtomicU64, Ordering::Relaxed};
use std::thread;
use std::time::{Duration, Instant};

use crate::device::Guard;
use crate::protocol::{STALL_LIMIT, VERDICT_TIMEOUT, WATCHDOG_CHECK};

const NONE: u64 = u64::MAX;

/// What the loop tells the watchdog thread, in milliseconds from a common start.
pub struct Progress {
    start: Instant,
    turned: AtomicU64,
    oldest: AtomicU64,
}

impl Progress {
    pub fn new() -> Arc<Progress> {
        Arc::new(Progress {
            start: Instant::now(),
            turned: AtomicU64::new(0),
            oldest: AtomicU64::new(NONE),
        })
    }

    fn ms(&self, t: Instant) -> u64 {
        u64::try_from(t.saturating_duration_since(self.start).as_millis()).unwrap_or(NONE - 1)
    }

    /// The loop has turned at `now`, and the oldest press in line was read at `oldest`.
    pub fn turned(&self, now: Instant, oldest: Option<Instant>) {
        self.turned.store(self.ms(now), Relaxed);
        self.oldest
            .store(oldest.map_or(NONE, |t| self.ms(t)), Relaxed);
    }

    /// What is wrong at `now`, if anything.
    pub fn stalled(&self, now: Instant) -> Option<String> {
        let now = self.ms(now);
        let turned = self.turned.load(Relaxed);
        let limit = STALL_LIMIT.as_millis() as u64;
        if now.saturating_sub(turned) > limit {
            return Some(format!(
                "the loop has not turned for {} ms",
                now.saturating_sub(turned)
            ));
        }
        let oldest = self.oldest.load(Relaxed);
        let bound = limit + VERDICT_TIMEOUT.as_millis() as u64;
        if oldest != NONE && now.saturating_sub(oldest) > bound {
            return Some(format!(
                "a key has been waiting for {} ms",
                now.saturating_sub(oldest)
            ));
        }
        None
    }
}

/// Starts the watchdog thread. When it finds the loop stalled while a keyboard is grabbed, it lets
/// go through `guard` and then runs `end`, which in the helper ends the process.
pub fn spawn(
    progress: Arc<Progress>,
    guard: Arc<dyn Guard>,
    end: Box<dyn FnOnce() + Send>,
) -> io::Result<thread::JoinHandle<()>> {
    thread::Builder::new()
        .name("watchdog".into())
        .spawn(move || {
            loop {
                thread::sleep(WATCHDOG_CHECK);
                if !guard.holding() {
                    continue;
                }
                if let Some(why) = progress.stalled(Instant::now()) {
                    guard.release_all();
                    log!("watchdog: {why}; let go of every keyboard, and ending");
                    end();
                    return;
                }
            }
        })
}

/// systemd's notification socket, when the helper runs as a service of type notify.
pub struct Notifier {
    socket: UnixDatagram,
    /// How often to feed systemd's watchdog: half its timeout, as sd_watchdog_enabled advises.
    interval: Option<Duration>,
    next: Instant,
}

impl Notifier {
    /// From NOTIFY_SOCKET, WATCHDOG_USEC and WATCHDOG_PID, as systemd sets them; None when not
    /// started by systemd.
    pub fn from_env() -> Option<Notifier> {
        let path = std::env::var_os("NOTIFY_SOCKET")?;
        let interval = std::env::var("WATCHDOG_USEC")
            .ok()
            .and_then(|v| v.parse::<u64>().ok())
            .filter(|_| {
                std::env::var("WATCHDOG_PID")
                    .ok()
                    .is_none_or(|p| p.parse() == Ok(std::process::id()))
            })
            .map(|usec| Duration::from_micros(usec) / 2);
        match Notifier::connect(path.as_encoded_bytes(), interval) {
            Ok(n) => Some(n),
            Err(e) => {
                log!("cannot reach systemd's notification socket: {e}");
                None
            }
        }
    }

    fn connect(path: &[u8], interval: Option<Duration>) -> io::Result<Notifier> {
        let socket = UnixDatagram::unbound()?;
        match path.strip_prefix(b"@") {
            Some(name) => {
                use std::os::linux::net::SocketAddrExt;
                let addr = std::os::unix::net::SocketAddr::from_abstract_name(name)?;
                socket.connect_addr(&addr)?;
            }
            None => {
                use std::os::unix::ffi::OsStrExt;
                socket.connect(std::ffi::OsStr::from_bytes(path))?;
            }
        }
        socket.set_nonblocking(true)?;
        Ok(Notifier {
            socket,
            interval,
            next: Instant::now(),
        })
    }

    pub fn send(&self, state: &str) {
        if let Err(e) = self.socket.send(state.as_bytes()) {
            log!("cannot tell systemd {state}: {e}");
        }
    }

    /// Feeds systemd's watchdog when it is due.
    pub fn feed(&mut self, now: Instant) {
        if let Some(i) = self.interval
            && now >= self.next
        {
            self.send("WATCHDOG=1");
            self.next = now + i;
        }
    }

    pub fn next_feed(&self) -> Option<Instant> {
        self.interval.map(|_| self.next)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Mutex;
    use std::sync::atomic::AtomicBool;

    struct FakeGuard {
        holding: AtomicBool,
        released: AtomicBool,
    }

    impl Guard for FakeGuard {
        fn holding(&self) -> bool {
            self.holding.load(Relaxed)
        }
        fn release_all(&self) {
            self.holding.store(false, Relaxed);
            self.released.store(true, Relaxed);
        }
    }

    #[test]
    fn a_loop_that_turns_is_not_stalled() {
        let p = Progress::new();
        let t = Instant::now();
        p.turned(t, None);
        assert_eq!(p.stalled(t + STALL_LIMIT), None);
        assert!(
            p.stalled(t + STALL_LIMIT + Duration::from_millis(2))
                .is_some()
        );
        p.turned(t + STALL_LIMIT, Some(t));
        let why = p.stalled(t + STALL_LIMIT + VERDICT_TIMEOUT + Duration::from_millis(2));
        assert!(why.unwrap().contains("a key has been waiting"));
    }

    #[test]
    fn the_watchdog_lets_go_and_ends_a_stalled_helper_that_holds_a_keyboard() {
        let p = Progress::new();
        let guard = Arc::new(FakeGuard {
            holding: AtomicBool::new(false),
            released: AtomicBool::new(false),
        });
        let ended = Arc::new(Mutex::new(None));
        let e = ended.clone();
        let started = Instant::now();
        p.turned(started, None);
        let thread = spawn(
            p.clone(),
            guard.clone(),
            Box::new(move || *e.lock().unwrap() = Some(Instant::now())),
        )
        .unwrap();
        // Not holding anything: a stalled loop harms nobody, and nothing happens.
        thread::sleep(STALL_LIMIT + WATCHDOG_CHECK * 3);
        assert!(ended.lock().unwrap().is_none());
        // Holding, and turning: nothing happens.
        guard.holding.store(true, Relaxed);
        let turning = Instant::now();
        while turning.elapsed() < STALL_LIMIT * 2 {
            p.turned(Instant::now(), None);
            thread::sleep(WATCHDOG_CHECK);
        }
        assert!(ended.lock().unwrap().is_none());
        // Holding, and stopped.
        let stopped = Instant::now();
        p.turned(stopped, None);
        thread.join().unwrap();
        assert!(guard.released.load(Relaxed));
        let at = ended.lock().unwrap().expect("ended");
        let after = at - stopped;
        assert!(
            after > STALL_LIMIT && after < STALL_LIMIT + Duration::from_secs(1),
            "{after:?}"
        );
        assert!(!guard.holding());
    }

    #[test]
    fn systemd_is_told_and_fed() {
        let dir = std::env::temp_dir().join(format!("keyd-notify-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir(&dir).unwrap();
        let path = dir.join("notify");
        let systemd = UnixDatagram::bind(&path).unwrap();
        systemd
            .set_read_timeout(Some(Duration::from_secs(5)))
            .unwrap();
        let mut n = Notifier::connect(
            path.as_os_str().as_encoded_bytes(),
            Some(Duration::from_millis(500)),
        )
        .unwrap();
        n.send("READY=1");
        let t = Instant::now();
        n.feed(t);
        n.feed(t + Duration::from_millis(100));
        n.feed(t + Duration::from_millis(500));
        assert_eq!(n.next_feed(), Some(t + Duration::from_millis(1000)));
        let mut got = Vec::new();
        let mut b = [0u8; 64];
        for _ in 0..3 {
            let len = systemd.recv(&mut b).unwrap();
            got.push(String::from_utf8_lossy(&b[..len]).into_owned());
        }
        assert_eq!(got, ["READY=1", "WATCHDOG=1", "WATCHDOG=1"]);
        let _ = std::fs::remove_dir_all(&dir);
        // An abstract socket, as systemd may give.
        let name = format!("keyd-notify-test-{}", std::process::id());
        use std::os::linux::net::SocketAddrExt;
        let addr = std::os::unix::net::SocketAddr::from_abstract_name(name.as_bytes()).unwrap();
        let systemd = UnixDatagram::bind_addr(&addr).unwrap();
        let n = Notifier::connect(format!("@{name}").as_bytes(), None).unwrap();
        n.send("READY=1");
        let len = systemd.recv(&mut b).unwrap();
        assert_eq!(&b[..len], b"READY=1");
        assert_eq!(n.next_feed(), None);
    }
}
